"""Structural consumption contracts only. This module implements no identity authority."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Protocol

from semantic_api.catalog_v1.catalog import PublishedCatalogs
from semantic_api.catalog_v1.models import CatalogDocument, ResourceVersion
from semantic_api.catalog_v1.trust import TrustedContext

if TYPE_CHECKING:
    from psycopg import AsyncConnection


class AccessView(Protocol):
    @property
    def entity_ids(self) -> frozenset[str]: ...
    @property
    def field_ids(self) -> frozenset[str]: ...
    @property
    def metric_ids(self) -> frozenset[str]: ...
    @property
    def relation_ids(self) -> frozenset[str]: ...
    @property
    def member_ids(self) -> frozenset[str]: ...


class AuthorizationDecision(Protocol):
    @property
    def connection(self) -> AsyncConnection[tuple[object, ...]]: ...
    @property
    def access(self) -> AccessView | None: ...


class AuthorizationGuard(Protocol):
    def guard(
        self,
        context: TrustedContext,
        pin: ResourceVersion | None = None,
        permission: str = "run.reader",
        *,
        deadline: float | None = None,
    ) -> AbstractAsyncContextManager[AuthorizationDecision]:
        """Own the shared auth gate, live authority checks, transaction and successful exit."""
        ...


class GuardedCatalogRepository(Protocol):
    async def get(
        self, connection: AsyncConnection[tuple[object, ...]], pin: ResourceVersion
    ) -> CatalogDocument:
        """Read an immutable pin; any database lock/read must use this same connection."""
        ...


class PublishedCatalogGuardAdapter:
    """A no-I/O adapter for the existing immutable in-memory publication repository."""

    def __init__(self, catalogs: PublishedCatalogs) -> None:
        self.catalogs = catalogs

    async def get(
        self, connection: AsyncConnection[tuple[object, ...]], pin: ResourceVersion
    ) -> CatalogDocument:
        return await self.catalogs.get(pin)
