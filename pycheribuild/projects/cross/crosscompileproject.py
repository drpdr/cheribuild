import os
import inspect
import pprint
import shutil
from enum import Enum
from pathlib import Path


from ...config.loader import ComputedDefaultValue
from ...config.chericonfig import CrossCompileTarget
from ..cheribsd import BuildCHERIBSD
from ..llvm import BuildLLVM
from ...project import *
from ...utils import *

__all__ = ["CheriConfig", "CrossCompileCMakeProject", "CrossCompileAutotoolsProject", "CrossCompileTarget",
           "CrossCompileProject", "CrossInstallDir"]

class CrossInstallDir(Enum):
    NONE = 0
    CHERIBSD_ROOTFS = 1
    SDK = 2

defaultTarget = ComputedDefaultValue(
    function=lambda config, project: config.crossCompileTarget.value,
    asString="'cheri' unless -xmips/-xhost is set")

def _installDir(config: CheriConfig, project: "CrossCompileProject"):
    if project.crossCompileTarget == CrossCompileTarget.NATIVE:
        return config.sdkDir
    if project.crossInstallDir == CrossInstallDir.CHERIBSD_ROOTFS:
        if project.crossCompileTarget == CrossCompileTarget.CHERI:
            targetName = "cheri" + config.cheriBitsStr
        else:
            assert project.crossCompileTarget == CrossCompileTarget.MIPS
            targetName = "mips"
        return Path(BuildCHERIBSD.rootfsDir(config) / "opt" / targetName / project.projectName.lower())
    elif project.crossInstallDir == CrossInstallDir.SDK:
        return config.sdkSysrootDir
    fatalError("Unknown install dir for", project.projectName)

def _installDirMessage(project: "CrossCompileProject"):
    if project.crossInstallDir == CrossInstallDir.CHERIBSD_ROOTFS:
        return "$CHERIBSD_ROOTFS/extra/" + project.projectName.lower() + " or $CHERI_SDK for --xhost build"
    elif project.crossInstallDir == CrossInstallDir.SDK:
        return "$CHERI_SDK/sysroot for cross builds or $CHERI_SDK for --xhost build"
    return "UNKNOWN"


