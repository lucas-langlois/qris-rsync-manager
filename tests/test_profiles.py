from __future__ import annotations

from pathlib import Path

from app.core.profiles import Profile, fallback_hosts, load_profiles, profile_with_host, save_profiles, upsert_profile


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
