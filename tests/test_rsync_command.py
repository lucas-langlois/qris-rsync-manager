from __future__ import annotations

from pathlib import Path

from app.core.profiles import Profile
from app.core.rsync_command import DEFAULT_RSYNC_OPTIONS, build_rsync_command, build_ssh_transport, validate_transfer_inputs


def test_rsync_command_uses_safe_argument_list_and_defaults(tmp_path: Path) -> None:
    local = tmp_path / "Folder With Spaces"
    local.mkdir()
    profile = Profile(
        name="Test",
        username="user",
        host="ssh1.qriscloud.org.au",
        collection_id="Q0101",
        remote_path="/data/Q0101",
        ssh_port=22,
        ssh_key_path=str(tmp_path / "key with space"),
        rsync_path=r"C:\msys64\usr\bin\rsync.exe",
    )

    command = build_rsync_command(profile, local, dry_run=True, ssh_path=r"C:\msys64\usr\bin\ssh.exe")

    assert isinstance(command, list)
    assert command[0] == r"C:\msys64\usr\bin\rsync.exe"
    for option in DEFAULT_RSYNC_OPTIONS:
        assert option in command
    assert "--dry-run" in command
    assert "--itemize-changes" in command
    assert "-c" not in command
    assert "-W" in command
    assert "--append-verify" not in command
    assert command[-2].endswith("/")
    assert "Folder With Spaces" in command[-2]
    assert command[-1] == "user@ssh1.qriscloud.org.au:/data/Q0101/"


def test_upload_command_does_not_include_compare_only_flags(tmp_path: Path) -> None:
    local = tmp_path / "data"
    local.mkdir()
    profile = Profile(username="user", rsync_path=r"C:\msys64\usr\bin\rsync.exe")

    command = build_rsync_command(profile, local, dry_run=False)

    assert "--dry-run" not in command
    assert "--itemize-changes" not in command


def test_upload_command_can_use_files_from(tmp_path: Path) -> None:
    local = tmp_path / "data"
    local.mkdir()
    file_list = tmp_path / "files to upload.txt"
    file_list.write_text("a.txt\n", encoding="utf-8")
    profile = Profile(username="user", rsync_path=r"C:\msys64\usr\bin\rsync.exe")

    command = build_rsync_command(profile, local, dry_run=False, files_from=file_list)

    assert any(part.startswith("--files-from=/") for part in command)


def test_upload_command_can_exclude_archived_source_files(tmp_path: Path) -> None:
    local = tmp_path / "data"
    local.mkdir()
    exclusions = tmp_path / "archived files.exclude"
    exclusions.write_text("/photos/image.jpg\n", encoding="utf-8")
    profile = Profile(username="user", rsync_path=r"C:\msys64\usr\bin\rsync.exe")

    command = build_rsync_command(profile, local, exclude_from=exclusions)

    assert any(part.startswith("--exclude-from=/") for part in command)


def test_upload_command_can_send_single_file(tmp_path: Path) -> None:
    local = tmp_path / "image one.JPG"
    local.write_text("data", encoding="utf-8")
    profile = Profile(username="user", remote_path="/data/Q8940/images", rsync_path=r"C:\msys64\usr\bin\rsync.exe")

    command = build_rsync_command(profile, local, dry_run=False)

    assert command[-2].endswith("image one.JPG")
    assert not command[-2].endswith("/")
    assert command[-1] == "user@ssh1.qriscloud.org.au:/data/Q8940/images/"


def test_upload_command_can_preserve_selected_folder_name(tmp_path: Path) -> None:
    local = tmp_path / "Girringun_Drone_Aug2026"
    local.mkdir()
    profile = Profile(username="user", remote_path="/data/Q9560", rsync_path=r"C:\msys64\usr\bin\rsync.exe")

    command = build_rsync_command(profile, local, include_source_directory=True)

    assert command[-2].endswith("Girringun_Drone_Aug2026")
    assert not command[-2].endswith("/")
    assert command[-1] == "user@ssh1.qriscloud.org.au:/data/Q9560/"


