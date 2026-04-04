from setuptools import setup, Extension

scan_ext = Extension(
    "scan",
    sources=["scan.c"],
    libraries=["xxhash"],
    extra_compile_args=["-O3", "-march=native"],
)

heavy_ext = Extension(
    "heavyhash",
    sources=["heavyhash.c"],
    libraries=["ssl", "crypto"],
    extra_compile_args=["-O3", "-march=native"],
)

verus_ext = Extension(
    "verus_aes",
    sources=["verus_aes.c"],
    libraries=["ssl", "crypto"],
    extra_compile_args=["-O3", "-march=native", "-maes", "-msse4.1", "-msse4.2"],
)

rx_ext = Extension(
    "randomx_sim",
    sources=["randomx_sim.c"],
    libraries=["ssl", "crypto"],
    extra_compile_args=["-O3", "-march=native", "-maes", "-msse4.1", "-msse4.2"],
)

eq_ext = Extension(
    "equihash_sim",
    sources=["equihash_sim.c"],
    libraries=["ssl", "crypto"],
    extra_compile_args=["-O3", "-march=native"],
)

pot_ext = Extension(
    "pot_skip",
    sources=["pot_skip.c"],
    libraries=["ssl", "crypto"],
    extra_compile_args=["-O3", "-march=native", "-maes", "-msse4.1", "-msse4.2"],
)

vreal_ext = Extension(
    "verus_real",
    sources=["verus_real.c"],
    libraries=["ssl", "crypto"],
    extra_compile_args=["-O3", "-march=native", "-maes", "-msse4.1", "-msse4.2", "-mssse3"],
)

setup(name="shabtc", ext_modules=[scan_ext, heavy_ext, verus_ext, rx_ext, eq_ext, pot_ext, vreal_ext])
