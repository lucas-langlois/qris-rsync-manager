from __future__ import annotations

from pathlib import Path

from app.core.archive_upload import UploadPackage, analyze_upload_tree
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
    monkeypatch.setattr(worker, "_available_profile", lambda *args, **kwargs: profile)
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


def test_packaged_worker_stops_after_loose_upload_failure_and_retains_package(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    plan = _plan(tmp_path)
    profile = Profile(name="Q0101", username="user", host="host", rsync_path="rsync.exe")
    worker = PackagedUploadWorker(profile, plan, "/data/Q0101", dry_run=False)
    monkeypatch.setattr(worker, "_available_profile", lambda *args, **kwargs: profile)
    commands: list[list[str]] = []
    monkeypatch.setattr(worker.runner, "run", lambda command, *_args, **_kwargs: commands.append(command) or 23)
    results: list[int] = []
    worker.finished.connect(results.append)

    worker.run()

    assert results == [23]
    assert len(commands) == 1
    assert len(list(tmp_path.glob(".source.qris-upload-*"))) == 1


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
    monkeypatch.setattr(worker, "_available_profile", lambda *args, **kwargs: profile)
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
    monkeypatch.setattr(worker, "_available_profile", lambda *args, **kwargs: profile)
    monkeypatch.setattr("app.gui.main_window.new_log_file", lambda _prefix: log_file)
    monkeypatch.setattr(
        "app.gui.main_window.prepare_upload_package",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError("inventory path failed")),
    )
    results: list[int] = []
    worker.finished.connect(results.append)

    worker.run()

    assert results == [1]
    assert "Packaged upload failed: inventory path failed" in log_file.read_text(encoding="utf-8")