def test_upload_command_quotes_remote_destination_spaces_and_can_force_payload_transfer(tmp_path: Path) -> None:
    local = tmp_path / "data"
    local.mkdir()
    profile = Profile(username="user", remote_path="/data/Q9560/archive payload", rsync_path=r"C:\\msys64\\usr\\bin\\rsync.exe")

    command = build_rsync_command(profile, local, ignore_times=True)

    assert command[-1] == "user@ssh1.qriscloud.org.au:/data/Q9560/archive payload/"
    assert "--protect-args" in command
    assert "--ignore-times" in command


def test_standard_remote_path_does_not_require_protected_args(tmp_path: Path) -> None:
    local = tmp_path / "data"
    local.mkdir()
    profile = Profile(username="user", remote_path="/data/Q9560", rsync_path="rsync.exe")

    command = build_rsync_command(profile, local)

    assert "--protect-args" not in command


def test_download_command_reverses_source_and_destination(tmp_path: Path) -> None:
    local = tmp_path / "download target"
    local.mkdir()
    profile = Profile(username="user", host="ssh2.qriscloud.org.au", remote_path="/data/Q8940", rsync_path=r"C:\msys64\usr\bin\rsync.exe")

    command = build_rsync_command(profile, local, dry_run=True, direction="download")

    assert command[-2] == "user@ssh2.qriscloud.org.au:/data/Q8940/"
    assert command[-1].endswith("/")
    assert "download target" in command[-1]


def test_download_command_can_use_files_from(tmp_path: Path) -> None:
    local = tmp_path / "download target"
    local.mkdir()
    file_list = tmp_path / "remote files.txt"
    file_list.write_text("image 1.JPG\nimage 2.JPG\n", encoding="utf-8")
    profile = Profile(username="user", host="ssh2.qriscloud.org.au", remote_path="/data/Q8940", rsync_path=r"C:\msys64\usr\bin\rsync.exe")

    command = build_rsync_command(profile, local, dry_run=False, direction="download", files_from=file_list)

    assert any(part.startswith("--files-from=/") for part in command)
    assert command[-2] == "user@ssh2.qriscloud.org.au:/data/Q8940/"
    assert command[-1].endswith("/")


def test_download_command_can_fetch_single_remote_file(tmp_path: Path) -> None:
    local = tmp_path / "download target"
    local.mkdir()
    profile = Profile(username="user", host="ssh2.qriscloud.org.au", rsync_path=r"C:\msys64\usr\bin\rsync.exe")

    command = build_rsync_command(
        profile,
        local,
        remote_path="/data/Q8940/images/image one.JPG",
        direction="download",
        remote_is_file=True,
    )

    assert command[-2] == "user@ssh2.qriscloud.org.au:/data/Q8940/images/image one.JPG"
    assert command[-1].endswith("/")


def test_validate_download_requires_local_folder(tmp_path: Path) -> None:
    local_file = tmp_path / "target.txt"
    local_file.write_text("data", encoding="utf-8")
    profile = Profile(username="user", rsync_path=r"C:\msys64\usr\bin\rsync.exe")

    errors = validate_transfer_inputs(profile, local_file, "/data/Q8940/file.txt", direction="download")

    assert "Local download folder must exist." in errors


def test_ssh_transport_has_keepalive_and_no_checksum_flag(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    profile = Profile(
        username="user",
        ssh_port=2222,
        ssh_key_path=str(tmp_path / "id key"),
        rsync_path=r"C:\msys64\usr\bin\rsync.exe",
    )

    transport = build_ssh_transport(profile, ssh_path=r"C:\msys64\usr\bin\ssh.exe")

    assert "ServerAliveInterval=60" in transport
    assert "ServerAliveCountMax=10" in transport
    assert "BatchMode=yes" in transport
    assert "-p 2222" in transport
    assert "id key" in transport
    assert "StrictHostKeyChecking=accept-new" in transport
    assert "UserKnownHostsFile=/" in transport


def test_ssh_transport_can_allow_passphrase_prompt(tmp_path: Path) -> None:
    profile = Profile(
        username="user",
        ssh_key_path=str(tmp_path / "id key"),
        rsync_path=r"C:\msys64\usr\bin\rsync.exe",
    )

    transport = build_ssh_transport(profile, ssh_path=r"C:\msys64\usr\bin\ssh.exe", batch_mode=False)

    assert "BatchMode=yes" not in transport
