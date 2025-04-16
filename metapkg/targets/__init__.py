from __future__ import annotations
from typing import TYPE_CHECKING

import platform

from .base import (
    Build,
    BuildRequest,
    ExprFlavor,
    InstallAspect,
    Target,
    Location,
    LinuxTarget,
    LinuxDistroTarget,
    EnsureDirAction,
    AddUserAction,
)
from .package import SystemPackage

from . import deb, rpm, linux, macos, generic, win  # noqa

if TYPE_CHECKING:
    from cleo.io.io import IO


__all__ = (
    "Build",
    "BuildRequest",
    "ExprFlavor",
    "EnsureDirAction",
    "AddUserAction",
    "InstallAspect",
    "Location",
    "Target",
    "LinuxTarget",
    "LinuxDistroTarget",
    "SystemPackage",
)


def detect_target(
    io: IO,
    portable: bool,
    libc: str | None = None,
    arch: str | None = None,
) -> Target:
    target: Target
    system = platform.system()
    if arch is None:
        arch = platform.machine().lower()

    if arch == "amd64":
        arch = "x86_64"
    elif arch == "arm64":
        arch = "aarch64"

    if system == "Linux":
        if libc is None:
            libc = "gnu"
        target = linux.get_specific_target(libc, arch, portable)

    elif system == "Darwin":
        v, _, _ = platform.mac_ver()
        version = tuple(int(p) for p in v.split("."))
        target = macos.get_specific_target(version, arch, portable)

    elif system == "Windows":
        v = platform.version()
        version = tuple(int(p) for p in v.split("."))
        target = win.get_specific_target(version, arch, portable)

    else:
        raise RuntimeError(f"System not supported: {system}")

    return target
