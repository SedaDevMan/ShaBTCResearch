/*
 * pot_skip.c — Unified intra-POT skip analysis
 *
 * Tests three algorithms for nonce-range structure:
 *
 *   1. SHA256d (reduced rounds: 1,2,4,8,16,32,64)
 *      - Like our VerusHash AES round test, but with SHA256
 *      - At what round count does intra-POT structure disappear?
 *
 *   2. ETHash-lite (Ethereum Classic style, simplified DAG)
 *      - DAG lookup indices depend on nonce via keccak
 *      - Do certain nonces systematically hit "low" DAG values?
 *
 *   3. Midstate scoring (SHA256d specific)
 *      - For each block header, compute midstate once
 *      - Does midstate value predict winner density?
 *
 * Core exposed function for all three:
 *   winner_density(prev_bytes, scan_range, target_bytes, algo, param)
 *     → list of (bucket_index, winner_count) for 256 nonce buckets
 *       bucket i = nonces [i*scan_range/256 .. (i+1)*scan_range/256)
 *
 * Uniformity test: if winners cluster in specific buckets → structure!
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <openssl/evp.h>
#include <openssl/sha.h>
#include <wmmintrin.h>
#include <smmintrin.h>
#include <stdint.h>
#include <string.h>
#include <stdlib.h>

/* ── SHA3-256 ─────────────────────────────────────────────────────────── */
static void sha3_256(const uint8_t *in, size_t len, uint8_t out[32]) {
    EVP_MD_CTX *ctx = EVP_MD_CTX_new();
    EVP_DigestInit_ex(ctx, EVP_sha3_256(), NULL);
    EVP_DigestUpdate(ctx, in, len);
    unsigned int l = 32;
    EVP_DigestFinal_ex(ctx, out, &l);
    EVP_MD_CTX_free(ctx);
}

/* ── SHA256 helpers ───────────────────────────────────────────────────── */

/* SHA256 initial state (FIPS 180-4) */
static const uint32_t SHA256_H0[8] = {
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
    0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19
};

static const uint32_t K[64] = {
    0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,
    0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
    0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,
    0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
    0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,
    0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
    0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,
    0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
    0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,
    0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
    0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,
    0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
    0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,
    0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
    0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,
    0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2
};

#define ROTR32(x,n) (((x)>>(n))|((x)<<(32-(n))))
#define CH(e,f,g)  (((e)&(f))^(~(e)&(g)))
#define MAJ(a,b,c) (((a)&(b))^((a)&(c))^((b)&(c)))
#define EP0(a)     (ROTR32(a,2)^ROTR32(a,13)^ROTR32(a,22))
#define EP1(e)     (ROTR32(e,6)^ROTR32(e,11)^ROTR32(e,25))
#define SIG0(x)    (ROTR32(x,7)^ROTR32(x,18)^((x)>>3))
#define SIG1(x)    (ROTR32(x,17)^ROTR32(x,19)^((x)>>10))

/*
 * sha256_compress_nr: SHA256 compression with configurable round count.
 * state[8] in/out, block[16] = 16 × uint32_t big-endian words.
 * nrounds: 1..64
 */
static void sha256_compress_nr(uint32_t state[8], const uint32_t block[16], int nrounds)
{
    uint32_t w[64];
    for (int i = 0; i < 16; i++) w[i] = block[i];
    for (int i = 16; i < 64; i++)
        w[i] = SIG1(w[i-2]) + w[i-7] + SIG0(w[i-15]) + w[i-16];

    uint32_t a=state[0], b=state[1], c=state[2], d=state[3];
    uint32_t e=state[4], f=state[5], g=state[6], h=state[7];

    int rounds = nrounds < 64 ? nrounds : 64;
    for (int i = 0; i < rounds; i++) {
        uint32_t t1 = h + EP1(e) + CH(e,f,g) + K[i] + w[i];
        uint32_t t2 = EP0(a) + MAJ(a,b,c);
        h=g; g=f; f=e; e=d+t1;
        d=c; c=b; b=a; a=t1+t2;
    }
    state[0]+=a; state[1]+=b; state[2]+=c; state[3]+=d;
    state[4]+=e; state[5]+=f; state[6]+=g; state[7]+=h;
}

/* Full SHA256 of arbitrary data → 32 bytes */
static void sha256_full(const uint8_t *data, size_t len, uint8_t out[32])
{
    EVP_MD_CTX *ctx = EVP_MD_CTX_new();
    EVP_DigestInit_ex(ctx, EVP_sha256(), NULL);
    EVP_DigestUpdate(ctx, data, len);
    unsigned int l = 32;
    EVP_DigestFinal_ex(ctx, out, &l);
    EVP_MD_CTX_free(ctx);
}

