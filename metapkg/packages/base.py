from __future__ import annotations
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Iterable,
    Literal,
    Mapping,
    Sequence,
    Type,
    TypeVar,
    Union,
    cast,
    overload,
)

import collections
import copy
import dataclasses
import enum
import functools
import glob
import hashlib
import inspect
import os
import pathlib
import platform
import pprint
import re
import shlex
import sys
import textwrap

import packaging.utils

from poetry.core.packages import dependency as poetry_dep
from poetry.core.packages import dependency_group as poetry_depgroup
from poetry.core.packages import package as poetry_pkg
from poetry.core.version import pep440 as poetry_pep440
from poetry.core.constraints import version as poetry_version
from poetry.core.version.pep440 import segments as poetry_pep440_segments
from poetry.core.spdx import helpers as poetry_spdx_helpers
from poetry.repositories import exceptions as poetry_repo_exc

from metapkg import tools
from . import repository
from . import sources as af_sources

if TYPE_CHECKING:
    from typing_extensions import TypeAlias
    from cleo.io import io as cleo_io
    from metapkg import targets
    from poetry.repositories import repository as poetry_repo


get_build_requirements = repository.get_build_requirements
set_build_requirements = repository.set_build_requirements
canonicalize_name = packaging.utils.canonicalize_name
NormalizedName = packaging.utils.NormalizedName
all_requires_include_build_reqs: bool = False


Args: TypeAlias = dict[str, Union[str, pathlib.Path, None]]


class AliasPackage(poetry_pkg.Package):
    def __repr__(self) -> str:
        return "<AliasPackage {}>".format(self.unique_name)


class PackageFileLayout(enum.IntEnum):
    REGULAR = enum.auto()
    FLAT = enum.auto()
    SINGLE_BINARY = enum.auto()


@dataclasses.dataclass
class MetaPackage:
    base_name: str
    """Package name not including the slot"""

    name: str
    """Package name with slot (if any)"""

    description: str
    dependencies: dict[str, str]


class PackageWithPrettyVersion(poetry_pkg.Package):
    def __init__(
        self,
        name: str,
        version: str | poetry_version.Version,
        source_type: str | None = None,
        source_url: str | None = None,
        source_reference: str | None = None,
        source_resolved_reference: str | None = None,
        source_subdirectory: str | None = None,
        features: Iterable[str] | None = None,
        develop: bool = False,
        yanked: str | bool = False,
        *,
        pretty_version: str | None = None,
    ) -> None:
        super().__init__(
            name=name,
            version=version,
            source_type=source_type,
            source_url=source_url,
            source_reference=source_reference,
            source_resolved_reference=source_resolved_reference,
            source_subdirectory=source_subdirectory,
            features=features,
            develop=develop,
            yanked=yanked,
        )

        if pretty_version:
            self._pretty_version = pretty_version
        else:
            if isinstance(version, poetry_version.Version):
                self._pretty_version = version.text
            else:
                self._pretty_version = version

    @property
    def pretty_version(self) -> str:
        return self._pretty_version


