from __future__ import annotations
from typing import (
    TYPE_CHECKING,
    Any,
)

import functools
import pathlib
import re
import subprocess
import textwrap

from poetry.core.packages import dependency as poetry_dep
from poetry.core.packages import package as poetry_pkg
from poetry.repositories import repository as poetry_repo

from metapkg import tools
from metapkg import packages as mpkg
from metapkg.targets import base as targets
from metapkg.targets.package import SystemPackage

from . import build as debuild

if TYPE_CHECKING:
    from distro import distro


PACKAGE_MAP = {
    "icu": "libicu??",
    "icu-dev": "libicu-dev",
    "zlib": "zlib1g",
    "zlib-dev": "zlib1g-dev",
    "libxslt-dev": "libxslt1-dev",
    "pam": "libpam0g",
    "pam-dev": "libpam0g-dev",
    "python": "python3",
    "uuid": "libuuid1",
    "uuid-dev": "uuid-dev",
    "systemd-dev": "libsystemd-dev",
    "ncurses": "ncurses-bin",
    "libffi-dev": "libffi-dev",
    "openssl-dev": "libssl-dev",
    "libexpat": "libexpat?",
    "libexpat-dev": "libexpat?-dev",
    "libgeos": "libgeos-c1v?",
    "libgeotiff": "libgeotiff?",
    "libjson-c": "libjson-c?",
    "libsqlite3": "libsqlite3-?",
    "libtiff": "libtiff?",
    "libprotobuf-c": "libprotobuf-c?",
    "libgdal": "libgdal??",
    "libproj": "libproj??",
    "protoc-c": "protobuf-c-compiler",
}


GROUP_MAP = {
    "Applications/Databases": "database",
}


_version_trans = str.maketrans({"+": ".", "-": ".", "~": "."})


def _debian_version_to_pep440(debver: str) -> str:
    m = re.match(
        r"""
        ^(?:(?P<epoch>\d+):)?(?P<upstream>[^-]+)(?:-(?P<debian>.*))?$
    """,
        debver,
        re.X,
    )

    if not m:
        raise ValueError(f"unexpected debian package version: {debver}")

    epoch = m.group("epoch")
    version = ""
    if epoch:
        version += f"{epoch}!"

    upstream_ver = m.group("upstream")
    is_extra = False

    for i, part in enumerate(upstream_ver.split(".")):
        if is_extra:
            version += "."
            version += part.translate(_version_trans)
        else:
            part_m = re.match(r"^([0-9]*)([A-Za-z]*)(.*)$", part)
            if part_m:
                if part_m.group(1):
                    if i > 0:
                        version += "."
                    version += part_m.group(1)

                alnum = part_m.group(2)
                if alnum:
                    # special handling for OpenSSL-like versions, e.g 1.1.1f
                    for char in alnum:
                        version += f".{ord(char)}"

                rest = part_m.group(3)
                if rest:
                    if rest[0] in "+-~":
                        rest = rest[1:]
                    version += f"+{rest.translate(_version_trans)}"
                    is_extra = True
            else:
                raise ValueError(
                    f"unexpected upstream version format: {upstream_ver}"
                )

    debian_part = m.group("debian")
    if debian_part:
        if not is_extra:
            version += "+"
        else:
            version += "."
        version += debian_part.translate(_version_trans)

    return version


class DebRepository(poetry_repo.Repository):
    def __init__(
        self,
        name: str = "deb",
        packages: list[poetry_pkg.Package] | None = None,
    ) -> None:
        super().__init__(name, packages)
        self._parsed: set[str] = set()

    def find_packages(
        self,
        dependency: poetry_dep.Dependency,
    ) -> list[poetry_pkg.Package]:
        if dependency.name not in self._parsed:
            packages = self.apt_get_packages(dependency.name)
            for package in packages:
                self.add_package(package)
            self._parsed.add(dependency.name)

        return super().find_packages(dependency)

    def apt_get_packages(self, name: str) -> tuple[poetry_pkg.Package, ...]:
        system_name = PACKAGE_MAP.get(name, name)

        try:
            output = tools.cmd(
                "apt-cache", "policy", system_name, errors_are_fatal=False
            )
        except subprocess.CalledProcessError:
            return ()
        else:
            policy = self._parse_apt_policy_output(output.strip())
            if not policy:
                return ()
            else:
                packages = []
                for pkgmeta in policy:
                    for version in pkgmeta["versions"]:
                        norm_version = _debian_version_to_pep440(version)
                        pkg = SystemPackage(
                            name,
                            norm_version,
                            pretty_version=version,
                            system_name=pkgmeta["name"],
                        )
                        packages.append(pkg)

                return tuple(packages)

    def _parse_apt_policy_output(self, output: str) -> list[dict[str, Any]]:
        if not output:
            return []

        metas = []

        lines = output.split("\n")

        while lines:
            meta: dict[str, Any] = {}
            seen_name = False

            for no, line in enumerate(lines):
                line = line.strip()
                if not line:
                    continue

                if not seen_name:
                    if not line.endswith(":"):
                        raise RuntimeError(
                            f"cannot parse apt-cache policy output:\n{output}"
                        )
                    meta["name"] = line[:-1]
                    seen_name = True
                    continue

                name, _, value = line.partition(":")
                value = value.strip()
                if value:
                    meta[name.lower()] = value
                elif name.lower() == "version table":
                    break

            if not seen_name:
                break

            lines = lines[no + 1 :]

            versions = []
            last_indent = -1
            vno = 0

            for vno, line in enumerate(lines):
                m = re.match(r"^((?:\s|\*)*)(.*)$", line)
                if m is not None:
                    indent = len(m.group(1))
                    content = m.group(2)

                    if indent == 0:
                        break

                    if last_indent == -1 or indent < last_indent:
                        version = content.split(" ")[0]
                        versions.append(version)

                    last_indent = indent
                else:
                    raise RuntimeError(
                        f"cannot parse apt-cache policy output:\n{output}"
                    )
            else:
                vno += 1

            meta["versions"] = versions
            lines = lines[vno:]
            metas.append(meta)

        return metas


