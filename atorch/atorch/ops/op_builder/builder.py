# Modifications Copyright 2023 AntGroups, Inc.
# ATorch Team

# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: Apache-2.0

# DeepSpeed Team

import importlib
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List

import distutils.ccompiler
import distutils.log
import distutils.sysconfig
from distutils.errors import CompileError, LinkError
from packaging.version import Version

from atorch.common.log_utils import default_logger as logger

YELLOW = "\033[93m"
END = "\033[0m"
WARNING = f"{YELLOW} [WARNING] {END}"

DEFAULT_TORCH_EXTENSION_PATH = "/tmp/torch_extensions"
DEFAULT_COMPUTE_CAPABILITIES = "7.0"  # Support Volta architecture (V100)

try:
    import torch
except ImportError:
    print(f"{WARNING} unable to import torch, please install it if you want to pre-compile any atorch ops.")
else:
    TORCH_MAJOR = int(torch.__version__.split(".")[0])
    TORCH_MINOR = int(torch.__version__.split(".")[1])


def installed_cuda_version(name=""):
    import torch.utils.cpp_extension

    cuda_home = torch.utils.cpp_extension.CUDA_HOME
    assert cuda_home is not None, "CUDA_HOME does not exist, unable to compile CUDA op(s)"
    # Ensure there is not a cuda version mismatch between torch and nvcc compiler
    output = subprocess.check_output([cuda_home + "/bin/nvcc", "-V"], universal_newlines=True)
    output_split = output.split()
    release_idx = output_split.index("release")
    release = output_split[release_idx + 1].replace(",", "").split(".")
    # Ignore patch versions, only look at major + minor
    cuda_major, cuda_minor = release[:2]
    return int(cuda_major), int(cuda_minor)


def get_default_compute_capabilities():
    compute_caps = DEFAULT_COMPUTE_CAPABILITIES
    import torch.utils.cpp_extension

    if torch.utils.cpp_extension.CUDA_HOME is not None and installed_cuda_version()[0] >= 11:
        cuda_ver_str = "%s.%s" % installed_cuda_version()
        # Support Ampere architecture (A100)
        compute_caps += ";8.0"
        if Version(cuda_ver_str) >= Version("11.8"):
            # Support Hopper architecture (H100/H100)
            compute_caps += ";9.0"
    return compute_caps


# list compatible minor CUDA versions - so that for example pytorch built with cuda-11.0 can be used
# to build atorch and system-wide installed cuda 11.2
cuda_minor_mismatch_ok = {
    10: [
        "10.0",
        "10.1",
        "10.2",
    ],
    11: ["11.0", "11.1", "11.2", "11.3", "11.4", "11.5", "11.6", "11.7", "11.8"],
    12: ["12.0", "12.1"],
}


def assert_no_cuda_mismatch(name=""):
    cuda_major, cuda_minor = installed_cuda_version(name)
    sys_cuda_version = f"{cuda_major}.{cuda_minor}"
    torch_cuda_version = ".".join(torch.version.cuda.split(".")[:2])
    # This is a show-stopping error, should probably not proceed past this
    if sys_cuda_version != torch_cuda_version:
        if (
            cuda_major in cuda_minor_mismatch_ok
            and sys_cuda_version in cuda_minor_mismatch_ok[cuda_major]
            and torch_cuda_version in cuda_minor_mismatch_ok[cuda_major]
        ):
            print(
                f"Installed CUDA version {sys_cuda_version} does not match the "
                f"version torch was compiled with {torch.version.cuda} "
                "but since the APIs are compatible, accepting this combination"
            )
            return True
        elif os.getenv("ATORCH_SKIP_CUDA_CHECK", "0") == "1":
            print(
                f"{WARNING} ATorch Op Builder: Installed CUDA version {sys_cuda_version} does not match the "
                f"version torch was compiled with {torch.version.cuda}."
                "Detected `ATORCH_SKIP_CUDA_CHECK=1`: Allowing this combination of CUDA, "
                "but it may result in unexpected behavior."
            )
            return True
        raise Exception(
            f">- ATorch Op Builder: Installed CUDA version {sys_cuda_version} does not match the "
            f"version torch was compiled with {torch.version.cuda}, unable to compile "
            "cuda/cpp extensions without a matching cuda version."
        )
    return True


