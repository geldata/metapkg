from __future__ import annotations
from typing import (
    TYPE_CHECKING,
    Any,
    Iterable,
    TypedDict,
)

import hashlib
import os
import pathlib
import platform
import re
import shlex
import shutil
import tarfile
import tempfile
import typing
import urllib.parse
import zipfile

import requests

from metapkg import cache
from metapkg import packages as mpkg
from metapkg import targets
from metapkg import tools

from cleo.ui import progress_bar

if TYPE_CHECKING:
    from cleo.io import io as cleo_io


class SourceDeclBase(TypedDict):
    url: str


class SourceDecl(SourceDeclBase, total=False):
    csum: str | None
    csum_url: str | None
    csum_algo: str | None
    extras: SourceExtraDecl | None


class SourceExtraDecl(TypedDict, total=False):
    exclude_submodules: list[str]
    clone_depth: int
    version: str
    vcs_version: str
    include_gitdir: bool
    archive: bool


class BaseVerification:
    def verify(self, path: pathlib.Path) -> None:
        raise NotImplementedError


class HashVerification(BaseVerification):
    def __init__(
        self,
        algorithm: str,
        *,
        hash_url: str | None = None,
        hash_value: str | None = None,
    ) -> None:
        self.algorithm = algorithm
        self._hash_value: str | None
        if hash_value is not None:
            self._hash_value = hash_value
        elif hash_url is not None:
            self._hash_url = hash_url
            self._hash_value = None
        else:
            raise ValueError(
                "either hash_url or hash_value is required "
                "for HashVerification"
            )

    def verify(self, path: pathlib.Path) -> None:
        if self._hash_value is None:
            self._obtain_hash_value()

        hashfunc = hashlib.new(self.algorithm)
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                hashfunc.update(chunk)

        if hashfunc.hexdigest() != self._hash_value:
            raise ValueError(
                f"{path} does not match expected {self.algorithm} value of "
                f"{self._hash_value}"
            )

    def _obtain_hash_value(self) -> str:
        content = requests.get(self._hash_url).text.strip()
        firstval, _, rest = content.partition(" ")
        self._hash_value = firstval
        return firstval


class BaseSource:
    def __init__(
        self,
        url: str,
        name: str,
        path: str | None = None,
        **extras: Any,
    ) -> None:
        self.url = url
        self.verifications: list[BaseVerification] = []
        self.name = name
        self.extras = extras
        self.path = path

    def add_verification(self, verification: BaseVerification) -> None:
        self.verifications.append(verification)

    def verify(self, path: pathlib.Path) -> None:
        for verification in self.verifications:
            verification.verify(path)

    def copy(
        self,
        target_dir: pathlib.Path,
        *,
        io: cleo_io.IO,
    ) -> None:
        raise NotImplementedError

    def tarball(
        self,
        pkg: mpkg.BasePackage,
        name_tpl: typing.Optional[str] = None,
        *,
        target_dir: pathlib.Path,
        io: cleo_io.IO,
        build: targets.Build,
        part: str = "",
    ) -> pathlib.Path:
        raise NotImplementedError