class BasePackage(PackageWithPrettyVersion):
    @property
    def slot_suffix(self) -> str:
        return ""

    def get_dep_pkg_name(self) -> str:
        """Name used by pkg-config or CMake to refer to this package."""
        return str(self.name).upper()

    def get_dep_pkg_config_script(self) -> str | None:
        return None

    @property
    def provides_pkg_config(self) -> bool:
        return False

    @property
    def provides_shlibs(self) -> bool:
        return False

    @property
    def provides_c_headers(self) -> bool:
        return False

    @property
    def provides_build_tools(self) -> bool:
        return False

    def get_pkg_config_meta(self) -> PkgConfigMeta:
        return PkgConfigMeta(
            pkg_name=self.get_dep_pkg_name(),
            pkg_config_script=self.get_dep_pkg_config_script(),
            provides_pkg_config=self.provides_pkg_config,
            provides_shlibs=self.provides_shlibs,
            provides_c_headers=self.provides_c_headers,
            provides_build_tools=self.provides_build_tools,
        )

    def get_sources(self) -> list[af_sources.BaseSource]:
        raise NotImplementedError

    def get_requirements(self) -> list[poetry_dep.Dependency]:
        return []

    def get_build_requirements(self) -> list[poetry_dep.Dependency]:
        return []

    def get_license_files_patterns(self) -> list[str]:
        return [
            "LICENSE*",
            "LICENCE*",
            "COPYING",
            "NOTICE",
            "COPYRIGHT",
        ]

    def get_prepare_script(self, build: targets.Build) -> str:
        return ""

    def get_configure_script(self, build: targets.Build) -> str:
        return ""

    def get_build_script(self, build: targets.Build) -> str:
        raise NotImplementedError(f"{self}.build()")

    def get_build_env(self, build: targets.Build, wd: str) -> Args:
        all_build_deps = build.get_build_reqs(self, recursive=True)
        return build.get_ld_env(all_build_deps, wd=wd)

    def get_build_install_script(self, build: targets.Build) -> str:
        script = ""

        licenses = self.get_license_files_patterns()
        if licenses:
            sdir = build.get_source_dir(self, relative_to="pkgbuild")
            legaldir = build.get_install_path(self, "legal").relative_to("/")
            lic_dest = (
                build.get_build_install_dir(self, relative_to="pkgbuild")
                / legaldir
            )
            prefix = str(lic_dest / self.name)
            licenses_pattern = "{" + ",".join(licenses) + "}"
            script += textwrap.dedent(
                f"""\
                mkdir -p "{lic_dest}"
                for _lic_src in "{sdir}"/{licenses_pattern}; do
                    if [ -f "$_lic_src" ]; then
                        cp "$_lic_src" "{prefix}-$(basename "$_lic_src")"
                    elif [ -d "$_lic_src" ]; then
                        for _lic_file in "$_lic_src"/*; do
                            cp "$_lic_file" "{prefix}-$(basename "$_lic_src")-$(basename "$_lic_file")"
                        done
                    fi
                done
                """
            )

        return script

    def get_build_install_env(self, build: targets.Build, wd: str) -> Args:
        return self.get_build_env(build, wd=wd)

    def get_build_tools(self, build: targets.Build) -> dict[str, pathlib.Path]:
        return {}

    def get_build_tools_path(
        self,
        build: targets.Build,
        *,
        relative_to: targets.Location = "pkgbuild",
        relative_to_package: BasePackage | None = None,
        wd: str | None = None,
    ) -> str | None:
        if wd is None:
            wd = "$(pwd -P)"

        rel_bin_path = self.get_install_path(build, "bin")
        if rel_bin_path:
            root_path = build.get_build_install_dir(
                self,
                relative_to=relative_to,
                relative_to_package=relative_to_package,
            )
            bin_path = root_path / rel_bin_path.relative_to("/")
            return f"{wd}/{shlex.quote(str(bin_path))}"
        else:
            return None

    def get_patches(
        self,
    ) -> dict[str, list[tuple[str, str]]]:
        return {}

    def _get_file_list_script(
        self,
        build: targets.Build,
        listname: str,
        *,
        entries: list[str],
        imply_parents: bool,
    ) -> str:
        if entries:
            script = self.write_file_list_script(
                build, listname, entries, imply_parents
            )
        else:
            script = ""

        return script

    def get_file_install_entries(self, build: targets.Build) -> list[str]:
        entries = []
        for shlib in self.get_shlibs(build):
            for shlib_pat in build.target.get_shlib_filename_patterns(shlib):
                entries.append(f"{{libdir}}/{shlib_pat}")
        for lic_pattern in self.get_license_files_patterns():
            entries.append(f"{{legaldir}}/{{name}}-{lic_pattern}")
        return entries

    def get_install_list_script(self, build: targets.Build) -> str:
        entries = self.get_file_install_entries(build)
        entries += [
            str(p.relative_to("/")) for p in self.get_service_scripts(build)
        ]
        return self._get_file_list_script(
            build,
            "install",
            entries=entries,
            imply_parents=True,
        )

    def get_file_no_install_entries(self, build: targets.Build) -> list[str]:
        return [
            # Never install static libraries or libtool stuff
            "{libdir}/*.a",
            "{libdir}/*.la",
        ]

    def get_no_install_list_script(self, build: targets.Build) -> str:
        entries = self.get_file_no_install_entries(build)
        return self._get_file_list_script(
            build,
            "no_install",
            entries=entries,
            imply_parents=False,
        )

    def get_file_ignore_entries(self, build: targets.Build) -> list[str]:
        return [
            # ignore binaries by default, packages can opt-in with
            # explicit entries in install.list
            "{bindir}/*",
            # autoconf, pkg-config and CMake stuff are not useful
            "{datadir}/aclocal/**",
            "{libdir}/aclocal/**",
            "{datadir}/cmake/**",
            "{libdir}/cmake/**",
            "{datadir}/pkgconfig/**",
            "{libdir}/pkgconfig/**",
            # likewise, include files
            "{includedir}/**",
            # And the documentation
            "{mandir}/**",
            "{docdir}/**",
            "{infodir}/**",
            "{datadir}/gtk-doc/**",
            "{bundlemandir}/**",
            "{bundledocdir}/**",
            "{bundleinfodir}/**",
            "{bundledatadir}/gtk-doc",
            "{bundledatadir}/gtk-doc/**",
        ]

    def get_ignore_list_script(self, build: targets.Build) -> str:
        entries = self.get_file_ignore_entries(build)
        return self._get_file_list_script(
            build,
            "ignore",
            entries=entries,
            imply_parents=True,
        )

    def get_private_libraries(self, build: targets.Build) -> list[str]:
        return []

    def get_extra_system_requirements(
        self, build: targets.Build
    ) -> dict[str, list[str]]:
        return {}

    def get_before_install_script(self, build: targets.Build) -> str:
        return ""

    def get_after_install_script(self, build: targets.Build) -> str:
        return ""

    def get_service_scripts(
        self, build: targets.Build
    ) -> dict[pathlib.Path, str]:
        return {}

    def get_bin_shims(self, build: targets.Build) -> dict[str, str]:
        return {}

    def get_exposed_commands(self, build: targets.Build) -> list[pathlib.Path]:
        return []

    def get_install_path(
        self,
        build: targets.Build,
        aspect: targets.InstallAspect,
    ) -> pathlib.Path | None:
        pkg_config = self.get_pkg_config_meta()
        if aspect == "lib" and not pkg_config.provides_shlibs:
            return None
        elif aspect == "include" and not pkg_config.provides_c_headers:
            return None
        elif (
            aspect == "bin"
            and not pkg_config.provides_build_tools
            and not pkg_config.pkg_config_script
        ):
            return None
        else:
            return build.get_install_path(self, aspect)

    def get_shlibs(self, build: targets.Build) -> list[str]:
        return []

    def get_dep_commands(self) -> list[str]:
        return []

    def get_dep_install_subdir(
        self,
        build: targets.Build,
        pkg: BasePackage,
    ) -> pathlib.Path:
        return pathlib.Path("")

    def get_root_install_subdir(
        self,
        build: targets.Build,
    ) -> pathlib.Path:
        raise NotImplementedError

    def write_file_list_script(
        self,
        build: targets.Build,
        listname: str,
        entries: list[str],
        imply_parents: bool,
    ) -> str:
        installdest = build.get_build_install_dir(self, relative_to="pkgbuild")

        paths: dict[str, str | pathlib.Path] = {}
        for aspect in (
            "systembin",
            "sysconf",
            "bin",
            "data",
            "include",
            "lib",
            "legal",
            "doc",
            "man",
            "info",
        ):
            path = build.get_install_path(self, aspect)
            paths[f"{aspect}dir"] = path.relative_to("/")
            path = build.get_bundle_install_path(aspect)
            paths[f"bundle{aspect}dir"] = path.relative_to("/")

        paths["name"] = self.name
        paths["version"] = str(self.version)
        paths["prefix"] = build.get_rel_install_prefix(self)
        paths["exesuffix"] = build.get_exe_suffix()

        processed_entries = []
        for entry in entries:
            processed_entries.append(
                entry.strip().format(**paths).replace("/", os.sep)
            )

        pyscript = textwrap.dedent(
            """\
            import glob
            import pathlib

            tmp = pathlib.Path({installdest!r})

            patterns = {patterns}

            matches = set()

            for pattern in patterns:
                if pattern.endswith('/**'):
                    pattern += "/*"
                for p in tmp.glob(pattern):
                    if p.exists():
                        rel_p = p.relative_to(tmp)
                        matches.add(rel_p)
                        if {imply_parents}:
                            matches.update(rel_p.parents)

            for match in matches:
                print(match)
        """
        ).format(
            installdest=str(installdest),
            patterns=pprint.pformat(processed_entries),
            imply_parents=str(imply_parents),
        )

        scriptfile_name = f"_gen_{listname}_list_{self.unique_name}.py"

        return build.sh_write_python_helper(
            scriptfile_name, pyscript, relative_to="pkgbuild"
        )

    def get_package_layout(self, build: targets.Build) -> PackageFileLayout:
        return PackageFileLayout.REGULAR

    @property
    def all_requires(
        self,
    ) -> list[poetry_dep.Dependency]:
        if all_requires_include_build_reqs:
            return super().all_requires + get_build_requirements(self)
        else:
            return super().all_requires


@dataclasses.dataclass(kw_only=True)
class PkgConfigMeta:
    #: Name used by pkg-config or CMake or autoconf to refer to this package.
    pkg_name: str
    #: Package-provided a package-config script, eg bin/package-config
    pkg_config_script: str | None = None
    #: Whether the package provides a pkg-config (*.pc) file
    provides_pkg_config: bool = False
    #: Whether the package provides shared libraries
    provides_shlibs: bool = False
    #: Whether the package provides C/C++ header files
    provides_c_headers: bool = False
    #: Whether the package provides build tools
    provides_build_tools: bool = False


BundledPackage_T = TypeVar("BundledPackage_T", bound="BundledPackage")

RequirementsSpec = Union[
    list[str | poetry_dep.Dependency],
    Mapping[
        str | poetry_version.VersionConstraint,
        Sequence[str | poetry_dep.Dependency],
    ],
]


