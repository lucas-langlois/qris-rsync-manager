from __future__ import annotations

import queue
import ctypes
import codecs
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFileSystemModel,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QAbstractItemView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QInputDialog,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from app.core.askpass import build_askpass_environment, scrub_askpass_environment
from app.core.logging_utils import new_log_file
from app.core.file_scan import FolderScan, scan_folder
from app.core.local_delete import LocalDeleteResult, delete_local_paths
from app.core.medici import build_recall_medici_files_command, medici_path_for_remote_path
from app.core.paths import app_data_dir, detect_ssh, is_executable_file
from app.core.profiles import Profile, fallback_hosts, load_profiles, profile_with_host, save_profiles, upsert_profile
from app.core.progress import parse_rsync_progress
from app.core.remote_dirs import RemoteEntry, build_list_remote_entries_command
from app.core.remote_ops import build_remote_delete_command, build_remote_mkdir_command, build_remote_move_command
from app.core.rsync_command import build_rsync_command, validate_transfer_inputs
from app.core.rsync_runner import RsyncRunner
from app.core.ssh_test import run_ssh_test
from app.core.sync_compare import (
    ManifestScanProgress,
    RemoteManifestCollector,
    build_remote_manifest_command,
    compare_manifests,
    scan_local_manifest,
    write_files_from,
)
from app.gui.profile_dialog import ProfileDialog
from app.gui.remote_browser_dialog import RemoteListWorker


class CommandWorker(QObject):
    output = Signal(str)
    finished = Signal(int)

    def __init__(self, command: list[str], log_prefix: str, passphrase: str = "", ssh_path: str | None = None) -> None:
        super().__init__()
        self.command = command
        self.log_prefix = log_prefix
        self.passphrase = passphrase
        self.ssh_path = ssh_path
        self.runner = RsyncRunner()

    @Slot()
    def run(self) -> None:
        log_file = new_log_file(self.log_prefix)
        code = self.runner.run(self.command, log_file, self.output.emit, passphrase=self.passphrase, ssh_path=self.ssh_path)
        self.output.emit(f"\nLog saved to: {log_file}\n")
        self.finished.emit(code)

    @Slot()
    def cancel(self) -> None:
        self.runner.cancel()


class FallbackCommandWorker(CommandWorker):
    def __init__(
        self,
        profile: Profile,
        command_factory,
        log_prefix: str,
        passphrase: str = "",
        ssh_path: str | None = None,
    ) -> None:
        super().__init__([], log_prefix, passphrase=passphrase, ssh_path=ssh_path)
        self.profile = profile
        self.command_factory = command_factory
        # This must not share RsyncRunner's flag: RsyncRunner resets it when a
        # process starts, while this worker also has a potentially slow SSH
        # preflight before that process exists.
        self._cancelled = threading.Event()

    @Slot()
    def run(self) -> None:
        log_file = new_log_file(self.log_prefix)
        for host in fallback_hosts(self.profile):
            if self._cancelled.is_set():
                self.output.emit("Transfer was cancelled before rsync started.\n")
                self.finished.emit(130)
                return
            attempt_profile = profile_with_host(self.profile, host)
            self.output.emit(f"Checking {host} before transfer...\n")
            result = run_ssh_test(
                attempt_profile,
                ssh_path=self.ssh_path,
                passphrase=self.passphrase,
                timeout=30,
                cancel_event=self._cancelled,
            )
            if self._cancelled.is_set():
                self.output.emit("Transfer was cancelled before rsync started.\n")
                self.finished.emit(130)
                return
            if result.returncode != 0:
                self.output.emit(f"{host} unavailable for transfer: exit code {result.returncode}\n")
                if result.output:
                    self.output.emit(result.output)
                    if not result.output.endswith("\n"):
                        self.output.emit("\n")
                continue
            self.output.emit(f"Using {host} for transfer.\n")
            if self._cancelled.is_set():
                self.output.emit("Transfer was cancelled before rsync started.\n")
                self.finished.emit(130)
                return
            try:
                self.command = self.command_factory(attempt_profile)
            except Exception as exc:
                self.output.emit(f"Could not build command: {exc}\n")
                self.finished.emit(1)
                return
            if self._cancelled.is_set():
                self.output.emit("Transfer was cancelled before rsync started.\n")
                self.finished.emit(130)
                return
            self.output.emit("Starting rsync. Large remote folders may spend time on the file list before file progress appears.\n")
            code = self.runner.run(
                self.command,
                log_file,
                self.output.emit,
                passphrase=self.passphrase,
                ssh_path=self.ssh_path,
                cancel_event=self._cancelled,
            )
            self.output.emit(f"\nLog saved to: {log_file}\n")
            self.finished.emit(code)
            return
        self.output.emit("No QRIScloud SSH host was available for transfer.\n")
        self.finished.emit(124)

    @Slot()
    def cancel(self) -> None:
        self._cancelled.set()
        self.runner.cancel()


class FolderScanWorker(QObject):
    output = Signal(str)
    finished = Signal(int)

    def __init__(self, path: str) -> None:
        super().__init__()
        self.path = path
        self.result: FolderScan | None = None
        self._cancelled = threading.Event()
        self._last_progress = 0

    @Slot()
    def run(self) -> None:
        try:
            self.result = scan_folder(
                self.path,
                cancel_event=self._cancelled,
                progress_callback=self._report_progress,
            )
            if self.result.cancelled:
                self.output.emit("Local folder scan cancelled.\n")
                self.finished.emit(130)
                return
            self.output.emit(
                f"Local scan: {self.result.file_count:,} files, {self.result.total_bytes:,} bytes, "
                f"{self.result.tiny_file_count:,} files under 1 MB.\n"
            )
            self.finished.emit(0)
        except Exception as exc:
            self.output.emit(f"Local folder scan failed: {exc}\n")
            self.finished.emit(1)

    def _report_progress(self, result: FolderScan) -> None:
        examined = result.file_count + result.directory_count + result.skipped_errors
        if examined - self._last_progress >= 1000:
            self._last_progress = examined
            self.output.emit(f"Scanned {result.file_count:,} files in {result.directory_count:,} folders...\n")

    @Slot()
    def cancel(self) -> None:
        self._cancelled.set()


class LocalDeleteWorker(QObject):
    output = Signal(str)
    finished = Signal(int)

    def __init__(self, paths: list[Path]) -> None:
        super().__init__()
        self.paths = list(paths)
        self.result: LocalDeleteResult | None = None
        self._cancelled = threading.Event()
        self._last_progress = 0

    @Slot()
    def run(self) -> None:
        try:
            self.result = delete_local_paths(
                self.paths,
                cancel_event=self._cancelled,
                progress_callback=self._report_progress,
            )
            if self.result.cancelled:
                self.output.emit("Local deletion cancelled; some selected items may already have been deleted.\n")
                self.finished.emit(130)
                return
            if self.result.failures:
                self.output.emit("Local deletion completed with errors:\n" + "\n".join(self.result.failures) + "\n")
                self.finished.emit(1)
                return
            self.output.emit(f"Moved {self.result.recycled_items:,} selected item(s) to the Recycle Bin.\n")
            self.finished.emit(0)
        except Exception as exc:
            self.output.emit(f"Local deletion failed: {exc}\n")
            self.finished.emit(1)

    def _report_progress(self, result: LocalDeleteResult) -> None:
        processed = result.deleted_count + result.skipped_errors
        if processed - self._last_progress >= 250:
            self._last_progress = processed
            self.output.emit(f"Deleted {result.deleted_count:,} local items...\n")

    @Slot()
    def cancel(self) -> None:
        self._cancelled.set()


