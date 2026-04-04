/*
 * verus_real.c — Real Haraka-512 based PoW hash (VerusHash 2.1 core)
 *
 * Our previous verus_aes.c was a TOY MODEL:
 *   - single 16-byte AES-128 block
 *   - N=1 showed byte_0 leakage (no MixColumns in aesenclast)
 *
 * Real Haraka-512 is fundamentally different:
 *   - 4 × 128-bit blocks (64 bytes total input → 32 bytes output)
 *   - 5 rounds × 2 AES ops per block + MIX512 cross-block permutation
 *   - Feed-forward XOR (Davies-Meyer): output ^= input → no fixed points
 *   - 40 fixed round constants from π digits
 *
 * For VerusHash 2.1 header hashing:
 *   - 140-byte header (ver4 + prevhash32 + merkle32 + reserved32 + ts4 + bits4 + nonce32)
 *   - Pad to 192 bytes (3 × 64), process each 64-byte chunk
 *   - Chain: state[64] updated iteratively
 *   - Final: keyed Haraka-512 with seed = last 32 bytes of input
 *
 * This lets us test:
 *   A. Does nonce structure leak through real Haraka? (vs toy N=1 which leaked byte_0)
 *   B. Winner distribution uniform?
 *   C. ML signal in nonce bits?
 *
 * Exposed Python functions:
 *   haraka512(data_bytes64) -> bytes(32)
 *   verus_hash_real(header_bytes140, nonce_uint32) -> bytes(32)
 *   scan_winners_real(header_template140, scan_range, target32) -> list[int]
 *   test_diffusion(prev32, nonce_uint32, flip_bit) -> (bytes(32), bytes(32))
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <wmmintrin.h>
#include <smmintrin.h>
#include <tmmintrin.h>
#include <stdint.h>
#include <string.h>
#include <stdlib.h>

/* ═══════════════════════════════════════════════════════════════════
 * Haraka-512 round constants (from π digits, VerusHash reference)
 * 40 × 128-bit values
 * ═══════════════════════════════════════════════════════════════════ */