class BundledPackage(BasePackage):
    ident: ClassVar[str]
    title: ClassVar[str | None] = None
    aliases: ClassVar[list[str] | None] = None
    description: str = ""
    license_id: ClassVar[str | None] = None
    group: ClassVar[str]
    url: ClassVar[str | None] = None
    identifier: ClassVar[str]

    source_version: str

    artifact_requirements: RequirementsSpec = []
    artifact_build_requirements: RequirementsSpec = []
    build_requires: list[poetry_dep.Dependency]

    options: dict[str, Any]
    metadata_tags: dict[str, str]

    sources: list[af_sources.SourceDecl]
    resolved_sources: list[af_sources.BaseSource] = []

    @property
    def slot(self) -> str:
        return ""

    @property
    def slot_suffix(self) -> str:
        if self.slot:
            return f"-{self.slot}"
        else:
            return ""

    @property
    def supports_out_of_tree_builds(self) -> bool:
        return True

    @property
    def name_slot(self) -> str:
        return f"{self.name}{self.slot_suffix}"

    @property
    def name_for_user_and_dir(self) -> str:
        return self.name

    def version_includes_revision(self) -> bool:
        return True

    def version_includes_slot(self) -> bool:
        return True

    @classmethod
    def get_source_url_variables(cls, version: str) -> dict[str, str]:
        return {}

    @classmethod
    def _get_sources(cls, version: str | None) -> list[af_sources.BaseSource]:
        sources = []

        if version is None:
            version = "HEAD"
        underscore_v = version.replace(".", "_")
        dash_v = version.replace(".", "-")
        parts = version.split(".")
        major_v = parts[0]
        major_minor_v = ".".join(parts[:2])
        for source in cls.sources:
            if isinstance(source, dict):
                clsfile = inspect.getsourcefile(cls)
                if clsfile is not None:
                    clsdirname = pathlib.Path(clsfile).parent
                else:
                    clsdirname = None
                url = source["url"].format(
                    version=version,
                    underscore_version=underscore_v,
                    dash_version=dash_v,
                    major_v=major_v,
                    major_minor_v=major_minor_v,
                    dirname=clsdirname,
                    **cls.get_source_url_variables(version),
                )
                extras = af_sources.SourceExtraDecl(
                    source.get("extras", {})  # type: ignore
                )
                if extras:
                    if "version" not in extras:
                        extras["version"] = version
                else:
                    extras = af_sources.SourceExtraDecl({"version": version})

                if "vcs_version" not in extras and "version" in extras:
                    extras["vcs_version"] = cls.to_vcs_version(
                        extras["version"]
                    )

                src = af_sources.source_for_url(url, extras)
                src.path = cast(str, source.get("path"))

                csum = source.get("csum")
                csum_url = source.get("csum_url")
                csum_algo = source.get("csum_algo")

                if csum_algo:
                    if csum_url:
                        csum_url = csum_url.format(
                            version=version,
                            underscore_version=underscore_v,
                            dash_version=dash_v,
                        )
                    csum_verify = af_sources.HashVerification(
                        csum_algo, hash_url=csum_url, hash_value=csum
                    )
                    src.add_verification(csum_verify)

            else:
                src = af_sources.source_for_url(source)

            sources.append(src)

        return sources

    @classmethod
    def to_vcs_version(cls, version: str) -> str:
        return version

    @classmethod
    def parse_vcs_version(cls, version: str) -> poetry_version.Version:
        return poetry_version.Version.parse(version)

    @classmethod
    def get_package_repository(
        cls, target: targets.Target, io: cleo_io.IO
    ) -> poetry_repo.Repository:
        return repository.bundle_repo

    @classmethod
    def version_from_source(
        cls,
        source_dir: pathlib.Path,
    ) -> str:
        raise NotImplementedError

    @classmethod
    def get_vcs_source(
        cls, io: cleo_io.IO, ref: str | None = None
    ) -> af_sources.GitSource | None:
        sources = cls._get_sources(version=ref)
        if isinstance(sources[0], af_sources.GitSource):
            return sources[0]
        else:
            return None

    @classmethod
    def resolve_vcs_repo(
        cls,
        io: cleo_io.IO,
        version: str | None = None,
    ) -> tools.git.GitClone:
        source = cls.get_vcs_source(io, ref=version)
        if source is None:
            raise ValueError("Unable to resolve non-git bundled package")
        return source.download(io)

    @classmethod
    def get_next_feature_version(
        cls,
        version: poetry_version.Version,
    ) -> poetry_version.Version:
        return version.next_minor()

    @classmethod
    def version_from_vcs_version(
        cls,
        io: cleo_io.IO,
        repo: tools.git.Git,
        vcs_version: str,
        is_release: bool,
    ) -> str:
        ver = repo.run("describe", "--tags", vcs_version).strip()
        if ver.startswith("v"):
            ver = ver[1:]

        parts = ver.rsplit("-", maxsplit=2)
        if (
            len(parts) == 3
            and parts[2].startswith("g")
            and parts[1].isdigit()
            and parts[1].isascii()
        ):
            # Have commits after the tag
            parsed_ver = cls.get_next_feature_version(
                cls.parse_vcs_version(parts[0]),
            )

            if not is_release:
                commits = repo.run(
                    "rev-list",
                    "--count",
                    vcs_version,
                )

                ver = parsed_ver.replace(
                    local=None,
                    pre=None,
                    dev=poetry_pep440.ReleaseTag("dev", int(commits)),
                ).to_string()
            else:
                ver = parsed_ver.to_string()

        return ver

    @classmethod
    def resolve(
        cls: Type[BundledPackage_T],
        io: cleo_io.IO,
        *,
        name: NormalizedName | None = None,
        version: str | None = None,
        revision: str | None = None,
        is_release: bool = False,
        target: targets.Target,
        requires: list[poetry_dep.Dependency] | None = None,
    ) -> BundledPackage_T:
        sources = list(cls._get_sources(version))
        vcs_source = cls.get_vcs_source(io, version)
        is_git = vcs_source is not None
        git_date = ""

        if vcs_source is not None:
            sources[0] = vcs_source
            repo = cls.resolve_vcs_repo(io, version)
            vcs_version = cls.to_vcs_version(version) if version else "HEAD"
            source_version = repo.resolve_local_rev(vcs_version)
            if source_version is None:
                raise ValueError(f"could not resolve {vcs_version}")
            version = cls.version_from_vcs_version(
                io, repo, source_version, is_release
            )

            git_date = repo.run(
                "log",
                "-1",
                "--format=%cd",
                "--date=format-local:%Y%m%d%H",
                source_version,
                env={**os.environ, **{"TZ": "UTC", "LANG": "C"}},
            )
        elif version is not None:
            source_version = version
        elif isinstance(sources[0], af_sources.LocalSource):
            source_dir = sources[0].url
            version = cls.version_from_source(pathlib.Path(source_dir))
            source_version = version
        else:
            raise ValueError("version must be specified for non-git packages")

        if not revision:
            revision = "1"

        if is_git:
            ver = cls.parse_vcs_version(version)
        else:
            ver = poetry_version.Version.parse(version)

        local = ver.local
        if isinstance(ver.local, tuple):
            local = ver.local
        elif ver.local is None:
            local = ()
        else:
            local = (ver.local,)

        if is_git:
            ver = ver.replace(
                local=local
                + (
                    f"r{revision}",
                    f"d{git_date}",
                    f"g{source_version[:9]}",
                )
            )
        else:
            ver = ver.replace(local=local + (f"r{revision}",))

        version, pretty_version = cls.format_version(ver)

        return cls(
            version=version,
            pretty_version=pretty_version,
            source_version=source_version,
            resolved_sources=sources,
            requires=requires,
            name=name,
        )

    @classmethod
    def format_version(cls, ver: poetry_version.Version) -> tuple[str, str]:
        full_ver = pep440_to_semver(ver)
        version_base = pep440_to_semver(ver.without_local())
        version_hash = hashlib.sha256(full_ver.encode("utf-8")).hexdigest()
        version = f"{version_base}+{version_hash[:7]}"
        pretty_version = f"{full_ver}.s{version_hash[:7]}"
        return version, pretty_version

    def get_root_install_subdir(self, build: targets.Build) -> pathlib.Path:
        return pathlib.Path(self.name_slot)

    def get_dep_pkg_config_meta(self, dep: BasePackage) -> PkgConfigMeta:
        if isinstance(dep, BundledPackage):
            return dep.get_pkg_config_meta()
        else:
            return _get_bundled_pkg_config_meta(dep.name)

    def get_sources(self) -> list[af_sources.BaseSource]:
        if self.resolved_sources:
            return self.resolved_sources
        else:
            return self._get_sources(version=self.source_version)

    def get_patches(
        self,
    ) -> dict[str, list[tuple[str, str]]]:
        modpath = pathlib.Path(sys.modules[self.__module__].__path__[0])
        patches_dir = modpath / "patches"

        patches = collections.defaultdict(list)
        if patches_dir.exists():
            for path in patches_dir.glob("*.patch"):
                with open(path, "r") as f:
                    pkg, _, rest = path.stem.partition("__")
                    patches[pkg].append((rest, f.read()))

            for pkg, plist in patches.items():
                plist.sort(key=lambda i: i[0])

        return patches

    def __init__(
        self,
        version: str | poetry_version.Version,
        pretty_version: str | None = None,
        *,
        source_version: str | None = None,
        requires: list[poetry_dep.Dependency] | None = None,
        options: Mapping[str, Any] | None = None,
        resolved_sources: list[af_sources.BaseSource] | None = None,
        name: NormalizedName | None = None,
    ) -> None:
        if self.title is None:
            raise RuntimeError(
                f"{type(self)!r} does not define the required "
                f"title attribute"
            )

        if name is None:
            name = canonicalize_name(self.__class__.ident)

        super().__init__(name, version, pretty_version=pretty_version)

        if requires is not None:
            reqs = list(requires)
        else:
            reqs = []

        reqs.extend(self.get_requirements())

        if reqs:
            if not self.has_dependency_group(poetry_depgroup.MAIN_GROUP):
                self.add_dependency_group(
                    poetry_depgroup.DependencyGroup(
                        poetry_depgroup.MAIN_GROUP
                    ),
                )

            main_group = self._dependency_groups[poetry_depgroup.MAIN_GROUP]
            for req in reqs:
                main_group.add_dependency(req)

        if resolved_sources is not None:
            self.resolved_sources = list(resolved_sources)
        else:
            self.resolved_sources = []

        self.metadata_tags = {}

        repository.set_build_requirements(self, self.get_build_requirements())
        self.description = type(self).description
        license_id = type(self).license_id
        if license_id is not None:
            self.license = poetry_spdx_helpers.license_by_id(license_id)
        self.options = dict(options) if options is not None else {}
        if source_version is None:
            self.source_version = self.pretty_version
        else:
            self.source_version = source_version

        repository.bundle_repo.add_package(self)

        if self.aliases:
            for alias in self.aliases:
                pkg = AliasPackage(name=alias, version=self.version)
                pkg.add_dependency(
                    poetry_dep.Dependency(self.name, self.version)
                )
                repository.bundle_repo.add_package(pkg)

    @property
    def pretty_version(self) -> str:
        return self._pretty_version

    def _get_requirements(
        self,
        spec: RequirementsSpec,
        prop: str,
    ) -> list[poetry_dep.Dependency]:
        reqs = []

        req_spec: list[str | poetry_dep.Dependency] = []

        if isinstance(spec, list):
            req_spec = list(spec)
        else:
            for ver, ver_reqs in spec.items():
                if isinstance(ver, str):
                    ver = poetry_version.parse_constraint(ver)
                if ver.allows(self.version):
                    req_spec.extend(ver_reqs)

            if not req_spec and spec:
                raise RuntimeError(
                    f"{prop} for {self.name!r} are not "
                    f"empty, but don't match the requested version "
                    f"{self.version}"
                )

        for item in req_spec:
            if isinstance(item, str):
                reqs.append(poetry_dep.Dependency.create_from_pep_508(item))
            else:
                reqs.append(item)
        return reqs

    def get_requirements(self) -> list[poetry_dep.Dependency]:
        return self._get_requirements(
            self.artifact_requirements,
            "artifact_requirements",
        )

    def get_build_requirements(self) -> list[poetry_dep.Dependency]:
        return self._get_requirements(
            self.artifact_build_requirements,
            "artifact_build_requirements",
        )

    def clone(self: BundledPackage_T) -> BundledPackage_T:
        clone = self.__class__(self.version)
        clone.__dict__ = copy.deepcopy(self.__dict__)
        return clone

    def is_root(self) -> bool:
        return False

    @overload
    def read_support_files(
        self, build: targets.Build, file_glob: str, binary: Literal[False]
    ) -> dict[str, str]: ...

    @overload
    def read_support_files(
        self, build: targets.Build, file_glob: str
    ) -> dict[str, str]: ...

    @overload
    def read_support_files(
        self, build: targets.Build, file_glob: str, binary: Literal[True]
    ) -> dict[str, bytes]: ...

    def read_support_files(
        self, build: targets.Build, file_glob: str, binary: bool = False
    ) -> dict[str, str] | dict[str, bytes]:
        mod = sys.modules[type(self).__module__]
        mod_file = mod.__file__
        assert mod_file is not None
        path = pathlib.Path(mod_file).parent / file_glob

        result = {}

        for pathname in glob.glob(str(path)):
            path = pathlib.Path(pathname)
            mode = "rb" if binary else "r"
            with open(path, mode) as f:
                content = f.read()
                name = path.name
                if not binary and name.endswith(".in"):
                    content = build.format_package_template(content, self)
                    name = name[:-3]
                    name = name.replace("SLOT", self.slot)
                    name = name.replace(
                        "IDENTIFIER",
                        build.target.get_package_system_ident(build, self),
                    )
                result[name] = content

        return result

    def _read_install_entries(
        self,
        build: targets.Build,
        listname: str,
    ) -> list[str]:
        mod = sys.modules[type(self).__module__]
        mod_file = mod.__file__
        assert mod_file is not None
        path = pathlib.Path(mod_file).parent / f"{listname}.list"

        entries: list[str] = []

        if path.exists():
            with open(path, "r") as f:
                entries.extend(f)

        return entries

    def get_file_install_entries(self, build: targets.Build) -> list[str]:
        entries = super().get_file_install_entries(build)
        return entries + self._read_install_entries(build, "install")

    def get_file_no_install_entries(self, build: targets.Build) -> list[str]:
        entries = super().get_file_no_install_entries(build)
        return entries + self._read_install_entries(build, "no_install")

    def get_file_ignore_entries(self, build: targets.Build) -> list[str]:
        entries = super().get_file_ignore_entries(build)
        return entries + self._read_install_entries(build, "ignore")

    def get_prepare_script(self, build: targets.Build) -> str:
        script = ""

        if not self.supports_out_of_tree_builds:
            sdir = shlex.quote(
                str(build.get_source_dir(self, relative_to="pkgbuild"))
            )
            script += f"test ./ -ef {sdir} || cp -a {sdir}/* ./\n"

        return script

    def get_build_command(
        self,
        build: targets.Build,
        args: Args,
        target: str = "",
    ) -> str:
        wd = "${_wd}"
        env = build.sh_format_command(
            "env",
            self.get_build_env(build, wd=wd),
            force_args_eq=True,
            linebreaks=False,
        )
        cmd = build.sh_format_args(args, force_args_eq=True)

        if target:
            if target.startswith("!"):
                target = target[1:]
            else:
                target = shlex.quote(target)

        return textwrap.dedent(
            f"""\
            _wd=$(pwd -P)
            {env} {cmd} {target}
            """
        )

    def get_build_install_script(self, build: targets.Build) -> str:
        script = super().get_build_install_script(build)
        service_scripts = self.get_service_scripts(build)
        if service_scripts:
            install = build.sh_get_command("cp", relative_to="pkgbuild")
            extras_dir = build.get_extras_root(relative_to="pkgbuild")
            install_dir = build.get_build_install_dir(
                self, relative_to="pkgbuild"
            )
            ensuredir = build.target.get_action("ensuredir", build)
            if TYPE_CHECKING:
                assert isinstance(ensuredir, targets.EnsureDirAction)

            commands = []

            for path, _content in service_scripts.items():
                path = path.relative_to("/")
                commands.append(
                    ensuredir.get_script(path=str((install_dir / path).parent))
                )
                args: dict[str, str | None] = {
                    str(extras_dir / path): None,
                    str(install_dir / path): None,
                }
                cmd = build.sh_format_command(install, args)
                commands.append(cmd)

            return script + "\n" + "\n".join(commands)

        return script

    def get_pre_start_script(self, build: targets.Build) -> str:
        return ""

    def get_resources(self, build: targets.Build) -> dict[str, bytes]:
        return self.read_support_files(build, "resources/*", binary=True)

    def get_service_scripts(
        self, build: targets.Build
    ) -> dict[pathlib.Path, str]:
        return build.target.service_scripts_for_package(build, self)

    def get_bin_shims(self, build: targets.Build) -> dict[str, str]:
        return self.read_support_files(build, "shims/*")

    def __repr__(self) -> str:
        return "<BundledPackage {}>".format(self.unique_name)

    def get_meta_packages(
        self,
        build: targets.Build,
        root_version: str,
    ) -> list[MetaPackage]:
        return []

    def get_conflict_packages(
        self,
        build: targets.Build,
        root_version: str,
    ) -> list[str]:
        return []

    def get_transition_packages(
        self,
        build: targets.Build,
    ) -> list[str]:
        return []

    def get_provided_packages(
        self,
        build: targets.Build,
        root_version: str,
    ) -> list[tuple[str, str]]:
        return []

    def get_version_details(self) -> dict[str, Any]:
        pv = poetry_version.Version.parse(self.pretty_version)

        prerelease = []
        if pv.pre is not None:
            prerelease.append(
                {
                    "phase": semver_pre_tag(pv),
                    "number": pv.pre.number,
                }
            )

        if pv.dev is not None:
            prerelease.append(
                {
                    "phase": pv.dev.phase,
                    "number": pv.dev.number,
                }
            )

        if pv.local:
            local: tuple[str | int, ...]
            if isinstance(pv.local, tuple):
                local = pv.local
            elif pv.local is None:
                local = ()
            else:
                local = (pv.local,)

            ver_metadata = self.parse_version_metadata(local)
        else:
            ver_metadata = {}

        return {
            "major": pv.major,
            "minor": pv.minor,
            "patch": pv.patch,
            "prerelease": prerelease,
            "metadata": ver_metadata,
        }

    def get_artifact_metadata(self, build: targets.Build) -> dict[str, Any]:
        metadata = {
            "name": self.name,
            "version": pep440_to_semver(self.version),
            "version_details": self.get_version_details(),
            "revision": build.revision,
            "build_date": build.build_date.isoformat(),
            "target": build.target.triple,
            "architecture": build.target.machine_architecture,
            "dist": build.target.ident,
            "channel": build.channel,
            "tags": self.metadata_tags,
        }

        if self.slot:
            metadata["version_slot"] = self.slot

        return metadata

    def parse_version_metadata(
        self,
        segments: tuple[str | int, ...],
    ) -> dict[str, str]:
        result = {}
        pfx_map = self.get_version_metadata_fields()
        for segment in segments:
            segment_str = str(segment)
            for pfx_len in (1, 2):
                key = pfx_map.get(segment_str[:pfx_len])
                if key is not None:
                    result[key] = segment_str[pfx_len:]
                    break
            else:
                raise RuntimeError(
                    f"unrecognized version metadata field `{segment}`"
                )

        return result

    def get_version_metadata_fields(self) -> dict[str, str]:
        return {
            "r": "build_revision",
            "d": "source_date",
            "g": "scm_revision",
            "t": "target",
            "s": "build_hash",
            "b": "build_type",
        }

    def set_metadata_tags(self, tags: Mapping[str, str]) -> None:
        self.metadata_tags = dict(tags)


