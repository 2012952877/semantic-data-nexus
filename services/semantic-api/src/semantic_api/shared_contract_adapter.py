from __future__ import annotations

from typing import Protocol, TypeVar

from pydantic import BaseModel

from semantic_api.models import CompileRequest, CompileResponse

SharedRequest = TypeVar("SharedRequest", bound=BaseModel, contravariant=True)
SharedResponse = TypeVar("SharedResponse", bound=BaseModel, covariant=True)


class SharedContractAdapter(Protocol[SharedRequest, SharedResponse]):
    """Boundary for replacing internal v0 types with foundation contracts."""

    def to_internal_request(self, request: SharedRequest) -> CompileRequest: ...

    def from_internal_response(self, response: CompileResponse) -> SharedResponse: ...


class InternalV0Adapter:
    def to_internal_request(self, request: CompileRequest) -> CompileRequest:
        return request

    def from_internal_response(self, response: CompileResponse) -> CompileResponse:
        return response
