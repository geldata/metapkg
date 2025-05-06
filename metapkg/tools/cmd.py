from __future__ import annotations
from typing import Any

import logging
import os
import subprocess
import sys

from metapkg.exceptions import MetapkgRuntimeError


logger = logging.getLogger(__name__)


def cmd(
    *cmd: str | os.PathLike[str],
    errors_are_fatal: bool = True,
    hide_stderr: bool = False,
    error_context: str | None = None,
    **kwargs: Any,
) -> str:
    default_kwargs: dict[str, Any] = {
        "stderr": subprocess.DEVNULL if hide_stderr else sys.stderr,
        "stdout": subprocess.PIPE,
    }

    default_kwargs.update(kwargs)

    str_cmd = [str(c) for c in cmd]
    cmd_line = " ".join(str_cmd)
    cwd = kwargs.get("cwd") or os.getcwd()
    print(f"{cwd}> {cmd_line}", file=sys.stderr)

    try:
        p = subprocess.run(str_cmd, text=True, check=True, **default_kwargs)
    except subprocess.CalledProcessError as e:
        if errors_are_fatal:
            raise MetapkgRuntimeError.create(
                reason=error_context or f"{cmd[0]} failed",
                exception=e,
            )
        else:
            raise
    else:
        output = p.stdout
        if output is not None:
            output = output.rstrip()
        return output  # type: ignore