def merge_requirements(
    *specs: RequirementsSpec,
) -> RequirementsSpec:
    result: collections.defaultdict[
        str | poetry_version.VersionConstraint,
        list[str | poetry_dep.Dependency],
    ] = collections.defaultdict(list)
    for spec in specs:
        if isinstance(spec, list):
            result["*"].extend(spec)
        else:
            for k, v in spec.items():
                result[k].extend(v)
    return dict(result)


@functools.cache
def _get_bundled_pkg_config_meta(name: str) -> PkgConfigMeta:
    package = _get_pkg_in_bundle_repo(poetry_dep.Dependency(name, "*"))
    if isinstance(package, BasePackage):
        return package.get_pkg_config_meta()
    else:
        return PkgConfigMeta(
            pkg_name=package.name.upper(),
            pkg_config_script=None,
            provides_pkg_config=False,
            provides_shlibs=False,
            provides_c_headers=False,
            provides_build_tools=False,
        )


@functools.cache
def _get_pkg_in_bundle_repo(dep: poetry_dep.Dependency) -> poetry_pkg.Package:
    packages = repository.bundle_repo.find_packages(dep)
    if not packages:
        raise poetry_repo_exc.PackageNotFoundError(
            f"package {dep.pretty_name} not found in bundled repo."
        )

    packages.sort(key=lambda pkg: pkg.version, reverse=True)
    return packages[0]


