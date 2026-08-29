from __future__ import annotations

import os
import time
from dataclasses import replace
from pathlib import Path

import pytest

from artifacts import ArtifactError, ArtifactStore


@pytest.mark.asyncio
async def test_artifacts_are_owner_bound_and_integrity_checked(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    store = ArtifactStore(root)
    await store.start()
    record = await store.create(
        "owner-a", b"png-data", suffix="png", mime_type="image/png", max_bytes=100
    )
    assert set(record.public()) == {
        "artifact_id",
        "mime_type",
        "size",
        "sha256",
        "expires_in_seconds",
    }
    returned, data = await store.read("owner-a", record.id)
    assert returned == record and data == b"png-data"
    with pytest.raises(ArtifactError, match="not found"):
        await store.read("owner-b", record.id)

    path = root / record.filename
    hardlink = root / "extra-link"
    os.link(path, hardlink)
    with pytest.raises(ArtifactError, match="integrity"):
        await store.read("owner-a", record.id)
    hardlink.unlink()
    path.write_bytes(b"changed!")
    with pytest.raises(ArtifactError, match="integrity"):
        await store.read("owner-a", record.id)
    await store.close()


@pytest.mark.asyncio
async def test_artifact_expiry_quota_and_opaque_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import artifacts as artifacts_module

    monkeypatch.setattr(artifacts_module, "MAX_CALLER_ARTIFACT_BYTES", 10)
    store = ArtifactStore(tmp_path / "artifacts")
    await store.start()
    record = await store.create(
        "owner", b"12345678", suffix="png", mime_type="image/png", max_bytes=10
    )
    with pytest.raises(ArtifactError, match="quota"):
        await store.create("owner", b"more", suffix="png", mime_type="image/png", max_bytes=10)
    with pytest.raises(ArtifactError, match="not found"):
        await store.read("owner", "../" + record.id)
    store._records[record.id] = replace(record, expires_at=time.time() - 1)
    with pytest.raises(ArtifactError, match="not found"):
        await store.read("owner", record.id)
    assert not (store.root / record.filename).exists()
    await store.close()


@pytest.mark.asyncio
async def test_artifact_root_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "artifacts"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ArtifactError):
        await ArtifactStore(link).start()
