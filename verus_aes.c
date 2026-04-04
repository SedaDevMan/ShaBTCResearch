/*
 * verus_aes.c — VerusHash-inspired AES-based PoW hash
 *
 * Tests the research question: does reduced-round AES (like Haraka in
 * VerusHash 2.1) leak exploitable statistical patterns compared to
 * full-round AES or SHA3?
 *
 * Pipeline per (prev_hash[32], nonce_uint32):
 *   1. key[16] = SHA3-256(prev_hash)[0:16]      — per-block AES-128 key
 *   2. block[16] = nonce_LE4 || prev_hash[0:12] — nonce in FIRST 4 bytes (row 0)
 *      Row 0 in AES ShiftRows = no shift → nonce directly affects out[0..3]
 *      even with N=1 round (aesenclast, no MixColumns)
 *   3. state = AddRoundKey(block, rk[0])
 *      then (N-1) × _mm_aesenc_si128
 *      then 1   × _mm_aesenclast_si128 with rk[N]
 *   4. output[32] = state[16] || SHA3-256(state)[16]
 *      (pad to 32 bytes: AES output padded with its own hash)
 *
 * With N=1:  minimal diffusion, SubBytes+ShiftRows+AddRoundKey only (no MixColumns in last round)
 * With N=2:  one full round + one last-round
 * With N=10: full AES-128 (cryptographically strong)
 *
 * Exposed Python functions:
 *   verus_hash(prev_hash_bytes, nonce_uint32, n_rounds) -> bytes(32)
 *   scan_winners(prev_hash_bytes, scan_range, target_bytes, n_rounds) -> list[int]
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <openssl/evp.h>
#include <wmmintrin.h>
#include <smmintrin.h>
#include <stdint.h>
#include <string.h>
#include <stdlib.h>

/* ── SHA3-256 helper ──────────────────────────────────────────────────────── */
static void sha3_256(const uint8_t *in, size_t len, uint8_t out[32])
{
    EVP_MD_CTX *ctx = EVP_MD_CTX_new();
    EVP_DigestInit_ex(ctx, EVP_sha3_256(), NULL);
    EVP_DigestUpdate(ctx, in, len);
    unsigned int l = 32;
    EVP_DigestFinal_ex(ctx, out, &l);
    EVP_MD_CTX_free(ctx);
}

/* ── AES-128 key expansion (standard) ────────────────────────────────────── */
static void aes128_expand(__m128i rk[11], const uint8_t key[16])
{
    rk[0] = _mm_loadu_si128((__m128i*)key);

#define KE_STEP(i, rcon) do {                                      \
    __m128i t = _mm_aeskeygenassist_si128(rk[i-1], rcon);         \
    t = _mm_shuffle_epi32(t, 0xff);                               \
    __m128i s = rk[i-1];                                          \
    s = _mm_xor_si128(s, _mm_slli_si128(s, 4));                   \
    s = _mm_xor_si128(s, _mm_slli_si128(s, 4));                   \
    s = _mm_xor_si128(s, _mm_slli_si128(s, 4));                   \
    rk[i] = _mm_xor_si128(s, t);                                  \
} while(0)

    KE_STEP(1,  0x01); KE_STEP(2,  0x02); KE_STEP(3,  0x04);
    KE_STEP(4,  0x08); KE_STEP(5,  0x10); KE_STEP(6,  0x20);
    KE_STEP(7,  0x40); KE_STEP(8,  0x80); KE_STEP(9,  0x1b);
    KE_STEP(10, 0x36);
#undef KE_STEP
}

/* ── N-round AES-128 encrypt one block ───────────────────────────────────── */
/* nrounds: total rounds (1..10); uses keys rk[0]..rk[nrounds] */
static void aes_nr(const __m128i rk[11], int nrounds,
                   const uint8_t in[16], uint8_t out[16])
{
    __m128i st = _mm_xor_si128(_mm_loadu_si128((__m128i*)in), rk[0]);
    for (int r = 1; r < nrounds; r++)
        st = _mm_aesenc_si128(st, rk[r]);
    st = _mm_aesenclast_si128(st, rk[nrounds]);
    _mm_storeu_si128((__m128i*)out, st);
}

/* ── Core hash function ───────────────────────────────────────────────────── */
/*
 * block[16] = prev[0:12] || nonce_LE4
 * Apply N-round AES with key = SHA3-256(prev)[0:16]
 * out[32] = aes_block[16] || SHA3-256(aes_block)[16]   (pad to 32 bytes)
 *
 * Note: the SHA3 padding only extends the output to 32 bytes for
 * comparison purposes; the primary randomness comes from the AES block.
 */
