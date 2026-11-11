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
    _group_snapshots,
    _archive_parts,
    _windows_io_path,
    analyze_upload_tree,
    build_upload_package,
    prepare_upload_package,
    revalidate_upload_plan,
)


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_analysis_qualifies_only_on_strict_file_count(tmp_path: Path) -> None:
    root = tmp_path / "source"
    _write(root / "a.jpg", b"12345")
    _write(root / "b.jpg", b"12345")

    at_limit = analyze_upload_tree(root, min_same_type=2)
    over_limit = analyze_upload_tree(root, min_same_type=1)

    assert not at_limit.groups
    assert [group.category for group in over_limit.groups] == ["jpg"]


def test_few_large_files_are_never_archived_by_size(tmp_path: Path) -> None:
    root = tmp_path / "outputs"
    _write(root / "big_ortho.tif", b"x" * 1000)
    _write(root / "big_mosaic.tif", b"x" * 1000)

    plan = analyze_upload_tree(root, max_part_bytes=10)

    assert not plan.groups


def test_qualifying_type_and_named_companions_share_one_archive(tmp_path: Path) -> None:
    root = tmp_path / "survey"
    for index in range(3):
        _write(root / f"station0{index}.jpg", b"photo")
    _write(root / "station00.xmp", b"photo metadata")
    _write(root / "transect01.mp4", b"video")
    _write(root / "field-notes.txt", b"keep loose")

    plan = analyze_upload_tree(root, min_same_type=2)

    assert [(group.archive_name, group.category, group.media_count, group.member_count) for group in plan.groups] == [
        ("survey.tar", "jpg", 3, 4),
    ]
    package = build_upload_package(plan)
    try:
        with tarfile.open(package.payload_dir / "survey.tar") as archive:
            assert archive.getnames() == ["station00.jpg", "station00.xmp", "station01.jpg", "station02.jpg"]

        exclusions = package.exclude_file.read_text(encoding="utf-8").splitlines()
        assert "/station00.xmp" in exclusions
        assert all("transect01.mp4" not in line and "field-notes.txt" not in line for line in exclusions)

        inventory = (package.payload_dir / "survey.tar.inventory.txt").read_text(encoding="utf-8")
        assert "# types: jpg" in inventory
        assert "filename\tbytes\tmodified_utc" in inventory
        assert "station00.xmp" in inventory
        assert (root / "station00.jpg").exists()
    finally:
        work_dir = package.work_dir
        package.cleanup()
    assert not work_dir.exists()


