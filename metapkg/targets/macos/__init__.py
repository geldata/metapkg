from __future__ import annotations

import pathlib
import re
import shlex
import shutil
import sys
import textwrap

from metapkg import packages as mpkg
from metapkg import tools
from metapkg.targets import base as targets
from metapkg.targets import generic
from metapkg.targets import package as tgt_pkg

from . import build as macbuild


class MacOSAddUserAction(targets.AddUserAction):
    def get_script(
        self,
        *,
        name: str,
        group: str | None = None,
        homedir: str | None = None,
        shell: bool = False,
        system: bool = False,
        description: str | None = None,
    ) -> str:
        if group:
            groupname = f"/Groups/{group}"
            groupadd_cmds = [
                self._get_dscl_cmd(groupname),
            ]
            if description:
                groupadd_cmds.append(
                    self._get_dscl_cmd(
                        groupname, key="RealName", value=description
                    )
                )

            last_gid = self._get_dscl_cmd(
                "/Groups", action="list", key="PrimaryGroupID"
            )
            last_gid += " | awk '{ print $2 }' | sort -n | tail -1"

            groupadd_cmds.append(
                self._get_dscl_cmd(
                    groupname,
                    key="Password",
                    value="*",
                )
            )

            groupadd_cmds.append(
                self._get_dscl_cmd(
                    groupname,
                    key="PrimaryGroupID",
                    value=f"!$(($({last_gid}) + 1))",
                )
            )

            group_exists = self._get_dscl_cmd(groupname, action="-read")

            group_script = textwrap.dedent(
                """\
                if ! {group_exists} >/dev/null 2>&1; then
                    {groupadd_cmds}
                fi
            """
            ).format(
                groupadd_cmds="\n    ".join(groupadd_cmds),
                group_exists=group_exists,
            )

        else:
            group_script = ""

        username = f"/Users/{name}"
        useradd_cmds = []

        useradd_cmds.append(self._get_dscl_cmd(username))

        if homedir:
            useradd_cmds.append(
                self._get_dscl_cmd(
                    username, key="NFSHomeDirectory", value=homedir
                )
            )
        if shell:
            useradd_cmds.append(
                self._get_dscl_cmd(
                    username, key="UserShell", value="/bin/bash"
                )
            )
        else:
            useradd_cmds.append(
                self._get_dscl_cmd(
                    username, key="UserShell", value="/sbin/nologin"
                )
            )
        if description:
            useradd_cmds.append(
                self._get_dscl_cmd(username, key="RealName", value=description)
            )

        useradd_cmds.append(
            self._get_dscl_cmd(username, key="Password", value="*")
        )

        useradd_cmds.append(
            self._get_dscl_cmd(username, key="IsHidden", value="1")
        )

        last_uid = self._get_dscl_cmd("/Users", action="list", key="UniqueID")
        last_uid += " | awk '{ print $2 }' | sort -n | tail -1"

        useradd_cmds.append(
            self._get_dscl_cmd(
                username,
                key="UniqueID",
                value=f"!$(($({last_uid}) + 1))",
            )
        )

        if system:
            primary_group = "daemon"
        elif group:
            primary_group = group

        if primary_group:
            get_group = self._get_dscl_cmd(
                f"/Groups/{primary_group}", action="-read"
            )

            get_group += "| awk '($1 == \"PrimaryGroupID:\") { print $2 }'"

            useradd_cmds.append(
                self._get_dscl_cmd(
                    username, key="PrimaryGroupID", value=f"!$({get_group})"
                )
            )

        if group and group != primary_group:
            assign_group_script = (
                f'dseditgroup -o edit -a "{name}" -t user "{group}"'
            )
        else:
            assign_group_script = ""

        user_exists = self._get_dscl_cmd(username, action="-read")

        return textwrap.dedent(
            """\
            {create_group_script}
            if ! {user_exists} >/dev/null 2>&1; then
                {useradd_cmd}
            fi
            {assign_group_script}
        """
        ).format(
            create_group_script=group_script,
            useradd_cmd="\n    ".join(useradd_cmds),
            user_exists=user_exists,
            assign_group_script=assign_group_script,
        )

    def _get_dscl_cmd(
        self,
        name: str,
        *,
        action: str = "create",
        key: str | None = None,
        value: str | None = None,
        indent: int = 0,
    ) -> str:
        args: dict[str, str | None] = {
            ".": None,
            action: None,
            name: None,
        }

        if key is not None:
            args[key] = value

        return self._build.sh_format_command(
            "dscl", args, extra_indent=indent, linebreaks=False
        )


class MacOSRepository(generic.GenericOSRepository):
    def list_provided_packages(self) -> frozenset[str]:
        # A list of packages assumed to be present on the system.
        pkgs = super().list_provided_packages()
        return pkgs | frozenset(
            (
                "libffi",
                "libffi-dev",
                "uuid",
                "uuid-dev",
                "zlib",
                "zlib-dev",
            )
        )


