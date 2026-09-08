"""Real operator shell against fake platform/browser commands and a temp HOME."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[2]


def executable(path: Path, text: str):
    path.write_text(text)
    path.chmod(0o755)


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def installation(tmp_path):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "mcp-browser-worker").mkdir()
    for name in (
        "install.sh",
        "scripts/browser-worker",
        "mcp-browser-worker/operator_support.py",
        "mcp-browser-worker/auth.py",
    ):
        shutil.copy2(SOURCE / name, repo / name)
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Operator Test")
    git(repo, "config", "user.email", "operator@example.test")
    git(repo, "add", ".")
    git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "fixture")
    home = tmp_path / "home"
    home.mkdir()
    fake = tmp_path / "bin"
    fake.mkdir()
    log = tmp_path / "events"
    log.touch()
    executable(
        fake / "python3",
        "#!/bin/sh\necho ambient-python-must-not-be-used >&2\nexit 99\n",
    )
    executable(
        fake / "uv",
        f"""#!{sys.executable}
import os, pathlib, sys
args=sys.argv[1:]
if args[:2] == ['python','find']:
    print(os.environ['TEST_PYTHON']); sys.exit(0)
if args[0] != 'sync': sys.exit(9)
project=pathlib.Path(args[args.index('--project')+1]); bindir=project/'.venv/bin'; bindir.mkdir(parents=True)
python=bindir/'python'
python.write_text("""
        + repr("""#!/bin/sh
if [ "$1" = -m ] && [ "$2" = patchright ]; then
  echo provision-browser >> "$EVENTS"
  [ "${FAIL_BROWSER:-0}" != 1 ] || exit 7
  exit 0
fi
case "$1" in *operator_support.py)
  if [ "$2" = browser ]; then echo check-browser >> "$EVENTS"; exit 0; fi
  if [ "$2" = verify ]; then echo verify >> "$EVENTS"; [ "${FAIL_VERIFY:-0}" != 1 ] || exit 8; exit 0; fi
esac
exec "$TEST_PYTHON" "$@"
""")
        + """)
python.chmod(0o755)
""",
    )
    executable(
        fake / "launchctl",
        """#!/bin/sh
echo "launchctl $1" >> "$EVENTS"
case "$1" in
 print) [ -f "$HOME/loaded" ] ;;
 bootout) rm -f "$HOME/loaded" ;;
 bootstrap|kickstart)
   if [ "${FAIL_NEW_START:-0}" = 1 ] && [ "$(readlink "$HOME/srv/browser-worker/current")" != "${OLD_CURRENT:-}" ]; then exit 9; fi
   touch "$HOME/loaded" ;;
 *) exit 9 ;;
