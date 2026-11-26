"""Check whether QRIScloud files are on disk or on Medici tape, before downloading."""

from __future__ import annotations

import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.core.paths import app_data_dir
from app.core.askpass import (
    build_askpass_environment,
    build_interactive_askpass_environment,
    scrub_askpass_environment,
)
from app.core.bunya import BUNYA_HOST, BunyaSettings, build_bunya_submit_command, parse_job_id
from app.core.medici import (
    TapeStatus,
    build_tape_status_command,
    parse_tape_line,
    write_bunya_recall_job,
)
from app.core.profiles import Profile, fallback_hosts, profile_with_host
from app.core.remote_dirs import format_bytes

# ssh exits 255 when it cannot connect or log in; try the other host then.
SSH_CONNECT_FAILURE = 255
MAX_LISTED_FILES = 5000


class TapeStatusWorker(QObject):
    """Read-only check: lists file sizes and disk blocks, never reads file data."""

    output = Signal(str)
    finished = Signal(int)

    def __init__(self, profile: Profile, remote_paths: list[str], ssh_path: str, passphrase: str = "") -> None:
        super().__init__()
        self.profile = profile
        self.remote_paths = list(remote_paths)
        self.ssh_path = ssh_path
        self.passphrase = passphrase
        self.result: TapeStatus | None = None
        self._cancelled = threading.Event()
        self._process: subprocess.Popen[str] | None = None

    @Slot()
    def run(self) -> None:
        code = 1
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0
        for host in fallback_hosts(self.profile):
            if self._cancelled.is_set():
                break
            self.output.emit(f"Checking tape status on {host}...\n")
            try:
                command = build_tape_status_command(
                    profile_with_host(self.profile, host),
                    self.remote_paths,
                    ssh_path=self.ssh_path,
                    batch_mode=not bool(self.passphrase),
                )
            except ValueError as exc:
                self.output.emit(f"{exc}\n")
                self.finished.emit(1)
                return
            env = build_askpass_environment(self.passphrase, ssh_path=self.ssh_path)
            status = TapeStatus()
            try:
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    shell=False,
                    creationflags=creationflags,
                    env=env,
                )
                self._process = process
                if self._cancelled.is_set():
                    process.terminate()
                for line in process.stdout:
                    if self._cancelled.is_set():
                        break
                    item = parse_tape_line(line)
                    if item is not None:
                        status.add(item)
                        if status.file_count % 5000 == 0:
                            self.output.emit(f"Checked {status.file_count:,} files...\n")
                    elif line.strip() and not line.strip().startswith("**") and len(status.problems) < 20:
                        status.problems.append(line.strip())
                code = process.wait()
            except OSError as exc:
                self.output.emit(f"Could not start ssh: {exc}\n")
                code = 1
            finally:
                self._process = None
                scrub_askpass_environment(env)
            if self._cancelled.is_set():
                self.output.emit("Tape status check cancelled.\n")
                self.finished.emit(130)
                return
            if code == SSH_CONNECT_FAILURE and status.file_count == 0:
                for problem in status.problems:
                    self.output.emit(problem + "\n")
                continue
            # find exits 1 when some folders could not be read; what it did
            # list is still accurate, so report it with the problems.
            if code in (0, 1) or status.file_count:
                self.result = status
                self.output.emit(
                    f"Checked {status.file_count:,} files: {status.on_disk_count:,} on disk, "
                    f"{len(status.needs_recall):,} need recall from tape.\n"
                )
                self.finished.emit(0)
                return
            for problem in status.problems:
                self.output.emit(problem + "\n")
            break
        self.finished.emit(130 if self._cancelled.is_set() else (code or 1))

    @Slot()
    def cancel(self) -> None:
        self._cancelled.set()
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()


