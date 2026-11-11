from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

import pytest

from app.core.profiles import Profile
from app.core.receipt_recorder import (
    ReceiptPlan,
    record_transfer_receipt,
    upload_local_base,
    upload_receipt_roots,
)
from app.core.receipts import (
    ReceiptCancelled,
    ReceiptFile,
    TransferCollector,
    hash_transferred_files,
    sha256sum_text,
    unescape_rsync_name,
    write_receipt,
)
from app.core.rsync_command import build_rsync_command

PROFILE = Profile(
    name="Q0101 Test",
    username="alice",
    host="ssh1.qriscloud.org.au",
    collection_id="Q0101",
    remote_path="/data/Q0101",
    rsync_path=r"C:\msys64\usr\bin\rsync.exe",
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_collector_reads_sent_files_across_chunks_and_progress_lines() -> None:
    collector = TransferCollector("upload")
    output = (
        "sending incremental file list\n"
        "cd+++++++++ survey/\n"
        "<f+++++++++ survey/a b.jpg\n"
        "              0   0%    0.00kB/s    0:00:00 (xfr#0, to-chk=2/5)\r"
        "      1.00M  50%   1.00MB/s    0:00:01 (xfr#1, to-chk=1/5)\r\n"
        "<f.st...... survey/notes.txt\n"
        ">f+++++++++ survey/received-not-sent.jpg\n"
        "<f+++++++++ survey/caf\\#303\\#251.jpg"
    )
    for index in range(0, len(output), 7):
        collector.feed(output[index : index + 7])

    assert collector.finish() == ["survey/a b.jpg", "survey/notes.txt", "survey/café.jpg"]


def test_download_collector_reads_received_files_and_dedupes_retries() -> None:
    collector = TransferCollector("download")
    collector.feed(">f+++++++++ one.tif\n>f.st...... one.tif\n<f+++++++++ ignored.tif\n")

    assert collector.finish() == ["one.tif"]


def test_unescape_keeps_plain_names() -> None:
    assert unescape_rsync_name("plain name.jpg") == "plain name.jpg"
    assert unescape_rsync_name("tab\\#011here") == "tab\there"


def test_upload_local_base_matches_rsync_name_roots(tmp_path: Path) -> None:
    folder = tmp_path / "survey"
    folder.mkdir()
    single = folder / "a.jpg"
    single.write_bytes(b"x")

    assert upload_local_base(folder, files_from=False, include_source_directory=True) == tmp_path
    assert upload_local_base(folder, files_from=False, include_source_directory=False) == folder
    assert upload_local_base(single, files_from=False, include_source_directory=False) == folder
    assert upload_local_base(folder, files_from=True, include_source_directory=False) == folder


def test_hash_records_checksums_and_missing_files(tmp_path: Path) -> None:
    (tmp_path / "survey").mkdir()
    (tmp_path / "survey" / "a.jpg").write_bytes(b"alpha")

    files = hash_transferred_files(["survey/a.jpg", "survey/gone.jpg"], tmp_path, "/data/Q0101/")

    assert files[0].sha256 == _sha(b"alpha")
    assert files[0].bytes == 5
    assert files[0].remote_path == "/data/Q0101/survey/a.jpg"
    assert files[1].sha256 is None
    assert files[1].note.startswith("not hashed")


def test_hash_with_prefix_and_cancel(tmp_path: Path) -> None:
    (tmp_path / "img.tar").write_bytes(b"tar")
    files = hash_transferred_files(["img.tar"], tmp_path, "/data/Q0101/survey", relative_prefix="survey")
    assert files[0].path == "survey/img.tar"
    assert files[0].remote_path == "/data/Q0101/survey/img.tar"

    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(ReceiptCancelled):
        hash_transferred_files(["img.tar"], tmp_path, "/data", cancel_event=cancelled)


def test_sha256sum_text_is_coreutils_compatible() -> None:
    files = [
        ReceiptFile("survey/a b.jpg", "", "", 1, None, "a" * 64),
        ReceiptFile("odd\\name", "", "", 1, None, "b" * 64),
        ReceiptFile("missing", "", "", None, None, None),
    ]

    assert sha256sum_text(files) == f"{'a' * 64}  survey/a b.jpg\n\\{'b' * 64}  odd\\\\name\n"


def test_write_receipt_never_overwrites(tmp_path: Path) -> None:
    first = write_receipt({"n": 1}, [], tmp_path, "20261111T000000Z_upload")
    second = write_receipt({"n": 2}, [], tmp_path, "20261111T000000Z_upload")

    assert first[0].name == "20261111T000000Z_upload.json"
    assert second[0].name == "20261111T000000Z_upload-2.json"
    assert json.loads(first[0].read_text(encoding="utf-8")) == {"n": 1}


def _upload_plan(tmp_path: Path) -> ReceiptPlan:
    """Upload of folder 'survey' into /data/Q0101/project (rsync names include 'survey/')."""
    source = tmp_path / "survey"
    source.mkdir()
    (source / "a.jpg").write_bytes(b"alpha")
    plan = ReceiptPlan(
        "upload",
        *upload_receipt_roots(source, "/data/Q0101/project", files_from=False, include_source_directory=True),
    )
    collector = plan.add_phase(tmp_path, "/data/Q0101/project")
    collector.feed("<f+++++++++ survey/a.jpg\n")
    return plan


def test_upload_receipt_roots_are_the_synced_folders(tmp_path: Path) -> None:
    folder = tmp_path / "survey"
    folder.mkdir()
    (folder / "a.jpg").write_bytes(b"x")

    assert upload_receipt_roots(folder, "/data/Q0101/", files_from=False, include_source_directory=True) == (
        folder,
        "/data/Q0101/survey",
    )
    assert upload_receipt_roots(folder, "/data/Q0101", files_from=False, include_source_directory=False) == (
        folder,
        "/data/Q0101",
    )
    assert upload_receipt_roots(folder / "a.jpg", "/data/Q0101", files_from=False, include_source_directory=False) == (
        folder,
        "/data/Q0101",
    )
    assert upload_receipt_roots(folder, "/data/Q0101", files_from=True, include_source_directory=False) == (
        folder,
        "/data/Q0101",
    )


def test_completed_upload_writes_receipt_in_synced_folders(tmp_path: Path) -> None:
    plan = _upload_plan(tmp_path)
    commands: list[list[str]] = []
    uploaded: dict[str, str] = {}

    def run_command(command: list[str]) -> int:
        commands.append(command)
        source = command[-2]
        if len(source) > 3 and source[0] == "/" and source[2] == "/":  # MSYS /x/... -> X:/...
            source = f"{source[1].upper()}:{source[2:]}"
        for path in Path(source).iterdir():
            uploaded[path.name] = path.read_text(encoding="utf-8")
        return 0

    messages: list[str] = []
    written = record_transfer_receipt(plan, PROFILE, completed=True, emit=messages.append, run_command=run_command)

    receipt = json.loads(written[0].read_text(encoding="utf-8"))
    assert written[0].parent == tmp_path / "survey" / ".qris-receipts"
    assert written[0].name.endswith("_upload.json")
    assert receipt["status"] == "complete"
    assert receipt["local"] == str(tmp_path / "survey")
    assert receipt["remote"] == "/data/Q0101/project/survey"
    assert receipt["files"][0]["path"] == "a.jpg"
    assert receipt["files"][0]["remote_path"] == "/data/Q0101/project/survey/a.jpg"
    assert receipt["files"][0]["sha256"] == _sha(b"alpha")
    assert written[1].read_text(encoding="utf-8") == f"{_sha(b'alpha')}  a.jpg\n"
    assert commands[0][-1] == "alice@ssh1.qriscloud.org.au:/data/Q0101/project/survey/.qris-receipts/"
    assert set(uploaded) == {written[0].name, written[1].name}
    assert any("Receipt saved on QRIScloud" in message for message in messages)


def test_incomplete_upload_keeps_receipt_local_only(tmp_path: Path) -> None:
    plan = _upload_plan(tmp_path)
    calls: list[list[str]] = []
    messages: list[str] = []

    written = record_transfer_receipt(
        plan, PROFILE, completed=False, emit=messages.append, run_command=lambda command: calls.append(command) or 0
    )

    assert written[0].name.endswith("_upload_incomplete.json")
    assert json.loads(written[0].read_text(encoding="utf-8"))["status"] == "incomplete"
    assert not calls
    assert any("kept locally only" in message for message in messages)


def test_download_receipt_is_written_only_in_download_folder(tmp_path: Path) -> None:
    destination = tmp_path / "downloads"
    (destination / "raw").mkdir(parents=True)
    (destination / "raw" / "one.tif").write_bytes(b"tif")
    plan = ReceiptPlan("download", destination, "/data/Q0101/project")
    plan.add_phase(destination, "/data/Q0101/project").feed(">f+++++++++ raw/one.tif\n")
    calls: list[list[str]] = []

    written = record_transfer_receipt(
        plan, PROFILE, completed=True, emit=lambda _message: None, run_command=lambda command: calls.append(command) or 0
    )

    assert not calls
    assert written[0].parent == destination / ".qris-receipts"
    entry = json.loads(written[0].read_text(encoding="utf-8"))["files"][0]
    assert entry["path"] == "raw/one.tif"
    assert entry["remote_path"] == "/data/Q0101/project/raw/one.tif"


def test_unwritable_local_folder_still_uploads_receipt(tmp_path: Path) -> None:
    plan = _upload_plan(tmp_path)
    (tmp_path / "survey" / ".qris-receipts").write_text("a file blocks the folder", encoding="utf-8")
    calls: list[list[str]] = []
    messages: list[str] = []

    written = record_transfer_receipt(
        plan, PROFILE, completed=True, emit=messages.append, run_command=lambda command: calls.append(command) or 0
    )

    assert written == []
    assert len(calls) == 1
    assert any("could not save the receipt" in message for message in messages)


def test_no_transferred_files_writes_no_receipt(tmp_path: Path) -> None:
    plan = ReceiptPlan("upload", tmp_path, "/data/Q0101")
    plan.add_phase(tmp_path, "/data/Q0101").feed("sending incremental file list\n")
    messages: list[str] = []

    assert record_transfer_receipt(plan, PROFILE, completed=True, emit=messages.append) == []
    assert "no receipt was written" in messages[0]
    assert not (tmp_path / ".qris-receipts").exists()


def test_real_transfers_can_itemize_and_always_skip_receipt_folders(tmp_path: Path) -> None:
    command = build_rsync_command(PROFILE, tmp_path, dry_run=False, itemize=True)
    dry_run = build_rsync_command(PROFILE, tmp_path, dry_run=True)

    assert "--itemize-changes" in command
    assert "--dry-run" not in command
    assert "--exclude=.qris-receipts/" in command
    assert "--exclude=.qris-receipts/" in dry_run


def test_receipt_upload_error_still_returns_local_receipt(tmp_path: Path) -> None:
    plan = _upload_plan(tmp_path)
    messages: list[str] = []

    def failing(_command: list[str]) -> int:
        raise OSError("network down")

    written = record_transfer_receipt(plan, PROFILE, completed=True, emit=messages.append, run_command=failing)

    assert written and written[0].is_file()
    assert any("could not be uploaded" in message for message in messages)


def test_local_sync_scan_ignores_receipt_folders(tmp_path: Path) -> None:
    from app.core.sync_compare import scan_local_manifest

    (tmp_path / "data.csv").write_text("1", encoding="utf-8")
    (tmp_path / ".qris-receipts").mkdir()
    (tmp_path / ".qris-receipts" / "x_upload.json").write_text("{}", encoding="utf-8")

    assert set(scan_local_manifest(tmp_path)) == {"data.csv"}


def test_app_version_matches_pyproject() -> None:
    import tomllib

    from app import __version__
    from app.core.receipts import app_version

    pyproject = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    assert app_version() == __version__ == pyproject["project"]["version"]