esac
""",
    )
    executable(fake / "curl", '#!/bin/sh\nprintf \'{"status":"ok"}\'\n')
    executable(fake / "lsof", '#!/bin/sh\n[ "${PORT_BUSY:-0}" = 1 ]\n')
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": str(fake) + ":/usr/bin:/bin",
        "TEST_PYTHON": sys.executable,
        "EVENTS": str(log),
    }
    for name in list(env):
        if name.startswith("BROWSER_WORKER_"):
            env.pop(name)
    return repo, home, log, env


def run(fixture, *args, **extra):
    repo, _, _, env = fixture
    return subprocess.run(
        ["bash", str(repo / "scripts/browser-worker"), *args],
        env={**env, **extra},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_install_no_start_provisions_production_token_and_browser_idempotently(
    installation,
):
    _, home, log, _ = installation
    first = run(installation, "install", "--no-start")
    assert first.returncode == 0, first.stdout + first.stderr
    assert "not started" in first.stdout
    token = home / "srv/browser-worker/shared/tokens/pi-production"
    before = token.read_bytes()
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
    assert before.decode().strip() not in first.stdout + first.stderr
    assert not (home / "loaded").exists()
    assert run(installation, "install", "--no-start").returncode == 0
    assert token.read_bytes() == before
    assert log.read_text().count("provision-browser") == 2
    assert "ambient-python" not in first.stderr


def test_preparation_failure_preserves_previous_release_and_service(installation):
    repo, home, log, _ = installation
    initial = run(installation, "install")
    assert initial.returncode == 0, initial.stdout + initial.stderr
    current = home / "srv/browser-worker/current"
    plist = home / "Library/LaunchAgents/com.local.mcp-browser-worker.plist"
    before = (os.readlink(current), plist.read_bytes())
    (repo / "new-version").write_text("next")
    git(repo, "add", ".")
    git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "next")
    log.write_text("")
    result = run(installation, "install", FAIL_BROWSER="1")
    assert result.returncode != 0
    assert "installed and verified" not in result.stdout
    assert (os.readlink(current), plist.read_bytes()) == before
    assert (home / "loaded").exists()
    assert "launchctl bootout" not in log.read_text()
    assert not list((home / "srv/browser-worker/releases").glob(".release-*"))


@pytest.mark.parametrize("failure", ["FAIL_VERIFY", "FAIL_NEW_START"])
def test_activation_failure_restores_previous_release_and_plist(installation, failure):
    repo, home, _, _ = installation
    initial = run(installation, "install")
    assert initial.returncode == 0, initial.stdout + initial.stderr
    current = home / "srv/browser-worker/current"
    plist = home / "Library/LaunchAgents/com.local.mcp-browser-worker.plist"
    before = (os.readlink(current), plist.read_bytes())
    (repo / "new-version").write_text("next")
    git(repo, "add", ".")
    git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "next")
    result = run(installation, "install", **{failure: "1", "OLD_CURRENT": before[0]})
    assert result.returncode != 0
    assert (os.readlink(current), plist.read_bytes()) == before
    assert (home / "loaded").exists()
    assert "Previous worker service restored" in result.stderr


def test_custom_port_and_data_paths_survive_new_shell(installation):
    _, home, _, _ = installation
    data = home / "private custom data"
    logs = home / "custom logs"
    result = run(
        installation,
        "install",
        "--no-start",
        BROWSER_WORKER_PORT="19890",
        BROWSER_WORKER_DATA_DIR=str(data),
        BROWSER_WORKER_LOG_DIR=str(logs),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    metadata = run(installation, "env")
    assert metadata.returncode == 0, metadata.stderr
    assert "PORT=19890" in metadata.stdout
    assert f"DATA_DIR={data}" in metadata.stdout
    assert f"PRODUCTION_TOKEN_FILE={data}/tokens/pi-production" in metadata.stdout
    (logs / "browser-worker.log").write_text("saved custom log\n")
    output = run(installation, "logs")
    assert output.returncode == 0, output.stderr
    assert "saved custom log" in output.stdout


def test_interruption_after_final_release_rename_is_recoverable(installation):
    repo, home, _, env = installation
    fakebin = Path(env["PATH"].split(os.pathsep)[0])
    executable(
        fakebin / "mv",
        """#!/bin/sh
/bin/mv "$@" || exit $?
case "$1" in */.release-*) [ "${INTERRUPT_AFTER_RENAME:-0}" != 1 ] || exit 9 ;; esac
""",
    )
    result = run(installation, "install", "--no-start", INTERRUPT_AFTER_RENAME="1")
    assert result.returncode != 0
    revision = git(repo, "rev-parse", "HEAD")
    release = home / "srv/browser-worker/releases" / revision
    assert release.is_dir()
    assert not (release / "install.sh").stat().st_mode & 0o222
    assert not (home / "srv/browser-worker/current").exists()
    retry = run(installation, "install", "--no-start")
    assert retry.returncode == 0, retry.stdout + retry.stderr
    assert (home / "srv/browser-worker/current").is_symlink()


def test_dangling_log_symlink_is_rejected_without_creating_target(installation):
    _, home, _, _ = installation
    logs = home / "Library/Logs/browser-worker"
    logs.mkdir(parents=True, mode=0o700)
    target = home / "must-not-be-created"
    (logs / "browser-worker.log").symlink_to(target)
    result = run(installation, "install", "--no-start")
    assert result.returncode != 0
    assert "refusing symlinked log" in result.stderr
    assert not target.exists()


def test_unrelated_port_owner_is_not_stopped(installation):
    _, _, log, _ = installation
    result = run(installation, "install", PORT_BUSY="1")
    assert result.returncode != 0
    assert "used by another service" in result.stderr
    assert "launchctl bootout" not in log.read_text()
