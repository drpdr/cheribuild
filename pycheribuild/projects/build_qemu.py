#
# Copyright (c) 2016 Alex Richardson
# All rights reserved.
#
# This software was developed by SRI International and the University of
# Cambridge Computer Laboratory under DARPA/AFRL contract FA8750-10-C-0237
# ("CTSRD"), as part of the DARPA CRASH research programme.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR AND CONTRIBUTORS ``AS IS'' AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY
# OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF
# SUCH DAMAGE.
#
import inspect
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from .project import (_cached_get_homebrew_prefix, AutotoolsProject, BuildType, CheriConfig, CrossCompileTarget,
                      DefaultInstallDir, GitRepository, MakeCommandKind, SimpleProject)
from ..config.compilation_targets import CompilationTargets, NewlibBaremetalTargetInfo
from ..config.loader import ComputedDefaultValue, ConfigOptionBase


class BuildQEMUBase(AutotoolsProject):
    repository = GitRepository("https://github.com/qemu/qemu.git")
    native_install_dir = DefaultInstallDir.CHERI_SDK
    # QEMU will not work with BSD make, need GNU make
    make_kind = MakeCommandKind.GnuMake
    do_not_add_to_targets = True
    is_sdk_target = True
    skip_git_submodules = True  # we don't need these
    can_build_with_asan = True
    default_targets = "some-invalid-target"
    default_build_type = BuildType.RELEASE
    default_use_smbd = True
    lto_by_default = True

    @classmethod
    def is_toolchain_target(cls):
        return True

    @property
    def _build_type_basic_compiler_flags(self):
        if self.build_type.is_release:
            return ["-O3"]  # Build with -O3 instead of -O2, we want QEMU to be as fast as possible
        return super()._build_type_basic_compiler_flags

    @classmethod
    def setup_config_options(cls, **kwargs):
        super().setup_config_options(**kwargs)
        cls.use_smbd = cls.add_bool_option("use-smbd", show_help=False, default=cls.default_use_smbd,
                                           help="Don't require SMB support when building QEMU (warning: most --test "
                                                "targets will fail without smbd support)")

        cls.gui = cls.add_bool_option("gui", show_help=False, default=False,
                                      help="Build a the graphical UI bits for QEMU (SDL,VNC)")
        cls.build_profiler = cls.add_bool_option("build-profiler", show_help=False, default=False,
                                                 help="Enable QEMU internal profiling")
        cls.qemu_targets = cls.add_config_option("targets",
                                                 show_help=True, help="Build QEMU for the following targets",
                                                 default=cls.default_targets)
        cls.prefer_full_lto_over_thin_lto = cls.add_bool_option("full-lto", show_help=False, default=True,
                                                                help="Prefer full LTO over LLVM ThinLTO when using LTO")

    @classmethod
    def qemu_cheri_binary(cls, caller: SimpleProject):
        raise NotImplementedError()

    def __init__(self, config: CheriConfig):
        super().__init__(config)
        self.add_required_system_tool("glibtoolize" if self.target_info.is_macos() else "libtoolize",
                                      default="libtool")
        self.add_required_system_tool("autoreconf", default="autoconf")
        self.add_required_system_tool("aclocal", default="automake")

        self.add_required_pkg_config("pixman-1", homebrew="pixman", zypper="libpixman-1-0-devel", apt="libpixman-1-dev",
                                     freebsd="pixman")
        self.add_required_pkg_config("glib-2.0", homebrew="glib", zypper="glib2-devel", apt="libglib2.0-dev",
                                     freebsd="glib")
        # Tests require GNU sed
        self.add_required_system_tool("sed" if self.target_info.is_linux() else "gsed", homebrew="gnu-sed",
                                      freebsd="gsed")

        if self.build_type == BuildType.DEBUG:
            self.COMMON_FLAGS.append("-DCONFIG_DEBUG_TCG=1")

        # Disable some more unneeded things (we don't usually need the GUI frontends)
        if not self.gui:
            self.configure_args.extend(["--disable-sdl", "--disable-gtk", "--disable-opengl"])
            if self.target_info.is_macos():
                self.configure_args.append("--disable-cocoa")

        if self.build_profiler:
            self.configure_args.extend(["--enable-profiler"])

        # QEMU now builds with python3
        self.configure_args.append("--python=" + sys.executable)
        if self.build_type == BuildType.DEBUG:
            self.configure_args.extend(["--enable-debug", "--enable-debug-tcg"])
        else:
            # Try to optimize as much as possible:
            self.configure_args.extend(["--disable-stack-protector"])

        if self.use_asan:
            self.configure_args.append("--enable-sanitizers")
            # Ensure that tests crash on UBSan reports
            self.COMMON_FLAGS.append("-fno-sanitize-recover=all")
            if self.use_lto:
                self.info("Disabling LTO for ASAN instrumented builds")
            self.use_lto = False

        # Having symbol information is useful for debugging and profiling
        self.configure_args.append("--disable-strip")

        if not self.target_info.is_linux():
            self.configure_args.extend(["--disable-linux-aio", "--disable-kvm"])

        if self.config.verbose:
            self.make_args.set(V=1)

    def setup(self):
        super().setup()
        compiler = self.CC
        ccinfo = self.get_compiler_info(compiler)
        if ccinfo.compiler == "apple-clang" or (ccinfo.compiler == "clang" and ccinfo.version >= (4, 0, 0)):
            # Turn implicit function declaration into an error -Wimplicit-function-declaration
            self.CFLAGS.extend(["-Werror=implicit-function-declaration",
                                "-Werror=incompatible-pointer-types",
                                # Also make discarding const an error:
                                "-Werror=incompatible-pointer-types-discards-qualifiers",
                                # silence this warning that comes lots of times (it's fine on x86)
                                "-Wno-address-of-packed-member",
                                "-Wextra", "-Wno-sign-compare", "-Wno-unused-parameter",
                                "-Wno-missing-field-initializers"
                                ])
        # This would have cought some problems in the past
        self.common_warning_flags.append("-Werror=return-type")
        if self.use_smbd:
            smbd_path = "/usr/sbin/smbd"
            if self.target_info.is_freebsd():
                smbd_path = "/usr/local/sbin/smbd"
            elif self.target_info.is_macos():
                prefix = _cached_get_homebrew_prefix("samba", self.config)
                if prefix:
                    smbd_path = prefix / "sbin/samba-dot-org-smbd"
                else:
                    smbd_path = self.config.other_tools_dir / "sbin/smbd"
                self.info("Guessed samba path", smbd_path)

            # Prefer the self-compiled samba if available.
            if (self.config.other_tools_dir / "sbin/smbd").exists():
                smbd_path = self.config.other_tools_dir / "sbin/smbd"

            self.add_required_system_tool(smbd_path, cheribuild_target="samba", freebsd="samba48", apt="samba",
                                          homebrew="samba")

            self.configure_args.append("--smbd=" + str(smbd_path))
            if not Path(smbd_path).exists():
                if self.target_info.is_macos():
                    # QEMU user networking expects a smbd that accepts the same flags and config files as the samba.org
                    # sources but the macos /usr/sbin/smbd is incompatible with that:
                    self.warning("QEMU user-mode samba shares require the samba.org smbd. You will need to install it "
                                 "using homebrew (`brew install samba`) or build from source (`cheribuild.py samba`) "
                                 "since the /usr/sbin/smbd shipped by macOS is incompatible with QEMU")
                self.fatal("Could not find smbd -> QEMU SMB shares networking will not work",
                           fixit_hint="Either install samba using the system package manager or with cheribuild. "
                                      "If you really don't need QEMU host shares you can disable the samba dependency "
                                      "by setting --" + self.target + "/no-use-smbd")

        self.configure_args.extend([
            "--target-list=" + self.qemu_targets,
            "--enable-slirp=git",
            "--disable-linux-user",
            "--disable-xen",
            "--disable-docs",
            "--disable-rdma",
            # there are some -Wdeprected-declarations, etc. warnings with new libraries/compilers and it builds
            # with -Werror by default but we don't want the build to fail because of that -> add -Wno-error
            "--disable-werror",
            "--disable-pie",  # no need to build as PIE (this just slows down QEMU)
            "--extra-cflags=" + self.commandline_to_str(self.default_compiler_flags + self.CFLAGS),
            "--cxx=" + str(self.CXX),
            "--cc=" + str(self.CC),
            # Using /usr/bin/make on macOS breaks compilation DB creation with bear since SIP prevents it from
            # injecting shared libraries into any process that is installed as part of the system.
            "--make=" + self.make_args.command,
        ])
        if self.config.create_compilation_db:
            self.make_args.set(V=1)  # Otherwise bear can't parse the compiler output
        ldflags = self.default_ldflags + self.LDFLAGS
        if ldflags:
            self.configure_args.append("--extra-ldflags=" + self.commandline_to_str(ldflags))
        cxxflags = self.default_compiler_flags + self.CXXFLAGS
        if cxxflags:
            self.configure_args.append("--extra-cxxflags=" + self.commandline_to_str(cxxflags))

    def run_tests(self):
        self.run_make("check", cwd=self.build_dir)

    def update(self):
        # the build sometimes modifies the po/ subdirectory
        # reset that directory by checking out the HEAD revision there
        # this is better than git reset --hard as we don't lose any other changes
        if (self.source_dir / "po").is_dir() and not self.skip_update:
            self.run_cmd("git", "checkout", "HEAD", "po/", cwd=self.source_dir, print_verbose_only=True)
        if (self.source_dir / "pixman/pixman").exists():
            self.warning("QEMU might build the broken pixman submodule, run `git submodule deinit -f pixman` to clean")
        super().update()


