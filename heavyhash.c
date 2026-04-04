/*
 * heavyhash.c — Kaspa HeavyHash Python C extension
 *
 * HeavyHash pipeline:
 *   1. SHA3-256(block_header + nonce_8bytes) → hash1[32]
 *   2. Unpack hash1 into 64 nibbles (4-bit values)
 *   3. product[i] = sum(matrix[i][j] * nibble[j], j=0..63) for i=0..63
 *   4. xored[i] = (product[i] & 0xF) XOR nibble[i]   (keep lower nibble)
 *   5. Pack xored nibbles back to 32 bytes → vec[32]
 *   6. SHA3-256(vec) → final_hash[32]
 *   7. final_hash < target → winner
 *
 * Matrix derivation (from pre_pow_hash[32]):
 *   - Expand pre_pow_hash via repeated SHA3-256 until we have 64*64 nibbles
 *   - Each nibble = 4 bits, values 0..15
 *   - matrix[i][j] = nibble (uint8, 0..15)
 *
 * Exposed Python functions:
 *   generate_matrix(pre_pow_hash_bytes) -> list[list[int]]  (64x64)
 *   heavyhash(matrix_flat, header_bytes, nonce_uint64)     -> bytes (32)
 *   scan_winners(matrix_flat, header_bytes, scan_range, target_bytes) -> list[int]
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <openssl/evp.h>
#include <stdint.h>
#include <string.h>

/* SHA3-256 via OpenSSL EVP */
static void sha3_256(const uint8_t *in, size_t in_len, uint8_t out[32])
{
    EVP_MD_CTX *ctx = EVP_MD_CTX_new();
    EVP_DigestInit_ex(ctx, EVP_sha3_256(), NULL);
    EVP_DigestUpdate(ctx, in, in_len);
    unsigned int len = 32;
    EVP_DigestFinal_ex(ctx, out, &len);
    EVP_MD_CTX_free(ctx);
}

/*
 * generate_matrix: expand pre_pow_hash into 64×64 nibble matrix.
 * We repeatedly SHA3-256 a counter-extended seed until we have
 * 64*64 = 4096 nibbles = 2048 bytes of material.
 */
static void generate_matrix(const uint8_t pre_pow[32], uint8_t mat[64][64])
{
    /* Need 4096 nibbles = 2048 bytes; SHA3-256 gives 32 bytes per call
       → need ceil(2048/32) = 64 calls */
    uint8_t buf[64 * 32];   /* 2048 bytes */
    uint8_t seed[36];
    memcpy(seed, pre_pow, 32);

    for (int i = 0; i < 64; i++) {
        seed[32] = (uint8_t)(i);
        seed[33] = (uint8_t)(i >> 8);
        seed[34] = (uint8_t)(i >> 16);
        seed[35] = (uint8_t)(i >> 24);
        sha3_256(seed, 36, buf + i * 32);
    }

    /* Unpack bytes into nibbles → matrix[row][col] */
    int idx = 0;
    for (int r = 0; r < 64; r++) {
        for (int c = 0; c < 64; c += 2) {
            uint8_t byte = buf[idx++];
            mat[r][c]     = (byte >> 4) & 0xF;
            mat[r][c + 1] =  byte       & 0xF;
        }
    }
}

/*
 * compute_heavyhash: given matrix and 80-byte header (nonce already set),
 * return 32-byte final hash.
 */
static void compute_heavyhash(const uint8_t mat[64][64],
                               const uint8_t *header, size_t hlen,
                               uint8_t out[32])
{
    /* Step 1: inner SHA3-256 */
    uint8_t hash1[32];
    sha3_256(header, hlen, hash1);

    /* Step 2: unpack to 64 nibbles */
    uint8_t nibbles[64];
    for (int i = 0; i < 32; i++) {
        nibbles[2*i]     = (hash1[i] >> 4) & 0xF;
        nibbles[2*i + 1] =  hash1[i]       & 0xF;
    }

    /* Step 3: matrix multiply — product[i] mod 16 */
    uint8_t product[64];
    for (int i = 0; i < 64; i++) {
        uint32_t acc = 0;
        for (int j = 0; j < 64; j++)
            acc += (uint32_t)mat[i][j] * (uint32_t)nibbles[j];
        product[i] = (uint8_t)(acc & 0xF);
    }

    /* Step 4: XOR product nibbles with original nibbles */
    uint8_t xored[64];
    for (int i = 0; i < 64; i++)
        xored[i] = product[i] ^ nibbles[i];

    /* Step 5: pack xored nibbles back to 32 bytes */
    uint8_t vec[32];
    for (int i = 0; i < 32; i++)
        vec[i] = (xored[2*i] << 4) | xored[2*i + 1];

    /* Step 6: outer SHA3-256 */
    sha3_256(vec, 32, out);
}

/* ── Python bindings ─────────────────────────────────────────────────────── */

