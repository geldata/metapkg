from __future__ import annotations
from typing import (
    TYPE_CHECKING,
)

import pathlib
import textwrap

from poetry.core.packages import dependency as poetry_dep
from poetry.core.constraints import version as poetry_version
from poetry.core.version import pep440 as poetry_pep440

from metapkg import targets
from metapkg import tools

from . import base
from . import sources as mpkg_sources

if TYPE_CHECKING:
    from cleo.io import io as cleo_io


class BundledRustPackage(base.BundledPackage):
    @classmethod
    def version_from_cargo(
        cls,
        source_dir: pathlib.Path,
    ) -> str:
        out = tools.cmd("cargo", "pkgid", cwd=source_dir).strip()
        _, _, version = out.rpartition("#")
        if ":" in version:
            _, _, version = version.rpartition(":")
        if "@" in version:
            _, _, version = version.rpartition("@")
        return version

    @classmethod
    def version_from_vcs_version(
        cls,
        io: cleo_io.IO,
        repo: tools.git.Git,
        vcs_version: str,
        is_release: bool,
    ) -> str:
        version = cls.version_from_cargo(repo.work_tree)

        if not is_release:
            commits = repo.run(
                "rev-list",
                "--count",
                vcs_version,
            )

            version = (
                poetry_version.Version.parse(version)
                .replace(
                    local=None,
                    pre=None,
                    dev=poetry_pep440.ReleaseTag("dev", int(commits)),
                )
                .to_string()
            )

        return version

    def get_build_script(self, build: targets.Build) -> str:
        return ""

    def get_prepare_script(self, build: targets.Build) -> str:
        script = super().get_prepare_script(build)
        sed = build.sh_get_command("sed")
        src = build.get_source_dir(self, relative_to="pkgbuild")
        semver = base.pep440_to_semver(self.version)
        script += textwrap.dedent(
            f"""\
            {sed} -i -e '/\\[package\\]/,/\\[.*\\]/{{
                    s/^version\\s*=.*/version = "{semver}"/;
                }}' \\
                "{src}/Cargo.toml"
            """
        )
        return script

    def get_build_install_script(self, build: targets.Build) -> str:
        script = super().get_build_install_script(build)
        cargo = build.sh_get_command("cargo")
        installdest = build.get_temp_dir(self, relative_to="pkgbuild")
        src = build.get_source_dir(self, relative_to="pkgbuild")
        bindir = build.get_bundle_install_path("systembin").relative_to("/")
        install_bindir = (
            build.get_build_install_dir(self, relative_to="pkgbuild") / bindir
        )
        env = build.sh_append_global_flags({})
        env["RUST_BACKTRACE"] = "1"
        env_str = build.sh_format_command("env", env, force_args_eq=True)
        script += textwrap.dedent(
            f"""\
            {env_str} \\
                {cargo} install --target {build.target.triple} \\
                    --verbose --verbose \\
                    --root "{installdest}" \\
                    --path "{src}" \\
                    --locked
            mkdir -p "{install_bindir}"
            cp -a "{installdest}/bin/"* "{install_bindir}/"
            """
        )
        return script

    def version_includes_revision(self) -> bool:
        return True


class BundledAdHocRustPackage(BundledRustPackage):
    @classmethod
    def resolve(
        cls,
        io: cleo_io.IO,
        *,
        name: base.NormalizedName | None = None,
        version: str | None = None,
        revision: str | None = None,
        is_release: bool = False,
        target: targets.Target,
        requires: list[poetry_dep.Dependency] | None = None,
    ) -> BundledAdHocRustPackage:
        sources = cls._get_sources(version)

        if not isinstance(sources[0], mpkg_sources.LocalSource):
            raise RuntimeError(
                "expected bundled Rust package source to be local"
            )
        source_dir = sources[0].url
        version = cls.version_from_cargo(pathlib.Path(source_dir))
        if not revision:
            revision = "1"

        ver = poetry_version.Version.parse(version)
        if isinstance(ver.local, tuple):
            local = ver.local
        elif ver.local is None:
            local = ()
        else:
            local = (ver.local,)
        ver = ver.replace(local=local + (f"r{revision}",))

        version, pretty_version = cls.format_version(ver)

        return cls(
            name=name,
            version=version,
            pretty_version=pretty_version,
            source_version=version,
            resolved_sources=sources,
            requires=requires,
        )