class BuildSystemQEMU(BuildQEMUBase):
    do_not_add_to_targets = True

    def setup(self):
        super().setup()
        # Don't build BSD user mode targets as we only want system mode binaries.
        self.configure_args.append("--disable-bsd-user")


# noinspection PyAbstractClass
class BuildUpstreamQEMU(BuildSystemQEMU):
    repository = GitRepository("https://github.com/qemu/qemu.git")
    target = "upstream-qemu"
    _default_install_dir_fn = ComputedDefaultValue(
        function=lambda config, project: config.output_root / "upstream-qemu",
        as_string="$INSTALL_ROOT/upstream-qemu")
    default_targets = "mips64-softmmu," \
                      "riscv64-softmmu,riscv32-softmmu," \
                      "x86_64-softmmu,aarch64-softmmu"


class BuildQEMU(BuildSystemQEMU):
    target = "qemu"
    repository = GitRepository("https://github.com/CTSRD-CHERI/qemu.git", default_branch="qemu-cheri")
    default_targets = "mips64-softmmu,mips64cheri128-softmmu," \
                      "riscv64-softmmu,riscv64cheri-softmmu,riscv32-softmmu,riscv32cheri-softmmu," \
                      "x86_64-softmmu,aarch64-softmmu"

    @classmethod
    def setup_config_options(cls, **kwargs):
        super().setup_config_options()
        # Turn on unaligned loads/stores by default
        cls.unaligned = cls.add_bool_option("unaligned", show_help=False, help="Permit un-aligned loads/stores",
                                            default=False)
        cls.statistics = cls.add_bool_option("statistics", show_help=True,
                                             help="Collect statistics on out-of-bounds capability creation.")

    @classmethod
    def qemu_cheri_binary(cls, caller: SimpleProject, xtarget: CrossCompileTarget = None):
        if xtarget is None:
            xtarget = caller.get_crosscompile_target(caller.config)
        if xtarget.is_riscv(include_purecap=True):
            # Always use the CHERI qemu even for plain riscv:
            binary_name = "qemu-system-riscv64cheri"
        elif xtarget.is_mips(include_purecap=True):
            binary_name = "qemu-system-mips64cheri128"
        else:
            raise ValueError("Invalid xtarget" + str(xtarget))
        return caller.config.qemu_bindir / os.getenv("QEMU_CHERI_PATH", binary_name)

    @classmethod
    def get_firmware_dir(cls, caller: SimpleProject, cross_target: CrossCompileTarget = None):
        return cls.get_install_dir(caller, cross_target=cross_target) / "share/qemu"

    def __init__(self, config: CheriConfig):
        super().__init__(config)
        if self.unaligned:
            self.COMMON_FLAGS.append("-DCHERI_UNALIGNED")
        if self.statistics:
            self.COMMON_FLAGS.append("-DDO_CHERI_STATISTICS=1")

    def setup(self):
        # Build Morello by default if we are building a commit that has morello support merged.
        if "morello-softmmu" not in self.qemu_targets and (
                self.source_dir / "default-configs/targets/morello-softmmu.mak").exists():
            targets = inspect.getattr_static(self, "qemu_targets")
            assert isinstance(targets, ConfigOptionBase)
            if targets.is_default_value:
                self.qemu_targets += ",morello-softmmu"
        super().setup()
        if self.build_type == BuildType.DEBUG:
            self.COMMON_FLAGS.append("-DENABLE_CHERI_SANITIY_CHECKS=1")
        # the capstone disassembler doesn't support CHERI instructions:
        self.configure_args.append("--disable-capstone")
        # TODO: tests:
        # noinspection PyUnreachableCode
        if False:
            # Get all the required compilation flags for the TCG tests
            fake_project = SimpleNamespace()
            fake_project.config = self.config
            fake_project.needs_sysroot = False
            fake_project.warning = self.warning
            fake_project.target = "qemu-tcg-tests"
            # noinspection PyTypeChecker
            tgt_info_mips = NewlibBaremetalTargetInfo(CompilationTargets.BAREMETAL_NEWLIB_MIPS64, fake_project)
            # noinspection PyTypeChecker
            tgt_info_riscv64 = NewlibBaremetalTargetInfo(CompilationTargets.BAREMETAL_NEWLIB_RISCV64, fake_project)
            self.configure_args.extend([
                "--cross-cc-mips=" + str(tgt_info_mips.c_compiler),
                "--cross-cc-cflags-mips=" + self.commandline_to_str(
                    tgt_info_mips.get_essential_compiler_and_linker_flags()).replace("=", " "),
                "--cross-cc-riscv64=" + str(tgt_info_riscv64.c_compiler),
                "--cross-cc-cflags-riscv64=" + self.commandline_to_str(
                    tgt_info_riscv64.get_essential_compiler_and_linker_flags()).replace("=", " ")
            ])


