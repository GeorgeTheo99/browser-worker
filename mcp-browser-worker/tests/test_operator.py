from __future__ import annotations

from pathlib import Path


def test_operator_uses_clean_committed_atomic_revision_releases() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (root / "scripts/browser-worker").read_text(encoding="utf-8")
    assert 'status --porcelain --untracked-files=all' in script
    assert 'git -C "$ROOT_DIR" archive --format=tar "$REVISION"' in script
    assert '.browser-worker-revision' in script
    assert "os.replace(sys.argv[1], sys.argv[2])" in script
    assert '$CURRENT_LINK/mcp-browser-worker/http_server.py' in script
    assert "BROWSER_WORKER_DEPLOY_REVISION" in script
    assert "live browser-worker revision mismatch" in script
    assert "uv sync --project \"$tmp/mcp-browser-worker\" --locked" in script
