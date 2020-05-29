#
# Copyright (c) 2020 SRI International
# All rights reserved.
#
# This software was developed by SRI International and the University of
# Cambridge Computer Laboratory (Department of Computer Science and
# Technology) under DARPA contract HR0011-18-C-0016 ("ECATS"), as part of the
# DARPA SSITH research programme.
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
import os

from pathlib import Path
from .crosscompileproject import *
from ..disk_image import BuildCheriBSDDiskImage
from ..disk_image import _default_disk_image_name
from ..run_qemu import LaunchCheriBSD
from ...config.loader import ComputedDefaultValue
from ...utils import classproperty, fatalError
from ...mtree import MtreeFile
from .nginx import BuildFettNginx
from .openssh import BuildFettOpenSSH
from .sqlite import BuildFettSQLite


fett_supported_architectures = CompilationTargets.ALL_CHERIBSD_MIPS_AND_RISCV_TARGETS

class BuildFettConfig(CrossCompileProject):
    project_name = "fett-config"
    repository = GitRepository("git@github.com:CTSRD-CHERI/SSITH-FETT-Target.git",
                               default_branch="cheri")
    skipGitSubmodules = True
    supported_architectures = fett_supported_architectures

    dependencies = ["fett-nginx", "fett-openssh", "fett-sqlite"]

    native_install_dir = DefaultInstallDir.DO_NOT_INSTALL
    cross_install_dir = DefaultInstallDir.ROOTFS

    def __init__(self, config):
        super().__init__(config)
        self.mtree = MtreeFile()
        self.METALOG = self.destdir / "METALOG"

    def compile(self):
        print("Nothing to build for " + self.project_name)

    def install(self, **kwargs):
        if os.getenv("_TEST_SKIP_METALOG"):
             return
        if not os.path.exists(str(self.METALOG)):
             fatalError("METALOG " + str(self.METALOG) + "does not exist")
             return

        self.mtree.load(self.METALOG)
        src = self.sourceDir

        # nginx bits
        nginx_src = src / "build/webserver"
        nginx_prefix = BuildFettNginx.get_instance(self)._installPrefix.relative_to('/')
        self.mtree.add_file(nginx_src / "common/conf/nginx.conf",
                            nginx_prefix / "conf/nginx.conf")
        self.mtree.add_dir(nginx_prefix / "logs")
        # XXX: make private key dir 700?
        self.mtree.add_file(nginx_src / "common/keys/private-selfsigned.key",
                            nginx_prefix / "etc/ssl/private/private-selfsigned.key", mode="0600")
        self.mtree.add_file(nginx_src / "common/certs/selfsigned.crt",
                            nginx_prefix / "etc/ssl/certs/selfsigned.crt")
        self.mtree.add_file(src / "build/webserver/FreeBSD/rcfile",
                            "etc/rc.d/fett_nginx", mode="0555")
        self.mtree.add_dir(nginx_prefix / "post", uname="www", gname="www")
        html_files = [
          "index.html",
          "private/secret.html",
          "stanford.png",
          "static.html",
          "test.txt",
        ]
        for file in html_files:
            self.mtree.add_file(src / "build/webserver/common/html" / file,
                                nginx_prefix / "html" / file)

        # sshd bits
        ssh_prefix = BuildFettOpenSSH.get_instance(self)._installPrefix.relative_to('/')
        keyfiles = ["ssh_host_dsa_key", "ssh_host_ecdsa_key", "ssh_host_ed25519_key", "ssh_host_rsa_key"]
        for keyfile in keyfiles:
            self.mtree.add_file("/etc/ssh/" + keyfile, ssh_prefix / "etc/" / keyfile, symlink=True)
        self.mtree.add_file(src / "build/ssh/FreeBSD/fett_sshd",
                            "etc/rc.d/fett_sshd", mode="0555")

        # sqlite bits
        # XXX-TODO: install a smoketest?

        # voting app
        # ???

        self.mtree.write(self.METALOG)

class BuildFettDiskImage(BuildCheriBSDDiskImage):
    project_name = "disk-image-fett"
    dependencies = ["fett-config"]
    supported_architectures = fett_supported_architectures

    default_disk_image_path = ComputedDefaultValue(
        function=lambda conf, proj: _default_disk_image_name(conf, conf.outputRoot, proj, "fett-cheribsd-"),
        as_string="$OUTPUT_ROOT/fett-$arch_prefix-disk.img.")

    def __init__(self, config: CheriConfig):
        super().__init__(config)
        self.autoPrefixes.append("fett/")

class LaunchFett(LaunchCheriBSD):
    project_name = "run-fett"
    _source_class = BuildFettDiskImage
    supported_architectures = fett_supported_architectures