class HttpsSource(BaseSource):
    def download(self, io: cleo_io.IO) -> pathlib.Path:
        destination_dir = cache.cachedir() / "distfiles"
        if not destination_dir.exists():
            destination_dir.mkdir()

        destination = destination_dir / self.name
        if destination.exists():
            try:
                self.verify(destination)
            except Exception:
                io.write_line(
                    f"<warning>Cached {self.name} exists, but does pass "
                    f"verification.  Downloading anew."
                )
            else:
                return destination

        return self._download(destination, io)

    def _download(
        self, destination: pathlib.Path, io: cleo_io.IO
    ) -> pathlib.Path:
        req = requests.get(self.url, stream=True)
        length = int(req.headers.get("content-length", 0))

        progress = progress_bar.ProgressBar(io, max=length)
        io.write_line(f"Downloading <info>{self.url}</>")
        if req.status_code < 200 or req.status_code >= 300:
            raise RuntimeError(f"download failed: {req.status_code}")

        progress.start(length)

        try:
            with open(destination, "wb") as f:
                for chunk in req.iter_content(chunk_size=4096):
                    if chunk:
                        progress.advance(len(chunk))
                        f.write(chunk)
        except BaseException:
            if destination.exists():
                destination.unlink()
        finally:
            progress.finish()
            io.write_line("")

        try:
            self.verify(destination)
        except Exception:
            destination.unlink()
            raise

        return destination

    def _tarball(
        self,
        pkg: typing.Optional[mpkg.BasePackage] = None,
        name_tpl: typing.Optional[str] = None,
        *,
        target_dir: pathlib.Path,
        io: cleo_io.IO,
        part: str = "",
        build: targets.Build | None = None,
    ) -> pathlib.Path:
        if name_tpl is None:
            assert pkg is not None
            name_tpl = f"{pkg.unique_name}{{part}}.tar{{comp}}"
        src = self.download(io)
        copy = True
        target_path = None
        if "archive" in self.extras and not self.extras["archive"]:
            copy = False
            comp = ".gz"
            target_path = target_dir / name_tpl.format(part=part, comp=comp)
            with tarfile.open(target_path, "w:gz") as tf:
                tf.add(
                    str(src),
                    arcname=str(pathlib.Path(src.name) / src.name),
                )
        elif src.suffix == ".tgz":
            comp = ".gz"
        elif src.suffix == ".tbz2":
            comp = ".bzip2"
        elif src.suffix != ".tar" and ".tar" in src.suffixes:
            comp = src.suffix
        elif src.suffix == ".zip":
            comp = ".gz"
            target_path = target_dir / name_tpl.format(part=part, comp=comp)
            with tempfile.TemporaryDirectory() as tmpdir:
                destdir = pathlib.Path(tmpdir)
                unpack(
                    src, dest=destdir, io=io, strip_components=0, build=build
                )
                subdirs = os.listdir(destdir)
                if len(subdirs) > 1:
                    raise RuntimeError(
                        "multiple top-level directories in source archive"
                    )
                subdir = next(iter(subdirs))
                with tarfile.open(target_path, "w:gz") as tf:
                    tf.add(str(destdir / subdir), arcname=subdir)
            copy = False
        else:
            raise RuntimeError(f"unsupported archive format: {src.suffix}")

        if copy:
            target_path = target_dir / name_tpl.format(part=part, comp=comp)
            shutil.copy(src, target_path)

        assert target_path is not None
        return target_path

    def tarball(
        self,
        pkg: typing.Optional[mpkg.BasePackage] = None,
        name_tpl: typing.Optional[str] = None,
        *,
        target_dir: pathlib.Path,
        io: cleo_io.IO,
        build: targets.Build,
        part: str = "",
    ) -> pathlib.Path:
        return self._tarball(
            pkg, name_tpl, part=part, build=build, target_dir=target_dir, io=io
        )

    def copy(
        self,
        target_dir: pathlib.Path,
        *,
        io: cleo_io.IO,
    ) -> None:
        self.download(io)
        with tempfile.TemporaryDirectory() as t:
            tardir = pathlib.Path(t)
            tarball = self._tarball(
                name_tpl="tmp{part}.tar{comp}",
                target_dir=tardir,
                io=io,
            )
            unpack(tarball, dest=target_dir, io=io)


class LocalSource(BaseSource):
    def tarball(
        self,
        pkg: mpkg.BasePackage,
        name_tpl: typing.Optional[str] = None,
        *,
        target_dir: pathlib.Path,
        io: cleo_io.IO,
        build: targets.Build,
        part: str = "",
    ) -> pathlib.Path:
        comp = ".gz"
        if name_tpl is None:
            name_tpl = f"{pkg.unique_name}{{part}}.tar{{comp}}"
        target_path = target_dir / name_tpl.format(part=part, comp=comp)

        tar = shlex.split(build.sh_get_command("tar"))
        tools.cmd(
            *tar,
            *[
                f"--directory={self.url}",
                "--exclude-vcs",
                "--exclude-vcs-ignores",
                "--create",
                "--gzip",
                f"--transform=flags=r;s|^\\./|{pkg.unique_name}/|",
                f"--file={target_path}",
                ".",
            ],
        )

        return target_path

    def copy(
        self,
        target_dir: pathlib.Path,
        *,
        io: cleo_io.IO,
    ) -> None:
        shutil.copytree(self.url, target_dir)