class BunyaSubmitWorker(QObject):
    """Upload a recall job's two files to Bunya and run sbatch, over one SSH login.

    The login prompts (key passphrase, MFA) open in small app windows through
    the interactive askpass helper.
    """

    output = Signal(str)
    finished = Signal(int)
    TIMEOUT_SECONDS = 300

    def __init__(self, settings: BunyaSettings, script: Path, file_list: Path, ssh_path: str) -> None:
        super().__init__()
        self.settings = settings
        self.script = script
        self.file_list = file_list
        self.ssh_path = ssh_path
        self.job_id: str | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._cancelled = threading.Event()

    @Slot()
    def run(self) -> None:
        try:
            script_bytes = self.script.read_bytes()
            list_bytes = self.file_list.read_bytes()
            command = build_bunya_submit_command(
                self.settings, self.script.name, self.file_list.name, len(script_bytes), ssh_path=self.ssh_path
            )
        except (OSError, ValueError) as exc:
            self.output.emit(f"Could not prepare the Bunya submission: {exc}\n")
            self.finished.emit(1)
            return
        self.output.emit(
            f"Connecting to {self.settings.username}@{BUNYA_HOST}. "
            "Approve the Bunya login (key passphrase and MFA) in the window that opens...\n"
        )
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                shell=False,
                creationflags=creationflags,
                env=build_interactive_askpass_environment(self.ssh_path),
            )
        except OSError as exc:
            self.output.emit(f"Could not start ssh: {exc}\n")
            self.finished.emit(1)
            return
        self._process = process
        try:
            raw, _ = process.communicate(script_bytes + list_bytes, timeout=self.TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            raw, _ = process.communicate()
            self.output.emit("Bunya did not respond within 5 minutes (was the MFA request approved?).\n")
        finally:
            self._process = None
        text = raw.decode("utf-8", errors="replace")
        lines = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("**")]
        if lines:
            self.output.emit("\n".join(lines) + "\n")
        if self._cancelled.is_set():
            self.output.emit("Bunya submission cancelled.\n")
            self.finished.emit(130)
            return
        self.job_id = parse_job_id(text)
        if self.job_id:
            self.output.emit(f"Submitted Bunya job {self.job_id} in {self.settings.folder}.\n")
            self.finished.emit(0)
        else:
            self.finished.emit(process.returncode or 1)

    @Slot()
    def cancel(self) -> None:
        self._cancelled.set()
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()


