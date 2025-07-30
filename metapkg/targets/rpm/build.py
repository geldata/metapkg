from __future__ import annotations

import datetime
import glob
import json
import os
import pathlib
import platform
import re
import shlex
import shutil
import stat
import subprocess
import textwrap

from metapkg import packages as mpkg
from metapkg import targets
from metapkg import tools


class Build(targets.Build):
    _target: targets.LinuxDistroTarget

    def prepare(self) -> None:
        super().prepare()

        self._pkgroot = self._droot / self._root_pkg.name_slot
        self._srcroot = self._pkgroot / self._root_pkg.name_slot

        self._sourceroot = pathlib.Path("BUILD")
        self._buildroot = pathlib.Path("BUILD2")
        self._tmproot = pathlib.Path("TEMP")
        self._installroot = pathlib.Path("INSTALL")
        self._bin_shims = self._root_pkg.get_bin_shims(self)

    def get_source_abspath(self) -> pathlib.Path:
        return self._srcroot

    def get_path(
        self,
        path: str | pathlib.Path,
        *,
        relative_to: targets.Location,
        package: mpkg.BasePackage | None = None,
    ) -> pathlib.Path:
        """Return *path* relative to *relative_to* location.

        :param pathlike path:
            A path relative to bundle source root.

        :param str relative_to:
            Location name.  Can be one of:
              - ``'sourceroot'``: bundle source root
              - ``'pkgsource'``: package source directory
              - ``'pkgbuild'``: package build directory
              - ``'helpers'``: build helpers directory
              - ``'fsroot'``: filesystem root (makes path absolute)

        :return:
            Path relative to the specified location.
        """

        if relative_to == "sourceroot":
            return pathlib.Path(path)
        elif relative_to == "buildroot":
            return pathlib.Path("..") / path
        elif relative_to == "pkgsource":
            return pathlib.Path("..") / ".." / path
        elif relative_to == "pkgbuild":
            return pathlib.Path("..") / ".." / path
        elif relative_to == "helpers":
            return pathlib.Path("..") / ".." / path
        elif relative_to == "fsroot":
            return (self.get_source_abspath() / path).resolve()
        else:
            raise ValueError(f"invalid relative_to argument: {relative_to}")

    def get_helpers_root(
        self, *, relative_to: targets.Location = "sourceroot"
    ) -> pathlib.Path:
        return self.get_dir(
            self.get_tarball_root() / "helpers", relative_to=relative_to
        )

    def get_source_root(
        self, *, relative_to: targets.Location = "sourceroot"
    ) -> pathlib.Path:
        return self.get_dir(pathlib.Path("."), relative_to=relative_to)

    def get_tarball_root(
        self, *, relative_to: targets.Location = "sourceroot"
    ) -> pathlib.Path:
        return self.get_dir(pathlib.Path("SOURCES"), relative_to=relative_to)

    def get_patches_root(
        self, *, relative_to: targets.Location = "sourceroot"
    ) -> pathlib.Path:
        return self.get_tarball_root(relative_to=relative_to)

    def get_extras_root(
        self, *, relative_to: targets.Location = "sourceroot"
    ) -> pathlib.Path:
        return self.get_tarball_root(relative_to=relative_to) / "extras"

    def get_spec_root(
        self, *, relative_to: targets.Location = "sourceroot"
    ) -> pathlib.Path:
        return self.get_dir(pathlib.Path("SPECS"), relative_to=relative_to)

    def get_source_dir(
        self,
        package: mpkg.BasePackage,
        *,
        relative_to: targets.Location = "sourceroot",
        relative_to_package: mpkg.BasePackage | None = None,
    ) -> pathlib.Path:
        return self.get_dir(
            self._sourceroot / package.name,
            relative_to=relative_to,
            relative_to_package=relative_to_package or package,
        )

    def get_temp_dir(
        self,
        package: mpkg.BasePackage,
        *,
        relative_to: targets.Location = "sourceroot",
        relative_to_package: mpkg.BasePackage | None = None,
    ) -> pathlib.Path:
        return self.get_dir(
            self._tmproot / package.name,
            relative_to=relative_to,
            relative_to_package=relative_to_package or package,
        )

    def get_temp_root(
        self, *, relative_to: targets.Location = "sourceroot"
    ) -> pathlib.Path:
        return self.get_dir(self._tmproot, relative_to=relative_to)

    def get_image_root(
        self, *, relative_to: targets.Location = "sourceroot"
    ) -> pathlib.Path:
        return self.get_dir(
            pathlib.Path("BUILDROOT") / self._root_pkg.name_slot,
            relative_to=relative_to,
        )

    def get_build_dir(
        self,
        package: mpkg.BasePackage,
        *,
        relative_to: targets.Location = "sourceroot",
        relative_to_package: mpkg.BasePackage | None = None,
    ) -> pathlib.Path:
        return self.get_dir(
            self._buildroot / package.name,
            relative_to=relative_to,
            relative_to_package=relative_to_package or package,
        )

    def get_build_install_dir(
        self,
        package: mpkg.BasePackage,
        *,
        relative_to: targets.Location = "sourceroot",
        relative_to_package: mpkg.BasePackage | None = None,
    ) -> pathlib.Path:
        return self.get_dir(
            self._installroot / package.name,
            relative_to=relative_to,
            relative_to_package=relative_to_package,
        )

    def _get_tarball_tpl(self, package: mpkg.BasePackage) -> str:
        rp = self._root_pkg
        return f"{rp.name}_{rp.version.text}.orig-{package.name}{{part}}.tar{{comp}}"

    def build(self) -> None:
        self.prepare_tools()
        self.prepare_tarballs()
        self.unpack_sources()
        self.prepare_patches()
        self._write_spec()
        self._rpmbuild()

    def _format_version(self) -> str:
        return mpkg.pep440_to_semver(self._root_pkg.version).replace("-", "_")

    def _write_spec(self) -> None:
        sysreqs = self.get_extra_system_requirements()
        base_name = self._root_pkg.name
        name = self._root_pkg.name_slot

        if self._bin_shims:
            common_package = textwrap.dedent(
                """\
                %package -n {name}-common
                Summary: Support files for {title}.
                Group: {group}
                License: {license}
                URL: {url}

                %description -n {name}-common
                {long_description}

                %files -n {name}-common
                {common_files}
            """
            ).format(
                name=base_name,
                title=self._root_pkg.title,
                long_description=self._root_pkg.description,
                license=self._root_pkg.license,
                url=self._root_pkg.url,
                group=self._root_pkg.group,
                common_files=self._get_common_files(),
            )
        else:
            common_package = ""

        rev = f'{self._revision}{self._subdist if self._subdist else ""}'
        root_v = self._format_version()
        root_version = f"{root_v}-{rev}%{{?dist}}"
        meta_pkgs = self._root_pkg.get_meta_packages(self, root_version)
        meta_pkg_specs = []
        for meta_pkg in meta_pkgs:
            meta_pkg_spec = textwrap.dedent(
                """\
                %package -n {name}
                Summary: {description}
                Group: {group}
                License: {license}
                URL: {url}
                {dependencies}

                %description -n {name}
                {description}

                %files -n {name}
            """
            ).format(
                name=meta_pkg.name,
                description=meta_pkg.description,
                license=self._root_pkg.license,
                url=self._root_pkg.url,
                group=self._root_pkg.group,
                dependencies="\n".join(
                    f'Requires: {d_name}{" " if d_ver else ""}{d_ver or ""}'
                    for d_name, d_ver in meta_pkg.dependencies.items()
                ),
            )
            meta_pkg_specs.append(meta_pkg_spec)

        transitions = self._root_pkg.get_transition_packages(self)
        for transition in transitions:
            description = (
                f"transitional package, can be safely removed, use "
                f"{name} instead"
            )
            meta_pkg_spec = textwrap.dedent(
                """\
                %package -n {name}
                Summary: {description}
                Group: {group}
                License: {license}
                URL: {url}
                {dependencies}

                %description -n {name}
                {description}

                %files -n {name}
            """
            ).format(
                name=transition,
                license=self._root_pkg.license,
                url=self._root_pkg.url,
                group=self._root_pkg.group,
                version=self._format_version(),
                description=description,
                dependencies=f"Requires: {name} = {root_version}",
            )
            meta_pkg_specs.append(meta_pkg_spec)

        conflicts = self._root_pkg.get_conflict_packages(self, root_version)
        provides = self._root_pkg.get_provided_packages(self, root_version)

        requires_exclude = [
            f"{re.escape(str(self.get_bundle_install_path('lib')))}/.*",
            f"{re.escape(str(self.get_bundle_install_path('bin')))}/.*",
        ]
        privatelibs = self._get_private_libs_pattern()
        if privatelibs:
            requires_exclude.append(privatelibs)

        rules = textwrap.dedent(
            """\
            Name: {name}
            Version: {version}
            Release: {revision}{subdist}%{{?dist}}
            Summary: {description}
            License: {license}
            URL: {url}
            Group: {group}

            BuildRequires: bash
            {build_reqs}
            {runtime_reqs}
            {conflicts}
            {provides}

            {source_spec}
            {patch_spec}

            %description
            {long_description}

            {common_pkg}

            {meta_pkgs}

            %global __provides_exclude ^.*\\.so(\\..*)?$
            %global __requires_exclude {requires_exclude}

            %define __python python3
            %define __brp_mangle_shebangs %{{nil}}

            {debug_pkg}

            %prep
            {unpack_script}
            {patch_script}

            %build
            {build_script}

            %install
            {install_script}
            {install_extras}

            %pre
            {pre_script}

            %post
            {post_script}

            %files -f {temp_root}/install.list
            {files_extras}

            %changelog
            {changelog}
        """
        ).format(
            name=self._root_pkg.name_slot,
            revision=self._revision,
            subdist=self._subdist if self._subdist else "",
            description=self._root_pkg.description,
            long_description=self._root_pkg.description,
            license=self._root_pkg.license,
            url=self._root_pkg.url,
            group=self._root_pkg.group,
            version=self._format_version(),
            build_reqs=self._get_build_reqs_spec(),
            runtime_reqs=self._get_runtime_reqs_spec(sysreqs),
            conflicts=self._get_conflict_spec(conflicts),
            provides=self._get_provides_spec(provides),
            source_spec=self._get_source_spec(),
            patch_spec=self._get_patch_spec(),
            patch_script=self._get_patch_script(),
            unpack_script=self._write_script(
                "unpack", relative_to="buildroot"
            ),
            build_script=self._write_script(
                "complete", relative_to="buildroot"
            ),
            install_script=self._write_script(
                "install", installable_only=True, relative_to="buildroot"
            ),
            install_extras=textwrap.indent(self._get_install_extras(), "\t"),
            files_extras=self._get_files_extras(),
            pre_script=self.get_script(
                "before_install",
                installable_only=True,
                relative_to="buildroot",
            ),
            post_script=self.get_script(
                "after_install", installable_only=True, relative_to="buildroot"
            ),
            temp_root=self.get_temp_root(relative_to="buildroot"),
            requires_exclude="^(" + ")|(".join(requires_exclude) + ")$",
            changelog=self._get_changelog(),
            common_pkg=common_package,
            meta_pkgs="\n\n".join(meta_pkg_specs),
            debug_pkg="%debug_package" if self._build_debug else "",
        )

        spec_root = self.get_spec_root(relative_to="fsroot")
        with open(spec_root / f"{self._root_pkg.name_slot}.spec", "w") as f:
            f.write(rules)

    def _get_changelog(self) -> str:
        root_v = self._format_version()
        changelog = textwrap.dedent(
            """\
            * {date} {maintainer} {version}
            - {metadata}
        """
        ).format(
            maintainer="MagicStack Inc. <hello@magic.io>",
            version=f"{root_v}-{self._revision}",
            date=datetime.datetime.now(datetime.timezone.utc).strftime(
                "%a %b %d %Y"
            ),
            metadata=json.dumps(self._root_pkg.get_artifact_metadata(self)),
        )

        return changelog

    def _get_private_libs_pattern(self) -> str:
        private_libs = set()

        for pkg in self._installable:
            private_libs.update(pkg.get_private_libraries(self))

        return "|".join(private_libs)

    def _get_build_reqs_spec(self) -> str:
        lines = []

        deps = (
            pkg
            for pkg in self._build_deps
            if isinstance(pkg, targets.SystemPackage)
        )
        for pkg in deps:
            lines.append(f"BuildRequires: {pkg.system_name}")

        return "\n".join(lines)

    def _get_runtime_reqs_spec(self, extrareqs: dict[str, set[str]]) -> str:
        lines = []

        deps = (
            pkg for pkg in self._deps if isinstance(pkg, targets.SystemPackage)
        )
        for pkg in deps:
            lines.append(f"Requires: {pkg.system_name}")

        if self._bin_shims:
            root_v = self._format_version()
            lines.append(f"Requires: {self._root_pkg.name}-common >= {root_v}")

        categorymap = {
            "before-install": "pre",
            "after-install": "post",
            "before-uninstall": "preun",
            "after-uninstall": "postun",
        }

        for cat, reqs in extrareqs.items():
            cat = categorymap[cat]
            lines.append(f'Requires({cat}): {" ".join(reqs)}')

        return "\n".join(lines)

    def _get_conflict_spec(self, conflicts: list[str]) -> str:
        lines = []

        for conflict in conflicts:
            lines.append(f"Conflicts: {conflict}")

        return "\n".join(lines)

    def _get_provides_spec(self, provides: list[tuple[str, str]]) -> str:
        lines = []

        for pkg, ver in provides:
            lines.append(f"Provides: {pkg} = {ver}")

        return "\n".join(lines)

    def _get_source_spec(self) -> str:
        lines = []

        i = 0
        for _, tarballs in enumerate(self._tarballs.values()):
            for _, tarball in tarballs:
                lines.append(f"Source{i}: {tarball.name}")
                i += 1

        return "\n".join(lines)

    def _get_patch_spec(self) -> str:
        lines = []

        for i, (_, patch) in enumerate(self._patches):
            lines.append(f"Patch{i}: {patch}")

        return "\n".join(lines)

    def _get_patch_script(self) -> str:
        lines = []

        for i, _patch in enumerate(self._patches):
            lines.append(f"%patch -P {i} -p1")

        return "\n".join(lines)

    def _get_package_unpack_script(self, pkg: mpkg.BasePackage) -> str:
        tarball_root = self.get_tarball_root(relative_to="pkgbuild")
        tarballs = self._tarballs.get(pkg, [])
        script = []

        for src, tarball_path in tarballs:
            tarball = tarball_root / tarball_path
            ext = tarball.suffix
            if ext == ".bz2":
                compflag = "j"
            elif ext == ".gz":
                compflag = "z"
            elif ext == ".xz":
                compflag = "J"
            elif ext == ".tar":
                compflag = ""
            else:
                raise NotImplementedError(f"tar{ext} files are not supported")

            src_dir = self.get_source_dir(pkg, relative_to="pkgbuild")
            if src.path:
                src_dir /= src.path

            script.append(
                textwrap.dedent(
                    f"""
                pushd "{src_dir}" >/dev/null
                /usr/bin/tar -x{compflag} -f {tarball} --strip-components=1
                popd >/dev/null
            """
                )
            )

        return "\n".join(script)

    def _get_package_install_script(self, pkg: mpkg.BasePackage) -> str:
        source_root = self.get_source_root(relative_to="pkgbuild")
        install_dir = self.get_build_install_dir(pkg, relative_to="sourceroot")
        image_root = self.get_image_root(relative_to="sourceroot")
        temp_root = self.get_temp_root(relative_to="sourceroot")
        temp_dir = self.get_temp_dir(pkg, relative_to="sourceroot")

        il_script_text = self._get_package_script(pkg, "install_list")
        il_script = self.sh_write_bash_helper(
            f"_gen_install_list_{pkg.unique_name}.sh",
            il_script_text,
            relative_to="sourceroot",
        )

        nil_script_text = self._get_package_script(pkg, "no_install_list")
        nil_script = self.sh_write_bash_helper(
            f"_gen_no_install_list_{pkg.unique_name}.sh",
            nil_script_text,
            relative_to="sourceroot",
        )

        ignore_script_text = self._get_package_script(pkg, "ignore_list")
        ignore_script = self.sh_write_bash_helper(
            f"_gen_ignore_list_{pkg.unique_name}.sh",
            ignore_script_text,
            relative_to="sourceroot",
        )

        ignored_dep_text = self._get_package_script(pkg, "ignored_dependency")
        ignored_dep_script = self.sh_write_bash_helper(
            f"_gen_ignored_deps_{pkg.unique_name}.sh",
            ignored_dep_text,
            relative_to="sourceroot",
        )
        trim_install = self.sh_get_command(
            "trim-install", relative_to="sourceroot"
        )
        copy_tree = self.sh_get_command("copy-tree", relative_to="sourceroot")

        return textwrap.dedent(
            f"""
            pushd "{source_root}" >/dev/null

            {il_script} > "{temp_dir}/install"
            {nil_script} > "{temp_dir}/not-installed"
            {ignore_script} > "{temp_dir}/ignored"
            {ignored_dep_script} >> "{temp_root}/ignored-reqs"

            {trim_install} "{temp_dir}/install" \\
                "{temp_dir}/not-installed" "{temp_dir}/ignored" \\
                "{install_dir}" > "{temp_dir}/install.final"

            {copy_tree} -v "{install_dir}/" "{image_root}/"

            while IFS= read -r path; do
                if [ -d "{install_dir}/${{path}}" ]; then
                    echo %dir \\"/${{path}}\\" >> "{temp_root}/install.list"
                else
                    echo \\"/${{path}}\\" >> "{temp_root}/install.list"
                fi
            done < <(cat "{temp_dir}/install.final")

            popd >/dev/null
        """
        )

    def _get_install_extras(self) -> str:
        lines = []
        symlinks = []

        extras_dir = self.get_extras_root(relative_to="fsroot")
        extras_dir_rel = self.get_extras_root(relative_to="buildroot")

        for pkg in self._installable:
            for path, content in pkg.get_service_scripts(self).items():
                directory = extras_dir / path.parent.relative_to("/")
                directory.mkdir(parents=True)
                with open(directory / path.name, "w") as f:
                    print(content, file=f)

            for cmd in pkg.get_exposed_commands(self):
                symlinks.append((cmd, f"{cmd.name}{pkg.slot_suffix}"))

        if symlinks:
            lines.append(r'install -m755 -d "${RPM_BUILD_ROOT}/%{_bindir}"')

            for src, tgt in symlinks:
                lines.append(
                    f'ln -sf "{src}" '
                    f'"${{RPM_BUILD_ROOT}}/%{{_bindir}}/{tgt}"'
                )

        if self._bin_shims:
            sysbindir = self.get_bundle_install_path("systembin")

            for shim_path, data in self._bin_shims.items():
                relpath = (sysbindir / shim_path).relative_to("/")
                inst_path = extras_dir / relpath
                inst_path.parent.mkdir(parents=True, exist_ok=True)
                with open(inst_path, "w") as f:
                    f.write(data)
                os.chmod(
                    inst_path,
                    stat.S_IRWXU
                    | stat.S_IRGRP
                    | stat.S_IXGRP
                    | stat.S_IROTH
                    | stat.S_IXOTH,
                )

                src_path = extras_dir_rel / relpath
                broot_path = f"%{{_bindir}}/{shim_path}"

                lines.append(
                    f'mkdir -p "$(dirname ${{RPM_BUILD_ROOT}}/{broot_path})"'
                )
                lines.append(
                    f"cp -p {shlex.quote(str(src_path))}"
                    f' "${{RPM_BUILD_ROOT}}/{broot_path}"'
                )

        return "\n".join(lines)

    def _get_files_extras(self) -> str:
        lines = []

        for pkg in self._installable:
            for cmd in pkg.get_exposed_commands(self):
                cmdname = f"{cmd.name}{pkg.slot_suffix}"
                lines.append(f"%{{_bindir}}/{cmdname}")

        return "\n".join(lines)

    def _get_common_files(self) -> str:
        if self._bin_shims:
            return "\n".join(
                f"%{{_bindir}}/{path}" for path in self._bin_shims
            )
        else:
            return ""

    def _rpmbuild(self) -> None:
        self.target.install_build_deps(  # type: ignore
            self, f"{self._root_pkg.name_slot}.spec"
        )

        image_root = self.get_image_root(relative_to="fsroot")

        args = [
            f"{self._root_pkg.name_slot}.spec",
            f"--define=%_topdir {self._srcroot}",
            f"--buildroot={image_root}",
            "--verbose",
        ]
        if self._build_source:
            args.append("-ba")
        else:
            args.append("-bb")

        tools.cmd(
            "rpmbuild",
            *args,
            cwd=str(self.get_spec_root(relative_to="fsroot")),
            stdout=self.stream,
            stderr=subprocess.STDOUT,
        )

        tools.cmd(
            "rpmlint",
            "-i",
            f"{self._root_pkg.name_slot}.spec",
            cwd=str(self.get_spec_root(relative_to="fsroot")),
            stdout=self.stream,
            stderr=subprocess.STDOUT,
        )

    def package(self) -> None:
        archives = self.get_intermediate_output_dir(relative_to="fsroot")

        contents = {}

        rpms = self.get_dir("RPMS", relative_to="fsroot") / platform.machine()
        for rpm_path in glob.glob(str(rpms / "*.rpm")):
            rpm = pathlib.Path(rpm_path)
            shutil.copy2(rpm, archives / rpm.name)
            contents[rpm.name] = {
                "type": "application/x-rpm",
                "encoding": "identity",
                "suffix": ".rpm",
            }

        srpms = self.get_dir("SRPMS", relative_to="fsroot")
        for rpm_path in glob.glob(str(srpms / "*.rpm")):
            rpm = pathlib.Path(rpm_path)
            shutil.copy2(rpm, archives / rpm.name)
            contents[rpm.name] = {
                "type": "application/x-rpm",
                "encoding": "identity",
                "suffix": ".rpm",
            }

        distro = self._target.distro["codename"]
        rev = f'{self._revision}{self._subdist if self._subdist else ""}'
        root_v = self._format_version()
        root_version = f"{root_v}-{rev}.{distro}"
        with open(archives / "build-metadata.json", "w") as f:
            installref = f"{self._root_pkg.name_slot}-{root_version}"
            json.dump(
                {
                    "installrefs": [installref],
                    "repository": "rpm",
                    "contents": contents,
                    **self._root_pkg.get_artifact_metadata(self),
                },
                f,
            )