def get_bundled_pkg(dep: poetry_dep.Dependency) -> BundledPackage:
    package = _get_pkg_in_bundle_repo(dep)
    if not isinstance(package, BundledPackage):
        raise RuntimeError(
            f"package {package} is in the bundle repo, but it "
            "is not a BundledPackage"
        )
    return package


class PrePackagedPackage(BundledPackage):
    pass


class BuildSystemMakePackage(BundledPackage):

    def get_build_script(self, build: targets.Build) -> str:
        args = self.get_make_args(build)
        target = self.get_make_target(build)
        return self.get_build_command(build, args, target)

    def get_build_command(
        self,
        build: targets.Build,
        args: Args,
        target: str = "",
    ) -> str:
        wd = "${_wd}"
        # Undefining MAKELEVEL is required because
        # some package makefiles have
        # conditions on MAKELEVEL.
        env = build.sh_format_command(
            "env",
            {"-uMAKELEVEL": None} | self.get_build_env(build, wd=wd),
            force_args_eq=True,
            linebreaks=False,
        )
        make_args = {f"-j{build.build_parallelism}": None} | args
        make = build.sh_get_command("make", args=make_args, force_args_eq=True)

        if target:
            target = shlex.quote(target)

        return textwrap.dedent(
            f"""\
            _wd=$(pwd -P)
            {env} {make} {target}
            """
        )

    def get_build_install_command(
        self,
        build: targets.Build,
        args: Args,
        target: str,
    ) -> str:
        wd = "${_wd}"
        env = build.sh_format_command(
            "env",
            {"-uMAKELEVEL": None} | self.get_build_install_env(build, wd=wd),
            force_args_eq=True,
            linebreaks=False,
        )
        make_args: Args = {f"-j{build.build_parallelism}": None}
        build.sh_append_quoted_flags(
            make_args,
            "DESTDIR",
            [self.sh_get_make_install_destdir(build, wd=wd)],
        )
        make_args |= args
        make = build.sh_get_command("make", args=make_args, force_args_eq=True)

        if target:
            target = shlex.quote(target)

        return textwrap.dedent(
            f"""\
            _wd=$(pwd -P)
            {env} {make} {target}
            """
        )

    def get_make_args(self, build: targets.Build) -> Args:
        return {}

    def get_make_target(self, build: targets.Build) -> str:
        return ""

    def get_make_install_args(self, build: targets.Build) -> Args:
        return {}

    def get_make_install_target(self, build: targets.Build) -> str:
        return "install"

    def sh_get_make_install_destdir(
        self,
        build: targets.Build,
        wd: str,
    ) -> str:
        instdir = build.get_build_install_dir(
            self, relative_to="pkgbuild"
        ) / self.get_make_install_destdir_subdir(build)
        return f"{wd}/{shlex.quote(str(instdir))}"

    def get_make_install_destdir_subdir(
        self,
        build: targets.Build,
    ) -> pathlib.Path:
        return pathlib.Path("")

    def get_build_install_script(self, build: targets.Build) -> str:
        script = super().get_build_install_script(build)
        if target := self.get_make_install_target(build):
            args = self.get_make_install_args(build)
            make_install = self.get_build_install_command(build, args, target)
            script += "\n" + make_install

        return script

    def get_binary_output_dir(self) -> pathlib.Path:
        """Return path relative to the build dir where the result binaries are"""
        return pathlib.Path("bin")