class OpBuilder(ABC):
    def __init__(self, name):
        self.name = name
        self.jit_mode = False
        self.build_for_cpu = False
        self.enable_bf16 = False
        self.error_log = None

    @abstractmethod
    def absolute_name(self):
        """
        Returns absolute build path for cases where the op is pre-installed, e.g., atorch.ops.adam.cpu_adam
        will be installed as something like: atorch/ops/adam/cpu_adam.so
        """
        pass

    @abstractmethod
    def sources(self):
        """
        Returns list of source files for your op, relative to root of atorch package (i.e., ATorch/atorch)
        """
        pass

    @staticmethod
    def validate_torch_version(torch_info):
        install_torch_version = torch_info["version"]
        current_torch_version = ".".join(torch.__version__.split(".")[:2])
        if install_torch_version != current_torch_version:
            raise RuntimeError(
                "PyTorch version mismatch! ATorch ops were compiled and installed "
                "with a different version than what is being used at runtime. "
                f"Please re-install ATorch or switch torch versions. "
                f"Install torch version={install_torch_version}, "
                f"Runtime torch version={current_torch_version}"
            )

    @staticmethod
    def validate_torch_op_version(torch_info):
        current_cuda_version = ".".join(torch.version.cuda.split(".")[:2])
        install_cuda_version = torch_info["cuda_version"]
        if install_cuda_version != current_cuda_version:
            raise RuntimeError(
                "CUDA version mismatch! ATorch ops were compiled and installed "
                "with a different version than what is being used at runtime. "
                f"Please re-install ATorch or switch torch versions. "
                f"Install CUDA version={install_cuda_version}, "
                f"Runtime CUDA version={current_cuda_version}"
            )

    def include_paths(self):
        """
        Returns list of include paths, relative to root of atorch package (i.e., ATorch/atorch)
        """
        return []

    def nvcc_args(self):
        """
        Returns optional list of compiler flags to forward to nvcc when building CUDA sources
        """
        return []

    def cxx_args(self):
        """
        Returns optional list of compiler flags to forward to the build
        """
        return []

    def is_compatible(self, verbose=True):
        """
        Check if all non-python dependencies are satisfied to build this op
        """
        return True

    def extra_ldflags(self):
        return []

    def has_function(self, funcname, libraries, verbose=False):
        """
        Test for existence of a function within a tuple of libraries.

        This is used as a smoke test to check whether a certain library is available.
        As a test, this creates a simple C program that calls the specified function,
        and then distutils is used to compile that program and link it with the specified libraries.
        Returns True if both the compile and link are successful, False otherwise.
        """
        tempdir = None  # we create a temporary directory to hold various files
        filestderr = None  # handle to open file to which we redirect stderr
        oldstderr = None  # file descriptor for stderr
        try:
            # Echo compile and link commands that are used.
            if verbose:
                distutils.log.set_verbosity(1)

            # Create a compiler object.
            compiler = distutils.ccompiler.new_compiler(verbose=verbose)

            # Configure compiler and linker to build according to Python install.
            distutils.sysconfig.customize_compiler(compiler)

            # Create a temporary directory to hold test files.
            tempdir = tempfile.mkdtemp()

            # Define a simple C program that calls the function in question
            prog = "void %s(void); int main(int argc, char** argv) { %s(); return 0; }" % (funcname, funcname)

            # Write the test program to a file.
            filename = os.path.join(tempdir, "test.c")
            with open(filename, "w") as f:
                f.write(prog)

            # Redirect stderr file descriptor to a file to silence compile/link warnings.
            if not verbose:
                filestderr = open(os.path.join(tempdir, "stderr.txt"), "w")
                oldstderr = os.dup(sys.stderr.fileno())
                os.dup2(filestderr.fileno(), sys.stderr.fileno())

            # Workaround for behavior in distutils.ccompiler.CCompiler.object_filenames()
            # Otherwise, a local directory will be used instead of tempdir
            drive, driveless_filename = os.path.splitdrive(filename)
            root_dir = driveless_filename[0] if os.path.isabs(driveless_filename) else ""
            output_dir = os.path.join(drive, root_dir)

            # Attempt to compile the C program into an object file.
            cflags = shlex.split(os.environ.get("CFLAGS", ""))
            objs = compiler.compile([filename], output_dir=output_dir, extra_preargs=self.strip_empty_entries(cflags))

            # Attempt to link the object file into an executable.
            # Be sure to tack on any libraries that have been specified.
            ldflags = shlex.split(os.environ.get("LDFLAGS", ""))
            compiler.link_executable(
                objs,
                os.path.join(tempdir, "a.out"),
                extra_preargs=self.strip_empty_entries(ldflags),
                libraries=libraries,
            )

            # Compile and link succeeded
            return True

        except CompileError:
            return False

        except LinkError:
            return False

        except BaseException as e:
            logger.error(f"Error compiling: {e}")
            return False

        finally:
            # Restore stderr file descriptor and close the stderr redirect file.
            if oldstderr is not None:
                os.dup2(oldstderr, sys.stderr.fileno())
            if filestderr is not None:
                filestderr.close()

            # Delete the temporary directory holding the test program and stderr files.
            if tempdir is not None:
                shutil.rmtree(tempdir)

    def strip_empty_entries(self, args):
        """
        Drop any empty strings from the list of compile and link flags
        """
        return [x for x in args if len(x) > 0]

    def cpu_arch(self):
        try:
            from cpuinfo import get_cpu_info
        except ImportError as e:
            logger.error(f"Error cpu_arch: {e}")
            cpu_info = self._backup_cpuinfo()
            if cpu_info is None:
                return "-march=native"

        try:
            cpu_info = get_cpu_info()
        except Exception as e:
            self.warning(
                f"{self.name} attempted to use `py-cpuinfo` but failed (exception type: {type(e)}, {e}), "
                "falling back to `lscpu` to get this information."
            )
            cpu_info = self._backup_cpuinfo()
            if cpu_info is None:
                return "-march=native"

        if cpu_info["arch"].startswith("PPC_"):
            # gcc does not provide -march on PowerPC, use -mcpu instead
            return "-mcpu=native"
        return "-march=native"

    def is_cuda_enable(self):
        try:
            assert_no_cuda_mismatch(self.name)
            return "-D__ENABLE_CUDA__"
        except BaseException:
            print(
                f"{WARNING} {self.name} cuda is missing or is incompatible with installed torch, "
                "only cpu ops can be compiled!"
            )
            return "-D__DISABLE_CUDA__"
        return "-D__DISABLE_CUDA__"

    def _backup_cpuinfo(self):
        # Construct cpu_info dict from lscpu that is similar to what py-cpuinfo provides
        if not self.command_exists("lscpu"):
            self.warning(
                f"{self.name} attempted to query 'lscpu' after failing to use py-cpuinfo "
                "to detect the CPU architecture. 'lscpu' does not appear to exist on "
                "your system, will fall back to use -march=native and non-vectorized execution."
            )
            return None
        result = subprocess.check_output("lscpu", shell=True)
        result = result.decode("utf-8").strip().lower()

        cpu_info = {}
        cpu_info["arch"] = None
        cpu_info["flags"] = ""
        if "genuineintel" in result or "authenticamd" in result:
            cpu_info["arch"] = "X86_64"
            if "avx512" in result:
                cpu_info["flags"] += "avx512,"
            elif "avx512f" in result:
                cpu_info["flags"] += "avx512f,"
            if "avx2" in result:
                cpu_info["flags"] += "avx2"
        elif "ppc64le" in result:
            cpu_info["arch"] = "PPC_"

        return cpu_info

    def simd_width(self):
        try:
            from cpuinfo import get_cpu_info
        except ImportError as e:
            logger.error(f"Error simd_width: {e}")
            cpu_info = self._backup_cpuinfo()
            if cpu_info is None:
                return "-D__SCALAR__"

        try:
            cpu_info = get_cpu_info()
        except Exception as e:
            self.warning(
                f"{self.name} attempted to use `py-cpuinfo` but failed (exception type: {type(e)}, {e}), "
                "falling back to `lscpu` to get this information."
            )
            cpu_info = self._backup_cpuinfo()
            if cpu_info is None:
                return "-D__SCALAR__"

        if cpu_info["arch"] == "X86_64":
            if "avx512" in cpu_info["flags"] or "avx512f" in cpu_info["flags"]:
                return "-D__AVX512__"
            elif "avx2" in cpu_info["flags"]:
                return "-D__AVX256__"
        return "-D__SCALAR__"

    def command_exists(self, cmd):
        if "|" in cmd:
            cmds = cmd.split("|")
        else:
            cmds = [cmd]
        valid = False
        for cmd in cmds:
            result = subprocess.Popen(f"type {cmd}", stdout=subprocess.PIPE, shell=True)
            valid = valid or result.wait() == 0

        if not valid and len(cmds) > 1:
            print(f"{WARNING} {self.name} requires one of the following commands '{cmds}', but it does not exist!")
        elif not valid and len(cmds) == 1:
            print(f"{WARNING} {self.name} requires the '{cmd}' command, but it does not exist!")
        return valid

    def warning(self, msg):
        self.error_log = f"{msg}"
        print(f"{WARNING} {msg}")

    def atorch_src_path(self, code_path):
        if os.path.isabs(code_path):
            return code_path
        else:
            return os.path.join(Path(__file__).parent.parent.absolute(), code_path)

    def builder(self):
        from torch.utils.cpp_extension import CppExtension

        sources = [self.atorch_src_path(path) for path in self.sources()]
        include_paths = [self.atorch_src_path(path) for path in self.include_paths()]

        return CppExtension(
            name=self.absolute_name(),
            sources=self.strip_empty_entries(sources),
            include_dirs=self.strip_empty_entries(include_paths),
            extra_compile_args={"cxx": self.strip_empty_entries(self.cxx_args())},
            extra_link_args=self.strip_empty_entries(self.extra_ldflags()),
        )

    def load(self, verbose=True):
        from atorch.ops.git_version_info import installed_ops, torch_info

        if installed_ops.get(self.name, False):
            # Ensure the op we're about to load was compiled with the same
            # torch/cuda versions we are currently using at runtime.
            self.validate_torch_version(torch_info)
            if torch.cuda.is_available() and isinstance(self, CUDAOpBuilder):
                self.validate_torch_op_version(torch_info)

            return importlib.import_module(self.absolute_name())
        else:
            return self.jit_load(verbose)

    def jit_load(self, verbose=True):
        if not self.is_compatible(verbose):
            raise RuntimeError(
                f"Unable to JIT load the {self.name} op due to it not being compatible due to "
                "hardware/software issue. {self.error_log}"
            )
        try:
            import ninja  # noqa: F401 # type: ignore
        except ImportError:
            raise RuntimeError(f"Unable to JIT load the {self.name} op due to ninja not being installed.")

        if isinstance(self, CUDAOpBuilder):
            try:
                assert_no_cuda_mismatch(self.name)
                self.build_for_cpu = False
            except BaseException:
                self.build_for_cpu = True

        self.jit_mode = True
        from torch.utils.cpp_extension import load

        start_build = time.time()
        sources = [self.atorch_src_path(path) for path in self.sources()]
        extra_include_paths = [self.atorch_src_path(path) for path in self.include_paths()]

        # Torch will try and apply whatever CCs are in the arch list at compile time,
        # we have already set the intended targets ourselves we know that will be
        # needed at runtime. This prevents CC collisions such as multiple __half
        # implementations. Stash arch list to reset after build.
        torch_arch_list = None
        if "TORCH_CUDA_ARCH_LIST" in os.environ:
            torch_arch_list = os.environ.get("TORCH_CUDA_ARCH_LIST")
            os.environ["TORCH_CUDA_ARCH_LIST"] = ""

        nvcc_args = self.strip_empty_entries(self.nvcc_args())
        cxx_args = self.strip_empty_entries(self.cxx_args())

        if isinstance(self, CUDAOpBuilder):
            if not self.build_for_cpu and self.enable_bf16:
                cxx_args.append("-DBF16_AVAILABLE")
                nvcc_args.append("-DBF16_AVAILABLE")

        op_module = load(
            name=self.name,
            sources=self.strip_empty_entries(sources),
            extra_include_paths=self.strip_empty_entries(extra_include_paths),
            extra_cflags=cxx_args,
            extra_cuda_cflags=nvcc_args,
            extra_ldflags=self.strip_empty_entries(self.extra_ldflags()),
            verbose=verbose,
        )

        build_duration = time.time() - start_build
        if verbose:
            print(f"Time to load {self.name} op: {build_duration} seconds")

        # Reset arch list so we are not silently removing it for other possible use cases
        if torch_arch_list:
            os.environ["TORCH_CUDA_ARCH_LIST"] = torch_arch_list

        return op_module


