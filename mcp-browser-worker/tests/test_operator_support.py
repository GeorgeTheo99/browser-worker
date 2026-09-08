from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

import operator_support as operator
from auth import AuthConfigError, ClientRegistry


def existing_config(
    root: Path, client_id="other-client", capabilities=None, token_file="tokens/other"
):
    (root / "tokens").mkdir(mode=0o700, exist_ok=True)
    token = root / token_file
    token.write_text("EXISTING_PRIVATE_TOKEN_" + "x" * 32)
    token.chmod(0o600)
    config = {
        "version": 1,
        "clients": [
            {
                "id": client_id,
                "token_file": token_file,
                "capabilities": capabilities or ["fetch", "inspect.read"],
            }
        ],
    }
    path = root / "clients.json"
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    return config, token


def test_production_bootstrap_is_private_and_idempotent(tmp_path, capsys):
    operator.ensure_clients(tmp_path)
    config = json.loads((tmp_path / "clients.json").read_text())
    assert [row["id"] for row in config["clients"]] == ["pi-production"]
    token_path = tmp_path / "tokens/pi-production"
    before = token_path.read_bytes()
    assert len(before.strip()) >= 32
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "clients.json").stat().st_mode) == 0o600
    operator.ensure_clients(tmp_path)
    assert token_path.read_bytes() == before
    assert before.decode().strip() not in capsys.readouterr().out
    ClientRegistry(tmp_path / "clients.json", tmp_path).load()


def test_existing_callers_and_tokens_preserved(tmp_path):
    config, token = existing_config(tmp_path)
    before = token.read_bytes()
    operator.ensure_clients(tmp_path)
    after = json.loads((tmp_path / "clients.json").read_text())
    assert after["clients"][0] == config["clients"][0]
    assert token.read_bytes() == before
    assert (tmp_path / "tokens/pi-production").read_bytes() != before


def test_custom_production_path_preserved(tmp_path):
    existing_config(tmp_path, "pi-production", token_file="tokens/custom-pi")
    before = (tmp_path / "clients.json").read_bytes()
    operator.ensure_clients(tmp_path)
    assert (tmp_path / "clients.json").read_bytes() == before
    assert not (tmp_path / "tokens/pi-production").exists()
    _, row, _ = operator.production_client(tmp_path)
    assert row["token_file"] == "tokens/custom-pi"


def test_existing_production_policy_is_not_escalated(tmp_path):
    existing_config(tmp_path, "pi-production", capabilities=["fetch"])
    before = (tmp_path / "clients.json").read_bytes()
    with pytest.raises(ValueError, match="review its policy"):
        operator.ensure_clients(tmp_path)
    assert (tmp_path / "clients.json").read_bytes() == before


@pytest.mark.parametrize("bad", ["malformed", "public", "symlink"])
def test_invalid_existing_configuration_is_not_silently_repaired(tmp_path, bad):
    existing_config(tmp_path)
    path = tmp_path / "clients.json"
    if bad == "malformed":
        path.write_text("{not json")
    elif bad == "public":
        path.chmod(0o644)
    else:
        target = tmp_path / "real.json"
        path.rename(target)
        path.symlink_to(target)
    before = path.read_bytes()
    with pytest.raises(AuthConfigError):
        operator.ensure_clients(tmp_path)
    assert path.read_bytes() == before
    assert not (tmp_path / "tokens/pi-production").exists()


def test_unsafe_data_directory_is_not_chmodded(tmp_path):
    data = tmp_path / "data"
    data.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="private"):
        operator.ensure_clients(data)
    assert stat.S_IMODE(data.stat().st_mode) == 0o755


def test_safe_orphan_production_token_is_reused_for_retry(tmp_path):
    (tmp_path / "tokens").mkdir(mode=0o700)
    token = tmp_path / "tokens/pi-production"
    token.write_text("ORPHAN_RETRY_TOKEN_" + "x" * 32)
    token.chmod(0o600)
    before = token.read_bytes()
    operator.ensure_clients(tmp_path)
    assert token.read_bytes() == before
    operator.production_client(tmp_path)


def test_verify_checks_auth_revision_inventory_and_smoke_without_logging_secret(
    tmp_path, monkeypatch, capsys
):
    operator.ensure_clients(tmp_path)
    secret = (tmp_path / "tokens/pi-production").read_text().strip()
    calls = []

    def request(port, path, body=None, token=None):
        calls.append((port, path, body, token))
        if path == "/health":
            return 200, {
                "status": "ok",
                "service": "browser-worker",
                "ready": True,
                "revision": "revision",
            }
        if not token:
            return 401, {"error": "authentication required"}
        if body["method"] == "tools/list":
            return 200, {
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "tools": [{"name": "browser_fetch"}, {"name": "browser_inspect"}]
                },
            }
        return 200, {
            "jsonrpc": "2.0",
            "id": body["id"],
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"status": "ok", "text": "Example Domain"}),
                    }
                ]
            },
        }

    monkeypatch.setattr(operator, "request", request)
    operator.verify(tmp_path, 19890, "revision", smoke=True)
    assert calls[1][3] is None
    assert calls[2][3] == secret
    assert secret not in capsys.readouterr().out


@pytest.mark.parametrize("condition", ["revision", "anonymous", "inventory"])
def test_verify_fails_on_wrong_service_or_authentication(
    tmp_path, monkeypatch, condition
):
    operator.ensure_clients(tmp_path)

    def request(port, path, body=None, token=None):
        if path == "/health":
            return 200, {
                "status": "ok",
                "service": "browser-worker",
                "ready": True,
                "revision": "wrong" if condition == "revision" else "revision",
            }
        if not token:
            return (200 if condition == "anonymous" else 401), {}
        return 200, {"id": body["id"], "result": {"tools": []}}

    monkeypatch.setattr(operator, "request", request)
    with pytest.raises(ValueError):
        operator.verify(tmp_path, 19890, "revision", smoke=False)