class BundledCPackage(BuildSystemMakePackage):
    # Assume all C packages are well-behaved and install *.pc files.
    @property
    def provides_pkg_config(self) -> bool:
        return True

    @property
    def provides_shlibs(self) -> bool:
        return True

    @property
    def provides_c_headers(self) -> bool:
        return True

    def configure_dependency(
        self,
        build: targets.Build,
        dep: BasePackage,
        conf_args: Args,
        conf_env: Args,
        wd: str | None = None,
    ) -> None:
        if build.is_bundled(dep):
            build.sh_append_pkgconfig_paths(conf_env, dep, wd=wd)

            rel_path = build.sh_get_bundled_install_path(dep, wd=wd)
            ldflags = [f"-L{rel_path}/lib/"]

            if platform.system() == "Darwin":
                root = build.get_build_install_dir(dep, relative_to="pkgbuild")
                # In case ./configure tries to compile and test a program
                # and it fails because dependency is not yet installed
                # at its install_name location.
                conf_env["DYLD_FALLBACK_LIBRARY_PATH"] = root
            else:
                ldflags.append(f"-Wl,-rpath-link,{rel_path}/lib")

            build.sh_append_quoted_ldflags(conf_env, ldflags)

            bin_path = build.sh_get_bundled_pkg_bin_path(dep)
            if bin_path:
                build.sh_prepend_quoted_paths(conf_env, "PATH", [bin_path])

    def sh_get_configure_command(self, build: targets.Build) -> str:
        if self.supports_out_of_tree_builds:
            sdir = build.get_source_dir(self, relative_to="pkgbuild")
        else:
            sdir = build.get_build_dir(self, relative_to="pkgbuild")

        return shlex.quote(str(sdir / "configure"))

    def get_configure_args(
        self,
        build: targets.Build,
        wd: str | None = None,
    ) -> Args:
        conf_args: Args = {}
        return conf_args

    def get_configure_env(
        self,
        build: targets.Build,
        wd: str | None = None,
    ) -> Args:
        env_args: Args = {}
        build.sh_append_run_time_ldflags(env_args, self)
        build.sh_append_link_time_ldflags(env_args, self, wd=wd)
        all_build_deps = build.get_build_reqs(self, recursive=True)
        return build.sh_append_global_flags(env_args) | build.get_ld_env(
            all_build_deps, wd=wd
        )

    def get_configure_script(self, build: targets.Build) -> str:
        script = super().get_configure_script(build)
        script += "_wd=$(pwd -P)\n"
        wd = "${_wd}"
        cmd = self.sh_get_configure_command(build)

        args = self.get_configure_args(build, wd=wd)
        env = self.get_configure_env(build, wd=wd)

        for build_dep in build.get_build_reqs(self, bundled_only=False):
            self.configure_dependency(build, build_dep, args, env, wd=wd)

        conf_script = build.sh_append_args(
            cmd, args, force_args_eq=True, linebreaks=False
        )

        if env:
            env_script = build.sh_format_command(
                "env", env, force_args_eq=True
            )
            script += f"{env_script} {textwrap.indent(conf_script, '  ')}"
        else:
            script += conf_script

        return script

    def get_build_install_script(self, build: targets.Build) -> str:
        script = super().get_build_install_script(build)
        install_target = self.get_make_install_target(build)

        if install_target:
            find = build.sh_get_command("find")
            sed = build.sh_get_command("sed")
            destdir = self.sh_get_make_install_destdir(build, "$(pwd)")
            libdir = self.get_install_path(build, "lib")
            re_libdir = re.escape(str(libdir))
            includedir = self.get_install_path(build, "include")
            re_includedir = re.escape(str(includedir))
            prefix = build.get_install_prefix(self)
            re_prefix = re.escape(str(prefix))
            script += "\n" + textwrap.dedent(
                f"""\
                _d={destdir}
                {find} "$_d" -name '*.la' -exec {sed} -i -r -e \
                    "s|{re_libdir}|${{_d}}{libdir}|g" {{}} \\;
                {find} "$_d" -path '*/pkgconfig/*.pc' -exec {sed} -i -r -e \
                    "s|includedir\\s*=.*|includedir=${{_d}}{includedir}|g
                     s|libdir\\s*=.*|libdir=${{_d}}{libdir}|g
                     s|exec_prefix\\s*=.*|exec_prefix=${{_d}}{prefix}|g
                    " {{}} \\;
                {find} "$_d" -path '*/cmake/*/*.cmake' -exec {sed} -i -r -e \
                    "s|_IMPORT_PREFIX\\s+\\"{re_prefix}\\"|_IMPORT_PREFIX \\"${{_d}}{prefix}\\"|g
                     s|(\\"\\|;){re_includedir}|\\1${{_d}}{includedir}|g
                     s|(\\"\\|;){re_libdir}|\\1${{_d}}{libdir}|g
                    " {{}} \\;
                """
            )
            if cfg := self.get_dep_pkg_config_script():
                script += "\n" + textwrap.dedent(
                    f"""\
                    {find} "$_d" -path '*/bin/{cfg}' -exec {sed} -i -r -e \
                        "s|includedir\\s*=.*|includedir=${{_d}}{includedir}|g
                         s|libdir\\s*=.*|libdir=${{_d}}{libdir}|g
                         s|exec_prefix\\s*=.*|exec_prefix=${{_d}}{prefix}|g
                         s|(-I){re_includedir}|\\1${{_d}}{includedir}|g
                         s|(-L){re_libdir}|\\1${{_d}}{libdir}|g
                        " {{}} \\;
                    """
                )

        return script


