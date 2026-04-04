/*
 * equihash_sim.c — Equihash(125,4) simulator for FLUX/ZEC-style PoW
 *
 * Parameters: n=125, k=4
 *   - Solution: 2^k = 16 indices
 *   - Wagner rounds: k = 4
 *   - Collision bits per round: n/(k+1) = 25
 *   - Hash: Blake2b-512, first 125 bits used per index (16 bytes, top bit zeroed)
 *   - Total hash bits needed: 16 indices × 125 bits
 *
 * Wagner's algorithm overview:
 *   Round 0: Generate 2^(n/(k+1)+1) = 2^26 hash values, group by first 25 bits
 *   Round 1: XOR pairs with matching first 25 bits, group by next 25 bits
 *   Round 2: XOR pairs with matching next 25 bits, group by next 25 bits
 *   Round 3: XOR pairs matching → final XOR = 0 (all 100 bits cancel) + ordering check
 *
 * For this simulation we use REDUCED parameters (n=40, k=4) to keep runtime
 * tractable in Python, preserving the same structural properties:
 *   - n=40, k=4 → collision bits = 40/5 = 8 per round
 *   - Solution = 16 indices
 *   - Hash: SHA3-256 truncated to 40 bits (5 bytes)
 *   - 4 Wagner rounds, each matching 8 bits
 *
 * Exposed Python functions:
 *   solve(header_bytes, nonce_u32, max_solutions=8) -> list[tuple[16 ints]]
 *   score_partial(header_bytes, nonce_u32, depth) -> int (candidates surviving to depth)
 *   bench(header_bytes, n_nonces) -> (solutions_found, seconds)
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <openssl/evp.h>
#include <stdint.h>
#include <string.h>
#include <stdlib.h>

/* ── Reduced parameters (tractable in pure C) ───────────────────────────── */
#define N_BITS      40      /* hash width */
#define K_ROUNDS    4       /* Wagner rounds */
#define COL_BITS    8       /* n/(k+1) = 40/5 = 8 collision bits per round */
#define N_IDX       16      /* 2^k solution indices */
#define HASH_BYTES  5       /* ceil(N_BITS/8) */

/* Initial list size: 2^(COL_BITS+1) = 512 per bucket, but we generate more */
#define LIST_SIZE   (1 << (COL_BITS + 2))   /* 1024 initial candidates */
#define MAX_SOL     64

/* ── SHA3-256 truncated to HASH_BYTES ────────────────────────────────────── */
static void hash_index(const uint8_t *header, int hlen,
                        uint32_t nonce, uint32_t idx,
                        uint8_t out[HASH_BYTES])
{
    uint8_t input[64];
    int pos = 0;
    int copy = hlen < 32 ? hlen : 32;
    memcpy(input, header, copy); pos += copy;
    input[pos++] = (uint8_t)(nonce);
    input[pos++] = (uint8_t)(nonce >> 8);
    input[pos++] = (uint8_t)(nonce >> 16);
    input[pos++] = (uint8_t)(nonce >> 24);
    input[pos++] = (uint8_t)(idx);
    input[pos++] = (uint8_t)(idx >> 8);
    input[pos++] = (uint8_t)(idx >> 16);
    input[pos++] = (uint8_t)(idx >> 24);

    EVP_MD_CTX *ctx = EVP_MD_CTX_new();
    EVP_DigestInit_ex(ctx, EVP_sha3_256(), NULL);
    EVP_DigestUpdate(ctx, input, pos);
    uint8_t tmp[32]; unsigned int l = 32;
    EVP_DigestFinal_ex(ctx, tmp, &l);
    EVP_MD_CTX_free(ctx);
    memcpy(out, tmp, HASH_BYTES);
}

/* ── 40-bit value packed/unpacked ───────────────────────────────────────── */
static inline uint64_t unpack40(const uint8_t b[5]) {
    return ((uint64_t)b[0]<<32)|((uint64_t)b[1]<<24)|
           ((uint64_t)b[2]<<16)|((uint64_t)b[3]<<8)|(uint64_t)b[4];
}
static inline void pack40(uint8_t b[5], uint64_t v) {
    b[0]=(v>>32)&0xFF; b[1]=(v>>24)&0xFF; b[2]=(v>>16)&0xFF;
    b[3]=(v>>8)&0xFF;  b[4]=v&0xFF;
}

