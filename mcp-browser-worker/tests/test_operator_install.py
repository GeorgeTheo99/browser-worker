"""Real operator shell against fake platform/browser commands and a temp HOME."""

from __future__ import annotations

import json
import os
import plistlib
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
    assert not (home / "srv/browser-worker/current").resolve().stat().st_mode & 0o222


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
# Enforce the macOS rename precondition on every platform, including Linux CI.
case "$1" in */.release-*)
  "$TEST_PYTHON" -c 'import os,sys; assert os.stat(sys.argv[1]).st_mode & 0o200' "$1" || exit 10 ;;
esac
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
    assert not release.stat().st_mode & 0o222


def test_release_symlink_is_rejected_without_chmod_of_target(installation):
    repo, home, _, _ = installation
    target = home / "unrelated"
    target.mkdir(mode=0o700)
    release = home / "srv/browser-worker/releases" / git(repo, "rev-parse", "HEAD")
    release.parent.parent.mkdir(parents=True, mode=0o700)
    release.parent.mkdir(mode=0o700)
    release.symlink_to(target, target_is_directory=True)
    result = run(installation, "install", "--no-start")
    assert result.returncode != 0
    assert "refusing symlinked release" in result.stderr
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


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


def test_vm_mode_builds_its_image_checks_the_browser_in_it_and_runs_the_host(installation):
    repo, home, log, env = installation
    container = Path(env["PATH"].split(":")[0]) / "container"
    executable(
        container,
        """#!/bin/sh
echo "container $*" >> "$EVENTS"
case "$1" in
 image) [ -f "$HOME/image-built" ] ;;
 build) touch "$HOME/image-built" ;;
esac
""",
    )
    vm = {"BROWSER_WORKER_MODE": "vm", "BROWSER_WORKER_CONTAINER_CLI": str(container), "BROWSER_WORKER_PORT": "8892",
          "BROWSER_WORKER_VM_NAME": "app-browser", "BROWSER_WORKER_VM_VOLUME": "app-browser-data",
          "BROWSER_WORKER_VM_IDLE_MINUTES": "15", "BROWSER_WORKER_VM_IMAGE_REPO": "app-browser-vm"}
    result = run(installation, "install", **vm)
    assert result.returncode == 0, result.stdout + result.stderr
    revision = git(repo, "rev-parse", "HEAD")
    image = f"app-browser-vm:{revision}"
    events = log.read_text()
    assert f"--build-arg BROWSER_WORKER_REVISION={revision} --file" in events and f"--tag {image}" in events
    assert f"container run --rm --shm-size 1g {image} " in events
    assert "operator_support.py browser --render" in events
    # Chromium lives in the VM: nothing provisions or checks one on the Mac.
    assert "provision-browser" not in events and "check-browser" not in events
    with (home / "Library/LaunchAgents/com.local.mcp-browser-worker-vm.plist").open("rb") as stream:
        service = plistlib.load(stream)
    assert service["ProgramArguments"][1] == str(home / "srv/browser-worker-vm/current/vm/host.py")
    assert service["EnvironmentVariables"]["BROWSER_WORKER_VM_IMAGE"] == image
    assert service["EnvironmentVariables"]["BROWSER_WORKER_DATA_DIR"] == str(home / "srv/browser-worker-vm/shared")
    assert service["EnvironmentVariables"]["BROWSER_WORKER_PORT"] == "8892"
    for key in ("BROWSER_WORKER_VM_NAME", "BROWSER_WORKER_VM_VOLUME", "BROWSER_WORKER_VM_IDLE_MINUTES",
                "BROWSER_WORKER_VM_IMAGE_REPO"):
        assert service["EnvironmentVariables"][key] == vm[key]
    # Stopping the VM outlasts launchd's default 20 s.
    assert service["ExitTimeOut"] == 60
    assert (home / "srv/browser-worker-vm/shared/tokens/pi-production").is_file()
    # A separate install: the host worker's paths and service are untouched.
    assert not (home / "srv/browser-worker").exists()
    assert not (home / "Library/LaunchAgents/com.local.mcp-browser-worker.plist").exists()
    # A reinstall of the same revision reuses its image, and keeps the VM's settings.
    again = {key: vm[key] for key in ("BROWSER_WORKER_MODE", "BROWSER_WORKER_CONTAINER_CLI")}
    assert run(installation, "install", **again).returncode == 0
    assert log.read_text().count("container build --progress") == 1
    with (home / "Library/LaunchAgents/com.local.mcp-browser-worker-vm.plist").open("rb") as stream:
        kept = plistlib.load(stream)["EnvironmentVariables"]
    assert kept["BROWSER_WORKER_VM_NAME"] == "app-browser" and kept["BROWSER_WORKER_PORT"] == "8892"
    assert kept["BROWSER_WORKER_VM_IMAGE"] == image


