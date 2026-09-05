"""Short guarded prepare/commit transactions with model work strictly between them."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg

from semantic_api.catalog_v1.clarification import Answered, ClarificationRecord
from semantic_api.catalog_v1.compiler import CatalogCompiler, CompilerConfiguration
from semantic_api.catalog_v1.guard import (
    AuthorizationDecision,
    AuthorizationGuard,
    GuardedCatalogRepository,
)
from semantic_api.catalog_v1.guarded_store import (
    Attempt,
    Connection,
    GuardedClarifications,
    Reservation,
    StoredRecord,
    action_hash,
)
from semantic_api.catalog_v1.models import (
    CatalogCompileRequest,
    CatalogDocument,
    Compilation,
    CompilerContext,
    CompilerFailure,
    Resolution,
    ResourceVersion,
)
from semantic_api.catalog_v1.trust import CatalogAccess, TrustedContext, owner_for
from semantic_api.catalog_v1.validator import validate


@dataclass(frozen=True)
class Rejected:
    code: str


@dataclass(frozen=True)
class Dispatch:
    record: ClarificationRecord
    attempt: Attempt
    context: CompilerContext
    answers: tuple[Resolution, ...]
    configuration: CompilerConfiguration


Preparation = Compilation | Rejected | Dispatch


class GuardedCatalogCompiler:
    def __init__(
        self,
        *,
        core: CatalogCompiler,
        guard: AuthorizationGuard,
        catalogs: GuardedCatalogRepository,
        store: GuardedClarifications,
    ) -> None:
        self.core = core
        self.guard = guard
        self.catalogs = catalogs
        self.store = store

    @staticmethod
    def _access(decision: AuthorizationDecision) -> CatalogAccess:
        access = decision.access
        if access is None:
            raise CompilerFailure("RESOURCE_NOT_AVAILABLE")
        return CatalogAccess(
            entity_ids=access.entity_ids,
            field_ids=access.field_ids,
            metric_ids=access.metric_ids,
            relation_ids=access.relation_ids,
            member_ids=access.member_ids,
        )

    def _fresh(
        self,
        stored: StoredRecord,
        context: TrustedContext,
        document: CatalogDocument,
        access: CatalogAccess,
        configuration: CompilerConfiguration | None = None,
    ) -> CompilerContext:
        owner, initialized, identity = self.core._context_from_catalog(
            stored.record.request, context, document, access, configuration=configuration
        )
        if (
            owner != stored.record.owner
            or initialized != stored.record.context
            or identity != stored.record.authority_fingerprint
        ):
            raise CompilerFailure("CLARIFICATION_CONTEXT_CHANGED")
        return initialized

    async def _adopt(
        self,
        connection: Connection,
        stored: StoredRecord,
    ) -> StoredRecord:
        if stored.guarded_version == 0:
            stored = await self.store.adopt(connection, stored)
            if stored.record.current is None:
                await self.store.claim(
                    connection,
                    stored,
                    step=0,
                    choice=None,
                    deadline_at=None,
                    reservation=None,
                )
                stored = await self.store.lock(connection, stored.record.id, stored.record.owner)
        return stored

    async def _unavailable_attempt(
        self,
        connection: Connection,
        attempt: Attempt,
    ) -> Rejected:
        if attempt.state == "outcome_unknown":
            return Rejected("COMPILATION_OUTCOME_UNKNOWN")
        now = await self.store.now(connection)
        if attempt.lease_until is None or now >= attempt.lease_until:
            await self.store.mark_unknown(connection, attempt)
            return Rejected("COMPILATION_OUTCOME_UNKNOWN")
        return Rejected("COMPILATION_IN_PROGRESS")

    async def _record_deadline(
        self,
        connection: Connection,
        record: ClarificationRecord,
        context: TrustedContext,
        deadline: float,
    ) -> float:
        now = await self.store.now(connection)
        loop_now = asyncio.get_running_loop().time()
        remaining = min(
            deadline - loop_now,
            (record.expires_at - now).total_seconds(),
            (context.authentication.expires_at - now).total_seconds(),
            (context.authentication.expires_at - datetime.now(UTC)).total_seconds(),
        )
        if remaining <= 0:
            raise CompilerFailure("CLARIFICATION_EXPIRED")
        return loop_now + remaining

    async def compile(
        self,
        request: CatalogCompileRequest,
        *,
        context: TrustedContext | None,
        deadline: float,
    ) -> Compilation:
        owner = owner_for(context, request.catalog)
        assert context is not None
        limit = self.core._deadline(deadline)
        try:
            async with asyncio.timeout_at(limit) as whole:
                async with self.guard.guard(
                    context, request.catalog, "compiler:query", deadline=limit
                ) as decision:
                    document = await self.catalogs.get(decision.connection, request.catalog)
                    access = self._access(decision)
                    configuration = self.core.configuration()
                    owner, initialized, identity = self.core._context_from_catalog(
                        request, context, document, access, configuration=configuration
                    )
                    now = await self.store.now(decision.connection)
                    stored = await self.store.create(
                        decision.connection,
                        ClarificationRecord(
                            id="clarification-" + uuid4().hex,
                            owner=owner,
                            request=request,
                            context=initialized,
                            authority_fingerprint=identity,
                            expires_at=now
                            + timedelta(seconds=configuration.limits.clarification_ttl_seconds),
                        ),
                    )
                    self._fresh(stored, context, document, access, configuration)
                    limit = await self._record_deadline(
                        decision.connection, stored.record, context, limit
                    )
                    whole.reschedule(limit)
                    stored = await self._adopt(decision.connection, stored)
                    active = await self.store.active(decision.connection, stored.record.id)
                    if active is not None:
                        prepared: Preparation = await self._unavailable_attempt(
                            decision.connection, active
                        )
                    elif stored.record.current is not None:
                        prepared = stored.record.current
                    else:
                        prepared = await self._prepare(
                            decision.connection,
                            stored,
                            initialized,
                            (),
                            0,
                            None,
                            context,
                            limit,
                            configuration,
                        )
                return await self._dispatch(prepared, context, limit)
        except psycopg.Error:
            raise CompilerFailure("CLARIFICATION_STORE_UNAVAILABLE") from None

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
        owner = owner_for(context, pin)
        assert context is not None
        if type(revision) is not int or revision < 1:
            raise CompilerFailure("CLARIFICATION_REVISION")
        limit = self.core._deadline(deadline)
        try:
            async with asyncio.timeout_at(limit) as whole:
                async with self.guard.guard(
                    context, pin, "compiler:query", deadline=limit
                ) as decision:
                    document = await self.catalogs.get(decision.connection, pin)
                    access = self._access(decision)
                    stored = await self.store.lock(decision.connection, clarification_id, owner)
                    record = stored.record
                    configuration = self.core.configuration()
                    if record.request.catalog != pin:
                        raise CompilerFailure("CLARIFICATION_NOT_AVAILABLE")
                    self._fresh(stored, context, document, access, configuration)
                    limit = await self._record_deadline(decision.connection, record, context, limit)
                    whole.reschedule(limit)
                    stored = await self._adopt(decision.connection, stored)
                    if revision <= len(record.history):
                        previous = record.history[revision - 1]
                        if previous.choice_id != answer_choice_id:
                            raise CompilerFailure("IDEMPOTENCY_CONFLICT")
                        prepared: Preparation = previous.response
                    else:
                        if (
                            record.current is None
                            or record.current.ambiguity is None
                            or revision != len(record.history) + 1
                        ):
                            raise CompilerFailure("CLARIFICATION_REVISION")
                        choice = next(
                            (
                                c
                                for c in record.current.ambiguity.choices
                                if c.id == answer_choice_id
                            ),
                            None,
                        )
                        if choice is None:
                            raise CompilerFailure("CLARIFICATION_CHOICE")
                        existing = await self.store.attempt(
                            decision.connection, record.id, revision
                        )
                        if existing is not None:
                            if existing.action_hash != action_hash(
                                record, revision, answer_choice_id
                            ):
                                raise CompilerFailure("IDEMPOTENCY_CONFLICT")
                            if existing.state == "completed":
                                raise CompilerFailure("CLARIFICATION_STORE_INVALID")
                            prepared = await self._unavailable_attempt(
                                decision.connection, existing
                            )
                        else:
                            active = await self.store.active(decision.connection, record.id)
                            if active is not None:
                                prepared = await self._unavailable_attempt(
                                    decision.connection, active
                                )
                            else:
                                answers = (
                                    *record.answers,
                                    Resolution(term=record.current.ambiguity.term, choice=choice),
                                )
                                _, resumed, _ = self.core._context_from_catalog(
                                    record.request,
                                    context,
                                    document,
                                    access,
                                    answers,
                                    configuration=configuration,
                                )
                                prepared = await self._prepare(
                                    decision.connection,
                                    stored,
                                    resumed,
                                    answers,
                                    revision,
                                    answer_choice_id,
                                    context,
                                    limit,
                                    configuration,
                                )
                return await self._dispatch(prepared, context, limit)
        except psycopg.Error:
            raise CompilerFailure("CLARIFICATION_STORE_UNAVAILABLE") from None

    async def _prepare(
        self,
        connection: Connection,
        stored: StoredRecord,
        initialized: CompilerContext,
        answers: tuple[Resolution, ...],
        step: int,
        choice: str | None,
        context: TrustedContext,
        deadline: float,
        configuration: CompilerConfiguration,
    ) -> Preparation:
        now = await self.store.now(connection)
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise CompilerFailure("COMPILER_DEADLINE")
        deadline_at = min(
            now + timedelta(seconds=remaining),
            context.authentication.expires_at,
            stored.record.expires_at,
        )
        if deadline_at <= now:
            raise CompilerFailure("CLARIFICATION_EXPIRED")
        if initialized.ambiguities and len(answers) >= configuration.limits.max_clarifications:
            raise CompilerFailure("CLARIFICATION_LIMIT")
        immediate = bool(initialized.ambiguities)
        reservation = Reservation(
            input_tokens=0 if immediate else configuration.limits.max_total_input_tokens,
            output_tokens=0 if immediate else configuration.limits.max_total_output_tokens,
            provider_calls=0 if immediate else 2,
        )
        attempt = await self.store.claim(
            connection,
            stored,
            step=step,
            choice=choice,
            deadline_at=deadline_at,
            reservation=reservation,
        )
        if not immediate:
            return Dispatch(
                stored.record,
                attempt,
                initialized,
                answers,
                configuration,
            )
        response = Compilation(
            status="clarification",
            catalog=stored.record.request.catalog,
            clarification_id=stored.record.id,
            clarification_revision=step + 1,
            ambiguity=initialized.ambiguities[0],
            expires_at=stored.record.expires_at,
        )
        updated = self._updated(stored.record, response, step, choice, answers)
        await self.store.finish(connection, stored, attempt, updated, response)
        return response

    @staticmethod
    def _updated(
        record: ClarificationRecord,
        response: Compilation,
        step: int,
        choice: str | None,
        answers: tuple[Resolution, ...],
    ) -> ClarificationRecord:
        history = record.history
        if step:
            if choice is None or step != len(history) + 1:
                raise CompilerFailure("CLARIFICATION_REVISION")
            history = (*history, Answered(choice_id=choice, response=response))
        return record.model_copy(
            update={
                "current": response,
                "answers": answers,
                "history": history,
            }
        )

    async def _dispatch(
        self,
        prepared: Preparation,
        context: TrustedContext,
        deadline: float,
    ) -> Compilation:
        if isinstance(prepared, Rejected):
            raise CompilerFailure(prepared.code)
        if isinstance(prepared, Compilation):
            return prepared
        if asyncio.get_running_loop().time() >= deadline:
            raise CompilerFailure("COMPILER_DEADLINE")
        if not self.core.configuration_matches(prepared.configuration):
            raise CompilerFailure("COMPILER_CONFIGURATION_CHANGED")
        # No guard or connection is live in this task while the provider runs.
        # Cancellation leaves the committed fence/reservation intact, never reset/retried.
        response = await self.core._generate(prepared.context, configuration=prepared.configuration)
        if asyncio.get_running_loop().time() >= deadline:
            raise CompilerFailure("COMPILER_DEADLINE")
        async with self.guard.guard(
            context, prepared.record.request.catalog, "compiler:query", deadline=deadline
        ) as decision:
            document = await self.catalogs.get(decision.connection, prepared.record.request.catalog)
            access = self._access(decision)
            owner = owner_for(context, prepared.record.request.catalog)
            stored = await self.store.lock(decision.connection, prepared.record.id, owner)
            self._fresh(stored, context, document, access)
            _, fresh_context, _ = self.core._context_from_catalog(
                stored.record.request, context, document, access, prepared.answers
            )
            if fresh_context != prepared.context:
                raise CompilerFailure("CLARIFICATION_CONTEXT_CHANGED")
            current = await self.store.attempt(
                decision.connection, stored.record.id, prepared.attempt.step
            )
            if current != prepared.attempt:
                raise CompilerFailure("ATTEMPT_FENCED")
            now = await self.store.now(decision.connection)
            if (
                current.state != "in_flight"
                or current.lease_until is None
                or current.deadline_at is None
                or now >= current.lease_until
                or now >= current.deadline_at
            ):
                await self.store.mark_unknown(decision.connection, current)
                committed: Compilation | Rejected = Rejected("COMPILATION_OUTCOME_UNKNOWN")
            else:
                if response.catalog != stored.record.request.catalog:
                    raise CompilerFailure("CATALOG_PIN_MISMATCH")
                if response.status == "compiled":
                    if response.graph is None or response.resolutions != fresh_context.resolutions:
                        raise CompilerFailure("CANDIDATE_SHAPE")
                    validate(response.graph, fresh_context)
                elif response.status != "blocked" or response.graph is not None:
                    raise CompilerFailure("CANDIDATE_SHAPE")
                choice = prepared.answers[-1].choice.id if current.step else None
                if current.action_hash != action_hash(stored.record, current.step, choice):
                    raise CompilerFailure("ATTEMPT_FENCED")
                updated = self._updated(
                    stored.record, response, current.step, choice, prepared.answers
                )
                await self.store.finish(decision.connection, stored, current, updated, response)
                committed = response
        if isinstance(committed, Rejected):
            raise CompilerFailure(committed.code)
        return committed