static void verus_compute(const uint8_t prev[32], uint32_t nonce,
                           int nrounds, uint8_t out[32],
                           const __m128i rk[11])
{
    /* Build 16-byte input block: nonce_LE4 first, then prev[0:12]
     * Nonce goes into row 0 of AES state (bytes 0-3 in row-major).
     * Row 0 has zero shift in ShiftRows → nonce affects out[0..3] even with N=1. */
    uint8_t block[16];
    block[0] = (uint8_t)(nonce);
    block[1] = (uint8_t)(nonce >> 8);
    block[2] = (uint8_t)(nonce >> 16);
    block[3] = (uint8_t)(nonce >> 24);
    memcpy(block + 4, prev, 12);

    /* N-round AES */
    uint8_t aes_out[16];
    aes_nr(rk, nrounds, block, aes_out);

    /* Extend to 32 bytes: aes_out || SHA3-256(aes_out)[0:16] */
    memcpy(out, aes_out, 16);
    uint8_t h[32];
    sha3_256(aes_out, 16, h);
    memcpy(out + 16, h, 16);
}

/* ── Python: verus_hash(prev_bytes, nonce_int, n_rounds) -> bytes(32) ──── */
static PyObject *py_verus_hash(PyObject *self, PyObject *args)
{
    const char *prev; Py_ssize_t plen;
    unsigned long nonce;
    int nrounds;
    if (!PyArg_ParseTuple(args, "y#ki", &prev, &plen, &nonce, &nrounds))
        return NULL;
    if (plen < 32) { PyErr_SetString(PyExc_ValueError, "need 32 bytes"); return NULL; }
    if (nrounds < 1 || nrounds > 10) { PyErr_SetString(PyExc_ValueError, "nrounds 1-10"); return NULL; }

    uint8_t key_mat[32];
    sha3_256((const uint8_t*)prev, 32, key_mat);
    __m128i rk[11];
    aes128_expand(rk, key_mat);

    uint8_t out[32];
    verus_compute((const uint8_t*)prev, (uint32_t)nonce, nrounds, out, rk);
    return PyBytes_FromStringAndSize((char*)out, 32);
}

/* ── Python: scan_winners(prev_bytes, scan_range, target_bytes, n_rounds) ── */
static PyObject *py_scan_winners(PyObject *self, PyObject *args)
{
    const char *prev;   Py_ssize_t plen;
    uint64_t    scan_range;
    const char *target; Py_ssize_t tlen;
    int         nrounds;

    if (!PyArg_ParseTuple(args, "y#Ky#i",
                          &prev, &plen,
                          &scan_range,
                          &target, &tlen,
                          &nrounds))
        return NULL;

    if (plen < 32) { PyErr_SetString(PyExc_ValueError, "need 32 bytes prev"); return NULL; }
    if (tlen != 32) { PyErr_SetString(PyExc_ValueError, "target 32 bytes"); return NULL; }
    if (nrounds < 1 || nrounds > 10) { PyErr_SetString(PyExc_ValueError, "nrounds 1-10"); return NULL; }

    /* Derive key once per prev_hash */
    uint8_t key_mat[32];
    sha3_256((const uint8_t*)prev, 32, key_mat);
    __m128i rk[11];
    aes128_expand(rk, key_mat);

    PyObject *winners = PyList_New(0);

    for (uint64_t nonce = 0; nonce < scan_range; nonce++) {
        uint8_t out[32];
        verus_compute((const uint8_t*)prev, (uint32_t)nonce, nrounds, out, rk);

        if (memcmp(out, target, 32) < 0) {
            PyObject *item = PyLong_FromUnsignedLongLong(nonce);
            PyList_Append(winners, item);
            Py_DECREF(item);
        }
    }

    return winners;
}

/* ── Python: derive_key(prev_bytes) -> bytes(16)  (expose AES key) ─────── */
static PyObject *py_derive_key(PyObject *self, PyObject *args)
{
    const char *prev; Py_ssize_t plen;
    if (!PyArg_ParseTuple(args, "y#", &prev, &plen)) return NULL;
    if (plen < 32) { PyErr_SetString(PyExc_ValueError, "need 32 bytes"); return NULL; }

    uint8_t key_mat[32];
    sha3_256((const uint8_t*)prev, 32, key_mat);
    return PyBytes_FromStringAndSize((char*)key_mat, 16);
}

/* ── Module ──────────────────────────────────────────────────────────────── */
static PyMethodDef VerusMethods[] = {
    {"verus_hash",    py_verus_hash,    METH_VARARGS, "verus_hash(prev32, nonce, nrounds) -> bytes(32)"},
    {"scan_winners",  py_scan_winners,  METH_VARARGS, "scan_winners(prev32, scan_range, target32, nrounds) -> list[int]"},
    {"derive_key",    py_derive_key,    METH_VARARGS, "derive_key(prev32) -> bytes(16)  AES key"},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef verusmodule = {
    PyModuleDef_HEAD_INIT, "verus_aes", NULL, -1, VerusMethods
};

PyMODINIT_FUNC PyInit_verus_aes(void) {
    return PyModule_Create(&verusmodule);
}
