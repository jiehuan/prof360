"""ADLS storage (Phase 12) against an in-memory stand-in for the Azure Data Lake SDK."""

import pytest

from uw_pipeline.clients.storage import AdlsStorage, make_storage
from uw_pipeline.settings import Settings


class FakeFile:
    def __init__(self, fs: "FakeFs", path: str) -> None:
        self.fs, self.path = fs, path

    def exists(self) -> bool:
        return self.path in self.fs.files

    def download_file(self) -> "FakeFile":
        return self

    def readall(self) -> bytes:
        return self.fs.files[self.path]

    def upload_data(self, data: bytes, overwrite: bool = False) -> None:
        assert overwrite
        self.fs.files[self.path] = data

    def rename_file(self, new_name: str) -> None:
        fs_name, _, target = new_name.partition("/")
        assert fs_name == self.fs.file_system_name
        if self.fs.fail_rename:
            raise OSError("rename failed")
        self.fs.files[target] = self.fs.files.pop(self.path)

    def delete_file(self) -> None:
        if self.fs.fail_delete:
            raise ConnectionError("service unreachable")
        self.fs.files.pop(self.path, None)


class FakeFs:
    file_system_name = "container"

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.fail_rename = False
        self.fail_delete = False

    def get_file_client(self, path: str) -> FakeFile:
        return FakeFile(self, path)


def test_round_trip_under_root_with_atomic_write() -> None:
    fs = FakeFs()
    st = AdlsStorage(
        fs, "professional360/raw/", locator_prefix="abfss://container@acct.dfs.core.windows.net"
    )
    key = "sec/submissions/CIK0001048286/2026-10-13.json"
    assert not st.exists(key)
    loc = st.write_bytes(key, b"{}")
    assert loc == f"abfss://container@acct.dfs.core.windows.net/professional360/raw/{key}"
    assert st.exists(key) and st.read_bytes(key) == b"{}"
    assert list(fs.files) == [f"professional360/raw/{key}"]  # no temporary file left behind


def test_failed_rename_leaves_no_partial_file() -> None:
    fs = FakeFs()
    fs.fail_rename = True
    st = AdlsStorage(fs, "raw")
    with pytest.raises(OSError):
        st.write_bytes("a/b.json", b"x")
    assert fs.files == {}


def test_failed_cleanup_keeps_the_original_error() -> None:
    fs = FakeFs()
    fs.fail_rename = fs.fail_delete = True
    with pytest.raises(OSError, match="rename failed"):  # not the cleanup's ConnectionError
        AdlsStorage(fs, "raw").write_bytes("a/b.json", b"x")


def test_delete() -> None:
    st = AdlsStorage(FakeFs(), "raw")
    st.write_bytes("a/b.json", b"x")
    st.delete("a/b.json")
    st.delete("a/b.json")  # absent: no error
    assert not st.exists("a/b.json")


@pytest.mark.parametrize("key", ["../x", "/abs", "a//b", "a/./b"])
def test_keys_cannot_escape_the_root(key: str) -> None:
    with pytest.raises(ValueError):
        AdlsStorage(FakeFs(), "raw").exists(key)


def test_backend_selection(tmp_path) -> None:
    base = {"sec_user_agent": "Test test@example.com", "data_dir": tmp_path}
    assert type(make_storage(Settings(**base))).__name__ == "LocalStorage"
    with pytest.raises(RuntimeError, match="ADLS_CLIENT_ID"):
        make_storage(Settings(**base, storage_backend="adls"))
    from pydantic import ValidationError

    with pytest.raises(ValidationError):  # a typo fails at startup, never falls back silently
        Settings(**base, storage_backend="s3")
    with pytest.raises(ValidationError):
        Settings(**base, database_backend="azure-sql")