class GitSource(BaseSource):
    def __init__(
        self,
        url: str,
        name: str,
        *,
        vcs_version: str | None = None,
        exclude_submodules: Iterable[str] | None = None,
        clone_depth: int | None = None,
        include_gitdir: bool = False,
        force_archive: Iterable[str] | None = None,
    ) -> None:
        super().__init__(url, name)
        self.ref = vcs_version
        if exclude_submodules is not None:
            self.exclude_submodules = frozenset(exclude_submodules)
        else:
            self.exclude_submodules = frozenset()
        if force_archive is not None:
            self.force_archive = frozenset(force_archive)
        else:
            self.force_archive = frozenset()
        self.clone_depth = clone_depth
        self.include_gitdir = include_gitdir
        self._clone: tools.git.GitClone | None = None

    def download(self, io: cleo_io.IO) -> tools.git.GitClone:
        if self._clone is None:
            self._clone = tools.git.clone_repo(
                self.url,
                remote_ref=self.ref,
                clean_checkout=os.environ.get("METAPKG_GIT_CACHE")
                == "disabled",
                exclude_submodules=self.exclude_submodules,
                clone_depth=self.clone_depth,
            )
        return self._clone

    def copy(
        self,
        target_dir: pathlib.Path,
        *,
        io: cleo_io.IO,
    ) -> None:
        repo = self.download(io)
        repo.run(
            "checkout-index",
            "-a",
            "-f",
            f"--prefix={target_dir}",
        )

    def tarball(
        self,
        pkg: mpkg.BasePackage,
        name_tpl: typing.Optional[str] = None,
        *,
        target_dir: pathlib.Path,
        io: cleo_io.IO,
        build: targets.Build,
        part: str = "",
    ) -> pathlib.Path:
        repo = self.download(io)
        if name_tpl is None:
            name_tpl = f"{pkg.unique_name}{{part}}.tar{{comp}}"
        target_path = target_dir / name_tpl.format(part=part, comp="")

        repo.run(
            "archive",
            f"--output={target_path}",
            "--format=tar",
            f"--prefix={pkg.unique_name}/",
            "HEAD",
        )

        submodules = repo.run(
            "submodule", "foreach", "--recursive", "echo $sm_path"
        )

        for path in submodules.splitlines():
            path = path.strip()
            if not path:
                continue
            module_repo = tools.git.Git(repo.work_tree / path)
            f = tempfile.NamedTemporaryFile(delete=False)
            f.close()
            try:
                module_repo.run(
                    "archive",
                    "--format=tar",
                    f"--output={f.name}",
                    f"--prefix={pkg.unique_name}/{path}/",
                    "HEAD",
                )
                self._tar_append(pathlib.Path(f.name), target_path)
            finally:
                os.unlink(f.name)

        if self.include_gitdir:
            prefix = f"{pkg.unique_name}/.git/"
            with tarfile.open(target_path, "a") as tf:
                tf.add(repo.git_dir, prefix)

        if self.force_archive:
            with tarfile.open(target_path, "a") as tf:
                for path in self.force_archive:
                    repo_path = repo.work_dir / path
                    prefix = f"{pkg.unique_name}/{path}"
                    if prefix not in set(tf.getnames()):
                        tf.add(repo_path, prefix)

        tools.cmd("gzip", target_path, cwd=target_dir)
        return pathlib.Path(f"{target_path}.gz")

    def _tar_append(
        self,
        source_tarball: pathlib.Path,
        target_tarball: pathlib.Path,
    ) -> None:
        if platform.system() != "Linux":
            with (
                tarfile.open(source_tarball) as modf,
                tarfile.open(target_tarball, "a") as tf,
            ):
                for m in modf.getmembers():
                    if m.issym():
                        # Skip broken symlinks.
                        target = os.path.normpath(
                            "/".join(
                                filter(
                                    None,
                                    (
                                        os.path.dirname(m.name),
                                        m.linkname,
                                    ),
                                )
                            )
                        )
                        try:
                            modf.getmember(target)
                        except KeyError:
                            continue
                    tf.addfile(m, modf.extractfile(m))

        else:
            tools.cmd(
                "tar",
                "--concatenate",
                "--file",
                target_tarball,
                source_tarball,
            )


