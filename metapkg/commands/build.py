from __future__ import annotations
from typing import cast

import collections
import datetime
import graphlib
import importlib
import os
import pathlib
import sys
import tempfile

from cleo.helpers import argument, option

from poetry import puzzle
from poetry.core.packages import dependency as poetry_dep
from poetry.core.packages import project_package
from poetry.utils import env as poetry_env

from metapkg import targets
from metapkg.packages import base as mpkg_base
from metapkg.packages import python as af_python
from metapkg.packages import repository as af_repo

from . import base


class Build(base.Command):
    name = "build"
    description = """Builds the specified package on the current platform."""
    arguments = [
        argument(
            "name",
            description="Package to build",
        ),
    ]
    options = [
        option(
            "jobs",
            description="Use up to N processes in parallel to build.",
            flag=False,
        ),
        option(
            "dest",
            description="Destination path.",
            flag=False,
        ),
        option(
            "keepwork",
            description="Do not remove the work directory upon exit.",
            flag=True,
        ),
        option(
            "generic",
            description="Build a generic artifact.",
            flag=True,
        ),
        option(
            "arch",
            description="Target architecture, if different from host.",
            flag=False,
        ),
        option(
            "libc",
            description="Libc to target.",
            flag=False,
        ),
        option(
            "build-source",
            description="Build source packages.",
            flag=True,
        ),
        option(
            "build-debug",
            description="Build debug packages.",
            flag=True,
        ),
        option(
            "release",
            description="Whether this build is a release.",
            flag=True,
        ),
        option(
            "source-ref",
            description="Source version to build (VCS ref or tarball version).",
            flag=False,
        ),
        option(
            "pkg-revision",
            description="Override package revision number (defaults to 1).",
            flag=False,
        ),
        option(
            "pkg-subdist",
            description="Set package sub-distribution (e.g. nightly).",
            flag=False,
        ),
        option(
            "pkg-tags",
            description=(
                "Comma-separated list of key=value pairs to include "
                "in package metadata."
            ),
            flag=False,
        ),
        option(
            "pkg-compression",
            description=(
                "Comma-separated list of compression encodings to apply "
                "if building for the specified target produces a tarball "
                "(defaults to gzip,zstd)."
            ),
            flag=False,
        ),
        option(
            "extra-optimizations",
            description="Enable extra optimization (increases build times).",
            flag=True,
        ),
    ]

    _loggers = ["metapkg.build"]

    def handle(self) -> int:
        pkgname = self.argument("name")
        keepwork = self.option("keepwork")
        destination = self.option("dest")
        generic = self.option("generic")
        arch = self.option("arch")
        libc = self.option("libc")
        build_source = self.option("build-source")
        build_debug = self.option("build-debug")
        version = self.option("source-ref")
        revision = self.option("pkg-revision")
        subdist = self.option("pkg-subdist")
        is_release = self.option("release")
        extra_opt = self.option("extra-optimizations")
        jobs = self.option("jobs")
        tags_string = self.option("pkg-tags")
        compression_string = self.option("pkg-compression")

        # Older yum, fakeroot and possibly other tools
        # customarily attempt to iterate over _all_
        # file descriptors up to the limit and close them
        # when forking subprocesses, which in the case of
        # a high limit may be prohibitively expensive,
        # so clamp RLIMIT_NOFILE to a lower value.
        self._clamp_rlimit_nofile()

        target = targets.detect_target(
            self.io, portable=generic, libc=libc, arch=arch
        )
        target.prepare()

        tags = {}
        if tags_string:
            for pair in tags_string.split(","):
                k, _, v = pair.strip().partition("=")
                tags[k.strip()] = v.strip()

        compression = []
        if compression_string:
            compression = compression_string.split(",")

        modname, _, clsname = pkgname.rpartition(":")

        mod = importlib.import_module(modname)
        pkgcls = getattr(mod, clsname)
        assert issubclass(pkgcls, mpkg_base.BundledPackage)
        root_pkg = pkgcls.resolve(
            self.io,
            version=version,
            revision=revision,
            is_release=is_release,
            target=target,
        )
        if tags:
            root_pkg.set_metadata_tags(tags)

        env = poetry_env.SystemEnv(pathlib.Path(sys.executable))
        packages, build_pkgs = self._resolve_deps(env, target, root_pkg, [])

        # Build dependency resolution could have changed the
        # installable dependency list, so we might need to re-run
        # the non-build-deps resolution.
        reresolve_deps = self._check_dep_consistency(packages, build_pkgs)
        if reresolve_deps:
            packages, build_pkgs = self._resolve_deps(
                env, target, root_pkg, reresolve_deps
            )

        # Check again
        reresolve_deps = self._check_dep_consistency(packages, build_pkgs)
        if reresolve_deps:
            self.io.write_error_line(
                "Unresolveable install-time vs build-time dependency graph. "
                + "Mismatching dependencies: "
                + ", ".join(dep.to_pep_508() for dep in reresolve_deps)
            )
            return 1

        if keepwork:
            workdir = tempfile.mkdtemp(prefix="metapkg.")
        else:
            tempdir = tempfile.TemporaryDirectory(prefix="metapkg.")
            workdir = tempdir.name

        os.chmod(workdir, 0o755)

        extra = {}
        if compression:
            extra["compression"] = compression

        try:
            target.build(
                targets.BuildRequest(
                    io=self.io,
                    env=env,
                    root_pkg=root_pkg,
                    deps=packages,
                    build_deps=build_pkgs,
                    workdir=workdir,
                    outputdir=destination,
                    build_source=build_source,
                    build_debug=build_debug,
                    build_date=datetime.datetime.now(tz=datetime.timezone.utc),
                    revision=revision or "1",
                    subdist=subdist,
                    extra_opt=extra_opt,
                    jobs=jobs or 0,
                    **extra,
                ),
            )
        finally:
            if not keepwork:
                tempdir.cleanup()

        return 0

    def _resolve_deps(
        self,
        env: poetry_env.Env,
        target: targets.Target,
        root_pkg: mpkg_base.BundledPackage,
        extra_deps: list[poetry_dep.Dependency] | None = None,
    ) -> tuple[list[mpkg_base.BasePackage], list[mpkg_base.BasePackage]]:
        root = project_package.ProjectPackage("__root__", "1")
        root.python_versions = af_python.python_dependency.pretty_constraint
        root.add_dependency(
            poetry_dep.Dependency(root_pkg.name, root_pkg.version)
        )
        if extra_deps is not None:
            for extra_dep in extra_deps:
                root.add_dependency(extra_dep)
        af_repo.bundle_repo.add_package(root)

        target_capabilities = target.get_capabilities()
        extras = [f"capability-{c}" for c in target_capabilities] + list(
            root_pkg.features
        )

        repo_pool = af_repo.Pool()
        repo_pool.add_repository(target.get_package_repository())
        repo_pool.add_repository(af_repo.bundle_repo, secondary=True)

        item_repo = root_pkg.get_package_repository(target, io=self.io)
        if item_repo is not None and item_repo is not af_repo.bundle_repo:
            repo_pool.add_repository(item_repo, secondary=True)

        provider = af_repo.Provider(root, repo_pool, self.io, extras=extras)
        solver = puzzle.Solver(root, repo_pool, [], [], self.io)
        solver._provider = provider
        resolution = solver._solve()

        pkg_map: dict[mpkg_base.NormalizedName, mpkg_base.BasePackage] = {}
        graph = {}
        for dep_package in resolution[0]:
            pkg_map[dep_package.name] = cast(
                mpkg_base.BasePackage, dep_package
            )
            deps = {
                req.name
                for req in dep_package.requires
                if env.is_valid_for_marker(req.marker)
            }
            graph[dep_package.name] = deps
        sorter = graphlib.TopologicalSorter(graph)
        packages = [pkg_map[pn] for pn in sorter.static_order()]

        af_repo.bundle_repo.remove_package(root)

        # Build a separate package list for build deps.
        build_root = project_package.ProjectPackage("__build_root__", "1")
        build_root.python_versions = (
            af_python.python_dependency.pretty_constraint
        )
        build_root.add_dependency(
            poetry_dep.Dependency(root_pkg.name, root_pkg.version)
        )
        provider = af_repo.Provider(
            build_root,
            repo_pool,
            self.io,
            include_build_reqs=True,
            extras=extras,
        )
        solver = puzzle.Solver(build_root, repo_pool, [], [], self.io)
        solver._provider = provider
        mpkg_base.all_requires_include_build_reqs = True
        try:
            resolution = solver._solve()
        finally:
            mpkg_base.all_requires_include_build_reqs = False

        pkg_map = {}
        graph = {}
        for dep_package in resolution[0]:
            pkg_map[dep_package.name] = cast(
                mpkg_base.BasePackage, dep_package
            )
            breqs = mpkg_base.get_build_requirements(dep_package)
            deps = {
                req.name
                for req in set(dep_package.requires) | set(breqs)
                if (
                    req.is_activated()
                    and env.is_valid_for_marker(req.marker)
                    # Poetry inserts package dependency on itself
                    # for dependencies with extras.
                    and req.name != dep_package.name
                )
            }
            graph[dep_package.name] = deps

        # Workaround cycles in build/runtime dependencies between
        # packages.  This requires the depending package to explicitly
        # declare its cyclic runtime dependencies in get_cyclic_runtime_deps()
        # and then the cyclic dependency must take care to inject itself
        # into the dependent's context to build itself (e.g. by manipulating
        # PYTHONPATH at build time.)  An example of such cycle is
        # flit-core -> tomli -> flit-core.
        cyclic_runtime_deps = collections.defaultdict(list)
        last_cycle = None
        current_cycle = None
        while True:
            sorter = graphlib.TopologicalSorter(graph)

            try:
                build_pkgs = [pkg_map[pn] for pn in sorter.static_order()]
            except graphlib.CycleError as e:
                cycle = e.args[1]
                if len(cycle) > 3 or cycle == last_cycle:
                    raise

                dep = pkg_map[cycle[-1]]
                pkg_with_dep = pkg_map[cycle[-2]]
                assert isinstance(pkg_with_dep, af_python.PythonPackage)
                if dep.name not in pkg_with_dep.get_cyclic_runtime_deps():
                    dep, pkg_with_dep = pkg_with_dep, dep
                    if dep.name not in pkg_with_dep.get_cyclic_runtime_deps():
                        raise

                last_cycle = current_cycle
                current_cycle = cycle
                cyclic_runtime_deps[pkg_with_dep].append(dep)
                graph[pkg_with_dep.name].remove(dep.name)
            else:
                break

        for pkg_with_cr_deps, cr_deps in cyclic_runtime_deps.items():
            for i, build_pkg in enumerate(build_pkgs):
                if build_pkg == pkg_with_cr_deps:
                    build_pkgs[i + 1 : i + 1] = cr_deps
                    break

        return packages, build_pkgs

    def _check_dep_consistency(
        self,
        packages: list[mpkg_base.BasePackage],
        build_pkgs: list[mpkg_base.BasePackage],
    ) -> list[poetry_dep.Dependency]:
        build_dep_index = {pkg.name: pkg for pkg in build_pkgs}
        reresolve_deps = []
        for pkg in packages:
            build_dep = build_dep_index.get(pkg.name)
            if build_dep is not None and build_dep.version != pkg.version:
                reresolve_deps.append(
                    poetry_dep.Dependency(build_dep.name, build_dep.version)
                )

        return reresolve_deps

    def _clamp_rlimit_nofile(self) -> None:
        try:
            import resource
        except ImportError:
            pass
        else:
            try:
                fno_soft, fno_hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            except resource.error:
                self.io.write_error_line("could not read RLIMIT_NOFILE")
            else:
                if fno_soft > 8192 or fno_hard > 8192:
                    try:
                        resource.setrlimit(
                            resource.RLIMIT_NOFILE,
                            (min(8192, fno_soft), min(8192, fno_hard)),
                        )
                    except resource.error:
                        self.io.write_error_line(
                            "could not read RLIMIT_NOFILE"
                        )