/* SHA256d (double SHA256) of arbitrary data → 32 bytes */
static void sha256d(const uint8_t *data, size_t len, uint8_t out[32])
{
    uint8_t h1[32];
    sha256_full(data, len, h1);
    sha256_full(h1, 32, out);
}

/*
 * SHA256-Nr hash: nrounds rounds of compression instead of 64.
 * Input: prev[32] + nonce_LE4 (36 bytes total)
 * Output: 32 bytes
 */
static void sha256_nr_hash(const uint8_t prev[32], uint32_t nonce,
                            int nrounds, uint8_t out[32])
{
    /* Build 64-byte padded input block (36 bytes data + padding) */
    uint8_t msg[64];
    memcpy(msg, prev, 32);
    msg[32] = (uint8_t)(nonce);
    msg[33] = (uint8_t)(nonce >> 8);
    msg[34] = (uint8_t)(nonce >> 16);
    msg[35] = (uint8_t)(nonce >> 24);
    /* SHA256 padding */
    msg[36] = 0x80;
    memset(msg + 37, 0, 64 - 37 - 8);
    uint64_t bit_len = 36 * 8;
    for (int i = 0; i < 8; i++)
        msg[63-i] = (uint8_t)(bit_len >> (i*8));

    /* Parse as 16 big-endian uint32s */
    uint32_t block[16];
    for (int i = 0; i < 16; i++) {
        block[i] = ((uint32_t)msg[i*4]   << 24) |
                   ((uint32_t)msg[i*4+1] << 16) |
                   ((uint32_t)msg[i*4+2] << 8)  |
                   ((uint32_t)msg[i*4+3]);
    }

    uint32_t state[8];
    memcpy(state, SHA256_H0, 32);
    sha256_compress_nr(state, block, nrounds);

    for (int i = 0; i < 8; i++) {
        out[i*4]   = (state[i] >> 24) & 0xFF;
        out[i*4+1] = (state[i] >> 16) & 0xFF;
        out[i*4+2] = (state[i] >> 8)  & 0xFF;
        out[i*4+3] = (state[i])        & 0xFF;
    }
}

/*
 * SHA256d midstate: compute midstate from first 64 bytes of header.
 * For a Bitcoin-like 80-byte header:
 *   header[0:64]  = version(4) + prev(32) + merkle[0:28]
 *   header[64:80] = merkle[28:32] + time(4) + bits(4) + nonce(4)
 * We simulate by putting prev_hash in header[4:36].
 */
static void sha256d_midstate(const uint8_t prev[32], uint8_t midstate[32])
{
    /* Build synthetic 64-byte first block */
    uint8_t block_bytes[64];
    memset(block_bytes, 0, 64);
    /* version = 0x00000001 */
    block_bytes[3] = 0x01;
    /* prev_hash at offset 4 */
    memcpy(block_bytes + 4, prev, 32);
    /* rest is zeros (merkle[0:28]) */

    uint32_t block[16];
    for (int i = 0; i < 16; i++) {
        block[i] = ((uint32_t)block_bytes[i*4]   << 24) |
                   ((uint32_t)block_bytes[i*4+1] << 16) |
                   ((uint32_t)block_bytes[i*4+2] << 8)  |
                   ((uint32_t)block_bytes[i*4+3]);
    }

    uint32_t state[8];
    memcpy(state, SHA256_H0, 32);
    sha256_compress_nr(state, block, 64);  /* full 64 rounds */

    for (int i = 0; i < 8; i++) {
        midstate[i*4]   = (state[i] >> 24) & 0xFF;
        midstate[i*4+1] = (state[i] >> 16) & 0xFF;
        midstate[i*4+2] = (state[i] >> 8)  & 0xFF;
        midstate[i*4+3] = (state[i])        & 0xFF;
    }
}

/*
 * SHA256d with midstate: second block contains nonce.
 * Uses precomputed midstate from sha256d_midstate().
 * Returns 32-byte hash.
 */
