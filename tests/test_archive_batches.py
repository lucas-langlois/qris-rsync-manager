"""Large backups: archives are built and uploaded in batches."""

from __future__ import annotations

from collections import namedtuple
from pathlib import Path

import app.core.archive_upload as archive_upload
from app.core.archive_upload import (
    ARCHIVE_TEMP_RESERVE_BYTES,
    required_temp_bytes,
    analyze_upload_tree,
    plan_archive_batches,
    prepare_upload_package,
    split_batch_by_folder,
)
from app.core.profiles import Profile
from app.core.rsync_command import build_rsync_command
from app.core.receipt_recorder import ReceiptPlan, hash_phase_now, record_transfer_receipt
from app.gui.main_window import PackagedUploadWorker

Usage = namedtuple("Usage", "total used free")
GB = 1_000_000_000


def _tree(tmp_path: Path, folders: int = 3, files: int = 3, size: int = 10) -> Path:
    root = tmp_path / "share"
    for index in range(folders):
        folder = root / f"site{index}"
        folder.mkdir(parents=True)
        for number in range(files):
            (folder / f"p{number}.jpg").write_bytes(b"x" * size)
    return root


def _free(monkeypatch, by_drive: dict[str, int]) -> None:
    def usage(path):
        return Usage(0, 0, by_drive.get(str(path)[:1].upper(), 0))

    monkeypatch.setattr(archive_upload.shutil, "disk_usage", usage)


def test_batches_hold_whole_parts_and_cover_every_archive(tmp_path, monkeypatch) -> None:
    plan = analyze_upload_tree(_tree(tmp_path), min_same_type=1)
    _free(monkeypatch, {str(tmp_path)[:1].upper(): 1000 * GB})
    two = required_temp_bytes(plan.groups[:2])

    batch_plan = plan_archive_batches(plan, batch_bytes=two)

    assert [len(batch.groups) for batch in batch_plan.batches] == [2, 1]
    assert [g.archive_name for b in batch_plan.batches for g in b.groups] == [g.archive_name for g in plan.groups]
    assert batch_plan.too_big == ()


def test_archives_are_built_beside_the_source_or_inside_it_when_the_parent_is_read_only(tmp_path, monkeypatch) -> None:
    plan = analyze_upload_tree(_tree(tmp_path), min_same_type=1)
    assert plan_archive_batches(plan).work_parent == plan.root.parent

    monkeypatch.setattr(archive_upload, "_is_writable_folder", lambda folder: folder != plan.root.parent)
    assert plan_archive_batches(plan).work_parent == plan.root


def test_parts_too_big_for_the_free_space_are_skipped_not_fatal(tmp_path, monkeypatch) -> None:
    plan = analyze_upload_tree(_tree(tmp_path), min_same_type=1)
    # Room for one part only, after the reserve that stays free for colleagues.
    room = required_temp_bytes(plan.groups[:1])
    _free(monkeypatch, {str(tmp_path)[:1].upper(): ARCHIVE_TEMP_RESERVE_BYTES + room})
    monkeypatch.setattr(archive_upload, "required_temp_bytes",
                        lambda groups: sum(g.member_bytes for g in groups) * (10 ** 9 if any(
                            g.relative_folder == "site2" for g in groups) else 1))

    batch_plan = plan_archive_batches(plan)

    assert [group.relative_folder for group in batch_plan.too_big] == ["site2"]
    assert sorted(g.relative_folder for b in batch_plan.batches for g in b.groups) == ["site0", "site1"]


def test_work_folder_inside_the_source_is_never_scanned_or_uploaded(tmp_path) -> None:
    from app.core.file_scan import scan_folder

    root = _tree(tmp_path, folders=1)
    work = root / ".share.qris-upload-1234abcd" / "payload"
    work.mkdir(parents=True)
    (work / "site0.tar").write_bytes(b"tar")

    assert scan_folder(root).file_count == 3
    assert analyze_upload_tree(root, min_same_type=1).scan.file_count == 3
    command = build_rsync_command(Profile(username="u", host="h", collection_id="Q1"), root, remote_path="/data/Q1")
    assert "--exclude=.*.qris-upload-*/" in command


def test_work_folder_creation_does_not_retry_on_access_denied(tmp_path, monkeypatch) -> None:
    calls: list[str] = []

    def denied(path, *args, **kwargs):
        calls.append(path)
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(archive_upload.os, "mkdir", denied)
    import pytest

    with pytest.raises(PermissionError):
        archive_upload._make_work_dir(tmp_path, ".x.qris-upload-")
    assert len(calls) == 1


