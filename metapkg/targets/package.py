from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Optional
from typing import TypeVar
from typing import Union

from poetry.core.packages import package as poetry_pkg

from . import base

if TYPE_CHECKING:
    from poetry.core.constraints.version import Version


class SystemPackage(poetry_pkg.Package):
    def __init__(
        self,
        name: str,
        version: Union[str, Version],
        pretty_version: Optional[str] = None,
        system_name: Optional[str] = None,
    ):
        super().__init__(name, version, pretty_version=pretty_version)
        self._system_name = system_name

    @property
    def system_name(self) -> Optional[str]:
        return self._system_name

    def get_shlibs(self, build: base.Build) -> list[str]:
        return []

    def __repr__(self) -> str:
        return "<SystemPackage {}>".format(self.unique_name)


SystemPackage_T = TypeVar("SystemPackage_T", bound=SystemPackage)


class StandardSystemPackage(SystemPackage):
    """A package that is part of standard system distribution.

    Standard packages take precendnce over those can be installed by a user
    or the package manager and that can be located via pkg-config.
    """

    pass