class CrossCompileMixin(object):
    doNotAddToTargets = True
    crossInstallDir = CrossInstallDir.CHERIBSD_ROOTFS
    defaultInstallDir = ComputedDefaultValue(function=_installDir, asString=_installDirMessage)
    appendCheriBitsToBuildDir = True
    dependencies = lambda cls: ["freestanding-sdk"] if cls.baremetal else ["cheribsd-sdk"]
    defaultLinker = "lld"
    baremetal = False
    forceDefaultCC = False  # for some reason ICU binaries build during build crash -> fall back to /usr/bin/cc there
    crossCompileTarget = None  # type: CrossCompileTarget
    defaultOptimizationLevel = ["-O2"]
    warningFlags = ["-Wall", "-Werror=cheri-capability-misuse", "-Werror=implicit-function-declaration",
                    "-Werror=format", "-Werror=undefined-internal", "-Werror=incompatible-pointer-types",
                    "-Werror=mips-cheri-prototypes"]

    def __init__(self, config: CheriConfig, target_arch: CrossCompileTarget, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        if target_arch:
            self.crossCompileTarget = target_arch
        self.compiler_dir = self.config.sdkBinDir
        # Use the compiler from the build directory for native builds to get stddef.h (which will be deleted)
        if self.crossCompileTarget == CrossCompileTarget.NATIVE:
            if (BuildLLVM.buildDir / "bin/clang").exists():
                self.compiler_dir = BuildLLVM.buildDir / "bin"

        self.targetTriple = None
        # compiler flags:
        if self.crossCompileTarget == CrossCompileTarget.NATIVE:
            self.COMMON_FLAGS = []
            self.targetTriple = self.get_host_triple()
            if self.crossInstallDir == CrossInstallDir.SDK:
                self.installDir = self.config.sdkDir
            elif self.crossInstallDir == CrossInstallDir.CHERIBSD_ROOTFS:
                self.installDir = self.buildDir / "test-install-prefix"
            else:
                assert self.installDir, "must be set"
        else:
            self.COMMON_FLAGS = ["-integrated-as", "-pipe", "-G0"]
            if not self.baremetal:
                self.COMMON_FLAGS.append("-msoft-float")

            # clang currently gets the TLS model wrong:
            # https://github.com/CTSRD-CHERI/cheribsd/commit/f863a7defd1bdc797712096b6778940cfa30d901
            self.COMMON_FLAGS.append("-ftls-model=initial-exec")
            # use *-*-freebsd12 to default to libc++
            if self.crossCompileTarget == CrossCompileTarget.CHERI:
                self.targetTriple = "cheri-unknown-freebsd" if not self.baremetal else "cheri-qemu-elf"
                # TODO: should we use -mcpu=cheri128/256?
                self.COMMON_FLAGS.extend(["-mabi=purecap", "-mcpu=mips4", "-cheri=" + self.config.cheriBitsStr])
            else:
                assert self.crossCompileTarget == CrossCompileTarget.MIPS
                self.targetTriple = "mips64-unknown-freebsd" if not self.baremetal else "mips64-qemu-elf"
                self.COMMON_FLAGS.append("-integrated-as")
                self.COMMON_FLAGS.append("-mabi=n64")
                self.COMMON_FLAGS.append("-mcpu=mips4")
                self.COMMON_FLAGS.append("-Wno-unused-command-line-argument")
                if not self.baremetal:
                    self.COMMON_FLAGS.append("-stdlib=libc++")
                else:
                    self.COMMON_FLAGS.append("-fno-pic")
                    # self.COMMON_FLAGS.append("-mabicalls")
                    if self.projectName != "newlib-baremetal":
                        assert self.baremetal
                        # Currently we need these flags to build anything against newlib baremetal
                        self.COMMON_FLAGS.append("-D_GNU_SOURCE=1")  # needed for the locale functions
                        self.COMMON_FLAGS.append("-D_POSIX_MONOTONIC_CLOCK=1")  # pretend that we have a monotonic clock
                        self.COMMON_FLAGS.append("-D_POSIX_TIMERS=1")  # pretend that we have a monotonic clock
            if self.useMxgot:
                self.COMMON_FLAGS.append("-mxgot")

            self.sdkSysroot = self.config.sdkSysrootDir
            if self.baremetal:
                self.sdkSysroot = self.config.sdkDir/ "baremetal" / self.targetTriple

            if self.crossInstallDir == CrossInstallDir.SDK:
                if self.baremetal:
                    self.installDir = self.sdkSysroot
                    # self.destdir = Path("/")
                else:
                    self.installPrefix = "/usr/local"
                    self.destdir = config.sdkSysrootDir
            elif self.crossInstallDir == CrossInstallDir.CHERIBSD_ROOTFS:
                self.installPrefix = Path("/", self.installDir.relative_to(BuildCHERIBSD.rootfsDir(config)))
                self.destdir = BuildCHERIBSD.rootfsDir(config)
            else:
                assert self.installPrefix and self.destdir, "both must be set!"

        if self.debugInfo:
            self.COMMON_FLAGS.append("-ggdb")
        self.CFLAGS = []
        self.CXXFLAGS = []
        self.ASMFLAGS = []
        self.LDFLAGS = []

    @property
    def targetTripleWithVersion(self):
        # we need to append the FreeBSD version to pick up the correct C++ standard library
        if self.compiling_for_host() or self.baremetal:
            return self.targetTriple
        else:
            # anything over 10 should use libc++ by default
            return self.targetTriple + "12"

    def get_host_triple(self):
        compiler = getCompilerInfo(self.config.clangPath if self.config.clangPath else shutil.which("cc"))
        return compiler.default_target

    @property
    def sizeof_void_ptr(self):
        if self.crossCompileTarget in (CrossCompileTarget.MIPS, CrossCompileTarget.NATIVE):
            return 8
        elif self.config.cheriBits == 128:
            return 16
        else:
            assert self.config.cheriBits == 256
            return 32

    @property
    def default_ldflags(self):
        if self.crossCompileTarget == CrossCompileTarget.NATIVE:
            # return ["-fuse-ld=" + self.linker]
            return []
        elif self.crossCompileTarget == CrossCompileTarget.CHERI:
            emulation = "elf64btsmip_cheri_fbsd" if not self.baremetal else "elf64btsmip_cheri"
            abi = "purecap"
        elif self.crossCompileTarget == CrossCompileTarget.MIPS:
            emulation = "elf64btsmip_fbsd" if not self.baremetal else "elf64btsmip"
            abi = "n64"
        else:
            fatalError("Logic error!")
            return []
        result = ["-mabi=" + abi,
                  "-Wl,-m" + emulation,
                  "-fuse-ld=" + self.linker,
                  "-Wl,-z,notext",  # needed so that LLD allows text relocations
                  "-B" + str(self.config.sdkBinDir)]
        if not self.baremetal:
            result.append("--sysroot=" + str(self.sdkSysroot))
        if self.compiling_for_cheri() and self.newCapRelocs:
            # TODO: check that we are using LLD and not BFD
            result += ["-no-capsizefix", "-Wl,-process-cap-relocs", "-Wl,-verbose"]
        if self.config.withLibstatcounters:
            #if self.linkDynamic:
            #    result.append("-lstatcounters")
            #else:
            result += ["-Wl,--whole-archive", "-lstatcounters", "-Wl,--no-whole-archive"]
        return result

    @property
    def CC(self):
        # on MacOS compiling with the SDK clang doesn't seem to work as expected (it picks the wrong linker)
        if self.compiling_for_host() and (not self.config.use_sdk_clang_for_native_xbuild or IS_MAC):
            return self.config.clangPath if not self.forceDefaultCC else Path("cc")
        use_prefixed_cc = not self.compiling_for_host() and not self.baremetal
        compiler_name = self.targetTriple + "-clang" if use_prefixed_cc else "clang"
        return self.compiler_dir / compiler_name

    @property
    def CXX(self):
        if self.compiling_for_host() and not self.config.use_sdk_clang_for_native_xbuild:
            return self.config.clangPlusPlusPath if not self.forceDefaultCC else Path("c++")
        use_prefixed_cxx = not self.compiling_for_host() and not self.baremetal
        compiler_name = self.targetTriple + "-clang++" if use_prefixed_cxx else "clang++"
        return self.compiler_dir / compiler_name

    @classmethod
    def setupConfigOptions(cls, **kwargs):
        super().setupConfigOptions(**kwargs)
        cls.useMxgot = cls.addBoolOption("use-mxgot", help="Compile without -mxgot flag (should not be needed when using lld)")
        cls.linker = cls.addConfigOption("linker", default=cls.defaultLinker,
                                         help="The linker to use (`lld` or `bfd`) (lld is  better but may"
                                              " not work for some projects!)")
        cls.debugInfo = cls.addBoolOption("debug-info", help="build with debug info", default=True)
        cls.optimizationFlags = cls.addConfigOption("optimization-flags", kind=list, metavar="OPTIONS",
                                                    default=cls.defaultOptimizationLevel)
        # TODO: check if LLD supports it and if yes default to true?
        cls.newCapRelocs = cls.addBoolOption("new-cap-relocs", help="Use the new __cap_relocs processing in LLD", default=False)
        if inspect.getattr_static(cls, "crossCompileTarget") is None:
            cls.crossCompileTarget = cls.addConfigOption("target", help="The target to build for (`cheri` or `mips` or `native`)",
                                                 default=defaultTarget, choices=["cheri", "mips", "native"],
                                                 kind=CrossCompileTarget)

    def compiling_for_mips(self):
        return self.crossCompileTarget == CrossCompileTarget.MIPS

    def compiling_for_cheri(self):
        return self.crossCompileTarget == CrossCompileTarget.CHERI

    def compiling_for_host(self):
        return self.crossCompileTarget == CrossCompileTarget.NATIVE

    @property
    def pkgconfig_dirs(self):
        if self.compiling_for_mips():
            return str(self.sdkSysroot / "usr/lib/pkgconfig") + ":" + str(self.sdkSysroot / "usr/local/lib/pkgconfig")
        if self.compiling_for_cheri():
            return str(self.sdkSysroot / "usr/libcheri/pkgconfig") + ":" + str(self.sdkSysroot / "usr/local/libcheri/pkgconfig")
        return None

    def configure(self, **kwargs):
        env = dict()
        if not self.compiling_for_host():
            env.update(PKG_CONFIG_LIBDIR=self.pkgconfig_dirs, PKG_CONFIG_SYSROOT_DIR=self.config.sdkSysrootDir)
        with setEnv():
            super().configure(**kwargs)


class CrossCompileProject(CrossCompileMixin, Project):
    doNotAddToTargets = True


class CrossCompileCMakeProject(CrossCompileMixin, CMakeProject):
    doNotAddToTargets = True  # only used as base class
    defaultCMakeBuildType = "RelWithDebInfo"  # default to O2

    @classmethod
    def setupConfigOptions(cls, **kwargs):
        super().setupConfigOptions(**kwargs)

    def __init__(self, config: CheriConfig, target_arch: CrossCompileTarget, generator: CMakeProject.Generator=CMakeProject.Generator.Ninja):
        super().__init__(config, target_arch, generator)
        # This must come first:
        if self.crossCompileTarget == CrossCompileTarget.NATIVE:
            self._cmakeTemplate = includeLocalFile("files/NativeToolchain.cmake.in")
            self.toolchainFile = self.buildDir / "NativeToolchain.cmake"
        else:
            self._cmakeTemplate = includeLocalFile("files/CheriBSDToolchain.cmake.in")
            self.toolchainFile = self.buildDir / "CheriBSDToolchain.cmake"
        self.add_cmake_options(CMAKE_TOOLCHAIN_FILE=self.toolchainFile)
        # The toolchain files need at least CMake 3.6
        self.set_minimum_cmake_version(3, 6)

    def _prepareToolchainFile(self, **kwargs):
        configuredTemplate = self._cmakeTemplate
        for key, value in kwargs.items():
            if value is None:
                continue
            strval = " ".join(value) if isinstance(value, list) else str(value)
            assert "@" + key + "@" in configuredTemplate, key
            configuredTemplate = configuredTemplate.replace("@" + key + "@", strval)
        assert "@" not in configuredTemplate, configuredTemplate
        self.writeFile(contents=configuredTemplate, file=self.toolchainFile, overwrite=True)

    def configure(self, **kwargs):
        if self.compiling_for_host():
            common_flags = self.COMMON_FLAGS
        else:
            self.COMMON_FLAGS.append("-B" + str(self.config.sdkBinDir))
            if not self.baremetal and self._get_cmake_version() < (3, 9, 0) and not (self.sdkSysroot / "usr/local/lib/cheri").exists():
                warningMessage("Workaround for missing custom lib suffix in CMake < 3.9")
                # create a /usr/lib/cheri -> /usr/libcheri symlink so that cmake can find the right libraries
                self.createSymlink(Path("../libcheri"), self.sdkSysroot / "usr/lib/cheri", relative=True,
                                   cwd=self.sdkSysroot / "usr/lib")
                self.makedirs(self.sdkSysroot / "usr/local/lib")
                self.makedirs(self.sdkSysroot / "usr/local/libcheri")
                self.createSymlink(Path("../libcheri"), self.sdkSysroot / "usr/local/lib/cheri",
                                   relative=True, cwd=self.sdkSysroot / "usr/local/lib")
            common_flags = self.COMMON_FLAGS + self.warningFlags + ["-target", self.targetTripleWithVersion]

        if self.compiling_for_cheri():
            add_lib_suffix = """
# cheri libraries are found in /usr/libcheri:
if("${CMAKE_VERSION}" VERSION_LESS 3.9)
  # message(STATUS "CMAKE < 3.9 HACK to find libcheri libraries")
  # need to create a <sysroot>/usr/lib/cheri -> <sysroot>/usr/libcheri symlink 
  set(CMAKE_LIBRARY_ARCHITECTURE "cheri")
  set(CMAKE_SYSTEM_LIBRARY_PATH "${CMAKE_FIND_ROOT_PATH}/usr/libcheri;${CMAKE_FIND_ROOT_PATH}/usr/local/libcheri")
else()
    set(CMAKE_FIND_LIBRARY_CUSTOM_LIB_SUFFIX "cheri")
endif()
set(LIB_SUFFIX "cheri" CACHE INTERNAL "")
"""
            processor = "CHERI (MIPS IV compatible)"
        elif self.compiling_for_mips():
            add_lib_suffix = "# no lib suffix for mips libraries"
            processor = "BERI (MIPS IV compatible)"
        else:
            add_lib_suffix = None
            processor = None

        if self.compiling_for_host():
            system_name = None
        else:
            system_name = "Generic" if self.baremetal else "FreeBSD"
        self._prepareToolchainFile(
            TOOLCHAIN_SDK_BINDIR=self.config.sdkBinDir,
            TOOLCHAIN_COMPILER_BINDIR=self.compiler_dir,
            TOOLCHAIN_TARGET_TRIPLE=self.targetTriple,
            TOOLCHAIN_COMMON_FLAGS=common_flags,
            TOOLCHAIN_C_FLAGS=self.CFLAGS,
            TOOLCHAIN_LINKER_FLAGS=self.LDFLAGS + self.default_ldflags,
            TOOLCHAIN_CXX_FLAGS=self.CXXFLAGS,
            TOOLCHAIN_ASM_FLAGS=self.ASMFLAGS,
            TOOLCHAIN_C_COMPILER=self.CC,
            TOOLCHAIN_CXX_COMPILER=self.CXX,
            TOOLCHAIN_SYSROOT=self.sdkSysroot if not self.compiling_for_host() else None,
            ADD_TOOLCHAIN_LIB_SUFFIX=add_lib_suffix,
            TOOLCHAIN_SYSTEM_PROCESSOR=processor,
            TOOLCHAIN_SYSTEM_NAME=system_name,
            TOOLCHAIN_PKGCONFIG_DIRS=self.pkgconfig_dirs
        )

        if self.generator == CMakeProject.Generator.Ninja:
            # Ninja can't change the RPATH when installing: https://gitlab.kitware.com/cmake/cmake/issues/13934
            # TODO: remove once it has been fixed
            self.add_cmake_options(CMAKE_BUILD_WITH_INSTALL_RPATH=True)
        if self.baremetal and not self.compiling_for_host():
            self.add_cmake_options(CMAKE_EXE_LINKER_FLAGS="-Wl,-T,qemu-malta.ld")
        # TODO: BUILD_SHARED_LIBS=OFF?
        super().configure()


class CrossCompileAutotoolsProject(CrossCompileMixin, AutotoolsProject):
    doNotAddToTargets = True  # only used as base class

    add_host_target_build_config_options = True
    _configure_supports_libdir = True  # override in nginx
    _configure_supports_variables_on_cmdline = True  # override in nginx

    def __init__(self, config: CheriConfig, target_arch: CrossCompileTarget):
        super().__init__(config, target_arch)
        buildhost = self.get_host_triple()
        if not self.compiling_for_host() and self.add_host_target_build_config_options:
            self.configureArgs.extend(["--host=" + self.targetTriple, "--target=" + self.targetTriple,
                                       "--build=" + buildhost])

    @property
    def default_compiler_flags(self):
        if self.compiling_for_host():
            return self.COMMON_FLAGS.copy()
        result = ["-target", self.targetTripleWithVersion] + self.COMMON_FLAGS + self.optimizationFlags
        if not self.baremetal:
            result.append("--sysroot=" + str(self.sdkSysroot))
        result += ["-B" + str(self.config.sdkBinDir)] + self.warningFlags

        return result

    def add_configure_env_arg(self, arg: str, value: str):
        if not value:
            return
        self.configureEnvironment[arg] = str(value)
        if self._configure_supports_variables_on_cmdline:
            self.configureArgs.append(arg + "=" + str(value))

    def add_configure_vars(self, **kwargs):
        for k, v in kwargs.items():
            self.add_configure_env_arg(k, v)

    def set_prog_with_args(self, prog: str, path: Path, args: list):
        fullpath = str(path)
        if args:
            fullpath += " " + " ".join(args)
        self.configureEnvironment[prog] = fullpath
        if self._configure_supports_variables_on_cmdline:
            self.configureArgs.append(prog + "=" + fullpath)

    def configure(self, **kwargs):
        # target triple contains a number suffix -> remove it when computing the compiler name
        if self.compiling_for_cheri() and self._configure_supports_libdir:
            # nginx configure script doesn't understand --libdir
            # make sure that we install to the right directory
            # TODO: can we use relative paths?
            self.configureArgs.append("--libdir=" + str(self.installPrefix) + "/libcheri")

        if not self.baremetal:
            CPPFLAGS = self.default_compiler_flags
            for key in ("CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS"):
                assert key not in self.configureEnvironment
            # autotools overrides CFLAGS -> use CC and CXX vars here
            self.set_prog_with_args("CC", self.CC, CPPFLAGS + self.CFLAGS)
            self.set_prog_with_args("CXX", self.CXX, CPPFLAGS + self.CXXFLAGS)
            # self.add_configure_env_arg("CPPFLAGS", " ".join(CPPFLAGS))
            # self.add_configure_env_arg("CFLAGS", " ".join(CPPFLAGS + self.CFLAGS))
            # self.add_configure_env_arg("CXXFLAGS", " ".join(CPPFLAGS + self.CXXFLAGS))
            # this one seems to work:
            self.add_configure_env_arg("LDFLAGS", " ".join(self.LDFLAGS + self.default_ldflags))

            if not self.compiling_for_host():
                self.set_prog_with_args("CPP", self.compiler_dir / (self.targetTriple + "-clang-cpp"), CPPFLAGS)
                if "lld" in self.linker and (self.compiler_dir / "ld.lld").exists():
                    self.add_configure_env_arg("LD", str(self.compiler_dir / "ld.lld"))

        # remove all empty items from environment:
        env = {k: v for k, v in self.configureEnvironment.items() if v}
        self.configureEnvironment.clear()
        self.configureEnvironment.update(env)
        print(coloured(AnsiColour.yellow, "Cross configure environment:",
                       pprint.pformat(self.configureEnvironment, width=160)))
        super().configure(**kwargs)

    def process(self):
        if not self.compiling_for_host():
            # We run all these commands with $PATH containing $CHERI_SDK/bin to ensure the right tools are used
            with setEnv(PATH=str(self.config.sdkDir / "bin") + ":" + os.getenv("PATH")):
                super().process()
        else:
            # when building the native target we just rely on the host tools in /usr/bin
            super().process()
