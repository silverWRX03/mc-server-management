from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field

from ..config import ModSpec

CHANNEL_RANK = {"release": 0, "beta": 1, "alpha": 2}


class ModError(Exception):
    pass


@dataclass
class Project:
    source: str
    id: str          # canonical id (slugs can change, ids do not)
    slug: str
    name: str
    server_side: str = "required"  # required | optional | unsupported | unknown
    client_side: str = "required"

    @property
    def key(self) -> str:
        return f"{self.source}:{self.id}"


@dataclass
class ModFile:
    """One concrete mod jar chosen for a Minecraft version."""
    key: str
    source: str
    project_id: str
    name: str
    version_id: str
    version_number: str
    filename: str
    url: str
    sha1: str | None = None
    sha512: str | None = None
    #: project ids (same source) this file requires
    dependencies: list[str] = field(default_factory=list)
    required: bool = True
    dependency_of: str | None = None
    #: set when the author blocks automatic downloads: a page where a person can download it
    manual_url: str | None = None

    @property
    def manual(self) -> bool:
        return bool(self.manual_url)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> ModFile:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class Unavailable(Exception):
    """The mod has no usable file for this Minecraft version / loader."""


class ClientOnly(Unavailable):
    """The mod does not run on servers; it is skipped rather than treated as a blocker."""


class ModProvider(ABC):
    source: str = ""

    @abstractmethod
    def project(self, mod_id: str) -> Project:
        """Look up a project by id or slug."""

    @abstractmethod
    def resolve(self, spec: ModSpec, minecraft: str, loaders: tuple[str, ...], channel: str) -> ModFile:
        """Pick the newest acceptable file, or raise :class:`Unavailable`."""

    @abstractmethod
    def supported_versions(self, spec: ModSpec, loaders: tuple[str, ...], channel: str) -> set[str]:
        """Every Minecraft version the mod has an acceptable file for."""
