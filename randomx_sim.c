/*
 * randomx_sim.c — Simplified RandomX-inspired simulation
 *
 * Implements the KEY structural properties of RandomX:
 *   1. Initial state: SHA3-256(header || nonce_LE8) → 8 registers × 64-bit
 *   2. Fixed random program: 256 instructions (ADD/SUB/MUL/XOR/ROT/CBRANCH/LOAD/STORE)
 *      generated once from block_key = SHA3-256(prev_hash)
 *   3. 256-byte scratchpad (simplified from 256KB)
 *   4. AES-128 finalization: 10 full rounds (same as real RandomX)
 *
 * Instruction set (weighted like RandomX):
 *   ADD/SUB/XOR/OR (45%)  — integer arithmetic/logic
 *   MUL/IMUL (20%)         — multiply
 *   ROTATE/SHIFT (15%)     — bit rotation
 *   LOAD/STORE (12%)       — scratchpad access
 *   CBRANCH (8%)           — conditional branch (loop back if reg[src] & mask == 0)
 *
 * Exposed Python functions:
 *   set_program(block_key_bytes)    — generate program from block_key, store globally
 *   hash(prev_hash_bytes, nonce_u64) -> bytes(32)
 *   scan_winners(prev_bytes, scan_range, target_bytes) -> list[int]
 *   scan_with_timing(prev_bytes, scan_range, target_bytes) -> list[(nonce, branch_count)]
 *   bench(prev_bytes, n) -> float (MH/s)
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <openssl/evp.h>
#include <wmmintrin.h>
#include <smmintrin.h>
#include <stdint.h>
#include <string.h>
#include <stdlib.h>

/* ── SHA3-256 ─────────────────────────────────────────────────────────────── */
static void sha3_256(const uint8_t *in, size_t len, uint8_t out[32])
{
    EVP_MD_CTX *ctx = EVP_MD_CTX_new();
    EVP_DigestInit_ex(ctx, EVP_sha3_256(), NULL);
    EVP_DigestUpdate(ctx, in, len);
    unsigned int l = 32;
    EVP_DigestFinal_ex(ctx, out, &l);
    EVP_MD_CTX_free(ctx);
}

/* ── AES-128 key expansion + 10-round encrypt ─────────────────────────────── */
static __m128i g_rk[11];   /* round keys */

static void aes128_expand(const uint8_t key[16], __m128i rk[11])
{
    rk[0] = _mm_loadu_si128((__m128i*)key);
#define KES(i, rcon) do {                                      \
    __m128i t = _mm_aeskeygenassist_si128(rk[i-1], rcon);     \
    t = _mm_shuffle_epi32(t, 0xff);                           \
    __m128i s = rk[i-1];                                      \
    s = _mm_xor_si128(s, _mm_slli_si128(s, 4));               \
    s = _mm_xor_si128(s, _mm_slli_si128(s, 4));               \
    s = _mm_xor_si128(s, _mm_slli_si128(s, 4));               \
    rk[i] = _mm_xor_si128(s, t);                              \
} while(0)
    KES(1,0x01); KES(2,0x02); KES(3,0x04); KES(4,0x08);
    KES(5,0x10); KES(6,0x20); KES(7,0x40); KES(8,0x80);
    KES(9,0x1b); KES(10,0x36);
#undef KES
}

static void aes128_encrypt10(const __m128i rk[11], const uint8_t in[16], uint8_t out[16])
{
    __m128i st = _mm_xor_si128(_mm_loadu_si128((__m128i*)in), rk[0]);
    for (int r = 1; r <= 9; r++) st = _mm_aesenc_si128(st, rk[r]);
    st = _mm_aesenclast_si128(st, rk[10]);
    _mm_storeu_si128((__m128i*)out, st);
}

/* ── Random program ──────────────────────────────────────────────────────── */
#define PROG_LEN  256
#define NREGS     8
#define SCRATCH   64      /* 64 × uint64 = 512 bytes */