class LibFFISystemPackage(tgt_pkg.StandardSystemPackage):
    def get_shlibs(self, build: targets.Build) -> list[str]:
        return ["ffi"]


class UuidSystemPackage(tgt_pkg.StandardSystemPackage):
    def get_shlibs(self, build: targets.Build) -> list[str]:
        # uuid is part of libc on MacOS
        return []


class ZlibSystemPackage(tgt_pkg.StandardSystemPackage):
    def get_shlibs(self, build: targets.Build) -> list[str]:
        return ["z"]


_frameworks_base = "/System/Library/Frameworks"
_frameworks = [
    "Accelerate",
    "CoreFoundation",
    "CoreServices",
    "IOKit",
    "Security",
    "SystemConfiguration",
    "vecLib",
]

_lib_base = "/usr/lib"
_libs = [
    r"libSystem(\.B)?",
    r"libobjc(\.A)?",
    r"libc\+\+\.1",
    r"libffi",
    r"libiconv\.2",
    r"libresolv\.9",
    r"libz\.1",
]

_sys_shlibs = [rf"{_lib_base}/{lib}\.dylib" for lib in _libs] + [
    rf"{_frameworks_base}/{fw}\.framework/Versions/A/{fw}"
    for fw in _frameworks
]

_sys_shlibs_re = re.compile(
    "|".join(f"({lib})" for lib in _sys_shlibs),
    re.A,
)