/* generate_matrix(pre_pow_bytes) -> list[list[int]] 64×64 */
static PyObject *py_generate_matrix(PyObject *self, PyObject *args)
{
    const char *buf; Py_ssize_t len;
    if (!PyArg_ParseTuple(args, "y#", &buf, &len)) return NULL;
    if (len < 32) { PyErr_SetString(PyExc_ValueError, "need 32 bytes"); return NULL; }

    uint8_t mat[64][64];
    generate_matrix((const uint8_t*)buf, mat);

    PyObject *rows = PyList_New(64);
    for (int r = 0; r < 64; r++) {
        PyObject *row = PyList_New(64);
        for (int c = 0; c < 64; c++)
            PyList_SET_ITEM(row, c, PyLong_FromLong(mat[r][c]));
        PyList_SET_ITEM(rows, r, row);
    }
    return rows;
}

/* heavyhash(matrix_flat_bytes, header_bytes) -> bytes(32) */
static PyObject *py_heavyhash(PyObject *self, PyObject *args)
{
    const char *matbuf; Py_ssize_t matlen;
    const char *hdr;   Py_ssize_t hdrlen;
    if (!PyArg_ParseTuple(args, "y#y#", &matbuf, &matlen, &hdr, &hdrlen))
        return NULL;
    if (matlen != 64*64) { PyErr_SetString(PyExc_ValueError, "matrix must be 4096 bytes"); return NULL; }

    uint8_t mat[64][64];
    memcpy(mat, matbuf, 64*64);

    uint8_t out[32];
    compute_heavyhash(mat, (const uint8_t*)hdr, (size_t)hdrlen, out);
    return PyBytes_FromStringAndSize((char*)out, 32);
}

/* scan_winners(matrix_flat_bytes, header_prefix_bytes, scan_range, target_bytes)
   header_prefix = everything before the 8-byte nonce field
   nonce appended as 8-byte little-endian, iterated 0..scan_range
   returns list of winning nonces */
static PyObject *py_scan_winners(PyObject *self, PyObject *args)
{
    const char *matbuf;  Py_ssize_t matlen;
    const char *prefix;  Py_ssize_t pfxlen;
    uint64_t    scan_range;
    const char *target;  Py_ssize_t tlen;

    if (!PyArg_ParseTuple(args, "y#y#Ky#",
                          &matbuf, &matlen,
                          &prefix, &pfxlen,
                          &scan_range,
                          &target, &tlen))
        return NULL;

    if (matlen != 64*64) { PyErr_SetString(PyExc_ValueError, "matrix 4096 bytes"); return NULL; }
    if (tlen != 32)       { PyErr_SetString(PyExc_ValueError, "target 32 bytes"); return NULL; }

    uint8_t mat[64][64];
    memcpy(mat, matbuf, 64*64);

    /* Build header buffer: prefix + 8-byte nonce */
    size_t hdrlen = (size_t)pfxlen + 8;
    uint8_t *hdr  = (uint8_t*)malloc(hdrlen);
    memcpy(hdr, prefix, pfxlen);

    PyObject *winners = PyList_New(0);

    for (uint64_t nonce = 0; nonce < scan_range; nonce++) {
        /* Write nonce little-endian at end of header */
        hdr[pfxlen+0] = (uint8_t)(nonce);
        hdr[pfxlen+1] = (uint8_t)(nonce >> 8);
        hdr[pfxlen+2] = (uint8_t)(nonce >> 16);
        hdr[pfxlen+3] = (uint8_t)(nonce >> 24);
        hdr[pfxlen+4] = (uint8_t)(nonce >> 32);
        hdr[pfxlen+5] = (uint8_t)(nonce >> 40);
        hdr[pfxlen+6] = (uint8_t)(nonce >> 48);
        hdr[pfxlen+7] = (uint8_t)(nonce >> 56);

        uint8_t out[32];
        compute_heavyhash(mat, hdr, hdrlen, out);

        /* Compare big-endian: out < target */
        if (memcmp(out, target, 32) < 0) {
            PyObject *item = PyLong_FromUnsignedLongLong(nonce);
            PyList_Append(winners, item);
            Py_DECREF(item);
        }
    }

    free(hdr);
    return winners;
}

static PyMethodDef HeavyMethods[] = {
    {"generate_matrix", py_generate_matrix, METH_VARARGS, "Generate 64x64 matrix from pre_pow_hash"},
    {"heavyhash",       py_heavyhash,       METH_VARARGS, "Compute HeavyHash(matrix, header)"},
    {"scan_winners",    py_scan_winners,    METH_VARARGS, "Scan nonces for winners"},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef heavymodule = {
    PyModuleDef_HEAD_INIT, "heavyhash", NULL, -1, HeavyMethods
};

PyMODINIT_FUNC PyInit_heavyhash(void) {
    return PyModule_Create(&heavymodule);
}
