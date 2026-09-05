from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from pydantic import Field, ValidationError

from semantic_api.catalog_v1.catalog import CatalogRepository, authorized_view, fingerprint, pin_for
from semantic_api.catalog_v1.clarification import Answered, ClarificationRecord, Clarifications
from semantic_api.catalog_v1.initializer import initialize
from semantic_api.catalog_v1.models import (
    CallMetadata,
    Candidate,
    CatalogCompileRequest,
    CatalogDocument,
    Compilation,
    CompilerContext,
    CompilerFailure,
    Frozen,
    Resolution,
    ResourceVersion,
)
from semantic_api.catalog_v1.provider import POLICY, CatalogProvider, candidate_schema
from semantic_api.catalog_v1.trust import (
    Authorization,
    CatalogAccess,
    Owner,
    TrustedContext,
    owner_for,
)
from semantic_api.catalog_v1.validator import CORE_CAPABILITIES, validate
from semantic_api.provider import ProviderError


class CompilerLimits(Frozen):
    timeout_seconds: float = Field(default=20, gt=0, le=120)
    max_context_bytes: int = Field(default=48_000, ge=1, le=128_000)
    max_candidate_bytes: int = Field(default=64_000, ge=1, le=256_000)
    max_total_input_tokens: int = Field(default=65_536, ge=1, le=262_144)
    max_total_output_tokens: int = Field(default=8_192, ge=1, le=32_768)
    clarification_ttl_seconds: int = Field(default=900, ge=1, le=3_600)
    max_clarifications: int = Field(default=8, ge=1, le=16)


