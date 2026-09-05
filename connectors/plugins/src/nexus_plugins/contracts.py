from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

import pyarrow as pa
from pydantic import BaseModel, ConfigDict, Field
from query_runtime.domain import BoundSource
from query_runtime.errors import ResolverFailure

PLUGIN_VERSION = "nexus-plugins/v1"


class CredentialReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str = Field(pattern=r"^credential:[a-z][a-z0-9-]{0,63}$")


class CredentialProvider(Protocol):
    async def resolve(self, reference: CredentialReference) -> Mapping[str, str]: ...


class PluginDescriptor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: Literal["nexus-plugins/v1"]
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    kind: Literal["resolver", "compute", "result-store"]
    runtime_versions: tuple[Literal["query-runtime/v0", "query-runtime/v1"], ...]
    interchange: tuple[Literal["arrow", "parquet"], ...]
    credential_ref: CredentialReference | None = None
    read_only: Literal[True] = True


@dataclass(frozen=True)
class Asset:
    """Server-owned binding, never deserialized from a query or SQG."""

    source: BoundSource
    schema: pa.Schema
    table: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        import re

        for value in (self.source.alias, self.source.object_name):
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value):
                raise ValueError("Asset aliases and IDs must be opaque identifiers")
        names = self.schema.names
        if not names or len(names) > 128 or len({n.lower() for n in names}) != len(names):
            raise ValueError("Asset schema requires 1..128 non-colliding columns")
        if any(not name or "\x00" in name for name in (*names, *self.table)):
            raise ValueError("Invalid asset identifier")


def require_asset(fragment_source: BoundSource, assets: Mapping[str, Asset]) -> Asset:
    asset = assets.get(fragment_source.object_name)
    if asset is None or asset.source != fragment_source:
        raise ResolverFailure("ASSET_DENIED", "Source is not in the server-owned asset inventory")
    return asset
