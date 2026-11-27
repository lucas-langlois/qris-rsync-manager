"""Windows Server deduplicated files are data; file-server system folders are not.

Seen live on L:\\RES_MMP Secure (27 Nov 2026): about three quarters of the
files are deduplicated (reparse tag 0x80000013). Older versions skipped them
as if they were links and reported them as "could not be scanned".
"""

from __future__ import annotations

import stat
from types import SimpleNamespace

from app.core.archive_upload import analyze_upload_tree
from app.core.file_scan import scan_folder
from app.core.paths import is_link_or_junction, is_windows_system_folder
from app.core.profiles import Profile
from app.core.rsync_command import build_rsync_command
from app.core.sync_compare import scan_local_manifest

REGULAR = stat.S_IFREG | 0o644
DIRECTORY = stat.S_IFDIR | 0o755


def _info(mode: int, attributes: int = 0x20, tag: int = 0) -> SimpleNamespace:
    return SimpleNamespace(st_mode=mode, st_file_attributes=attributes, st_reparse_tag=tag)


def test_deduplicated_and_cloud_files_are_ordinary_files() -> None:
    # Values as reported by os.scandir (attribute and tag) and os.stat (neither).
    assert not is_link_or_junction(_info(REGULAR, 0x620, 0x80000013))
    assert not is_link_or_junction(_info(REGULAR, 0x20, 0))
    assert not is_link_or_junction(_info(REGULAR, 0x420, 0x9000001A))  # OneDrive placeholder


def test_symlinks_junctions_and_unknown_reparse_points_stay_links() -> None:
    assert is_link_or_junction(_info(DIRECTORY, 0x416, 0xA0000003))  # junction (DfsrPrivate)
    assert is_link_or_junction(_info(REGULAR, 0x420, 0xA000000C))  # symlink
    assert is_link_or_junction(_info(REGULAR, 0x420, 0))  # attribute without a tag
    assert is_link_or_junction(_info(stat.S_IFLNK | 0o777, 0, 0))


def test_system_folder_names() -> None:
    assert is_windows_system_folder("DfsrPrivate") and is_windows_system_folder("dfsrprivate")
    assert is_windows_system_folder("System Volume Information")
    assert is_windows_system_folder("$RECYCLE.BIN")
    assert not is_windows_system_folder("DfsrPrivate data")


def _share(tmp_path):
    root = tmp_path / "share"
    (root / "survey").mkdir(parents=True)
    (root / "survey" / "a.csv").write_text("1", encoding="utf-8")
    for name in ("DfsrPrivate", "System Volume Information"):
        (root / name).mkdir()
        (root / name / "hidden.dat").write_text("x", encoding="utf-8")
    return root


def test_scanners_leave_out_system_folders(tmp_path) -> None:
    root = _share(tmp_path)

    assert scan_folder(root).file_count == 1
    plan = analyze_upload_tree(root)
    assert plan.scan.file_count == 1 and plan.scan.skipped_errors == 0
    assert list(scan_local_manifest(root)) == ["survey/a.csv"]


def test_uploads_exclude_system_folders_and_downloads_do_not(tmp_path) -> None:
    profile = Profile(username="u", host="ssh1.qriscloud.org.au", collection_id="Q1")
    upload = build_rsync_command(profile, tmp_path, remote_path="/data/Q1", direction="upload")
    download = build_rsync_command(profile, tmp_path, remote_path="/data/Q1", direction="download")

    for name in ("DfsrPrivate", "System Volume Information", "$RECYCLE.BIN"):
        assert f"--exclude={name}/" in upload
        assert f"--exclude={name}/" not in download