def test_native_tar_handles_unicode_spaces_and_leading_dash_names(tmp_path: Path) -> None:
    root = tmp_path / "survey"
    for name in ("-leading.jpg", "café photo.jpg", "ordinary.jpg"):
        _write(root / name, name.encode("utf-8"))
    plan = analyze_upload_tree(root, min_same_type=2)

    package = build_upload_package(plan)
    try:
        with tarfile.open(package.payload_dir / "survey.tar") as archive:
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
    two_members = _native_tar_command(
        "tar.exe", tmp_path / "output.tar", root,
        [member.path.name for member in members[:2]], append=False,
    )
    command_limit = len(subprocess.list2cmdline(two_members).encode("utf-16-le")) // 2

    commands = _native_tar_commands(
        "tar.exe",
        tmp_path / "output.tar",
        root,
        members,
        command_limit=command_limit,
    )

    assert len(commands) > 1
    assert commands[0][1] == "-c"
    assert all(command[1] == "-r" for command in commands[1:])
    assert all(len(subprocess.list2cmdline(command).encode("utf-16-le")) // 2 <= command_limit for command in commands)
    assert [name for command in commands for name in command[command.index("--") + 1 :]] == [
        member.path.name for member in members
    ]


def test_native_tar_batches_astral_names_within_windows_limit(tmp_path: Path) -> None:
    root = tmp_path / "source"
    names = ["\U0001f4f7" * 100 + f"{index}.jpg" for index in range(220)]
    members = [FileSnapshot(root / name, 1, 1) for name in names]

    commands = _native_tar_commands("tar.exe", tmp_path / "output.tar", root, members)

    assert len(commands) > 1
    assert commands[0][1] == "-c"
    assert all(command[1] == "-r" for command in commands[1:])
    assert all(len(subprocess.list2cmdline(command).encode("utf-16-le")) // 2 <= 30_000 for command in commands)
    assert [name for command in commands for name in command[command.index("--") + 1:]] == names


def test_native_tar_rejects_single_member_exceeding_command_limit(tmp_path: Path) -> None:
    root = tmp_path / "source"
    member = FileSnapshot(root / ("\U0001f4f7" * 100 + ".jpg"), 1, 1)
    command = _native_tar_command("tar.exe", tmp_path / "output.tar", root, [member.path.name], append=False)

    with pytest.raises(ValueError, match="command-line limit"):
        _native_tar_commands(
            "tar.exe", tmp_path / "output.tar", root, [member],
            command_limit=len(subprocess.list2cmdline(command)),
        )


def test_same_named_file_of_other_type_joins_archive_but_unrelated_stays_loose(tmp_path: Path) -> None:
    root = tmp_path / "survey"
    for name in ("sample.jpg", "other.jpg", "third.jpg"):
        _write(root / name, b"photo")
    _write(root / "sample.mp4", b"video with the same base name")
    _write(root / "unrelated.mp4", b"video")

    plan = analyze_upload_tree(root, min_same_type=2)
    package = build_upload_package(plan)
    try:
        exclusions = package.exclude_file.read_text(encoding="utf-8")
        assert "/sample.jpg" in exclusions
        assert "/sample.mp4" in exclusions
        assert "unrelated.mp4" not in exclusions
    finally:
        package.cleanup()


def test_qualifying_nested_folder_preserves_remote_relative_path(tmp_path: Path) -> None:
    root = tmp_path / "survey"
    folder = root / "camera A"
    _write(folder / "one.jpg", b"123")
    _write(folder / "two.jpg", b"456")

    plan = analyze_upload_tree(root, min_same_type=1)
    package = build_upload_package(plan)
    try:
        assert (package.payload_dir / "camera A" / "camera A.tar").is_file()
        assert "/camera A/one.jpg" in package.exclude_file.read_text(encoding="utf-8")
    finally:
        package.cleanup()


def test_cancelled_package_build_removes_temporary_directory(tmp_path: Path) -> None:
    root = tmp_path / "survey"
    _write(root / "one.jpg", b"123")
    _write(root / "two.jpg", b"456")
    plan = analyze_upload_tree(root, min_same_type=1)
    cancelled = threading.Event()
    cancelled.set()

    with pytest.raises(ArchiveCancelled):
        build_upload_package(plan, cancel_event=cancelled)

    assert not list(tmp_path.glob(".survey.qris-upload-*"))


def test_cancellation_stops_native_tar_and_removes_partial_package(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "survey"
    _write(root / "one.jpg", b"123")
    _write(root / "two.jpg", b"456")
    plan = analyze_upload_tree(root, min_same_type=1)
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
    plan = analyze_upload_tree(root, min_same_type=1)

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
    plan = analyze_upload_tree(root, min_same_type=1)
    (root / "one.jpg").rename(root / "renamed.jpg")

    with pytest.raises(RuntimeError, match="changed after it was scanned"):
        build_upload_package(plan)

    assert not list(tmp_path.glob(".survey.qris-upload-*"))


def test_revalidate_refuses_selected_folder_replaced_by_symlink(tmp_path: Path) -> None:
    root = tmp_path / "survey"
    selected = root / "camera"
    _write(selected / "one.jpg", b"123")
    _write(selected / "two.jpg", b"456")
    plan = analyze_upload_tree(root, min_same_type=1)
    replacement = tmp_path / "replacement"
    _write(replacement / "one.jpg", b"123")
    _write(replacement / "two.jpg", b"456")
    selected.rename(root / "camera-original")
    try:
        selected.symlink_to(replacement, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(RuntimeError, match="changed after it was scanned"):
        revalidate_upload_plan(plan)


def test_dry_preparation_writes_only_exclusions_and_exposes_planned_groups(tmp_path: Path) -> None:
    root = tmp_path / "survey"
    _write(root / "one.jpg", b"123")
    _write(root / "two.jpg", b"456")
    plan = analyze_upload_tree(root, min_same_type=1)

    package = prepare_upload_package(plan, build_archives=False, include_source_directory=True)
    try:
        assert package.archive_count == package.inventory_count == 0
        assert package.planned_groups == tuple(plan.groups)
        assert package.payload_dir.exists()
        assert not list(package.payload_dir.rglob("*.tar"))
        assert "/survey/one.jpg" in package.exclude_file.read_text(encoding="utf-8")
    finally:
        package.cleanup()

    contents_package = prepare_upload_package(plan, build_archives=False, include_source_directory=False)
    try:
        exclusions = contents_package.exclude_file.read_text(encoding="utf-8")
        assert "/one.jpg" in exclusions
        assert "/survey/one.jpg" not in exclusions
    finally:
        contents_package.cleanup()


def test_companion_matching_handles_large_synthetic_folder() -> None:
    folder = Path("synthetic")
    snapshots = [
        FileSnapshot(folder / f"frame-{index:05d}.jpg", 1, 1)
        for index in range(2_000)
    ] + [
        FileSnapshot(folder / f"frame-{index:05d}.jpg.xmp", 1, 1)
        for index in range(2_000)
    ]

    parts = _archive_parts(snapshots, min_same_type=50)

    assert len(parts) == 1
    assert len(parts[0][1]) == 4_000


def test_long_source_component_generates_bounded_unique_archive_names() -> None:
    root = Path("root")
    folder = root / ("camera-" + "x" * 400)
    snapshots = [
        FileSnapshot(folder / "one.jpg", 1, 1),
        FileSnapshot(folder / "two.jpg", 1, 1),
    ]

    groups = _group_snapshots(root, folder, snapshots)

    assert len(groups) == 1
    assert len(groups[0].archive_name) <= 255
    assert len(groups[0].inventory_name) <= 255
    assert groups[0].archive_name.endswith(".tar")


def test_astral_source_component_respects_windows_utf16_component_limit() -> None:
    root = Path("root")
    folder = root / ("camera-" + "\U0001f4f7" * 140)
    snapshots = [
        FileSnapshot(folder / "one.jpg", 1, 1),
        FileSnapshot(folder / "two.jpg", 1, 1),
    ]

    group = _group_snapshots(root, folder, snapshots)[0]

    assert len(group.archive_name.encode("utf-16-le")) // 2 <= 255
    assert len(group.inventory_name.encode("utf-16-le")) // 2 <= 255


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows extended-path regression")
def test_package_writes_inventory_beyond_windows_max_path(tmp_path: Path) -> None:
    root = tmp_path / "survey"
    long_folder = root / ("D" * 60)
    expected = [f"{index:04d}-{'x' * 24}.jpg" for index in range(950)]
    for name in expected:
        _write(long_folder / name, b"x")
    plan = analyze_upload_tree(root, min_same_type=1)

    package = build_upload_package(plan)
    try:
        inventory = package.payload_dir / long_folder.name / f"{long_folder.name}.tar.inventory.txt"
        archive = package.payload_dir / long_folder.name / f"{long_folder.name}.tar"
        assert len(str(inventory)) > 260
        assert len(str(archive)) > 260
        with open(_windows_io_path(inventory), encoding="utf-8") as inventory_file:
            contents = inventory_file.read()
        assert expected[0] in contents
        assert expected[-1] in contents
        with tarfile.open(_windows_io_path(archive)) as tar:
            assert tar.getnames() == expected
    finally:
        cleanup_error = package.cleanup()
    assert cleanup_error is None


def test_small_archive_keeps_single_unnumbered_name(tmp_path: Path) -> None:
    root = tmp_path / "survey"
    _write(root / "a.jpg", b"12345")
    _write(root / "b.jpg", b"12345")

    plan = analyze_upload_tree(root, min_same_type=1, max_part_bytes=10)

    assert [(group.archive_name, group.part_number, group.part_count) for group in plan.groups] == [
        ("survey.tar", 1, 1),
    ]


def test_large_archive_splits_into_parts_with_their_own_inventories(tmp_path: Path) -> None:
    root = tmp_path / "survey"
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        _write(root / name, b"x" * 6)
    _write(root / "a.xmp", b"meta")
    _write(root / "notes.txt", b"loose")

    # a.jpg + a.xmp = 10 bytes; adding b.jpg (6) or pairing b+c (12) exceeds 11.
    plan = analyze_upload_tree(root, min_same_type=1, max_part_bytes=11)

    assert [(group.archive_name, group.part_number, group.part_count, group.member_count) for group in plan.groups] == [
        ("survey.part01.tar", 1, 3, 2),
        ("survey.part02.tar", 2, 3, 1),
        ("survey.part03.tar", 3, 3, 1),
    ]
    package = build_upload_package(plan)
    try:
        expected = {
            "survey.part01.tar": ["a.jpg", "a.xmp"],
            "survey.part02.tar": ["b.jpg"],
            "survey.part03.tar": ["c.jpg"],
        }
        for archive_name, names in expected.items():
            with tarfile.open(package.payload_dir / archive_name) as archive:
                assert archive.getnames() == names
            inventory = (package.payload_dir / f"{archive_name}.inventory.txt").read_text(encoding="utf-8")
            assert f"# archive: {archive_name}" in inventory
            assert "# part: " in inventory and " of 3" in inventory
            assert all(name in inventory for name in names)
        assert package.archive_count == package.inventory_count == 3
        exclusions = package.exclude_file.read_text(encoding="utf-8").splitlines()
        assert sorted(exclusions) == ["/a.jpg", "/a.xmp", "/b.jpg", "/c.jpg"]
    finally:
        package.cleanup()


def test_parts_keep_sidecars_with_media_and_oversized_units_alone() -> None:
    folder = Path("root") / "cam"
    snapshots = [
        FileSnapshot(folder / "big.mp4", 100, 1),
        FileSnapshot(folder / "big.mp4.json", 1, 1),
        FileSnapshot(folder / "small.mp4", 2, 1),
        FileSnapshot(folder / "small.srt", 1, 1),
    ]

    groups = _group_snapshots(Path("root"), folder, snapshots, max_part_bytes=10, min_same_type=1)

    assert [(group.archive_name, group.member_count, group.member_bytes, group.media_count) for group in groups] == [
        ("cam.part01.tar", 2, 101, 1),
        ("cam.part02.tar", 2, 3, 1),
    ]


def test_revalidation_rejects_change_that_moves_part_boundaries(tmp_path: Path) -> None:
    root = tmp_path / "survey"
    _write(root / "a.jpg", b"x" * 6)
    _write(root / "b.jpg", b"x" * 6)
    plan = analyze_upload_tree(root, min_same_type=1, max_part_bytes=12)
    assert len(plan.groups) == 1
    _write(root / "c.jpg", b"x" * 6)

    with pytest.raises(RuntimeError, match="changed after it was scanned"):
        revalidate_upload_plan(plan)


def test_several_qualifying_types_share_one_folder_archive() -> None:
    folder = Path("root") / "cam"
    snapshots = [FileSnapshot(folder / f"img{index}.png", 1, 1) for index in range(3)] + [
        FileSnapshot(folder / f"row{index}.csv", 1, 1) for index in range(3)
    ] + [FileSnapshot(folder / "readme.txt", 1, 1)]

    groups = _group_snapshots(Path("root"), folder, snapshots, min_same_type=2)

    assert [(group.archive_name, group.category, group.member_count) for group in groups] == [
        ("cam.tar", "csv, png", 6),
    ]


def test_shapefile_sets_stay_together_and_alias_extensions_count_as_one_type() -> None:
    folder = Path("root") / "shapes"
    snapshots = []
    for index in range(2):
        for suffix in (".shp", ".shx", ".dbf", ".prj", ".shp.xml"):
            snapshots.append(FileSnapshot(folder / f"site{index}{suffix}", 1, 1))
    snapshots += [FileSnapshot(folder / "a.jpg", 1, 1), FileSnapshot(folder / "b.jpeg", 1, 1)]
    snapshots.append(FileSnapshot(folder / "c.JPG", 1, 1))

    parts = _archive_parts(snapshots, min_same_type=2)

    names = [item.path.name for item in parts[0][1]]
    assert names == ["a.jpg", "b.jpeg", "c.JPG"]
    shp_parts = _archive_parts(snapshots, min_same_type=1)
    assert {item.path.name for item in shp_parts[0][1]} >= {
        "site0.shp", "site0.shx", "site0.dbf", "site0.prj", "site0.shp.xml",
    }


def test_folder_below_same_type_threshold_is_not_archived(tmp_path: Path) -> None:
    root = tmp_path / "tiles"
    for index in range(50):
        _write(root / f"tile{index:02d}.png", b"x")
    for index in range(40):
        _write(root / f"tile{index:02d}.tif", b"x")

    assert not analyze_upload_tree(root).groups
    _write(root / "tile50.png", b"x")
    assert [group.media_count for group in analyze_upload_tree(root).groups] == [51]
