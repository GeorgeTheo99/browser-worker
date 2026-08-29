from __future__ import annotations

import pytest

import auth
from auth import Caller
from server import browser_fetch, browser_inspect


@pytest.mark.asyncio
async def test_read_only_caller_cannot_interact_script_or_create_artifacts() -> None:
    token = auth._current_caller.set(Caller("my-ai-staging", frozenset({"fetch", "inspect.read"})))
    try:
        for kwargs in [
            {"action": "click", "session_id": "missing", "selector": "button"},
            {"action": "type", "session_id": "missing", "selector": "input", "text": "value"},
            {"action": "evaluate", "session_id": "missing", "script": "1+1"},
            {"action": "screenshot", "session_id": "missing"},
            {"action": "export_pdf", "session_id": "missing"},
        ]:
            result = await browser_inspect(**kwargs)
            assert result.is_error
            assert result.structured_content == {
                "status": "error",
                "error": "caller capability does not allow this operation",
            }
        screenshot = await browser_fetch("https://example.com", include_screenshot=True)
        assert screenshot.is_error
        assert screenshot.structured_content["error"] == "caller capability does not allow this operation"
    finally:
        auth._current_caller.reset(token)
