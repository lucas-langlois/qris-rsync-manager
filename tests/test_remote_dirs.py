from __future__ import annotations

from app.core.profiles import Profile
from app.core.remote_dirs import (
    RemoteDirectoryCache,
    RemoteEntry,
    build_list_remote_entries_command,
    format_bytes,
    parse_remote_entries,
)


def test_build_list_remote_entries_command_is_read_only(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    profile = Profile(username="user", host="ssh1.qriscloud.org.au", ssh_key_path=r"C:\Users\a key")

    command = build_list_remote_entries_command(profile, "/data/Q8940", ssh_path=r"C:\msys64\usr\bin\ssh.exe")

    assert command[0] == r"C:\msys64\usr\bin\ssh.exe"
    assert "user@ssh1.qriscloud.org.au" in command
    assert "find /data/Q8940 -mindepth 1 -maxdepth 1" in command[-1]
    assert "-printf '%y\\t%f\\t%s\\t%TY-%Tm-%Td %TH:%TM\\t%p\\n'" in command[-1]
    assert "rm " not in command[-1]
    assert "delete" not in command[-1].lower()
    assert "ControlMaster=auto" not in command
    assert "ControlPersist=120" not in command
    assert not any(part.startswith("ControlPath=") for part in command)
    assert "| sort" not in command[-1]
    assert "StrictHostKeyChecking=accept-new" in command
    assert any(part.startswith("UserKnownHostsFile=/") for part in command)


def test_native_windows_ssh_does_not_receive_unix_socket_options() -> None:
    profile = Profile(username="user", host="host")

    command = build_list_remote_entries_command(
        profile,
        "/data/Q8940",
        ssh_path=r"C:\Windows\System32\OpenSSH\ssh.exe",
    )

    assert "ControlMaster=auto" not in command
    assert not any(part.startswith("ControlPath=") for part in command)


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


def test_remote_directory_cache_is_profile_scoped_and_lru_bounded() -> None:
    cache = RemoteDirectoryCache(max_directories=2, max_entries=3)
    first_profile = Profile(name="one", username="user", host="ssh1.qriscloud.org.au")
    second_profile = Profile(name="two", username="user", host="ssh1.qriscloud.org.au")
    first = [RemoteEntry("d", "a", 0, "now", "/data/Q1/a")]
    second = [RemoteEntry("f", "b", 1, "now", "/data/Q1/b")]
    third = [RemoteEntry("f", "c", 1, "now", "/data/Q1/c")]

    cache.put(first_profile, "/data/Q1", first)
    cache.put(second_profile, "/data/Q1", second)
    assert cache.get(first_profile, "/data/Q1") == first
    assert cache.get(second_profile, "/data/Q1") == second

    cache.put(first_profile, "/data/Q1/a", third)
    assert len(cache) == 2
    assert cache.get(first_profile, "/data/Q1") is None
    assert cache.get(second_profile, "/data/Q1") == second


def test_remote_directory_cache_invalidates_subdirectories_only_for_profile() -> None:
    cache = RemoteDirectoryCache()
    profile = Profile(name="one", username="user")
    other = Profile(name="two", username="user")
    entries = [RemoteEntry("f", "x", 1, "now", "/data/Q1/x")]
    for path in ("/data/Q1", "/data/Q1/a", "/data/Q1/a/b"):
        cache.put(profile, path, entries)
        cache.put(other, path, entries)

    cache.invalidate(profile, "/data/Q1/a", subdirectories=True)

    assert cache.get(profile, "/data/Q1") == entries
    assert cache.get(profile, "/data/Q1/a") is None
    assert cache.get(profile, "/data/Q1/a/b") is None
    assert cache.get(other, "/data/Q1/a") == entries