class TapeStatusDialog(QDialog):
    """Show what is on disk and what needs recalling.

    Result: Accepted means "recall now from this app"; BUNYA_SUBMIT means
    submit ``created_job`` to Bunya with ``submit_settings``; BUNYA_JOB_CREATED
    means job files were saved for the user to submit, so nothing more to do.
    """

    BUNYA_JOB_CREATED = 2
    BUNYA_SUBMIT = 3

    def __init__(
        self, status: TapeStatus, checked_paths: list[str], parent=None, *, default_username: str = ""
    ) -> None:
        super().__init__(parent)
        self.status = status
        self.default_username = default_username
        self.created_job: tuple[Path, Path] | None = None
        self.submit_settings: BunyaSettings | None = None
        self.setWindowTitle("Tape status")
        self.resize(860, 600)
        layout = QVBoxLayout(self)

        where = checked_paths[0] if len(checked_paths) == 1 else f"{len(checked_paths):,} selected items"
        needs = status.needs_recall
        headline = QLabel()
        headline.setWordWrap(True)
        if not status.file_count:
            headline.setText(f"<b>No files found</b> in {where}.")
        elif not needs:
            headline.setText(
                f"<b>✅ All {status.file_count:,} files are on disk</b> in {where} "
                f"({format_bytes(status.on_disk_bytes)}). They can be downloaded straight away."
            )
        else:
            headline.setText(
                f"<b>🧊 {len(needs):,} of {status.file_count:,} files are on tape</b> in {where} "
                f"({format_bytes(status.recall_bytes)}). Downloading them will wait for a tape recall, "
                "which can take minutes to hours. Recall them first, then download."
            )
        layout.addWidget(headline)

        counts = QLabel(
            f"On disk: {status.on_disk_count:,} files, {format_bytes(status.on_disk_bytes)}"
            f"   ·   On tape: {len(status.on_tape):,} files, {format_bytes(status.recall_bytes)}"
        )
        counts.setStyleSheet("color: #5b6670;")
        layout.addWidget(counts)

        if needs:
            self.table = QTableWidget(min(len(needs), MAX_LISTED_FILES), 2)
            self.table.setHorizontalHeaderLabels(["File on tape", "Size"])
            self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
            self.table.verticalHeader().setVisible(False)
            self.table.setEditTriggers(QTableWidget.NoEditTriggers)
            for row, item in enumerate(needs[:MAX_LISTED_FILES]):
                size = QTableWidgetItem(format_bytes(item.size))
                size.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(row, 0, QTableWidgetItem(item.path))
                self.table.setItem(row, 1, size)
            layout.addWidget(self.table, stretch=1)
            if len(needs) > MAX_LISTED_FILES:
                layout.addWidget(QLabel(f"Showing the first {MAX_LISTED_FILES:,} files."))
        else:
            layout.addStretch()

        if status.problems:
            problems = QLabel("Some items could not be checked:\n" + "\n".join(status.problems[:5]))
            problems.setWordWrap(True)
            problems.setStyleSheet("color: #9a5b00;")
            layout.addWidget(problems)

        note = QLabel(
            "How this is worked out: QRIScloud keeps recent files on disk and moves older ones to tape. "
            "A file on tape still shows its full size but leaves only a tiny stub on disk. "
            "Files of 4 KB or less cannot be told apart and are counted as on disk. "
            "Checking reads no file data, so it never starts a recall."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #5b6670;")
        layout.addWidget(note)

        if needs:
            layout.addWidget(self._recall_options())

        buttons = QHBoxLayout()
        buttons.addStretch()
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        buttons.addWidget(close)
        layout.addLayout(buttons)

    # --- recall choices ------------------------------------------------------------

    def _recall_options(self) -> QGroupBox:
        needs = self.status.needs_recall
        size = format_bytes(self.status.recall_bytes)
        box = QGroupBox(f"Recall {len(needs):,} file(s), {size}")
        grid = QGridLayout(box)

        bunya_text = QLabel(
            "<b>Recommended: run it on Bunya.</b> Submits a Slurm job to UQ RCC's Bunya cluster. "
            "It keeps going after you close this app or turn off your computer. "
            "RCC asks for recalls to run on a Bunya compute node, at most 4 files at a time. "
            "You approve the Bunya login (MFA) once per submission."
        )
        bunya_text.setWordWrap(True)
        settings = BunyaSettings.load(self.default_username)
        self.account_edit = QLineEdit(settings.account)
        self.account_edit.setToolTip("Your Bunya Slurm account (AccountString). JCU users: a_jcu_hpc.")
        self.username_edit = QLineEdit(settings.username)
        self.username_edit.setToolTip("Your Bunya username (usually your UQ username).")
        self.key_edit = QLineEdit(settings.key_path)
        self.key_edit.setToolTip("The private SSH key registered for Bunya.")
        self.folder_edit = QLineEdit(settings.folder)
        self.folder_edit.setToolTip("Where the job files and log go on Bunya. Must be in /scratch or /home.")
        self.username_edit.textEdited.connect(self._username_edited)
        form = QGridLayout()
        for row, (label, widget) in enumerate(
            (
                ("Bunya account", self.account_edit),
                ("Bunya username", self.username_edit),
                ("SSH key", self.key_edit),
                ("Folder on Bunya", self.folder_edit),
            )
        ):
            form.addWidget(QLabel(label), row // 2, (row % 2) * 2)
            form.addWidget(widget, row // 2, (row % 2) * 2 + 1)
        form.setColumnStretch(1, 1)
        form.setColumnStretch(3, 1)
        self.bunya_button = QPushButton("Submit to Bunya")
        self.bunya_button.setDefault(True)
        self.bunya_button.clicked.connect(self._submit_to_bunya)
        self.save_job_button = QPushButton("Save job files only…")
        self.save_job_button.setToolTip("Save the Slurm script and file list to submit on Bunya yourself.")
        self.save_job_button.clicked.connect(self._create_bunya_job)
        bunya_buttons = QHBoxLayout()
        bunya_buttons.addStretch()
        bunya_buttons.addWidget(self.save_job_button)
        bunya_buttons.addWidget(self.bunya_button)
        grid.addWidget(bunya_text, 0, 0)
        grid.addLayout(form, 1, 0)
        grid.addLayout(bunya_buttons, 2, 0)

        here_text = QLabel(
            "<b>Or recall now from this app.</b> Fine for a few files. The app must stay open "
            "and your computer awake until it finishes; closing the app stops it."
        )
        here_text.setWordWrap(True)
        self.recall_button = QPushButton(f"Recall now ({size})")
        self.recall_button.clicked.connect(self.accept)
        here_row = QHBoxLayout()
        here_row.addWidget(here_text, stretch=1)
        here_row.addWidget(self.recall_button)
        grid.addLayout(here_row, 3, 0)
        return box

    def _username_edited(self, username: str) -> None:
        # Keep the default scratch folder in step with the username.
        folder = self.folder_edit.text()
        if folder.startswith("/scratch/user/") and folder.endswith("/qris_recall"):
            self.folder_edit.setText(f"/scratch/user/{username.strip()}/qris_recall")

    def bunya_settings(self) -> BunyaSettings:
        return BunyaSettings(
            account=self.account_edit.text().strip(),
            username=self.username_edit.text().strip(),
            key_path=self.key_edit.text().strip(),
            folder=self.folder_edit.text().strip(),
        )

    def _submit_to_bunya(self) -> None:
        settings = self.bunya_settings()
        problems = settings.errors()
        if problems:
            QMessageBox.warning(self, "Check the Bunya settings", "\n".join(problems))
            return
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # Keep a local copy of every submitted job, next to the app's logs.
        folder = app_data_dir() / "bunya_jobs"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            script, file_list = write_bunya_recall_job(
                folder, [item.path for item in self.status.needs_recall], settings.account, stamp
            )
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Could not create the Bunya job", str(exc))
            return
        settings.save()
        self.created_job = (script, file_list)
        self.submit_settings = settings
        self.done(self.BUNYA_SUBMIT)

    def _choose_folder(self) -> str:
        start = str(Path.home() / "Documents")
        return QFileDialog.getExistingDirectory(self, "Save the Bunya recall job in", start)

    def _create_bunya_job(self) -> None:
        account = self.account_edit.text().strip()
        folder = self._choose_folder()
        if not folder:
            return
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            script, file_list = write_bunya_recall_job(
                Path(folder), [item.path for item in self.status.needs_recall], account, stamp
            )
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Could not create the Bunya job", str(exc))
            return
        self.bunya_settings().save()
        self.created_job = (script, file_list)
        self._show_bunya_instructions(script, file_list)
        self.done(self.BUNYA_JOB_CREATED)

    def _show_bunya_instructions(self, script: Path, file_list: Path) -> None:
        message = QMessageBox(self)
        message.setWindowTitle("Bunya recall job created")
        message.setText(
            f"Saved in {script.parent}:\n"
            f"    {script.name}\n"
            f"    {file_list.name}\n\n"
            "To start the recall:\n"
            "1. Copy both files to your Bunya scratch folder, for example /scratch/user/<your username>/ "
            "(WinSCP or scp). Not into /QRISdata.\n"
            "2. Log in to Bunya, go to that folder and run:\n"
            f"       sbatch {script.name}\n"
            "3. Check on it with:  squeue --me\n\n"
            "The job keeps running after you close this app. Use Tape status and recall → Check tape status… here to see "
            "how many files are still on tape."
        )
        open_button = message.addButton("Open folder", QMessageBox.ActionRole)
        message.addButton(QMessageBox.Ok)
        message.exec()
        if message.clickedButton() is open_button:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(script.parent)))
