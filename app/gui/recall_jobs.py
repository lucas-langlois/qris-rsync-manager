"""Follow Bunya recall jobs submitted from the app.

Progress is read from QRIScloud with the profile's usual key (no MFA): Bunya
and QRIScloud share the same disk cache, so a file the job has recalled shows
as on disk there too (checked live on /data/Q5722, 26 Nov 2026). The Bunya
job state and log need a Bunya login, so they cost one MFA approval.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.core.askpass import (
    build_askpass_environment,
    build_interactive_askpass_environment,
    scrub_askpass_environment,
)
from app.core.bunya import BunyaJob, BunyaSettings, build_job_status_command, load_jobs, parse_job_status
from app.core.medici import (
    TapeStatus,
    build_listed_files_status_command,
    parse_tape_line,
    qriscloud_path_for_bunya_path,
)
from app.core.profiles import Profile, fallback_hosts, profile_with_host
from app.core.remote_dirs import format_bytes

CREATIONFLAGS = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0
SSH_CONNECT_FAILURE = 255


class JobProgressWorker(QObject):
    """Read-only: stat every file in a job's list on QRIScloud."""

    finished = Signal(object, str)

    def __init__(self, profile: Profile, paths: list[str], ssh_path: str, passphrase: str) -> None:
        super().__init__()
        self.profile = profile
        self.paths = paths
        self.ssh_path = ssh_path
        self.passphrase = passphrase
        self._process: subprocess.Popen[bytes] | None = None
        self._cancelled = threading.Event()

    @Slot()
    def run(self) -> None:
        data = "".join(f"{path}\n" for path in self.paths).encode("utf-8")
        error = "No QRIScloud SSH host was available."
        for host in fallback_hosts(self.profile):
            if self._cancelled.is_set():
                break
            command = build_listed_files_status_command(
                profile_with_host(self.profile, host), ssh_path=self.ssh_path, batch_mode=not bool(self.passphrase)
            )
            env = build_askpass_environment(self.passphrase, ssh_path=self.ssh_path)
            status = TapeStatus()
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    shell=False,
                    creationflags=CREATIONFLAGS,
                    env=env,
                )
                self._process = process

                def feed() -> None:
                    try:
                        process.stdin.write(data)
                        process.stdin.close()
                    except OSError:
                        pass

                writer = threading.Thread(target=feed, daemon=True)
                writer.start()
                for raw in process.stdout:
                    line = raw.decode("utf-8", errors="replace")
                    item = parse_tape_line(line)
                    if item is not None:
                        status.add(item)
                    elif line.strip() and not line.lstrip().startswith("**") and len(status.problems) < 20:
                        status.problems.append(line.strip())
                code = process.wait()
                writer.join(timeout=5)
            except OSError as exc:
                code, status.problems = 1, [f"Could not start ssh: {exc}"]
            finally:
                self._process = None
                scrub_askpass_environment(env)
            if self._cancelled.is_set():
                break
            if code == SSH_CONNECT_FAILURE and status.file_count == 0:
                error = "\n".join(status.problems) or error
                continue
            self.finished.emit(status, "")
            return
        self.finished.emit(None, "Cancelled." if self._cancelled.is_set() else error)

    @Slot()
    def cancel(self) -> None:
        self._cancelled.set()
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()


class BunyaJobStatusWorker(QObject):
    """Read the job's state, counts and log on Bunya (one MFA approval)."""

    finished = Signal(object, str)
    TIMEOUT_SECONDS = 300

    def __init__(self, settings: BunyaSettings, job: BunyaJob, ssh_path: str) -> None:
        super().__init__()
        self.settings = settings
        self.job = job
        self.ssh_path = ssh_path
        self._process: subprocess.Popen[bytes] | None = None

    @Slot()
    def run(self) -> None:
        try:
            command = build_job_status_command(self.settings, self.job, ssh_path=self.ssh_path)
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                shell=False,
                creationflags=CREATIONFLAGS,
                env=build_interactive_askpass_environment(self.ssh_path),
            )
        except (OSError, ValueError) as exc:
            self.finished.emit(None, str(exc))
            return
        self._process = process
        try:
            raw, _ = process.communicate(timeout=self.TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            raw, _ = process.communicate()
        finally:
            self._process = None
        text = raw.decode("utf-8", errors="replace")
        if "@@STATE" not in text:
            lines = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("**")]
            self.finished.emit(None, "\n".join(lines[-6:]) or "Could not log in to Bunya.")
            return
        self.finished.emit(parse_job_status(text), "")

    @Slot()
    def cancel(self) -> None:
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()


