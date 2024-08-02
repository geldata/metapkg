from __future__ import annotations
from typing import (
    TYPE_CHECKING,
    Iterator,
)

import contextlib
import copy
import itertools
import pathlib

import packaging.utils

from poetry.repositories import exceptions as poetry_repo_exc
from poetry.repositories import repository_pool as poetry_pool
from poetry.repositories import repository as poetry_repo
from poetry.core.packages import dependency as poetry_dep
from poetry.core.packages import dependency_group as poetry_depgroup
from poetry.core.packages import vcs_dependency as poetry_vcsdep
from poetry.core.constraints import version as poetry_version
from poetry.packages import dependency_package as poetry_deppkg
from poetry.core.packages import package as poetry_pkg
from poetry.mixology import incompatibility as poetry_incompat
from poetry.mixology import version_solver as poetry_versolver
from poetry.puzzle import provider as poetry_provider
from poetry.vcs import git as poetry_git

from . import sources as mpkg_sources

if TYPE_CHECKING:
    from cleo.io import io as cleo_io


def _DependencyCache_search_for(
    self: poetry_versolver.DependencyCache,
    dependency: poetry_dep.Dependency,
) -> list[poetry_deppkg.DependencyPackage]:
    key = (
        dependency.complete_name,
        dependency.pretty_constraint,
        dependency.source_type,
        dependency.source_url,
        dependency.source_reference,
        dependency.source_subdirectory,
    )

    packages = self.cache.get(key)  # type: ignore
    if packages is None:
        packages = self.provider.search_for(dependency)
    else:
        packages = [
            p
            for p in packages
            if dependency.constraint.allows(p.package.version)
        ]

    self.cache[key] = packages  # type: ignore

    return packages


poetry_versolver.DependencyCache._search_for = _DependencyCache_search_for  # type: ignore


class Pool(poetry_pool.RepositoryPool):
    pass
    # def _package(
    #     self,
    #     name: str,
    #     version: poetry_version.Version,
    #     extras: list[str] | None = None,
    #     repository_name: str | None = None,
    # ) -> poetry_pkg.Package:
    #     for repo in self.repositories:
    #         try:
    #             package = repo.package(name, version, extras=extras)
    #         except poetry_repo_exc.PackageNotFound:
    #             continue
    #         if package:
    #             self._packages.append(package)

    #             return package

    #     raise poetry_repo_exc.PackageNotFound(
    #         f"Package {name} ({version}) not found."
    #     )

    # def find_packages(
    #     self, dependency: poetry_dep.Dependency
    # ) -> list[poetry_pkg.Package]:
    #     for repo in self.repositories:
    #         packages = repo.find_packages(dependency)
    #         if packages:
    #             return packages

    #     return []


class BundleRepository(poetry_repo.Repository):
    def add_package(self, package: poetry_pkg.Package) -> None:
        if not self.has_package(package):
            super().add_package(package)


bundle_repo = BundleRepository("bundled")


class Provider(poetry_provider.Provider):
    def __init__(
        self,
        package: poetry_pkg.Package,
        pool: poetry_pool.Pool,
        io: cleo_io.IO,
        *,
        installed: list[poetry_pkg.Package] | None = None,
        locked: list[poetry_pkg.Package] | None = None,
        include_build_reqs: bool = False,
        extras: list[str] | None = None,
    ) -> None:
        super().__init__(package, pool, io, installed=installed, locked=locked)
        self.include_build_reqs = include_build_reqs
        self._active_extras = set(extras) if extras else set()

    def _search_for_vcs(
        self,
        dependency: poetry_vcsdep.VCSDependency,
    ) -> poetry_pkg.Package:
        from . import python

        pkg = self._direct_origin.get_package_from_vcs(
            dependency.vcs,
            dependency.source,
            branch=dependency.branch,
            tag=dependency.tag,
            rev=dependency.rev,
            subdirectory=dependency.source_subdirectory,
            source_root=(
                self._source_root
                or (self._env.path.joinpath("src") if self._env else None)
            ),
        )

        pkg.develop = dependency.develop

        package = python.PythonPackage(
            f"pypkg-{pkg.name}",
            version=pkg.version,
            pretty_version=pkg.pretty_version,
        )
        package.__dict__.update(
            {
                k: copy.deepcopy(v)
                for k, v in pkg.__dict__.items()
                if k not in {"_name", "_pretty_name"}
            }
        )

        for dep in package.all_requires:
            dep._name = packaging.utils.canonicalize_name(f"pypkg-{dep.name}")
            dep._pretty_name = f"pypkg-{dep.pretty_name}"

        source = poetry_git.Git.clone(
            url=dependency.source,
            source_root=(
                self._source_root
                or (self._env.path.joinpath("src") if self._env else None)
            ),
            branch=dependency.branch,
            tag=dependency.tag,
            revision=dependency.rev,
        )
        path = pathlib.Path(source.path)
        if dependency.source_subdirectory:
            path = path.joinpath(dependency.source_subdirectory)

        breqs = python.get_build_requires_from_srcdir(package, path)
        set_build_requirements(package, breqs)

        package.source = mpkg_sources.source_for_url(f"file://{path}")

        return package

    def incompatibilities_for(
        self,
        package: poetry_deppkg.DependencyPackage,
    ) -> list[poetry_incompat.Incompatibility]:
        if self.include_build_reqs:
            breqs = get_build_requirements(package.package)
            with extra_requirements(package.package, breqs):
                return super().incompatibilities_for(package)
        else:
            return super().incompatibilities_for(package)

    def complete_package(
        self,
        package: poetry_deppkg.DependencyPackage,
    ) -> poetry_deppkg.DependencyPackage:
        chain = [package.package.all_requires]
        build_requires = get_build_requirements(package.package)
        if build_requires:
            chain.append(build_requires)

        pkg = super().complete_package(package)

        for dep in itertools.chain.from_iterable(chain):
            dep_in_extras = {str(e) for e in dep.in_extras}
            if dep_in_extras and not (dep_in_extras - self._active_extras):
                dep.activate()
                pkg.package.add_dependency(dep)

        return pkg


@contextlib.contextmanager
def extra_requirements(
    pkg: poetry_pkg.Package, reqs: list[poetry_dep.Dependency]
) -> Iterator[None]:
    if not pkg.has_dependency_group(poetry_depgroup.MAIN_GROUP):
        dep_group = poetry_depgroup.DependencyGroup(poetry_depgroup.MAIN_GROUP)
        pkg.add_dependency_group(dep_group)
        orig_reqs = []
    else:
        dep_group = pkg.dependency_group(poetry_depgroup.MAIN_GROUP)
        orig_reqs = list(dep_group.dependencies)

    orig_req_names = {d.name for d in orig_reqs}

    all_reqs = orig_reqs + [d for d in reqs if d.name not in orig_req_names]

    try:
        for dep in all_reqs:
            if dep.is_activated():
                dep_group.add_dependency(dep)

        yield
    finally:
        dep_group._dependencies = orig_reqs


def set_build_requirements(
    pkg: poetry_pkg.Package, reqs: list[poetry_dep.Dependency]
) -> None:
    setattr(pkg, "build_requires", list(reqs))


def get_build_requirements(
    pkg: poetry_pkg.Package,
) -> list[poetry_dep.Dependency]:
    return getattr(pkg, "build_requires", [])