class BuildBsdUserQEMU(BuildQEMUBase):
    repository = GitRepository("https://github.com/CTSRD-CHERI/qemu.git",
                               default_branch="qemu-cheri-bsd-user",
                               force_branch=True)
    native_install_dir = DefaultInstallDir.BSD_USER_SDK
    default_targets = "riscv64cheri-bsd-user"
    default_use_smbd = False
    target = "bsd-user-qemu"
    hide_options_from_help = True

    @classmethod
    def qemu_cheri_binary(cls, caller: SimpleProject, xtarget: CrossCompileTarget = None, absolute_path=True):
        if xtarget is None:
            xtarget = caller.get_crosscompile_target(caller.config)
        if xtarget.is_riscv(include_purecap=True):
            binary_name = "qemu-riscv64cheri"
        else:
            raise ValueError("Invalid xtarget" + str(xtarget))
        if absolute_path:
            return caller.config.bsd_user_qemu_bindir / os.getenv("QEMU_BSD_USER_PATH", binary_name)
        else:
            return binary_name

    def setup(self):
        super().setup()
        # Disable capstone disassembler unsupporting CHERI instructions.
        self.configure_args.append("--disable-capstone")
        # Disable RVFI-DDI unsupported in the user mode.
        self.configure_args.append("--disable-rvfi-dii")
        # Enable to build BSD user mode targets.
        self.configure_args.append("--enable-bsd-user")
        # Build a static binary that can be easily included in a guest jail.
        self.configure_args.append("--static")


