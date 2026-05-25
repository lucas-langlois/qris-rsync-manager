from __future__ import annotations

from app.core.profiles import Profile
from app.core.remote_dirs import build_list_remote_entries_command, format_bytes, parse_remote_entries


def test_build_list_remote_entries_command_is_read_only() -> None:
    profile = Profile(username="user", host="ssh1.qriscloud.org.au", ssh_key_path=r"C:\Users\a key")

    command = build_list_remote_entries_command(profile, "/data/Q8940", ssh_path=r"C:\msys64\usr\bin\ssh.exe")

    assert command[0] == r"C:\msys64\usr\bin\ssh.exe"
    assert "user@ssh1.qriscloud.org.au" in command
    assert "find /data/Q8940 -mindepth 1 -maxdepth 1" in command[-1]
    assert "-printf '%y\\t%f\\t%s\\t%TY-%Tm-%Td %TH:%TM\\t%p\\n'" in command[-1]
    assert "rm " not in command[-1]
    assert "delete" not in command[-1].lower()


def test_parse_remote_entries_sorts_directories_first() -> None:
    output = (
        "f\treadme.txt\t2048\t2026-05-25 08:01\t/data/Q8940/readme.txt\n"
        "d\tRaw Data\t4096\t2026-05-25 08:02\t/data/Q8940/Raw Data\n"
    )

    entries = parse_remote_entries(output)

    assert entries[0].is_dir
    assert entries[0].name == "Raw Data"
    assert entries[0].type_label == "File Folder"
    assert entries[1].name == "readme.txt"
    assert entries[1].size_label == "2.00 KiB"
    assert entries[1].modified == "2026-05-25 08:01"


def test_format_bytes() -> None:
    assert format_bytes(10) == "10 B"
    assert format_bytes(1536) == "1.50 KiB"
