from __future__ import annotations

import tarfile
import threading
import subprocess
import sys
from pathlib import Path

import pytest

from app.core.archive_upload import (
    NATIVE_TAR_BLOCKING_FACTOR,
    ArchiveCancelled,
    FileSnapshot,
    _native_tar_command,
    _native_tar_commands,
    _windows_io_path,
    analyze_upload_tree,
    build_upload_package,
)


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_analysis_qualifies_on_either_strict_folder_threshold(tmp_path: Path) -> None:
    root = tmp_path / "source"
    _write(root / "a.jpg", b"12345")
    _write(root / "b.jpg", b"12345")

    neither_over = analyze_upload_tree(root, min_flat_files=2, min_folder_bytes=10)
    size_only = analyze_upload_tree(root, min_flat_files=99, min_folder_bytes=9)
    count_only = analyze_upload_tree(root, min_flat_files=1, min_folder_bytes=99)

    assert not neither_over.groups
    assert [group.category for group in size_only.groups] == ["photos"]
    assert [group.category for group in count_only.groups] == ["photos"]


def test_photos_videos_and_matching_sidecars_are_archived_separately(tmp_path: Path) -> None:
    root = tmp_path / "survey"
    _write(root / "station01.jpg", b"photo")
    _write(root / "station01.xmp", b"photo metadata")
    _write(root / "transect01.mp4", b"video")
    _write(root / "transect01.mp4.json", b"video metadata")
    _write(root / "field-notes.txt", b"keep loose")

    plan = analyze_upload_tree(root, min_flat_files=4, min_folder_bytes=1)

    assert [(group.category, group.media_count, group.member_count) for group in plan.groups] == [
        ("photos", 1, 2),
        ("videos", 1, 2),
    ]
    package = build_upload_package(plan)
    try:
        photo_tar = package.payload_dir / "survey__photos.tar"
        video_tar = package.payload_dir / "survey__videos.tar"
        with tarfile.open(photo_tar) as archive:
            assert archive.getnames() == ["station01.jpg", "station01.xmp"]
        with tarfile.open(video_tar) as archive:
            assert archive.getnames() == ["transect01.mp4", "transect01.mp4.json"]

        exclusions = package.exclude_file.read_text(encoding="utf-8").splitlines()
        assert "/station01.jpg" in exclusions
        assert "/station01.xmp" in exclusions
        assert "/transect01.mp4" in exclusions
        assert "/transect01.mp4.json" in exclusions
        assert all("field-notes.txt" not in line for line in exclusions)

        inventory = (package.payload_dir / "survey__photos.tar.inventory.txt").read_text(encoding="utf-8")
        assert "filename\tbytes\tmodified_utc" in inventory
        assert "station01.jpg" in inventory
        assert "station01.xmp" in inventory
        assert (root / "station01.jpg").exists()
    finally:
        work_dir = package.work_dir
        package.cleanup()
    assert not work_dir.exists()


def test_native_tar_handles_unicode_spaces_and_leading_dash_names(tmp_path: Path) -> None:
    root = tmp_path / "survey"
    for name in ("-leading.jpg", "café photo.jpg", "ordinary.jpg"):
        _write(root / name, name.encode("utf-8"))
    plan = analyze_upload_tree(root, min_flat_files=2, min_folder_bytes=1)

    package = build_upload_package(plan)
    try:
        with tarfile.open(package.payload_dir / "survey__photos.tar") as archive:
            assert archive.getnames() == ["-leading.jpg", "café photo.jpg", "ordinary.jpg"]
    finally:
        package.cleanup()


def test_native_tar_command_uses_one_mib_block_and_nul_member_list(tmp_path: Path) -> None:
    command = _native_tar_command(
        r"C:\Windows\System32\tar.exe",
        tmp_path / "output.tar",
        tmp_path / "source folder",
        ["one.jpg", "-leading.jpg"],
        append=False,
    )

    assert command[:5] == [
        r"C:\Windows\System32\tar.exe",
        "-c",
        "-b",
        str(NATIVE_TAR_BLOCKING_FACTOR),
        "--format",
    ]
    assert NATIVE_TAR_BLOCKING_FACTOR * 512 == 1_048_576
    assert command[-3:] == ["--", "one.jpg", "-leading.jpg"]


def test_native_tar_splits_long_windows_commands_into_create_then_append_batches(tmp_path: Path) -> None:
    root = tmp_path / "source"
    members = [FileSnapshot(root / f"very-long-photo-name-{index:03d}.jpg", 1, 1) for index in range(10)]

    commands = _native_tar_commands(
        "tar.exe",
        tmp_path / "output.tar",
        root,
        members,
        command_limit=180,
    )

    assert len(commands) > 1
    assert commands[0][1] == "-c"
    assert all(command[1] == "-r" for command in commands[1:])
    assert [name for command in commands for name in command[command.index("--") + 1 :]] == [
        member.path.name for member in members
    ]


