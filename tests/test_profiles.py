from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.core.profiles import (
    Profile,
    fallback_hosts,
    load_profiles,
    load_profiles_result,
    profile_with_host,
    save_profiles,
    upsert_profile,
)


def test_profile_save_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "profiles.json"
    profile = Profile(
        name="Q0101",
        username="alice",
        host="ssh2.qriscloud.org.au",
        collection_id="q0101",
        remote_path="",
        ssh_port=2200,
        ssh_key_path=r"C:\Users\Alice\.ssh\id_ed25519",
        rsync_path=r"C:\msys64\usr\bin\rsync.exe",
    )

    save_profiles([profile], path)
    loaded = load_profiles(path)

    assert len(loaded) == 1
    assert loaded[0].name == "Q0101"
    assert loaded[0].username == "alice"
    assert loaded[0].host == "ssh2.qriscloud.org.au"
    assert loaded[0].collection_id == "Q0101"
    assert loaded[0].remote_path == "/data/Q0101"
    assert loaded[0].ssh_port == 2200


def test_upsert_profile_replaces_by_name() -> None:
    original = Profile(name="One", username="old")
    updated = Profile(name="One", username="new")

    result = upsert_profile([original], updated)

    assert len(result) == 1
    assert result[0].username == "new"


def test_fallback_hosts_for_qriscloud_ssh() -> None:
    assert fallback_hosts(Profile(host="ssh1.qriscloud.org.au")) == ["ssh1.qriscloud.org.au", "ssh2.qriscloud.org.au"]
    assert fallback_hosts(Profile(host="ssh2.qriscloud.org.au")) == ["ssh2.qriscloud.org.au", "ssh1.qriscloud.org.au"]
    assert fallback_hosts(Profile(host="data.qriscloud.org.au")) == ["data.qriscloud.org.au"]


def test_profile_with_host_preserves_profile_fields() -> None:
    profile = Profile(name="Q8940", username="lucas", host="ssh1.qriscloud.org.au")

    updated = profile_with_host(profile, "ssh2.qriscloud.org.au")

    assert updated.name == "Q8940"
    assert updated.username == "lucas"
    assert updated.host == "ssh2.qriscloud.org.au"


def test_load_skips_malformed_records_and_reports_diagnostics(tmp_path: Path) -> None:
    path = tmp_path / "profiles.json"
    good = Profile(name="Good", username="alice", rsync_path="rsync.exe")
    path.write_text(
        json.dumps([good.__dict__, {"name": "Missing fields"}, "not a record", {**good.__dict__, "name": "Bad port", "ssh_port": "22"}]),
        encoding="utf-8",
    )

    result = load_profiles_result(path)

    assert [profile.name for profile in result.profiles] == ["Good"]
    assert result.source == "primary"
    assert len(result.diagnostics) == 3
    assert "ssh_port" in result.diagnostics[-1]


def test_load_rejects_out_of_range_port(tmp_path: Path) -> None:
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps([{**Profile(rsync_path="rsync.exe").__dict__, "ssh_port": 70000}]), encoding="utf-8")

    result = load_profiles_result(path)

    assert result.source == "default"
    assert any("ssh_port" in message for message in result.diagnostics)


def test_save_replaces_file_atomically_and_keeps_previous_backup(tmp_path: Path) -> None:
    path = tmp_path / "profiles.json"
    save_profiles([Profile(name="Old", rsync_path="rsync.exe")], path)

    save_profiles([Profile(name="New", rsync_path="rsync.exe")], path)

    assert [profile.name for profile in load_profiles(path)] == ["New"]
    assert [profile.name for profile in load_profiles(path.with_name("profiles.json.bak"))] == ["Old"]
    assert not list(tmp_path.glob("*.tmp"))


def test_failed_replace_leaves_prior_data_recoverable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "profiles.json"
    save_profiles([Profile(name="Old", rsync_path="rsync.exe")], path)
    actual_replace = os.replace

    def fail_primary_replace(source: str | Path, destination: str | Path) -> None:
        if Path(destination) == path:
            raise OSError("simulated replace failure")
        actual_replace(source, destination)

    monkeypatch.setattr("app.core.profiles.os.replace", fail_primary_replace)

    with pytest.raises(OSError, match="simulated"):
        save_profiles([Profile(name="New", rsync_path="rsync.exe")], path)

    assert [profile.name for profile in load_profiles(path)] == ["Old"]
    assert [profile.name for profile in load_profiles(path.with_name("profiles.json.bak"))] == ["Old"]


def test_failed_temporary_write_leaves_prior_data_recoverable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "profiles.json"
    save_profiles([Profile(name="Old", rsync_path="rsync.exe")], path)

    def fail_write(*_args: object, **_kwargs: object) -> Path:
        raise OSError("simulated write failure")

    monkeypatch.setattr("app.core.profiles._write_temporary", fail_write)

    with pytest.raises(OSError, match="simulated"):
        save_profiles([Profile(name="New", rsync_path="rsync.exe")], path)

    assert [profile.name for profile in load_profiles(path)] == ["Old"]
    assert [profile.name for profile in load_profiles(path.with_name("profiles.json.bak"))] == ["Old"]


def test_load_uses_backup_when_primary_is_malformed(tmp_path: Path) -> None:
    path = tmp_path / "profiles.json"
    save_profiles([Profile(name="Recover", rsync_path="rsync.exe")], path)
    path.write_text("{not JSON", encoding="utf-8")

    result = load_profiles_result(path)

    assert result.source == "backup"
    assert [profile.name for profile in result.profiles] == ["Recover"]
    assert any("Could not read primary" in message for message in result.diagnostics)


def test_missing_profile_file_uses_default_without_false_warning(tmp_path: Path) -> None:
    result = load_profiles_result(tmp_path / "missing.json")

    assert result.source == "default"
    assert result.diagnostics == ()


def test_save_after_backup_recovery_preserves_good_backup_on_primary_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "profiles.json"
    save_profiles([Profile(name="Recover", rsync_path="rsync.exe")], path)
    path.write_text("{corrupt", encoding="utf-8")
    actual_replace = os.replace

    def fail_primary_replace(source: str | Path, destination: str | Path) -> None:
        if Path(destination) == path:
            raise OSError("primary replace failed")
        actual_replace(source, destination)

    monkeypatch.setattr("app.core.profiles.os.replace", fail_primary_replace)

    with pytest.raises(OSError, match="primary replace failed"):
        save_profiles([Profile(name="New", rsync_path="rsync.exe")], path)

    result = load_profiles_result(path)
    assert result.source == "backup"
    assert [profile.name for profile in result.profiles] == ["Recover"]


def test_successful_save_after_backup_recovery_keeps_prior_good_backup(tmp_path: Path) -> None:
    path = tmp_path / "profiles.json"
    save_profiles([Profile(name="Recover", rsync_path="rsync.exe")], path)
    path.write_text("{corrupt", encoding="utf-8")

    save_profiles([Profile(name="New", rsync_path="rsync.exe")], path)

    assert [profile.name for profile in load_profiles(path)] == ["New"]
    assert [profile.name for profile in load_profiles(path.with_name("profiles.json.bak"))] == ["Recover"]
