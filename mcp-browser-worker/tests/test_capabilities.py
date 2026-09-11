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
            {"action": "prepare_action", "session_id": "missing", "operation": "click", "selector": "button"},
            {"action": "execute_prepared", "session_id": "missing", "proposal_id": "opaque"},
            {"action": "discard_prepared", "session_id": "missing", "proposal_id": "opaque"},
            {"action": "expand_control", "session_id": "missing", "control_id": "opaque"},
            {"action": "select_option", "session_id": "missing", "control_id": "opaque", "option": "All"},
        ]:
            result = await browser_inspect(**kwargs)
            assert result.is_error
            assert result.structured_content == {
                "status": "error",
                "code": "capability_denied",
                "error": "caller capability does not allow this operation",
            }
        screenshot = await browser_fetch("https://example.com", include_screenshot=True)
        assert screenshot.is_error
        assert screenshot.structured_content["error"] == "caller capability does not allow this operation"
    finally:
        auth._current_caller.reset(token)


@pytest.mark.asyncio
async def test_confirmation_is_not_raw_interaction_or_artifact_authority(monkeypatch):
    import server

    calls = []

    async def act(owner, session_id, action, **params):
        calls.append(action)
        return {"status": "ok"}

    monkeypatch.setattr(server.manager, "act", act)
    token = auth._current_caller.set(Caller("my-ai", frozenset({"inspect.read", "inspect.confirmed"})))
    try:
        for action, fields in [
            ("click", {"selector": "button"}), ("type", {"selector": "input", "text": "x"}),
            ("evaluate", {"script": "1"}), ("screenshot", {}), ("export_pdf", {}),
        ]:
            assert (await browser_inspect(action, session_id="s", **fields)).structured_content["code"] == "capability_denied"
        for action, fields in [
            ("elements", {}), ("prepare_action", {"operation": "click", "selector": "button"}),
            ("execute_prepared", {"proposal_id": "p"}), ("discard_prepared", {"proposal_id": "p"}),
        ]:
            assert (await browser_inspect(action, session_id="s", **fields)).structured_content["status"] == "ok"
        assert calls == ["elements", "prepare_action", "execute_prepared", "discard_prepared"]
    finally:
        auth._current_caller.reset(token)
    token = auth._current_caller.set(Caller("pi", frozenset({"inspect.read", "inspect.interact", "inspect.script"})))
    try:
        for action, fields in [("click", {"selector": "button"}),
                               ("type", {"selector": "input", "text": "x"}),
                               ("evaluate", {"script": "1"})]:
            assert (await browser_inspect(action, session_id="s", **fields)).structured_content["status"] == "ok"
    finally:
        auth._current_caller.reset(token)


@pytest.mark.asyncio
async def test_frame_id_rejected_not_ignored_on_unsupported_actions(monkeypatch):
    from typing import get_args

    import server
    from frames import FRAME_ACTIONS

    async def never_called(*args, **kwargs):
        pytest.fail('invalid frame_id must be rejected before runtime')

    monkeypatch.setattr(server.manager, 'act', never_called)
    monkeypatch.setattr(server.manager, 'open', never_called)
    monkeypatch.setattr(server.manager, 'cleanup_scope', never_called)
    token = auth._current_caller.set(Caller('research', frozenset({'inspect.read', 'inspect.confirmed'})))
    try:
        for action in set(get_args(server.InspectAction)) - FRAME_ACTIONS:
            result = await browser_inspect(action, session_id='s', frame_id='f')
            assert result.structured_content['code'] == 'invalid_request', action
        for fid in ['', 'x' * 129]:
            assert (await browser_inspect('elements', session_id='s', frame_id=fid)).structured_content['code'] == 'invalid_request'
    finally:
        auth._current_caller.reset(token)