class BundledCAutoconfPackage(BundledCPackage):
    def get_configure_args(
        self,
        build: targets.Build,
        wd: str | None = None,
    ) -> Args:
        return super().get_configure_args(build, wd=wd) | {
            "--prefix": build.get_install_prefix(self),
            "--bindir": build.get_install_path(self, "bin"),
            "--sbindir": build.get_install_path(self, "bin"),
            "--sysconfdir": build.get_install_path(self, "sysconf"),
            "--localstatedir": build.get_install_path(self, "localstate"),
            "--libdir": build.get_install_path(self, "lib"),
            "--includedir": build.get_install_path(self, "include"),
            "--datarootdir": build.get_install_path(self, "data"),
            "--docdir": build.get_install_path(self, "doc"),
            "--mandir": build.get_install_path(self, "man"),
            "--infodir": build.get_install_path(self, "info"),
        }

    def configure_dependency(
        self,
        build: targets.Build,
        dep: BasePackage,
        conf_args: Args,
        conf_env: Args,
        wd: str | None = None,
    ) -> None:
        super().configure_dependency(build, dep, conf_args, conf_env, wd=wd)
        try:
            pkg_config_meta = self.get_dep_pkg_config_meta(dep)
        except poetry_repo_exc.PackageNotFoundError:
            # This is a preinstalled system build-time package,
            # for which we have no in-tree definition.
            return

        var_prefix = pkg_config_meta.pkg_name
        if build.is_bundled(dep):
            if not pkg_config_meta.provides_pkg_config:
                dep_ldflags = build.sh_get_bundled_pkg_ldflags(dep, wd=wd)

                for shlib in dep.get_shlibs(build):
                    dep_ldflags.append(f"-l{shlex.quote(shlib)}")

                transitive_deps = build.get_build_reqs(dep, recursive=True)
                transitive_cflags = build.sh_get_bundled_pkgs_cflags(
                    transitive_deps
                )

                rel_path = build.sh_get_bundled_install_path(
                    dep, relative_to="pkgbuild", wd=wd
                )

                if var_prefix:
                    build.sh_append_quoted_flags(
                        conf_args,
                        f"{var_prefix}_CFLAGS",
                        [f"-I{rel_path}/include"] + transitive_cflags,
                    )
                    build.sh_append_quoted_flags(
                        conf_args,
                        f"{var_prefix}_LIBS",
                        dep_ldflags,
                    )
                else:
                    build.sh_append_quoted_flags(
                        conf_args,
                        "CFLAGS",
                        [f"-I{rel_path}/include"] + transitive_cflags,
                    )
                    build.sh_append_quoted_ldflags(
                        conf_args,
                        dep_ldflags,
                    )

        elif build.is_stdlib(dep) and pkg_config_meta.provides_pkg_config:
            conf_args[f"{var_prefix}_CFLAGS"] = f"-D_{var_prefix}_IS_SYSLIB"
            std_ldflags = []
            for shlib in dep.get_shlibs(build):
                std_ldflags.append(f"-l{shlib}")
            conf_args[f"{var_prefix}_LIBS"] = build.sh_join_flags(std_ldflags)


class BundledCMesonPackage(BundledCPackage):
    def sh_get_configure_command(self, build: targets.Build) -> str:
        sdir = str(build.get_source_dir(self, relative_to="pkgbuild"))
        bdir = str(build.get_build_dir(self, relative_to="pkgbuild"))
        meson = build.sh_get_command("meson")
        return f"{meson} setup {shlex.quote(sdir)} {shlex.quote(bdir)}"

    def get_configure_args(
        self,
        build: targets.Build,
        wd: str | None = None,
    ) -> Args:
        return {
            "--prefix": build.get_install_prefix(self),
            "--sysconfdir": build.get_install_path(self, "sysconf"),
            "--bindir": build.get_install_path(self, "bin"),
            "--sbindir": build.get_install_path(self, "bin"),
            "--libdir": build.get_install_path(self, "lib"),
            "--localstatedir": build.get_install_path(self, "localstate"),
            "--includedir": build.get_install_path(self, "include"),
            # Meson does not support --docdir
            # "--docdir": build.get_install_path(self, "doc"),
            "--mandir": build.get_install_path(self, "man"),
            "--infodir": build.get_install_path(self, "info"),
            "-Ddefault_library": "shared",
        }

    def get_configure_env(
        self,
        build: targets.Build,
        wd: str | None = None,
    ) -> Args:
        env_args = dict(super().get_configure_env(build, wd))
        build.sh_append_run_time_ldflags(env_args, self)
        env_args = build.sh_append_global_flags(env_args)
        return env_args

    def get_build_command(
        self,
        build: targets.Build,
        args: Args,
        target: str = "",
    ) -> str:
        wd = "${_wd}"

        env = build.sh_format_command(
            "env",
            self.get_build_env(build, wd=wd),
            force_args_eq=True,
            linebreaks=False,
        )

        bdir = str(build.get_build_dir(self, relative_to="pkgbuild"))
        meson_args: Args = {
            "compile": None,
            "-C": bdir,
        }
        ninja_args = {
            f"-j{build.build_parallelism}": None,
            "--verbose": None,
        } | args
        ninja_args_line = build.sh_format_args(
            ninja_args, force_args_eq=True, linebreaks=False
        )
        build.sh_append_quoted_flags(
            meson_args,
            "--ninja-args",
            [ninja_args_line],
        )
        if target:
            meson_args[target] = None

        meson_compile = build.sh_get_command(
            "meson",
            args=meson_args,
            force_args_eq=False,
        )

        return textwrap.dedent(
            f"""\
            _wd=$(pwd -P)
            {env} {meson_compile}
            """
        )

    def get_build_install_script(self, build: targets.Build) -> str:
        script = BundledPackage.get_build_install_script(self, build)
        meson = build.sh_get_command("meson")
        destdir = self.sh_get_make_install_destdir(build, "$(pwd)")
        bdir = str(build.get_build_dir(self, relative_to="pkgbuild"))
        script += "\n" + textwrap.dedent(
            f"""\
            {meson} install -C {shlex.quote(bdir)} --destdir={destdir} --no-rebuild
            """
        )

        return script


CMakeTargetBuildSystem = Literal["make", "ninja"]


