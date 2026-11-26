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
    "approval_expired": "browser approval has expired; prepare a new action",
    "approval_stale": "browser approval no longer matches the page; prepare a new action",
    "approval_unavailable": "browser approval is unavailable or cannot be prepared",
    "stale_control": "dropdown reference is stale or unavailable; inspect controls again",
    "unsupported_control": "dropdown or option is not supported for this operation",
    "ambiguous_control": "dropdown or option is ambiguous",
    "blocked_access": "page access is blocked by the public-network policy",
    "site_unreachable": "the site couldn't be reached (its name didn't resolve); check the address or try again",
    "not_a_check": "not clicked: the page isn't a bot check (or the point isn't in its CAPTCHA's frame)",
    "artifact_error": "browser artifact is unavailable",
    "session_busy": "browser session is busy; try again shortly",
    "stale_ref": "element reference is unknown or gone from the page; list elements again",
    "point_outside": "that point is outside the visible page",
    "point_in_frame": "that point is inside an embedded frame; use frames and frame_id",
    "stale_capture": "that point no longer shows what the screenshot did (something moved, scrolled, or was replaced); take a new one",
    "native_select": "that is a dropdown list (a select): choose its option with select_option, its ref, and one of the options elements lists for it",
    "native_picker": "that field's picker opens outside the page: type its value instead (date YYYY-MM-DD, time HH:MM, date and time YYYY-MM-DDTHH:MM, month YYYY-MM, week YYYY-Www, colour #rrggbb)",
    "not_a_select": "select_option by ref is for a dropdown list (a select); click a custom dropdown, then click its option",
    "unknown_option": "that dropdown list has no option with exactly that label; elements lists its options",
    "stale_picker": "that dropdown list changed or is gone; open it again",
}


class WorkerError(RuntimeError):
    def __init__(self, message: str, *, code: str = "operation_failed") -> None:
        super().__init__(message)
        self.code = code if code in ERROR_MESSAGES else "operation_failed"


def fail(code: str) -> WorkerError:
    return WorkerError(ERROR_MESSAGES[code], code=code)
