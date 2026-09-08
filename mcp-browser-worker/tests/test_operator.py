from __future__ import annotations

from pathlib import Path


def test_operator_uses_clean_committed_atomic_revision_releases() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (root / "scripts/browser-worker").read_text(encoding="utf-8")
    assert 'status --porcelain --untracked-files=all' in script
    assert 'git -C "$ROOT_DIR" archive --format=tar "$REVISION"' in script
    assert '.browser-worker-revision' in script
    assert "os.replace(sys.argv[1], sys.argv[2])" in script
    assert "current+'/mcp-browser-worker/http_server.py'" in script
    assert ".browser-worker-manifest.json" in script
    assert 'verify_release "$release" "$REVISION"' in script
    assert 'chmod -R a-w "$tmp"' in script
    assert script.index('verify_release "$tmp" "$REVISION"\n') < script.index('mv "$tmp" "$release"')
    assert "BROWSER_WORKER_DEPLOY_REVISION" in script
    support = (root / "mcp-browser-worker/operator_support.py").read_text(encoding="utf-8")
    assert "live browser-worker revision mismatch" in support
    assert "authorization: Bearer $(token)" not in script
    assert "bootstrap_python" in script
    assert "PLAYWRIGHT_SKIP_BROWSER_GC=1" in script
    assert 'browser --render' in script
    assert 'clients "$DATA_DIR"' in script
    assert "uv sync --project \"$tmp/mcp-browser-worker\" --locked" in script