static const uint32_t RC_RAW[40][4] = {
    {0x0684704c, 0xe620c00a, 0xb2c5fef0, 0x75817b9d},
    {0x8b66b4e1, 0x88f3a06b, 0x640f6ba4, 0x2f08f717},
    {0x3402de2d, 0x53f28498, 0xcf029d60, 0x9f029114},
    {0x0ed6eae6, 0x2e7b4f08, 0xbbf3bcaf, 0xfd5b4f79},
    {0xcbcfb0cb, 0x4872448b, 0x79eecd1c, 0xbe397044},
    {0x7eeacdee, 0x6e9032b7, 0x8d5335ed, 0x2b8a057b},
    {0x67c28f43, 0x5e2e7cd0, 0xe2412761, 0xda4fef1b},
    {0x2924d9b0, 0xafcacc07, 0x675ffde2, 0x1fc70b3b},
    {0xab4d63f1, 0xe6867fe9, 0xecdb8fca, 0xb9d465ee},
    {0x1c30bf84, 0xd4b7cd64, 0x5b2a404f, 0xad037e33},
    {0xb2cc0bb9, 0x941723bf, 0x69028b2e, 0x8df69800},
    {0xfa0478a6, 0xde6f5572, 0x4aaa9ec8, 0x5c9d2d8a},
    {0xdfb49f2b, 0x6b772a12, 0x0efa4f2e, 0x29129fd4},
    {0x1ea10344, 0xf449a236, 0x32d611ae, 0xbb6a12ee},
    {0xaf044988, 0x4b050084, 0x5f9600c9, 0x9ca8eca6},
    {0x21025ed8, 0x9d199c4f, 0x78a2c7e3, 0x27e593ec},
    {0xbf3aaaf8, 0xa759c9b7, 0xb9282ecd, 0x82d40173},
    {0x6260700d, 0x6186b017, 0x37f2efd9, 0x10307d6b},
    {0x5aca45c2, 0x21300443, 0x81c29153, 0xf6fc9ac6},
    {0x9223973c, 0x226b68bb, 0x2caf92e8, 0x36d1943a},
    {0xd3bf9238, 0x225886eb, 0x6cbab958, 0xe51071b4},
    {0xdb863ce5, 0xaef0c677, 0x933dfddd, 0x24e1128d},
    {0xbb606268, 0xffeba09c, 0x83e48de3, 0xcb2212b1},
    {0x734bd3dc, 0xe2e4d19c, 0x2db91a4e, 0xc72bf77d},
    {0x43bb47c3, 0x61301b43, 0x4b1415c4, 0x2cb3924e},
    {0xdba775a8, 0xe707eff6, 0x03b231dd, 0x16eb6899},
    {0x6df3614b, 0x3c755977, 0x8e5e2302, 0x7eca472c},
    {0xcda75a17, 0xd6de7d77, 0x6d1be5b9, 0xb88617f9},
    {0xec6b43f0, 0x6ba8e9aa, 0x9d6c069d, 0xa946ee5d},
    {0xcb1e6950, 0xf957332b, 0xa2531159, 0x3bf327c1},
    {0x2cee0c75, 0x00da619c, 0xe4ed0353, 0x600ed0d9},
    {0xf0b1a5a1, 0x96e90cab, 0x80bbbabc, 0x63a4a350},
    {0xae3db102, 0x5e962988, 0xab0dde30, 0x938dca39},
    {0x17bb8f38, 0xd554a40b, 0x8814f3a8, 0x2e75b442},
    {0x34bb8a5b, 0x5f427fd7, 0xaeb6b779, 0x360a16f6},
    {0x26f65241, 0xcbe55438, 0x43ce5918, 0xffbaafde},
    {0x4ce99a54, 0xb9f3026a, 0xa2ca9cf7, 0x839ec978},
    {0xae51a51a, 0x1bdff7be, 0x40c06e28, 0x22901235},
    {0xa0c1613c, 0xba7ed22b, 0xc173bc0f, 0x48a659cf},
    {0x756acc03, 0x02288288, 0x4ad6bdfd, 0xe9c59da1},
};

static __m128i RC[40];

static void init_rc(void)
{
    static int done = 0;
    if (done) return;
    for (int i = 0; i < 40; i++)
        RC[i] = _mm_set_epi32(RC_RAW[i][0], RC_RAW[i][1],
                               RC_RAW[i][2], RC_RAW[i][3]);
    done = 1;
}

/* ═══════════════════════════════════════════════════════════════════
 * Haraka-512: 64 bytes → 32 bytes
 * ROUNDS=5, AES_PER_ROUND=2 (2 AES ops per block per round)
 * Each round: AES on all 4 blocks, then MIX512
 * Feed-forward XOR at end
 * ═══════════════════════════════════════════════════════════════════ */
