from __future__ import annotations

import json
import mimetypes
import os
import pathlib
import stat
import shutil

from xml.dom import minidom

from metapkg.targets import generic
from metapkg import tools


class MacOSBuild(generic.Build):
    def define_tools(self) -> None:
        super().define_tools()
        bash = self._find_tool("bash")
        self._system_tools["bash"] = bash
        gmake = self._find_tool("gmake")
        self._system_tools["make"] = gmake
        self._system_tools["sed"] = self._find_tool("gsed")
        self._system_tools["tar"] = self._find_tool("gtar")
        self._system_tools["cmake"] = self._find_tool("cmake")
        self._system_tools["ninja"] = self._find_tool("ninja")

    def _find_tool(self, tool: str) -> str:
        tool_path = shutil.which(tool)
        if tool_path is None:
            raise RuntimeError(f"required program not found: {tool}")
        return tool_path

    def _fixup_rpath(
        self, image_root: pathlib.Path, binary_relpath: pathlib.Path
    ) -> None:
        inst_prefix = self.get_bundle_install_prefix()
        full_path = image_root / binary_relpath
        inst_path = pathlib.Path("/") / binary_relpath
        shlibs, existing_rpaths = self.target.get_shlib_refs(
            self, image_root, binary_relpath, resolve=False
        )
        rpaths = set()
        shlib_alters: set[tuple[str, str]] = set()
        if existing_rpaths:
            for rpath in existing_rpaths:
                if rpath.parts[0] != "@loader_path":
                    if rpath.is_relative_to(inst_prefix):
                        rel_rpath = pathlib.Path(
                            f"@loader_path"
                        ) / os.path.relpath(rpath, start=inst_path.parent)
                        for shlib in shlibs:
                            if shlib.is_relative_to(rpath):
                                rel_shlib = pathlib.Path(
                                    "@rpath"
                                ) / os.path.relpath(shlib, start=rpath)
                                shlib_alters.add((str(shlib), str(rel_shlib)))
                        rpath = rel_rpath
                    else:
                        print(
                            f"RPATH {rpath} points outside of install image, "
                            f"removing"
                        )
                        continue
                rpaths.add(rpath)

        args: list[str | pathlib.Path] = []
        for added in rpaths - existing_rpaths:
            args.extend(("-add_rpath", added))

        for old, new in shlib_alters:
            args.extend(("-change", old, new))

        if args:
            args.append(full_path)

            tools.cmd(
                "install_name_tool",
                *args,
            )

        for removed in existing_rpaths - rpaths:
            present_rpaths = existing_rpaths
            # Unfortunately, macOS ld creates duplicate LC_RPATH
            # entries (from duplicate -rpath command line arguments),
            # and install_name_tool only removes the _first_ matching
            # entry rather than all of them.
            while removed in present_rpaths:
                tools.cmd(
                    "install_name_tool",
                    "-delete_rpath",
                    removed,
                    full_path,
                )
                _, present_rpaths = self.target.get_shlib_refs(
                    self, image_root, binary_relpath, resolve=False
                )


