"""Following Bunya recall jobs: job records, progress via QRIScloud, Bunya status."""

from __future__ import annotations

import io

from PySide6.QtWidgets import QApplication

from app.core.bunya import (
    BunyaJob,
    BunyaSettings,
    build_job_status_command,
    load_jobs,
    parse_job_status,
    record_job,
)
from app.core.medici import (
    build_listed_files_status_command,
    glob_escape,
    qriscloud_path_for_bunya_path,
    unescape_glob,
)
from app.core.profiles import Profile


def _job(tmp_path, job_id="33834602") -> BunyaJob:
    file_list = tmp_path / "recall_medici_1_files.txt"
    file_list.write_text("/QRISdata/Q1/a.jpg\n/QRISdata/Q1/x[[]1].jpg\n", encoding="utf-8", newline="\n")
    return BunyaJob(job_id, "2026-11-26 16:43", "uqjsmith", "/scratch/user/uqjsmith/qris_recall",
                    "recall_medici_1.slurm", str(file_list), 2)


def test_jobs_are_remembered_newest_first(tmp_path) -> None:
    record_job(_job(tmp_path, "1"))
    record_job(_job(tmp_path, "2"))
    record_job(_job(tmp_path, "1"))  # resubmitting the same ID replaces it

    assert [job.job_id for job in load_jobs()] == ["1", "2"]
    assert load_jobs()[0].log_name == "recall_medici_1_1.log"
    assert load_jobs()[0].done_name == "recall_medici_1_1_done.txt"


def test_bunya_paths_map_back_to_qriscloud_paths() -> None:
    name = "/data/Q1/site[1] *.jpg"
    assert unescape_glob(glob_escape(name)) == name
    assert qriscloud_path_for_bunya_path("/QRISdata/Q1/x[[]1].jpg") == "/data/Q1/x[1].jpg"


def test_progress_command_only_reads_file_details() -> None:
    remote = build_listed_files_status_command(Profile(username="u", host="h"))[-1]

    assert remote == r"xargs -r -d '\n' stat --printf '%b\t%s\t%n\n'"


def test_job_status_command_is_read_only(tmp_path) -> None:
    settings = BunyaSettings("a_jcu_hpc", "uqjsmith", "", "/scratch/user/uqjsmith/qris_recall")
    remote = build_job_status_command(settings, _job(tmp_path))[-1]

    assert "squeue -h -j 33834602" in remote and "sacct -X -n -j 33834602" in remote
    assert "tail -n 15 recall_medici_1_33834602.log" in remote
    for word in ("sbatch", "scancel", "rm ", "> "):
        assert word not in remote


def test_parse_job_status() -> None:
    status = parse_job_status(
        "== ATTENTION UQ Users ==\n@@STATE\nRUNNING 12:03\nRUNNING 00:12:03 0:0\n"
        "@@COUNTS\n812\n3\n@@LOG\nThu: recalling 4201 files, 4 at a time.\nThu: 815 of 4201 done\n"
    )
    assert status.state == "RUNNING 12:03"
    assert (status.recalled, status.failed) == (812, 3)
    assert status.log_tail.endswith("815 of 4201 done")

    finished = parse_job_status("@@STATE\nCOMPLETED 01:02:03 0:0\n@@COUNTS\n@@LOG\n")
    assert finished.state == "COMPLETED 01:02:03 0:0" and finished.recalled is None


class _Process:
    def __init__(self, lines: list[bytes]) -> None:
        self.stdin = io.BytesIO()
        self.stdin.close = lambda: None
        self.stdout = iter(lines)
        self.returncode = 0

    def wait(self):
        return 0

    def poll(self):
        return 0


def test_progress_worker_sends_the_list_and_counts(monkeypatch) -> None:
    from app.gui import recall_jobs

    process = _Process([b"12864\t6585014\t/data/Q1/a.jpg\n", b"1\t6852842\t/data/Q1/x[1].jpg\n"])
    monkeypatch.setattr(recall_jobs, "fallback_hosts", lambda _profile: ["ssh1"])
    monkeypatch.setattr(recall_jobs, "build_askpass_environment", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(recall_jobs, "scrub_askpass_environment", lambda _env: None)
    monkeypatch.setattr(recall_jobs.subprocess, "Popen", lambda *_args, **_kwargs: process)
    worker = recall_jobs.JobProgressWorker(
        Profile(username="u", host="ssh1"), ["/data/Q1/a.jpg", "/data/Q1/x[1].jpg"], "ssh.exe", ""
    )
    results: list[tuple] = []
    worker.finished.connect(lambda status, error: results.append((status, error)))

    worker.run()

    assert process.stdin.getvalue() == b"/data/Q1/a.jpg\n/data/Q1/x[1].jpg\n"
    status, error = results[0]
    assert error == "" and status.on_disk_count == 1 and len(status.on_tape) == 1


def test_dialog_lists_jobs_and_reports_progress(tmp_path) -> None:
    from app.core.medici import TapeFile, TapeStatus
    from app.gui.recall_jobs import RecallJobsDialog

    QApplication.instance() or QApplication([])
    record_job(_job(tmp_path))
    dialog = RecallJobsDialog(lambda: None, lambda _profile: "", "ssh.exe")
    assert dialog.table.item(0, 0).text() == "33834602"
    assert dialog.progress_button.isEnabled()

    status = TapeStatus()
    status.add(TapeFile("/data/Q1/a.jpg", 6_585_014, 12864))
    status.add(TapeFile("/data/Q1/b.jpg", 6_852_842, 1))
    dialog._progress_done(status, "")
    assert "1 of 2 files are back on disk (50%)" in dialog.result.toPlainText()

    dialog._bunya_done(parse_job_status("@@STATE\nRUNNING 5:35\n@@COUNTS\n10\n0\n@@LOG\nok\n"), "")
    assert "Slurm state: RUNNING 5:35" in dialog.result.toPlainText()
    dialog.close()
