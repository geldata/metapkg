from __future__ import annotations
from typing import (
    TYPE_CHECKING,
)

from cleo.application import Application as BaseApplication
from cleo.formatters.style import Style

import metapkg

from . import commands as metapkg_commands

if TYPE_CHECKING:
    from cleo.io.inputs.input import Input
    from cleo.io.io import IO
    from cleo.io.outputs.output import Output


class App(BaseApplication):
    def __init__(self) -> None:
        super().__init__(metapkg.__name__, metapkg.__version__)

    def create_io(
        self,
        input: Input | None = None,
        output: Output | None = None,
        error_output: Output | None = None,
    ) -> IO:
        io = super().create_io(input, output, error_output)
        io.output.formatter.set_style("info", Style("blue").bold())
        io.error_output.formatter.set_style("info", Style("blue").bold())
        return io


def main() -> int:
    app = App()
    for cmd_name in metapkg_commands.__all__:
        cmd = getattr(metapkg_commands, cmd_name)
        app.add(cmd())

    return app.run()
