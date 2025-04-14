from __future__ import annotations
from typing import (
    Any,
)

import os
import pathlib
import subprocess
import sys
from functools import cached_property

from dulwich import repo as dulwich_repo

from poetry.core.vcs import git as core_git
from poetry.vcs import git as poetry_git

from .cmd import cmd


class Git(core_git.Git):
    def run(
        self,
        *args: Any,
        folder: pathlib.Path | None = None,
        **kwargs: Any,
    ) -> str:
        if not folder and self._work_dir and self._work_dir.exists():
            folder = self._work_dir
        result = cmd("git", *args, cwd=folder, **kwargs)
        result = result.strip(" \n\t")
        return result

    @property
    def work_tree(self) -> pathlib.Path:
        work_tree = self._work_dir
        assert work_tree is not None
        return work_tree

    @cached_property
    def dulwich_repo(self) -> dulwich_repo.Repo:
        assert self._work_dir is not None
        return dulwich_repo.Repo(str(self._work_dir))

    def rev_parse(self, rev: str) -> str:
        repo = self.dulwich_repo
        with repo:
            return repo.get_peeled(rev.encode("utf-8")).decode("utf-8")

    def peel_ref(self, ref: str) -> str:
        if ref == "HEAD":
            return self.rev_parse(ref)

        # The name can be a branch or tag, so we attempt to look it up
        # with ls-remote. If we don't find anything, we assume it's a
        # commit hash.
        rev = None
        output = self.run("ls-remote", "--heads", "--tags", "origin", ref)
        if not output:
            print("git ls-remote produced no output", file=sys.stderr)
        if output:
            lines = output.splitlines()

            sha_map: dict[str, str] = {}
            for line in lines:
                if not line.strip():
                    continue
                sha, name = line.strip().split("\t")
                sha_map[name] = sha

            # Try peeled tag (refs/tags/<ref>^{})
            peeled_tag = f"refs/tags/{ref}^{{}}"
            if peeled_tag in sha_map:
                rev = sha_map[peeled_tag]

            if rev is None:
                # Try direct tag (refs/tags/<ref>)
                tag = f"refs/tags/{ref}"
                if tag in sha_map:
                    rev = sha_map[tag]

                # If it's a tag object, peel it.
                if (
                    rev is not None
                    and self.run("cat-file", "-t", rev) == "tag"
                ):
                    rev = self.run("rev-list", "-n", "1", rev)

            if rev is None:
                # Try branch (refs/heads/<ref>)
                branch = f"refs/heads/{ref}"
                if branch in sha_map:
                    rev = sha_map[branch]

        if rev is None:
            # The name can be a branch or tag, so we attempt to look it up
            # with ls-remote. If we don't find anything, we assume it's a
            # commit hash.
            rev = ref

        return rev


class GitBackend(poetry_git.Git):
    @classmethod
    def _clone_submodules(cls, repo: dulwich_repo.Repo) -> None:
        return


def repodir(repo_url: str) -> pathlib.Path:
    source_root = GitBackend.get_default_source_root()
    name = GitBackend.get_name_from_source_url(url=repo_url)
    return source_root / name


def repo(repo_url: str) -> Git:
    return Git(repodir(repo_url))


def update_repo(
    repo_url: str,
    *,
    exclude_submodules: frozenset[str] | None = None,
    clone_depth: int = 0,
    clean_checkout: bool = False,
    ref: str | None = None,
) -> pathlib.Path:
    if ref == "HEAD":
        ref = None

    if not clean_checkout:
        checkout = (
            GitBackend.get_default_source_root()
            / GitBackend.get_name_from_source_url(repo_url)
        )

        if checkout.exists():
            cache_remote_url = GitBackend.get_remote_url(
                dulwich_repo.Repo(str(checkout)),
            )
            if cache_remote_url != repo_url:
                # Origin URL has changed, perform a full clone.
                clean_checkout = True

    old_keyring_backend = os.environ.get("PYTHON_KEYRING_BACKEND")
    try:
        # Prevent Poetry from trying to read system keyrings and failing
        # (specifically reading Windows keyring from an SSH session fails
        # with "A specified logon session does not exist.")
        os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"
        GitBackend.clone(repo_url, revision=ref, clean=clean_checkout)
    finally:
        if old_keyring_backend is None:
            os.environ.pop("PYTHON_KEYRING_BACKEND")
        else:
            os.environ["PYTHON_KEYRING_BACKEND"] = old_keyring_backend

    repo_dir = repodir(repo_url)
    repo = Git(repo_dir)
    args: tuple[str | pathlib.Path, ...]

    submodules: set[str] | None = None
    deinit_submodules = set()
    if exclude_submodules:
        try:
            output = repo.run(
                "config",
                "--file",
                ".gitmodules",
                "--name-only",
                "--get-regexp",
                "path",
                errors_are_fatal=False,
            )
        except subprocess.CalledProcessError as e:
            if e.returncode == 1:
                # No .gitmodules file, that's fine
                submodules = set()
            else:
                raise
        else:
            submodules = set()
            submodule_configs = output.strip().split("\n")
            for smc in submodule_configs:
                submodule_path = repo.run(
                    "config", "--file", ".gitmodules", smc
                ).strip()
                if submodule_path not in exclude_submodules:
                    submodules.add(submodule_path)
                else:
                    deinit_submodules.add(submodule_path)

    if submodules != set():
        args = ("submodule", "update", "--init", "--checkout", "--force")
        if clone_depth:
            args += (f"--depth={clone_depth}",)
        if submodules:
            args += tuple(submodules)
        repo.run(*args)

        if deinit_submodules:
            repo.run(*(("submodule", "deinit") + tuple(deinit_submodules)))

    return repo_dir