static void sha256d_from_midstate(const uint8_t midstate[32], uint32_t nonce,
                                   uint8_t out[32])
{
    /* Second block (16 bytes data + 48 bytes padding) */
    uint8_t block2[64];
    memset(block2, 0, 64);
    /* nonce at bytes 12-15 of second block (like Bitcoin header) */
    block2[12] = (uint8_t)(nonce);
    block2[13] = (uint8_t)(nonce >> 8);
    block2[14] = (uint8_t)(nonce >> 16);
    block2[15] = (uint8_t)(nonce >> 24);
    /* padding: total input to first SHA256 is 80 bytes */
    block2[16] = 0x80;
    memset(block2 + 17, 0, 64 - 17 - 8);
    uint64_t bit_len = 80 * 8;
    for (int i = 0; i < 8; i++)
        block2[63-i] = (uint8_t)(bit_len >> (i*8));

    /* Load midstate as SHA256 state */
    uint32_t state[8];
    for (int i = 0; i < 8; i++) {
        state[i] = ((uint32_t)midstate[i*4]   << 24) |
                   ((uint32_t)midstate[i*4+1] << 16) |
                   ((uint32_t)midstate[i*4+2] << 8)  |
                   ((uint32_t)midstate[i*4+3]);
    }

    /* Continue SHA256 with second block */
    uint32_t block[16];
    for (int i = 0; i < 16; i++) {
        block[i] = ((uint32_t)block2[i*4]   << 24) |
                   ((uint32_t)block2[i*4+1] << 16) |
                   ((uint32_t)block2[i*4+2] << 8)  |
                   ((uint32_t)block2[i*4+3]);
    }
    sha256_compress_nr(state, block, 64);

    uint8_t h1[32];
    for (int i = 0; i < 8; i++) {
        h1[i*4]   = (state[i] >> 24) & 0xFF;
        h1[i*4+1] = (state[i] >> 16) & 0xFF;
        h1[i*4+2] = (state[i] >> 8)  & 0xFF;
        h1[i*4+3] = (state[i])        & 0xFF;
    }

    /* Second SHA256 */
    sha256_full(h1, 32, out);
}

/* ── ETHash-lite (simplified DAG simulation) ─────────────────────────── */
/*
 * Real ETHash: 1GB+ DAG, 64 × 128-byte page accesses per nonce.
 * This simulation: 256-entry × 32-byte "mini-DAG", 8 accesses per nonce.
 * Same structural property: nonce determines which DAG entries are summed.
 * If certain nonces pick "low-valued" DAG entries → winners cluster.
 *
 * DAG generation: dag[i] = SHA3-256(epoch_seed || i_LE4)
 * Per nonce:
 *   mix = SHA3-256(header || nonce_LE8)        [determines indices]
 *   for 8 rounds: idx = mix XOR round_const, dag_page = dag[idx & 0xFF]
 *                 mix = SHA3-256(mix || dag_page)
 *   result = SHA3-256(mix)
 */
#define DAG_SIZE   256
#define DAG_ROUNDS 8

static uint8_t g_dag[DAG_SIZE][32];
static int     g_dag_ready = 0;

static void build_dag(const uint8_t epoch_seed[32])
{
    for (int i = 0; i < DAG_SIZE; i++) {
        uint8_t input[36];
        memcpy(input, epoch_seed, 32);
        input[32] = (uint8_t)(i);
        input[33] = (uint8_t)(i>>8);
        input[34] = (uint8_t)(i>>16);
        input[35] = (uint8_t)(i>>24);
        sha3_256(input, 36, g_dag[i]);
    }
    g_dag_ready = 1;
}

static void ethhash_lite(const uint8_t header[32], uint64_t nonce,
                          uint8_t out[32])
{
    /* Initial mix */
    uint8_t seed_in[40];
    memcpy(seed_in, header, 32);
    for (int i = 0; i < 8; i++) seed_in[32+i] = (uint8_t)(nonce >> (i*8));
    uint8_t mix[32];
    sha3_256(seed_in, 40, mix);

    /* DAG lookup rounds */
    for (int r = 0; r < DAG_ROUNDS; r++) {
        /* Index = first byte of mix XOR round */
        uint8_t idx = mix[0] ^ (uint8_t)r;
        /* Combine mix with DAG entry */
        uint8_t combined[64];
        memcpy(combined, mix, 32);
        memcpy(combined + 32, g_dag[idx], 32);
        sha3_256(combined, 64, mix);
    }

    sha3_256(mix, 32, out);
}

/* ── AES-based hash (for comparison) ─────────────────────────────────── */
static void aes128_expand(const uint8_t key[16], __m128i rk[11])
{
    rk[0] = _mm_loadu_si128((__m128i*)key);
#define KES(i,r) do { \
    __m128i t=_mm_aeskeygenassist_si128(rk[i-1],r); \
    t=_mm_shuffle_epi32(t,0xff); \
    __m128i s=rk[i-1]; \
    s=_mm_xor_si128(s,_mm_slli_si128(s,4)); \
    s=_mm_xor_si128(s,_mm_slli_si128(s,4)); \
    s=_mm_xor_si128(s,_mm_slli_si128(s,4)); \
    rk[i]=_mm_xor_si128(s,t); } while(0)
    KES(1,0x01);KES(2,0x02);KES(3,0x04);KES(4,0x08);KES(5,0x10);
    KES(6,0x20);KES(7,0x40);KES(8,0x80);KES(9,0x1b);KES(10,0x36);