static void haraka512(const uint8_t in[64], uint8_t out[32])
{
    __m128i s[4], tmp;

    /* Load 4 × 128-bit state blocks */
    s[0] = _mm_loadu_si128((__m128i*)(in +  0));
    s[1] = _mm_loadu_si128((__m128i*)(in + 16));
    s[2] = _mm_loadu_si128((__m128i*)(in + 32));
    s[3] = _mm_loadu_si128((__m128i*)(in + 48));

    /* 5 rounds */
    for (int r = 0; r < 5; r++) {
        int base = r * 8;  /* 4 blocks × 2 AES ops = 8 constants per round */

        /* First AES pass */
        s[0] = _mm_aesenc_si128(s[0], RC[base + 0]);
        s[1] = _mm_aesenc_si128(s[1], RC[base + 1]);
        s[2] = _mm_aesenc_si128(s[2], RC[base + 2]);
        s[3] = _mm_aesenc_si128(s[3], RC[base + 3]);

        /* Second AES pass */
        s[0] = _mm_aesenc_si128(s[0], RC[base + 4]);
        s[1] = _mm_aesenc_si128(s[1], RC[base + 5]);
        s[2] = _mm_aesenc_si128(s[2], RC[base + 6]);
        s[3] = _mm_aesenc_si128(s[3], RC[base + 7]);

        /* MIX512: cross-block column permutation */
        tmp  = _mm_unpacklo_epi32(s[0], s[1]);
        s[0] = _mm_unpackhi_epi32(s[0], s[1]);
        s[1] = _mm_unpacklo_epi32(s[2], s[3]);
        s[2] = _mm_unpackhi_epi32(s[2], s[3]);
        s[3] = _mm_unpacklo_epi32(s[0], s[2]);
        s[0] = _mm_unpackhi_epi32(s[0], s[2]);
        s[2] = _mm_unpackhi_epi32(s[1], tmp);
        s[1] = _mm_unpacklo_epi32(s[1], tmp);
    }

    /* Feed-forward XOR (Davies-Meyer) */
    s[0] = _mm_xor_si128(s[0], _mm_loadu_si128((__m128i*)(in +  0)));
    s[1] = _mm_xor_si128(s[1], _mm_loadu_si128((__m128i*)(in + 16)));
    s[2] = _mm_xor_si128(s[2], _mm_loadu_si128((__m128i*)(in + 32)));
    s[3] = _mm_xor_si128(s[3], _mm_loadu_si128((__m128i*)(in + 48)));

    /* Output: first 32 bytes of 64-byte state (truncate) */
    _mm_storeu_si128((__m128i*)(out +  0), s[0]);
    _mm_storeu_si128((__m128i*)(out + 16), s[2]);
}

/* Haraka-512 "keyed": XOR key into round constants before each round
 * key[32]: first 16 bytes XOR'd into round RC offsets 0 and 4 of each round
 * This approximates the VerusHash CLKey mechanism */
static void haraka512_keyed(const uint8_t in[64], uint8_t out[32],
                             const uint8_t key[32])
{
    __m128i s[4], tmp;
    __m128i krc0 = _mm_loadu_si128((__m128i*)key);
    __m128i krc1 = _mm_loadu_si128((__m128i*)(key + 16));

    s[0] = _mm_loadu_si128((__m128i*)(in +  0));
    s[1] = _mm_loadu_si128((__m128i*)(in + 16));
    s[2] = _mm_loadu_si128((__m128i*)(in + 32));
    s[3] = _mm_loadu_si128((__m128i*)(in + 48));

    for (int r = 0; r < 5; r++) {
        int base = r * 8;

        /* XOR key into the first two constants of each round */
        __m128i rc0_k = _mm_xor_si128(RC[base + 0], krc0);
        __m128i rc4_k = _mm_xor_si128(RC[base + 4], krc1);

        s[0] = _mm_aesenc_si128(s[0], rc0_k);
        s[1] = _mm_aesenc_si128(s[1], RC[base + 1]);
        s[2] = _mm_aesenc_si128(s[2], RC[base + 2]);
        s[3] = _mm_aesenc_si128(s[3], RC[base + 3]);

        s[0] = _mm_aesenc_si128(s[0], rc4_k);
        s[1] = _mm_aesenc_si128(s[1], RC[base + 5]);
        s[2] = _mm_aesenc_si128(s[2], RC[base + 6]);
        s[3] = _mm_aesenc_si128(s[3], RC[base + 7]);

        tmp  = _mm_unpacklo_epi32(s[0], s[1]);
        s[0] = _mm_unpackhi_epi32(s[0], s[1]);
        s[1] = _mm_unpacklo_epi32(s[2], s[3]);
        s[2] = _mm_unpackhi_epi32(s[2], s[3]);
        s[3] = _mm_unpacklo_epi32(s[0], s[2]);
        s[0] = _mm_unpackhi_epi32(s[0], s[2]);
        s[2] = _mm_unpackhi_epi32(s[1], tmp);
        s[1] = _mm_unpacklo_epi32(s[1], tmp);
    }

    s[0] = _mm_xor_si128(s[0], _mm_loadu_si128((__m128i*)(in +  0)));
    s[1] = _mm_xor_si128(s[1], _mm_loadu_si128((__m128i*)(in + 16)));
    s[2] = _mm_xor_si128(s[2], _mm_loadu_si128((__m128i*)(in + 32)));
    s[3] = _mm_xor_si128(s[3], _mm_loadu_si128((__m128i*)(in + 48)));

    _mm_storeu_si128((__m128i*)(out +  0), s[0]);
    _mm_storeu_si128((__m128i*)(out + 16), s[2]);
}