class BaseDebTarget(targets.FHSTarget, targets.LinuxDistroTarget):
    def __init__(
        self, distro_info: distro.InfoDict, arch: str, libc: str
    ) -> None:
        targets.FHSTarget.__init__(self, arch, libc=libc)
        targets.LinuxDistroTarget.__init__(
            self, distro_info=distro_info, arch=arch, libc=libc
        )

    def prepare(self) -> None:
        tools.cmd("apt-get", "update")

    def get_package_repository(self) -> poetry_repo.Repository:
        return DebRepository()

    def get_package_group(self, pkg: mpkg.BundledPackage) -> str:
        return GROUP_MAP.get(pkg.group, pkg.group)

    @functools.cache
    def get_arch_libdir(self) -> pathlib.Path:
        arch = tools.cmd("dpkg-architecture", "-qDEB_HOST_MULTIARCH").strip()
        return pathlib.Path("/usr/lib") / arch

    def get_builder(self) -> type[debuild.Build]:
        return debuild.Build

    def get_capabilities(self) -> list[str]:
        capabilities = super().get_capabilities()
        return capabilities + ["systemd", "tzdata"]

    def get_resource_path(
        self, build: targets.Build, resource: str
    ) -> pathlib.Path | None:
        if resource == "systemd-units":
            return pathlib.Path("/lib/systemd/system")
        else:
            return super().get_resource_path(build, resource)

    def get_global_make_vars(
        self,
        build: targets.Build,
        flavor: targets.ExprFlavor,
    ) -> dict[str, str]:
        opts = (
            "$(DEB_BUILD_MAINT_OPTIONS)"
            if flavor == "make"
            else "${DEB_BUILD_MAINT_OPTIONS}"
        )
        dpkg_buildflags = f"DEB_BUILD_MAINT_OPTIONS={opts} dpkg-buildflags"
        return super().get_global_make_vars(build, flavor) | {
            "DH_VERBOSE": "1",
            "dpkg_buildflags": dpkg_buildflags,
        }


class ModernDebianTarget(BaseDebTarget):
    def get_global_make_vars(
        self,
        build: targets.Build,
        flavor: targets.ExprFlavor,
    ) -> dict[str, str]:
        return {
            "DEB_BUILD_MAINT_OPTIONS": "hardening=+all"
        } | super().get_global_make_vars(build, flavor)


class DebianStretchOrNewerTarget(ModernDebianTarget):
    pass


class UbuntuXenialOrNewerTarget(BaseDebTarget):
    pass


class UbuntuBionicOrNewerTarget(ModernDebianTarget):
    def __init__(
        self, distro_info: distro.InfoDict, arch: str, libc: str
    ) -> None:
        super().__init__(distro_info, arch, libc)
        if " " in self.distro["codename"]:
            # distro described in full, e,g, "Bionic Beaver",
            # normalize that to a single lowercase word as
            # per debian convention
            c = self.distro["codename"].split(" ")[0].lower()
            self.distro["codename"] = c


def get_specific_target(
    distro_info: distro.InfoDict, arch: str, libc: str
) -> BaseDebTarget:
    if distro_info["id"] == "debian":
        ver = int(distro_info["version_parts"]["major"])
        if ver >= 9:
            return DebianStretchOrNewerTarget(distro_info, arch, libc)
        else:
            raise NotImplementedError(
                f'{distro_info["id"]} {distro_info["codename"]} '
                f"is not supported"
            )

    elif distro_info["id"] == "ubuntu":
        major = int(distro_info["version_parts"]["major"])
        minor = int(distro_info["version_parts"]["minor"])

        if (major, minor) >= (18, 4):
            return UbuntuBionicOrNewerTarget(distro_info, arch, libc)
        elif (major, minor) >= (16, 4):
            return UbuntuXenialOrNewerTarget(distro_info, arch, libc)
        else:
            raise NotImplementedError(
                f'{distro_info["id"]} {distro_info["codename"]} '
                f"is not supported"
            )

    else:
        raise NotImplementedError(f'{distro_info["id"]} is not supported')
