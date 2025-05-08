from __future__ import annotations
from typing import (
    Any,
    Callable,
    Collection,
    overload,
)

import pathlib
import subprocess

from poetry.core.vcs import git as core_git
from poetry.utils import helpers as poetry_helpers
from poetry.vcs.git import backend as poetry_git

from metapkg import exceptions

from .cmd import cmd


def repodir(repo_url: str) -> pathlib.Path:
    source_root = poetry_git.Git.get_default_source_root()
    name = poetry_git.Git.get_name_from_source_url(url=repo_url)
    return source_root / name


class GitError(Exception):
    pass


class Git(core_git.Git):
    _work_dir: pathlib.Path

    def __init__(self, work_dir: pathlib.Path) -> None:
        try:
            super().__init__(work_dir)
        except Exception as e:
            raise GitError(f"git: could not initialize: {e}") from e

        self._work_dir = work_dir

    @property
    def head(self) -> str:
        head = self.resolve_local_rev("HEAD")
        if head is None or not poetry_git.is_revision_sha(head):
            raise GitError("git: HEAD does not point to a valid commit object")
        return head

    @property
    def work_dir(self) -> pathlib.Path:
        return self._work_dir

    @property
    def git_dir(self) -> pathlib.Path:
        return self._work_dir / ".git"

    def run(
        self,
        *args: Any,
        folder: pathlib.Path | None = None,
        error_context: str | None = None,
        **kwargs: Any,
    ) -> str:
        if not folder and self._work_dir and self._work_dir.exists():
            folder = self._work_dir
        return cmd(
            "git",
            *args,
            cwd=folder,
            error_context=error_context or f"git {args[0]} failed",
            **kwargs,
        ).strip(" \n\t")

    @overload
    def run_or(
        self,
        *args: Any,
        folder: pathlib.Path | None = None,
        error_context: str | None = None,
        when_exit_code: int | Collection[int] | None = None,
        default: None = None,
        **kwargs: Any,
    ) -> str | None: ...

    @overload
    def run_or(
        self,
        *args: Any,
        folder: pathlib.Path | None = None,
        error_context: str | None = None,
        when_exit_code: int | Collection[int] | None = None,
        default: str | Callable[[subprocess.CalledProcessError], str],
        **kwargs: Any,
    ) -> str: ...

    @overload
    def run_or(
        self,
        *args: Any,
        folder: pathlib.Path | None = None,
        error_context: str | None = None,
        when_exit_code: int | Collection[int] | None = None,
        default: Callable[[subprocess.CalledProcessError], str | None],
        **kwargs: Any,
    ) -> str | None: ...

    def run_or(
        self,
        *args: Any,
        folder: pathlib.Path | None = None,
        error_context: str | None = None,
        when_exit_code: int | Collection[int] | None = None,
        default: (
            str | Callable[[subprocess.CalledProcessError], str | None] | None
        ) = None,
        **kwargs: Any,
    ) -> str | None:
        try:
            return self.run(
                *args,
                folder=folder,
                errors_are_fatal=False,
                error_context=error_context,
                **kwargs,
            )
        except subprocess.CalledProcessError as e:
            if when_exit_code is not None:
                if isinstance(when_exit_code, int):
                    codes = {when_exit_code}
                else:
                    codes = set(when_exit_code)
                if e.returncode not in codes:
                    raise exceptions.MetapkgRuntimeError.create(
                        error_context or f"git {args[0]} failed",
                        exception=e,
                    )

            if callable(default):
                try:
                    return default(e)
                except Exception as defe:
                    msg = error_context or f"git {args[0]} failed"

                    if isinstance(defe, subprocess.CalledProcessError):
                        error = exceptions.PrettyCalledProcessError(
                            defe, indent="    | "
                        )
                        messages = [
                            error.message.wrap("warning"),
                            error.output.wrap("warning"),
                            error.errors.wrap("warning"),
                            error.command_message.make_section(
                                "Default Callback Exception", indent="    | "
                            ),
                        ]
                    else:
                        messages = [
                            exceptions.ConsoleMessage(str(defe)).make_section(
                                "Default Callback Exception", indent="    | "
                            )
                        ]

                    err = exceptions.MetapkgRuntimeError.create(
                        f"{msg} and the default callback failed also",
                        exception=e,
                    )

                    for m in messages:
                        err.append(m)

                    raise err from e
            else:
                return default

    @property
    def work_tree(self) -> pathlib.Path:
        work_tree = self._work_dir
        assert work_tree is not None
        return work_tree

    def resolve_local_rev(self, rev: str) -> str | None:
        """Resolve a local revision spec to a full commit SHA.

        If the *refspec* cannot be resolved, return None.
        """
        sha = self.run_or(
            "rev-parse",
            "--verify",
            "--quiet",
            "--end-of-options",
            f"{rev}^{{commit}}",
        )
        if sha is not None and not poetry_git.is_revision_sha(sha):
            # rev-parse returned *something* but it isn't a SHA
            sha = None
        return sha


