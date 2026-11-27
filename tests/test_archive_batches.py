"""Large backups: archives are built and uploaded in batches."""

from __future__ import annotations

from collections import namedtuple
from pathlib import Path

import app.core.archive_upload as archive_upload
from app.core.archive_upload import (
    analyze_upload_tree,
    plan_archive_batches,
    prepare_upload_package,
    split_batch_by_folder,
)
from app.core.profiles import Profile
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

    batches = plan_archive_batches(plan, batch_bytes=60).batches

    assert [len(batch.groups) for batch in batches] == [2, 1]
    assert [g.archive_name for b in batches for g in b.groups] == [g.archive_name for g in plan.groups]


def test_network_source_builds_on_local_temp_when_it_has_room(tmp_path, monkeypatch) -> None:
    plan = analyze_upload_tree(_tree(tmp_path), min_same_type=1)
    monkeypatch.setattr(archive_upload, "is_network_path", lambda _path: True)
    local = Path("Z:/temp")
    _free(monkeypatch, {"Z": 50 * GB, str(tmp_path)[:1].upper(): 1000 * GB})

    batch_plan = plan_archive_batches(plan, local_temp=local)

    assert batch_plan.work_parent == local
    assert len(batch_plan.batches) == 1


def test_network_source_falls_back_beside_source_when_local_temp_is_full(tmp_path, monkeypatch) -> None:
    plan = analyze_upload_tree(_tree(tmp_path), min_same_type=1)
    monkeypatch.setattr(archive_upload, "is_network_path", lambda _path: True)
    _free(monkeypatch, {"Z": 1 * GB, str(tmp_path)[:1].upper(): 1000 * GB})

    batch_plan = plan_archive_batches(plan, local_temp=Path("Z:/temp"))

    assert batch_plan.work_parent == plan.root.parent


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
    assert len(commands) == 2  # site0 and site2, one batch each after the split
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
