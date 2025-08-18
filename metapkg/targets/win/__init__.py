from __future__ import annotations

import pathlib

from metapkg import packages as mpkg
from metapkg import targets
from metapkg.targets import generic

from . import build as winbuild


class WindowsTarget(generic.GenericTarget):
    def __init__(self, version: tuple[int, ...], arch: str) -> None:
        super().__init__(arch, libc="vcrt")
        self.version = version

    @property
    def name(self) -> str:
        return f'Windows {".".join(str(v) for v in self.version)}'

    @property
    def triple(self) -> str:
        return f"{self.arch}-pc-windows-msvc"

    @property
    def executable_mime_type(self) -> str:
        return "application/vnd.microsoft.portable-executable"

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

    def get_exe_suffix(self) -> str:
        return ".exe"

    def is_binary_code_file(
        self, build: targets.Build, path: pathlib.Path
    ) -> bool:
        return path.suffix in {".exe", ".dll"}

    def is_dynamically_linked(
        self, build: targets.Build, path: pathlib.Path
    ) -> bool:
        # Windows binaries are always dynamically linked
        return True

    def get_shlib_refs(
        self,
        build: targets.Build,
        image_root: pathlib.Path,
        install_path: pathlib.Path,
        *,
        resolve: bool = True,
    ) -> tuple[set[pathlib.Path], set[pathlib.Path]]:
        # YOLO for now.
        return (set(), set())


class ModernWindowsTarget(WindowsTarget):
    pass


class ModernWindowsPortableTarget(ModernWindowsTarget):
    @property
    def ident(self) -> str:
        return f"windowsportable"

    def is_portable(self) -> bool:
        return True

    def get_builder(self) -> type[winbuild.Build]:
        return winbuild.Build


def get_specific_target(
    version: tuple[int, ...], arch: str, portable: bool
) -> WindowsTarget:
    if version >= (10, 0):
        return ModernWindowsPortableTarget(version, arch)
    else:
        raise NotImplementedError(
            f'Windows version {".".join(str(v) for v in version)}'
            " is not supported"
        )