/* ── Candidate node ─────────────────────────────────────────────────────── */
typedef struct {
    uint64_t  val;          /* current XOR value (40 bits) */
    uint32_t  indices[16];  /* up to 16 leaf indices */
    uint8_t   n_idx;        /* how many indices stored */
} Node;

/* ── Check no duplicate indices in two nodes ────────────────────────────── */
static int no_dup(const Node *a, const Node *b)
{
    for (int i = 0; i < a->n_idx; i++)
        for (int j = 0; j < b->n_idx; j++)
            if (a->indices[i] == b->indices[j]) return 0;
    return 1;
}

/* ── Check indices are in canonical order ───────────────────────────────── */
static int canonical(const Node *a, const Node *b)
{
    return a->indices[0] < b->indices[0];
}

/* ── Equihash solver (Wagner's algorithm, reduced params) ───────────────── */
/*
 * Returns number of solutions found (up to max_sol).
 * solutions[][16] filled with sorted index sets.
 * If depth_out != NULL, also returns candidate count at each depth (for scoring).
 */
static int solve_inner(const uint8_t *header, int hlen, uint32_t nonce,
                        uint32_t solutions[][N_IDX], int max_sol,
                        int depth_counts[K_ROUNDS+1])
{
    int n_sol = 0;

    /* Round 0: generate initial hashes */
    /* Both buffers sized to handle worst-case growth across all rounds */
    int max_nodes = LIST_SIZE * 16;
    Node *buf0 = (Node*)malloc(max_nodes * sizeof(Node));
    Node *buf1 = (Node*)malloc(max_nodes * sizeof(Node));
    if (!buf0 || !buf1) { free(buf0); free(buf1); return 0; }

    Node *cur = buf0, *nxt = buf1;
    int n_cur = 0;

    /* Round 0: generate initial hashes (LIST_SIZE candidates) */
    for (int i = 0; i < LIST_SIZE; i++) {
        uint8_t h[HASH_BYTES];
        hash_index(header, hlen, nonce, (uint32_t)i, h);
        cur[n_cur].val        = unpack40(h);
        cur[n_cur].indices[0] = (uint32_t)i;
        cur[n_cur].n_idx      = 1;
        n_cur++;
    }

    if (depth_counts) depth_counts[0] = n_cur;

    for (int round = 0; round < K_ROUNDS; round++) {
        int col_shift = (K_ROUNDS - round) * COL_BITS;
        uint64_t col_mask = ((1ULL << COL_BITS) - 1) << col_shift;

        /* Insertion sort by collision bits */
        for (int i = 1; i < n_cur; i++) {
            Node tmp = cur[i];
            uint64_t key = tmp.val & col_mask;
            int j = i - 1;
            while (j >= 0 && (cur[j].val & col_mask) > key) {
                cur[j+1] = cur[j]; j--;
            }
            cur[j+1] = tmp;
        }

        int n_nxt = 0;

        for (int i = 0; i < n_cur - 1; i++) {
            for (int j = i + 1; j < n_cur; j++) {
                if ((cur[i].val & col_mask) != (cur[j].val & col_mask)) break;

                if (!canonical(&cur[i], &cur[j])) continue;
                if (!no_dup(&cur[i], &cur[j])) continue;

                if (round == K_ROUNDS - 1) {
                    uint64_t xor_val = cur[i].val ^ cur[j].val;
                    if (xor_val == 0 && n_sol < max_sol) {
                        int ni = cur[i].n_idx, nj = cur[j].n_idx;
                        for (int x = 0; x < ni; x++)
                            solutions[n_sol][x] = cur[i].indices[x];
                        for (int x = 0; x < nj; x++)
                            solutions[n_sol][ni+x] = cur[j].indices[x];
                        n_sol++;
                    }
                } else {
                    if (n_nxt < max_nodes) {
                        nxt[n_nxt].val   = cur[i].val ^ cur[j].val;
                        nxt[n_nxt].n_idx = cur[i].n_idx + cur[j].n_idx;
                        memcpy(nxt[n_nxt].indices, cur[i].indices,
                               cur[i].n_idx * sizeof(uint32_t));
                        memcpy(nxt[n_nxt].indices + cur[i].n_idx,
                               cur[j].indices,
                               cur[j].n_idx * sizeof(uint32_t));
                        n_nxt++;
                    }
                }
            }
        }

        if (round < K_ROUNDS - 1) {
            /* Swap buffers */
            Node *tmp = cur; cur = nxt; nxt = tmp;
            n_cur = n_nxt;
            if (depth_counts) depth_counts[round+1] = n_cur;
        }
    }

    free(buf0);
    free(buf1);
    return n_sol;
}