/* ═══════════════════════════════════════════════════════════════════
 * VerusHash 2.1 pipeline (simplified, captures core structure)
 *
 * Input: 140-byte header with 32-byte nonce at bytes 108-139
 *        (ver4 + prev32 + merkle32 + reserved32 + ts4 + bits4 + nonce32)
 * Process: pad to 192 bytes (3 × 64), run Haraka-512 chain
 * Key: derived from last 32 bytes of header (the nonce itself)
 * Output: 32-byte hash
 * ═══════════════════════════════════════════════════════════════════ */
#define HEADER_LEN 140

static void verus_hash_real_compute(const uint8_t header[HEADER_LEN],
                                    uint8_t out[32])
{
    /* Pad to 192 bytes */
    uint8_t padded[192];
    memcpy(padded, header, HEADER_LEN);
    memset(padded + HEADER_LEN, 0, 192 - HEADER_LEN);

    /* Running state: 32 bytes, initialized from first Haraka call */
    uint8_t state[32];
    haraka512(padded, state);

    /* Mix remaining two 64-byte chunks into state */
    uint8_t chunk[64];

    /* Chunk 2: bytes 64-127, XOR'd with current state (first 32 bytes) */
    memcpy(chunk, padded + 64, 64);
    for (int i = 0; i < 32; i++) chunk[i] ^= state[i];
    haraka512(chunk, state);

    /* Chunk 3: bytes 128-191, XOR'd with current state */
    memcpy(chunk, padded + 128, 64);
    for (int i = 0; i < 32; i++) chunk[i] ^= state[i];

    /* Final keyed Haraka: key = last 32 bytes of original header (the nonce) */
    haraka512_keyed(chunk, out, header + 108);
}

/* ── Variant with configurable nonce position for scan tests ──── */
/* Build 140-byte header: fixed template + 4-byte LE nonce at bytes 108-111 */
static void make_header(const uint8_t tmpl[HEADER_LEN],
                        uint32_t nonce32,
                        uint8_t hdr[HEADER_LEN])
{
    memcpy(hdr, tmpl, HEADER_LEN);
    /* Overwrite first 4 bytes of the 32-byte nonce field */
    hdr[108] = (uint8_t)(nonce32);
    hdr[109] = (uint8_t)(nonce32 >> 8);
    hdr[110] = (uint8_t)(nonce32 >> 16);
    hdr[111] = (uint8_t)(nonce32 >> 24);
}

/* ═══════════════════════════════════════════════════════════════════
 * Python wrappers
 * ═══════════════════════════════════════════════════════════════════ */

/* haraka512(data_bytes64) -> bytes(32) — raw Haraka-512 */
static PyObject *py_haraka512(PyObject *self, PyObject *args)
{
    const char *data; Py_ssize_t dlen;
    if (!PyArg_ParseTuple(args, "y#", &data, &dlen)) return NULL;
    if (dlen != 64) {
        PyErr_SetString(PyExc_ValueError, "need exactly 64 bytes");
        return NULL;
    }
    init_rc();
    uint8_t out[32];
    haraka512((const uint8_t*)data, out);
    return PyBytes_FromStringAndSize((char*)out, 32);
}

/* verus_hash_real(header140, nonce_uint32) -> bytes(32) */
static PyObject *py_verus_hash_real(PyObject *self, PyObject *args)
{
    const char *tmpl; Py_ssize_t tlen;
    unsigned long nonce32;
    if (!PyArg_ParseTuple(args, "y#k", &tmpl, &tlen, &nonce32)) return NULL;
    if (tlen != HEADER_LEN) {
        PyErr_SetString(PyExc_ValueError, "need 140-byte header");
        return NULL;
    }
    init_rc();
    uint8_t hdr[HEADER_LEN];
    make_header((const uint8_t*)tmpl, (uint32_t)nonce32, hdr);
    uint8_t out[32];
    verus_hash_real_compute(hdr, out);
    return PyBytes_FromStringAndSize((char*)out, 32);
}

