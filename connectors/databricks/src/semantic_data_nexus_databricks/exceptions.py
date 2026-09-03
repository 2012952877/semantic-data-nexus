class DatabricksResolverError(Exception):
    """Base error for the package."""


class ConfigurationError(DatabricksResolverError):
    """Resolver configuration is invalid."""


class AuthenticationError(DatabricksResolverError):
    """An authentication value is unavailable or invalid."""


class TransportHttpError(DatabricksResolverError):
    def __init__(self, status_code: int, request_id: str | None = None) -> None:
        self.status_code = status_code
        self.request_id = request_id
        suffix = f" (request_id={request_id})" if request_id else ""
        super().__init__(f"Databricks HTTP request failed with status {status_code}{suffix}")


class TransportError(DatabricksResolverError):
    """A transport operation failed without exposing request data."""


class TransportTimeoutError(TransportError):
    """A transport operation exceeded its request timeout."""


class ProtocolError(DatabricksResolverError):
    """The service response does not satisfy the documented API contract."""


class ResultDecodeError(DatabricksResolverError):
    """A result chunk cannot be decoded without exposing its contents."""


class ResultLimitExceededError(DatabricksResolverError):
    """The result exceeded a configured row or byte boundary."""


class StatementTimeoutError(DatabricksResolverError):
    def __init__(self, statement_id: str | None) -> None:
        self.statement_id = statement_id
        if statement_id is None:
            super().__init__("Statement submission exceeded its execution deadline")
        else:
            super().__init__(f"Statement {statement_id} exceeded its execution deadline")


class StatementFailedError(DatabricksResolverError):
    def __init__(
        self,
        statement_id: str,
        error_code: str | None,
        sql_state: str | None,
    ) -> None:
        self.statement_id = statement_id
        self.error_code = error_code
        self.sql_state = sql_state
        details = ", ".join(
            part
            for part in (
                f"error_code={error_code}" if error_code else "",
                f"sql_state={sql_state}" if sql_state else "",
            )
            if part
        )
        suffix = f" ({details})" if details else ""
        super().__init__(f"Statement {statement_id} failed{suffix}")


class StatementCanceledError(DatabricksResolverError):
    def __init__(self, statement_id: str) -> None:
        self.statement_id = statement_id
        super().__init__(f"Statement {statement_id} was canceled")


class StatementClosedError(DatabricksResolverError):
    def __init__(self, statement_id: str) -> None:
        self.statement_id = statement_id
        super().__init__(f"Statement {statement_id} is closed and its result is unavailable")


class UnsafeStatementError(DatabricksResolverError):
    """The physical fragment is not one conservative read-only query."""