/* ── Python: solve(header, nonce, max_solutions=8) → list of tuples ──── */
static PyObject *py_solve(PyObject *self, PyObject *args)
{
    const char *hdr; Py_ssize_t hlen;
    unsigned long nonce;
    int max_sol = 8;
    if (!PyArg_ParseTuple(args, "y#k|i", &hdr, &hlen, &nonce, &max_sol)) return NULL;

    uint32_t solutions[MAX_SOL][N_IDX];
    int n = solve_inner((const uint8_t*)hdr, (int)hlen, (uint32_t)nonce,
                         solutions, max_sol < MAX_SOL ? max_sol : MAX_SOL, NULL);

    PyObject *lst = PyList_New(n);
    for (int i = 0; i < n; i++) {
        PyObject *t = PyTuple_New(N_IDX);
        for (int j = 0; j < N_IDX; j++)
            PyTuple_SET_ITEM(t, j, PyLong_FromUnsignedLong(solutions[i][j]));
        PyList_SET_ITEM(lst, i, t);
    }
    return lst;
}

/* ── Python: score_partial(header, nonce, depth) → candidates at depth ── */
static PyObject *py_score_partial(PyObject *self, PyObject *args)
{
    const char *hdr; Py_ssize_t hlen;
    unsigned long nonce;
    int depth;
    if (!PyArg_ParseTuple(args, "y#ki", &hdr, &hlen, &nonce, &depth)) return NULL;
    if (depth < 0 || depth > K_ROUNDS)
        { PyErr_SetString(PyExc_ValueError, "depth 0..4"); return NULL; }

    int depth_counts[K_ROUNDS+1] = {0};
    uint32_t solutions[MAX_SOL][N_IDX];
    solve_inner((const uint8_t*)hdr, (int)hlen, (uint32_t)nonce,
                solutions, MAX_SOL, depth_counts);

    return PyLong_FromLong(depth_counts[depth]);
}

/* ── Python: solve_with_scores(header, nonce) → (n_solutions, depth_counts[5]) */
static PyObject *py_solve_with_scores(PyObject *self, PyObject *args)
{
    const char *hdr; Py_ssize_t hlen;
    unsigned long nonce;
    if (!PyArg_ParseTuple(args, "y#k", &hdr, &hlen, &nonce)) return NULL;

    int depth_counts[K_ROUNDS+1] = {0};
    uint32_t solutions[MAX_SOL][N_IDX];
    int n_sol = solve_inner((const uint8_t*)hdr, (int)hlen, (uint32_t)nonce,
                             solutions, MAX_SOL, depth_counts);

    PyObject *counts = PyTuple_New(K_ROUNDS+1);
    for (int i = 0; i <= K_ROUNDS; i++)
        PyTuple_SET_ITEM(counts, i, PyLong_FromLong(depth_counts[i]));

    return PyTuple_Pack(2, PyLong_FromLong(n_sol), counts);
}

/* ── Module ─────────────────────────────────────────────────────────────── */
static PyMethodDef EqMethods[] = {
    {"solve",            py_solve,            METH_VARARGS, "solve(hdr, nonce[, max]) -> [(i0..i15)]"},
    {"score_partial",    py_score_partial,    METH_VARARGS, "score_partial(hdr, nonce, depth) -> int"},
    {"solve_with_scores",py_solve_with_scores,METH_VARARGS, "solve_with_scores(hdr, nonce) -> (n_sol, counts[5])"},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef eqmod = {
    PyModuleDef_HEAD_INIT, "equihash_sim", NULL, -1, EqMethods
};

PyMODINIT_FUNC PyInit_equihash_sim(void) {
    return PyModule_Create(&eqmod);
}
