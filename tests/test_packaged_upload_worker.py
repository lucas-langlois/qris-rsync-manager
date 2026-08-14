from __future__ import annotations

from pathlib import Path

from app.core.archive_upload import analyze_upload_tree
from app.core.profiles import Profile
from app.gui.main_window import PackagedUploadWorker


def _plan(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "one.jpg").write_bytes(b"123")
    (source / "two.jpg").write_bytes(b"456")
    return analyze_upload_tree(source, min_flat_files=1, min_folder_bytes=5)


def test_packaged_worker_uploads_loose_tree_then_archive_payload_and_cleans_up(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    plan = _plan(tmp_path)
    profile = Profile(
        name="Q0101",
        username="user",
        host="ssh1.qriscloud.org.au",
        remote_path="/data/Q0101",
        rsync_path=r"C:\msys64\usr\bin\rsync.exe",
    )
    worker = PackagedUploadWorker(profile, plan, "/data/Q0101", dry_run=False, ssh_path="ssh.exe")
    monkeypatch.setattr(worker, "_available_profile", lambda: profile)
    commands: list[list[str]] = []

    def run(command, *_args, **_kwargs) -> int:
        commands.append(command)
        return 0

    monkeypatch.setattr(worker.runner, "run", run)
    results: list[int] = []
    worker.finished.connect(results.append)

    worker.run()

    assert results == [0]
    assert len(commands) == 2
    assert any(part.startswith("--exclude-from=") for part in commands[0])
    assert not any(part.startswith("--exclude-from=") for part in commands[1])
    assert not list(tmp_path.glob(".source.qris-upload-*"))


def test_packaged_worker_stops_after_loose_upload_failure_and_cleans_up(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    plan = _plan(tmp_path)
    profile = Profile(name="Q0101", username="user", host="host", rsync_path="rsync.exe")
    worker = PackagedUploadWorker(profile, plan, "/data/Q0101", dry_run=False)
    monkeypatch.setattr(worker, "_available_profile", lambda: profile)
    commands: list[list[str]] = []
    monkeypatch.setattr(worker.runner, "run", lambda command, *_args, **_kwargs: commands.append(command) or 23)
    results: list[int] = []
    worker.finished.connect(results.append)

    worker.run()

    assert results == [23]
    assert len(commands) == 1
    assert not list(tmp_path.glob(".source.qris-upload-*"))


def test_packaged_worker_preserves_source_folder_for_both_upload_phases(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    plan = _plan(tmp_path)
    profile = Profile(
        name="Q0101",
        username="user",
        host="ssh1.qriscloud.org.au",
        rsync_path=r"C:\msys64\usr\bin\rsync.exe",
    )
    worker = PackagedUploadWorker(
        profile,
        plan,
        "/data/Q0101",
        dry_run=False,
        ssh_path="ssh.exe",
        include_source_directory=True,
    )
    monkeypatch.setattr(worker, "_available_profile", lambda: profile)
    commands: list[list[str]] = []
    monkeypatch.setattr(worker.runner, "run", lambda command, *_args, **_kwargs: commands.append(command) or 0)

    worker.run()

    assert len(commands) == 2
    assert commands[0][-2].endswith("source")
    assert not commands[0][-2].endswith("/")
    assert commands[0][-1] == "user@ssh1.qriscloud.org.au:/data/Q0101/"
    assert commands[1][-1] == "user@ssh1.qriscloud.org.au:/data/Q0101/source/"


def test_packaging_failure_is_written_to_its_own_log(monkeypatch, tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    profile = Profile(name="Q0101", username="user", host="host", rsync_path="rsync.exe")
    worker = PackagedUploadWorker(profile, plan, "/data/Q0101", dry_run=False)
    log_file = tmp_path / "packaged_failure.log"
    monkeypatch.setattr(worker, "_available_profile", lambda: profile)
    monkeypatch.setattr("app.gui.main_window.new_log_file", lambda _prefix: log_file)
    monkeypatch.setattr(
        "app.gui.main_window.build_upload_package",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError("inventory path failed")),
    )
    results: list[int] = []
    worker.finished.connect(results.append)

    worker.run()

    assert results == [1]
    assert "Packaged upload failed: inventory path failed" in log_file.read_text(encoding="utf-8")