class MacOSTarget(generic.GenericTarget):
    def __init__(self, arch: str) -> None:
        super().__init__(arch, libc="libSystem")

    @property
    def name(self) -> str:
        return f"Generic macOS"

    @property
    def triple(self) -> str:
        return f"{self.machine_architecture}-apple-darwin"

    @property
    def machine_architecture_alias(self) -> str:
        arch = self.machine_architecture
        if arch == "aarch64":
            # Apple calls aarch64 arm64 in their toolchain options
            arch = "arm64"
        return arch

    @property
    def min_supported_version(self) -> str:
        if self.machine_architecture_alias == "arm64":
            return "11.0"
        else:
            return "10.12"

    def get_package_repository(self) -> MacOSRepository:
        repo = MacOSRepository("macos")
        repo.register_package_impl("libffi", LibFFISystemPackage)
        repo.register_package_impl("uuid", UuidSystemPackage)
        repo.register_package_impl("zlib", ZlibSystemPackage)
        return repo

    def _get_necessary_host_tools(self) -> list[str]:
        return [
            "bash",
            "make",
            "gnu-sed",
            "gnu-tar",
            "cmake",
            "ninja",
            "meson",
        ]

    def prepare(self) -> None:
        if not shutil.which("brew"):
            print(
                "no Homebrew detected on system, skipping "
                "auto-installation of build tools",
                file=sys.stderr,
            )
            return

        tools.cmd("brew", "update")
        brew_env = "env HOMEBREW_NO_AUTO_UPDATE=1"
        brew_inst = (
            f'if brew ls --versions "$1"; '
            f'then {brew_env} brew upgrade "$1" || true; '
            f'else {brew_env} brew install "$1" || true; fi'
        )
        for tool in self._get_necessary_host_tools():
            tools.cmd("/bin/sh", "-c", brew_inst, "--", tool)

    def is_binary_code_file(
        self, build: targets.Build, path: pathlib.Path
    ) -> bool:
        with open(path, "rb") as f:
            signature = f.read(4)
        # Mach-O binaries
        return signature in {
            b"\xfe\xed\xfa\xce",
            b"\xfe\xed\xfa\xcf",
            b"\xce\xfa\xed\xfe",
            b"\xcf\xfa\xed\xfe",
        }

    def is_dynamically_linked(
        self, build: targets.Build, path: pathlib.Path
    ) -> bool:
        # macOS binaries are always dynamically linked
        return True

    def is_allowed_system_shlib(
        self, build: targets.Build, shlib: pathlib.Path
    ) -> bool:
        return bool(_sys_shlibs_re.fullmatch(str(shlib)))

    def get_shlib_refs(
        self,
        build: targets.Build,
        image_root: pathlib.Path,
        install_path: pathlib.Path,
        *,
        resolve: bool = True,
    ) -> tuple[set[pathlib.Path], set[pathlib.Path]]:
        shlibs = set()
        rpaths = set()
        output = tools.cmd("otool", "-l", image_root / install_path)
        section_re = re.compile(r"^Section$", re.I)
        load_cmd_re = re.compile(r"^Load command (\d+)\s*$", re.I)
        lc_load_dylib_cmd_re = re.compile(r"^\s*cmd\s+LC_LOAD_DYLIB\s*$")
        lc_load_dylib_name_re = re.compile(r"^\s*name\s+([^(]+).*$")
        lc_rpath_cmd_re = re.compile(r"^\s*cmd\s+LC_RPATH\s*$")
        lc_rpath_path_re = re.compile(r"^\s*path\s+([^(]+).*$")

        state = "skip"
        for line in output.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            if state == "skip":
                if load_cmd_re.match(line):
                    state = "load_cmd"
            elif state == "load_cmd":
                if lc_load_dylib_cmd_re.match(line):
                    state = "lc_load_dylib"
                elif lc_rpath_cmd_re.match(line):
                    state = "lc_rpath"
                elif section_re.match(line):
                    state = "skip"
            elif state == "lc_load_dylib":
                if m := lc_load_dylib_name_re.match(line):
                    dylib = pathlib.Path(m.group(1).strip())
                    if dylib.parts[0] == "@rpath" and resolve:
                        dylib = pathlib.Path(*dylib.parts[1:])
                    shlibs.add(dylib)
                    state = "skip"
                elif section_re.match(line):
                    state = "skip"
                elif load_cmd_re.match(line):
                    state = "load_cmd"
            elif state == "lc_rpath":
                if m := lc_rpath_path_re.match(line):
                    entry = m.group(1).strip()
                    if entry.startswith("@loader_path") and resolve:
                        relpath = entry[len("@loader_path/") :]
                        rpath = (
                            pathlib.Path("/") / install_path.parent / relpath
                        )
                    else:
                        rpath = pathlib.Path(entry)
                    rpaths.add(rpath)
                    state = "skip"
                elif section_re.match(line):
                    state = "skip"
                elif load_cmd_re.match(line):
                    state = "load_cmd"

        return shlibs, rpaths

    def get_builder(self) -> type[macbuild.MacOSBuild]:
        raise NotImplementedError

    def get_su_script(
        self, build: targets.Build, script: str, user: str
    ) -> str:
        return f"su '{user}' -c {shlex.quote(script)}\n"

    def get_action(
        self, name: str, build: targets.Build
    ) -> targets.TargetAction:
        if name == "adduser":
            return MacOSAddUserAction(build)
        else:
            return super().get_action(name, build)

    def sh_get_package_ld_env(
        self, build: targets.Build, package: mpkg.BasePackage, wd: str
    ) -> dict[str, str]:
        pkg_install_root = build.get_build_install_dir(
            package, relative_to="pkgbuild"
        )
        pkg_lib_path = pkg_install_root / build.get_install_path(
            package, "lib"
        ).relative_to("/")

        return {
            "DYLD_LIBRARY_PATH": f"{wd}/{shlex.quote(str(pkg_lib_path))}",
        }

    def get_ld_env_keys(self, build: targets.Build) -> list[str]:
        return ["DYLD_LIBRARY_PATH"]

    def get_shlib_filename(self, shlib: str) -> str:
        return f"lib{shlib}.dylib"

    def get_shlib_filename_patterns(self, shlib: str) -> list[str]:
        return [
            f"lib{shlib}.dylib",
            f"lib{shlib}.*.dylib",
            f"lib{shlib}.so",
            f"lib{shlib}.so.*",
        ]

    def get_shlib_path_link_time_ldflags(
        self, build: targets.Build, path: str
    ) -> list[str]:
        return [f"-L{path}"]

    def get_shlib_path_run_time_ldflags(
        self, build: targets.Build, path: str
    ) -> list[str]:
        return [f"-Wl,-rpath,{path}"]

    def get_shlib_relpath_run_time_ldflags(
        self, build: targets.Build, path: str = ""
    ) -> list[str]:
        return []

    def get_global_env(self, build: targets.Build) -> dict[str, str]:
        return super().get_global_env(build) | {
            "MACOSX_DEPLOYMENT_TARGET": self.min_supported_version,
        }

    def get_global_cflags(self, build: targets.Build) -> list[str]:
        flags = super().get_global_cflags(build)
        return flags + [
            "-g",
            "-O2",
            "-D_FORTIFY_SOURCE=2",
            "-fstack-protector-strong",
            "-Wdate-time",
            "-Wformat",
            "-Werror=format-security",
            f"-mmacosx-version-min={self.min_supported_version}",
            "-arch",
            self.machine_architecture_alias,
        ]

    def get_global_ldflags(self, build: targets.Build) -> list[str]:
        flags = super().get_global_ldflags(build)
        return flags + [
            f"-mmacosx-version-min={self.min_supported_version}",
            "-arch",
            self.machine_architecture_alias,
        ]