/* Instruction types (weighted like RandomX) */
#define OP_ADD    0
#define OP_SUB    1
#define OP_XOR    2
#define OP_MUL    3
#define OP_ROTATE 4
#define OP_LOAD   5
#define OP_STORE  6
#define OP_CBRANCH 7

typedef struct {
    uint8_t  op;      /* operation */
    uint8_t  dst;     /* destination reg (0..7) */
    uint8_t  src;     /* source reg (0..7) */
    uint32_t imm;     /* immediate value */
    int16_t  target;  /* for CBRANCH: jump target (backwards, signed) */
    uint16_t mask;    /* for CBRANCH: condition mask */
} Instr;

static Instr g_prog[PROG_LEN];
static int   g_prog_ready = 0;

/* Simple PRNG from seed (xorshift64) for program generation */
static uint64_t xorshift64(uint64_t *state)
{
    *state ^= *state << 13;
    *state ^= *state >> 7;
    *state ^= *state << 17;
    return *state;
}

/* Generate 256-instruction random program from block_key */
static void generate_program(const uint8_t block_key[32])
{
    /* Expand key into seed via SHA3 */
    uint8_t seed[32];
    sha3_256(block_key, 32, seed);
    uint64_t prng;
    memcpy(&prng, seed, 8);
    if (!prng) prng = 1;

    /* Instruction type weights (cumulative, out of 64) */
    /* ADD=15, SUB=10, XOR=10, MUL=12, ROT=10, LOAD=8, STORE=4, CBRANCH=5 */
    static const uint8_t OP_TABLE[64] = {
        0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,   /* ADD × 15 */
        1,1,1,1,1,1,1,1,1,1,              /* SUB × 10 */
        2,2,2,2,2,2,2,2,2,2,              /* XOR × 10 */
        3,3,3,3,3,3,3,3,3,3,3,3,         /* MUL × 12 */
        4,4,4,4,4,4,4,4,4,4,             /* ROT × 10 */
        5,5,5,5,5,5,5,5,                 /* LOAD × 8 */
        6,6,6,6,                          /* STORE × 4 */
        7,7,7,7,7                         /* CBRANCH × 5 */
    };

    int cbranch_count = 0;

    for (int i = 0; i < PROG_LEN; i++) {
        uint64_t r = xorshift64(&prng);
        uint8_t op = OP_TABLE[r & 63];
        /* Limit CBRANCH to max 3 per program */
        if (op == OP_CBRANCH && cbranch_count >= 3)
            op = OP_XOR;
        if (op == OP_CBRANCH) cbranch_count++;

        g_prog[i].op     = op;
        g_prog[i].dst    = (r >> 8) & 7;
        g_prog[i].src    = (r >> 16) & 7;
        g_prog[i].imm    = (uint32_t)(r >> 24);
        /* CBRANCH: jump back 2..16 instructions */
        g_prog[i].target = (op == OP_CBRANCH) ? -(int16_t)(2 + ((r>>32) & 15)) : 0;
        /* CBRANCH: mask for condition check (8-bit) */
        g_prog[i].mask   = (op == OP_CBRANCH) ? (uint16_t)(0x0F + ((r>>40) & 0xF0)) : 0;
    }

    g_prog_ready = 1;
}

