from __future__ import annotations

from collections.abc import Callable
from time import perf_counter

from semantic_api.initializer import DeterministicInitializer
from semantic_api.models import (
    CompileRequest,
    CompileResponse,
    CompileStatus,
    Diagnostic,
    DiagnosticSeverity,
    DiagnosticStage,
    InitializeRequest,
    InitializeResponse,
    ProviderSelection,
    TimingMetadata,
    TokenMetadata,
)
from semantic_api.ontology import OntologyRegistry
from semantic_api.provider import (
    CompilerProvider,
    ProviderInvoker,
    ProviderResult,
    ProviderTimeoutError,
    StaticFixtureProvider,
    StructuredCompileContext,
    UntrustedQuestion,
)
from semantic_api.validator import SQGValidator


class SemanticCompiler:
    def __init__(
        self,
        registry: OntologyRegistry,
        provider_factory: Callable[[ProviderSelection], CompilerProvider] | None = None,
        *,
        provider_timeout_seconds: float = 5,
        max_output_tokens: int = 2_048,
    ) -> None:
        self.registry = registry
        self.initializer = DeterministicInitializer(registry)
        self.validator = SQGValidator(registry)
        self.provider_factory = provider_factory or (lambda _: StaticFixtureProvider())
        self.provider_timeout_seconds = provider_timeout_seconds
        self.max_output_tokens = max_output_tokens

    @classmethod
    def default(cls) -> SemanticCompiler:
        return cls(OntologyRegistry.load_default())

    @property
    def ready(self) -> bool:
        return bool(self.registry.document.version)

    def initialize(self, request: InitializeRequest, correlation_id: str) -> InitializeResponse:
        return self.initializer.initialize(request, correlation_id)

    async def compile(self, request: CompileRequest, correlation_id: str) -> CompileResponse:
        total_start = perf_counter()
        initialization_start = perf_counter()
        initialize_request = InitializeRequest.model_validate(request.model_dump())
        initialization = self.initializer.initialize(initialize_request, correlation_id)
        initialization_ms = self._elapsed_ms(initialization_start)
        empty_timing = TimingMetadata(
            initialization_ms=initialization_ms,
            provider_ms=0,
            validation_ms=0,
            total_ms=self._elapsed_ms(total_start),
        )
        empty_tokens = TokenMetadata(max_output_tokens=self.max_output_tokens)
        if initialization.status is not CompileStatus.SUCCEEDED:
            return CompileResponse(
                status=initialization.status,
                correlation_id=correlation_id,
                resolved_terms=initialization.resolved_terms,
                selected_semantic_context=initialization.selected_semantic_context,
                candidate_sqg=None,
                normalized_sqg=None,
                diagnostics=initialization.diagnostics,
                token_metadata=empty_tokens,
                timing_metadata=empty_timing,
            )

        authoritative_json = initialization.model_dump_json()
        authoritative = InitializeResponse.model_validate_json(authoritative_json)
        context = StructuredCompileContext(
            question=UntrustedQuestion(value=request.question),
            compilation_mode=request.compilation_mode,
            resolved_terms=authoritative.resolved_terms,
            time_windows=authoritative.time_windows,
            semantic_context=authoritative.selected_semantic_context,
        )
        provider = self.provider_factory(request.provider_selection)
        invoker = ProviderInvoker(provider, self.provider_timeout_seconds)
        provider_start = perf_counter()
        try:
            provider_result = await invoker.compile(context)
        except ProviderTimeoutError:
            provider_ms = self._elapsed_ms(provider_start)
            return self._failure(
                correlation_id=correlation_id,
                initialization=authoritative,
                candidate=None,
                diagnostics=[
                    *authoritative.diagnostics,
                    Diagnostic(
                        code="PROVIDER_TIMEOUT",
                        severity=DiagnosticSeverity.ERROR,
                        stage=DiagnosticStage.PROVIDER,
                        message="The compiler provider exceeded the configured timeout.",
                    ),
                ],
                initialization_ms=initialization_ms,
                provider_ms=provider_ms,
                validation_ms=0,
                total_start=total_start,
                tokens=empty_tokens,
            )
        provider_ms = self._elapsed_ms(provider_start)

        validation_start = perf_counter()
        validation = self.validator.validate(
            provider_result.candidate,
            authoritative.selected_semantic_context,
            authoritative.resolved_terms,
            authoritative.time_windows,
            request.compilation_mode,
        )
        validation_ms = self._elapsed_ms(validation_start)
        tokens = self._tokens(provider_result)
        if validation.valid:
            return CompileResponse(
                status=CompileStatus.SUCCEEDED,
                correlation_id=correlation_id,
                resolved_terms=authoritative.resolved_terms,
                selected_semantic_context=authoritative.selected_semantic_context,
                candidate_sqg=provider_result.candidate,
                normalized_sqg=validation.normalized,
                diagnostics=[*authoritative.diagnostics, *validation.diagnostics],
                token_metadata=tokens,
                timing_metadata=TimingMetadata(
                    initialization_ms=initialization_ms,
                    provider_ms=provider_ms,
                    validation_ms=validation_ms,
                    total_ms=self._elapsed_ms(total_start),
                ),
            )

        repair_start = perf_counter()
        try:
            repair_result = await invoker.repair(
                context, provider_result.candidate, validation.diagnostics
            )
        except ProviderTimeoutError:
            provider_ms += self._elapsed_ms(repair_start)
            return self._failure(
                correlation_id=correlation_id,
                initialization=authoritative,
                candidate=provider_result.candidate,
                diagnostics=[
                    *authoritative.diagnostics,
                    *validation.diagnostics,
                    Diagnostic(
                        code="REPAIR_TIMEOUT",
                        severity=DiagnosticSeverity.ERROR,
                        stage=DiagnosticStage.REPAIR,
                        message="The single structured repair attempt timed out.",
                    ),
                ],
                initialization_ms=initialization_ms,
                provider_ms=provider_ms,
                validation_ms=validation_ms,
                total_start=total_start,
                tokens=tokens,
                repair_attempted=True,
            )
        provider_ms += self._elapsed_ms(repair_start)
        repair_validation_start = perf_counter()
        repaired = self.validator.validate(
            repair_result.candidate,
            authoritative.selected_semantic_context,
            authoritative.resolved_terms,
            authoritative.time_windows,
            request.compilation_mode,
        )
        validation_ms += self._elapsed_ms(repair_validation_start)
        repair_tokens = self._tokens(repair_result)
        tokens = TokenMetadata(
            input_tokens=self._sum_optional(tokens.input_tokens, repair_tokens.input_tokens),
            output_tokens=self._sum_optional(tokens.output_tokens, repair_tokens.output_tokens),
            max_output_tokens=self.max_output_tokens,
        )
        if repaired.valid:
            return CompileResponse(
                status=CompileStatus.SUCCEEDED,
                correlation_id=correlation_id,
                resolved_terms=authoritative.resolved_terms,
                selected_semantic_context=authoritative.selected_semantic_context,
                candidate_sqg=repair_result.candidate,
                normalized_sqg=repaired.normalized,
                diagnostics=[
                    *authoritative.diagnostics,
                    Diagnostic(
                        code="REPAIR_APPLIED",
                        severity=DiagnosticSeverity.INFO,
                        stage=DiagnosticStage.REPAIR,
                        message="The provider produced a valid SQG on its single repair attempt.",
                    ),
                ],
                token_metadata=tokens,
                timing_metadata=TimingMetadata(
                    initialization_ms=initialization_ms,
                    provider_ms=provider_ms,
                    validation_ms=validation_ms,
                    total_ms=self._elapsed_ms(total_start),
                ),
                repair_attempted=True,
            )
        return self._failure(
            correlation_id=correlation_id,
            initialization=authoritative,
            candidate=repair_result.candidate,
            diagnostics=[
                *authoritative.diagnostics,
                *repaired.diagnostics,
                Diagnostic(
                    code="REPAIR_FAILED",
                    severity=DiagnosticSeverity.ERROR,
                    stage=DiagnosticStage.REPAIR,
                    message="The candidate remained invalid after one structured repair attempt.",
                ),
            ],
            initialization_ms=initialization_ms,
            provider_ms=provider_ms,
            validation_ms=validation_ms,
            total_start=total_start,
            tokens=tokens,
            repair_attempted=True,
        )

    def _failure(
        self,
        *,
        correlation_id: str,
        initialization: InitializeResponse,
        candidate: dict[str, object] | None,
        diagnostics: list[Diagnostic],
        initialization_ms: float,
        provider_ms: float,
        validation_ms: float,
        total_start: float,
        tokens: TokenMetadata,
        repair_attempted: bool = False,
    ) -> CompileResponse:
        return CompileResponse(
            status=CompileStatus.FAILED,
            correlation_id=correlation_id,
            resolved_terms=initialization.resolved_terms,
            selected_semantic_context=initialization.selected_semantic_context,
            candidate_sqg=candidate,
            normalized_sqg=None,
            diagnostics=diagnostics,
            token_metadata=tokens,
            timing_metadata=TimingMetadata(
                initialization_ms=initialization_ms,
                provider_ms=provider_ms,
                validation_ms=validation_ms,
                total_ms=self._elapsed_ms(total_start),
            ),
            repair_attempted=repair_attempted,
        )

    def _tokens(self, result: ProviderResult) -> TokenMetadata:
        return TokenMetadata(
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            max_output_tokens=self.max_output_tokens,
        )

    @staticmethod
    def _sum_optional(left: int | None, right: int | None) -> int | None:
        if left is None or right is None:
            return None
        return left + right

    @staticmethod
    def _elapsed_ms(start: float) -> float:
        return max(0.0, (perf_counter() - start) * 1_000)