class GitClone(Git):
    @classmethod
    def initial_clone(
        cls,
        repo_url: str,
        repo_dir: pathlib.Path,
        clone_depth: int | None = None,
    ) -> GitClone:
        args = []
        if clone_depth:
            args.append(f"--depth={clone_depth}")
        args.extend(
            [
                "--quiet",
                "--filter=blob:none",
                "--origin=origin",
                "--",
                repo_url,
                str(repo_dir),
            ]
        )

        cmd("git", "clone", *args)

        try:
            return GitClone(repo_url, repo_dir)
        except GitError as e:
            raise GitError(
                f"cloning {repo_url} produced a broken clone: {e}"
            ) from e

    def __init__(self, repo_url: str, work_dir: pathlib.Path) -> None:
        super().__init__(work_dir)
        self._remote_refs: dict[str, str] = {}
        self.run_or(
            "remote",
            "set-url",
            "origin",
            repo_url,
            when_exit_code=2,
            default=lambda _: self.run("remote", "add", repo_url),
        )

    def resolve_remote_symref(self, ref: str) -> str:
        output = self.run(
            "ls-remote",
            "--exit-code",
            "--quiet",
            "--symref",
            "origin",
            ref,
            error_context=f"could not resolve remote {ref} symbolic ref",
        )
        target = ""
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            line_oid, _, line_ref = line.partition("\t")
            if line_ref == ref and line_oid.startswith("ref:"):
                target = line_oid.removeprefix("ref:").strip()
                break

        if not target:
            raise exceptions.MetapkgRuntimeError.create(
                f"could not resolve remote {ref} symbolic ref",
                info=[
                    f"git ls-remote --symref origin {ref} did not produce "
                    "useful output",
                ],
            )

        return target

    def _fetch_remote_refs(self) -> dict[str, str]:
        output = self.run(
            "ls-remote",
            "origin",
            "refs/tags/**",
            "refs/heads/**",
            error_context="could not list remote git refs",
        )

        if not output:
            return {}

        ref_map: dict[str, str] = {}
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            sha, _, name = line.partition("\t")
            ref_map[name] = sha

        return ref_map

    @property
    def remote_refs(self) -> dict[str, str]:
        if not self._remote_refs:
            self._remote_refs = self._fetch_remote_refs()

        return self._remote_refs

    def normalize_remote_ref(self, ref: str) -> str | None:
        if ref == "HEAD":
            return self.resolve_remote_symref(ref)

        tag_ref = f"refs/tags/{ref}"
        peeled_tag_ref = f"{tag_ref}^{{}}"
        branch_ref = f"refs/heads/{ref}"
        remote_refs = self.remote_refs

        if tag_ref in remote_refs or peeled_tag_ref in remote_refs:
            # It's a tag
            return tag_ref
        elif branch_ref in remote_refs:
            # It's a branch
            return branch_ref
        else:
            # It's a SHA or maybe a refspec, loop through remote refs
            # to find if any ref points to it.
            for name, rev in remote_refs.items():
                if poetry_git.is_revision_sha(rev) and rev.startswith(ref):
                    return name
            else:
                # No remote ref points to the requested revision,
                # the caller would need to do something about it.
                return None

    def update(
        self,
        remote_ref: str | None = None,
        *,
        exclude_submodules: frozenset[str] | None = None,
        clone_depth: int | None = None,
    ) -> None:
        if remote_ref is None:
            remote_ref = "HEAD"
        refspec = self.normalize_remote_ref(remote_ref)
        if refspec is None:
            refspec = ""
            fetch_depth = None
        else:
            fetch_depth = clone_depth
        # Reset the index.
        self.run("reset", "--hard")
        # Fetch new stuff.
        args = ["--quiet", "--prune", "--prune-tags", "--tags"]
        if fetch_depth:
            args.append(f"--depth={fetch_depth}")
        self.run("fetch", *args, "origin", refspec)
        if refspec.startswith("refs/tags/"):
            # Avoid creating ambiguous local refs when updating to a tag.
            branch = f"{pathlib.Path(refspec).name}-branch"
        elif refspec.startswith("refs/heads/"):
            branch = pathlib.Path(refspec).name
        else:
            # Detached HEAD
            branch = None

        args = ["--quiet"]
        if branch is not None:
            args.extend(["-B", branch])
        else:
            args.append("--detach")
        args.append("FETCH_HEAD" if refspec else remote_ref)

        self.run("checkout", *args)

        self._update_submodules(
            exclude=exclude_submodules,
            clone_depth=clone_depth,
        )

    def _update_submodules(
        self,
        *,
        exclude: frozenset[str] | None = None,
        clone_depth: int | None = None,
    ) -> None:
        args: tuple[str | pathlib.Path, ...]
        submodules: set[str] | None = None
        deinit_submodules = set()

        if exclude:
            output = self.run_or(
                "config",
                "--file",
                ".gitmodules",
                "--name-only",
                "--get-regexp",
                "path",
                # No .gitmodules file, that's fine
                when_exit_code=1,
                default="",
            )

            submodules = set()
            for smc in output.splitlines():
                submodule_path = self.run(
                    "config", "--file", ".gitmodules", smc
                ).strip()
                if submodule_path not in exclude:
                    submodules.add(submodule_path)
                else:
                    deinit_submodules.add(submodule_path)

        if submodules != set():
            args = ("submodule", "update", "--init", "--checkout", "--force")
            if clone_depth:
                args += (f"--depth={clone_depth}",)
            if submodules:
                args += tuple(submodules)
            self.run(*args)

            if deinit_submodules:
                self.run(*(("submodule", "deinit") + tuple(deinit_submodules)))


def clone_repo(
    repo_url: str,
    *,
    remote_ref: str | None = None,
    clean_checkout: bool = False,
    exclude_submodules: frozenset[str] | None = None,
    clone_depth: int | None = None,
) -> GitClone:
    repo_dir = repodir(repo_url)

    if clean_checkout:
        poetry_helpers.remove_directory(repo_dir, force=True)

    if repo_dir.exists():
        try:
            repo = GitClone(repo_url, repo_dir)
        except GitError:
            # Something is up with the current clone, start fresh.
            poetry_helpers.remove_directory(repo_dir, force=True)

    if not repo_dir.exists():
        repo = GitClone.initial_clone(
            repo_url, repo_dir, clone_depth=clone_depth
        )

    repo.update(
        remote_ref,
        exclude_submodules=exclude_submodules,
        clone_depth=clone_depth,
    )

    return repo