def source_for_url(
    url: str,
    extras: SourceExtraDecl | None = None,
) -> BaseSource:
    parts = urllib.parse.urlparse(url)
    path_parts = parts.path.split("/")
    name = path_parts[-1]
    if extras is None:
        extras = {}
    if parts.scheme == "https" or parts.scheme == "http":
        return HttpsSource(url, name=name, **extras)
    elif parts.scheme.startswith("git+"):
        extras_dict = dict(extras)
        version = extras_dict.pop("version", None)
        if "vcs_version" not in extras and version is not None:
            extras_dict["vcs_version"] = version
        return GitSource(url[4:], name=name, **extras_dict)  # type: ignore
    elif parts.scheme == "file":
        return LocalSource(parts.path, name, **extras)
    else:
        raise ValueError(f"unsupported source URL scheme: {parts.scheme}")


def unpack(
    archive: pathlib.Path,
    dest: pathlib.Path,
    io: cleo_io.IO,
    *,
    build: targets.Build | None = None,
    strip_components: int = 1,
) -> None:
    parts = archive.name.split(".")
    if len(parts) == 1:
        raise ValueError(f"{archive.name} is not a supported archive")

    if not dest.exists():
        dest.mkdir()

    ext = parts[-1]

    if parts[-2] == "tar" or ext in {"tgz", "tbz2", "tar"}:
        unpack_tar(
            archive, dest, build=build, strip_components=strip_components
        )
    elif parts[-1] == "zip":
        unpack_zip(
            archive, dest, build=build, strip_components=strip_components
        )
    else:
        raise ValueError(f"{archive.name} is not a supported archive")


def _win_path_to_msys_path(
    path: pathlib.PurePath,
) -> pathlib.PurePosixPath:
    return (
        pathlib.PurePosixPath("/")
        / path.drive.rstrip(":").lower()
        / path.relative_to(path.drive + "\\")
    )


def unpack_tar(
    archive: pathlib.PurePath,
    dest: pathlib.PurePath,
    *,
    build: targets.Build | None = None,
    strip_components: int,
) -> None:
    ext = archive.suffix

    if build is not None:
        if ext in (".gz", ".tgz"):
            compression = "z"
        elif ext in (".bz2", ".tbz2"):
            compression = "j"
        elif ext == ".xz":
            compression = "J"
        else:
            raise ValueError(f"{archive.name} is not a supported archive")

        if platform.system() == "Windows":
            archive = _win_path_to_msys_path(archive)
            dest = _win_path_to_msys_path(dest)

        tar = build.sh_get_command("tar", relative_to="fsroot")
        args = [
            f"-x{compression}",
            f"-f{archive}",
            f"-C{dest}",
        ]
        if strip_components:
            args.append(f"--strip-components={strip_components}")

        tools.cmd(tar, *args)
    else:
        if ext in (".gz", ".tgz"):
            compression = "gz"
        elif ext in (".bz2", ".tbz2"):
            compression = "bz2"
        elif ext == ".xz":
            compression = "xz"
        else:
            raise ValueError(f"{archive.name} is not a supported archive")

        with tarfile.open(archive, mode=f"r:{compression}") as tf:  # type: ignore
            for member in tf.getmembers():
                if strip_components:
                    member_parts = pathlib.Path(member.name).parts
                    if len(member_parts) <= strip_components:
                        continue

                    path = pathlib.Path(
                        member_parts[strip_components]
                    ).joinpath(*member_parts[strip_components + 1 :])
                    member.name = str(path)
                tf.extract(member, path=dest)


def unpack_zip(
    archive: pathlib.Path,
    dest: pathlib.Path,
    *,
    strip_components: int,
    build: targets.Build | None = None,
) -> None:
    zf = zipfile.ZipFile(archive)

    try:
        for member in zf.infolist():
            if strip_components:
                member_parts = pathlib.Path(member.filename).parts
                if len(member_parts) == strip_components:
                    continue

                relpath = pathlib.Path(
                    member_parts[strip_components]
                ).joinpath(*member_parts[strip_components + 1 :])
            else:
                relpath = pathlib.Path(member.filename)
            targetpath = dest / relpath
            if member.is_dir():
                targetpath.mkdir(parents=True, exist_ok=True)
            else:
                dirname = targetpath.parent
                if not dirname.exists():
                    dirname.mkdir(parents=True)
                with open(targetpath, "wb") as df, zf.open(member) as sf:
                    shutil.copyfileobj(sf, df)
    finally:
        zf.close()
