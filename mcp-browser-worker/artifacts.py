from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from config import ARTIFACT_TTL_SECONDS, MAX_CALLER_ARTIFACT_BYTES


class ArtifactError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    id: str
    owner: str
    filename: str
    mime_type: str
    size: int
    sha256: str
    created_at: float
    expires_at: float

    def public(self) -> dict[str, object]:
        return {
            "artifact_id": self.id,
            "mime_type": self.mime_type,
            "size": self.size,
            "sha256": self.sha256,
            "expires_in_seconds": max(0, int(self.expires_at - time.time())),
        }


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._records: dict[str, ArtifactRecord] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.root.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid()
        ):
            raise ArtifactError("artifact root must be a service-owned directory")
        os.chmod(self.root, 0o700)
        for child in self.root.iterdir():
            if child.is_file() or child.is_symlink():
                child.unlink(missing_ok=True)

    async def close(self) -> None:
        async with self._lock:
            records = list(self._records.values())
            self._records.clear()
        for record in records:
            (self.root / record.filename).unlink(missing_ok=True)

    async def sweep(self) -> None:
        now = time.time()
        async with self._lock:
            expired = [record for record in self._records.values() if record.expires_at <= now]
            for record in expired:
                self._records.pop(record.id, None)
        for record in expired:
            (self.root / record.filename).unlink(missing_ok=True)

    async def create(
        self,
        owner: str,
        data: bytes,
        *,
        suffix: str,
        mime_type: str,
        max_bytes: int,
    ) -> ArtifactRecord:
        if not data or len(data) > max_bytes:
            raise ArtifactError("artifact exceeds its size limit")
        if suffix not in {"png", "pdf"}:
            raise ArtifactError("artifact type is not allowed")
        await self.sweep()
        async with self._lock:
            used = sum(record.size for record in self._records.values() if record.owner == owner)
            if used + len(data) > MAX_CALLER_ARTIFACT_BYTES:
                raise ArtifactError("caller artifact quota exceeded")
            artifact_id = secrets.token_urlsafe(24)
            filename = f"{artifact_id}.{suffix}"
            path = self.root / filename
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                with os.fdopen(descriptor, "wb", closefd=True) as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                path.unlink(missing_ok=True)
                raise
            now = time.time()
            record = ArtifactRecord(
                artifact_id,
                owner,
                filename,
                mime_type,
                len(data),
                hashlib.sha256(data).hexdigest(),
                now,
                now + ARTIFACT_TTL_SECONDS,
            )
            self._records[artifact_id] = record
            return record

    async def read(self, owner: str, artifact_id: str) -> tuple[ArtifactRecord, bytes]:
        await self.sweep()
        async with self._lock:
            record = self._records.get(artifact_id)
        if record is None or record.owner != owner:
            raise ArtifactError("artifact not found")
        path = self.root / record.filename
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as exc:
            raise ArtifactError("artifact not found") from exc
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_size != record.size
            ):
                raise ArtifactError("artifact integrity check failed")
            chunks: list[bytes] = []
            remaining = record.size
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(descriptor)
            data = b"".join(chunks)
            if (
                after.st_dev != before.st_dev
                or after.st_ino != before.st_ino
                or after.st_size != before.st_size
                or len(data) != record.size
                or hashlib.sha256(data).hexdigest() != record.sha256
            ):
                raise ArtifactError("artifact integrity check failed")
            return record, data
        finally:
            os.close(descriptor)