#undef KES
}

/* ── Winner density scan ─────────────────────────────────────────────── */
/*
 * Scans nonces 0..scan_range in N_BUCKETS buckets.
 * Returns winner count per bucket.
 * algo: 0=SHA256-Nr, 1=ETHash-lite, 2=SHA256d-midstate
 * param: for algo=0: nrounds (1..64); for others: unused
 */
#define N_BUCKETS 256

static PyObject *py_winner_density(PyObject *self, PyObject *args)
{
    const char *prev;   Py_ssize_t plen;
    uint64_t    scan_range;
    const char *target; Py_ssize_t tlen;
    int         algo;
    int         param = 64;

    if (!PyArg_ParseTuple(args, "y#Ky#i|i",
                          &prev, &plen, &scan_range, &target, &tlen,
                          &algo, &param))
        return NULL;
    if (plen < 32) { PyErr_SetString(PyExc_ValueError, "need 32 bytes prev"); return NULL; }
    if (tlen != 32) { PyErr_SetString(PyExc_ValueError, "target 32 bytes"); return NULL; }

    uint64_t bucket_size = scan_range / N_BUCKETS;
    if (bucket_size < 1) bucket_size = 1;

    /* Precompute per-algo setup */
    uint8_t midstate[32];
    if (algo == 2) {
        sha256d_midstate((const uint8_t*)prev, midstate);
    }
    if (algo == 1 && !g_dag_ready) {
        build_dag((const uint8_t*)prev);
    }

    uint64_t counts[N_BUCKETS] = {0};

    for (uint64_t nonce = 0; nonce < scan_range; nonce++) {
        uint8_t out[32];

        switch (algo) {
            case 0:  /* SHA256-Nr */
                sha256_nr_hash((const uint8_t*)prev, (uint32_t)nonce, param, out);
                break;
            case 1:  /* ETHash-lite */
                if (!g_dag_ready) build_dag((const uint8_t*)prev);
                ethhash_lite((const uint8_t*)prev, nonce, out);
                break;
            case 2:  /* SHA256d midstate */
                sha256d_from_midstate(midstate, (uint32_t)nonce, out);
                break;
            default:
                sha256d((const uint8_t*)prev, 32 + 4, out);
        }

        if (memcmp(out, target, 32) < 0) {
            uint64_t bucket = (nonce / bucket_size);
            if (bucket >= N_BUCKETS) bucket = N_BUCKETS - 1;
            counts[bucket]++;
        }
    }

    PyObject *lst = PyList_New(N_BUCKETS);
    for (int i = 0; i < N_BUCKETS; i++)
        PyList_SET_ITEM(lst, i, PyLong_FromUnsignedLongLong(counts[i]));
    return lst;
}

/* build_dag_py: build DAG from epoch seed (call before ETHash scan) */
static PyObject *py_build_dag(PyObject *self, PyObject *args)
{
    const char *seed; Py_ssize_t slen;
    if (!PyArg_ParseTuple(args, "y#", &seed, &slen)) return NULL;
    if (slen < 32) { PyErr_SetString(PyExc_ValueError, "need 32 bytes"); return NULL; }
    build_dag((const uint8_t*)seed);
    Py_RETURN_NONE;
}

/* get_midstate: expose midstate for inspection */
static PyObject *py_get_midstate(PyObject *self, PyObject *args)
{
    const char *prev; Py_ssize_t plen;
    if (!PyArg_ParseTuple(args, "y#", &prev, &plen)) return NULL;
    if (plen < 32) { PyErr_SetString(PyExc_ValueError, "need 32 bytes"); return NULL; }
    uint8_t ms[32];
    sha256d_midstate((const uint8_t*)prev, ms);
    return PyBytes_FromStringAndSize((char*)ms, 32);
}

/* ── Module ──────────────────────────────────────────────────────────── */
static PyMethodDef PotMethods[] = {
    {"winner_density", py_winner_density, METH_VARARGS,
     "winner_density(prev32, scan_range, target32, algo, param=64) -> list[256]"},
    {"build_dag",      py_build_dag,      METH_VARARGS,
     "build_dag(seed32) — precompute ETHash-lite DAG"},
    {"get_midstate",   py_get_midstate,   METH_VARARGS,
     "get_midstate(prev32) -> bytes(32)"},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef potmod = {
    PyModuleDef_HEAD_INIT, "pot_skip", NULL, -1, PotMethods
};

PyMODINIT_FUNC PyInit_pot_skip(void) {
    return PyModule_Create(&potmod);
}
