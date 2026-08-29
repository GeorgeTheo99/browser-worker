from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from server import mcp, verify_release_receipt


def test_exact_two_tool_inventory_and_closed_schemas() -> None:
    tools = asyncio.run(mcp.list_tools())
    assert [tool.name for tool in tools] == ["browser_fetch", "browser_inspect"]
    by_name = {tool.name: tool for tool in tools}
    assert by_name["browser_fetch"].parameters["additionalProperties"] is False
    assert by_name["browser_inspect"].parameters["additionalProperties"] is False
    assert by_name["browser_fetch"].parameters["required"] == ["url"]
    actions = by_name["browser_inspect"].parameters["properties"]["action"]["enum"]
    assert "evaluate" in actions
    assert "upload" not in actions
    assert "download" not in actions
    assert set(by_name) == {"browser_fetch", "browser_inspect"}


def test_release_receipt_must_match_revision(tmp_path: Path) -> None:
    verify_release_receipt("", tmp_path)
    with pytest.raises(RuntimeError, match="unavailable"):
        verify_release_receipt("a" * 40, tmp_path)
    (tmp_path / ".browser-worker-revision").write_text("b" * 40 + "\n", encoding="ascii")
    with pytest.raises(RuntimeError, match="does not match"):
        verify_release_receipt("a" * 40, tmp_path)
    verify_release_receipt("b" * 40, tmp_path)