class MacOSNativePackageTarget(MacOSTarget):
    def __init__(self, version: tuple[int, ...], arch: str) -> None:
        super().__init__(arch)
        self.version = version

    @property
    def name(self) -> str:
        return f'macOS {".".join(str(v) for v in self.version)}'

    @property
    def ident(self) -> str:
        return f"macospkg"

    def get_package_system_ident(
        self,
        build: targets.Build,
        package: mpkg.BundledPackage,
        include_slot: bool = False,
    ) -> str:
        if include_slot:
            return f"{package.identifier}{package.slot_suffix}"
        else:
            return package.identifier

    def get_install_path(
        self,
        build: targets.Build,
        root: pathlib.Path,
        root_subdir: pathlib.Path,
        prefix: pathlib.Path,
        aspect: targets.InstallAspect,
    ) -> pathlib.Path:
        if aspect == "localstate":
            return pathlib.Path("/") / "var"
        elif aspect == "userconf":
            return pathlib.Path("$HOME") / "Library" / "Application Support"
        elif aspect == "runstate":
            return pathlib.Path("/") / "var" / "run"
        elif aspect == "systembin":
            return root.parent.parent / "bin"
        elif aspect == "data":
            return root / "share" / root_subdir
        elif aspect == "doc":
            return root / "share" / "doc" / root_subdir
        elif aspect == "info":
            return root / "share" / "info"
        elif aspect == "man":
            return root / "share" / "man"
        else:
            return super().get_install_path(
                build, root, root_subdir, prefix, aspect
            )

    def get_framework_root(self, build: targets.Build) -> pathlib.Path:
        rpkg = build.root_package
        return pathlib.Path(f"/Library/Frameworks/{rpkg.title}.framework")

    def get_bundle_install_root(self, build: targets.Build) -> pathlib.Path:
        rpkg = build.root_package
        return self.get_framework_root(build) / "Versions" / rpkg.slot

    def get_bundle_install_subdir(self, build: targets.Build) -> pathlib.Path:
        root_subdir = build.root_package.get_root_install_subdir(build)
        return pathlib.Path("lib") / root_subdir

    def get_resource_path(
        self, build: targets.Build, resource: str
    ) -> pathlib.Path | None:
        if resource == "system-daemons":
            return pathlib.Path("/Library/LaunchDaemons/")
        else:
            return super().get_resource_path(build, resource)

    def service_scripts_for_package(
        self, build: targets.Build, package: mpkg.BundledPackage
    ) -> dict[pathlib.Path, str]:
        units = package.read_support_files(build, "*.plist.in")
        launchd_path = self.get_resource_path(build, "system-daemons")
        assert launchd_path is not None
        return {launchd_path / name: data for name, data in units.items()}

    def get_capabilities(self) -> list[str]:
        capabilities = super().get_capabilities()
        return capabilities + ["launchd"]

    def sh_get_package_ld_env(
        self, build: targets.Build, package: mpkg.BasePackage, wd: str
    ) -> dict[str, str]:
        env = super().sh_get_package_ld_env(build, package, wd)
        pkg_install_root = build.get_build_install_dir(
            package, relative_to="pkgbuild"
        )
        fw_root = self.get_framework_root(build).parent
        pkg_fw_root = pkg_install_root / fw_root.relative_to("/")
        env["DYLD_FRAMEWORK_PATH"] = f"{wd}/{shlex.quote(str(pkg_fw_root))}"
        return env

    def get_ld_env_keys(self, build: targets.Build) -> list[str]:
        return super().get_ld_env_keys(build) + ["DYLD_FRAMEWORK_PATH"]

    def get_builder(self) -> type[macbuild.NativePackageBuild]:
        return macbuild.NativePackageBuild


class ModernMacOSNativePackageTarget(MacOSNativePackageTarget):
    pass


class MacOSPortableTarget(MacOSTarget):
    def is_portable(self) -> bool:
        return True

    @property
    def ident(self) -> str:
        return f"macosportable"

    def get_global_ldflags(self, build: targets.Build) -> list[str]:
        flags = super().get_global_ldflags(build)
        # -headerpad_max_install_names is needed because we want
        # to be able to manipulate rpath with install_name_tool.
        return flags + [
            "-Wl,-headerpad_max_install_names",
        ]

    def get_builder(self) -> type[macbuild.GenericMacOSBuild]:
        return macbuild.GenericMacOSBuild

    def _get_necessary_host_tools(self) -> list[str]:
        return super()._get_necessary_host_tools() + ["zstd"]


def get_specific_target(
    version: tuple[int, ...], arch: str, portable: bool
) -> MacOSTarget:
    if version >= (10, 10):
        if portable:
            return MacOSPortableTarget(arch)
        else:
            return ModernMacOSNativePackageTarget(version, arch)
    else:
        raise NotImplementedError(
            f'macOS version {".".join(str(v) for v in version)}'
            " is not supported"
        )