def test_release_archive_installs_without_git_under_its_own_label(installation, tmp_path):
    repo, home, _, env = installation
    archive = tmp_path / "browser-worker.tar"
    made = run(installation, "archive", str(archive))
    assert made.returncode == 0, made.stdout + made.stderr
    revision = git(repo, "rev-parse", "HEAD")
    source = tmp_path / "unpacked"
    source.mkdir()
    subprocess.run(["tar", "-xf", str(archive), "-C", str(source)], check=True)
    assert (source / ".browser-worker-revision").read_text() == revision + "\n"
    assert not (source / ".git").exists()
    # Git isn't needed (nor on PATH) to install from it.
    nogit = Path(env["PATH"].split(":")[0])
    executable(nogit / "git", "#!/bin/sh\necho git-must-not-be-used >&2\nexit 99\n")
    result = subprocess.run(
        ["bash", str(source / "scripts/browser-worker"), "install"],
        env={**env, "BROWSER_WORKER_LABEL": "com.example.app.browser"},
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "git-must-not-be-used" not in result.stderr
    assert os.readlink(home / "srv/browser-worker/current") == f"releases/{revision}"
    assert (home / f"srv/browser-worker/releases/{revision}/.browser-worker-revision").read_text() == revision + "\n"
    plist = home / "Library/LaunchAgents/com.example.app.browser.plist"
    with plist.open("rb") as stream:
        assert plistlib.load(stream)["Label"] == "com.example.app.browser"
    assert not (home / "Library/LaunchAgents/com.local.mcp-browser-worker.plist").exists()

    caller = subprocess.run(
        ["bash", str(source / "scripts/browser-worker"), "caller", "myai", "fetch,inspect.read"],
        env={**env, "BROWSER_WORKER_LABEL": "com.example.app.browser"},
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert caller.returncode == 0, caller.stdout + caller.stderr
    assert caller.stdout.strip() == str(home / "srv/browser-worker/shared/tokens/myai")


def test_archive_and_label_are_checked(installation, tmp_path):
    repo, _, _, _ = installation
    (repo / "uncommitted").write_text("x")
    result = run(installation, "archive", str(tmp_path / "out.tar"))
    assert result.returncode != 0 and "committed and clean" in result.stderr
    assert not (tmp_path / "out.tar").exists()
    result = run(installation, "status", BROWSER_WORKER_LABEL="bad label;")
    assert result.returncode == 2 and "BROWSER_WORKER_LABEL is invalid" in result.stderr


def test_an_apps_install_owns_its_caller(installation):
    _, home, _, _ = installation
    tokens = home / "srv/browser-worker/shared/tokens"
    first = run(installation, "install", "--no-start", BROWSER_WORKER_CALLER="app:fetch,inspect.read")
    assert first.returncode == 0, first.stdout + first.stderr
    before = (tokens / "app").read_bytes()
    wider = run(installation, "install", "--no-start", BROWSER_WORKER_CALLER="app:fetch,inspect.live,inspect.read")
    assert wider.returncode == 0, wider.stdout + wider.stderr
    clients = json.loads((home / "srv/browser-worker/shared/clients.json").read_text())["clients"]
    assert next(row for row in clients if row["id"] == "app")["capabilities"] == ["fetch", "inspect.live", "inspect.read"]
    assert (tokens / "app").read_bytes() == before
    # The caller command doesn't change grants; it prints the token file of one that matches.
    assert run(installation, "caller", "app", "fetch").returncode != 0
    shown = run(installation, "caller", "app", "inspect.read,fetch,inspect.live")
    assert shown.stdout.strip() == str(tokens / "app")
    bad = run(installation, "install", "--no-start", BROWSER_WORKER_CALLER="app;fetch")
    assert bad.returncode != 0 and "id:capability" in bad.stderr