/* ── Execute program, return branch count ─────────────────────────────────── */
static int execute_program(uint64_t regs[NREGS], uint64_t scratch[SCRATCH])
{
    int branch_count = 0;
    const int MAX_ITER = PROG_LEN + 256;  /* safety cap */
    int i = 0, iter = 0;

    while (i < PROG_LEN && iter < MAX_ITER) {
        iter++;
        Instr *ins = &g_prog[i];
        uint64_t sv = regs[ins->src];
        uint64_t dv = regs[ins->dst];

        switch (ins->op) {
            case OP_ADD:    regs[ins->dst] = dv + sv + ins->imm; break;
            case OP_SUB:    regs[ins->dst] = dv - sv - ins->imm; break;
            case OP_XOR:    regs[ins->dst] = dv ^ sv ^ ins->imm; break;
            case OP_MUL:    regs[ins->dst] = dv * (sv | 1);      break;
            case OP_ROTATE: {
                int rot = (sv + ins->imm) & 63;
                regs[ins->dst] = (dv << rot) | (dv >> (64 - rot));
                break;
            }
            case OP_LOAD:   regs[ins->dst] = scratch[(sv + ins->imm) & (SCRATCH-1)]; break;
            case OP_STORE:  scratch[(dv + ins->imm) & (SCRATCH-1)] = sv; break;
            case OP_CBRANCH:
                if ((sv & ins->mask) == 0) {
                    i += ins->target;
                    if (i < 0) i = 0;
                    branch_count++;
                    continue;
                }
                break;
        }
        i++;
    }
    return branch_count;
}

/* ── Core hash function ─────────────────────────────────────────────────────
 * Returns branch_count via out pointer if not NULL.
 */
static void rx_hash(const uint8_t prev[32], uint64_t nonce,
                    uint8_t out[32], int *branch_count_out)
{
    /* 1. Initial state: SHA3-256(prev || nonce_LE8) → 8 × uint64 regs */
    uint8_t seed_input[40];
    memcpy(seed_input, prev, 32);
    for (int i = 0; i < 8; i++) seed_input[32+i] = (uint8_t)(nonce >> (i*8));

    uint8_t seed[32];
    sha3_256(seed_input, 40, seed);

    uint64_t regs[NREGS];
    for (int i = 0; i < 8; i++) {
        memcpy(&regs[i], seed + i*4, 4);  /* use 32 bits per register */
        regs[i] ^= (uint64_t)i * 0x9E3779B185EBCA87ULL;
    }

    /* 2. Scratchpad: fill from seed XOR'd with program constants */
    uint64_t scratch[SCRATCH];
    uint8_t sp_seed[32];
    sha3_256(seed, 32, sp_seed);
    for (int i = 0; i < SCRATCH; i++) {
        memcpy(&scratch[i], sp_seed + (i*8) % 32, 4);
        scratch[i] ^= regs[i & 7] ^ (uint64_t)i;
    }

    /* 3. Execute random program */
    int bc = execute_program(regs, scratch);
    if (branch_count_out) *branch_count_out = bc;

    /* 4. AES-128 finalization (10 full rounds) — same as real RandomX */
    /* Pack 8 registers into two 128-bit blocks, encrypt both */
    uint8_t state[32];
    for (int i = 0; i < 8; i++) {
        uint32_t lo = (uint32_t)regs[i];
        state[i*4]   = (lo)      & 0xFF;
        state[i*4+1] = (lo>>8)   & 0xFF;
        state[i*4+2] = (lo>>16)  & 0xFF;
        state[i*4+3] = (lo>>24)  & 0xFF;
    }

    /* Derive AES key from first 16 bytes of seed */
    __m128i rk[11];
    aes128_expand(seed, rk);

    uint8_t enc0[16], enc1[16];
    aes128_encrypt10(rk, state,      enc0);
    aes128_encrypt10(rk, state + 16, enc1);

    /* XOR with original state (like real RandomX finalization) */
    for (int i = 0; i < 16; i++) out[i]    = enc0[i] ^ state[i];
    for (int i = 0; i < 16; i++) out[16+i] = enc1[i] ^ state[16+i];
}

/* ── Python bindings ──────────────────────────────────────────────────────── */

static PyObject *py_set_program(PyObject *self, PyObject *args)
{
    const char *key; Py_ssize_t klen;
    if (!PyArg_ParseTuple(args, "y#", &key, &klen)) return NULL;
    if (klen < 32) { PyErr_SetString(PyExc_ValueError, "need 32 bytes"); return NULL; }
    generate_program((const uint8_t*)key);
    Py_RETURN_NONE;
}

