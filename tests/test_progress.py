from __future__ import annotations

from app.core.progress import parse_rsync_progress


def test_parse_rsync_progress_line() -> None:
    progress = parse_rsync_progress("462.36K  34%  350.61kB/s    0:00:01 (xfr#27, to-chk=0/389)")

    assert progress is not None
    assert progress.percent == 34
    assert progress.transferred == "462.36K"
    assert progress.speed == "350.61kB/s"
    assert progress.eta == "0:00:01"


def test_parse_rsync_progress_uses_latest_match() -> None:
    progress = parse_rsync_progress("1.00M  10%  1.00MB/s 0:00:09\r2.00M  20%  1.00MB/s 0:00:08")

    assert progress is not None
    assert progress.percent == 20


def test_parse_rsync_progress_keeps_rsync_byte_percent_over_to_chk() -> None:
    progress = parse_rsync_progress("462.36K  1%  417.16kB/s 0:00:01 (xfr#27, to-chk=200/400)")

    assert progress is not None
    assert progress.percent == 1


def test_parse_rsync_progress_accepts_zero_without_unit() -> None:
    progress = parse_rsync_progress("0  0%  0.00kB/s 0:00:00")

    assert progress is not None
    assert progress.percent == 0