class RecallMediciWorker(QObject):
    output = Signal(str)
    finished = Signal(int)

    def __init__(
        self,
        profile: Profile,
        remote_files: list[str],
        ssh_path: str,
        passphrase: str = "",
        batch_size: int = 1,
        max_reconnect_attempts: int = 3,
        reconnect_delay_seconds: int = 15,
    ) -> None:
        super().__init__()
        self.profile = profile
        self.remote_files = remote_files
        self.ssh_path = ssh_path
        self.passphrase = passphrase
        self.batch_size = max(1, int(batch_size))
        self.max_reconnect_attempts = max(1, int(max_reconnect_attempts))
        self.reconnect_delay_seconds = max(0, int(reconnect_delay_seconds))
        self._process: subprocess.Popen[str] | None = None
        self._cancelled = False

    @Slot()
    def run(self) -> None:
        log_file = new_log_file(f"recall_medici_{self.profile.name}")
        try:
            code = self._run_with_fallback(log_file)
        except Exception as exc:
            self._write_and_emit(log_file, f"\nRecall failed before completion: {exc}\n")
            code = 1
        self.output.emit(f"\nLog saved to: {log_file}\n")
        self.finished.emit(code)

    def _run_with_fallback(self, log_file: Path) -> int:
        for host in fallback_hosts(self.profile):
            if self._cancelled:
                self._write_and_emit(log_file, "Recall was cancelled.\n")
                return 130
            attempt_profile = profile_with_host(self.profile, host)
            self._write_and_emit(log_file, f"Checking {host} before recall...\n")
            result = run_ssh_test(attempt_profile, ssh_path=self.ssh_path, passphrase=self.passphrase, timeout=30)
            if result.returncode != 0:
                self._write_and_emit(log_file, f"{host} unavailable for recall: exit code {result.returncode}\n")
                if result.output:
                    text = result.output if result.output.endswith("\n") else result.output + "\n"
                    self._write_and_emit(log_file, text)
                continue
            self._write_and_emit(log_file, f"Using {host} for recall.\n")
            return self._run_batches(attempt_profile, log_file)
        self._write_and_emit(log_file, "No QRIScloud SSH host was available for recall.\n")
        return 124

    def _run_batches(self, profile: Profile, log_file: Path) -> int:
        chunks = [
            self.remote_files[index : index + self.batch_size]
            for index in range(0, len(self.remote_files), self.batch_size)
        ]
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0
        for index, chunk in enumerate(chunks, start=1):
            if self._cancelled:
                self._write_and_emit(log_file, "Recall was cancelled.\n")
                return 130
            first_name = Path(chunk[0]).name
            last_name = Path(chunk[-1]).name
            if first_name == last_name:
                summary = first_name
            else:
                summary = f"{first_name} ... {last_name}"
            self._write_and_emit(log_file, f"\n[{index}/{len(chunks)}] recalling {len(chunk)} file(s): {summary}\n")
            code = self._run_batch_with_reconnects(profile, chunk, log_file, index, len(chunks), creationflags)
            if code != 0:
                self._write_and_emit(log_file, f"[{index}/{len(chunks)}] recall failed with exit code {code}.\n")
                return code
            self._write_and_emit(log_file, f"[{index}/{len(chunks)}] done.\n")
        self._write_and_emit(log_file, f"\nRecall finished for {len(self.remote_files):,} file(s).\n")
        return 0

    def _run_batch_with_reconnects(
        self,
        profile: Profile,
        chunk: list[str],
        log_file: Path,
        batch_index: int,
        batch_count: int,
        creationflags: int,
    ) -> int:
        hosts = fallback_hosts(profile)
        last_code = 1
        for attempt in range(1, self.max_reconnect_attempts + 1):
            for host in hosts:
                if self._cancelled:
                    self._write_and_emit(log_file, "Recall was cancelled.\n")
                    return 130
                attempt_profile = profile_with_host(profile, host)
                if attempt > 1 or host != profile.host:
                    self._write_and_emit(
                        log_file,
                        f"[{batch_index}/{batch_count}] reconnect attempt {attempt}/{self.max_reconnect_attempts} using {host}...\n",
                    )
                code = self._run_recall_command(attempt_profile, chunk, log_file, batch_index, batch_count, creationflags)
                if code == 0 or self._cancelled:
                    return code
                last_code = code
                if not self._is_retryable_recall_exit(code):
                    return code
                self._write_and_emit(
                    log_file,
                    f"[{batch_index}/{batch_count}] SSH connection failed with exit code {code}; will retry this file.\n",
                )
            if attempt < self.max_reconnect_attempts and self.reconnect_delay_seconds > 0:
                self._sleep_before_reconnect(log_file, batch_index, batch_count)
        return last_code

    def _run_recall_command(
        self,
        profile: Profile,
        chunk: list[str],
        log_file: Path,
        batch_index: int,
        batch_count: int,
        creationflags: int,
    ) -> int:
        command = build_recall_medici_files_command(
            profile,
            chunk,
            ssh_path=self.ssh_path,
            batch_mode=not bool(self.passphrase),
            batch_size=len(chunk),
        )
        env = build_askpass_environment(self.passphrase, ssh_path=self.ssh_path)
        try:
            with subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                shell=False,
                creationflags=creationflags,
                env=env,
            ) as process:
                self._process = process
                code = self._monitor_recall_process(process, log_file, batch_index, batch_count)
        finally:
            self._process = None
            scrub_askpass_environment(env)
        return code

    def _sleep_before_reconnect(self, log_file: Path, batch_index: int, batch_count: int) -> None:
        self._write_and_emit(
            log_file,
            f"[{batch_index}/{batch_count}] waiting {self.reconnect_delay_seconds}s before reconnect...\n",
        )
        deadline = time.monotonic() + self.reconnect_delay_seconds
        while not self._cancelled and time.monotonic() < deadline:
            time.sleep(0.2)

    @staticmethod
    def _is_retryable_recall_exit(code: int) -> bool:
        return code == 255

    def _monitor_recall_process(
        self,
        process: subprocess.Popen[str],
        log_file: Path,
        batch_index: int,
        batch_count: int,
    ) -> int:
        output_queue: queue.Queue[str | None] = queue.Queue()

        def read_output() -> None:
            try:
                if process.stdout:
                    for line in process.stdout:
                        output_queue.put(line)
            except (OSError, ValueError):
                pass
            finally:
                output_queue.put(None)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        started = time.monotonic()
        next_heartbeat = started + 30
        output_done = False

        while True:
            try:
                item = output_queue.get(timeout=0.2)
            except queue.Empty:
                item = ""

            if item is None:
                output_done = True
            elif item:
                self._write_and_emit(log_file, item)

            if self._cancelled:
                process.terminate()
                self._write_and_emit(log_file, "\nRecall was cancelled.\n")
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                return 130

            now = time.monotonic()
            if process.poll() is None and now >= next_heartbeat:
                elapsed = int(now - started)
                self._write_and_emit(
                    log_file,
                    f"[{batch_index}/{batch_count}] still recalling after {elapsed}s; waiting for QRIScloud tape/cache...\n",
                )
                next_heartbeat = now + 30

            if process.poll() is not None and output_done and output_queue.empty():
                break

        reader.join(timeout=1)
        return process.wait()

    def _write_and_emit(self, log_file: Path, text: str) -> None:
        with log_file.open("a", encoding="utf-8", errors="replace") as handle:
            handle.write(text)
        self.output.emit(text)

    @Slot()
    def cancel(self) -> None:
        self._cancelled = True
        if self._process and self._process.poll() is None:
            self._process.terminate()


class SshTestWorker(QObject):
    output = Signal(str)
    finished = Signal(int)

    def __init__(self, profile: Profile, ssh_path: str, passphrase: str = "") -> None:
        super().__init__()
        self.profile = profile
        self.ssh_path = ssh_path
        self.passphrase = passphrase
        self._cancelled = threading.Event()

    @Slot()
    def run(self) -> None:
        log_file = new_log_file(f"ssh_test_{self.profile.name}")
        self.output.emit("Testing SSH connection... This can take up to 90 seconds per host on a slow QRIScloud login.\n")
        log_parts: list[str] = []
        result = None
        for host in fallback_hosts(self.profile):
            if self._cancelled.is_set():
                self.output.emit("SSH test was cancelled.\n")
                self.finished.emit(130)
                return
            attempt_profile = profile_with_host(self.profile, host)
            self.output.emit(f"Trying {host}...\n")
            result = run_ssh_test(
                attempt_profile,
                ssh_path=self.ssh_path,
                passphrase=self.passphrase,
                cancel_event=self._cancelled,
            )
            if self._cancelled.is_set():
                self.output.emit("SSH test was cancelled.\n")
                self.finished.emit(130)
                return
            log_parts.append(f"=== {host} ===\n{result.output}\n")
            self.output.emit(result.output or "(SSH produced no output)\n")
            if result.returncode == 0:
                self.output.emit(f"SSH test succeeded using {host}.\n")
                break
            self.output.emit(f"SSH test failed on {host} with code {result.returncode}.\n")
        if result is None:
            result = run_ssh_test(self.profile, ssh_path=self.ssh_path, passphrase=self.passphrase)
        log_file.write_text("".join(log_parts), encoding="utf-8", errors="replace")
        self.output.emit(f"\nSSH test exited with code {result.returncode}\nLog saved to: {log_file}\n")
        self.finished.emit(result.returncode)

    @Slot()
    def cancel(self) -> None:
        self._cancelled.set()