def test_ambiguous_same_stem_sidecar_stays_outside_archives(tmp_path: Path) -> None:
    root = tmp_path / "survey"
    _write(root / "sample.jpg", b"photo")
    _write(root / "sample.mp4", b"video")
    _write(root / "sample.xmp", b"ambiguous")

    plan = analyze_upload_tree(root, min_flat_files=2, min_folder_bytes=1)
    package = build_upload_package(plan)
    try:
        exclusions = package.exclude_file.read_text(encoding="utf-8")
        assert "/sample.jpg" in exclusions
        assert "/sample.mp4" in exclusions
        assert "sample.xmp" not in exclusions
    finally:
        package.cleanup()


def test_qualifying_nested_folder_preserves_remote_relative_path(tmp_path: Path) -> None:
    root = tmp_path / "survey"
    folder = root / "camera A"
    _write(folder / "one.jpg", b"123")
    _write(folder / "two.jpg", b"456")

    plan = analyze_upload_tree(root, min_flat_files=1, min_folder_bytes=5)
    package = build_upload_package(plan)
    try:
        assert (package.payload_dir / "camera A" / "camera A__photos.tar").is_file()
        assert "/camera A/one.jpg" in package.exclude_file.read_text(encoding="utf-8")
    finally:
        package.cleanup()


def test_cancelled_package_build_removes_temporary_directory(tmp_path: Path) -> None:
    root = tmp_path / "survey"
    _write(root / "one.jpg", b"123")
    _write(root / "two.jpg", b"456")
    plan = analyze_upload_tree(root, min_flat_files=1, min_folder_bytes=5)
    cancelled = threading.Event()
    cancelled.set()

    with pytest.raises(ArchiveCancelled):
        build_upload_package(plan, cancel_event=cancelled)

    assert not list(tmp_path.glob(".survey.qris-upload-*"))


def test_cancellation_stops_native_tar_and_removes_partial_package(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "survey"
    _write(root / "one.jpg", b"123")
    _write(root / "two.jpg", b"456")
    plan = analyze_upload_tree(root, min_flat_files=1, min_folder_bytes=5)
    cancelled = threading.Event()

    class FakeProcess:
        returncode = None
        terminated = False

        def communicate(self, timeout=None):
            if self.returncode is not None:
                return "", None
            raise subprocess.TimeoutExpired("tar.exe", timeout)

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    process = FakeProcess()
    monkeypatch.setattr("app.core.archive_upload.subprocess.Popen", lambda *_args, **_kwargs: process)

    with pytest.raises(ArchiveCancelled):
        build_upload_package(
            plan,
            cancel_event=cancelled,
            progress_callback=lambda message: cancelled.set() if "Native tar.exe started" in message else None,
        )

    assert process.terminated
    assert not list(tmp_path.glob(".survey.qris-upload-*"))


def test_rsync_exclusions_escape_wildcards_in_real_filenames(tmp_path: Path) -> None:
    root = tmp_path / "survey"
    _write(root / "photo[1].jpg", b"123")
    _write(root / "photo[2].jpg", b"456")
    plan = analyze_upload_tree(root, min_flat_files=1, min_folder_bytes=5)

    package = build_upload_package(plan)
    try:
        exclusions = package.exclude_file.read_text(encoding="utf-8")
        assert r"/photo\[1\].jpg" in exclusions
        assert r"/photo\[2\].jpg" in exclusions
    finally:
        package.cleanup()


def test_build_refuses_exact_tree_change_after_confirmation(tmp_path: Path) -> None:
    root = tmp_path / "survey"
    _write(root / "one.jpg", b"123")
    _write(root / "two.jpg", b"456")
    plan = analyze_upload_tree(root, min_flat_files=1, min_folder_bytes=5)
    (root / "one.jpg").rename(root / "renamed.jpg")

    with pytest.raises(RuntimeError, match="changed after it was scanned"):
        build_upload_package(plan)

    assert not list(tmp_path.glob(".survey.qris-upload-*"))


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows extended-path regression")
def test_package_writes_inventory_beyond_windows_max_path(tmp_path: Path) -> None:
    root = tmp_path / "survey"
    long_folder = root / ("D" * 80)
    _write(long_folder / "one.jpg", b"123")
    _write(long_folder / "two.jpg", b"456")
    plan = analyze_upload_tree(root, min_flat_files=1, min_folder_bytes=5)

    package = build_upload_package(plan)
    try:
        inventory = package.payload_dir / long_folder.name / f"{long_folder.name}__photos.tar.inventory.txt"
        assert len(str(inventory)) > 260
        with open(_windows_io_path(inventory), encoding="utf-8") as inventory_file:
            contents = inventory_file.read()
        assert "one.jpg" in contents
        assert "two.jpg" in contents
    finally:
        cleanup_error = package.cleanup()
    assert cleanup_error is None
