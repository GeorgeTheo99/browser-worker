"""Stable, privacy-safe errors at the MCP boundary (never serialize exceptions)."""

ERROR_MESSAGES = {
    "operation_failed": "browser operation failed",
    "invalid_request": "browser action parameters are invalid",
    "capability_denied": "caller capability does not allow this operation",
    "invalid_session": "browser session is invalid or expired",
    "selector_not_found": "selector did not match an element",
    "selector_ambiguous": "selector matched multiple elements",
    "invalid_selector": "selector is invalid",
    "extraction_timeout": "page extraction exceeded its deadline",
    "operation_timeout": "browser operation exceeded its deadline",
    "stale_control": "dropdown reference is stale or unavailable; inspect controls again",
    "unsupported_control": "dropdown or option is not supported for this operation",
    "ambiguous_control": "dropdown or option is ambiguous",
    "blocked_access": "page access is blocked; do not attempt to bypass the restriction",
    "artifact_error": "browser artifact is unavailable",
}


class WorkerError(RuntimeError):
    def __init__(self, message: str, *, code: str = "operation_failed") -> None:
        super().__init__(message)
        self.code = code if code in ERROR_MESSAGES else "operation_failed"


def fail(code: str) -> WorkerError:
    return WorkerError(ERROR_MESSAGES[code], code=code)