static PyObject *py_hash(PyObject *self, PyObject *args)
{
    const char *prev; Py_ssize_t plen;
    uint64_t nonce;
    if (!PyArg_ParseTuple(args, "y#K", &prev, &plen, &nonce)) return NULL;
    if (!g_prog_ready) { PyErr_SetString(PyExc_RuntimeError, "call set_program first"); return NULL; }
    if (plen < 32) { PyErr_SetString(PyExc_ValueError, "need 32 bytes"); return NULL; }
    uint8_t out[32];
    rx_hash((const uint8_t*)prev, nonce, out, NULL);
    return PyBytes_FromStringAndSize((char*)out, 32);
}

static PyObject *py_scan_winners(PyObject *self, PyObject *args)
{
    const char *prev; Py_ssize_t plen;
    uint64_t scan_range;
    const char *target; Py_ssize_t tlen;
    if (!PyArg_ParseTuple(args, "y#Ky#", &prev, &plen, &scan_range, &target, &tlen)) return NULL;
    if (!g_prog_ready) { PyErr_SetString(PyExc_RuntimeError, "call set_program first"); return NULL; }
    if (plen < 32) { PyErr_SetString(PyExc_ValueError, "need 32 bytes"); return NULL; }
    if (tlen != 32) { PyErr_SetString(PyExc_ValueError, "target 32 bytes"); return NULL; }

    PyObject *winners = PyList_New(0);
    for (uint64_t n = 0; n < scan_range; n++) {
        uint8_t out[32];
        rx_hash((const uint8_t*)prev, n, out, NULL);
        if (memcmp(out, target, 32) < 0) {
            PyObject *item = PyLong_FromUnsignedLongLong(n);
            PyList_Append(winners, item);
            Py_DECREF(item);
        }
    }
    return winners;
}

/* scan_with_timing: returns list of (nonce, branch_count, is_winner) tuples */
static PyObject *py_scan_with_timing(PyObject *self, PyObject *args)
{
    const char *prev; Py_ssize_t plen;
    uint64_t scan_range;
    const char *target; Py_ssize_t tlen;
    if (!PyArg_ParseTuple(args, "y#Ky#", &prev, &plen, &scan_range, &target, &tlen)) return NULL;
    if (!g_prog_ready) { PyErr_SetString(PyExc_RuntimeError, "call set_program first"); return NULL; }

    PyObject *results = PyList_New(0);
    for (uint64_t n = 0; n < scan_range; n++) {
        uint8_t out[32];
        int bc = 0;
        rx_hash((const uint8_t*)prev, n, out, &bc);
        int is_winner = (memcmp(out, target, 32) < 0) ? 1 : 0;
        PyObject *t = PyTuple_Pack(3,
            PyLong_FromUnsignedLongLong(n),
            PyLong_FromLong(bc),
            PyLong_FromLong(is_winner));
        PyList_Append(results, t);
        Py_DECREF(t);
    }
    return results;
}

static PyMethodDef RxMethods[] = {
    {"set_program",      py_set_program,      METH_VARARGS, "set_program(key32) — generate program"},
    {"hash",             py_hash,             METH_VARARGS, "hash(prev32, nonce) -> bytes(32)"},
    {"scan_winners",     py_scan_winners,     METH_VARARGS, "scan_winners(prev32, range, target32) -> list"},
    {"scan_with_timing", py_scan_with_timing, METH_VARARGS, "scan_with_timing(prev32, range, target32) -> [(nonce,branches,is_win)]"},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef rxmod = {
    PyModuleDef_HEAD_INIT, "randomx_sim", NULL, -1, RxMethods
};

PyMODINIT_FUNC PyInit_randomx_sim(void) {
    return PyModule_Create(&rxmod);
}