def test_packaged_worker_rechecks_host_and_plan_before_forced_payload_upload(monkeypatch, tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    profile = Profile(name="Q0101", username="user", host="host", rsync_path="rsync.exe")
    worker = PackagedUploadWorker(profile, plan, "/data/Q0101", dry_run=False, include_source_directory=True)
    work_dir = tmp_path / "work"
    payload_dir = work_dir / "payload"
    payload_dir.mkdir(parents=True)
    exclude_file = work_dir / "exclude"
    exclude_file.write_text("/source/one.jpg\n", encoding="utf-8")
    package = UploadPackage(work_dir, payload_dir, exclude_file, 1, 1)
    prep_kwargs: dict[str, object] = {}
    hosts: list[object] = []

    def prepare(_plan, **kwargs):
        assert len(hosts) == 1
        prep_kwargs.update(kwargs)
        return package

    monkeypatch.setattr(
        "app.gui.main_window.prepare_upload_package",
        prepare,
    )
    monkeypatch.setattr(worker, "_available_profile", lambda *args, **kwargs: hosts.append(object()) or profile)
    revalidated: list[object] = []
    monkeypatch.setattr("app.gui.main_window.revalidate_upload_plan", lambda *args, **_kwargs: revalidated.append(args[0]))
    commands: list[list[str]] = []
    monkeypatch.setattr(worker.runner, "run", lambda command, *_args, **_kwargs: commands.append(command) or 0)

    worker.run()

    assert prep_kwargs["cancel_event"] is worker._cancelled
    assert callable(prep_kwargs["progress_callback"])
    assert prep_kwargs["build_archives"] is True
    assert prep_kwargs["include_source_directory"] is True
    assert len(hosts) == 3
    assert revalidated == [plan]
    assert len(commands) == 2
    assert "--ignore-times" in commands[1]
    assert commands[1][-1] == "user@host:/data/Q0101/source/"


def test_packaged_dry_run_does_not_build_or_transfer_tar_payload(monkeypatch, tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    profile = Profile(name="Q0101", username="user", host="host", rsync_path="rsync.exe")
    worker = PackagedUploadWorker(profile, plan, "/data/Q0101", dry_run=True)
    work_dir = tmp_path / "dry-work"
    payload_dir = work_dir / "payload"
    payload_dir.mkdir(parents=True)
    exclude_file = work_dir / "exclude"
    exclude_file.write_text("/one.jpg\n", encoding="utf-8")
    package = UploadPackage(work_dir, payload_dir, exclude_file, 1, 1)
    prep_kwargs: dict[str, object] = {}
    monkeypatch.setattr(
        "app.gui.main_window.prepare_upload_package",
        lambda _plan, **kwargs: prep_kwargs.update(kwargs) or package,
    )
    monkeypatch.setattr(worker, "_available_profile", lambda *args, **kwargs: profile)
    monkeypatch.setattr("app.gui.main_window.revalidate_upload_plan", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("revalidated")))
    commands: list[list[str]] = []
    monkeypatch.setattr(worker.runner, "run", lambda command, *_args, **_kwargs: commands.append(command) or 0)
    output: list[str] = []
    worker.output.connect(output.append)

    worker.run()

    assert prep_kwargs["build_archives"] is False
    assert len(commands) == 1
    assert "--dry-run" in commands[0]
    assert any("Would create and upload" in message for message in output)


def test_packaged_worker_retries_payload_with_delta_resume_and_cleans_after_success(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    plan = _plan(tmp_path)
    first = Profile(
        name="Q0101",
        username="user",
        host="ssh1.qriscloud.org.au",
        rsync_path="rsync.exe",
    )
    second = Profile(
        name="Q0101",
        username="user",
        host="ssh2.qriscloud.org.au",
        rsync_path="rsync.exe",
    )
    worker = PackagedUploadWorker(first, plan, "/data/Q0101", dry_run=False)
    worker.RETRY_DELAYS_SECONDS = (0,)
    available = iter((first, first, first, second))
    monkeypatch.setattr(worker, "_available_profile", lambda *args, **kwargs: next(available))
    commands: list[list[str]] = []
    results_to_return = iter((0, 10, 0))

    def run(command, *_args, **_kwargs) -> int:
        commands.append(command)
        return next(results_to_return)

    monkeypatch.setattr(worker.runner, "run", run)
    results: list[int] = []
    output: list[str] = []
    worker.finished.connect(results.append)
    worker.output.connect(output.append)

    worker.run()

    assert results == [0]
    assert len(commands) == 3
    assert "-W" in commands[1]
    assert "--no-whole-file" not in commands[1]
    assert "-W" not in commands[2]
    assert "--no-whole-file" in commands[2]
    assert commands[2][-1].startswith("user@ssh2.qriscloud.org.au:")
    assert any("partial transfer was kept" in message for message in output)
    assert not list(tmp_path.glob(".source.qris-upload-*"))


def test_packaged_worker_retains_package_after_retry_exhaustion(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    plan = _plan(tmp_path)
    profile = Profile(name="Q0101", username="user", host="host", rsync_path="rsync.exe")
    worker = PackagedUploadWorker(profile, plan, "/data/Q0101", dry_run=False)
    worker.RETRY_DELAYS_SECONDS = (0, 0)
    monkeypatch.setattr(worker, "_available_profile", lambda *args, **kwargs: profile)
    results_to_return = iter((0, 10, 12, 30))
    monkeypatch.setattr(worker.runner, "run", lambda *_args, **_kwargs: next(results_to_return))
    results: list[int] = []
    output: list[str] = []
    worker.finished.connect(results.append)
    worker.output.connect(output.append)

    worker.run()

    retained = list(tmp_path.glob(".source.qris-upload-*"))
    assert results == [30]
    assert len(retained) == 1
    assert (retained[0] / "payload").is_dir()
    assert any("automatic retry attempts have been used" in message for message in output)
    assert any(str(retained[0]) in message for message in output)


def test_packaged_worker_cancellation_during_recovery_cleans_package(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    plan = _plan(tmp_path)
    profile = Profile(name="Q0101", username="user", host="host", rsync_path="rsync.exe")
    worker = PackagedUploadWorker(profile, plan, "/data/Q0101", dry_run=False)
    monkeypatch.setattr(worker, "_available_profile", lambda *args, **kwargs: profile)
    call_count = 0

    def run(*_args, **_kwargs) -> int:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return 0
        worker._cancelled.set()
        return 10

    monkeypatch.setattr(worker.runner, "run", run)
    results: list[int] = []
    worker.finished.connect(results.append)

    worker.run()

    assert results == [130]
    assert not list(tmp_path.glob(".source.qris-upload-*"))


def test_packaged_retry_prefers_the_other_qriscloud_host(monkeypatch, tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    profile = Profile(
        name="Q0101",
        username="user",
        host="ssh1.qriscloud.org.au",
        rsync_path="rsync.exe",
    )
    worker = PackagedUploadWorker(profile, plan, "/data/Q0101", dry_run=False)
    attempted_hosts: list[str] = []

    def ssh_test(attempt, **_kwargs):
        attempted_hosts.append(attempt.host)
        return type("Result", (), {"returncode": 0, "output": ""})()

    monkeypatch.setattr("app.gui.main_window.run_ssh_test", ssh_test)

    selected = worker._available_profile(avoid_host="ssh1.qriscloud.org.au")

    assert selected is not None
    assert selected.host == "ssh2.qriscloud.org.au"
    assert attempted_hosts == ["ssh2.qriscloud.org.au"]