class BundledCMakePackage(BundledCPackage):
    def sh_get_configure_command(self, build: targets.Build) -> str:
        srcdir = str(build.get_source_dir(self, relative_to="pkgbuild"))
        bdir = str(build.get_build_dir(self, relative_to="pkgbuild"))
        return build.sh_get_command(
            "cmake", args={"-S": srcdir, "-B": bdir}, linebreaks=False
        )

    def get_target_build_system(
        self,
        build: targets.Build,
    ) -> CMakeTargetBuildSystem:
        return "make"

    def get_configure_script(self, build: targets.Build) -> str:
        build_rules_path = str(
            build.get_build_dir(self, relative_to="fsroot")
            / "metapkg_rules.cmake"
        )
        build_rules: list[str] = []
        config_path = "metapkg_common_config.cmake"
        config = []

        buildsys = self.get_target_build_system(build)
        if buildsys == "make":
            make = build.sh_get_command("make")
            config.append(
                f'set(CMAKE_MAKE_PROGRAM "{make}" CACHE PATH "path to make")'
            )
        elif buildsys == "ninja":
            ninja = build.sh_get_command("ninja")
            config.append(
                f'set(CMAKE_MAKE_PROGRAM "{ninja}" CACHE PATH "path to ninja")'
            )
        else:
            raise AssertionError(f"unexpected target build system: {buildsys}")

        config.append(
            'set(CMAKE_PREFIX_PATH "$ENV{CMAKE_PREFIX_PATH}" CACHE STRING "" FORCE)'
        )

        config.append(
            'set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY CACHE STRING "" FORCE)'
        )

        config.append(
            f'set(CMAKE_INSTALL_PREFIX "{build.get_install_prefix(self)}" CACHE STRING "" FORCE)'
        )

        config.append(
            f'set(CMAKE_INSTALL_BINDIR "{build.get_rel_install_path(self, "bin")}" '
            f'CACHE PATH "Output directory for binaries")'
        )

        config.append(
            f'set(CMAKE_INSTALL_LIBDIR "{build.get_rel_install_path(self, "lib")}" '
            f'CACHE PATH "Output directory for libraries")'
        )

        config.append(
            f'set(CMAKE_INSTALL_INCLUDEDIR "{build.get_rel_install_path(self, "include")}" '
            f'CACHE PATH "Output directory for headers")'
        )

        config.append(
            f'set(CMAKE_INSTALL_DATAROOTDIR "{build.get_rel_install_path(self, "data")}" '
            f'CACHE PATH "Output directory for data")'
        )

        config.append(
            f'set(CMAKE_INSTALL_DOCDIR "{build.get_rel_install_path(self, "doc")}" '
            f'CACHE PATH "Output directory for documentation")'
        )

        config.append(
            f'set(CMAKE_INSTALL_MANDIR "{build.get_rel_install_path(self, "man")}" '
            f'CACHE PATH "Output directory for man pages")'
        )

        config.append(
            f'set(CMAKE_INSTALL_INFODIR "{build.get_rel_install_path(self, "info")}" '
            f'CACHE PATH "Output directory for info pages")'
        )

        config.extend(
            [
                f'set(CMAKE_USER_MAKE_RULES_OVERRIDE "{build_rules_path}" '
                + 'CACHE FILEPATH "metapkg override rules")',
                'set(BUILD_SHARED_LIBS ON CACHE BOOL "")',
                'set(Python3_FIND_UNVERSIONED_NAMES FIRST CACHE STRING "")',
                'set(CMAKE_DISABLE_PRECOMPILE_HEADERS ON CACHE BOOL "")',
                'set(CMAKE_TLS_VERIFY ON CACHE BOOL "")',
                'set(CMAKE_COMPILE_WARNING_AS_ERROR OFF CACHE BOOL "")',
            ]
        )

        build_rules_text = textwrap.indent(
            "\n".join(build_rules), " " * 12
        ).lstrip()
        config_text = textwrap.indent("\n".join(config), " " * 12).lstrip()
        script = textwrap.dedent(
            f"""
            _wd=$(pwd -P)
            cat > "{build_rules_path}" <<- '_EOF_'
            {build_rules_text}
            _EOF_
            cat > "{config_path}" <<- '_EOF_'
            {config_text}
            _EOF_
            """
        )
        return script + super().get_configure_script(build)

    def get_configure_args(
        self,
        build: targets.Build,
        wd: str | None = None,
    ) -> Args:
        if self.get_target_build_system(build) == "make":
            generator = "Unix Makefiles"
        else:
            generator = "Ninja"

        return {
            "-Cmetapkg_common_config.cmake": None,
            f"-G{generator}": None,
            "-DCMAKE_BUILD_TYPE": "metapkg",
            "-DCMAKE_VERBOSE_MAKEFILE": "ON",
            "-DCMAKE_POLICY_DEFAULT_CMP0144": "NEW",
        }

    def get_configure_env(
        self,
        build: targets.Build,
        wd: str | None = None,
    ) -> Args:
        env_args = dict(super().get_configure_env(build, wd))
        build.sh_append_run_time_ldflags(env_args, self)
        env_args = build.sh_append_global_flags(env_args)
        return env_args

    def get_build_command(
        self,
        build: targets.Build,
        args: Args,
        target: str = "",
    ) -> str:
        args = args | {f"-j{build.build_parallelism}": None}
        if self.get_target_build_system(build) == "ninja":
            args |= {"--verbose": None}
        else:
            args |= {"V": "100"}

        bdir = str(build.get_build_dir(self, relative_to="pkgbuild"))
        cmake_args: Args = {
            "--build": None,
            bdir: None,
        }
        if target:
            cmake_args["--target"] = target

        cmake = build.sh_get_command(
            "cmake", args=cmake_args, force_args_eq=True
        )

        cmake += " -- "

        cmake_build = build.sh_append_args(
            cmake,
            args,
            force_args_eq=True,
        )

        env = build.sh_format_command(
            "env",
            self.get_build_env(build, wd="${_wd}"),
            force_args_eq=True,
            linebreaks=False,
        )

        return textwrap.dedent(
            f"""\
            _wd=$(pwd -P)
            {env} {cmake_build}
            """
        )

    def get_build_install_command(
        self,
        build: targets.Build,
        args: Args,
        target: str,
    ) -> str:
        bdir = str(build.get_build_dir(self, relative_to="pkgbuild"))
        cmake_args: Args = {
            "--install": None,
            bdir: None,
            "--verbose": None,
        } | dict(args)

        cmake = build.sh_get_command(
            "cmake", args=cmake_args, force_args_eq=True
        )

        wd = "${_wd}"
        env_args = self.get_build_install_env(build, wd=wd)
        build.sh_append_quoted_flags(
            env_args,
            "DESTDIR",
            [self.sh_get_make_install_destdir(build, wd=wd)],
        )

        env = build.sh_format_args(
            env_args,
            force_args_eq=True,
            linebreaks=False,
        )

        return textwrap.dedent(
            f"""\
            _wd=$(pwd -P)
            {env} {cmake}
            """
        )

    def configure_dependency(
        self,
        build: targets.Build,
        dep: BasePackage,
        conf_args: Args,
        conf_env: Args,
        wd: str | None = None,
    ) -> None:
        super().configure_dependency(build, dep, conf_args, conf_env, wd=wd)

        if build.is_bundled(dep):
            var_prefix = self.get_dep_pkg_config_meta(dep).pkg_name
            rel_path = build.sh_get_bundled_install_path(dep, wd=wd)
            conf_args[f"-D{var_prefix}_ROOT"] = f"!{rel_path}"


_semver_phase_spelling_map = {
    poetry_pep440_segments.RELEASE_PHASE_ID_ALPHA: "alpha",
    poetry_pep440_segments.RELEASE_PHASE_ID_BETA: "beta",
}


def semver_pre_tag(version: poetry_pep440.PEP440Version) -> str:
    pre = version.pre
    if pre is not None:
        return _semver_phase_spelling_map.get(pre.phase, pre.phase)
    else:
        return ""


def pep440_to_semver(ver: poetry_version.Version) -> str:
    version_string = ver.release.to_string()

    pre = []

    if ver.pre:
        pre.append(f"{semver_pre_tag(ver)}.{ver.pre.number}")

    if ver.post:
        pre.append(f"{ver.post.phase}.{ver.post.number}")

    if ver.dev:
        pre.append(f"{ver.dev.phase}.{ver.dev.number}")

    if pre:
        version_string = f"{version_string}-{'.'.join(pre)}"

    if ver.local:
        assert isinstance(ver.local, tuple)
        version_string += "+" + ".".join(map(str, ver.local))

    return version_string.lower()
