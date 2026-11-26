"""Check tape status: classification, the read-only command, worker and dialog."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from app.core.medici import TapeFile, TapeStatus, build_tape_status_command, parse_tape_line
from app.core.profiles import Profile


def test_stub_files_are_on_tape_and_small_files_are_never_misread() -> None:
    # Seen live on /data/Q5722: a 7.7 MB photo on tape uses 1 block.
    assert TapeFile("/data/Q1/photo.JPG", 7_749_465, 1).state == "tape"
    assert TapeFile("/data/Q1/ortho.tif", 1_900_000_000, 0).state == "tape"
    # GPFS keeps tiny files in the inode: 1 block for 2.6 KB is still on disk.
    assert TapeFile("/data/Q1/README.md", 2682, 1).state == "disk"
    assert TapeFile("/data/Q1/tiny.txt", 4096, 0).state == "disk"
    assert TapeFile("/data/Q1/log.docx", 37345, 80).state == "disk"
    assert TapeFile("/data/Q1/empty.txt", 0, 0).state == "disk"


def test_parse_tape_line() -> None:
    assert parse_tape_line("0\t5000\t/data/Q1/a b.tif\n") == TapeFile("/data/Q1/a b.tif", 5000, 0)
    assert parse_tape_line("** WARNING: connection is not using a post-quantum key exchange") is None
    assert parse_tape_line("find: '/data/Q1/x': Permission denied") is None


def test_status_totals() -> None:
    status = TapeStatus()
    for item in (TapeFile("a", 10, 1), TapeFile("b", 5000, 0), TapeFile("c", 50_000_000, 10)):
        status.add(item)

    assert (status.on_disk_count, len(status.on_tape)) == (1, 2)
    assert status.recall_bytes == 50_005_000 and status.file_count == 3


def test_tape_status_command_is_read_only_and_quoted() -> None:
    command = build_tape_status_command(Profile(username="u", host="h"), ["/data/Q1/my folder", "/data/Q1/x.tif"])
    remote = command[-1]

    assert remote.startswith("find '/data/Q1/my folder' /data/Q1/x.tif ")
    assert r"-printf '%b\t%s\t%p\n'" in remote
    assert "-name .afm" in remote and "-prune" in remote
    for word in ("rm ", "cat ", "recall", "dd ", "mv "):
        assert word not in remote
    with pytest.raises(ValueError):
        build_tape_status_command(Profile(username="u", host="h"), ["/home/u/x"])


class _Process:
    def __init__(self, lines, code=0) -> None:
        self.stdout = iter(lines)
        self.code = code

    def wait(self):
        return self.code

    def poll(self):
        return self.code


def _worker(monkeypatch, lines, code=0):
    from app.gui import tape_status

    monkeypatch.setattr(tape_status, "fallback_hosts", lambda _profile: ["ssh1"])
    monkeypatch.setattr(tape_status, "build_askpass_environment", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(tape_status, "scrub_askpass_environment", lambda _env: None)
    monkeypatch.setattr(tape_status.subprocess, "Popen", lambda *_args, **_kwargs: _Process(lines, code))
    worker = tape_status.TapeStatusWorker(Profile(username="u", host="ssh1"), ["/data/Q1/survey"], "ssh.exe")
    codes: list[int] = []
    worker.finished.connect(codes.append)
    return worker, codes


def test_worker_classifies_files_and_keeps_problems(monkeypatch) -> None:
    worker, codes = _worker(
        monkeypatch,
        [
            "** WARNING: connection is not using a post-quantum key exchange algorithm.\n",
            "80\t37345\t/data/Q1/survey/log.docx\n",
            "0\t9000000\t/data/Q1/survey/photo.jpg\n",
            "find: '/data/Q1/survey/locked': Permission denied\n",
        ],
        code=1,  # find: some folders unreadable, but the rest is valid
    )

    worker.run()

    assert codes == [0]
    assert [item.path for item in worker.result.on_tape] == ["/data/Q1/survey/photo.jpg"]
    assert worker.result.on_disk_count == 1
    assert worker.result.problems == ["find: '/data/Q1/survey/locked': Permission denied"]


def test_worker_reports_failure_when_ssh_cannot_connect(monkeypatch) -> None:
    worker, codes = _worker(monkeypatch, ["Permission denied (publickey).\n"], code=255)

    worker.run()

    assert worker.result is None and codes == [255]


def test_dialog_offers_recall_only_when_needed() -> None:
    from app.gui.tape_status import TapeStatusDialog

    QApplication.instance() or QApplication([])
    on_disk = TapeStatus()
    on_disk.add(TapeFile("/data/Q1/a.txt", 10, 1))
    dialog = TapeStatusDialog(on_disk, ["/data/Q1"])
    assert not hasattr(dialog, "recall_button")
    dialog.close()

    mixed = TapeStatus()
    mixed.add(TapeFile("/data/Q1/a.txt", 10, 1))
    mixed.add(TapeFile("/data/Q1/b.tif", 9_000_000, 0))
    dialog = TapeStatusDialog(mixed, ["/data/Q1"])
    assert dialog.recall_button.text().startswith("Recall now (")
    assert dialog.bunya_button.text() == "Submit to Bunya"
    assert dialog.table.item(0, 0).text() == "/data/Q1/b.tif"
    dialog.close()


# --- Bunya recall job ------------------------------------------------------------


def test_glob_escape_makes_recall_medici_match_only_that_file() -> None:
    import fnmatch

    from app.core.medici import glob_escape

    name = "/data/Q1/site[1] *best?.jpg"
    assert fnmatch.fnmatchcase(name, glob_escape(name))
    assert not fnmatch.fnmatchcase("/data/Q1/site1 xbestx.jpg", glob_escape(name))


def test_bunya_job_follows_rcc_guidance(tmp_path) -> None:
    from app.core.medici import write_bunya_recall_job

    script, file_list = write_bunya_recall_job(
        tmp_path, ["/data/Q5722/sGBR_2022/a b.JPG", "/QRISdata/Q5722/x.tif"], "a_jcu_hpc", "20261126_150000"
    )

    text = script.read_text(encoding="utf-8")
    assert b"\r" not in script.read_bytes() and b"\r" not in file_list.read_bytes()
    assert text.startswith("#!/bin/bash --login\n")
    for line in (
        "#SBATCH --account=a_jcu_hpc",
        "#SBATCH --partition=general",
        "#SBATCH --cpus-per-task=4",
        "LIST=recall_medici_20261126_150000_files.txt",
    ):
        assert line in text
    # At most 4 recalls at a time, never from inside /QRISdata.
    assert "-P 4 bash -c 'recall_one" in text and "/usr/local/bin/recall_medici \"$1\"" in text
    assert "/QRISdata*)" in text
    # Progress every 5 minutes and a per-file OK/FAILED record.
    assert "sleep 300" in text and 'DONE="recall_medici_20261126_150000_${SLURM_JOB_ID:-local}_done.txt"' in text
    # /data paths become /QRISdata paths on Bunya.
    assert file_list.read_text(encoding="utf-8") == "/QRISdata/Q5722/sGBR_2022/a b.JPG\n/QRISdata/Q5722/x.tif\n"
    assert script.name == "recall_medici_20261126_150000.slurm"


@pytest.mark.parametrize("account", ["", "a_jcu_hpc; rm -rf ~", "a jcu"])
def test_bunya_job_rejects_bad_accounts(tmp_path, account) -> None:
    from app.core.medici import write_bunya_recall_job

    with pytest.raises(ValueError):
        write_bunya_recall_job(tmp_path, ["/data/Q1/a.tif"], account, "x")
    assert list(tmp_path.iterdir()) == []


def test_dialog_creates_bunya_job_and_remembers_account(monkeypatch, tmp_path) -> None:
    from app.core.app_settings import get_setting
    from app.gui.tape_status import TapeStatusDialog

    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    QApplication.instance() or QApplication([])
    status = TapeStatus()
    status.add(TapeFile("/data/Q1/b.tif", 9_000_000, 0))
    dialog = TapeStatusDialog(status, ["/data/Q1"])
    assert dialog.account_edit.text() == "a_jcu_hpc"
    out = tmp_path / "jobs"
    out.mkdir()
    shown: list[object] = []
    monkeypatch.setattr(dialog, "_choose_folder", lambda: str(out))
    monkeypatch.setattr(dialog, "_show_bunya_instructions", lambda *paths: shown.append(paths))
    dialog.account_edit.setText("a_other_group")

    dialog.save_job_button.click()

    assert dialog.result() == TapeStatusDialog.BUNYA_JOB_CREATED
    script, file_list = dialog.created_job
    assert "#SBATCH --account=a_other_group" in script.read_text(encoding="utf-8")
    assert file_list.read_text(encoding="utf-8") == "/QRISdata/Q1/b.tif\n"
    assert shown and get_setting("bunya_account") == "a_other_group"
