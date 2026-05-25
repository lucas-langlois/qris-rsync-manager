from __future__ import annotations

from pathlib import Path

from app.core.profiles import Profile
from app.core.sync_compare import (
    FileRecord,
    build_remote_manifest_command,
    compare_manifests,
    files_from_argument,
    parse_remote_manifest,
    scan_local_manifest,
    write_files_from,
)


def test_compare_manifests_selects_missing_and_changed() -> None:
    local = {
        "same.txt": FileRecord("same.txt", 10, 100.0),
        "missing.txt": FileRecord("missing.txt", 20, 100.0),
        "changed.txt": FileRecord("changed.txt", 30, 100.0),
    }
    remote = {
        "same.txt": FileRecord("same.txt", 10, 101.0),
        "changed.txt": FileRecord("changed.txt", 31, 100.0),
    }

    selection = compare_manifests(local, remote)

    assert [item.relative_path for item in selection.missing] == ["missing.txt"]
    assert [item.relative_path for item in selection.changed] == ["changed.txt"]


def test_parse_remote_manifest() -> None:
    output = "a.txt\t10\t100.5\nfolder/b.txt\t20\t101.0\n"

    records = parse_remote_manifest(output)

    assert records["a.txt"].size == 10
    assert records["folder/b.txt"].modified_timestamp == 101.0


def test_scan_local_manifest_uses_relative_posix_paths(tmp_path: Path) -> None:
    nested = tmp_path / "Folder With Spaces"
    nested.mkdir()
    file_path = nested / "a.txt"
    file_path.write_text("hello", encoding="utf-8")

    records = scan_local_manifest(tmp_path)

    assert "Folder With Spaces/a.txt" in records
    assert records["Folder With Spaces/a.txt"].size == 5


def test_write_files_from_and_argument(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    file_list = write_files_from([FileRecord("a.txt", 1, 1.0)], "test")

    assert file_list.read_text(encoding="utf-8") == "a.txt\n"
    assert files_from_argument(file_list, r"C:\msys64\usr\bin\rsync.exe").startswith("--files-from=/")


def test_build_remote_manifest_command_is_read_only() -> None:
    profile = Profile(username="user", host="ssh1.qriscloud.org.au")

    command = build_remote_manifest_command(profile, "/data/Q8940", ssh_path="ssh.exe")

    assert "user@ssh1.qriscloud.org.au" in command
    assert "find . -type f" in command[-1]
    assert "rm " not in command[-1]
