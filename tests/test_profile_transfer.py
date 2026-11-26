"""Export and import profiles to use them on another computer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from app.core.profiles import (
    Profile,
    adapt_profile_to_this_computer,
    export_profiles,
    merge_imported_profiles,
    read_profile_file,
)


def _profile(name="Q9560 - Girringun", key=r"C:\Users\olduser\.ssh\qriscloud_ed25519") -> Profile:
    return Profile(
        name=name, username="lucaslanglois", collection_id="Q9560", remote_path="/data/Q9560",
        ssh_key_path=key, rsync_path=r"C:\msys64\usr\bin\rsync.exe",
    )


def test_export_then_read_round_trips_without_secrets(tmp_path) -> None:
    path = tmp_path / "profiles.json"

    export_profiles([_profile(), _profile("Q8940 Drone")], path)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["format"] == "qris-rsync-manager-profiles"
    assert "passphrase" not in path.read_text(encoding="utf-8").lower().replace("no passwords or passphrases", "")
    profiles, problems = read_profile_file(path)
    assert [profile.name for profile in profiles] == ["Q9560 - Girringun", "Q8940 Drone"] and problems == []


def test_a_copied_profiles_json_with_a_bom_can_be_imported(tmp_path) -> None:
    from dataclasses import asdict

    path = tmp_path / "profiles.json"
    path.write_text(json.dumps([asdict(_profile()), {"name": "broken"}]), encoding="utf-8-sig")

    profiles, problems = read_profile_file(path)

    assert [profile.name for profile in profiles] == ["Q9560 - Girringun"]
    assert problems and "Skipped profile 2" in problems[0]


def test_not_a_profile_file_is_rejected(tmp_path) -> None:
    path = tmp_path / "other.json"
    path.write_text('{"hello": 1}', encoding="utf-8")
    with pytest.raises(ValueError):
        read_profile_file(path)


def test_key_path_points_at_this_users_ssh_folder(tmp_path) -> None:
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "qriscloud_ed25519").write_text("key", encoding="utf-8")

    adapted, note = adapt_profile_to_this_computer(_profile(), ssh_dir=ssh)

    assert adapted.ssh_key_path == str(ssh / "qriscloud_ed25519") and note == ""

    missing, note = adapt_profile_to_this_computer(_profile(key=r"C:\Users\x\.ssh\other_key"), ssh_dir=ssh)
    assert missing.ssh_key_path == str(ssh / "other_key")
    assert "SSH key not found" in note


def test_merge_replaces_or_keeps_same_named_profiles() -> None:
    mine = [_profile("A", key="mine"), _profile("B")]
    incoming = [_profile("A", key="theirs"), _profile("C")]

    kept = merge_imported_profiles(mine, incoming, replace=False)
    assert [p.name for p in kept.profiles] == ["A", "B", "C"] and kept.profiles[0].ssh_key_path == "mine"
    assert kept.added == ["C"] and kept.skipped == ["A"]

    replaced = merge_imported_profiles(mine, incoming, replace=True)
    assert replaced.profiles[0].ssh_key_path == "theirs" and replaced.replaced == ["A"]


def test_import_from_the_menu_replaces_the_placeholder_profile(monkeypatch, tmp_path) -> None:
    from app.gui.main_window import MainWindow

    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    QApplication.instance() or QApplication([])
    window = MainWindow()
    assert [profile.name for profile in window.profiles] == ["Default QRIScloud"]
    export = tmp_path / "export.json"
    export_profiles([_profile(), _profile("Q8940 Drone")], export)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args, **_kwargs: (str(export), ""))
    shown: list[str] = []
    monkeypatch.setattr(QMessageBox, "information", lambda _parent, _title, text, *_args: shown.append(text))

    window._import_profiles()

    assert [profile.name for profile in window.profiles] == ["Q9560 - Girringun", "Q8940 Drone"]
    assert window.profile_combo.count() == 2
    assert shown and shown[0].startswith("Added 2 profile(s).")
    # Saved, so they are there next time the app starts.
    assert "Q8940 Drone" in Path(tmp_path / "appdata" / "QRISRsyncManager" / "profiles.json").read_text(encoding="utf-8")
    window.close()