class CUDAOpBuilder(OpBuilder):
    def compute_capability_args(self, cross_compile_archs=None):
        """
        Returns nvcc compute capability compile flags.

        1. `TORCH_CUDA_ARCH_LIST` takes priority over `cross_compile_archs`.
        2. If neither is set default compute capabilities will be used
        3. Under `jit_mode` compute capabilities of all visible cards will be used plus PTX

        Format:

        - `TORCH_CUDA_ARCH_LIST` may use ; or whitespace separators. Examples:

        TORCH_CUDA_ARCH_LIST="6.1;7.5;8.6" pip install ...
        TORCH_CUDA_ARCH_LIST="6.0 6.1 7.0 7.5 8.0 8.6+PTX" pip install ...

        - `cross_compile_archs` uses ; separator.

        """
        ccs = []
        if self.jit_mode:
            # Compile for underlying architectures since we know those at runtime
            for i in range(torch.cuda.device_count()):
                CC_MAJOR, CC_MINOR = torch.cuda.get_device_capability(i)
                cc = f"{CC_MAJOR}.{CC_MINOR}"
                if cc not in ccs:
                    ccs.append(cc)
            ccs = sorted(ccs)
            ccs[-1] += "+PTX"
        else:
            # Cross-compile mode, compile for various architectures
            # env override takes priority
            cross_compile_archs_env = os.environ.get("TORCH_CUDA_ARCH_LIST", None)
            if cross_compile_archs_env is not None:
                if cross_compile_archs is not None:
                    print(
                        f"{WARNING} env var `TORCH_CUDA_ARCH_LIST={cross_compile_archs_env}` "
                        "overrides `cross_compile_archs={cross_compile_archs}`"
                    )
                cross_compile_archs = cross_compile_archs_env.replace(" ", ";")
            else:
                if cross_compile_archs is None:
                    cross_compile_archs = get_default_compute_capabilities()
            ccs = cross_compile_archs.split(";")

        ccs = self.filter_ccs(ccs)
        if len(ccs) == 0:
            raise RuntimeError(
                f"Unable to load {self.name} op due to no compute capabilities remaining after filtering"
            )

        args = []
        self.enable_bf16 = True
        for cc in ccs:
            num = cc[0] + cc[2]
            args.append(f"-gencode=arch=compute_{num},code=sm_{num}")
            if cc.endswith("+PTX"):
                args.append(f"-gencode=arch=compute_{num},code=compute_{num}")

            if int(cc[0]) <= 7:
                self.enable_bf16 = False

        return args

    def filter_ccs(self, ccs: List[str]):
        """
        Prune any compute capabilities that are not compatible with the builder. Should log
        which CCs have been pruned.
        """
        return ccs

    def version_dependent_macros(self):
        # Fix from apex that might be relevant for us as well, related to https://github.com/NVIDIA/apex/issues/456
        version_ge_1_1 = []
        if (TORCH_MAJOR > 1) or (TORCH_MAJOR == 1 and TORCH_MINOR > 0):
            version_ge_1_1 = ["-DVERSION_GE_1_1"]
        version_ge_1_3 = []
        if (TORCH_MAJOR > 1) or (TORCH_MAJOR == 1 and TORCH_MINOR > 2):
            version_ge_1_3 = ["-DVERSION_GE_1_3"]
        version_ge_1_5 = []
        if (TORCH_MAJOR > 1) or (TORCH_MAJOR == 1 and TORCH_MINOR > 4):
            version_ge_1_5 = ["-DVERSION_GE_1_5"]
        return version_ge_1_1 + version_ge_1_3 + version_ge_1_5

    def is_compatible(self, verbose=True):
        return super().is_compatible(verbose)

    def builder(self):
        try:
            assert_no_cuda_mismatch(self.name)
            self.build_for_cpu = False
        except BaseException:
            self.build_for_cpu = True

        if self.build_for_cpu:
            from torch.utils.cpp_extension import CppExtension as ExtensionBuilder
        else:
            from torch.utils.cpp_extension import CUDAExtension as ExtensionBuilder

        compile_args = (
            {"cxx": self.strip_empty_entries(self.cxx_args())}
            if self.build_for_cpu
            else {"cxx": self.strip_empty_entries(self.cxx_args()), "nvcc": self.strip_empty_entries(self.nvcc_args())}
        )

        if not self.build_for_cpu and self.enable_bf16:
            compile_args["cxx"].append("-DBF16_AVAILABLE")

        sources = [self.atorch_src_path(path) for path in self.sources()]
        include_paths = [self.atorch_src_path(path) for path in self.include_paths()]

        cuda_ext = ExtensionBuilder(
            name=self.absolute_name(),
            sources=self.strip_empty_entries(sources),
            include_dirs=self.strip_empty_entries(include_paths),
            libraries=self.strip_empty_entries(self.libraries_args()),
            extra_compile_args=compile_args,
            extra_link_args=self.strip_empty_entries(self.extra_ldflags()),
        )
        return cuda_ext

    def cxx_args(self):
        if sys.platform == "win32":
            return ["-O2"]
        else:
            return ["-O3", "-std=c++17", "-g", "-Wno-reorder"]

    def nvcc_args(self):
        if self.build_for_cpu:
            return []
        args = ["-O3"]
        cuda_major, _ = installed_cuda_version()
        args += [
            "-allow-unsupported-compiler" if sys.platform == "win32" else "",
            "--use_fast_math",
            "-std=c++17" if cuda_major > 10 else "-std=c++14",
            "-U__CUDA_NO_HALF_OPERATORS__",
            "-U__CUDA_NO_HALF_CONVERSIONS__",
            "-U__CUDA_NO_HALF2_OPERATORS__",
        ]
        if os.environ.get("ATORCH_DEBUG_CUDA_BUILD", "0") == "1":
            args.append("--ptxas-options=-v")
        args += self.compute_capability_args()
        return args

    def libraries_args(self):
        if self.build_for_cpu:
            return []

        if sys.platform == "win32":
            return ["cublas", "curand"]
        else:
            return []


class TorchCPUOpBuilder(CUDAOpBuilder):
    def extra_ldflags(self):
        if self.build_for_cpu:
            return ["-fopenmp"]

        return ["-lcurand"]

    def cxx_args(self):
        import torch

        args = []
        if not self.build_for_cpu:
            CUDA_LIB64 = os.path.join(torch.utils.cpp_extension.CUDA_HOME, "lib64")
            args += super().cxx_args()
            args += [
                f"-L{CUDA_LIB64}",
                "-lcudart",
                "-lcublas",
                "-g",
            ]

        CPU_ARCH = self.cpu_arch()
        SIMD_WIDTH = self.simd_width()
        CUDA_ENABLE = self.is_cuda_enable()
        args += [
            CPU_ARCH,
            "-fopenmp",
            SIMD_WIDTH,
            CUDA_ENABLE,
        ]

        return args