"""
Build the quant_core C++ extension via pybind11.

Usage (from the project root, on Windows PowerShell or cmd):

    pip install pybind11 setuptools wheel
    python setup_quant.py build_ext --inplace

That produces a quant_core.<tag>.pyd next to setup_quant.py — Python on this
machine will then `import quant_core` directly. The .pyd is OS- and Python-
version-specific and should NOT be committed to git (covered by .gitignore).

On the production Linux server the same command produces a .so file:

    pip install pybind11 setuptools wheel
    python3 setup_quant.py build_ext --inplace

Compiler requirements:
    Windows: MSVC 19+ (Visual Studio 2019 or 2022 with the
             "Desktop development with C++" workload). Using just the
             standalone Build Tools 2022 is fine.
    Linux:   GCC 9+ or Clang 9+ (C++17).
"""
from setuptools import setup
from pybind11.setup_helpers import Pybind11Extension, build_ext


ext_modules = [
    Pybind11Extension(
        "quant_core",
        ["quant_core.cpp"],
        cxx_std=17,
    ),
]


setup(
    name="quant_core",
    version="0.1.0",
    author="crypto_ai_agent",
    description="Monte Carlo dispersion estimator for the brain aggregator",
    long_description=__doc__,
    ext_modules=ext_modules,
    cmdclass={"build_ext": build_ext},
    zip_safe=False,
    python_requires=">=3.10",
)
