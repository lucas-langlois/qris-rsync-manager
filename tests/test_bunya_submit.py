"""Submitting a recall job to Bunya: settings, the ssh command, worker and dialog."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from app.core.bunya import BunyaSettings, build_bunya_submit_command, parse_job_id
from app.core.medici import TapeFile, TapeStatus, write_bunya_recall_job


def _settings(tmp_path: Path, **changes) -> BunyaSettings:
    key = tmp_path / "bunya_ed25519"
    key.write_text("not a real key", encoding="utf-8")
    values = dict(account="a_jcu_hpc", username="uqjsmith", key_path=str(key), folder="/scratch/user/uqjsmith/qris_recall")
    values.update(changes)
    return BunyaSettings(**values)


def test_defaults_come_from_the_profile_username(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    settings = BunyaSettings.load("uqjsmith")

    assert settings.account == "a_jcu_hpc"
    assert settings.folder == "/scratch/user/uqjsmith/qris_recall"
    assert settings.key_path == ""  # no ~/.ssh/bunya_ed25519 here

    settings.username = "other"
    settings.save()
    assert BunyaSettings.load("uqjsmith").username == "other"


@pytest.mark.parametrize(
    "changes",
    [
        {"username": ""},
        {"username": "me; rm -rf ~"},
        {"folder": "/QRISdata/Q1/jobs"},
        {"folder": "/scratch/user/u/../../etc"},
        {"key_path": "C:/no/such/key"},
    ],
)
def test_bad_settings_are_rejected(tmp_path, changes) -> None:
    assert _settings(tmp_path, **changes).errors()


def test_submit_command_uploads_two_files_and_runs_sbatch(tmp_path) -> None:
    settings = _settings(tmp_path, folder="/scratch/user/uqjsmith/my jobs")

    command = build_bunya_submit_command(settings, "recall_medici_1.slurm", "recall_medici_1_files.txt", 812)

    assert "uqjsmith@bunya.rcc.uq.edu.au" in command
    assert "NumberOfPasswordPrompts=1" in command  # one MFA attempt per click
    assert "BatchMode=yes" not in command
    remote = command[-1]
    assert remote == (
        "mkdir -p '/scratch/user/uqjsmith/my jobs' && cd '/scratch/user/uqjsmith/my jobs' && "
        "head -c 812 > recall_medici_1.slurm && cat > recall_medici_1_files.txt && sbatch recall_medici_1.slurm"
    )
    with pytest.raises(ValueError):
        build_bunya_submit_command(settings, "x;reboot", "list.txt", 1)


def test_parse_job_id() -> None:
    assert parse_job_id("Submitted batch job 12345678\n") == "12345678"
    assert parse_job_id("sbatch: error: Invalid account") is None


class _Process:
    def __init__(self, output: bytes, code: int) -> None:
        self.output = output
        self.returncode = code
        self.stdin_data = b""

    def communicate(self, data=None, timeout=None):
        self.stdin_data = data or b""
        return self.output, None

    def poll(self):
        return self.returncode


def _worker(monkeypatch, tmp_path, output: bytes, code: int):
    from app.gui import tape_status

    process = _Process(output, code)
    monkeypatch.setattr(tape_status, "build_interactive_askpass_environment", lambda *_args: {})
    monkeypatch.setattr(tape_status.subprocess, "Popen", lambda *_args, **_kwargs: process)
    script, file_list = write_bunya_recall_job(tmp_path, ["/data/Q1/a.tif"], "a_jcu_hpc", "1")
    worker = tape_status.BunyaSubmitWorker(_settings(tmp_path), script, file_list, "ssh.exe")
    codes: list[int] = []
    worker.finished.connect(codes.append)
    return worker, process, script, file_list, codes


def test_worker_sends_script_then_list_and_reads_the_job_id(monkeypatch, tmp_path) -> None:
    worker, process, script, file_list, codes = _worker(
        monkeypatch, tmp_path, b"** WARNING: post-quantum\nSubmitted batch job 4242\n", 0
    )

    worker.run()

    assert codes == [0] and worker.job_id == "4242"
    assert process.stdin_data == script.read_bytes() + file_list.read_bytes()


def test_worker_reports_a_failed_login(monkeypatch, tmp_path) -> None:
    worker, _process, _script, _list, codes = _worker(
        monkeypatch, tmp_path, b"uqjsmith@bunya.rcc.uq.edu.au: Permission denied (keyboard-interactive).\n", 255
    )

    worker.run()

    assert worker.job_id is None and codes == [255]


def test_dialog_submit_writes_job_and_returns_settings(monkeypatch, tmp_path) -> None:
    from app.gui.tape_status import TapeStatusDialog

    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    QApplication.instance() or QApplication([])
    status = TapeStatus()
    status.add(TapeFile("/data/Q1/b.tif", 9_000_000, 0))
    dialog = TapeStatusDialog(status, ["/data/Q1"], default_username="uqjsmith")
    assert dialog.folder_edit.text() == "/scratch/user/uqjsmith/qris_recall"
    dialog.username_edit.setText("uqother")
    dialog._username_edited("uqother")
    assert dialog.folder_edit.text() == "/scratch/user/uqother/qris_recall"
    key = tmp_path / "key"
    key.write_text("x", encoding="utf-8")
    dialog.key_edit.setText(str(key))

    dialog.bunya_button.click()

    assert dialog.result() == TapeStatusDialog.BUNYA_SUBMIT
    assert dialog.submit_settings.username == "uqother"
    script, file_list = dialog.created_job
    assert script.parent.name == "bunya_jobs" and file_list.read_text(encoding="utf-8") == "/QRISdata/Q1/b.tif\n"
