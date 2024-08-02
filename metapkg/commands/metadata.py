from __future__ import annotations

import importlib
import json
import pathlib
import sys

from cleo.helpers import argument, option
from poetry.utils import env as poetry_env

from metapkg import targets

from . import base


class Metadata(base.Command):
    name = "metadata"
    description = """Returns metadata for a given package and version"""
    arguments = [
        argument(
            "name",
            description="Package to build",
        ),
    ]
    options = [
        option(
            "generic",
            description="Build a generic artifact",
            flag=True,
        ),
        option(
            "libc",
            description="Libc to target",
            flag=False,
        ),
        option(
            "release",
            description="Whether this build is a release",
            flag=True,
        ),
        option(
            "source-ref",
            description="Source version to build (VCS ref or tarball version)",
            flag=False,
        ),
        option(
            "pkg-revision",
            description="Override package revision number (defaults to 1)",
            flag=False,
        ),
    ]

    _loggers = ["metapkg.metadata"]

    def handle(self) -> int:
        pkgname = self.argument("name")
        generic = self.option("generic")
        libc = self.option("libc")
        version = self.option("source-ref")
        revision = self.option("pkg-revision")
        is_release = self.option("release")

        target = targets.detect_target(self.io, portable=generic, libc=libc)
        target.prepare()

        modname, _, clsname = pkgname.rpartition(":")

        mod = importlib.import_module(modname)
        pkgcls = getattr(mod, clsname)
        root_pkg = pkgcls.resolve(
            self.io,
            version=version,
            revision=revision,
            is_release=is_release,
            target=target,
        )

        env = poetry_env.SystemEnv(pathlib.Path(sys.executable))

        build = target.get_builder_instance(
            targets.BuildRequest(
                io=self.io,
                env=env,
                root_pkg=root_pkg,
                revision=revision or "1",
            ),
        )

        print(json.dumps(root_pkg.get_artifact_metadata(build)))

        return 0