def test_batch_with_some_parts_of_a_folder_builds(tmp_path) -> None:
    root = _tree(tmp_path, folders=1, files=4, size=100)
    plan = analyze_upload_tree(root, min_same_type=1, max_part_bytes=200)
    assert len(plan.groups) == 2
    batches = plan_archive_batches(plan, batch_bytes=1).batches
    assert len(batches) == 2

    package = prepare_upload_package(batches[1], build_archives=True, work_parent=tmp_path)
    try:
        assert package.archive_count == 1
        assert len(list(package.payload_dir.rglob("*.tar"))) == 1
    finally:
        package.cleanup()


def test_split_batch_by_folder(tmp_path) -> None:
    plan = analyze_upload_tree(_tree(tmp_path), min_same_type=1)
    parts = split_batch_by_folder(plan)
    assert [len(part.folders) for part in parts] == [1, 1, 1]


def test_folder_that_changes_is_skipped_and_the_rest_upload(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    root = _tree(tmp_path)
    plan = analyze_upload_tree(root, min_same_type=1)
    (root / "site1" / "p0.jpg").write_bytes(b"edited by a colleague")
    profile = Profile(name="Q1", username="u", host="h", rsync_path="rsync.exe")
    worker = PackagedUploadWorker(profile, plan, "/data/Q1", dry_run=False)
    monkeypatch.setattr(worker, "_available_profile", lambda *args, **kwargs: profile)
    commands: list[list[str]] = []
    monkeypatch.setattr(worker.runner, "run", lambda command, *_a, **_k: commands.append(command) or 0)
    output: list[str] = []
    worker.output.connect(output.append)
    results: list[int] = []
    worker.finished.connect(results.append)
    # The loose pass is checked separately; here the change happens after it.
    monkeypatch.setattr(
        archive_upload, "revalidate_upload_plan", archive_upload.revalidate_upload_plan
    )

    code = worker._upload_archives_in_batches(plan, "/data/Q1", tmp_path / "log.txt")

    assert code == PackagedUploadWorker.PARTIAL_TRANSFER_CODE
    text = "".join(output)
    assert "site1" in text and "Run Synchronise later" in text
    # A quick listing check drops site1; site0 and site2 still go up in one batch.
    assert len(commands) == 1
    assert not list(tmp_path.glob(".share.qris-upload-*"))


def test_batch_hashes_survive_cleanup(tmp_path) -> None:
    local = tmp_path / "batch"
    local.mkdir()
    (local / "a.tar").write_bytes(b"tar")
    receipt = ReceiptPlan("upload", tmp_path, "/data/Q1")
    collector = receipt.add_phase(local, "/data/Q1")
    collector.feed("<f+++++++++ a.tar\n")
    messages: list[str] = []

    hash_phase_now(receipt, collector, emit=messages.append)
    (local / "a.tar").unlink()
    written = record_transfer_receipt(
        receipt, Profile(name="Q1", username="u", host="h"), completed=False, emit=messages.append
    )

    sums = [path for path in written if path.suffix == ".sha256"][0].read_text(encoding="utf-8")
    assert "a.tar" in sums and "not hashed" not in "".join(messages)


def test_changed_folder_before_start_is_skipped_not_fatal(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    root = _tree(tmp_path)
    plan = analyze_upload_tree(root, min_same_type=1)
    (root / "site0" / "p9.jpg").write_bytes(b"new photo")  # added after the scan
    profile = Profile(name="Q1", username="u", host="h", rsync_path="rsync.exe")
    worker = PackagedUploadWorker(profile, plan, "/data/Q1", dry_run=False)
    monkeypatch.setattr(worker, "_available_profile", lambda *args, **kwargs: profile)
    commands: list[list[str]] = []
    monkeypatch.setattr(worker.runner, "run", lambda command, *_a, **_k: commands.append(command) or 0)
    results: list[int] = []
    worker.finished.connect(results.append)
    output: list[str] = []
    worker.output.connect(output.append)

    worker.run()

    assert results == [PackagedUploadWorker.PARTIAL_TRANSFER_CODE]
    assert len(commands) == 2  # loose files, then one archive batch
    assert "site0" in "".join(output)


def test_changed_folder_gets_no_loose_files_either(tmp_path) -> None:
    root = _tree(tmp_path, folders=2)
    plan = analyze_upload_tree(root, min_same_type=1)
    (root / "site1" / "p0.jpg").write_bytes(b"edited")
    changed: list[Path] = []

    package = prepare_upload_package(plan, build_archives=False, skip_changed=changed)
    try:
        rules = package.exclude_file.read_text(encoding="utf-8").splitlines()
    finally:
        package.cleanup()

    assert changed == [root / "site1"]
    assert rules[-2:] == ["+ /site1/*/", "- /site1/*"]