class RecallJobsDialog(QDialog):
    def __init__(
        self,
        profile_provider: Callable[[], Profile | None],
        passphrase_provider: Callable[[Profile], str | None],
        ssh_path: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._profile_provider = profile_provider
        self._passphrase_provider = passphrase_provider
        self.ssh_path = ssh_path
        self.jobs = load_jobs()
        self.thread: QThread | None = None
        self.worker: QObject | None = None
        self.setWindowTitle("Recall jobs on Bunya")
        self.resize(860, 520)
        layout = QVBoxLayout(self)

        intro = QLabel(
            "Tape recall jobs submitted to Bunya from this app. <b>Check progress</b> asks QRIScloud how "
            "many of the job's files are back on disk (no MFA needed). <b>Show Bunya job log</b> logs in "
            "to Bunya to read the job's state and log (one MFA approval)."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.table = QTableWidget(len(self.jobs), 4)
        self.table.setHorizontalHeaderLabels(["Job", "Submitted", "Files", "Folder on Bunya"])
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        for row, job in enumerate(self.jobs):
            for column, text in enumerate((job.job_id, job.submitted, f"{job.file_count:,}", job.folder)):
                self.table.setItem(row, column, QTableWidgetItem(text))
        if self.jobs:
            self.table.selectRow(0)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.table, stretch=1)

        self.result = QPlainTextEdit()
        self.result.setReadOnly(True)
        self.result.setPlaceholderText("Choose a job, then Check progress.")
        layout.addWidget(self.result, stretch=1)

        buttons = QHBoxLayout()
        self.progress_button = QPushButton("Check progress")
        self.progress_button.setDefault(True)
        self.progress_button.clicked.connect(self.check_progress)
        self.bunya_button = QPushButton("Show Bunya job log (MFA)")
        self.bunya_button.clicked.connect(self.show_bunya_log)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        buttons.addWidget(self.progress_button)
        buttons.addWidget(self.bunya_button)
        buttons.addStretch()
        buttons.addWidget(close)
        layout.addLayout(buttons)
        if not self.jobs:
            self.result.setPlainText("No recall jobs have been submitted to Bunya from this app yet.")
        self._update_buttons()

    # --- helpers ----------------------------------------------------------------------

    def selected_job(self) -> BunyaJob | None:
        rows = self.table.selectionModel().selectedRows()
        return self.jobs[rows[0].row()] if rows else None

    def _update_buttons(self) -> None:
        idle = self.thread is None and self.selected_job() is not None
        self.progress_button.setEnabled(idle)
        self.bunya_button.setEnabled(idle)

    def _start(self, worker: QObject, on_done: Callable[[object, str], None]) -> None:
        thread = QThread(self)
        worker.moveToThread(thread)
        worker.finished.connect(on_done)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.started.connect(worker.run)
        thread.finished.connect(self._thread_finished)
        thread.finished.connect(thread.deleteLater)
        self.thread, self.worker = thread, worker
        self._update_buttons()
        thread.start()

    @Slot()
    def _thread_finished(self) -> None:
        self.thread = None
        self.worker = None
        self._update_buttons()

    # --- actions --------------------------------------------------------------------------

    def check_progress(self) -> None:
        job = self.selected_job()
        profile = self._profile_provider()
        if job is None or profile is None:
            return
        try:
            lines = Path(job.local_list).read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            self.result.setPlainText(f"The job's file list is missing from this computer: {exc}")
            return
        passphrase = self._passphrase_provider(profile)
        if passphrase is None:
            return
        paths = [qriscloud_path_for_bunya_path(line) for line in lines if line.strip()]
        self.result.setPlainText(f"Checking {len(paths):,} files on QRIScloud...")
        self._start(JobProgressWorker(profile, paths, self.ssh_path, passphrase), self._progress_done)

    @Slot(object, str)
    def _progress_done(self, status: object, error: str) -> None:
        if status is None:
            self.result.setPlainText(f"Could not check progress: {error}")
            return
        job = self.selected_job()
        total = status.file_count
        on_disk = status.on_disk_count
        percent = (100 * on_disk // total) if total else 0
        text = [
            f"Job {job.job_id if job else ''}: {on_disk:,} of {total:,} files are back on disk ({percent}%).",
            f"Still on tape: {len(status.on_tape):,} files, {format_bytes(status.recall_bytes)}.",
            f"Checked {datetime.now():%d %b %Y %H:%M}.",
        ]
        if total and not status.on_tape:
            text.append("All files in this job are on disk and can be downloaded.")
        if status.problems:
            text.append("\nSome files could not be checked:\n" + "\n".join(status.problems[:5]))
        self.result.setPlainText("\n".join(text))

    def show_bunya_log(self) -> None:
        job = self.selected_job()
        if job is None:
            return
        settings = BunyaSettings.load(job.username)
        settings.username = job.username
        self.result.setPlainText(
            f"Logging in to Bunya as {job.username}. Approve the MFA request (Okta push or code)..."
        )
        self._start(BunyaJobStatusWorker(settings, job, self.ssh_path), self._bunya_done)

    @Slot(object, str)
    def _bunya_done(self, status: object, error: str) -> None:
        if status is None:
            self.result.setPlainText(f"Could not read the job from Bunya:\n{error}")
            return
        state = status.state or "not in the queue (finished, or older than Slurm keeps)"
        text = [f"Slurm state: {state}"]
        if status.recalled is not None:
            text.append(f"Recalled so far: {status.recalled:,}   Failed: {status.failed:,}")
        if status.log_tail:
            text.append("\nEnd of the job log:\n" + status.log_tail)
        self.result.setPlainText("\n".join(text))

    def closeEvent(self, event) -> None:
        worker, thread = self.worker, self.thread
        if worker is not None and hasattr(worker, "cancel"):
            worker.cancel()
        if thread is not None:
            thread.quit()
            thread.wait(6000)
        super().closeEvent(event)
