"""Raw landing-zone storage: local files for development, ADLS on the corporate network.
Keys are '/'-separated relative paths; `raw_path` columns store keys, so data moves between
backends without rewriting the database."""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from uw_pipeline.settings import Settings


class Storage(Protocol):
    def exists(self, key: str) -> bool: ...
    def read_bytes(self, key: str) -> bytes: ...
    def write_bytes(self, key: str, data: bytes) -> str:
        """Write `data` under `key` and return a locator (path/URI) recorded for lineage."""
        ...


class LocalStorage:
    """Stores objects as files under `root`; keys are '/'-separated relative paths."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError(f"storage key escapes root: {key!r}")
        return path

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def read_bytes(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def write_bytes(self, key: str, data: bytes) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replace so a killed run never leaves a truncated raw file behind.
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return str(path)


class AdlsStorage:
    """Stores objects in an ADLS Gen2 container under `root` (Phase 12, D-040).

    `fs` is an azure.storage.filedatalake FileSystemClient (or a test double with the same
    methods). Writes go to a temporary name and are renamed into place, so a killed run never
    leaves a truncated raw file behind, as with LocalStorage."""

    def __init__(self, fs: Any, root: str, *, locator_prefix: str = "") -> None:
        self.fs = fs
        self.root = root.strip("/")
        self.locator_prefix = locator_prefix.rstrip("/")

    def _path(self, key: str) -> str:
        parts = key.split("/")
        if key.startswith("/") or any(p in ("", ".", "..") for p in parts):
            raise ValueError(f"storage key escapes root: {key!r}")
        return f"{self.root}/{key}" if self.root else key

    def exists(self, key: str) -> bool:
        return bool(self.fs.get_file_client(self._path(key)).exists())

    def read_bytes(self, key: str) -> bytes:
        data: bytes = self.fs.get_file_client(self._path(key)).download_file().readall()
        return data

    def write_bytes(self, key: str, data: bytes) -> str:
        path = self._path(key)
        head, _, name = path.rpartition("/")
        tmp = f"{head}/.tmp-{uuid.uuid4().hex}-{name}" if head else f".tmp-{uuid.uuid4().hex}"
        tmp_client = self.fs.get_file_client(tmp)
        tmp_client.upload_data(data, overwrite=True)
        try:
            # rename_file takes "<file system>/<path>"
            tmp_client.rename_file(f"{self.fs.file_system_name}/{path}")
        except BaseException:
            tmp_client.delete_file()
            raise
        return f"{self.locator_prefix}/{path}" if self.locator_prefix else path


def make_storage(settings: Settings) -> Storage:
    """Local files (data/raw) off the corporate network; ADLS on it (STORAGE_BACKEND=adls)."""
    if settings.storage_backend == "local":
        return LocalStorage(settings.raw_dir)
    if settings.storage_backend != "adls":
        raise ValueError(f"STORAGE_BACKEND={settings.storage_backend!r}: use local or adls")
    if not (settings.adls_client_id and settings.adls_client_secret):
        raise RuntimeError("STORAGE_BACKEND=adls needs ADLS_CLIENT_ID and ADLS_CLIENT_SECRET")
    from azure.identity import ClientSecretCredential  # optional dependency: ".[azure]"
    from azure.storage.filedatalake import DataLakeServiceClient

    from uw_pipeline.clients.db import load_azure_config

    cfg = load_azure_config(settings.config_dir)
    env = settings.azure_env or cfg.default_env
    if env not in cfg.adls.environments:
        raise ValueError(f"AZURE_ENV={env!r} is not in config/azure.yaml")
    e = cfg.adls.environments[env]
    credential = ClientSecretCredential(cfg.adls.tenant_id, settings.adls_client_id,
                                        settings.adls_client_secret.get_secret_value())  # fmt: skip
    host = f"{e.storage_account}.dfs.core.windows.net"
    service = DataLakeServiceClient(account_url=f"https://{host}", credential=credential)
    fs = service.get_file_system_client(file_system=e.container)
    return AdlsStorage(fs, e.root, locator_prefix=f"abfss://{e.container}@{host}")
