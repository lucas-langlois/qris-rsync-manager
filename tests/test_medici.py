from __future__ import annotations

import pytest

from app.core.medici import (
    build_recall_medici_command,
    build_recall_medici_files_command,
    medici_path_for_remote_path,
    recall_file_path_for_remote_path,
)
from app.core.profiles import Profile


def test_medici_path_maps_data_to_qrisdata() -> None:
    assert medici_path_for_remote_path("/data/Q8940/folder") == "/QRISdata/Q8940/folder"


def test_medici_path_allows_existing_hpc_paths() -> None:
    assert medici_path_for_remote_path("/QRISdata/Q8940/folder") == "/QRISdata/Q8940/folder"
    assert medici_path_for_remote_path("/RDS/Q8940/folder") == "/RDS/Q8940/folder"


def test_medici_path_rejects_unknown_roots() -> None:
    with pytest.raises(ValueError):
        medici_path_for_remote_path("/tmp/Q8940")


def test_recall_file_path_preserves_data_paths() -> None:
    assert recall_file_path_for_remote_path("/data/Q8940/a file.jpg") == "/data/Q8940/a file.jpg"


def test_build_recall_medici_command_is_ssh_command() -> None:
    profile = Profile(username="user", host="ssh1.qriscloud.org.au", ssh_key_path=r"C:\Users\a key")

    command = build_recall_medici_command(profile, "/data/Q8940/folder", ssh_path=r"C:\msys64\usr\bin\ssh.exe")

    assert command[0] == r"C:\msys64\usr\bin\ssh.exe"
    assert "user@ssh1.qriscloud.org.au" in command
    assert "recall_medici /QRISdata/Q8940/folder" in command[-1]
    assert "rm " not in command[-1]
    assert "delete" not in command[-1].lower()


def test_build_recall_medici_files_command_batches_files() -> None:
    profile = Profile(username="user", host="ssh1.qriscloud.org.au")

    command = build_recall_medici_files_command(
        profile,
        ["/data/Q8940/a file.jpg", "/data/Q8940/b.jpg", "/data/Q8940/c.jpg"],
        ssh_path="ssh.exe",
        batch_size=2,
    )

    assert "recall_medici '/data/Q8940/a file.jpg' /data/Q8940/b.jpg" in command[-1]
    assert "recall_medici /data/Q8940/c.jpg" in command[-1]