class NativePackageBuild(MacOSBuild):
    def _package(self, files: list[pathlib.Path]) -> None:
        pkg = self._root_pkg
        title = pkg.name
        version = pkg.pretty_version
        ident = f"{pkg.identifier}{pkg.slot_suffix}"

        temp_root = self.get_temp_root(relative_to="fsroot")
        installer = temp_root / "installer"
        installer.mkdir(parents=True)

        srcdir = self.get_image_root(relative_to="fsroot")

        # Unversioned package.

        selectdir = installer / "Common"
        selectdir.mkdir(parents=True)

        sysbindir = self.get_bundle_install_path("systembin")

        for path, data in self._root_pkg.get_bin_shims(self).items():
            bin_path = (sysbindir / path).relative_to("/")
            inst_path = selectdir / bin_path
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

        paths_d = selectdir / "etc" / "paths.d" / self._root_pkg.identifier
        paths_d.parent.mkdir(parents=True)
        sysbindir = self.get_bundle_install_path("systembin")

        with open(paths_d, "w") as f:
            print(sysbindir, file=f)

        common_pkgname = f"{title}-common.pkg"
        common_pkgpath = installer / common_pkgname

        tools.cmd(
            "pkgbuild",
            "--root",
            selectdir,
            "--identifier",
            f"{self._root_pkg.identifier}-common",
            "--version",
            version,
            "--install-location",
            "/",
            common_pkgpath,
        )

        # Main Versioned Package
        stagemap = {
            "before_install": "preinstall",
            "after_install": "postinstall",
        }

        scriptdir = installer / "Scripts"
        scriptdir.mkdir(parents=True)

        for genstage, inststage in stagemap.items():
            script = self.get_script(genstage, installable_only=True)
            if script:
                with open(scriptdir / inststage, "w") as f:
                    print("#!/bin/bash\nset -e", file=f)
                    print(script, file=f)
                os.chmod(
                    scriptdir / inststage,
                    stat.S_IRWXU
                    | stat.S_IRGRP
                    | stat.S_IXGRP
                    | stat.S_IROTH
                    | stat.S_IXOTH,
                )

        pkgname = f"{title}{pkg.slot_suffix}.pkg"
        pkgpath = installer / pkgname

        tools.cmd(
            "pkgbuild",
            "--root",
            srcdir,
            "--identifier",
            ident,
            "--scripts",
            scriptdir,
            "--version",
            version,
            "--install-location",
            "/",
            pkgpath,
        )

        rsrcdir = installer / "Resources"
        rsrcdir.mkdir(parents=True)

        resources = self._root_pkg.get_resources(self)

        nice_title = pkg.title if pkg.title is not None else pkg.name

        for name, res_data in resources.items():
            with open(rsrcdir / name, "wb") as rf:
                res_data = res_data.replace(b"$TITLE", nice_title.encode())
                res_data = res_data.replace(b"$FULL_VERSION", version.encode())
                rf.write(res_data)

        distribution = installer / "Distribution.xml"

        tools.cmd(
            "productbuild",
            "--package",
            pkgpath,
            "--package",
            common_pkgpath,
            "--resources",
            rsrcdir,
            "--identifier",
            ident,
            "--version",
            version,
            "--synthesize",
            distribution,
        )

        dist_xml = minidom.parse(str(distribution))
        gui_xml = dist_xml.documentElement
        assert gui_xml is not None

        for name in resources:
            res_type = pathlib.Path(name).stem.lower()
            if res_type in (
                "welcome",
                "readme",
                "license",
                "conclusion",
                "background",
            ):
                mimetype = mimetypes.guess_type(name)
                element = dist_xml.createElement(res_type)
                element.setAttribute("file", name)
                if mimetype[0] is not None:
                    element.setAttribute("mime-type", mimetype[0])
                if res_type == "background":
                    element.setAttribute("alignment", "left")

                gui_xml.appendChild(element)

        title_el = dist_xml.createElement("title")
        title_text = dist_xml.createTextNode(pkg.title or "<no title>")
        title_el.appendChild(title_text)
        gui_xml.appendChild(title_el)

        if options_els := gui_xml.getElementsByTagName("options"):
            options = options_els[0]
        else:
            options = dist_xml.createElement("options")
            gui_xml.appendChild(options)

        options.setAttribute("customize", "never")
        options.setAttribute("rootVolumeOnly", "true")

        with open(distribution, "w") as f:
            f.write(dist_xml.toprettyxml())

        archives = self.get_intermediate_output_dir(relative_to="fsroot")

        suffix = self._revision
        if self._subdist:
            suffix = f"{suffix}~{self._subdist}"

        root_version = f"{pkg.slot_suffix}_{version}_{suffix}"
        finalname = f"{title}{root_version}.pkg"

        tools.cmd(
            "productbuild",
            "--package-path",
            pkgpath.parent,
            "--resources",
            rsrcdir,
            "--identifier",
            ident,
            "--version",
            version,
            "--distribution",
            distribution,
            archives / finalname,
        )

        with open(archives / "build-metadata.json", "w") as f:
            json.dump(
                {
                    "installrefs": [finalname],
                    **self._root_pkg.get_artifact_metadata(self),
                },
                f,
            )


class GenericMacOSBuild(MacOSBuild):
    pass
