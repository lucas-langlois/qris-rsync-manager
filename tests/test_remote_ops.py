from __future__ import annotations

import pytest

from app.core.profiles import Profile
from app.core.remote_ops import (
    build_remote_delete_command,
    build_remote_mkdir_command,
    build_remote_move_command,
    is_dangerous_remote_path,
    remote_child_path,
)


def test_remote_child_path_rejects_nested_names() -> None:
    with pytest.raises(ValueError):
        remote_child_path("/data/Q8940/folder", "nested/name")


def test_build_remote_mkdir_command_quotes_spaces() -> None:
    profile = Profile(username="user", host="ssh1.qriscloud.org.au")

    command = build_remote_mkdir_command(profile, "/data/Q8940/folder", "New Folder", ssh_path="ssh.exe")

    assert command[0] == "ssh.exe"
    assert "mkdir -p -- '/data/Q8940/folder/New Folder'" in command[-1]


def test_build_remote_move_command_refuses_overwrite() -> None:
    profile = Profile(username="user")

    command = build_remote_move_command(profile, "/data/Q8940/folder/a.txt", "/data/Q8940/folder/b.txt", ssh_path="ssh.exe")

    assert "Destination already exists" in command[-1]
    assert "mv -- /data/Q8940/folder/a.txt /data/Q8940/folder/b.txt" in command[-1]


def test_build_remote_delete_command_rejects_collection_root() -> None:
    profile = Profile(username="user")

    with pytest.raises(ValueError):
        build_remote_delete_command(profile, ["/data/Q8940"], ssh_path="ssh.exe")


def test_build_remote_delete_command_quotes_paths() -> None:
    profile = Profile(username="user")

    command = build_remote_delete_command(profile, ["/data/Q8940/folder/a file.txt"], ssh_path="ssh.exe")

    assert "rm -rf -- '/data/Q8940/folder/a file.txt'" in command[-1]


def test_dangerous_remote_paths() -> None:
    assert is_dangerous_remote_path("/")
    assert is_dangerous_remote_path("/data/Q8940")
    assert not is_dangerous_remote_path("/data/Q8940/folder")


@pytest.mark.parametrize(
    "path",
    ["/data/Q8940/../Q8940", "/data/Q8940/folder/./file.txt", "/data/Q8940/folder/../../Q8940"],
)
def test_remote_mutations_reject_dot_path_components(path: str) -> None:
    profile = Profile(username="user")

    with pytest.raises(ValueError, match="components"):
        build_remote_delete_command(profile, [path], ssh_path="ssh.exe")
    with pytest.raises(ValueError, match="components"):
        build_remote_move_command(profile, path, "/data/Q8940/safe.txt", ssh_path="ssh.exe")


def test_remote_move_quotes_apostrophe_in_destination_message() -> None:
    command = build_remote_move_command(
        Profile(username="user"),
        "/data/Q8940/source.txt",
        "/data/Q8940/researcher's file.txt",
        ssh_path="ssh.exe",
    )

    assert "researcher'\"'\"'s file.txt" in command[-1]