class CatalogCompiler:
    def __init__(
        self,
        *,
        catalogs: CatalogRepository,
        authorization: Authorization,
        provider: CatalogProvider,
        clarifications: Clarifications,
        capabilities: tuple[str, ...],
        limits: CompilerLimits | None = None,
    ) -> None:
        if not set(capabilities) <= set(CORE_CAPABILITIES):
            raise CompilerFailure("CAPABILITY_UNSUPPORTED")
        self.catalogs = catalogs
        self.authorization = authorization
        self.provider = provider
        self.clarifications = clarifications
        self.capabilities = tuple(sorted(set(capabilities)))
        self.limits = limits or CompilerLimits()

    def _deadline(self, deadline: float) -> float:
        import math

        now = asyncio.get_running_loop().time()
        if not math.isfinite(deadline) or deadline <= now:
            raise CompilerFailure("COMPILER_DEADLINE")
        return min(deadline, now + self.limits.timeout_seconds)

    async def _context(
        self,
        request: CatalogCompileRequest,
        context: TrustedContext | None,
        answers: tuple[Resolution, ...] = (),
    ) -> tuple[Owner, CompilerContext, str]:
        owner_for(context, request.catalog)
        assert context is not None
        access = await self.authorization.require(context, request.catalog, "compiler:query")
        document = await self.catalogs.get(request.catalog)
        return self._context_from_catalog(request, context, document, access, answers)

    def _context_from_catalog(
        self,
        request: CatalogCompileRequest,
        context: TrustedContext | None,
        document: CatalogDocument,
        access: CatalogAccess,
        answers: tuple[Resolution, ...] = (),
    ) -> tuple[Owner, CompilerContext, str]:
        """Pure context construction; guarded callers supply current transaction-owned grants."""
        owner = owner_for(context, request.catalog)
        if pin_for(document) != request.catalog:
            raise CompilerFailure("CATALOG_PIN_MISMATCH")
        selected = authorized_view(document, access)
        if len(selected.model_dump_json().encode()) > self.limits.max_context_bytes:
            raise CompilerFailure("CONTEXT_LIMIT")
        initialized = initialize(
            request.question, request.catalog, selected, self.capabilities, answers
        )
        if len(initialized.model_dump_json().encode()) > self.limits.max_context_bytes:
            raise CompilerFailure("CONTEXT_LIMIT")
        identity = fingerprint(
            {
                "owner": owner.model_dump(mode="json"),
                "access": {key: sorted(value) for key, value in access.model_dump().items()},
                "capabilities": self.capabilities,
                "prompt": POLICY,
                "schema": candidate_schema(),
                "limits": self.limits.model_dump(mode="json"),
                "provider": getattr(self.provider, "configuration_fingerprint", "injected-test"),
            }
        )
        return owner, initialized, identity

    async def compile(
        self, request: CatalogCompileRequest, *, context: TrustedContext | None, deadline: float
    ) -> Compilation:
        async with asyncio.timeout_at(self._deadline(deadline)):
            owner, initialized, identity = await self._context(request, context)
            expires = datetime.now(UTC) + timedelta(seconds=self.limits.clarification_ttl_seconds)
            identifier = "clarification-" + uuid4().hex
            # Reserve every request before choosing a branch or calling the provider.
            # A cancelled generation leaves an explicit pending record for same-hash retry.
            stored = await self.clarifications.create(
                ClarificationRecord(
                    id=identifier,
                    owner=owner,
                    request=request,
                    context=initialized,
                    authority_fingerprint=identity,
                    expires_at=expires,
                )
            )
            async with self.clarifications.lock(stored.id, owner) as transaction:
                record = transaction.record
                refreshed_owner, initialized, identity = await self._context(request, context)
                if (
                    record.owner != refreshed_owner
                    or record.request != request
                    or record.context != initialized
                    or record.authority_fingerprint != identity
                ):
                    raise CompilerFailure("CLARIFICATION_CONTEXT_CHANGED")
                if await transaction.now() >= record.expires_at:
                    raise CompilerFailure("CLARIFICATION_EXPIRED")
                owner_for(context, request.catalog)
                response = record.current
                if response is None:
                    if initialized.ambiguities:
                        response = Compilation(
                            status="clarification",
                            catalog=request.catalog,
                            clarification_id=record.id,
                            clarification_revision=1,
                            ambiguity=initialized.ambiguities[0],
                            expires_at=record.expires_at,
                        )
                    else:
                        response = await self._generate(initialized)
                await self._unchanged(request, context, identity)
                if await transaction.now() >= record.expires_at:
                    raise CompilerFailure("CLARIFICATION_EXPIRED")
                if record.current is None:
                    await transaction.save(record.model_copy(update={"current": response}))
                return response

    async def _unchanged(
        self,
        request: CatalogCompileRequest,
        context: TrustedContext | None,
        identity: str,
        answers: tuple[Resolution, ...] = (),
    ) -> None:
        _, _, current = await self._context(request, context, answers)
        if current != identity:
            raise CompilerFailure("AUTHORIZATION_CHANGED")

    async def resume(
        self,
        clarification_id: str,
        answer_choice_id: str,
        *,
        revision: int,
        context: TrustedContext | None,
        pin: ResourceVersion,
        deadline: float,
    ) -> Compilation:
        async with asyncio.timeout_at(self._deadline(deadline)):
            owner = owner_for(context, pin)
            assert context is not None
            # Revoke before reading even an opaque ID; do not trust historical grants.
            await self.authorization.require(context, pin, "compiler:query")
            async with self.clarifications.lock(clarification_id, owner) as transaction:
                record = transaction.record
                if record.request.catalog != pin:
                    raise CompilerFailure("CLARIFICATION_NOT_AVAILABLE")
                current = record.current
                if current is None:
                    raise CompilerFailure("CLARIFICATION_NOT_AVAILABLE")
                _, initialized, identity = await self._context(record.request, context)
                if identity != record.authority_fingerprint or initialized != record.context:
                    raise CompilerFailure("CLARIFICATION_CONTEXT_CHANGED")
                if type(revision) is not int or revision < 1:
                    raise CompilerFailure("CLARIFICATION_REVISION")
                if revision <= len(record.history):
                    previous = record.history[revision - 1]
                    if previous.choice_id != answer_choice_id:
                        raise CompilerFailure("IDEMPOTENCY_CONFLICT")
                    if await transaction.now() >= record.expires_at:
                        raise CompilerFailure("CLARIFICATION_EXPIRED")
                    return previous.response
                if revision != len(record.history) + 1 or current.ambiguity is None:
                    raise CompilerFailure("CLARIFICATION_REVISION")
                choice = next(
                    (c for c in current.ambiguity.choices if c.id == answer_choice_id), None
                )
                if choice is None:
                    raise CompilerFailure("CLARIFICATION_CHOICE")
                answers = (
                    *record.answers,
                    Resolution(term=current.ambiguity.term, choice=choice),
                )
                _, resumed, _ = await self._context(record.request, context, answers)
                if resumed.ambiguities:
                    if len(answers) >= self.limits.max_clarifications:
                        raise CompilerFailure("CLARIFICATION_LIMIT")
                    response = Compilation(
                        status="clarification",
                        catalog=pin,
                        clarification_id=record.id,
                        clarification_revision=revision + 1,
                        ambiguity=resumed.ambiguities[0],
                        expires_at=record.expires_at,
                    )
                else:
                    response = await self._generate(resumed)
                await self._unchanged(record.request, context, identity, answers)
                if await transaction.now() >= record.expires_at:
                    raise CompilerFailure("CLARIFICATION_EXPIRED")
                await transaction.save(
                    record.model_copy(
                        update={
                            "answers": answers,
                            "current": response,
                            "history": (
                                *record.history,
                                Answered(choice_id=answer_choice_id, response=response),
                            ),
                        }
                    )
                )
                return response

    async def _generate(self, context: CompilerContext) -> Compilation:
        provider, limits = self.provider, self.limits
        calls: list[CallMetadata] = []
        rejected: dict[str, Any] | None = None
        diagnostics: tuple[str, ...] = ()
        settings = getattr(provider, "settings", None)
        if settings is not None and (
            settings.max_input_tokens > limits.max_total_input_tokens
            or settings.max_output_tokens > limits.max_total_output_tokens
        ):
            return Compilation(
                status="blocked", catalog=context.catalog, diagnostics=("TOKEN_BUDGET",)
            )
        for phase in ("compile", "repair"):
            try:
                result = await provider.invoke(
                    context, phase=phase, rejected=rejected, diagnostics=diagnostics
                )
            except ProviderError as error:
                metadata = error.metadata
                calls.append(
                    CallMetadata(
                        provider=metadata.provider if metadata else None,
                        model=metadata.model if metadata else "unknown",
                        deployment=metadata.deployment if metadata else None,
                        phase=phase,
                        outcome=error.code,
                        input_tokens=metadata.input_tokens if metadata else None,
                        output_tokens=metadata.output_tokens if metadata else None,
                    )
                )
                return self._response(context, "blocked", calls, (error.code,))
            metadata = result.metadata
            calls.append(
                CallMetadata(
                    provider=metadata.provider if metadata else None,
                    model=metadata.model if metadata else "injected-test-provider",
                    deployment=metadata.deployment if metadata else None,
                    phase=phase,
                    outcome=metadata.outcome if metadata else "succeeded",
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                )
            )
            known_input = sum(c.input_tokens or 0 for c in calls)
            known_output = sum(c.output_tokens or 0 for c in calls)
            if (
                known_input > limits.max_total_input_tokens
                or known_output > limits.max_total_output_tokens
            ):
                return self._response(context, "blocked", calls, ("TOKEN_BUDGET",))
            rejected = result.candidate
            try:
                import json

                if len(json.dumps(rejected, allow_nan=False).encode()) > limits.max_candidate_bytes:
                    return self._response(context, "blocked", calls, ("CANDIDATE_LIMIT",))
                candidate = Candidate.model_validate(rejected)
                if candidate.status != "graph":
                    if candidate.graph is not None or candidate.ambiguity_id is not None:
                        raise CompilerFailure("CANDIDATE_SHAPE")
                    return self._response(context, "blocked", calls, ("QUESTION_UNRESOLVED",))
                if candidate.graph is None or candidate.ambiguity_id is not None:
                    raise CompilerFailure("CANDIDATE_SHAPE")
                graph = validate(candidate.graph, context)
                return self._response(context, "compiled", calls, ()).model_copy(
                    update={"graph": graph}
                )
            except ValidationError:
                diagnostics = ("CANDIDATE_SCHEMA",)
            except CompilerFailure as error:
                diagnostics = (error.code,)
            except (ValueError, TypeError, RecursionError):
                return self._response(context, "blocked", calls, ("CANDIDATE_ENCODING",))
            # Reserve a complete per-call allocation before admitting a repair.
            if (
                phase == "compile"
                and settings is not None
                and (
                    known_input + settings.max_input_tokens > limits.max_total_input_tokens
                    or known_output + settings.max_output_tokens > limits.max_total_output_tokens
                )
            ):
                return self._response(context, "blocked", calls, ("REPAIR_BUDGET",))
        return self._response(context, "blocked", calls, (*diagnostics, "REPAIR_FAILED"))

    @staticmethod
    def _response(
        context: CompilerContext,
        status: str,
        calls: list[CallMetadata],
        diagnostics: tuple[str, ...],
    ) -> Compilation:
        return Compilation.model_validate(
            {
                "status": status,
                "catalog": context.catalog,
                "resolutions": context.resolutions,
                "calls": tuple(calls),
                "diagnostics": diagnostics,
                "repair_attempted": len(calls) > 1,
                "input_tokens": (
                    sum(c.input_tokens for c in calls if c.input_tokens is not None)
                    if all(c.input_tokens is not None for c in calls)
                    else None
                ),
                "output_tokens": (
                    sum(c.output_tokens for c in calls if c.output_tokens is not None)
                    if all(c.output_tokens is not None for c in calls)
                    else None
                ),
            }
        )
