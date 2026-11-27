"""Synchronise window: comparison rules, archive awareness, and the dialog."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from app.core.archive_upload import build_upload_package
from app.core.sync_compare import (
    DOWNLOAD,
    MODE_BOTH,
    MODE_DOWNLOAD,
    MODE_UPLOAD,
    UPLOAD,
    FileRecord,
    SyncStatus,
    build_sync_plan,
    scan_local_for_sync,
)


def _rec(path: str, size: int, mtime: float) -> FileRecord:
    return FileRecord(path, size, mtime)


def _by_path(plan):
    return {item.path: item for item in plan.items}


def _sample_plan(mode=MODE_BOTH):
    local = {
        "same.txt": _rec("same.txt", 5, 100.0),
        "new_here.txt": _rec("new_here.txt", 1, 100.0),
        "edited_here.txt": _rec("edited_here.txt", 9, 500.0),
        "edited_there.txt": _rec("edited_there.txt", 9, 100.0),
        "odd.txt": _rec("odd.txt", 3, 100.0),
        ".qris-receipts/r.json": _rec(".qris-receipts/r.json", 1, 1.0),
    }
    remote = {
        "same.txt": _rec("same.txt", 5, 101.0),
        "new_there.txt": _rec("new_there.txt", 1, 100.0),
        "edited_here.txt": _rec("edited_here.txt", 8, 100.0),
        "edited_there.txt": _rec("edited_there.txt", 8, 500.0),
        "odd.txt": _rec("odd.txt", 4, 100.5),
        "old/stuff.tar": _rec("old/stuff.tar", 10, 1.0),
        "sub/.qris-receipts/r.sha256": _rec("sub/.qris-receipts/r.sha256", 1, 1.0),
    }
    return build_sync_plan(local, remote, mode=mode)


def test_statuses_and_defaults_both_ways() -> None:
    plan = _sample_plan()
    rows = _by_path(plan)

    assert plan.identical_files == 1
    assert set(rows) == {"new_here.txt", "edited_here.txt", "edited_there.txt", "odd.txt", "new_there.txt", "old/stuff.tar"}
    expected = {
        "new_here.txt": (SyncStatus.LOCAL_ONLY, UPLOAD, True),
        "edited_here.txt": (SyncStatus.LOCAL_NEWER, UPLOAD, True),
        "edited_there.txt": (SyncStatus.REMOTE_NEWER, DOWNLOAD, True),
        "new_there.txt": (SyncStatus.REMOTE_ONLY, DOWNLOAD, True),
        # No safe default: same time, different size.
        "odd.txt": (SyncStatus.CONFLICT, UPLOAD, False),
        # Big archives on QRIScloud are never downloaded by default.
        "old/stuff.tar": (SyncStatus.REMOTE_ARCHIVE, DOWNLOAD, False),
    }
    for path, (status, direction, checked) in expected.items():
        assert (rows[path].status, rows[path].direction, rows[path].checked) == (status, direction, checked), path


def test_one_way_modes_never_tick_the_other_direction() -> None:
    upload = _by_path(_sample_plan(MODE_UPLOAD))
    assert not upload["new_there.txt"].checked
    assert upload["edited_there.txt"].direction == UPLOAD and not upload["edited_there.txt"].checked
    assert upload["new_here.txt"].checked and upload["edited_here.txt"].checked

    download = _by_path(_sample_plan(MODE_DOWNLOAD))
    assert not download["new_here.txt"].checked
    assert download["edited_here.txt"].direction == DOWNLOAD and not download["edited_here.txt"].checked
    assert download["new_there.txt"].checked and download["edited_there.txt"].checked


def test_one_sided_rows_cannot_be_sent_the_wrong_way() -> None:
    rows = _by_path(_sample_plan())
    assert rows["new_here.txt"].allowed_directions == (UPLOAD,)
    assert rows["new_there.txt"].allowed_directions == (DOWNLOAD,)
    with pytest.raises(ValueError):
        rows["new_here.txt"].set_direction(DOWNLOAD)


def test_replacing_a_newer_copy_is_flagged() -> None:
    rows = _by_path(_sample_plan())
    item = rows["edited_there.txt"]
    assert not item.replaces_newer
    item.set_direction(UPLOAD)
    assert item.replaces_newer and item.replaces_existing


# --- automatic archives -------------------------------------------------------------


def _photo_folder(tmp_path: Path, count: int = 60) -> Path:
    root = tmp_path / "survey"
    photos = root / "photos"
    photos.mkdir(parents=True)
    for index in range(count):
        path = photos / f"IMG_{index:04d}.jpg"
        path.write_bytes(b"x" * (index + 1))
        os.utime(path, (1_700_000_000 + index, 1_700_000_000 + index))
    (root / "notes.txt").write_text("hello", encoding="utf-8")
    return root


def test_local_scan_groups_archive_members(tmp_path: Path) -> None:
    root = _photo_folder(tmp_path)

    scan = scan_local_for_sync(root)

    assert "notes.txt" in scan.records and "photos/IMG_0000.jpg" in scan.records
    assert len(scan.archives) == 1
    archive = scan.archives[0]
    assert archive.relative_folder == "photos" and archive.member_count == 60
    assert archive.parts[0].archive_name == "photos_jpg.tar"


def test_archive_folder_is_one_row_until_uploaded(tmp_path: Path) -> None:
    root = _photo_folder(tmp_path)
    scan = scan_local_for_sync(root)

    plan = build_sync_plan(scan.records, {}, scan.archives)

    rows = _by_path(plan)
    assert set(rows) == {"notes.txt", "photos"}
    assert rows["photos"].status == SyncStatus.ARCHIVE_NEW and rows["photos"].checked
    assert rows["photos"].allowed_directions == (UPLOAD,)


def test_uploaded_archive_matching_local_files_is_up_to_date(tmp_path: Path) -> None:
    root = _photo_folder(tmp_path)
    scan = scan_local_for_sync(root)
    # Build the real archive, then present its files as the remote listing.
    package = build_upload_package(scan.plan)
    try:
        payload = package.payload_dir / "photos"
        remote = {
            f"photos/{path.name}": FileRecord(f"photos/{path.name}", path.stat().st_size, path.stat().st_mtime)
            for path in payload.iterdir()
        }
    finally:
        package.cleanup()
    remote["notes.txt"] = scan.records["notes.txt"]

    plan = build_sync_plan(scan.records, remote, scan.archives)

    assert plan.items == []
    assert plan.identical_archives == 1 and plan.identical_files == 1


def test_changed_member_makes_the_archive_out_of_date(tmp_path: Path) -> None:
    root = _photo_folder(tmp_path)
    scan = scan_local_for_sync(root)
    part = scan.archives[0].parts[0]
    remote = {
        "photos/photos_jpg.tar": FileRecord("photos/photos_jpg.tar", 99_999, part.modified),
        "photos/photos_jpg.tar.inventory.txt": FileRecord(
            "photos/photos_jpg.tar.inventory.txt", part.inventory_size, part.modified
        ),
    }
    (root / "photos" / "IMG_0003.jpg").write_bytes(b"changed and longer")

    rescanned = scan_local_for_sync(root)
    plan = build_sync_plan(rescanned.records, remote, rescanned.archives)

    rows = _by_path(plan)
    assert rows["photos"].status == SyncStatus.ARCHIVE_CHANGED
    # Members and the remote archive files are never listed one by one.
    assert not any(path.startswith("photos/") for path in rows)


# --- dialog ---------------------------------------------------------------------------


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _dialog(plan=None, **kwargs):
    from app.gui.sync_dialog import SynchronizeDialog

    _app()
    return SynchronizeDialog(plan or _sample_plan(), r"C:\data\survey", "/data/Q0101/survey", **kwargs)


def test_dialog_summary_and_direction_toggle() -> None:
    from app.gui.sync_dialog import COL_ACTION, COL_TICK

    dialog = _dialog()
    model = dialog.model
    row = next(i for i, item in enumerate(dialog.plan.items) if item.path == "edited_there.txt")

    assert "Upload 2" in dialog.summary_label.text() and "Download 2" in dialog.summary_label.text()
    model.toggle_direction(row)
    assert dialog.plan.items[row].direction == UPLOAD
    assert "replace a newer copy" in dialog.warning_label.text()

    model.setData(model.index(row, COL_TICK), Qt.Unchecked, Qt.CheckStateRole)
    assert not dialog.plan.items[row].checked
    assert model.data(model.index(row, COL_ACTION)).startswith("(")
    dialog.close()


def test_bulk_download_skips_rows_that_cannot_download() -> None:
    dialog = _dialog()
    rows = list(range(len(dialog.plan.items)))

    dialog.model.update_rows(rows, checked=True, direction=DOWNLOAD)

    items = _by_path(dialog.plan)
    assert items["new_here.txt"].direction == UPLOAD  # only exists locally
    assert items["edited_here.txt"].direction == DOWNLOAD
    dialog.close()


def test_mode_buttons_reset_rows() -> None:
    dialog = _dialog()
    upload_button = next(b for b in dialog.mode_buttons.buttons() if b.property("mode") == MODE_UPLOAD)

    upload_button.click()

    assert not _by_path(dialog.plan)["new_there.txt"].checked
    dialog.close()


def test_confirmation_lists_replacements_and_never_deletes() -> None:
    dialog = _dialog()

    text = dialog.confirmation_text()

    assert "Upload 2 item(s)" in text and "Download 2 item(s)" in text
    assert "existing file(s) on QRIScloud will be replaced" in text
    assert "Nothing is deleted" in text
    dialog.close()


def test_synchronise_needs_confirmation() -> None:
    from PySide6.QtWidgets import QDialog

    answers = [False, True]
    dialog = _dialog(confirm=lambda: answers.pop(0))

    dialog._synchronise_clicked()
    assert dialog.result() != QDialog.Accepted
    dialog._synchronise_clicked()
    assert dialog.result() == QDialog.Accepted


def test_filter_and_ticked_only() -> None:
    dialog = _dialog()
    dialog.proxy.set_text("edited")
    assert dialog.proxy.rowCount() == 2
    dialog.proxy.set_text("")
    dialog.proxy.set_ticked_only(True)
    assert dialog.proxy.rowCount() == sum(1 for item in dialog.plan.items if item.checked)
    dialog.close()


def test_archive_uploaded_under_the_old_folder_only_name_counts_as_up_to_date(tmp_path: Path) -> None:
    # Before v1.3.3 photos/ was archived as photos.tar; it must not be sent
    # again as photos_jpg.tar.
    root = _photo_folder(tmp_path)
    scan = scan_local_for_sync(root)
    part = scan.archives[0].parts[0]
    assert (part.archive_name, part.legacy_archive_name) == ("photos_jpg.tar", "photos.tar")
    remote = {
        "notes.txt": scan.records["notes.txt"],
        "photos/photos.tar": FileRecord("photos/photos.tar", 99_999, part.modified),
        "photos/photos.tar.inventory.txt": FileRecord(
            "photos/photos.tar.inventory.txt", part.legacy_inventory_size, part.modified
        ),
    }

    plan = build_sync_plan(scan.records, remote, scan.archives)

    assert plan.identical_archives == 1 and plan.items == []


def test_archive_names_say_which_types_they_hold(tmp_path: Path) -> None:
    from app.core.archive_upload import analyze_upload_tree

    root = tmp_path / "PILOT"
    root.mkdir()
    for index in range(3):
        (root / f"L{index}.txt").write_text("x", encoding="utf-8")
        (root / f"R{index}").write_text("x", encoding="utf-8")
    (root / "I79.TIF").write_bytes(b"tif")

    names = [group.archive_name for group in analyze_upload_tree(root, min_same_type=2).groups]

    assert names == ["PILOT_noext_txt.tar"]