class SyncCompareWorker(QObject):
    output = Signal(str)
    finished = Signal(object, str)

    REMOTE_TIMEOUT_SECONDS = 300.0
    STOP_GRACE_SECONDS = 5.0
    OUTPUT_DRAIN_SECONDS = 5.0
    READER_JOIN_SECONDS = 1.0

    def __init__(self, profile: Profile, ssh_path: str, local_folder: str, remote_path: str, passphrase: str) -> None:
        super().__init__()
        self.profile = profile
        self.ssh_path = ssh_path
        self.local_folder = local_folder
        self.remote_path = remote_path
        self.passphrase = passphrase
        self._process: subprocess.Popen[str] | None = None
        self._process_lock = threading.Lock()
        self._finish_lock = threading.Lock()
        self._cancelled = threading.Event()
        self._did_finish = False
        self._last_local_progress = 0

    @Slot()
    def run(self) -> None:
        try:
            if self._cancelled.is_set():
                self._finish(None, "Sync comparison was cancelled.")
                return
            self.output.emit("Scanning local files...\n")
            local_manifest = scan_local_manifest(
                self.local_folder,
                cancel_event=self._cancelled,
                progress_callback=self._report_local_progress,
            )
            if self._cancelled.is_set():
                self._finish(None, "Sync comparison was cancelled.")
                return
            self.output.emit(f"Local manifest: {len(local_manifest):,} files.\n")

            creationflags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0
            returncode = 1
            remote_manifest = None
            last_error = ""
            deadline = time.monotonic() + self.REMOTE_TIMEOUT_SECONDS
            for host in fallback_hosts(self.profile):
                if self._cancelled.is_set():
                    self._finish(None, last_error if "could not stop" in last_error.lower() else "Sync comparison was cancelled.")
                    return
                if time.monotonic() >= deadline:
                    self._finish(None, "Remote manifest timed out.")
                    return
                attempt_profile = profile_with_host(self.profile, host)
                self.output.emit(f"Reading remote file manifest from {host}... This can take a while for large QRIScloud folders.\n")
                command = build_remote_manifest_command(
                    attempt_profile,
                    self.remote_path,
                    ssh_path=self.ssh_path,
                    batch_mode=not bool(self.passphrase),
                )
                env = build_askpass_environment(self.passphrase, ssh_path=self.ssh_path)
                collector = RemoteManifestCollector()
                process = None
                try:
                    if self._cancelled.is_set():
                        self._finish(None, "Sync comparison was cancelled.")
                        return
                    process = subprocess.Popen(
                        command,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1,
                        shell=False,
                        creationflags=creationflags,
                        env=env,
                    )
                    with self._process_lock:
                        self._process = process
                    returncode, last_error = self._collect_remote_manifest(
                        process,
                        collector,
                        host,
                        deadline,
                    )
                finally:
                    with self._process_lock:
                        if process is not None and self._process is process:
                            self._process = None
                    scrub_askpass_environment(env)
                if self._cancelled.is_set():
                    self._finish(None, last_error if "could not stop" in last_error.lower() else "Sync comparison was cancelled.")
                    return
                if returncode == 0:
                    self.output.emit(f"Remote manifest succeeded using {host}.\n")
                    remote_manifest = collector.records
                    break
                self.output.emit(f"Remote manifest failed on {host} with exit code {returncode}.\n")
            if returncode != 0 or remote_manifest is None:
                self._finish(None, last_error or f"Remote manifest failed with exit code {returncode}.")
                return

            self.output.emit(f"Remote manifest: {len(remote_manifest):,} files.\n")
            selection = compare_manifests(local_manifest, remote_manifest)
            selected = selection.selected
            if self._cancelled.is_set():
                self._finish(None, "Sync comparison was cancelled.")
                return
            file_list = write_files_from(selected, f"sync_selection_{time.time_ns()}")
            if self._cancelled.is_set():
                try:
                    file_list.unlink()
                except OSError:
                    pass
                self._finish(None, "Sync comparison was cancelled.")
                return
            self.output.emit(
                f"Sync selection: {len(selection.missing):,} missing, {len(selection.changed):,} changed, "
                f"{len(selected):,} total selected.\n"
            )
            self.output.emit(f"Selection file list: {file_list}\n")
            self._finish_success(
                file_list,
                {
                    "missing": len(selection.missing),
                    "changed": len(selection.changed),
                    "selected": len(selected),
                    "file_list": str(file_list),
                    "local_folder": self.local_folder,
                    "remote_path": self.remote_path,
                },
            )
        except Exception as exc:
            self._finish(None, f"Sync comparison failed: {exc}")
        finally:
            with self._process_lock:
                self._process = None

    def _report_local_progress(self, progress: ManifestScanProgress) -> None:
        if progress.files_scanned - self._last_local_progress >= 1000:
            self._last_local_progress = progress.files_scanned
            self.output.emit(
                f"Local manifest: scanned {progress.files_scanned:,} files in "
                f"{progress.directories_scanned:,} folders...\n"
            )

    def _collect_remote_manifest(
        self,
        process: subprocess.Popen[str],
        collector: RemoteManifestCollector,
        host: str,
        deadline: float,
    ) -> tuple[int, str]:
        if sys.platform.startswith("win") and getattr(process, "stdout", None) is not None:
            try:
                process.stdout.fileno()
            except (AttributeError, OSError, ValueError):
                pass
            else:
                return self._collect_remote_manifest_windows(process, collector, host, deadline)

        output_queue: queue.Queue[str | None] = queue.Queue(maxsize=1000)
        reader_stop = threading.Event()

        def read_output() -> None:
            try:
                if process.stdout:
                    for line in process.stdout:
                        while not reader_stop.is_set():
                            try:
                                output_queue.put(line, timeout=0.1)
                                break
                            except queue.Full:
                                continue
            except (OSError, ValueError):
                pass
            finally:
                while not reader_stop.is_set():
                    try:
                        output_queue.put(None, timeout=0.1)
                        break
                    except queue.Full:
                        continue

        reader = threading.Thread(target=read_output, daemon=True, name="sync-manifest-reader")
        reader.start()
        output_done = False
        line_count = 0
        error_tail = ""
        drain_deadline: float | None = None
        while True:
            now = time.monotonic()
            if self._cancelled.is_set():
                stopped = self._stop_process(process)
                self._shutdown_manifest_reader(process, reader, reader_stop)
                return (130, "Sync comparison was cancelled.") if stopped else (1, "Remote SSH process could not stop after cancellation.")
            if now >= deadline:
                stopped = self._stop_process(process)
                self._shutdown_manifest_reader(process, reader, reader_stop)
                if not stopped:
                    return 1, "Remote SSH process could not stop after the manifest timeout."
                return 124, f"Remote manifest timed out after {self.REMOTE_TIMEOUT_SECONDS:g} seconds."
            try:
                item = output_queue.get(timeout=min(0.2, max(0.01, deadline - now)))
            except queue.Empty:
                item = ""
            if item is None:
                output_done = True
            elif item:
                record = collector.feed_line(item)
                line_count += 1
                if record is None and not item.startswith("** WARNING:"):
                    error_tail = (error_tail + item)[-4000:]
                if line_count % 500 == 0:
                    self.output.emit(f"Remote manifest on {host}: read {line_count:,} lines...\n")

            polled = process.poll()
            if polled is not None:
                if output_done and output_queue.empty():
                    reader.join(timeout=1.0)
                    return polled, error_tail.strip()
                if drain_deadline is None:
                    drain_deadline = min(deadline, now + self.OUTPUT_DRAIN_SECONDS)
                elif now >= drain_deadline:
                    self._shutdown_manifest_reader(process, reader, reader_stop)
                    return 1, "Remote manifest output did not close cleanly; comparison was stopped to avoid using incomplete data."

    def _collect_remote_manifest_windows(
        self,
        process: subprocess.Popen[str],
        collector: RemoteManifestCollector,
        host: str,
        deadline: float,
    ) -> tuple[int, str]:
        import msvcrt

        stdout = process.stdout
        assert stdout is not None
        descriptor = stdout.fileno()
        pipe_handle = msvcrt.get_osfhandle(descriptor)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.PeekNamedPipe.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.c_void_p,
        ]
        kernel32.PeekNamedPipe.restype = ctypes.c_int
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        text_buffer = ""
        error_tail = ""
        line_count = 0
        drain_deadline: float | None = None

        def consume(text: str, final: bool = False) -> None:
            nonlocal text_buffer, error_tail, line_count
            text_buffer += text
            lines = text_buffer.splitlines(keepends=True)
            text_buffer = ""
            for index, line in enumerate(lines):
                if not final and index == len(lines) - 1 and not line.endswith(("\n", "\r")):
                    text_buffer = line
                    continue
                record = collector.feed_line(line)
                line_count += 1
                if record is None and not line.startswith("** WARNING:"):
                    error_tail = (error_tail + line)[-4000:]
                if line_count % 500 == 0:
                    self.output.emit(f"Remote manifest on {host}: read {line_count:,} lines...\n")

        try:
            while True:
                now = time.monotonic()
                if self._cancelled.is_set():
                    stopped = self._stop_process(process)
                    return (130, "Sync comparison was cancelled.") if stopped else (1, "Remote SSH process could not stop after cancellation.")
                if now >= deadline:
                    stopped = self._stop_process(process)
                    if not stopped:
                        return 1, "Remote SSH process could not stop after the manifest timeout."
                    return 124, f"Remote manifest timed out after {self.REMOTE_TIMEOUT_SECONDS:g} seconds."

                available = ctypes.c_ulong()
                peek_ok = kernel32.PeekNamedPipe(
                    ctypes.c_void_p(pipe_handle), None, 0, None, ctypes.byref(available), None
                )
                if peek_ok and available.value:
                    chunk = os.read(descriptor, min(int(available.value), 65536))
                    if chunk:
                        consume(decoder.decode(chunk))
                        continue

                polled = process.poll()
                if not peek_ok:
                    error = ctypes.get_last_error()
                    if error in (109, 232) and polled is not None:
                        consume(decoder.decode(b"", final=True), final=True)
                        consume("", final=True)
                        return polled, error_tail.strip()
                    if polled is None:
                        stopped = self._stop_process(process)
                        if not stopped:
                            return 1, "Remote SSH process closed its output and could not be stopped."
                    return 1, f"Could not read remote manifest output (Windows error {error})."

                if polled is not None:
                    if drain_deadline is None:
                        drain_deadline = min(deadline, now + self.OUTPUT_DRAIN_SECONDS)
                    elif now >= drain_deadline:
                        return 1, "Remote manifest output did not close cleanly; comparison was stopped to avoid using incomplete data."
                self._cancelled.wait(0.02)
        finally:
            try:
                stdout.close()
            except OSError:
                pass

    def _shutdown_manifest_reader(
        self,
        process: subprocess.Popen[str],
        reader: threading.Thread,
        reader_stop: threading.Event,
    ) -> None:
        reader_stop.set()
        self._cancel_reader_io(process, reader)
        reader.join(timeout=self.READER_JOIN_SECONDS)
        stdout = getattr(process, "stdout", None)
        if reader.is_alive() and stdout is not None and not hasattr(stdout, "fileno") and hasattr(stdout, "close"):
            stdout.close()
            reader.join(timeout=self.READER_JOIN_SECONDS)
        if not reader.is_alive():
            if stdout:
                try:
                    stdout.close()
                except OSError:
                    pass

    @staticmethod
    def _cancel_reader_io(process: subprocess.Popen[str], reader: threading.Thread) -> None:
        if not sys.platform.startswith("win") or reader.native_id is None:
            return
        # CancelIoEx targets the pipe handle without acquiring TextIOWrapper's
        # lock. CancelSynchronousIo is retained as a thread-specific fallback.
        import msvcrt

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CancelIoEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.CancelIoEx.restype = ctypes.c_int
        stdout = getattr(process, "stdout", None)
        if stdout is not None:
            try:
                pipe_handle = msvcrt.get_osfhandle(stdout.fileno())
                kernel32.CancelIoEx(ctypes.c_void_p(pipe_handle), None)
            except (AttributeError, OSError, ValueError):
                pass
        kernel32.OpenThread.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel32.OpenThread.restype = ctypes.c_void_p
        kernel32.CancelSynchronousIo.argtypes = [ctypes.c_void_p]
        kernel32.CancelSynchronousIo.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        thread_handle = kernel32.OpenThread(0x0001, False, reader.native_id)
        if not thread_handle:
            return
        try:
            kernel32.CancelSynchronousIo(thread_handle)
        finally:
            kernel32.CloseHandle(thread_handle)

    def _stop_process(self, process: subprocess.Popen[str]) -> bool:
        if process.poll() is not None:
            return True
        try:
            process.terminate()
            process.wait(timeout=self.STOP_GRACE_SECONDS)
            return True
        except (OSError, subprocess.TimeoutExpired):
            if process.poll() is not None:
                return True
            pass
        try:
            process.kill()
            process.wait(timeout=self.STOP_GRACE_SECONDS)
            return process.poll() is not None
        except (OSError, subprocess.TimeoutExpired):
            return process.poll() is not None

    def _finish(self, result: object, error: str) -> None:
        with self._finish_lock:
            if self._did_finish:
                return
            self._did_finish = True
        self.finished.emit(result, error)

    def _finish_success(self, file_list: Path, result: object) -> None:
        cancelled = False
        with self._finish_lock:
            if self._did_finish:
                return
            if self._cancelled.is_set():
                cancelled = True
            self._did_finish = True
        if cancelled:
            try:
                file_list.unlink()
            except OSError:
                pass
            self.finished.emit(None, "Sync comparison was cancelled.")
        else:
            self.finished.emit(result, "")

    @Slot()
    def cancel(self) -> None:
        with self._finish_lock:
            if not self._did_finish:
                self._cancelled.set()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("QRIS Rsync Manager")
        self.resize(980, 720)

        self.profiles = load_profiles()
        self.current_thread: QThread | None = None
        self.current_worker: CommandWorker | SshTestWorker | RecallMediciWorker | FolderScanWorker | LocalDeleteWorker | None = None
        self.current_label = ""
        self.current_finished_handled = False
        self.current_refresh_local = False
        self.current_refresh_remote = False
        self.current_after_finish = None
        self.current_exit_code: int | None = None
        self._current_operation_id = 0
        self.remote_thread: QThread | None = None
        self.remote_worker: RemoteListWorker | None = None
        self._remote_request_id = 0
        self.compare_thread: QThread | None = None
        self.compare_worker: SyncCompareWorker | None = None
        self.sync_selection: dict[str, object] | None = None
        self.remote_entries: list[RemoteEntry] = []
        self.remote_entries_path = ""
        self.session_passphrase: str | None = None
        self._close_after_stop = False

        self.profile_combo = QComboBox()
        self.local_folder_edit = QLineEdit()
        self.remote_path_edit = QLineEdit()
        self.detected_ssh = detect_ssh()
        self.status_label = QLabel()
        self.transfer_progress = QProgressBar()
        self.transfer_progress.setRange(0, 100)
        self.transfer_progress.setValue(0)
        self.transfer_progress.setTextVisible(True)
        self.transfer_status_label = QLabel("No transfer running.")
        self._progress_buffer = ""
        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.document().setMaximumBlockCount(5000)

        self.local_model = QFileSystemModel(self)
        self.local_model.setRootPath("")
        self.local_tree = QTreeView()
        self.local_tree.setModel(self.local_model)
        self.local_tree.setSortingEnabled(True)
        self.local_tree.sortByColumn(0, Qt.AscendingOrder)
        self.local_tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.local_tree.doubleClicked.connect(self._local_double_clicked)

        self.remote_table = QTableWidget(0, 4)
        self.remote_table.setHorizontalHeaderLabels(["Name", "Size", "Type", "Date Modified"])
        self.remote_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.remote_table.setColumnWidth(0, 260)
        self.remote_table.setColumnWidth(1, 90)
        self.remote_table.setColumnWidth(2, 110)
        self.remote_table.setColumnWidth(3, 150)
        self.remote_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.remote_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.remote_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.remote_table.itemDoubleClicked.connect(self._remote_double_clicked)
        self.remote_status_label = QLabel("Remote browser not loaded.")

        self.ssh_button = QPushButton("Test SSH")
        self.local_up_button = QPushButton("Up")
        self.local_refresh_button = QPushButton("Refresh")
        self.local_new_folder_button = QPushButton("New folder")
        self.local_rename_button = QPushButton("Rename/move")
        self.local_delete_button = QPushButton("Delete")
        self.remote_load_button = QPushButton("Load")
        self.remote_up_button = QPushButton("Up")
        self.remote_refresh_button = QPushButton("Refresh")
        self.remote_new_folder_button = QPushButton("New folder")
        self.remote_rename_button = QPushButton("Rename/move")
        self.remote_delete_button = QPushButton("Delete")
        self.transfer_scope_combo = QComboBox()
        self.transfer_scope_combo.addItem("Current folders", "folder")
        self.transfer_scope_combo.addItem("Selected files", "selected")
        self.dry_run_button = QPushButton("Compare / dry-run")
        self.download_dry_run_button = QPushButton("Compare download")
        self.build_selection_button = QPushButton("Build sync selection")
        self.upload_selection_button = QPushButton("Upload compared files")
        self.upload_selection_button.setEnabled(False)
        self.upload_button = QPushButton("Upload folder")
        self.recall_button = QPushButton("Recall tape")
        self.download_button = QPushButton("Download folder")
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)

        self._build_ui()
        self._set_local_root(str(Path.home()))
        self._load_profile_combo()
        self._update_status()
        self._update_transfer_scope_ui()

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        self.profile_box = QGroupBox("Profile")
        profile_layout = QGridLayout(self.profile_box)
        profile_layout.addWidget(QLabel("Profile"), 0, 0)
        profile_layout.addWidget(self.profile_combo, 0, 1)
        new_button = QPushButton("New")
        edit_button = QPushButton("Edit")
        delete_button = QPushButton("Delete")
        save_button = QPushButton("Save profiles")
        profile_layout.addWidget(new_button, 0, 2)
        profile_layout.addWidget(edit_button, 0, 3)
        profile_layout.addWidget(delete_button, 0, 4)
        profile_layout.addWidget(save_button, 0, 5)
        profile_layout.addWidget(self.status_label, 1, 0, 1, 6)
        layout.addWidget(self.profile_box)

        browser = QSplitter()
        browser.addWidget(self._local_panel())
        browser.addWidget(self._remote_panel())
        browser.setSizes([480, 480])
        layout.addWidget(browser, stretch=2)

        button_row = QHBoxLayout()
        button_row.addWidget(QLabel("Transfer scope"))
        button_row.addWidget(self.transfer_scope_combo)
        button_row.addWidget(self.ssh_button)
        button_row.addWidget(self.dry_run_button)
        button_row.addWidget(self.download_dry_run_button)
        button_row.addWidget(self.build_selection_button)
        button_row.addWidget(self.upload_selection_button)
        button_row.addWidget(self.upload_button)
        button_row.addWidget(self.recall_button)
        button_row.addWidget(self.download_button)
        button_row.addStretch()
        button_row.addWidget(self.stop_button)
        layout.addLayout(button_row)

        progress_row = QHBoxLayout()
        progress_row.addWidget(QLabel("Progress"))
        progress_row.addWidget(self.transfer_progress, stretch=1)
        progress_row.addWidget(self.transfer_status_label)
        layout.addLayout(progress_row)

        layout.addWidget(QLabel("Log"))
        layout.addWidget(self.log_output, stretch=1)

        self.profile_combo.currentIndexChanged.connect(self._profile_changed)
        new_button.clicked.connect(self._new_profile)
        edit_button.clicked.connect(self._edit_profile)
        delete_button.clicked.connect(self._delete_profile)
        save_button.clicked.connect(self._save_profiles)
        self.ssh_button.clicked.connect(self._start_ssh_test)
        self.local_up_button.clicked.connect(self._local_go_up)
        self.local_refresh_button.clicked.connect(self._refresh_local_tree)
        self.local_new_folder_button.clicked.connect(self._local_new_folder)
        self.local_rename_button.clicked.connect(self._local_rename_or_move)
        self.local_delete_button.clicked.connect(self._local_delete_selected)
        self.remote_load_button.clicked.connect(self._refresh_remote_table)
        self.remote_up_button.clicked.connect(self._remote_go_up)
        self.remote_refresh_button.clicked.connect(self._refresh_remote_table)
        self.remote_new_folder_button.clicked.connect(self._remote_new_folder)
        self.remote_rename_button.clicked.connect(self._remote_rename_or_move)
        self.remote_delete_button.clicked.connect(self._remote_delete_selected)
        self.dry_run_button.clicked.connect(lambda: self._start_rsync(dry_run=True, direction="upload"))
        self.download_dry_run_button.clicked.connect(lambda: self._start_rsync(dry_run=True, direction="download"))
        self.build_selection_button.clicked.connect(self._build_sync_selection)
        self.upload_selection_button.clicked.connect(self._upload_sync_selection)
        self.upload_button.clicked.connect(lambda: self._start_rsync(dry_run=False, direction="upload"))
        self.recall_button.clicked.connect(self._recall_medici)
        self.download_button.clicked.connect(lambda: self._start_rsync(dry_run=False, direction="download"))
        self.stop_button.clicked.connect(self._cancel_current)
        self.transfer_scope_combo.currentIndexChanged.connect(self._update_transfer_scope_ui)
        self.local_tree.selectionModel().selectionChanged.connect(self._update_transfer_scope_ui)
        self.remote_table.itemSelectionChanged.connect(self._update_transfer_scope_ui)

    def _local_panel(self) -> QGroupBox:
        box = QGroupBox("Local")
        layout = QVBoxLayout(box)
        layout.addLayout(self._folder_row())
        layout.addWidget(self.local_tree, stretch=1)
        return box

    def _remote_panel(self) -> QGroupBox:
        box = QGroupBox("Remote")
        layout = QVBoxLayout(box)
        layout.addLayout(self._remote_path_row())
        layout.addWidget(self.remote_table, stretch=1)
        layout.addWidget(self.remote_status_label)
        return box

    def _folder_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(QLabel("Path"))
        row.addWidget(self.local_folder_edit)
        button = QPushButton("Browse")
        button.clicked.connect(self._browse_local_folder)
        row.addWidget(button)
        row.addWidget(self.local_up_button)
        row.addWidget(self.local_refresh_button)
        row.addWidget(self.local_new_folder_button)
        row.addWidget(self.local_rename_button)
        row.addWidget(self.local_delete_button)
        return row

    def _remote_path_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(QLabel("Path"))
        row.addWidget(self.remote_path_edit)
        row.addWidget(self.remote_load_button)
        row.addWidget(self.remote_up_button)
        row.addWidget(self.remote_refresh_button)
        row.addWidget(self.remote_new_folder_button)
        row.addWidget(self.remote_rename_button)
        row.addWidget(self.remote_delete_button)
        return row

    def _load_profile_combo(self) -> None:
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        for profile in self.profiles:
            self.profile_combo.addItem(profile.name)
        self.profile_combo.blockSignals(False)
        self._profile_changed()

    def _profile_changed(self) -> None:
        self.session_passphrase = None
        self._clear_sync_selection()
        profile = self.current_profile()
        if profile:
            self.remote_path_edit.setText(profile.remote_path)
        self._update_status()

    def current_profile(self) -> Profile | None:
        index = self.profile_combo.currentIndex()
        if 0 <= index < len(self.profiles):
            return self.profiles[index].normalized()
        return None

    def _new_profile(self) -> None:
        dialog = ProfileDialog(parent=self)
        if dialog.exec() == ProfileDialog.Accepted:
            self.profiles = upsert_profile(self.profiles, dialog.profile())
            save_profiles(self.profiles)
            self._load_profile_combo()

    def _edit_profile(self) -> None:
        profile = self.current_profile()
        if not profile:
            return
        dialog = ProfileDialog(profile, self)
        if dialog.exec() == ProfileDialog.Accepted:
            self.profiles = upsert_profile(self.profiles, dialog.profile())
            save_profiles(self.profiles)
            self._load_profile_combo()

    def _delete_profile(self) -> None:
        index = self.profile_combo.currentIndex()
        if index < 0:
            return
        if len(self.profiles) == 1:
            QMessageBox.information(self, "Profile required", "At least one profile is required.")
            return
        removed = self.profiles[index]
        if QMessageBox.question(self, "Delete profile", f"Delete profile '{removed.name}'?") == QMessageBox.Yes:
            del self.profiles[index]
            save_profiles(self.profiles)
            self._load_profile_combo()

    def _save_profiles(self) -> None:
        save_profiles(self.profiles)
        self._append_log("Profiles saved.\n")

    def _browse_local_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select local folder", str(Path.home()))
        if path:
            self.local_folder_edit.setText(path)
            self._set_local_root(path)

    def _set_local_root(self, path: str) -> None:
        local = Path(path).expanduser()
        if local.is_file():
            local = local.parent
        if not local.exists():
            return
        self.local_folder_edit.setText(str(local))
        self._clear_sync_selection()
        self.local_tree.setRootIndex(self.local_model.setRootPath(str(local)))

    def _refresh_local_tree(self) -> None:
        self._set_local_root(self.local_folder_edit.text() or str(Path.home()))

    def _local_go_up(self) -> None:
        current = Path(self.local_folder_edit.text() or str(Path.home())).expanduser()
        parent = current.parent if current.parent != current else current
        self._set_local_root(str(parent))

    def _local_double_clicked(self, index) -> None:
        path = Path(self.local_model.filePath(index))
        if path.is_dir():
            self._set_local_root(str(path))

    def _selected_local_files(self) -> list[Path]:
        return [path for path in self._selected_local_paths() if path.is_file()]

    def _selected_local_paths(self) -> list[Path]:
        indexes = self.local_tree.selectionModel().selectedRows(0)
        paths: list[Path] = []
        for index in indexes:
            path = Path(self.local_model.filePath(index)).expanduser()
            if path.exists():
                paths.append(path)
        return paths

    def _local_new_folder(self) -> None:
        root = Path(self.local_folder_edit.text() or str(Path.home())).expanduser()
        name, accepted = QInputDialog.getText(self, "New local folder", "Folder name:")
        if not accepted:
            return
        name = name.strip()
        if not name or "/" in name or "\\" in name:
            self._show_errors(["Folder name is required and must not contain path separators."])
            return
        target = root / name
        try:
            target.mkdir()
        except OSError as exc:
            self._show_errors([f"Could not create local folder: {exc}"])
            return
        self._append_log(f"Created local folder: {target}\n")
        self._refresh_local_tree()

    def _local_rename_or_move(self) -> None:
        paths = self._selected_local_paths()
        if len(paths) != 1:
            self._show_errors(["Select exactly one local file or folder to rename/move."])
            return
        source = paths[0]
        text, accepted = QInputDialog.getText(
            self,
            "Rename/move local item",
            "New name, or full destination path:",
            text=source.name,
        )
        if not accepted:
            return
        text = text.strip()
        if not text:
            self._show_errors(["Destination is required."])
            return
        destination = Path(text).expanduser()
        if not destination.is_absolute():
            destination = source.parent / text
        if destination.exists():
            self._show_errors([f"Destination already exists: {destination}"])
            return
        try:
            source.rename(destination)
        except OSError as exc:
            self._show_errors([f"Could not rename/move local item: {exc}"])
            return
        self._append_log(f"Moved local item: {source} -> {destination}\n")
        self._refresh_local_tree()

    def _local_delete_selected(self) -> None:
        paths = self._selected_local_paths()
        if not paths:
            self._show_errors(["Select one or more local files or folders to delete."])
            return
        root = Path(self.local_folder_edit.text()).expanduser().absolute()
        unique: list[Path] = []
        for path in sorted({item.absolute() for item in paths}, key=lambda item: len(item.parts)):
            if path == root:
                self._show_errors(["The displayed local folder itself cannot be deleted. Select items inside it instead."])
                return
            if any(path.is_relative_to(parent) for parent in unique):
                continue
            unique.append(path)
        preview = "\n".join(str(path) for path in unique[:10])
        if len(unique) > 10:
            preview += f"\n... and {len(unique) - 10:,} more"
        confirmation, accepted = QInputDialog.getText(
            self,
            "Delete local items",
            f"Type DELETE to permanently delete {len(unique):,} selected item(s).\n\n{preview}\n\n"
            "Items will be moved to the Recycle Bin in the background:",
        )
        if not accepted or confirmation.strip() != "DELETE":
            return
        self._start_worker(
            LocalDeleteWorker(unique),
            "Delete local items",
            refresh_local=True,
        )

    def _start_ssh_test(self) -> None:
        profile = self.current_profile()
        if not profile:
            return
        errors = self._profile_errors(profile, require_rsync=False)
        if errors:
            self._show_errors(errors)
            return
        if not is_executable_file(self.detected_ssh):
            self._show_errors([f"ssh.exe was not found at {self.detected_ssh}. Install MSYS2 or add OpenSSH."])
            return
        passphrase = self._get_session_passphrase(profile)
        if passphrase is None:
            return
        self._start_worker(SshTestWorker(profile, self.detected_ssh, passphrase=passphrase), "SSH test")

    def _refresh_remote_table(self) -> None:
        profile = self.current_profile()
        if not profile:
            return
        if self.remote_thread:
            return
        errors = self._profile_errors(profile, require_rsync=False)
        if errors:
            self._show_errors(errors)
            return
        passphrase = self._get_session_passphrase(profile)
        if passphrase is None:
            return
        try:
            attempts = [
                (
                    host,
                    build_list_remote_entries_command(
                        profile_with_host(profile, host),
                        self.remote_path_edit.text(),
                        ssh_path=self.detected_ssh,
                        batch_mode=not bool(passphrase),
                    ),
                )
                for host in fallback_hosts(profile)
            ]
        except ValueError as exc:
            self._show_errors([str(exc)])
            return

        requested_path = self.remote_path_edit.text().strip().rstrip("/") or "/"
        request_context = (
            profile.name,
            profile.host,
            profile.username,
            profile.ssh_port,
            profile.ssh_key_path,
            requested_path,
        )
        self._remote_request_id += 1
        request_id = self._remote_request_id
        self.remote_status_label.setText("Loading remote directory...")
        self._set_remote_busy(True)
        worker = RemoteListWorker(attempts[0][1], passphrase, attempts=attempts)
        worker.request_id = request_id
        worker.request_context = request_context
        thread = QThread(self)
        worker.moveToThread(thread)
        worker.finished.connect(self._remote_listing_finished)
        worker.finished.connect(thread.quit, Qt.DirectConnection)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.request_id = request_id
        thread.finished.connect(self._remote_thread_finished)
        thread.started.connect(worker.run)
        self.remote_thread = thread
        self.remote_worker = worker
        thread.start()

    @Slot(object, str)
    def _remote_listing_finished(self, entries: object, error: str) -> None:
        worker = self.sender()
        if not isinstance(worker, RemoteListWorker):
            return
        request_id = worker.request_id
        request_context = worker.request_context
        if request_id != self._remote_request_id:
            return
        profile = self.current_profile()
        current_context = (
            profile.name if profile else "",
            profile.host if profile else "",
            profile.username if profile else "",
            profile.ssh_port if profile else 0,
            profile.ssh_key_path if profile else "",
            self.remote_path_edit.text().strip().rstrip("/") or "/",
        )
        if current_context != request_context:
            self.remote_entries = []
            self.remote_entries_path = ""
            self.remote_table.setRowCount(0)
            self.remote_status_label.setText("Remote directory changed while loading. Refresh to view it.")
            return
        if error:
            if "cancelled" in error.lower():
                self.remote_status_label.setText("Remote listing cancelled.")
                return
            self.remote_status_label.setText("Remote listing failed.")
            QMessageBox.warning(self, "Remote listing failed", error)
            return
        remote_entries = list(entries)
        self.remote_entries = remote_entries
        self.remote_entries_path = self.remote_path_edit.text().strip().rstrip("/")
        self._populate_remote_table(remote_entries)
        self.remote_status_label.setText(f"{len(remote_entries)} items in {self.remote_path_edit.text().strip()}")

    @Slot()
    def _remote_thread_finished(self) -> None:
        thread = self.sender()
        if not isinstance(thread, QThread):
            return
        request_id = thread.request_id
        if request_id != self._remote_request_id or self.remote_thread is not thread:
            return
        self.remote_thread = None
        self.remote_worker = None
        self._set_remote_busy(False)

    def _populate_remote_table(self, entries: list[RemoteEntry]) -> None:
        self.remote_table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            name_item = QTableWidgetItem(entry.name)
            size_item = QTableWidgetItem(entry.size_label)
            type_item = QTableWidgetItem(entry.type_label)
            modified_item = QTableWidgetItem(entry.modified)
            for item in (name_item, size_item, type_item, modified_item):
                item.setData(Qt.UserRole, entry.path)
                item.setData(Qt.UserRole + 1, entry.is_dir)
            self.remote_table.setItem(row, 0, name_item)
            self.remote_table.setItem(row, 1, size_item)
            self.remote_table.setItem(row, 2, type_item)
            self.remote_table.setItem(row, 3, modified_item)

    def _remote_double_clicked(self, item: QTableWidgetItem) -> None:
        if item.data(Qt.UserRole + 1):
            self.remote_path_edit.setText(str(item.data(Qt.UserRole)))
            self._clear_sync_selection()
            self._refresh_remote_table()

    def _selected_remote_files(self) -> list[RemoteEntry]:
        return [entry for entry in self._selected_remote_entries() if not entry.is_dir]

    def _selected_remote_entries(self) -> list[RemoteEntry]:
        current_path = self.remote_path_edit.text().strip().rstrip("/")
        if self.remote_entries_path != current_path:
            return []
        rows = {index.row() for index in self.remote_table.selectionModel().selectedRows()}
        if not rows:
            rows = {item.row() for item in self.remote_table.selectedItems()}
        rows = sorted(rows)
        entries: list[RemoteEntry] = []
        for row in rows:
            if row < 0 or row >= len(self.remote_entries):
                continue
            entry = self.remote_entries[row]
            entries.append(entry)
        return entries

    def _remote_new_folder(self) -> None:
        profile = self.current_profile()
        if not profile:
            return
        name, accepted = QInputDialog.getText(self, "New remote folder", "Folder name:")
        if not accepted:
            return
        name = name.strip()
        if not name or "/" in name:
            self._show_errors(["Folder name is required and must not contain '/'."])
            return
        errors = self._profile_errors(profile, require_rsync=False)
        if errors:
            self._show_errors(errors)
            return
        passphrase = self._get_session_passphrase(profile)
        if passphrase is None:
            return

        def command_factory(attempt_profile: Profile) -> list[str]:
            return build_remote_mkdir_command(
                attempt_profile,
                self.remote_path_edit.text(),
                name,
                ssh_path=self.detected_ssh,
                batch_mode=not bool(passphrase),
            )

        self._start_worker(
            FallbackCommandWorker(profile, command_factory, f"remote_mkdir_{profile.name}", passphrase=passphrase, ssh_path=self.detected_ssh),
            "Create remote folder",
            refresh_remote=True,
        )

    def _remote_rename_or_move(self) -> None:
        profile = self.current_profile()
        if not profile:
            return
        errors = self._profile_errors(profile, require_rsync=False)
        if errors:
            self._show_errors(errors)
            return
        entries = self._selected_remote_entries()
        if len(entries) != 1:
            self._show_errors(["Select exactly one remote file or folder to rename/move."])
            return
        entry = entries[0]
        text, accepted = QInputDialog.getText(
            self,
            "Rename/move remote item",
            "New name, or full remote destination path:",
            text=entry.name,
        )
        if not accepted:
            return
        text = text.strip()
        if not text:
            self._show_errors(["Destination is required."])
            return
        if text.startswith("/"):
            destination = text.rstrip("/")
        else:
            parent = entry.path.rsplit("/", 1)[0]
            destination = f"{parent}/{text}"
        passphrase = self._get_session_passphrase(profile)
        if passphrase is None:
            return

        def command_factory(attempt_profile: Profile) -> list[str]:
            return build_remote_move_command(
                attempt_profile,
                entry.path,
                destination,
                ssh_path=self.detected_ssh,
                batch_mode=not bool(passphrase),
            )

        self._start_worker(
            FallbackCommandWorker(profile, command_factory, f"remote_move_{profile.name}", passphrase=passphrase, ssh_path=self.detected_ssh),
            "Rename/move remote item",
            refresh_remote=True,
        )

    def _remote_delete_selected(self) -> None:
        profile = self.current_profile()
        if not profile:
            return
        errors = self._profile_errors(profile, require_rsync=False)
        if errors:
            self._show_errors(errors)
            return
        entries = self._selected_remote_entries()
        if not entries:
            self._show_errors(["Select one or more remote files or folders to delete."])
            return
        preview = "\n".join(f"- {entry.path}" for entry in entries[:10])
        if len(entries) > 10:
            preview += f"\n... and {len(entries) - 10:,} more"
        typed, accepted = QInputDialog.getText(
            self,
            "Delete remote items",
            f"Type DELETE to permanently delete {len(entries):,} remote item(s):\n\n{preview}",
        )
        if not accepted or typed != "DELETE":
            return
        passphrase = self._get_session_passphrase(profile)
        if passphrase is None:
            return

        def command_factory(attempt_profile: Profile) -> list[str]:
            return build_remote_delete_command(
                attempt_profile,
                [entry.path for entry in entries],
                ssh_path=self.detected_ssh,
                batch_mode=not bool(passphrase),
            )

        self._start_worker(
            FallbackCommandWorker(profile, command_factory, f"remote_delete_{profile.name}", passphrase=passphrase, ssh_path=self.detected_ssh),
            "Delete remote items",
            refresh_remote=True,
        )

    def _remote_go_up(self) -> None:
        current = self.remote_path_edit.text().strip().rstrip("/") or "/"
        if current == "/":
            return
        parent = current.rsplit("/", 1)[0] or "/"
        self.remote_path_edit.setText(parent)
        self._clear_sync_selection()
        self._refresh_remote_table()

    def _visible_remote_file_list(self) -> Path | None:
        current_path = self.remote_path_edit.text().strip().rstrip("/")
        if not self.remote_entries or self.remote_entries_path != current_path:
            return None
        files = [entry.name for entry in self.remote_entries if not entry.is_dir]
        directories = [entry.name for entry in self.remote_entries if entry.is_dir]
        if not files or directories:
            return None
        path = new_log_file("download_visible_files").with_suffix(".txt")
        path.write_text("\n".join(files) + "\n", encoding="utf-8", newline="\n")
        return path

    def _visible_remote_file_paths(self) -> list[str]:
        current_path = self.remote_path_edit.text().strip().rstrip("/")
        if not self.remote_entries or self.remote_entries_path != current_path:
            return []
        return [entry.path for entry in self.remote_entries if not entry.is_dir]

    def _selected_local_file_list(self, files: list[Path]) -> Path:
        root = Path(self.local_folder_edit.text()).expanduser()
        path = new_log_file("upload_selected_files").with_suffix(".txt")
        lines: list[str] = []
        for file in files:
            relative = file.relative_to(root)
            lines.append(relative.as_posix())
        path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        return path

    def _selected_remote_file_list(self, entries: list[RemoteEntry]) -> Path:
        path = new_log_file("download_selected_files").with_suffix(".txt")
        path.write_text("\n".join(entry.name for entry in entries) + "\n", encoding="utf-8", newline="\n")
        return path

    def _recall_medici(self) -> None:
        profile = self.current_profile()
        if not profile:
            return
        errors = self._profile_errors(profile, require_rsync=False)
        if errors:
            self._show_errors(errors)
            return
        try:
            medici_path = medici_path_for_remote_path(self.remote_path_edit.text())
        except ValueError as exc:
            self._show_errors([str(exc)])
            return
        remote_files = self._visible_remote_file_paths()
        if not remote_files:
            self._show_errors(["Load a remote folder containing files before running tape recall."])
            return
        if (
            QMessageBox.question(
                self,
                "Recall tape",
                f"Run recall_medici for {len(remote_files):,} visible files under {medici_path}?\n\nThis runs one file at a time for testing and can take a long time on QRIScloud/HPC.",
            )
            != QMessageBox.Yes
        ):
            return
        passphrase = self._get_session_passphrase(profile)
        if passphrase is None:
            return

        self._start_worker(
            RecallMediciWorker(
                profile,
                remote_files,
                self.detected_ssh,
                passphrase=passphrase,
                batch_size=1,
            ),
            "Recall tape",
        )

    def _build_sync_selection(self) -> None:
        profile = self.current_profile()
        if not profile:
            return
        if self.compare_thread or self.current_thread or self.remote_thread:
            QMessageBox.information(self, "Operation running", "Wait for the current operation to finish.")
            return
        errors = self._profile_errors(profile, require_rsync=True)
        errors.extend(validate_transfer_inputs(profile, self.local_folder_edit.text(), self.remote_path_edit.text()))
        if errors:
            self._show_errors(errors)
            return
        passphrase = self._get_session_passphrase(profile)
        if passphrase is None:
            return
        self._clear_sync_selection()
        self.log_output.clear()
        self._set_compare_running(True)
        self.transfer_progress.setRange(0, 0)
        self.transfer_status_label.setText("Building sync selection...")
        self._append_log("Building sync selection by name, size, and modified time...\n")
        worker = SyncCompareWorker(
            profile,
            self.detected_ssh,
            self.local_folder_edit.text(),
            self.remote_path_edit.text(),
            passphrase,
        )
        thread = QThread(self)
        worker.moveToThread(thread)
        worker.output.connect(self._append_log)
        worker.finished.connect(self._sync_compare_finished)
        worker.finished.connect(thread.quit, Qt.DirectConnection)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._compare_thread_finished)
        thread.started.connect(worker.run)
        self.compare_thread = thread
        self.compare_worker = worker
        thread.start()

    @Slot(object, str)
    def _sync_compare_finished(self, result: object, error: str) -> None:
        self._set_compare_running(False)
        self.transfer_progress.setRange(0, 100)
        if error:
            self.transfer_progress.setValue(0)
            self.transfer_status_label.setText("Sync comparison failed.")
            self._append_log(f"\n{error}\n")
            QMessageBox.warning(self, "Sync comparison failed", error)
            return
        self.sync_selection = dict(result)
        selected = int(self.sync_selection["selected"])
        self.upload_selection_button.setEnabled(selected > 0)
        if selected == 0:
            self.transfer_progress.setValue(100)
            self.transfer_status_label.setText("Sync comparison complete: no files selected.")
            self._append_log("\nNo missing or changed files found.\n")
        else:
            self.transfer_progress.setValue(100)
            self.transfer_status_label.setText(f"Sync comparison complete: {selected:,} files selected.")
            self._append_log("\nUse Upload selection to transfer only these files.\n")

    def _compare_thread_finished(self) -> None:
        self.compare_thread = None
        self.compare_worker = None

    def _upload_sync_selection(self) -> None:
        profile = self.current_profile()
        if not profile or not self.sync_selection:
            QMessageBox.information(self, "No selection", "Build a sync selection first.")
            return
        if self.sync_selection.get("local_folder") != self.local_folder_edit.text() or self.sync_selection.get("remote_path") != self.remote_path_edit.text():
            self._clear_sync_selection()
            QMessageBox.information(self, "Selection out of date", "The local or remote path changed. Build the sync selection again.")
            return
        selected = int(self.sync_selection.get("selected", 0))
        if selected == 0:
            QMessageBox.information(self, "No files selected", "No missing or changed files were found.")
            return
        passphrase = self._get_session_passphrase(profile)
        if passphrase is None:
            return
        if (
            QMessageBox.question(
                self,
                "Upload selection",
                f"Upload {selected:,} missing or changed files to {self.remote_path_edit.text()}?",
            )
            != QMessageBox.Yes
        ):
            return
        def command_factory(attempt_profile: Profile) -> list[str]:
            return build_rsync_command(
                attempt_profile,
                self.local_folder_edit.text(),
                remote_path=self.remote_path_edit.text(),
                dry_run=False,
                ssh_path=self.detected_ssh,
                batch_mode=not bool(passphrase),
                files_from=str(self.sync_selection["file_list"]),
            )

        self._start_worker(
            FallbackCommandWorker(
                profile,
                command_factory,
                f"sync_selection_upload_{profile.name}",
                passphrase=passphrase,
                ssh_path=self.detected_ssh,
            ),
            "Upload selection",
        )

    def _start_rsync(self, dry_run: bool, direction: str = "upload") -> None:
        profile = self.current_profile()
        if not profile:
            return
        scope = str(self.transfer_scope_combo.currentData())
        selected_local_paths = self._selected_local_paths() if direction == "upload" and scope == "selected" else []
        selected_remote_entries = self._selected_remote_entries() if direction == "download" and scope == "selected" else []
        if scope == "selected":
            selected_items = selected_local_paths if direction == "upload" else selected_remote_entries
            if not selected_items:
                self._show_errors([f"Select one or more {direction} files, or choose Current folders."])
                return
            directories = [item for item in selected_items if (item.is_dir() if isinstance(item, Path) else item.is_dir)]
            if directories:
                self._show_errors(["Selected-files transfers do not include folders. Select files only, or choose Current folders."])
                return
        selected_local_files = selected_local_paths
        selected_remote_files = selected_remote_entries
        selected_local_file = selected_local_files[0] if len(selected_local_files) == 1 else None
        selected_remote_file = selected_remote_files[0] if len(selected_remote_files) == 1 else None
        local_transfer_path = str(selected_local_file) if selected_local_file else self.local_folder_edit.text()
        remote_transfer_path = selected_remote_file.path if selected_remote_file else self.remote_path_edit.text()
        errors = self._profile_errors(profile, require_rsync=True)
        errors.extend(validate_transfer_inputs(profile, local_transfer_path, remote_transfer_path, direction=direction))
        if errors:
            self._show_errors(errors)
            return
        try:
            selected_local_files_from = self._selected_local_file_list(selected_local_files) if len(selected_local_files) > 1 else None
        except ValueError:
            self._show_errors(["Every selected local file must be inside the displayed local folder."])
            return
        selected_remote_files_from = self._selected_remote_file_list(selected_remote_files) if len(selected_remote_files) > 1 else None
        if direction == "upload" and scope == "folder":
            self._start_upload_scan(profile, dry_run)
            return
        self._start_resolved_rsync(
            profile,
            dry_run,
            direction,
            scope,
            selected_local_files,
            selected_remote_files,
            selected_local_file,
            selected_remote_file,
            local_transfer_path,
            remote_transfer_path,
            selected_local_files_from,
            selected_remote_files_from,
        )

    def _start_resolved_rsync(
        self,
        profile: Profile,
        dry_run: bool,
        direction: str,
        scope: str,
        selected_local_files: list[Path],
        selected_remote_files: list[RemoteEntry],
        selected_local_file: Path | None,
        selected_remote_file: RemoteEntry | None,
        local_transfer_path: str,
        remote_transfer_path: str,
        selected_local_files_from: Path | None,
        selected_remote_files_from: Path | None,
        scan: FolderScan | None = None,
    ) -> None:
        source = local_transfer_path if direction == "upload" else remote_transfer_path
        destination = remote_transfer_path if direction == "upload" else local_transfer_path
        item_count = len(selected_local_files) if direction == "upload" else len(selected_remote_files)
        count_text = f"{item_count:,} selected file(s)" if scope == "selected" else "current folder (recursive)"
        if scan is not None:
            count_text = f"{scan.file_count:,} files ({scan.total_bytes:,} bytes; {scan.tiny_file_count:,} under 1 MB)"
        warnings = ""
        if scan and scan.warnings():
            warnings = "\n\nWarnings:\n" + "\n".join(f"- {warning}" for warning in scan.warnings())
        mode = "Dry run / compare (no files will be changed)" if dry_run else "Transfer (existing files may be updated; destination files are not deleted)"
        if (
            QMessageBox.question(
                self,
                f"Confirm {direction}",
                f"Scope: {count_text}\nSource: {source}\nDestination: {destination}\nMode: {mode}{warnings}\n\nContinue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            != QMessageBox.Yes
        ):
            return
        if direction == "download":
            visible_files_from = selected_remote_files_from if selected_remote_files_from else None
        else:
            visible_files_from = selected_local_files_from if direction == "upload" else None
        passphrase = self._get_session_passphrase(profile)
        if passphrase is None:
            return
        def command_factory(attempt_profile: Profile) -> list[str]:
            return build_rsync_command(
                attempt_profile,
                local_transfer_path,
                remote_path=remote_transfer_path,
                dry_run=dry_run,
                ssh_path=self.detected_ssh,
                batch_mode=not bool(passphrase),
                files_from=visible_files_from,
                direction=direction,
                remote_is_file=selected_remote_file is not None,
            )

        if dry_run:
            action = f"{direction}_dry_run"
            label = "Compare download" if direction == "download" else "Dry run"
        else:
            action = direction
            label = "Download" if direction == "download" else "Upload"
        self._start_worker(
            FallbackCommandWorker(
                profile,
                command_factory,
                f"{action}_{profile.name}",
                passphrase=passphrase,
                ssh_path=self.detected_ssh,
            ),
            label,
        )
        if visible_files_from:
            if selected_local_files_from:
                self._append_log(f"Uploading {len(selected_local_files):,} selected files.\n")
            elif selected_remote_files_from:
                self._append_log(f"Downloading {len(selected_remote_files):,} selected files.\n")
            else:
                self._append_log(
                    f"Using visible remote file list ({self.remote_table.rowCount():,} entries) to avoid recursive rsync discovery.\n"
                )
        elif selected_local_file:
            self._append_log(f"Uploading selected file: {selected_local_file}\n")
        elif selected_remote_file:
            self._append_log(f"Downloading selected file: {selected_remote_file.path}\n")

    def _start_upload_scan(self, profile: Profile, dry_run: bool) -> None:
        local_path = self.local_folder_edit.text()
        remote_path = self.remote_path_edit.text()

        def continue_upload(worker: object, code: int) -> None:
            if code != 0 or not isinstance(worker, FolderScanWorker) or worker.result is None:
                return
            self._start_resolved_rsync(
                profile,
                dry_run,
                "upload",
                "folder",
                [],
                [],
                None,
                None,
                local_path,
                remote_path,
                None,
                None,
                scan=worker.result,
            )

        self._start_worker(FolderScanWorker(local_path), "Scan upload folder", after_finish=continue_upload)

    def _ask_passphrase(self, profile: Profile) -> str | None:
        if not profile.ssh_key_path:
            return ""
        passphrase, accepted = QInputDialog.getText(
            self,
            "SSH key passphrase",
            "Enter SSH key passphrase. Leave blank if the key is already loaded in ssh-agent.",
            QLineEdit.Password,
        )
        if not accepted:
            return None
        return passphrase

    def _get_session_passphrase(self, profile: Profile) -> str | None:
        if not profile.ssh_key_path:
            return ""
        if self.session_passphrase is not None:
            return self.session_passphrase
        passphrase = self._ask_passphrase(profile)
        if passphrase is not None:
            self.session_passphrase = passphrase
        return passphrase

    def _start_worker(
        self,
        worker: CommandWorker | SshTestWorker | RecallMediciWorker | FolderScanWorker | LocalDeleteWorker,
        label: str,
        refresh_local: bool = False,
        refresh_remote: bool = False,
        after_finish=None,
    ) -> None:
        if self.current_thread or self.remote_thread or self.compare_thread:
            QMessageBox.information(self, "Operation running", "Another operation is already running.")
            return
        self.log_output.clear()
        thread = QThread(self)
        worker.moveToThread(thread)
        worker.output.connect(self._append_log)
        self._current_operation_id += 1
        operation_id = self._current_operation_id
        worker.operation_id = operation_id
        thread.operation_id = operation_id
        worker.finished.connect(self._worker_finished)
        worker.finished.connect(thread.quit, Qt.DirectConnection)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._current_thread_finished)
        thread.finished.connect(thread.deleteLater)
        thread.started.connect(worker.run)
        self.current_thread = thread
        self.current_worker = worker
        self.current_label = label
        self.current_finished_handled = False
        self.current_refresh_local = refresh_local
        self.current_refresh_remote = refresh_remote
        self.current_after_finish = after_finish
        self.current_exit_code = None
        self._set_running(True)
        self._reset_transfer_progress(label)
        if isinstance(worker, LocalDeleteWorker):
            self.stop_button.setEnabled(False)
            self.transfer_status_label.setText("Moving items to the Recycle Bin (this OS operation cannot be interrupted safely)...")
        self._append_log(f"{label} started.\n")
        thread.start()

    @Slot(int)
    def _worker_finished(self, code: int) -> None:
        worker = self.sender()
        if worker is None:
            return
        operation_id = getattr(worker, "operation_id", None)
        if operation_id != self._current_operation_id or self.current_finished_handled:
            return
        self.current_finished_handled = True
        self.current_exit_code = code
        label = self.current_label or "Command"
        self._append_log(f"\n{label} finished with exit code {code}.\n")
        if code == 0 and any(word in label.lower() for word in ("upload", "download", "dry run", "recall")):
            self.transfer_progress.setValue(100)
        self.transfer_status_label.setText(f"{label} finished with exit code {code}.")

    @Slot()
    def _current_thread_finished(self) -> None:
        thread = self.sender()
        if not isinstance(thread, QThread):
            return
        operation_id = getattr(thread, "operation_id", None)
        if operation_id != self._current_operation_id or self.current_thread is not thread:
            return
        label = self.current_label or "Command"
        if not self.current_finished_handled:
            self.current_finished_handled = True
            self._append_log(f"\n{label} thread finished.\n")
            self.transfer_status_label.setText(f"{label} finished.")
        refresh_local = self.current_refresh_local
        refresh_remote = self.current_refresh_remote
        worker = self.current_worker
        exit_code = self.current_exit_code if self.current_exit_code is not None else 1
        after_finish = self.current_after_finish
        self.current_thread = None
        self.current_worker = None
        self.current_label = ""
        self.current_after_finish = None
        self.current_exit_code = None
        self._set_running(False)
        self._run_post_worker_refresh(refresh_local, refresh_remote)
        if after_finish and not self._close_after_stop:
            after_finish(worker, exit_code)

    def _run_post_worker_refresh(self, refresh_local: bool, refresh_remote: bool) -> None:
        self.current_refresh_local = False
        self.current_refresh_remote = False
        if refresh_local:
            self._refresh_local_tree()
        if refresh_remote:
            self._refresh_remote_table()

    def _cancel_current(self) -> None:
        if isinstance(self.current_worker, (CommandWorker, SshTestWorker, RecallMediciWorker, FolderScanWorker, LocalDeleteWorker)):
            if isinstance(self.current_worker, LocalDeleteWorker):
                self._append_log("\nThe current Recycle Bin operation cannot be interrupted safely.\n")
                return
            self.current_after_finish = None
            self._append_log("\nCancelling current command...\n")
            self.current_worker.cancel()
        elif isinstance(self.compare_worker, SyncCompareWorker):
            self._append_log("\nCancelling sync comparison...\n")
            self.compare_worker.cancel()
        else:
            self._append_log("\nNothing cancellable is currently running.\n")

    def closeEvent(self, event) -> None:
        active_threads = [thread for thread in (self.current_thread, self.remote_thread, self.compare_thread) if thread and thread.isRunning()]
        if not active_threads:
            self._close_after_stop = False
            event.accept()
            return
        if isinstance(self.current_worker, LocalDeleteWorker):
            QMessageBox.information(
                self,
                "Recycle Bin operation running",
                "Please wait for the current Recycle Bin operation to finish before closing the app.",
            )
            event.ignore()
            return
        if self._close_after_stop:
            event.ignore()
            return
        answer = QMessageBox.question(
            self,
            "Operations running",
            "An operation is still running. Cancel it and close QRIS Rsync Manager?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            event.ignore()
            return
        self._close_after_stop = True
        self.current_after_finish = None
        self.transfer_status_label.setText("Stopping active operations before closing...")
        if self.current_worker and hasattr(self.current_worker, "cancel"):
            self.current_worker.cancel()
        if self.remote_worker:
            self.remote_worker.cancel()
        if self.compare_worker:
            self.compare_worker.cancel()
        event.ignore()
        QTimer.singleShot(100, self._close_when_operations_stop)

    def _close_when_operations_stop(self) -> None:
        if not self._close_after_stop:
            return
        active = any(
            thread and thread.isRunning()
            for thread in (self.current_thread, self.remote_thread, self.compare_thread)
        )
        if active:
            QTimer.singleShot(100, self._close_when_operations_stop)
            return
        self.close()

    def _set_running(self, running: bool) -> None:
        self.profile_box.setEnabled(not running and self.remote_thread is None and self.compare_thread is None)
        self.remote_path_edit.setEnabled(not running and self.remote_thread is None and self.compare_thread is None)
        self.ssh_button.setEnabled(not running)
        self.remote_load_button.setEnabled(not running and self.remote_thread is None)
        self.remote_refresh_button.setEnabled(not running and self.remote_thread is None)
        self.remote_up_button.setEnabled(not running and self.remote_thread is None)
        self.remote_table.setEnabled(not running and self.remote_thread is None)
        self.local_new_folder_button.setEnabled(not running)
        self.local_rename_button.setEnabled(not running)
        self.local_delete_button.setEnabled(not running)
        self.remote_new_folder_button.setEnabled(not running and self.remote_thread is None)
        self.remote_rename_button.setEnabled(not running and self.remote_thread is None)
        self.remote_delete_button.setEnabled(not running and self.remote_thread is None)
        self.dry_run_button.setEnabled(not running)
        self.download_dry_run_button.setEnabled(not running)
        self.build_selection_button.setEnabled(not running and self.compare_thread is None)
        self.upload_selection_button.setEnabled(not running and self.compare_thread is None and bool(self.sync_selection and self.sync_selection.get("selected")))
        self.upload_button.setEnabled(not running)
        self.recall_button.setEnabled(not running)
        self.download_button.setEnabled(not running)
        self.transfer_scope_combo.setEnabled(not running)
        self.stop_button.setEnabled(running)
        if not running:
            self._update_transfer_scope_ui()

    def _set_remote_busy(self, busy: bool) -> None:
        self.profile_box.setEnabled(not busy and self.current_thread is None and self.compare_thread is None)
        self.remote_path_edit.setEnabled(not busy and self.current_thread is None and self.compare_thread is None)
        self.remote_load_button.setEnabled(not busy and self.current_thread is None)
        self.remote_refresh_button.setEnabled(not busy and self.current_thread is None)
        self.remote_up_button.setEnabled(not busy)
        self.remote_table.setEnabled(not busy)
        self.remote_new_folder_button.setEnabled(not busy and self.current_thread is None)
        self.remote_rename_button.setEnabled(not busy and self.current_thread is None)
        self.remote_delete_button.setEnabled(not busy and self.current_thread is None)
        self.transfer_scope_combo.setEnabled(not busy and self.current_thread is None and self.compare_thread is None)
        self.ssh_button.setEnabled(not busy and self.current_thread is None and self.compare_thread is None)
        self.dry_run_button.setEnabled(not busy and self.current_thread is None and self.compare_thread is None)
        self.download_dry_run_button.setEnabled(not busy and self.current_thread is None and self.compare_thread is None)
        self.upload_button.setEnabled(not busy and self.current_thread is None and self.compare_thread is None)
        self.download_button.setEnabled(not busy and self.current_thread is None and self.compare_thread is None)
        self.build_selection_button.setEnabled(not busy and self.current_thread is None and self.compare_thread is None)
        self.upload_selection_button.setEnabled(
            not busy
            and self.current_thread is None
            and self.compare_thread is None
            and bool(self.sync_selection and self.sync_selection.get("selected"))
        )
        self.recall_button.setEnabled(not busy and self.current_thread is None and self.compare_thread is None)
        if not busy:
            self._update_transfer_scope_ui()

    def _set_compare_running(self, running: bool) -> None:
        self.profile_box.setEnabled(not running and self.current_thread is None and self.remote_thread is None)
        self.remote_path_edit.setEnabled(not running and self.current_thread is None and self.remote_thread is None)
        self.ssh_button.setEnabled(not running and self.current_thread is None)
        self.dry_run_button.setEnabled(not running and self.current_thread is None)
        self.download_dry_run_button.setEnabled(not running and self.current_thread is None)
        self.build_selection_button.setEnabled(not running and self.current_thread is None)
        self.upload_selection_button.setEnabled(not running and self.remote_thread is None and bool(self.sync_selection and self.sync_selection.get("selected")))
        self.upload_button.setEnabled(not running and self.current_thread is None)
        self.local_new_folder_button.setEnabled(not running and self.current_thread is None)
        self.local_rename_button.setEnabled(not running and self.current_thread is None)
        self.local_delete_button.setEnabled(not running and self.current_thread is None)
        self.recall_button.setEnabled(not running and self.current_thread is None)
        self.remote_new_folder_button.setEnabled(not running and self.current_thread is None and self.remote_thread is None)
        self.remote_rename_button.setEnabled(not running and self.current_thread is None and self.remote_thread is None)
        self.remote_delete_button.setEnabled(not running and self.current_thread is None and self.remote_thread is None)
        self.download_button.setEnabled(not running and self.current_thread is None)
        self.transfer_scope_combo.setEnabled(not running and self.current_thread is None)
        self.stop_button.setEnabled(running or self.current_thread is not None)

    def _update_transfer_scope_ui(self, *_args) -> None:
        selected_scope = str(self.transfer_scope_combo.currentData()) == "selected"
        local_files = self._selected_local_files() if selected_scope else []
        remote_files = self._selected_remote_files() if selected_scope else []
        if selected_scope:
            self.upload_button.setText(f"Upload selected files ({len(local_files):,})")
            self.download_button.setText(f"Download selected files ({len(remote_files):,})")
            self.dry_run_button.setText(f"Compare selected files ({len(local_files):,})")
            self.download_dry_run_button.setText(f"Compare selected files ({len(remote_files):,})")
        else:
            self.upload_button.setText("Upload folder")
            self.download_button.setText("Download folder")
            self.dry_run_button.setText("Compare upload folder")
            self.download_dry_run_button.setText("Compare download folder")

        idle = self.current_thread is None and self.compare_thread is None and self.remote_thread is None
        self.upload_button.setEnabled(idle and (not selected_scope or bool(local_files)))
        self.dry_run_button.setEnabled(idle and (not selected_scope or bool(local_files)))
        self.download_button.setEnabled(idle and (not selected_scope or bool(remote_files)))
        self.download_dry_run_button.setEnabled(idle and (not selected_scope or bool(remote_files)))

    def _clear_sync_selection(self) -> None:
        selection = self.sync_selection
        self.sync_selection = None
        if selection and selection.get("file_list"):
            file_list = Path(str(selection["file_list"]))
            expected_parent = app_data_dir() / "filelists"
            try:
                if file_list.parent.resolve() == expected_parent.resolve() and file_list.name.startswith("sync_selection_"):
                    file_list.unlink(missing_ok=True)
            except OSError:
                pass
        if hasattr(self, "upload_selection_button"):
            self.upload_selection_button.setEnabled(False)

    def _profile_errors(self, profile: Profile, require_rsync: bool) -> list[str]:
        errors: list[str] = []
        if not profile.username:
            errors.append("Username is required in the selected profile.")
        if require_rsync and not is_executable_file(profile.rsync_path):
            errors.append(f"rsync.exe was not found at {profile.rsync_path}. MSYS2 default is C:\\msys64\\usr\\bin\\rsync.exe.")
        return errors

    def _show_errors(self, errors: list[str]) -> None:
        QMessageBox.warning(self, "Cannot start", "\n".join(errors))

    def _append_log(self, text: str) -> None:
        text = text.replace("\r", "\n")
        self.log_output.moveCursor(QTextCursor.End)
        self.log_output.insertPlainText(text)
        self.log_output.moveCursor(QTextCursor.End)
        self._update_transfer_progress(text)

    def _reset_transfer_progress(self, label: str) -> None:
        self._progress_buffer = ""
        self.transfer_progress.setRange(0, 100)
        self.transfer_progress.setValue(0)
        self.transfer_status_label.setText(f"{label} running...")

    def _update_transfer_progress(self, text: str) -> None:
        self._progress_buffer = (self._progress_buffer + text)[-1000:]
        progress = parse_rsync_progress(self._progress_buffer)
        if not progress:
            return
        self.transfer_progress.setValue(progress.percent)
        self.transfer_status_label.setText(
            f"{progress.percent}% | {progress.transferred} | {progress.speed} | ETA {progress.eta}"
        )

    def _update_status(self) -> None:
        profile = self.current_profile()
        if not profile:
            self.status_label.setText("No profile loaded.")
            return
        rsync_state = "found" if is_executable_file(profile.rsync_path) else "not found"
        ssh_state = "found" if is_executable_file(self.detected_ssh) else "not found"
        self.status_label.setText(
            f"rsync: {rsync_state} ({profile.rsync_path}) | ssh: {ssh_state} ({self.detected_ssh})"
        )


def show() -> int:
    app = QApplication([])
    window = MainWindow()
    window.show()
    return app.exec()