/* scan_winners_real(header_tmpl140, scan_range, target32) -> list[int] */
static PyObject *py_scan_winners_real(PyObject *self, PyObject *args)
{
    const char *tmpl;   Py_ssize_t tlen;
    uint64_t    scan_range;
    const char *target; Py_ssize_t targlen;

    if (!PyArg_ParseTuple(args, "y#Ky#", &tmpl, &tlen,
                          &scan_range, &target, &targlen))
        return NULL;
    if (tlen != HEADER_LEN) {
        PyErr_SetString(PyExc_ValueError, "need 140-byte header");
        return NULL;
    }
    if (targlen != 32) {
        PyErr_SetString(PyExc_ValueError, "target must be 32 bytes");
        return NULL;
    }

    init_rc();
    PyObject *winners = PyList_New(0);
    uint8_t hdr[HEADER_LEN];
    uint8_t out[32];

    for (uint64_t n = 0; n < scan_range; n++) {
        make_header((const uint8_t*)tmpl, (uint32_t)n, hdr);
        verus_hash_real_compute(hdr, out);
        if (memcmp(out, target, 32) < 0) {
            PyObject *item = PyLong_FromUnsignedLongLong(n);
            PyList_Append(winners, item);
            Py_DECREF(item);
        }
    }
    return winners;
}

/* test_diffusion(header_tmpl140, nonce, flip_bit) -> (hash_orig, hash_flipped)
 * Flip one bit in the nonce field and show avalanche effect */
static PyObject *py_test_diffusion(PyObject *self, PyObject *args)
{
    const char *tmpl; Py_ssize_t tlen;
    unsigned long nonce32;
    int flip_bit;
    if (!PyArg_ParseTuple(args, "y#ki", &tmpl, &tlen, &nonce32, &flip_bit))
        return NULL;
    if (tlen != HEADER_LEN) {
        PyErr_SetString(PyExc_ValueError, "need 140-byte header");
        return NULL;
    }

    init_rc();

    uint8_t hdr_orig[HEADER_LEN], hdr_flip[HEADER_LEN];
    make_header((const uint8_t*)tmpl, (uint32_t)nonce32,       hdr_orig);
    make_header((const uint8_t*)tmpl, (uint32_t)nonce32, hdr_flip);

    /* Flip one bit in the nonce region (bytes 108-139) */
    int byte_off = 108 + (flip_bit / 8);
    int bit_off  = flip_bit % 8;
    hdr_flip[byte_off] ^= (1 << bit_off);

    uint8_t out_orig[32], out_flip[32];
    verus_hash_real_compute(hdr_orig, out_orig);
    verus_hash_real_compute(hdr_flip, out_flip);

    return Py_BuildValue("y#y#", out_orig, 32, out_flip, 32);
}

/* ── Module ──────────────────────────────────────────────────────── */
static PyMethodDef VerusRealMethods[] = {
    {"haraka512",         py_haraka512,         METH_VARARGS,
     "haraka512(data64) -> bytes(32)  — raw Haraka-512"},
    {"verus_hash_real",   py_verus_hash_real,   METH_VARARGS,
     "verus_hash_real(hdr140, nonce) -> bytes(32)"},
    {"scan_winners_real", py_scan_winners_real, METH_VARARGS,
     "scan_winners_real(hdr140, scan_range, target32) -> list[int]"},
    {"test_diffusion",    py_test_diffusion,    METH_VARARGS,
     "test_diffusion(hdr140, nonce, flip_bit) -> (orig32, flipped32)"},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef verus_real_module = {
    PyModuleDef_HEAD_INIT, "verus_real", NULL, -1, VerusRealMethods
};

PyMODINIT_FUNC PyInit_verus_real(void) {
    return PyModule_Create(&verus_real_module);
}