class BuildMorelloQEMU(BuildSystemQEMU):
    repository = GitRepository("https://github.com/CTSRD-CHERI/qemu.git", default_branch="qemu-morello-merged",
                               force_branch=True,
                               old_urls=[
                                   b"https://github.com/LawrenceEsswood/qemu.git",
                                   # None of these were provided by cheribuild, but try and handle common
                                   # insteadOf/pushInsteadOf configs that will otherwise confuse cheribuild as they
                                   # affect the output of git remote.
                                   b"ssh://git@github.com/LawrenceEsswood/qemu.git",
                                   b"ssh://github.com/LawrenceEsswood/qemu.git",
                                   b"git@github.com:LawrenceEsswood/qemu.git"
                               ])
    native_install_dir = DefaultInstallDir.MORELLO_SDK
    default_targets = "aarch64-softmmu,morello-softmmu"
    target = "morello-qemu"
    hide_options_from_help = True

    @classmethod
    def qemu_cheri_binary(cls, caller: SimpleProject, xtarget: CrossCompileTarget = None):
        if xtarget is None:
            xtarget = caller.get_crosscompile_target(caller.config)
        if xtarget.is_aarch64(include_purecap=True):
            # Always use the Morello qemu even for plain AArch64:
            binary_name = "qemu-system-morello"
        else:
            raise ValueError("Invalid xtarget" + str(xtarget))
        return caller.config.morello_qemu_bindir / os.getenv("QEMU_MORELLO_PATH", binary_name)
