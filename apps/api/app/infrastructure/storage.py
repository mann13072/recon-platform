"""Object storage for uploads, evidence and export packages (spec section 4).

Two backends behind one interface:

* :class:`LocalObjectStorage` - the filesystem, for development and tests;
* :class:`S3ObjectStorage`    - any S3-compatible service (AWS S3, R2, MinIO).

Storage keys are always tenant-prefixed, and the key is derived from the
content hash rather than the user-supplied filename, so an upload cannot be
made to write outside its own prefix.
"""

from __future__ import annotations

import re
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from packages.audit.evidence import sha256_bytes

__all__ = [
    "LocalObjectStorage",
    "ObjectStorage",
    "S3ObjectStorage",
    "build_storage",
    "safe_filename",
    "storage_key",
]

_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(filename: str) -> str:
    """Reduce a user-supplied filename to something safe to echo back.

    The result is never used to build a storage path - see :func:`storage_key` -
    but it is stored and displayed, so it must not carry control characters or
    path separators.
    """
    normalised = unicodedata.normalize("NFKD", filename)
    cleaned = _UNSAFE_RE.sub("_", normalised).strip("._")
    cleaned = cleaned[:200]
    return cleaned or "upload"


def storage_key(tenant_id: UUID, kind: str, content_hash: str, extension: str = "") -> str:
    """Build a tenant-prefixed, content-addressed key.

    Nothing from the client reaches the path, so ``../`` and absolute paths in a
    filename are structurally impossible rather than filtered.
    """
    if kind not in {"uploads", "evidence", "exports", "snapshots"}:
        raise ValueError(f"unknown storage kind: {kind}")
    suffix = ""
    if extension:
        suffix = "." + _UNSAFE_RE.sub("", extension.lstrip("."))[:16]
    return f"{tenant_id}/{kind}/{content_hash[:2]}/{content_hash}{suffix}"


class ObjectStorage(ABC):
    @abstractmethod
    def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> str:
        """Store bytes and return the key."""

    @abstractmethod
    def get(self, key: str) -> bytes:
        ...

    @abstractmethod
    def exists(self, key: str) -> bool:
        ...

    @abstractmethod
    def delete(self, key: str) -> None:
        ...

    @abstractmethod
    def presigned_url(self, key: str, *, expires_seconds: int = 900) -> str:
        """A short-lived download URL (spec section 53: signed object-storage URLs)."""

    def put_content(
        self, tenant_id: UUID, kind: str, data: bytes, *, extension: str = "",
        content_type: str = "application/octet-stream",
    ) -> tuple[str, str]:
        """Store content-addressed. Returns ``(key, sha256)``."""
        digest = sha256_bytes(data)
        key = storage_key(tenant_id, kind, digest, extension)
        self.put(key, data, content_type=content_type)
        return key, digest


@dataclass(slots=True)
class LocalObjectStorage(ObjectStorage):
    root: Path

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        candidate = (self.root / key).resolve()
        # Defence in depth: keys are generated, but a bug upstream must not be
        # able to write outside the storage root.
        if not candidate.is_relative_to(self.root):
            raise ValueError("storage key escapes the storage root")
        return candidate

    def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> str:
        del content_type
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()

    def presigned_url(self, key: str, *, expires_seconds: int = 900) -> str:
        del expires_seconds
        return f"file://{self._path(key)}"


@dataclass(slots=True)
class S3ObjectStorage(ObjectStorage):
    bucket: str
    endpoint_url: str | None = None
    region: str = "us-east-1"
    access_key: str | None = None
    secret_key: str | None = None
    _client: Any = None

    def client(self) -> Any:
        if self._client is None:
            import boto3  # imported lazily so the domain layer stays dependency-free

            self._client = boto3.client(
                "s3",
                endpoint_url=self.endpoint_url or None,
                region_name=self.region,
                aws_access_key_id=self.access_key or None,
                aws_secret_access_key=self.secret_key or None,
            )
        return self._client

    def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> str:
        self.client().put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            # Server-side encryption at rest (spec section 53).
            ServerSideEncryption="AES256",
        )
        return key

    def get(self, key: str) -> bytes:
        response = self.client().get_object(Bucket=self.bucket, Key=key)
        return bytes(response["Body"].read())

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client().head_object(Bucket=self.bucket, Key=key)
        except ClientError:
            return False
        return True

    def delete(self, key: str) -> None:
        self.client().delete_object(Bucket=self.bucket, Key=key)

    def presigned_url(self, key: str, *, expires_seconds: int = 900) -> str:
        return str(
            self.client().generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=expires_seconds,
            )
        )


def build_storage(settings: Any) -> ObjectStorage:
    """Pick a backend from settings."""
    if settings.object_storage_endpoint or settings.is_production:
        return S3ObjectStorage(
            bucket=settings.object_storage_bucket,
            endpoint_url=settings.object_storage_endpoint or None,
            region=settings.object_storage_region,
            access_key=settings.object_storage_access_key,
            secret_key=settings.object_storage_secret_key,
        )
    return LocalObjectStorage(root=Path(settings.local_storage_path))
