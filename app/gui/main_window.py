from __future__ import annotations

import queue
import ctypes
import codecs
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFileSystemModel,
    QFormLayout,
    QGridLayout,
    QMenu,
    QToolButton,
    QStyle,
    QGroupBox,
    QHBoxLayout,
    QAbstractItemView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QInputDialog,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTextEdit,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from app.core.askpass import (
    build_askpass_environment,
    passphrase_cache_key,
    scrub_askpass_environment,
    ssh_key_was_rejected,
)
from app.core.archive_upload import (
    ArchiveCancelled,
    UploadArchivePlan,
    UploadPackage,
    analyze_upload_tree,
    ARCHIVE_TEMP_RESERVE_BYTES,
    NotEnoughTempSpace,
    plan_archive_batches,
    required_temp_bytes,
    prepare_upload_package,
    split_batch_by_folder,
    subset_upload_plan,
)
from app.core.logging_utils import new_log_file
from app.core.file_scan import FolderScan, scan_folder
from app.core.local_delete import LocalDeleteResult, delete_local_paths
from app.core.local_move import move_local_paths, plan_local_moves
from app.core.medici import build_recall_medici_files_command, medici_path_for_remote_path
from app.core.paths import detect_ssh, is_executable_file
from app.core.profiles import (
    Profile,
    adapt_profile_to_this_computer,
    export_profiles,
    fallback_hosts,
    load_profiles_result,
    merge_imported_profiles,
    read_profile_file,
    profile_with_host,
    save_profiles,
    suggest_ssh_key_path,
    upsert_profile,
)
from app.core.progress import parse_rsync_progress
from app.core.receipt_recorder import (
    ReceiptPlan,
    hash_phase_now,
    record_transfer_receipt,
    upload_local_base,
    upload_receipt_roots,
)
from app.core.remote_dirs import RemoteDirectoryCache, RemoteEntry, build_list_remote_entries_command, format_bytes
from app.core.remote_ops import (
    build_remote_delete_command,
    build_remote_mkdir_command,
    build_remote_move_command,
    build_remote_move_into_command,
)
from app.core.rsync_command import (
    build_collection_copy_command,
    build_rsync_command,
    collection_copy_destinations,
    collection_key,
    collection_of_remote_path,
    validate_transfer_inputs,
)
from app.core.rsync_runner import RsyncRunner
from app.core.ssh_test import run_ssh_test
from app.core.sync_compare import (
    DOWNLOAD,
    UPLOAD,
    RemoteManifestCollector,
    build_remote_manifest_command,
    build_sync_plan,
    scan_local_for_sync,
    write_files_from,
)
from app.gui.profile_dialog import ProfileDialog
from app.gui.remote_folder_loader import RemoteFolderLoader
from app.gui.sync_dialog import SynchronizeDialog
from app.core.bunya import BunyaJob, record_job
from app.gui.recall_jobs import RecallJobsDialog
from app.gui.tape_status import BunyaSubmitWorker, TapeStatusDialog, TapeStatusWorker
from app.gui.collection_pane import (
    CollectionPane,
    exclude_collection,
    fill_collection_combo,
    sync_collection_combo,
)
from app.gui.pane_actions import PaneAction, install_pane_actions
from app.gui.drag_views import (
    LocalMoveTreeView,
    IS_DIR_ROLE,
    PATH_ROLE,
    RemoteMoveTree,
    populate_remote_table,
    selected_remote_entries,
)
from app.gui.remote_browser_dialog import RemoteBrowserDialog, RemoteListWorker
from app.gui.setup_guide_dialog import SetupGuideDialog


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
        receipt_plan: ReceiptPlan | None = None,
        receipt_base: tuple[Path, str] | None = None,
    ) -> None:
        super().__init__([], log_prefix, passphrase=passphrase, ssh_path=ssh_path)
        self.profile = profile
        self.command_factory = command_factory
        # receipt_base: (local folder, remote folder) that rsync's reported
        # names are relative to. Without a plan, no receipt is written.
        self.receipt_plan = receipt_plan
        self.receipt_base = receipt_base
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
            collector = None
            if self.receipt_plan is not None and self.receipt_base is not None:
                collector = self.receipt_plan.add_phase(*self.receipt_base)

            def on_output(text: str) -> None:
                if collector is not None:
                    collector.feed(text)
                self.output.emit(text)

            code = self.runner.run(
                self.command,
                log_file,
                on_output,
                passphrase=self.passphrase,
                ssh_path=self.ssh_path,
                cancel_event=self._cancelled,
            )
            if self.receipt_plan is not None:
                record_transfer_receipt(
                    self.receipt_plan,
                    attempt_profile,
                    completed=code == 0,
                    emit=self.output.emit,
                    run_command=lambda command: self.runner.run(
                        command,
                        log_file,
                        self.output.emit,
                        passphrase=self.passphrase,
                        ssh_path=self.ssh_path,
                        cancel_event=self._cancelled,
                    ),
                    ssh_path=self.ssh_path,
                    batch_mode=not bool(self.passphrase),
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


class UploadAnalysisWorker(QObject):
    output = Signal(str)
    finished = Signal(int)

    def __init__(self, path: str) -> None:
        super().__init__()
        self.path = path
        self.result: UploadArchivePlan | None = None
        self._cancelled = threading.Event()
        self._last_progress = 0

    @Slot()
    def run(self) -> None:
        try:
            self.result = analyze_upload_tree(
                self.path,
                cancel_event=self._cancelled,
                progress_callback=self._report_progress,
            )
            scan = self.result.scan
            self.output.emit(
                f"Local scan: {scan.file_count:,} files, {scan.total_bytes:,} bytes, "
                f"{scan.tiny_file_count:,} files under 1 MB.\n"
            )
            if self.result.groups:
                self.output.emit(
                    f"Automatic archive plan: {len(self.result.groups):,} TAR archive(s) will contain "
                    f"{self.result.archived_file_count:,} files "
                    f"({self.result.archived_bytes:,} bytes).\n"
                )
            self.finished.emit(0)
        except ArchiveCancelled:
            self.output.emit("Upload analysis cancelled.\n")
            self.finished.emit(130)
        except Exception as exc:
            self.output.emit(f"Upload analysis failed: {exc}\n")
            self.finished.emit(1)

    def _report_progress(self, result: FolderScan) -> None:
        examined = result.file_count + result.directory_count + result.skipped_errors
        if examined - self._last_progress >= 1000:
            self._last_progress = examined
            self.output.emit(f"Scanned {result.file_count:,} files in {result.directory_count:,} folders...\n")

    @Slot()
    def cancel(self) -> None:
        self._cancelled.set()


class PackagedUploadWorker(QObject):
    output = Signal(str)
    finished = Signal(int)

    # Do not include SSH's generic 255: it can mean an authentication failure,
    # and repeatedly retrying a rejected key can trigger QRIScloud throttling.
    RETRYABLE_RSYNC_EXIT_CODES = frozenset({10, 12, 30, 35})
    RETRY_DELAYS_SECONDS = (10, 30, 60)
    # rsync 23 (some files unreadable) and 24 (files vanished, normal on a
    # share in use) still let the archives go up; the final code reports it.
    CONTINUE_AFTER_LOOSE_CODES = frozenset({0, 23, 24})
    PARTIAL_TRANSFER_CODE = 23

    def __init__(
        self,
        profile: Profile,
        plan: UploadArchivePlan,
        remote_path: str,
        *,
        dry_run: bool,
        passphrase: str = "",
        ssh_path: str | None = None,
        include_source_directory: bool = False,
    ) -> None:
        super().__init__()
        self.profile = profile
        self.plan = plan
        self.remote_path = remote_path
        self.dry_run = dry_run
        self.passphrase = passphrase
        self.ssh_path = ssh_path
        self.include_source_directory = include_source_directory
        self.runner = RsyncRunner()
        self._cancelled = threading.Event()
        self._log_file: Path | None = None
        self._host_checks_retryable = False
        self._receipt_plan: ReceiptPlan | None = None
        self._collector = None
        self._last_attempt_profile: Profile | None = None
        self._changed_folders: list[Path] = []

    @Slot()
    def run(self) -> None:
        package: UploadPackage | None = None
        log_file: Path | None = None
        code = 1
        try:
            log_file = new_log_file(f"packaged_upload_{self.profile.name}")
            self._log_file = log_file
            # Fail fast before source-sized archive work, then select a host
            # again after packaging because preparation can take hours.
            if self._available_profile() is None:
                code = 130 if self._cancelled.is_set() else 124
                return
            # Only the list of archived files is prepared here. Real archives
            # are built and sent batch by batch after the loose files.
            self._changed_folders = []
            package = prepare_upload_package(
                self.plan,
                cancel_event=self._cancelled,
                progress_callback=lambda message: self._emit(message + "\n"),
                build_archives=False,
                include_source_directory=self.include_source_directory,
                skip_changed=self._changed_folders,
            )
            if self._cancelled.is_set():
                code = 130
                return
            if self._changed_folders:
                self._emit(
                    f"{len(self._changed_folders):,} folder(s) changed since the scan and will be skipped; "
                    "they are listed at the end.\n"
                )
            self._emit(
                f"{'Would create' if self.dry_run else 'Will create'} {len(package.planned_groups):,} TAR archive(s), "
                "each with an inventory file.\n"
            )
            if self.dry_run:
                self._emit_planned_payload(package)
            else:
                self._receipt_plan = ReceiptPlan(
                    "upload",
                    *upload_receipt_roots(
                        self.plan.root,
                        self.remote_path,
                        files_from=False,
                        include_source_directory=self.include_source_directory,
                    ),
                )
                self._collector = self._receipt_plan.add_phase(
                    upload_local_base(
                        self.plan.root, files_from=False, include_source_directory=self.include_source_directory
                    ),
                    self.remote_path,
                )
            code = self._run_transfer_with_retries(
                lambda attempt_profile, is_retry: build_rsync_command(
                    attempt_profile,
                    self.plan.root,
                    remote_path=self.remote_path,
                    dry_run=self.dry_run,
                    ssh_path=self.ssh_path,
                    batch_mode=not bool(self.passphrase),
                    exclude_from=package.exclude_file,
                    direction="upload",
                    include_source_directory=self.include_source_directory,
                    resume_partial=is_retry and not self.dry_run,
                    itemize=not self.dry_run,
                ),
                log_file,
                "Uploading non-archived files and folders",
            )
            if self.dry_run:
                # No TAR is materialised for comparisons: the loose dry-run
                # covers retained files, while planned archive payload is logged.
                return
            loose_code = code
            if code not in self.CONTINUE_AFTER_LOOSE_CODES:
                return
            if code:
                self._emit(
                    f"Some loose files were not sent (rsync exit code {code}); "
                    "continuing with the archives. See the log above.\n"
                )
            payload_remote = (
                f"{self.remote_path.rstrip('/')}/{self.plan.root.name}"
                if self.include_source_directory
                else self.remote_path
            )
            code = self._upload_archives_in_batches(self.plan, payload_remote, log_file)
            if code == 0:
                code = loose_code
        except ArchiveCancelled:
            self._emit("Archive preparation was cancelled.\n")
            code = 130
        except Exception as exc:
            self._emit(f"Packaged upload failed: {exc}\n")
            code = 1
        finally:
            # Hash before cleanup: the receipt includes the temporary TAR files.
            if self._receipt_plan is not None and self._last_attempt_profile is not None and log_file is not None:
                self._write_receipt(code == 0, log_file)
            if package is not None:
                # Holds only the exclusion list; archive batches clean up after themselves.
                cleanup_error = package.cleanup()
                if cleanup_error:
                    self._emit(cleanup_error + "\n")
            if log_file is not None and log_file.exists():
                self.output.emit(f"\nLog saved to: {log_file}\n")
            self.finished.emit(code)

    def _upload_archives_in_batches(self, plan: UploadArchivePlan, payload_remote: str, log_file: Path) -> int:
        """Build and upload archives one batch at a time.

        Each batch is removed once it is on QRIScloud, so a large backup needs
        temporary space for one batch only, and a failure costs at most one
        batch. A folder that changes while the upload runs (a live share) is
        skipped and reported instead of stopping the whole upload.
        """
        skipped: list[str] = [str(folder) for folder in self._changed_folders]
        changed = set(self._changed_folders)
        plan = replace(plan, folders=[folder for folder in plan.folders if folder.folder not in changed])
        batch_plan = plan_archive_batches(plan)
        queue = list(batch_plan.batches)
        self._emit(
            f"Building {len(plan.groups):,} TAR archive(s) one batch at a time ({len(queue):,} batch(es)), "
            f"each deleted before the next. Temporary folder in {batch_plan.work_parent}. "
            "Source files will not be changed; press Stop to cancel.\n"
        )
        for group in batch_plan.too_big:
            skipped.append(f"{group.folder} ({group.archive_name})")
            self._emit(
                f"Skipped {group.folder / group.archive_name}: it needs about "
                f"{format_bytes(required_temp_bytes([group]))} of temporary space, but only "
                f"{format_bytes(max(0, batch_plan.room))} can be used in {batch_plan.work_parent}.\n"
            )
        batch_number = 0
        while queue:
            if self._cancelled.is_set():
                return 130
            batch = queue.pop(0)
            changed_now: list[Path] = []
            try:
                package = prepare_upload_package(
                    batch,
                    cancel_event=self._cancelled,
                    progress_callback=lambda message: self._emit(message + "\n"),
                    build_archives=True,
                    include_source_directory=self.include_source_directory,
                    work_parent=batch_plan.work_parent,
                    skip_changed=changed_now,
                )
            except ArchiveCancelled:
                raise
            except NotEnoughTempSpace as exc:
                # Someone else filled the drive since the plan was made.
                for folder in batch.folders:
                    skipped.append(str(folder.folder))
                self._emit(f"Skipped {len(batch.folders):,} folder(s): {exc}\n")
                continue
            except (RuntimeError, ValueError) as exc:
                # A file changed while its archive was being written: rare, so
                # retry this batch folder by folder and skip only that folder.
                if len(batch.folders) > 1:
                    queue[0:0] = split_batch_by_folder(batch)
                    continue
                skipped.append(str(batch.folders[0].folder))
                self._emit(f"Skipped {batch.folders[0].folder}: {exc}\n")
                continue
            for folder in changed_now:
                skipped.append(str(folder))
                self._emit(f"Skipped {folder}: it changed after the scan.\n")
            if package.archive_count == 0:
                cleanup_error = package.cleanup()
                if cleanup_error:
                    self._emit(cleanup_error + "\n")
                continue
            batch_number += 1
            keep = False
            try:
                self._emit(
                    f"Archive batch {batch_number}: uploading {package.archive_count:,} archive(s) "
                    f"({format_bytes(batch.archived_bytes)}).\n"
                )
                if self._receipt_plan is not None:
                    self._collector = self._receipt_plan.add_phase(package.payload_dir, payload_remote)
                code = self._run_transfer_with_retries(
                    lambda attempt_profile, is_retry: build_rsync_command(
                        attempt_profile,
                        package.payload_dir,
                        remote_path=payload_remote,
                        ssh_path=self.ssh_path,
                        batch_mode=not bool(self.passphrase),
                        direction="upload",
                        # TARs and inventories deliberately use source-derived mtimes.
                        # They must be sent even if an older payload happens to match
                        # their size and timestamp at the remote destination.
                        ignore_times=True,
                        resume_partial=is_retry,
                        itemize=True,
                    ),
                    log_file,
                    "Uploading TAR archives and inventories",
                )
                if self._receipt_plan is not None and self._collector is not None:
                    # Hash now: this batch's archives are deleted below.
                    hash_phase_now(self._receipt_plan, self._collector, emit=self._emit, cancel_event=self._cancelled)
                    self._collector = None
                if code != 0:
                    keep = code != 130
                    if keep:
                        self._emit(
                            f"Archive batch {batch_number} did not upload. Its archives were kept at: "
                            f"{package.work_dir}\nEarlier batches are on QRIScloud. Run Synchronise later "
                            "to send what is missing.\n"
                        )
                    return code
            finally:
                if not keep:
                    cleanup_error = package.cleanup()
                    if cleanup_error:
                        self._emit(cleanup_error + "\n")
        if skipped:
            self._emit(
                f"{len(skipped):,} folder(s) were not archived (changed during the upload, or not enough "
                "temporary space):\n"
                + "".join(f"  {folder}\n" for folder in skipped[:20])
                + "Run Synchronise later to upload them.\n"
            )
            return self.PARTIAL_TRANSFER_CODE
        self._emit("All archives were uploaded and the temporary files removed.\n")
        return 0

    def _available_profile(
        self,
        phase: str = "before packaged upload",
        avoid_host: str | None = None,
    ) -> Profile | None:
        self._host_checks_retryable = True
        hosts = fallback_hosts(self.profile)
        if avoid_host and len(hosts) > 1:
            hosts = [host for host in hosts if host != avoid_host] + [host for host in hosts if host == avoid_host]
        for host in hosts:
            if self._cancelled.is_set():
                self._emit(f"Upload cancelled {phase}.\n")
                return None
            attempt = profile_with_host(self.profile, host)
            self._emit(f"Checking {host} {phase}...\n")
            result = run_ssh_test(
                attempt,
                ssh_path=self.ssh_path,
                passphrase=self.passphrase,
                timeout=30,
                cancel_event=self._cancelled,
            )
            if self._cancelled.is_set():
                self._emit(f"Upload cancelled {phase}.\n")
                return None
            if result.returncode == 0:
                self._emit(f"Using {host} {phase}.\n")
                return attempt
            self._emit(f"{host} unavailable: exit code {result.returncode}\n")
            if result.output:
                self._emit(result.output + ("" if result.output.endswith("\n") else "\n"))
            # SSH uses 255 for both network and authentication failures. Only
            # retry known connection failures; leave ambiguous failures alone.
            transient = result.returncode == 124 or (
                result.returncode == 255
                and any(text in result.output.lower() for text in (
                    "connection timed out", "connection refused", "connection reset",
                    "network is unreachable", "no route to host",
                    "temporary failure in name resolution",
                ))
            )
            if not transient:
                self._host_checks_retryable = False
        self._emit(f"No QRIScloud SSH host was available {phase}.\n")
        return None

    def _run_transfer_with_retries(self, command_builder, log_file: Path, message: str) -> int:
        last_code: int | None = None
        previous_host: str | None = None
        attempt_count = len(self.RETRY_DELAYS_SECONDS) + 1
        for attempt_index in range(attempt_count):
            if self._cancelled.is_set():
                return 130
            phase = (
                f"before {message.lower()}"
                if attempt_index == 0
                else f"before retry {attempt_index + 1} of {attempt_count} for {message.lower()}"
            )
            attempt_profile = self._available_profile(phase=phase, avoid_host=previous_host)
            if attempt_profile is None:
                if self._cancelled.is_set():
                    return 130
                if last_code is None or not self._host_checks_retryable:
                    return last_code if last_code is not None else 124
            else:
                if attempt_index:
                    self._emit(
                        f"Retrying with {attempt_profile.host} "
                        f"(attempt {attempt_index + 1} of {attempt_count}).\n"
                    )
                last_code = self._run_command(
                    command_builder(attempt_profile, attempt_index > 0),
                    log_file,
                    message,
                )
                previous_host = attempt_profile.host
                self._last_attempt_profile = attempt_profile
            if last_code == 0 or last_code == 130 or self._cancelled.is_set():
                return 130 if self._cancelled.is_set() else last_code
            if last_code not in self.RETRYABLE_RSYNC_EXIT_CODES:
                return last_code
            if attempt_index == attempt_count - 1:
                self._emit(
                    f"{message} failed with recoverable rsync exit code {last_code}, "
                    "but all automatic retry attempts have been used.\n"
                )
                return last_code
            delay = self.RETRY_DELAYS_SECONDS[attempt_index]
            self._emit(
                f"Connection interrupted with rsync exit code {last_code}. "
                f"The partial transfer was kept; retrying in {delay} seconds.\n"
            )
            if self._cancelled.wait(delay):
                return 130
        return last_code if last_code is not None else 1

    def _emit_planned_payload(self, package: UploadPackage) -> None:
        """Describe dry-run TAR/inventory output without creating archive data."""
        for group in package.planned_groups or tuple(self.plan.groups):
            relative = f"{group.relative_folder}/" if group.relative_folder else ""
            self._emit(
                f"Would create and upload {relative}{group.archive_name} "
                f"from {group.member_count:,} files ({group.member_bytes:,} member bytes), "
                f"with {relative}{group.inventory_name}.\n"
            )

    def _run_command(self, command: list[str], log_file: Path, message: str) -> int:
        if self._cancelled.is_set():
            return 130
        self._emit(message + "...\n")
        collector = self._collector

        def on_output(text: str) -> None:
            if collector is not None:
                collector.feed(text)
            self.output.emit(text)

        return self.runner.run(
            command,
            log_file,
            on_output,
            passphrase=self.passphrase,
            ssh_path=self.ssh_path,
            cancel_event=self._cancelled,
        )

    def _write_receipt(self, completed: bool, log_file: Path) -> None:
        if self._receipt_plan is None or self._last_attempt_profile is None:
            return
        self._collector = None
        record_transfer_receipt(
            self._receipt_plan,
            self._last_attempt_profile,
            completed=completed,
            emit=self._emit,
            run_command=lambda command: self._run_command(command, log_file, "Uploading transfer receipt"),
            ssh_path=self.ssh_path,
            batch_mode=not bool(self.passphrase),
            cancel_event=self._cancelled,
        )

    def _emit(self, message: str) -> None:
        if self._log_file is not None:
            try:
                self._log_file.parent.mkdir(parents=True, exist_ok=True)
                with self._log_file.open("a", encoding="utf-8", errors="replace") as log:
                    log.write(message)
            except OSError:
                pass
        self.output.emit(message)

    @Slot()
    def cancel(self) -> None:
        self._cancelled.set()
        self.runner.cancel()


class SyncUploadWorker(PackagedUploadWorker):
    """Upload the files ticked in the Synchronise window.

    Loose files go up as an exact ``--files-from`` list. Ticked archive
    folders are then packed into ``.tar`` archives batch by batch, exactly as
    a packaged upload would pack them, and only those archives are sent. It
    reuses the packaged upload's host checks, automatic retries and receipts.
    """

    def __init__(
        self,
        profile: Profile,
        local_root: Path,
        remote_path: str,
        loose_list: Path | None,
        archive_plan: UploadArchivePlan | None,
        *,
        passphrase: str = "",
        ssh_path: str | None = None,
    ) -> None:
        super().__init__(
            profile,
            archive_plan,
            remote_path,
            dry_run=False,
            passphrase=passphrase,
            ssh_path=ssh_path,
        )
        self.local_root = local_root
        self.loose_list = loose_list
        self.archive_plan = archive_plan

    @Slot()
    def run(self) -> None:
        log_file: Path | None = None
        code = 1
        try:
            log_file = new_log_file(f"sync_upload_{self.profile.name}")
            self._log_file = log_file
            if self._available_profile() is None:
                code = 130 if self._cancelled.is_set() else 124
                return
            self._receipt_plan = ReceiptPlan("upload", self.local_root, self.remote_path)
            code = 0
            if self.loose_list is not None:
                self._collector = self._receipt_plan.add_phase(self.local_root, self.remote_path)
                code = self._run_transfer_with_retries(
                    lambda attempt_profile, is_retry: build_rsync_command(
                        attempt_profile,
                        self.local_root,
                        remote_path=self.remote_path,
                        ssh_path=self.ssh_path,
                        batch_mode=not bool(self.passphrase),
                        files_from=self.loose_list,
                        direction="upload",
                        resume_partial=is_retry,
                        itemize=True,
                    ),
                    log_file,
                    "Uploading ticked files",
                )
            if self.archive_plan is None or not self.archive_plan.folders:
                return
            loose_code = code
            if code not in self.CONTINUE_AFTER_LOOSE_CODES:
                return
            code = self._upload_archives_in_batches(self.archive_plan, self.remote_path, log_file)
            if code == 0:
                code = loose_code
        except ArchiveCancelled:
            self._emit("Archive preparation was cancelled.\n")
            code = 130
        except Exception as exc:
            self._emit(f"Synchronise upload failed: {exc}\n")
            code = 1
        finally:
            if self._receipt_plan is not None and self._last_attempt_profile is not None and log_file is not None:
                self._write_receipt(code == 0, log_file)
            if log_file is not None and log_file.exists():
                self.output.emit(f"\nLog saved to: {log_file}\n")
            self.finished.emit(code)


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


class LocalMoveWorker(QObject):
    output = Signal(str)
    finished = Signal(int)

    def __init__(self, paths: list[Path], destination: Path, allowed_root: Path) -> None:
        super().__init__()
        self.paths = list(paths)
        self.destination = destination
        self.allowed_root = allowed_root

    @Slot()
    def run(self) -> None:
        try:
            moved = move_local_paths(self.paths, self.destination, self.allowed_root)
            for source, destination in moved:
                self.output.emit(f"Moved local item: {source} -> {destination}\n")
            self.finished.emit(0)
        except Exception as exc:
            self.output.emit(f"Local move failed: {exc}\n")
            self.finished.emit(1)

    @Slot()
    def cancel(self) -> None:
        return


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
        self._cancelled = threading.Event()

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
            if self._cancelled.is_set():
                self._write_and_emit(log_file, "Recall was cancelled.\n")
                return 130
            attempt_profile = profile_with_host(self.profile, host)
            self._write_and_emit(log_file, f"Checking {host} before recall...\n")
            result = run_ssh_test(
                attempt_profile,
                ssh_path=self.ssh_path,
                passphrase=self.passphrase,
                timeout=30,
                cancel_event=self._cancelled,
            )
            if self._cancelled.is_set():
                self._write_and_emit(log_file, "Recall was cancelled.\n")
                return 130
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
            if self._cancelled.is_set():
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
                if self._cancelled.is_set():
                    self._write_and_emit(log_file, "Recall was cancelled.\n")
                    return 130
                attempt_profile = profile_with_host(profile, host)
                if attempt > 1 or host != profile.host:
                    self._write_and_emit(
                        log_file,
                        f"[{batch_index}/{batch_count}] reconnect attempt {attempt}/{self.max_reconnect_attempts} using {host}...\n",
                    )
                code = self._run_recall_command(attempt_profile, chunk, log_file, batch_index, batch_count, creationflags)
                if code == 0 or self._cancelled.is_set():
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
                encoding="utf-8",
                errors="replace",
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
        while not self._cancelled.is_set() and time.monotonic() < deadline:
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
            elif item and not item.lstrip().startswith("**"):
                # Skip OpenSSH's multi-line "** WARNING ... post-quantum" notice.
                self._write_and_emit(log_file, item)

            if self._cancelled.is_set():
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
        self._cancelled.set()
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
            local_scan = scan_local_for_sync(
                self.local_folder,
                cancel_event=self._cancelled,
                progress_callback=self._report_local_progress,
            )
            if self._cancelled.is_set() or local_scan.cancelled:
                self._finish(None, "Sync comparison was cancelled.")
                return
            self.output.emit(
                f"Local folder: {len(local_scan.records):,} files"
                + (f", {len(local_scan.archives):,} folder(s) packed into archives on upload" if local_scan.archives else "")
                + ".\n"
            )

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
                        encoding="utf-8",
                        errors="replace",
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
                    if collector.malformed_line_count:
                        examples = "\n".join(f"    {line}" for line in collector.malformed_examples)
                        self._finish(
                            None,
                            f"Remote manifest contained {collector.malformed_line_count:,} malformed line(s); "
                            "comparison was stopped to avoid an incomplete selection.\n\n"
                            f"Unexpected output from QRIScloud:\n{examples}",
                        )
                        return
                    self.output.emit(f"Remote manifest succeeded using {host}.\n")
                    remote_manifest = collector.records
                    break
                self.output.emit(f"Remote manifest failed on {host} with exit code {returncode}.\n")
            if returncode != 0 or remote_manifest is None:
                self._finish(None, last_error or f"Remote manifest failed with exit code {returncode}.")
                return

            self.output.emit(f"QRIScloud folder: {len(remote_manifest):,} files.\n")
            plan = build_sync_plan(local_scan.records, remote_manifest, local_scan.archives)
            if self._cancelled.is_set():
                self._finish(None, "Sync comparison was cancelled.")
                return
            self.output.emit(
                f"Comparison finished: {len(plan.items):,} difference(s), "
                f"{plan.identical_files + plan.identical_archives:,} item(s) already the same.\n"
            )
            self._finish_success(
                None,
                {
                    "plan": plan,
                    "upload_plan": local_scan.plan,
                    "local_folder": self.local_folder,
                    "remote_path": self.remote_path,
                },
            )
        except Exception as exc:
            self._finish(None, f"Sync comparison failed: {exc}")
        finally:
            with self._process_lock:
                self._process = None

    def _report_local_progress(self, progress: FolderScan) -> None:
        if progress.file_count - self._last_local_progress >= 1000:
            self._last_local_progress = progress.file_count
            self.output.emit(
                f"Local folder: scanned {progress.file_count:,} files in "
                f"{progress.directory_count:,} folders...\n"
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

    def _finish_success(self, file_list: Path | None, result: object) -> None:
        cancelled = False
        with self._finish_lock:
            if self._did_finish:
                return
            if self._cancelled.is_set():
                cancelled = True
            self._did_finish = True
        if cancelled:
            if file_list is not None:
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
        self.resize(1200, 800)
        self.setMinimumSize(900, 650)

        profile_load = load_profiles_result()
        self.profiles = profile_load.profiles
        self._profile_load_diagnostics = profile_load.diagnostics
        self.current_thread: QThread | None = None
        self.current_worker: (
            CommandWorker
            | SshTestWorker
            | RecallMediciWorker
            | FolderScanWorker
            | UploadAnalysisWorker
            | PackagedUploadWorker
            | LocalDeleteWorker
            | LocalMoveWorker
            | None
        ) = None
        self.current_label = ""
        self.current_finished_handled = False
        self.current_refresh_local = False
        self.current_refresh_remote = False
        self.current_remote_cache_invalidations: list[tuple[Profile, str, bool]] = []
        self.current_after_finish = None
        self.current_cleanup = None
        self.current_exit_code: int | None = None
        self._current_operation_id = 0
        self.remote_thread: QThread | None = None
        self.remote_worker: RemoteListWorker | None = None
        self._remote_request_id = 0
        self._pending_remote_path: str | None = None
        self._pending_remote_force = False
        self.remote_directory_cache = RemoteDirectoryCache()
        self.compare_thread: QThread | None = None
        self.compare_worker: SyncCompareWorker | None = None
        self.remote_entries: list[RemoteEntry] = []
        self.remote_entries_path = ""
        # SSH key passphrases for this app session only, keyed by key file, so
        # switching between profiles that use the same key does not ask again.
        # Never saved to disk; forgotten when the key is rejected.
        self._session_passphrases: dict[str, str] = {}
        self._close_after_stop = False
        self._cut_clipboard: tuple[str, list[str]] | None = None

        self.profile_combo = QComboBox()
        self.local_folder_edit = QLineEdit()
        self.remote_path_edit = QLineEdit()
        self.detected_ssh = detect_ssh()
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Local ↔ QRIScloud", "local")
        self.mode_combo.addItem("Collection ↔ Collection", "collections")
        self.mode_combo.setToolTip(
            "Collection ↔ Collection replaces the local pane with a second QRIScloud browser. "
            "Copies run on the QRIScloud server, so data does not pass through this PC."
        )
        self.remote_collection_combo = QComboBox()
        self.remote_collection_combo.setToolTip(
            "Collections from your saved profiles. Picking one opens it here; it does not change the profile."
        )
        self.collection_pane = CollectionPane(
            self.current_profile,
            self._get_session_passphrase,
            self.detected_ssh,
            on_listing_error=self._forget_passphrase_if_rejected,
        )
        self.left_stack = QStackedWidget()
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
        self.local_tree = LocalMoveTreeView()
        self.local_tree.setModel(self.local_model)
        self.local_tree.setSortingEnabled(True)
        self.local_tree.sortByColumn(0, Qt.AscendingOrder)
        self.local_tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.local_tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.local_tree.setToolTip("Drag selected local items onto a local folder to move them.")
        self.local_tree.doubleClicked.connect(self._local_double_clicked)

        self.remote_table = RemoteMoveTree()
        self.remote_table.setToolTip(
            "Click a folder's arrow to look inside it; double-click to open it. "
            "Drag selected items onto a folder to move them within this collection."
        )
        self.remote_table.itemDoubleClicked.connect(self._remote_double_clicked)
        self.remote_folder_loader = RemoteFolderLoader(
            self.remote_table,
            self.current_profile,
            self._get_session_passphrase,
            lambda: self.detected_ssh,
            self.remote_directory_cache,
            on_error=self._forget_passphrase_if_rejected,
            parent=self,
        )
        self.remote_status_label = QLabel("Remote browser not loaded.")

        self.ssh_button = QPushButton("Test SSH")
        self.local_up_button = QPushButton("Up")
        self.local_refresh_button = QPushButton("Refresh")
        self.local_new_folder_button = QPushButton("New folder")
        self.local_rename_button = QPushButton("Rename")
        self.local_move_button = QPushButton("Move to…")
        self.local_delete_button = QPushButton("Delete")
        self.remote_load_button = QPushButton("Load")
        self.remote_up_button = QPushButton("Up")
        self.remote_refresh_button = QPushButton("Refresh")
        self.remote_new_folder_button = QPushButton("New folder")
        self.remote_rename_button = QPushButton("Rename")
        self.remote_move_button = QPushButton("Move to…")
        self.remote_delete_button = QPushButton("Delete")
        self.transfer_scope_combo = QComboBox()
        self.transfer_scope_combo.addItem("Current folder", "folder")
        self.transfer_scope_combo.addItem("Selected items", "selected")
        self.dry_run_button = QPushButton("Compare upload")
        self.download_dry_run_button = QPushButton("Compare download")
        self.build_selection_button = QPushButton("Synchronise…")
        self.upload_button = QPushButton("Upload folder")
        self.recall_button = QPushButton("Recall from tape")
        self.tape_status_button = QPushButton("Check tape status")
        self.recall_jobs_button = QPushButton("Recall jobs…")
        self.download_button = QPushButton("Download folder")
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.selection_status_label = QLabel("Current folder")

        self.dry_run_button.setToolTip("Compare upload changes only; does not transfer or delete files.")
        self.download_dry_run_button.setToolTip("Compare download changes only; does not transfer or delete files.")
        self.build_selection_button.setToolTip(
            "Compare the local and QRIScloud folders side by side, then tick what to upload or download."
        )
        self.recall_button.setToolTip("Request retrieval from Medici tape; this does not download files.")
        self.tape_status_button.setToolTip(
            "Check whether the selected QRIScloud items (or the open folder) are on disk or on tape. "
            "Reads no file data."
        )

        self._build_ui()
        self._set_local_root(str(Path.home()))
        self._load_profile_combo()
        self._update_status()
        self._update_transfer_scope_ui()
        for diagnostic in self._profile_load_diagnostics:
            self._append_log(f"Profile settings: {diagnostic}\n")

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
        self.setup_guide_button = QPushButton("First-time setup")
        profile_layout.addWidget(new_button, 0, 2)
        profile_layout.addWidget(edit_button, 0, 3)
        profile_layout.addWidget(delete_button, 0, 4)
        profile_layout.addWidget(self._profiles_menu_button(), 0, 5)
        profile_layout.addWidget(self.setup_guide_button, 0, 6)
        # Test SSH checks this profile's connection, so it lives with the profile.
        profile_layout.addWidget(self.ssh_button, 0, 7)
        profile_layout.addWidget(self.status_label, 1, 0, 1, 8)
        profile_layout.setColumnStretch(1, 1)
        layout.addWidget(self.profile_box)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Mode"))
        mode_row.addWidget(self.mode_combo)
        self.mode_hint_label = QLabel()
        mode_row.addWidget(self.mode_hint_label, stretch=1)
        layout.addLayout(mode_row)

        browser = QSplitter()
        self.left_stack.addWidget(self._local_panel())
        self.left_stack.addWidget(self.collection_pane)
        # Each side's transfer buttons sit right under it, like WinSCP: upload under
        # the local pane, download and tape under the QRIScloud pane. They live
        # outside the panes so they stay visible when the left pane is swapped
        # for the second-collection browser.
        browser.addWidget(self._pane_column(self.left_stack, self._upload_row()))
        browser.addWidget(self._pane_column(self._remote_panel(), self._download_row()))
        browser.setSizes([480, 480])
        layout.addWidget(browser, stretch=2)

        layout.addLayout(self._bottom_bar())

        progress_row = QHBoxLayout()
        progress_row.addWidget(QLabel("Progress"))
        progress_row.addWidget(self.transfer_progress, stretch=1)
        progress_row.addWidget(self.transfer_status_label)
        # Stop sits with the progress it stops.
        self.stop_button.setMinimumWidth(90)
        progress_row.addWidget(self.stop_button)
        layout.addLayout(progress_row)

        layout.addWidget(QLabel("Log"))
        layout.addWidget(self.log_output, stretch=1)

        self.profile_combo.currentIndexChanged.connect(self._profile_changed)
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        self.remote_collection_combo.activated.connect(self._remote_collection_picked)
        self.collection_pane.pathChanged.connect(self._update_collection_exclusions)
        self.remote_path_edit.editingFinished.connect(self._sync_remote_collection_combo)
        self.collection_pane.busyChanged.connect(self._collection_pane_busy_changed)
        self.collection_pane.selectionChanged.connect(self._update_transfer_scope_ui)
        new_button.clicked.connect(self._new_profile)
        edit_button.clicked.connect(self._edit_profile)
        delete_button.clicked.connect(self._delete_profile)
        self.setup_guide_button.clicked.connect(self._open_setup_guide)
        self.ssh_button.clicked.connect(self._start_ssh_test)
        self.local_up_button.clicked.connect(self._local_go_up)
        self.local_refresh_button.clicked.connect(self._refresh_local_tree)
        self.local_new_folder_button.clicked.connect(self._local_new_folder)
        self.local_rename_button.clicked.connect(self._local_rename)
        self.local_move_button.clicked.connect(self._local_move_to)
        self.local_delete_button.clicked.connect(self._local_delete_selected)
        self.remote_load_button.clicked.connect(lambda: self._refresh_remote_table(force=True))
        self.remote_up_button.clicked.connect(self._remote_go_up)
        self.remote_refresh_button.clicked.connect(lambda: self._refresh_remote_table(force=True))
        self.remote_new_folder_button.clicked.connect(lambda: self._remote_new_folder("right"))
        self.remote_rename_button.clicked.connect(lambda: self._remote_rename("right"))
        self.remote_move_button.clicked.connect(lambda: self._remote_move_to("right"))
        self.remote_delete_button.clicked.connect(lambda: self._remote_delete_selected("right"))
        pane = self.collection_pane
        pane.new_folder_button.clicked.connect(lambda: self._remote_new_folder("left"))
        pane.rename_button.clicked.connect(lambda: self._remote_rename("left"))
        pane.move_button.clicked.connect(lambda: self._remote_move_to("left"))
        pane.delete_button.clicked.connect(lambda: self._remote_delete_selected("left"))
        pane.table.moveDropped.connect(lambda paths, target: self._move_remote_items_by_drop(paths, target, "left"))
        self.dry_run_button.clicked.connect(lambda: self._start_rsync(dry_run=True, direction="upload"))
        self.download_dry_run_button.clicked.connect(lambda: self._start_rsync(dry_run=True, direction="download"))
        self.build_selection_button.clicked.connect(self._build_sync_selection)
        self.upload_button.clicked.connect(lambda: self._start_rsync(dry_run=False, direction="upload"))
        self.recall_button.clicked.connect(self._recall_medici)
        self.tape_status_button.clicked.connect(self._check_tape_status)
        self.recall_jobs_button.clicked.connect(self._show_recall_jobs)
        self.recall_jobs_button.setToolTip("Follow tape recall jobs submitted to Bunya from this app.")
        self.download_button.clicked.connect(lambda: self._start_rsync(dry_run=False, direction="download"))
        self.stop_button.clicked.connect(self._cancel_current)
        self.transfer_scope_combo.currentIndexChanged.connect(self._update_transfer_scope_ui)
        self.local_tree.selectionModel().selectionChanged.connect(self._update_transfer_scope_ui)
        self.local_tree.moveDropped.connect(self._move_local_items_by_drop)
        self.remote_table.itemSelectionChanged.connect(self._update_transfer_scope_ui)
        self.remote_table.moveDropped.connect(self._move_remote_items_by_drop)
        self._install_pane_actions()

    def _pane_column(self, pane: QWidget, actions: QHBoxLayout) -> QWidget:
        column = QWidget()
        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(pane, stretch=1)
        layout.addLayout(actions)
        return column

    def _emphasise(self, button: QPushButton, icon: QStyle.StandardPixmap) -> None:
        button.setIcon(self.style().standardIcon(icon))
        font = button.font()
        font.setBold(True)
        button.setFont(font)

    def _upload_row(self) -> QHBoxLayout:
        self._emphasise(self.upload_button, QStyle.SP_ArrowRight)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(self.dry_run_button)
        row.addWidget(self.upload_button)
        return row

    def _download_row(self) -> QHBoxLayout:
        self._emphasise(self.download_button, QStyle.SP_ArrowLeft)
        row = QHBoxLayout()
        row.addWidget(self.download_button)
        row.addWidget(self.download_dry_run_button)
        row.addStretch()
        row.addWidget(self._tape_menu_button())
        return row

    def _tape_menu_button(self) -> QToolButton:
        """One menu for the three tape actions. The menu items run the existing
        buttons (kept hidden), so they follow the same enabled rules."""
        self.tape_menu_button = QToolButton()
        self.tape_menu_button.setText("Tape status and recall")
        self.tape_menu_button.setIcon(self.style().standardIcon(QStyle.SP_DriveHDIcon))
        self.tape_menu_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.tape_menu_button.setPopupMode(QToolButton.InstantPopup)
        self.tape_menu_button.setToolTip("Check whether QRIScloud files are on tape, and recall them.")
        menu = QMenu(self.tape_menu_button)
        self.tape_menu_actions = []
        for text, button in (
            ("Check tape status…", self.tape_status_button),
            ("Recall files in this folder now", self.recall_button),
            ("Recall jobs on Bunya…", self.recall_jobs_button),
        ):
            action = menu.addAction(text)
            action.setToolTip(button.toolTip())
            action.triggered.connect(button.click)
            self.tape_menu_actions.append((action, button))
            button.setParent(self)
            button.hide()
        menu.aboutToShow.connect(self._sync_tape_menu)
        self.tape_menu_button.setMenu(menu)
        return self.tape_menu_button

    def _sync_tape_menu(self) -> None:
        for action, button in self.tape_menu_actions:
            action.setEnabled(button.isEnabled())

    def _bottom_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Transfer scope"))
        bar.addWidget(self.transfer_scope_combo)
        bar.addWidget(self.selection_status_label)
        bar.addStretch()
        self.build_selection_button.setIcon(self.style().standardIcon(QStyle.SP_BrowserReload))
        bar.addWidget(self.build_selection_button)
        return bar

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

    def _folder_row(self) -> QVBoxLayout:
        rows = QVBoxLayout()
        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("Path"))
        path_row.addWidget(self.local_folder_edit, stretch=1)
        button = QPushButton("Browse")
        button.clicked.connect(self._browse_local_folder)
        path_row.addWidget(button)
        action_row = QHBoxLayout()
        action_row.addWidget(self.local_up_button)
        action_row.addWidget(self.local_refresh_button)
        action_row.addWidget(self.local_new_folder_button)
        action_row.addWidget(self.local_rename_button)
        action_row.addWidget(self.local_move_button)
        action_row.addWidget(self.local_delete_button)
        action_row.addStretch()
        rows.addLayout(path_row)
        rows.addLayout(action_row)
        return rows

    def _remote_path_row(self) -> QVBoxLayout:
        rows = QVBoxLayout()
        collection_row = QHBoxLayout()
        collection_row.addWidget(QLabel("Collection"))
        collection_row.addWidget(self.remote_collection_combo, stretch=1)
        rows.addLayout(collection_row)
        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("Path"))
        path_row.addWidget(self.remote_path_edit, stretch=1)
        path_row.addWidget(self.remote_load_button)
        action_row = QHBoxLayout()
        action_row.addWidget(self.remote_up_button)
        action_row.addWidget(self.remote_refresh_button)
        action_row.addWidget(self.remote_new_folder_button)
        action_row.addWidget(self.remote_rename_button)
        action_row.addWidget(self.remote_move_button)
        action_row.addWidget(self.remote_delete_button)
        action_row.addStretch()
        rows.addLayout(path_row)
        rows.addLayout(action_row)
        return rows

    def _load_profile_combo(self, selected_name: str | None = None) -> None:
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        for profile in self.profiles:
            self.profile_combo.addItem(profile.name)
        if selected_name:
            index = self.profile_combo.findText(selected_name)
            if index >= 0:
                self.profile_combo.setCurrentIndex(index)
        self.profile_combo.blockSignals(False)
        self._profile_changed()

    def _profile_changed(self) -> None:
        if hasattr(self, "remote_folder_loader"):
            self.remote_folder_loader.cancel()
        self._pending_remote_path = None
        self._pending_remote_force = False
        self._cut_clipboard = None
        profile = self.current_profile()
        if profile:
            self.remote_path_edit.setText(profile.remote_path)
        self.collection_pane.cancel()
        self.collection_pane.clear()
        self._refresh_collection_choices()
        if self._collection_mode():
            self._move_left_pane_off_right_collection()
            self._update_collection_exclusions()
        self._update_status()

    def current_profile(self) -> Profile | None:
        index = self.profile_combo.currentIndex()
        if 0 <= index < len(self.profiles):
            return self.profiles[index].normalized()
        return None

    def _new_profile(self) -> None:
        self._create_profile()

    def _create_profile(self, initial_profile: Profile | None = None) -> None:
        dialog = ProfileDialog(initial_profile, self, suggested_key_path=suggest_ssh_key_path(self.profiles))
        if dialog.exec() == ProfileDialog.Accepted:
            profile = dialog.profile()
            if any(existing.name == profile.name for existing in self.profiles):
                self._show_errors([f"A profile named '{profile.name}' already exists. Choose a unique name."])
                return
            candidate = upsert_profile(self.profiles, profile)
            if self._persist_profiles(candidate):
                self.profiles = candidate
                self._load_profile_combo(profile.name)

    def _open_setup_guide(self) -> None:
        dialog = SetupGuideDialog(self)
        if dialog.exec() == SetupGuideDialog.Accepted and dialog.create_profile_after_finish:
            self._create_profile(dialog.profile_template())

    def _edit_profile(self) -> None:
        profile = self.current_profile()
        if not profile:
            return
        dialog = ProfileDialog(profile, self, suggested_key_path=suggest_ssh_key_path(self.profiles))
        if dialog.exec() == ProfileDialog.Accepted:
            updated = dialog.profile()
            index = self.profile_combo.currentIndex()
            if any(position != index and existing.name == updated.name for position, existing in enumerate(self.profiles)):
                self._show_errors([f"A profile named '{updated.name}' already exists. Choose a unique name."])
                return
            candidate = list(self.profiles)
            candidate[index] = updated
            if self._persist_profiles(candidate):
                self.profiles = candidate
                self._load_profile_combo(updated.name)

    def _delete_profile(self) -> None:
        index = self.profile_combo.currentIndex()
        if index < 0:
            return
        if len(self.profiles) == 1:
            QMessageBox.information(self, "Profile required", "At least one profile is required.")
            return
        removed = self.profiles[index]
        if QMessageBox.question(self, "Delete profile", f"Delete profile '{removed.name}'?") == QMessageBox.Yes:
            candidate = [profile for position, profile in enumerate(self.profiles) if position != index]
            if self._persist_profiles(candidate):
                self.profiles = candidate
                self._load_profile_combo(candidate[min(index, len(candidate) - 1)].name)

    def _profiles_menu_button(self) -> QToolButton:
        button = QToolButton()
        button.setText("Profiles")
        button.setPopupMode(QToolButton.InstantPopup)
        button.setToolButtonStyle(Qt.ToolButtonTextOnly)
        button.setMinimumHeight(self.ssh_button.sizeHint().height())
        button.setMinimumWidth(90)
        button.setToolTip("Save, export or import profiles (for example to use them on another computer).")
        menu = QMenu(button)
        menu.addAction("Save profiles", self._save_profiles)
        menu.addSeparator()
        menu.addAction("Export profiles…", self._export_profiles)
        menu.addAction("Import profiles…", self._import_profiles)
        button.setMenu(menu)
        self.profiles_menu_button = button
        return button

    def _ask_export_scope(self) -> list[Profile] | None:
        current = self.current_profile()
        if current is None or len(self.profiles) == 1:
            return list(self.profiles)
        box = QMessageBox(self)
        box.setWindowTitle("Export profiles")
        box.setText("Which profiles do you want to export?")
        all_button = box.addButton(f"All profiles ({len(self.profiles)})", QMessageBox.AcceptRole)
        one_button = box.addButton(f"Only '{current.name}'", QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Cancel)
        box.exec()
        if box.clickedButton() is all_button:
            return list(self.profiles)
        if box.clickedButton() is one_button:
            return [current]
        return None

    def _export_profiles(self) -> None:
        chosen = self._ask_export_scope()
        if not chosen:
            return
        start = str(Path.home() / "Documents" / "qris_rsync_profiles.json")
        path, _filter = QFileDialog.getSaveFileName(self, "Export profiles", start, "Profiles (*.json)")
        if not path:
            return
        try:
            export_profiles(chosen, path)
        except OSError as exc:
            QMessageBox.warning(self, "Export failed", str(exc))
            return
        QMessageBox.information(
            self,
            "Profiles exported",
            f"Exported {len(chosen)} profile(s) to:\n{path}\n\n"
            "The file contains no passwords or passphrases. To use the profiles on another computer, "
            "choose Profiles → Import profiles… there. The SSH key itself is not included: copy it "
            "privately, or create and register a new key on that computer.",
        )

    def _import_profiles(self) -> None:
        start = str(Path.home() / "Documents")
        path, _filter = QFileDialog.getOpenFileName(self, "Import profiles", start, "Profiles (*.json);;All files (*)")
        if not path:
            return
        try:
            incoming, problems = read_profile_file(path)
        except ValueError as exc:
            QMessageBox.warning(self, "Import failed", str(exc))
            return
        if not incoming:
            QMessageBox.warning(self, "Import failed", "No profiles found.\n" + "\n".join(problems[:5]))
            return
        adapted = [adapt_profile_to_this_computer(profile) for profile in incoming]
        incoming = [profile for profile, _note in adapted]
        notes = sorted({note for _profile, note in adapted if note})
        existing_names = {profile.name for profile in self.profiles}
        clashes = [profile.name for profile in incoming if profile.name in existing_names]
        replace = False
        if clashes:
            box = QMessageBox(self)
            box.setWindowTitle("Import profiles")
            box.setText(
                f"{len(clashes)} imported profile(s) have the same name as one you already have, "
                f"for example '{clashes[0]}'."
            )
            replace_button = box.addButton("Replace mine", QMessageBox.AcceptRole)
            keep_button = box.addButton("Keep mine", QMessageBox.AcceptRole)
            box.addButton(QMessageBox.Cancel)
            box.exec()
            if box.clickedButton() is replace_button:
                replace = True
            elif box.clickedButton() is not keep_button:
                return
        # A brand-new install has only the placeholder profile; drop it.
        existing = [profile for profile in self.profiles if not self._is_placeholder_profile(profile)]
        result = merge_imported_profiles(existing, incoming, replace)
        if not self._persist_profiles(result.profiles):
            return
        self.profiles = result.profiles
        current = self.current_profile()
        names = [profile.name for profile in self.profiles]
        self._load_profile_combo(current.name if current and current.name in names else names[0])
        lines = [f"Added {len(result.added)} profile(s)."]
        if result.replaced:
            lines.append(f"Replaced {len(result.replaced)}.")
        if result.skipped:
            lines.append(f"Kept your own {len(result.skipped)} with the same name.")
        lines.extend(problems[:5])
        if notes:
            lines.append("")
            lines.extend(notes)
            lines.append("Copy that SSH key to this computer, or set another key with Edit.")
        QMessageBox.information(self, "Profiles imported", "\n".join(lines))

    @staticmethod
    def _is_placeholder_profile(profile: Profile) -> bool:
        return profile.name == "Default QRIScloud" and not profile.username

    def _save_profiles(self) -> None:
        if self._persist_profiles(self.profiles):
            self._append_log("Profiles saved.\n")

    def _persist_profiles(self, candidate: list[Profile]) -> bool:
        try:
            save_profiles(candidate)
        except OSError as exc:
            QMessageBox.critical(
                self,
                "Could not save profiles",
                f"Profile changes were not saved. Check that the settings folder is writable and has free space.\n\n{exc}",
            )
            return False
        return True

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
        if self.local_folder_edit.text() != str(local):
            # Otherwise the old, now invisible, selection would still drive
            # Delete, Rename, Cut and the Paste target.
            self.local_tree.clearSelection()
        self.local_folder_edit.setText(str(local))
        self.local_tree.setRootIndex(self.local_model.setRootPath(str(local)))

    def _refresh_local_tree(self) -> None:
        self._set_local_root(self.local_folder_edit.text() or str(Path.home()))

    def _local_go_up(self) -> None:
        current = Path(self.local_folder_edit.text() or str(Path.home())).expanduser()
        parent = current.parent if current.parent != current else current
        self._set_local_root(str(parent))

    def _install_pane_actions(self) -> None:
        """Right-click menus and WinSCP-style keys for each pane.

        Every action is gated on the same state as its button, so a shortcut
        never runs while that button is disabled (for example mid-transfer).
        """
        local, right, pane = "local", "right", self.collection_pane

        def selected_local_dir() -> Path | None:
            paths = self._selected_local_paths()
            return paths[0] if len(paths) == 1 and paths[0].is_dir() else None

        def selected_remote_dir(side: str):
            selected = self._remote_pane_state(side)[3]
            return selected[0] if len(selected) == 1 and selected[0].is_dir else None

        def open_local() -> None:
            folder = selected_local_dir()
            if folder is not None:
                self._set_local_root(str(folder))

        def open_remote(side: str) -> None:
            folder = selected_remote_dir(side)
            if folder is None:
                return
            if side == "left":
                pane.navigate(folder.path)
            else:
                self._navigate_remote_path(folder.path)

        def copy_paths(side: str) -> None:
            if side == local:
                paths = [str(path) for path in self._selected_local_paths()] or [self.local_folder_edit.text()]
            else:
                current, _loaded, _entries, selected = self._remote_pane_state(side)
                paths = [entry.path for entry in selected] or [current]
            QApplication.clipboard().setText("\n".join(paths))
            self._append_log(f"Copied {len(paths):,} path(s) to the clipboard.\n")

        def has_local_selection() -> bool:
            return bool(self._selected_local_paths())

        def has_remote_selection(side: str) -> bool:
            return bool(self._remote_pane_state(side)[3])

        install_pane_actions(
            self.local_tree,
            [
                PaneAction("Open", ("Return", "Enter"), open_local, lambda: selected_local_dir() is not None),
                PaneAction("Up one level", ("Backspace",), self._local_go_up, self.local_up_button.isEnabled),
                PaneAction("Refresh", ("F5",), self._refresh_local_tree, self.local_refresh_button.isEnabled),
                None,
                PaneAction("New folder…", ("F7",), self._local_new_folder, self.local_new_folder_button.isEnabled),
                PaneAction("Rename…", ("F2",), self._local_rename, self.local_rename_button.isEnabled),
                PaneAction("Move to…", ("F6",), self._local_move_to,
                           lambda: self.local_move_button.isEnabled() and has_local_selection()),
                PaneAction("Cut", ("Ctrl+X",), lambda: self._cut_items(local),
                           lambda: self.local_move_button.isEnabled() and has_local_selection()),
                PaneAction("Paste (move here)", ("Ctrl+V",), lambda: self._paste_items(local),
                           lambda: self.local_move_button.isEnabled() and self._has_cut_items(local)),
                None,
                PaneAction("Copy path", ("Ctrl+Shift+C",), lambda: copy_paths(local), lambda: True),
                PaneAction("Delete…", ("Delete",), self._local_delete_selected,
                           lambda: self.local_delete_button.isEnabled() and has_local_selection()),
            ],
        )
        install_pane_actions(
            self.remote_table,
            [
                PaneAction("Open", ("Return", "Enter"), lambda: open_remote(right),
                           lambda: self.remote_up_button.isEnabled() and selected_remote_dir(right) is not None),
                PaneAction("Up one level", ("Backspace",), self._remote_go_up, self.remote_up_button.isEnabled),
                PaneAction("Refresh", ("F5",), lambda: self._refresh_remote_table(force=True),
                           self.remote_refresh_button.isEnabled),
                None,
                PaneAction("New folder…", ("F7",), lambda: self._remote_new_folder(right),
                           self.remote_new_folder_button.isEnabled),
                PaneAction("Rename…", ("F2",), lambda: self._remote_rename(right), self.remote_rename_button.isEnabled),
                PaneAction("Move to…", ("F6",), lambda: self._remote_move_to(right),
                           lambda: self.remote_move_button.isEnabled() and has_remote_selection(right)),
                PaneAction("Cut", ("Ctrl+X",), lambda: self._cut_items(right),
                           lambda: self.remote_move_button.isEnabled() and has_remote_selection(right)),
                PaneAction("Paste (move here)", ("Ctrl+V",), lambda: self._paste_items(right),
                           lambda: self.remote_move_button.isEnabled() and self._has_cut_items(right)),
                None,
                PaneAction("Copy path", ("Ctrl+Shift+C",), lambda: copy_paths(right), lambda: True),
                PaneAction("Delete…", ("Delete",), lambda: self._remote_delete_selected(right),
                           lambda: self.remote_delete_button.isEnabled() and has_remote_selection(right)),
            ],
        )
        install_pane_actions(
            pane.table,
            [
                PaneAction("Open", ("Return", "Enter"), lambda: open_remote("left"),
                           lambda: pane.refresh_button.isEnabled() and selected_remote_dir("left") is not None),
                PaneAction("Up one level", ("Backspace",), pane.go_up, pane.up_button.isEnabled),
                PaneAction("Refresh", ("F5",), lambda: pane.refresh(force=True), pane.refresh_button.isEnabled),
                None,
                PaneAction("New folder…", ("F7",), lambda: self._remote_new_folder("left"),
                           pane.new_folder_button.isEnabled),
                PaneAction("Rename…", ("F2",), lambda: self._remote_rename("left"), pane.rename_button.isEnabled),
                PaneAction("Move to…", ("F6",), lambda: self._remote_move_to("left"),
                           lambda: pane.move_button.isEnabled() and has_remote_selection("left")),
                PaneAction("Cut", ("Ctrl+X",), lambda: self._cut_items("left"),
                           lambda: pane.move_button.isEnabled() and has_remote_selection("left")),
                PaneAction("Paste (move here)", ("Ctrl+V",), lambda: self._paste_items("left"),
                           lambda: pane.move_button.isEnabled() and self._has_cut_items("left")),
                None,
                PaneAction("Copy path", ("Ctrl+Shift+C",), lambda: copy_paths("left"), lambda: True),
                PaneAction("Delete…", ("Delete",), lambda: self._remote_delete_selected("left"),
                           lambda: pane.delete_button.isEnabled() and has_remote_selection("left")),
            ],
        )

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

    def _local_rename(self) -> None:
        paths = self._selected_local_paths()
        if len(paths) != 1:
            self._show_errors(["Select exactly one local file or folder to rename."])
            return
        source = paths[0]
        text, accepted = QInputDialog.getText(
            self,
            "Rename local item",
            "New name (to move items, drag them, use Move to…, or Cut and Paste):",
            text=source.name,
        )
        if not accepted:
            return
        name = text.strip()
        if not name or "/" in name or "\\" in name or name in {".", ".."}:
            self._show_errors(["Enter a new name only, without path separators."])
            return
        if name == source.name:
            return
        destination = source.parent / name
        if destination.exists() and destination.name.casefold() != source.name.casefold():
            self._show_errors([f"Destination already exists: {destination}"])
            return
        try:
            source.rename(destination)
        except OSError as exc:
            self._show_errors([f"Could not rename local item: {exc}"])
            return
        self._append_log(f"Renamed local item: {source} -> {destination}\n")
        self._refresh_local_tree()

    def _move_local_items(
        self,
        raw_paths: list[str],
        target_text: str | Path,
        title: str = "Move local items",
        allowed_root: Path | None = None,
    ) -> bool:
        """Confirm and start a local move. Return True if started.

        Drag passes the displayed folder as allowed_root. Move to… and Paste
        can reach any folder on the same drive, so they use the drive root and
        refuse a different drive up front.
        """
        if self.current_thread or self.remote_thread or self.compare_thread or self.collection_pane.is_busy():
            QMessageBox.information(self, "Operation running", "Wait for the current operation to finish.")
            return False
        paths = [Path(str(path)).expanduser() for path in list(raw_paths or [])]
        target = Path(target_text).expanduser()
        if allowed_root is None:
            drive = target.absolute().anchor
            if not drive or any(path.absolute().anchor.casefold() != drive.casefold() for path in paths):
                self._show_errors(["Local items can only be moved within the same drive."])
                return False
            root = Path(drive)
        else:
            root = allowed_root
        try:
            moves = plan_local_moves(paths, target, root)
        except (OSError, ValueError) as exc:
            self._show_errors([str(exc)])
            return False
        preview = "\n".join(f"{source}\n  → {destination}" for source, destination in moves[:10])
        if len(moves) > 10:
            preview += f"\n... and {len(moves) - 10:,} more"
        if (
            QMessageBox.question(
                self,
                title,
                f"Move {len(moves):,} local item(s)?\n\n{preview}",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            != QMessageBox.Yes
        ):
            return False
        self._start_worker(LocalMoveWorker(paths, target, root), "Move local items", refresh_local=True)
        return True

    @Slot(object, str)
    def _move_local_items_by_drop(self, raw_paths: object, target_text: str) -> None:
        root = Path(self.local_folder_edit.text()).expanduser()
        self._move_local_items([str(path) for path in list(raw_paths or [])], target_text, allowed_root=root)

    def _local_move_to(self) -> None:
        paths = self._selected_local_paths()
        if not paths:
            self._show_errors(["Select one or more local items to move."])
            return
        start = self.local_folder_edit.text() or str(Path.home())
        target = QFileDialog.getExistingDirectory(self, f"Move {len(paths):,} item(s) to…", start)
        if not target:
            return
        self._move_local_items([str(path) for path in paths], target, title="Move to…")

    def _local_paste_target(self) -> Path:
        selected = self._selected_local_paths()
        if len(selected) == 1 and selected[0].is_dir():
            return selected[0]
        return Path(self.local_folder_edit.text() or str(Path.home())).expanduser()

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

    def _refresh_remote_table(self, force: bool = False) -> None:
        profile = self.current_profile()
        if not profile:
            return
        requested_path = self.remote_path_edit.text().strip().rstrip("/") or "/"
        if self.remote_thread:
            self._pending_remote_path = requested_path
            self._pending_remote_force = self._pending_remote_force or force
            cached = None if force else self.remote_directory_cache.get(profile, requested_path)
            if cached is not None:
                self._apply_remote_entries(cached, requested_path)
                self.remote_status_label.setText(f"Showing cached {requested_path}. Use Refresh to reload.")
                self.remote_table.setEnabled(True)
                self.remote_up_button.setEnabled(True)
            else:
                self.remote_status_label.setText(f"Waiting to open {requested_path}...")
            return
        cached = None if force else self.remote_directory_cache.get(profile, requested_path)
        if cached is not None:
            self._apply_remote_entries(cached, requested_path)
            self.remote_status_label.setText(f"Showing cached {requested_path}. Use Refresh to reload.")
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
        worker.cache_profile = profile
        worker.cache_path = requested_path
        thread = QThread(self)
        worker.moveToThread(thread)
        worker.finished.connect(self._remote_listing_finished)
        thread.request_id = request_id
        thread.finished.connect(self._remote_thread_finished)
        thread.finished.connect(thread.deleteLater)
        thread.started.connect(worker.run)
        self.remote_thread = thread
        self.remote_worker = worker
        thread.start()

    @Slot(object, str)
    def _remote_listing_finished(self, entries: object, error: str) -> None:
        worker = self.sender()
        if not isinstance(worker, RemoteListWorker):
            return
        try:
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
                if self._pending_remote_path:
                    self.remote_status_label.setText(f"Waiting to open {self._pending_remote_path}...")
                else:
                    self.remote_entries = []
                    self.remote_entries_path = ""
                    self.remote_table.clear()
                    self.remote_status_label.setText("Remote directory changed while loading. Refresh to view it.")
                return
            if error:
                if "cancelled" in error.lower():
                    self.remote_status_label.setText("Remote listing cancelled.")
                    return
                self.remote_status_label.setText("Remote listing failed.")
                self._forget_passphrase_if_rejected(error)
                QMessageBox.warning(self, "Remote listing failed", error)
                return
            remote_entries = list(entries)
            self.remote_directory_cache.put(worker.cache_profile, worker.cache_path, remote_entries)
            self._apply_remote_entries(remote_entries, worker.cache_path)
            self.remote_status_label.setText(f"{len(remote_entries)} items in {worker.cache_path}")
        finally:
            if self.remote_worker is worker:
                worker.deleteLater()
                if self.remote_thread is not None:
                    self.remote_thread.quit()

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
        pending_path = self._pending_remote_path
        pending_force = self._pending_remote_force
        self._pending_remote_path = None
        self._pending_remote_force = False
        if pending_path and not self._close_after_stop:
            self.remote_path_edit.setText(pending_path)
            self._refresh_remote_table(force=pending_force)

    def _apply_remote_entries(self, entries: list[RemoteEntry], path: str) -> None:
        self.remote_entries = list(entries)
        self.remote_entries_path = path.rstrip("/")
        self.remote_table.current_remote_path = path.rstrip("/") or "/"
        self._populate_remote_table(self.remote_entries)

    def _populate_remote_table(self, entries: list[RemoteEntry]) -> None:
        populate_remote_table(self.remote_table, entries, self.remote_table.current_remote_path or "/")

    def _remote_double_clicked(self, item, _column: int = 0) -> None:
        if item.data(0, IS_DIR_ROLE):
            path = str(item.data(0, PATH_ROLE))
            QTimer.singleShot(0, lambda path=path: self._navigate_remote_path(path))

    def _navigate_remote_path(self, path: str) -> None:
        target = path.strip().rstrip("/") or "/"
        self.remote_path_edit.setText(target)
        self._sync_remote_collection_combo()
        self._refresh_remote_table()

    def _selected_remote_files(self) -> list[RemoteEntry]:
        return [entry for entry in self._selected_remote_entries() if not entry.is_dir]

    def _selected_remote_entries(self) -> list[RemoteEntry]:
        current_path = self.remote_path_edit.text().strip().rstrip("/")
        if self.remote_entries_path != current_path:
            return []
        return selected_remote_entries(self.remote_table, self.remote_entries)

    # Remote file management, shared by both remote panes -------------------
    #
    # side is "right" (the main remote pane) or "left" (the second collection
    # pane in Collection ↔ Collection mode). Every action stays inside one
    # pane; nothing here starts an upload, download or copy.

    def _remote_pane_state(self, side: str) -> tuple[str, str, list[RemoteEntry], list[RemoteEntry]]:
        """Return (current path, loaded path, listed entries, selected entries)."""
        if side == "left":
            pane = self.collection_pane
            return pane.current_path(), pane.entries_path, list(pane.entries), pane.selected_entries()
        current = self.remote_path_edit.text().strip().rstrip("/") or "/"
        loaded = (self.remote_entries_path or "/") if self.remote_entries_path == current.rstrip("/") else ""
        return current, loaded, list(self.remote_entries), self._selected_remote_entries()

    def _remote_operation_profile(self) -> Profile | None:
        if self.current_thread or self.remote_thread or self.compare_thread or self.collection_pane.is_busy():
            QMessageBox.information(self, "Operation running", "Wait for the current operation to finish.")
            return None
        profile = self.current_profile()
        if not profile:
            return None
        errors = self._profile_errors(profile, require_rsync=False)
        if errors:
            self._show_errors(errors)
            return None
        return profile

    def _start_remote_operation(
        self,
        profile: Profile,
        side: str,
        passphrase: str,
        command_factory,
        log_prefix: str,
        label: str,
        touched_paths: list[str],
    ) -> None:
        touched = list(dict.fromkeys(path for path in touched_paths if path))

        def after(_worker: object, _code: int) -> None:
            # Refresh even after a failure: a multi-item move can stop halfway.
            for path in touched:
                self.remote_directory_cache.invalidate(profile, path, subdirectories=True)
                self.collection_pane.invalidate(profile, path)
            if side == "left":
                self.collection_pane.refresh(force=True)

        self._start_worker(
            FallbackCommandWorker(
                profile,
                command_factory,
                f"{log_prefix}_{profile.name}",
                passphrase=passphrase,
                ssh_path=self.detected_ssh,
            ),
            label,
            refresh_remote=side == "right",
            remote_cache_invalidations=[(profile, path, True) for path in touched] if side == "right" else [],
            cleanup=after,
        )

    def _remote_new_folder(self, side: str = "right") -> None:
        profile = self._remote_operation_profile()
        if not profile:
            return
        parent, loaded, _entries, _selected = self._remote_pane_state(side)
        if not loaded or loaded.rstrip("/") != parent.rstrip("/"):
            # mkdir -p on a mistyped, never-listed path would create every
            # missing folder along it.
            self._show_errors(["Load this folder first (click Load or Refresh), then create the new folder."])
            return
        name, accepted = QInputDialog.getText(self, "New remote folder", f"New folder in {parent}:")
        if not accepted:
            return
        name = name.strip()
        if not name or "/" in name or name in {".", ".."}:
            self._show_errors(["Folder name is required and must not contain '/'."])
            return
        passphrase = self._get_session_passphrase(profile)
        if passphrase is None:
            return

        def command_factory(attempt_profile: Profile) -> list[str]:
            return build_remote_mkdir_command(
                attempt_profile, parent, name, ssh_path=self.detected_ssh, batch_mode=not bool(passphrase)
            )

        self._start_remote_operation(
            profile, side, passphrase, command_factory, "remote_mkdir", "Create remote folder", [parent]
        )

    def _remote_rename(self, side: str = "right") -> None:
        profile = self._remote_operation_profile()
        if not profile:
            return
        _current, _loaded, _entries, selected = self._remote_pane_state(side)
        if len(selected) != 1:
            self._show_errors(["Select exactly one remote file or folder to rename."])
            return
        entry = selected[0]
        text, accepted = QInputDialog.getText(
            self,
            "Rename remote item",
            "New name (to move items, drag them, use Move to…, or Cut and Paste):",
            text=entry.name,
        )
        if not accepted:
            return
        name = text.strip()
        if not name or "/" in name or name in {".", ".."}:
            self._show_errors(["Enter a new name only, without '/'."])
            return
        if name == entry.name:
            return
        parent = entry.path.rsplit("/", 1)[0] or "/"
        destination = f"{parent.rstrip('/')}/{name}"
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

        self._start_remote_operation(
            profile, side, passphrase, command_factory, "remote_rename", "Rename remote item",
            [parent, entry.path, destination],
        )

    def _move_remote_items(
        self,
        side: str,
        source_paths: list[str],
        destination_directory: str,
        require_visible: bool,
        title: str = "Move remote items",
    ) -> bool:
        """Confirm and start a move inside one collection. Return True if started."""
        profile = self._remote_operation_profile()
        if not profile:
            return False
        _current, _loaded, entries, _selected = self._remote_pane_state(side)
        if require_visible:
            visible_paths = {entry.path for entry in entries}
            if not source_paths or any(path not in visible_paths for path in source_paths):
                self._show_errors(["The remote selection changed. Refresh the folder and try again."])
                return False
        if not source_paths:
            self._show_errors(["Select one or more remote items to move."])
            return False
        # Moves stay inside the collection the items come from.
        try:
            collection_root = collection_of_remote_path(source_paths[0])
        except ValueError:
            self._show_errors(["Remote moves are only allowed inside a collection folder (for example /data/Q0101/...)."])
            return False
        collection_name = collection_root.rsplit("/", 1)[-1]
        try:
            build_remote_move_into_command(
                profile, source_paths, destination_directory, collection_root, ssh_path=self.detected_ssh
            )
        except ValueError as exc:
            self._show_errors([str(exc)])
            return False
        preview = "\n".join(
            f"{source}\n  → {destination_directory.rstrip('/')}/{source.rsplit('/', 1)[-1]}"
            for source in source_paths[:10]
        )
        if len(source_paths) > 10:
            preview += f"\n... and {len(source_paths) - 10:,} more"
        if (
            QMessageBox.question(
                self,
                title,
                f"Move {len(source_paths):,} remote item(s) within {collection_name}?\n\n{preview}",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            != QMessageBox.Yes
        ):
            return False
        passphrase = self._get_session_passphrase(profile)
        if passphrase is None:
            return False

        def command_factory(attempt_profile: Profile) -> list[str]:
            return build_remote_move_into_command(
                attempt_profile,
                source_paths,
                destination_directory,
                collection_root,
                ssh_path=self.detected_ssh,
                batch_mode=not bool(passphrase),
            )

        touched = [destination_directory, *source_paths, *(path.rsplit("/", 1)[0] or "/" for path in source_paths)]
        self._start_remote_operation(
            profile, side, passphrase, command_factory, "remote_move", "Move remote items", touched
        )
        return True

    @Slot(object, str)
    def _move_remote_items_by_drop(self, raw_paths: object, destination_directory: str, side: str = "right") -> None:
        source_paths = [str(path) for path in list(raw_paths or [])]
        self._move_remote_items(side, source_paths, destination_directory, require_visible=True)

    def _remote_move_to(self, side: str = "right") -> None:
        profile = self._remote_operation_profile()
        if not profile:
            return
        current, _loaded, _entries, selected = self._remote_pane_state(side)
        if not selected:
            self._show_errors(["Select one or more remote items to move."])
            return
        passphrase = self._get_session_passphrase(profile)
        if passphrase is None:
            return
        dialog = RemoteBrowserDialog(profile, self.detected_ssh, current, passphrase, parent=self)
        dialog.setWindowTitle(f"Move {len(selected):,} item(s) to… (same collection only)")
        if dialog.exec() != QDialog.Accepted:
            return
        self._move_remote_items(
            side, [entry.path for entry in selected], dialog.selected_path, require_visible=False, title="Move to…"
        )

    def _remote_delete_selected(self, side: str = "right") -> None:
        profile = self._remote_operation_profile()
        if not profile:
            return
        current, _loaded, _entries, entries = self._remote_pane_state(side)
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

        self._start_remote_operation(
            profile, side, passphrase, command_factory, "remote_delete", "Delete remote items",
            [current, *(entry.path for entry in entries)],
        )

    # Cut and paste (moves only, always within the pane it was cut from) -----

    def _cut_items(self, pane: str) -> None:
        if pane == "local":
            paths = [str(path) for path in self._selected_local_paths()]
        else:
            paths = [entry.path for entry in self._remote_pane_state(pane)[3]]
        if not paths:
            self._show_errors(["Select one or more items to cut."])
            return
        self._cut_clipboard = (pane, paths)
        self._append_log(
            f"Cut {len(paths):,} item(s). Open the destination folder in the same pane and press Ctrl+V "
            "(or right-click → Paste) to move them there.\n"
        )
        self.transfer_status_label.setText(f"{len(paths):,} item(s) cut — paste in the same pane to move them.")

    def _paste_items(self, pane: str) -> None:
        clipboard = self._cut_clipboard
        if not clipboard:
            self._show_errors(["Nothing to paste. Select items and use Cut (Ctrl+X) first."])
            return
        source_pane, paths = clipboard
        if source_pane != pane:
            self._show_errors(
                [
                    "Paste in the same pane you cut from. Cut and Paste only moves items; "
                    "use the transfer buttons to upload, download or copy between panes."
                ]
            )
            return
        # A paste attempt always consumes the cut list, so a stale list can
        # never be pasted twice.
        self._cut_clipboard = None
        if pane == "local":
            destination = self._local_paste_target()
            self._move_local_items(paths, destination, title="Paste (move) local items")
        else:
            current, _loaded, _entries, selected = self._remote_pane_state(pane)
            folders = [entry for entry in selected if entry.is_dir]
            destination = folders[0].path if len(selected) == 1 and folders else current
            self._move_remote_items(pane, paths, destination, require_visible=False, title="Paste (move) remote items")

    def _has_cut_items(self, pane: str) -> bool:
        return bool(self._cut_clipboard and self._cut_clipboard[0] == pane)

    def _remote_go_up(self) -> None:
        current = self.remote_path_edit.text().strip().rstrip("/") or "/"
        if current == "/":
            return
        parent = current.rsplit("/", 1)[0] or "/"
        self._navigate_remote_path(parent)

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
        """rsync --files-from list, relative to the displayed folder.

        Files picked inside an expanded subfolder keep their subfolder, so
        they download into the matching local subfolder.
        """
        base = (self.remote_path_edit.text().strip().rstrip("/") or "") + "/"
        names = [entry.path[len(base):] if entry.path.startswith(base) else entry.name for entry in entries]
        path = new_log_file("download_selected_files").with_suffix(".txt")
        path.write_text("\n".join(names) + "\n", encoding="utf-8", newline="\n")
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

    def _check_tape_status(self) -> None:
        """Report which selected items (or the open folder) are on tape."""
        profile = self.current_profile()
        if not profile:
            return
        errors = self._profile_errors(profile, require_rsync=False)
        if errors:
            self._show_errors(errors)
            return
        current = self.remote_path_edit.text().strip().rstrip("/")
        selected = self._selected_remote_entries()
        paths = [entry.path for entry in selected] or [current]
        try:
            medici_path_for_remote_path(paths[0])
        except ValueError as exc:
            self._show_errors([str(exc)])
            return
        passphrase = self._get_session_passphrase(profile)
        if passphrase is None:
            return

        def show_result(worker, _code: int) -> None:
            if worker.result is None:
                QMessageBox.warning(self, "Tape status", "The tape status check did not finish. See the log for details.")
                return
            dialog = TapeStatusDialog(worker.result, paths, self, default_username=profile.username)
            outcome = dialog.exec()
            if outcome == QDialog.Accepted:
                self._recall_files(profile, [item.path for item in worker.result.needs_recall])
            elif outcome == TapeStatusDialog.BUNYA_SUBMIT and dialog.created_job and dialog.submit_settings:
                self._submit_bunya_job(dialog.submit_settings, *dialog.created_job)

        self._start_worker(
            TapeStatusWorker(profile, paths, self.detected_ssh, passphrase=passphrase),
            "Check tape status",
            after_finish=show_result,
        )

    def _show_recall_jobs(self) -> None:
        RecallJobsDialog(self.current_profile, self._get_session_passphrase, self.detected_ssh, self).exec()

    def _submit_bunya_job(self, settings, script: Path, file_list: Path) -> None:
        def show_result(worker, _code: int) -> None:
            if worker.job_id:
                try:
                    file_count = sum(1 for line in file_list.read_text(encoding="utf-8").splitlines() if line.strip())
                except OSError:
                    file_count = 0
                record_job(
                    BunyaJob(
                        job_id=worker.job_id,
                        submitted=datetime.now().strftime("%Y-%m-%d %H:%M"),
                        username=settings.username,
                        folder=settings.folder,
                        script_name=script.name,
                        local_list=str(file_list),
                        file_count=file_count,
                    )
                )
                QMessageBox.information(
                    self,
                    "Recall submitted to Bunya",
                    f"Bunya job {worker.job_id} is queued. It keeps running after you close this app.\n\n"
                    f"Job files and its log ({script.stem}_{worker.job_id}.log) are in "
                    f"{settings.folder} on Bunya. A copy of the job files is in {script.parent}.\n\n"
                    "To follow it, open Tape status and recall → Recall jobs on Bunya… and click Check progress. That asks QRIScloud how many "
                    "of the job's files are back on disk, with no MFA needed.",
                )
            else:
                QMessageBox.warning(
                    self,
                    "Bunya submission failed",
                    "The job was not submitted. See the log for Bunya's message.\n\n"
                    "Common causes: the MFA request was not approved, the SSH key is not registered "
                    "for Bunya, or the Bunya account name is wrong.\n\n"
                    f"The job files are saved in {script.parent}, so you can also submit them yourself.",
                )

        self._start_worker(
            BunyaSubmitWorker(settings, script, file_list, self.detected_ssh),
            "Submit recall to Bunya",
            after_finish=show_result,
        )

    def _recall_files(self, profile: Profile, remote_files: list[str]) -> None:
        passphrase = self._get_session_passphrase(profile)
        if passphrase is None or not remote_files:
            return
        self._start_worker(
            RecallMediciWorker(profile, remote_files, self.detected_ssh, passphrase=passphrase, batch_size=25),
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
        self.log_output.clear()
        self._set_compare_running(True)
        self.transfer_progress.setRange(0, 0)
        self.transfer_status_label.setText("Comparing folders...")
        self._append_log("Comparing the local and QRIScloud folders by name, size, and modified time...\n")
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
            self.transfer_status_label.setText("Comparison failed.")
            self._append_log(f"\n{error}\n")
            self._forget_passphrase_if_rejected(error)
            QMessageBox.warning(self, "Comparison failed", error)
            return
        self.transfer_progress.setValue(100)
        result = dict(result)
        plan = result["plan"]
        local_folder = str(result["local_folder"])
        remote_path = str(result["remote_path"])
        if not plan.items:
            same = plan.identical_files + plan.identical_archives
            self.transfer_status_label.setText("Folders are already in sync.")
            QMessageBox.information(
                self,
                "Already in sync",
                f"Nothing to do: all {same:,} item(s) are the same on this computer and on QRIScloud.",
            )
            return
        self.transfer_status_label.setText(f"Comparison finished: {len(plan.items):,} difference(s).")
        dialog = SynchronizeDialog(
            plan,
            local_folder,
            remote_path,
            self,
            suggested_remote_path=self._suggested_sync_folder(plan, local_folder, remote_path),
        )
        outcome = dialog.exec()
        if outcome == SynchronizeDialog.RECOMPARE:
            self._build_sync_selection()
        elif outcome == SynchronizeDialog.RECOMPARE_SUGGESTED and dialog.suggested_remote_path:
            self.remote_path_edit.setText(dialog.suggested_remote_path)
            self._refresh_remote_table()
            self._build_sync_selection()
        elif outcome == QDialog.Accepted:
            self._start_sync_transfer(plan, result.get("upload_plan"), local_folder, remote_path)

    def _suggested_sync_folder(self, plan, local_folder: str, remote_path: str) -> str | None:
        """Spot the common slip of comparing a folder with the parent it was uploaded into."""
        if plan.identical_files or plan.identical_archives or any(item.local and item.remote for item in plan.items):
            return None
        name = Path(local_folder).name
        if not name or self.remote_entries_path.rstrip("/") != remote_path.rstrip("/"):
            return None
        if any(entry.kind == "d" and entry.name == name for entry in self.remote_entries):
            return f"{remote_path.rstrip('/')}/{name}"
        return None

    def _compare_thread_finished(self) -> None:
        self.compare_thread = None
        self.compare_worker = None

    def _start_sync_transfer(self, plan, upload_plan, local_folder: str, remote_path: str) -> None:
        """Run the ticked uploads, then the ticked downloads, from the Synchronise window."""
        profile = self.current_profile()
        if not profile:
            return
        passphrase = self._get_session_passphrase(profile)
        if passphrase is None:
            return
        uploads = plan.chosen(UPLOAD)
        downloads = plan.chosen(DOWNLOAD)
        stamp = time.time_ns()
        loose_list = None
        loose = [item.local for item in uploads if not item.is_archive and item.local is not None]
        if loose:
            loose_list = write_files_from(loose, f"sync_upload_{stamp}")
        archive_folders = {item.archive.relative_folder for item in uploads if item.is_archive}
        archive_plan = subset_upload_plan(upload_plan, archive_folders) if archive_folders and upload_plan else None
        download_list = None
        if downloads:
            download_list = write_files_from([item.remote for item in downloads], f"sync_download_{stamp}")

        def start_downloads(_worker=None, code: int = 0) -> None:
            if download_list is None:
                return
            if code != 0:
                self._append_log("\nDownloads were not started because the upload did not finish.\n")
                return

            def command_factory(attempt_profile: Profile) -> list[str]:
                return build_rsync_command(
                    attempt_profile,
                    local_folder,
                    remote_path=remote_path,
                    dry_run=False,
                    ssh_path=self.detected_ssh,
                    batch_mode=not bool(passphrase),
                    files_from=str(download_list),
                    direction="download",
                    itemize=True,
                )

            self._start_worker(
                FallbackCommandWorker(
                    profile,
                    command_factory,
                    f"sync_download_{profile.name}",
                    passphrase=passphrase,
                    ssh_path=self.detected_ssh,
                    receipt_plan=ReceiptPlan("download", Path(local_folder), remote_path),
                    receipt_base=(Path(local_folder), remote_path),
                ),
                "Synchronise: download",
                refresh_local=True,
                clear_log=loose_list is None and archive_plan is None,
            )

        if loose_list is None and archive_plan is None:
            start_downloads()
            return
        self._start_worker(
            SyncUploadWorker(
                profile,
                Path(local_folder),
                remote_path,
                loose_list,
                archive_plan,
                passphrase=passphrase,
                ssh_path=self.detected_ssh,
            ),
            "Synchronise: upload",
            refresh_remote=True,
            remote_cache_invalidations=[(profile, remote_path, True)],
            after_finish=start_downloads,
        )

    # Collection ↔ Collection mode -------------------------------------------

    def _collection_mode(self) -> bool:
        return str(self.mode_combo.currentData()) == "collections"

    def _collection_choices(self) -> list[tuple[str, str]]:
        """(profile name, remote path) for profiles that share the active login.

        Both panes browse with the selected profile's account, so collections
        saved under a different username would not be reachable.
        """
        profile = self.current_profile()
        if profile is None:
            return []
        return [
            (candidate.name, candidate.remote_path)
            for candidate in (item.normalized() for item in self.profiles)
            if candidate.username == profile.username
        ]

    def _refresh_collection_choices(self) -> None:
        profile = self.current_profile()
        choices = self._collection_choices()
        fill_collection_combo(self.remote_collection_combo, choices)
        self._sync_remote_collection_combo()
        current = profile.remote_path if profile else ""
        others = [path for _label, path in choices if path != current]
        self.collection_pane.set_collections(choices, preferred=others[0] if others else "")

    def _sync_remote_collection_combo(self) -> None:
        sync_collection_combo(self.remote_collection_combo, self.remote_path_edit.text())
        self._update_collection_exclusions()

    def _update_collection_exclusions(self) -> None:
        """In collection mode, a collection open in one pane cannot be picked in the other."""
        if not hasattr(self, "mode_combo"):
            return
        collections = self._collection_mode()
        exclude_collection(self.remote_collection_combo, self.collection_pane.current_path() if collections else "")
        exclude_collection(self.collection_pane.collection_combo, self.remote_path_edit.text() if collections else "")

    def _same_collection_in_both_panes(self) -> bool:
        try:
            return collection_key(self.collection_pane.current_path())[0] == collection_key(
                self.remote_path_edit.text()
            )[0]
        except ValueError:
            return False

    def _move_left_pane_off_right_collection(self) -> None:
        """Open the left pane on another collection when it would match the right pane."""
        if not self._same_collection_in_both_panes():
            return
        right = self.remote_path_edit.text()
        for _label, path in self._collection_choices():
            try:
                if collection_key(path)[0] != collection_key(right)[0]:
                    self.collection_pane.path_edit.setText(path)
                    self.collection_pane.entries = []
                    self.collection_pane.entries_path = ""
                    self.collection_pane.table.setRowCount(0)
                    self.collection_pane._path_edited()
                    return
            except ValueError:
                continue

    def _remote_collection_picked(self, index: int) -> None:
        path = self.remote_collection_combo.itemData(index)
        if path:
            self._navigate_remote_path(str(path))

    def _mode_changed(self) -> None:
        busy = self.current_thread or self.remote_thread or self.compare_thread or self.collection_pane.is_busy()
        if busy:
            self.mode_combo.blockSignals(True)
            self.mode_combo.setCurrentIndex(self.left_stack.currentIndex())
            self.mode_combo.blockSignals(False)
            QMessageBox.information(self, "Operation running", "Wait for the current operation to finish before switching mode.")
            return
        collections = self._collection_mode()
        self.left_stack.setCurrentIndex(1 if collections else 0)
        self.build_selection_button.setVisible(not collections)
        if collections:
            self.mode_hint_label.setText(
                "Both panes use the selected profile's account. Copies run on QRIScloud and never delete files."
            )
            self._move_left_pane_off_right_collection()
            profile = self.current_profile()
            if profile and profile.username and not self.collection_pane.entries_path:
                self.collection_pane.refresh()
        else:
            self.mode_hint_label.setText("")
        self._update_collection_exclusions()
        self._update_transfer_scope_ui()

    @Slot(bool)
    def _collection_pane_busy_changed(self, busy: bool) -> None:
        if not busy:
            self.profile_box.setEnabled(
                self.current_thread is None and self.remote_thread is None and self.compare_thread is None
            )
        self._update_mode_controls()
        self._update_transfer_scope_ui()

    def _update_mode_controls(self) -> None:
        # The _set_*_busy/running callers have just set the profile box from
        # their own state (which can change before the thread fields do), so
        # follow it rather than recomputing idleness here.
        pane_busy = self.collection_pane.is_busy()
        if pane_busy:
            # A listing in either pane locks the profile, so results cannot be
            # applied to a different account's context.
            self.profile_box.setEnabled(False)
        self.mode_combo.setEnabled(self.profile_box.isEnabled())
        self.remote_collection_combo.setEnabled(self.remote_path_edit.isEnabled())
        self.collection_pane.set_locked(not pane_busy and not self.profile_box.isEnabled())

    def _update_collection_scope_ui(self) -> None:
        selected_scope = str(self.transfer_scope_combo.currentData()) == "selected"
        left_items = self.collection_pane.selected_entries() if selected_scope else []
        right_items = self._selected_remote_entries() if selected_scope else []
        if selected_scope:
            self.dry_run_button.setText(f"Compare selected → right ({len(left_items):,})")
            self.upload_button.setText(f"Copy selected → right ({len(left_items):,})")
            self.download_dry_run_button.setText(f"Compare selected → left ({len(right_items):,})")
            self.download_button.setText(f"Copy selected → left ({len(right_items):,})")
            self.selection_status_label.setText(
                f"Selected: {len(left_items):,} left item(s) · {len(right_items):,} right item(s)"
            )
        else:
            self.dry_run_button.setText("Compare left folder → right")
            self.upload_button.setText("Copy left folder → right")
            self.download_dry_run_button.setText("Compare right folder → left")
            self.download_button.setText("Copy right folder → left")
            self.selection_status_label.setText("Current folder")
        idle = (
            self.current_thread is None
            and self.compare_thread is None
            and self.remote_thread is None
            and not self.collection_pane.is_busy()
        )
        left_ok = idle and (not selected_scope or bool(left_items))
        right_ok = idle and (not selected_scope or bool(right_items))
        self.dry_run_button.setEnabled(left_ok)
        self.upload_button.setEnabled(left_ok)
        self.download_dry_run_button.setEnabled(right_ok)
        self.download_button.setEnabled(right_ok)

    def _start_collection_copy(self, dry_run: bool, left_to_right: bool) -> None:
        profile = self.current_profile()
        if not profile:
            return
        errors = self._profile_errors(profile, require_rsync=False)
        if errors:
            self._show_errors(errors)
            return
        if self.remote_thread or self.collection_pane.is_busy():
            QMessageBox.information(self, "Operation running", "Wait for the remote listing to finish.")
            return
        right_path = self.remote_path_edit.text().strip().rstrip("/") or "/"
        left_path = self.collection_pane.current_path()
        if self.remote_entries_path != right_path or self.collection_pane.entries_path != left_path:
            self._show_errors(["Load both panes first, so the app has confirmed both folders exist and are accessible."])
            return
        scope = str(self.transfer_scope_combo.currentData())
        if left_to_right:
            source_folder, destination = left_path, right_path
            selected = self.collection_pane.selected_entries()
        else:
            source_folder, destination = right_path, left_path
            selected = self._selected_remote_entries()
        if scope == "selected":
            if not selected:
                self._show_errors(["Select one or more items in the source pane, or choose Current folder."])
                return
            sources = [entry.path for entry in selected]
        else:
            sources = [source_folder]
        try:
            landings = collection_copy_destinations(sources, destination)
        except ValueError as exc:
            self._show_errors([str(exc)])
            return

        preview = "\n".join(f"{source}\n  → {landing}" for source, landing in zip(sources[:10], landings[:10]))
        if len(sources) > 10:
            preview += f"\n... and {len(sources) - 10:,} more"
        mode = (
            "Compare only (nothing is copied or changed)"
            if dry_run
            else "Copy (files with the same name at the destination may be updated)"
        )
        if (
            QMessageBox.question(
                self,
                "Confirm collection copy",
                f"Mode: {mode}\n\n{preview}\n\n"
                "Copy only: nothing is deleted or moved. rsync runs on the QRIScloud server, "
                "so the data does not pass through this PC. The source's owner and group are not "
                "copied; files are owned as if this account had uploaded them.\n\n"
                "Files that are on Medici tape are recalled when read, which can be slow. "
                "Use Recall from tape first for large folders.\n\nContinue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            != QMessageBox.Yes
        ):
            return
        passphrase = self._get_session_passphrase(profile)
        if passphrase is None:
            return

        def command_factory(attempt_profile: Profile) -> list[str]:
            return build_collection_copy_command(
                attempt_profile,
                sources,
                destination,
                dry_run=dry_run,
                ssh_path=self.detected_ssh,
                batch_mode=not bool(passphrase),
            )

        def after_copy(_worker: object, _code: int) -> None:
            # Runs after success, failure and Stop: a partial copy still
            # changes the destination. The panes keep separate caches.
            self.collection_pane.invalidate(profile, destination)
            self.remote_directory_cache.invalidate(profile, destination, subdirectories=True)
            if not left_to_right:
                self.collection_pane.refresh(force=True)

        refresh_right = left_to_right and not dry_run
        self._start_worker(
            FallbackCommandWorker(
                profile,
                command_factory,
                f"collection_copy{'_dry_run' if dry_run else ''}_{profile.name}",
                passphrase=passphrase,
                ssh_path=self.detected_ssh,
            ),
            "Compare collection copy" if dry_run else "Collection copy",
            refresh_remote=refresh_right,
            remote_cache_invalidations=[(profile, destination, True)] if refresh_right else [],
            cleanup=None if dry_run else after_copy,
        )
        if self.current_worker is not None and not dry_run:
            self._append_log(
                "Stop closes the SSH connection. The server-side rsync normally stops within seconds, "
                "but may continue briefly (for example while waiting on a tape recall). "
                "Running the copy again skips files that were already copied.\n"
            )

    def _start_rsync(self, dry_run: bool, direction: str = "upload") -> None:
        if self._collection_mode():
            # In collection mode the upload buttons copy left → right and the
            # download buttons copy right → left.
            self._start_collection_copy(dry_run, left_to_right=direction == "upload")
            return
        profile = self.current_profile()
        if not profile:
            return
        scope = str(self.transfer_scope_combo.currentData())
        selected_local_paths = self._selected_local_paths() if direction == "upload" and scope == "selected" else []
        selected_remote_entries = self._selected_remote_entries() if direction == "download" and scope == "selected" else []
        if scope == "selected":
            selected_items = selected_local_paths if direction == "upload" else selected_remote_entries
            if not selected_items:
                self._show_errors([f"Select one or more {direction} items, or choose Current folder."])
                return
            directories = [item for item in selected_items if (item.is_dir() if isinstance(item, Path) else item.is_dir)]
            if directories:
                if direction != "upload" or len(selected_items) != 1:
                    self._show_errors(
                        [
                            "Select one local folder at a time, or select files only. "
                            "Selected remote folders are not yet supported."
                        ]
                    )
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
        if direction == "upload" and scope == "selected" and selected_local_file and selected_local_file.is_dir():
            self._start_upload_scan(
                profile,
                dry_run,
                local_path=str(selected_local_file),
                scope="selected",
                selected_local_paths=[selected_local_file],
                include_source_directory=True,
            )
            return
        if direction == "upload" and scope == "folder":
            self._start_upload_scan(profile, dry_run, include_source_directory=True)
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

    @staticmethod
    def _archive_batch_summary(archive_plan: UploadArchivePlan, dry_run: bool) -> str:
        total = f"Archives hold {archive_plan.archived_file_count:,} files ({format_bytes(archive_plan.archived_bytes)})."
        if dry_run:
            return total + " A comparison does not build them."
        try:
            batch_plan = plan_archive_batches(archive_plan)
        except OSError:
            return total
        largest = max((required_temp_bytes(batch.groups) for batch in batch_plan.batches), default=0)
        text = (
            f"{total} After the other files, they are built and uploaded one batch at a time "
            f"({len(batch_plan.batches):,} batch(es)), each deleted before the next. Temporary space used at most: "
            f"about {format_bytes(largest)} in {batch_plan.work_parent}; "
            f"{format_bytes(ARCHIVE_TEMP_RESERVE_BYTES)} is always left free there."
        )
        if batch_plan.too_big:
            text += (
                f"\n{len(batch_plan.too_big):,} archive part(s) are too big for the free space there and will be "
                "skipped and listed in the log, for example: "
                + ", ".join(group.archive_name for group in batch_plan.too_big[:3])
            )
        return text

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
        archive_plan: UploadArchivePlan | None = None,
        include_source_directory: bool = False,
    ) -> None:
        source = local_transfer_path if direction == "upload" else remote_transfer_path
        destination = remote_transfer_path if direction == "upload" else local_transfer_path
        item_count = len(selected_local_files) if direction == "upload" else len(selected_remote_files)
        count_text = f"{item_count:,} selected item(s)" if scope == "selected" else "current folder (recursive)"
        if scan is not None:
            count_text = f"{scan.file_count:,} files ({scan.total_bytes:,} bytes; {scan.tiny_file_count:,} under 1 MB)"
        warnings = ""
        if scan and scan.warnings():
            warnings = "\n\nWarnings:\n" + "\n".join(f"- {warning}" for warning in scan.warnings())
        archive_notice = ""
        if archive_plan and archive_plan.groups:
            group_lines = [
                f"- {(group.relative_folder + '/') if group.relative_folder else ''}{group.archive_name}: "
                f"{group.member_count:,} files ({group.media_count:,} {group.category} plus related files)"
                for group in archive_plan.groups[:12]
            ]
            if len(archive_plan.groups) > 12:
                group_lines.append(f"- ...and {len(archive_plan.groups) - 12:,} more archive(s)")
            archive_notice = (
                "\n\nAutomatic media archiving:\n"
                "A folder with more than 50 files of the same type directly inside it gets one "
                "uncompressed TAR archive holding those files and files that share their names "
                "(sidecars, shapefile parts). Archives over 50 GB are split into numbered parts, "
                "each with its own inventory. Other files upload normally. "
                "The source folder will not be changed. Existing individual copies already on QRIScloud "
                "will not be deleted automatically.\n"
                + "\n".join(group_lines)
                + "\n"
                + self._archive_batch_summary(archive_plan, dry_run)
            )
        mode = "Dry run / compare (no files will be changed)" if dry_run else "Transfer (existing files may be updated; destination files are not deleted)"
        if (
            QMessageBox.question(
                self,
                f"Confirm {direction}",
                f"Scope: {count_text}\nSource: {source}\nDestination: {destination}\nMode: {mode}"
                f"{archive_notice}{warnings}\n\nContinue?",
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
        if direction == "upload" and archive_plan and archive_plan.groups:
            label = "Packaged upload dry run" if dry_run else "Packaged upload"
            self._start_worker(
                PackagedUploadWorker(
                    profile,
                    archive_plan,
                    remote_transfer_path,
                    dry_run=dry_run,
                    passphrase=passphrase,
                    ssh_path=self.detected_ssh,
                    include_source_directory=include_source_directory,
                ),
                label,
                refresh_remote=not dry_run,
                remote_cache_invalidations=[] if dry_run else [(profile, remote_transfer_path, True)],
            )
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
                include_source_directory=include_source_directory,
                itemize=not dry_run,
            )

        receipt_plan = None
        receipt_base = None
        if not dry_run:
            if direction == "upload":
                receipt_plan = ReceiptPlan(
                    direction,
                    *upload_receipt_roots(
                        local_transfer_path,
                        remote_transfer_path,
                        files_from=bool(visible_files_from),
                        include_source_directory=include_source_directory,
                    ),
                )
                receipt_base = (
                    upload_local_base(
                        local_transfer_path,
                        files_from=bool(visible_files_from),
                        include_source_directory=include_source_directory,
                    ),
                    remote_transfer_path,
                )
            else:
                remote_base = (
                    remote_transfer_path.rsplit("/", 1)[0] or "/"
                    if selected_remote_file is not None
                    else remote_transfer_path
                )
                receipt_plan = ReceiptPlan(direction, Path(local_transfer_path), remote_base)
                receipt_base = (Path(local_transfer_path), remote_base)

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
                receipt_plan=receipt_plan,
                receipt_base=receipt_base,
            ),
            label,
            refresh_remote=direction == "upload" and not dry_run,
            remote_cache_invalidations=(
                [(profile, remote_transfer_path, True)] if direction == "upload" and not dry_run else []
            ),
        )
        if visible_files_from:
            if selected_local_files_from:
                self._append_log(f"Uploading {len(selected_local_files):,} selected files.\n")
            elif selected_remote_files_from:
                self._append_log(f"Downloading {len(selected_remote_files):,} selected files.\n")
            else:
                self._append_log(
                    f"Using visible remote file list ({len(self.remote_entries):,} entries) to avoid recursive rsync discovery.\n"
                )
        elif selected_local_file:
            self._append_log(f"Uploading selected item: {selected_local_file}\n")
        elif selected_remote_file:
            self._append_log(f"Downloading selected file: {selected_remote_file.path}\n")

    def _start_upload_scan(
        self,
        profile: Profile,
        dry_run: bool,
        *,
        local_path: str | None = None,
        scope: str = "folder",
        selected_local_paths: list[Path] | None = None,
        include_source_directory: bool = False,
    ) -> None:
        local_path = local_path or self.local_folder_edit.text()
        remote_path = self.remote_path_edit.text()
        selected_paths = selected_local_paths or []

        def continue_upload(worker: object, code: int) -> None:
            if code != 0 or not isinstance(worker, UploadAnalysisWorker) or worker.result is None:
                return
            self._start_resolved_rsync(
                profile,
                dry_run,
                "upload",
                scope,
                selected_paths,
                [],
                selected_paths[0] if len(selected_paths) == 1 else None,
                None,
                local_path,
                remote_path,
                None,
                None,
                scan=worker.result.scan,
                archive_plan=worker.result,
                include_source_directory=include_source_directory,
            )

        self._start_worker(UploadAnalysisWorker(local_path), "Analyse upload folder", after_finish=continue_upload)

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
        key = passphrase_cache_key(profile.ssh_key_path)
        if key in self._session_passphrases:
            return self._session_passphrases[key]
        passphrase = self._ask_passphrase(profile)
        if passphrase is not None:
            self._session_passphrases[key] = passphrase
        return passphrase

    def _forget_passphrase_if_rejected(self, text: str) -> None:
        """Drop a remembered passphrase after QRIScloud refuses the key.

        Otherwise a mistyped passphrase would be reused for the rest of the
        session. The next connection asks for it again.
        """
        profile = self.current_profile()
        if profile is None or not profile.ssh_key_path or not ssh_key_was_rejected(text):
            return
        if self._session_passphrases.pop(passphrase_cache_key(profile.ssh_key_path), None) is not None:
            self._append_log(
                "\nThe SSH key was rejected, so the remembered passphrase has been cleared. "
                "You will be asked for it again next time.\n"
            )

    def _start_worker(
        self,
        worker: CommandWorker | SshTestWorker | RecallMediciWorker | FolderScanWorker | UploadAnalysisWorker | PackagedUploadWorker | LocalDeleteWorker | LocalMoveWorker,
        label: str,
        refresh_local: bool = False,
        refresh_remote: bool = False,
        remote_cache_invalidations: list[tuple[Profile, str, bool]] | None = None,
        after_finish=None,
        cleanup=None,
        clear_log: bool = True,
    ) -> None:
        """Start a background operation.

        after_finish is a continuation that Stop cancels. cleanup always runs
        when the operation ends (success, failure or Stop), for refreshing
        listings that the operation may have changed.
        """
        if self.current_thread or self.remote_thread or self.compare_thread or self.collection_pane.is_busy():
            QMessageBox.information(self, "Operation running", "Another operation is already running.")
            return
        if clear_log:
            self.log_output.clear()
        thread = QThread(self)
        worker.moveToThread(thread)
        worker.output.connect(self._append_log)
        self._current_operation_id += 1
        operation_id = self._current_operation_id
        worker.operation_id = operation_id
        thread.operation_id = operation_id
        worker.finished.connect(self._worker_finished)
        thread.finished.connect(self._current_thread_finished)
        thread.finished.connect(thread.deleteLater)
        thread.started.connect(worker.run)
        self.current_thread = thread
        self.current_worker = worker
        self.current_label = label
        self.current_finished_handled = False
        self.current_refresh_local = refresh_local
        self.current_refresh_remote = refresh_remote
        self.current_remote_cache_invalidations = list(remote_cache_invalidations or [])
        self.current_after_finish = after_finish
        self.current_cleanup = cleanup
        self.current_exit_code = None
        self._set_running(True)
        self._reset_transfer_progress(label)
        if isinstance(worker, (LocalDeleteWorker, LocalMoveWorker)):
            self.stop_button.setEnabled(False)
            self.transfer_status_label.setText("Moving local items (this filesystem operation cannot be interrupted safely)...")
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
        try:
            self.current_finished_handled = True
            self.current_exit_code = code
            label = self.current_label or "Command"
            self._append_log(f"\n{label} finished with exit code {code}.\n")
            if code == 0 and any(word in label.lower() for word in ("upload", "download", "dry run", "recall", "copy")):
                self.transfer_progress.setValue(100)
            self.transfer_status_label.setText(f"{label} finished with exit code {code}.")
        finally:
            if self.current_worker is worker:
                worker.deleteLater()
                if self.current_thread is not None:
                    self.current_thread.quit()

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
        remote_cache_invalidations = self.current_remote_cache_invalidations
        worker = self.current_worker
        exit_code = self.current_exit_code if self.current_exit_code is not None else 1
        after_finish = self.current_after_finish
        cleanup = self.current_cleanup
        self.current_thread = None
        self.current_worker = None
        self.current_label = ""
        self.current_after_finish = None
        self.current_cleanup = None
        self.current_remote_cache_invalidations = []
        self.current_exit_code = None
        self._set_running(False)
        if exit_code != 0:
            self._forget_passphrase_if_rejected(self.log_output.toPlainText()[-20000:])
        if exit_code == 0:
            for profile, path, subdirectories in remote_cache_invalidations:
                self.remote_directory_cache.invalidate(profile, path, subdirectories=subdirectories)
        self._run_post_worker_refresh(refresh_local, refresh_remote)
        if cleanup and not self._close_after_stop:
            cleanup(worker, exit_code)
        if after_finish and not self._close_after_stop:
            after_finish(worker, exit_code)

    def _run_post_worker_refresh(self, refresh_local: bool, refresh_remote: bool) -> None:
        self.current_refresh_local = False
        self.current_refresh_remote = False
        if refresh_local:
            self._refresh_local_tree()
        if refresh_remote:
            self._refresh_remote_table(force=True)

    def _cancel_current(self) -> None:
        if isinstance(
            self.current_worker,
            (CommandWorker, SshTestWorker, RecallMediciWorker, FolderScanWorker, UploadAnalysisWorker, PackagedUploadWorker, LocalDeleteWorker, LocalMoveWorker, TapeStatusWorker, BunyaSubmitWorker),
        ):
            if isinstance(self.current_worker, (LocalDeleteWorker, LocalMoveWorker)):
                self._append_log("\nThe current local filesystem operation cannot be interrupted safely.\n")
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
        active_threads = [
            thread
            for thread in (self.current_thread, self.remote_thread, self.compare_thread, self.collection_pane.list_thread)
            if thread and thread.isRunning()
        ]
        if not active_threads:
            self._close_after_stop = False
            self.remote_folder_loader.shutdown()
            self.collection_pane.folder_loader.shutdown()
            event.accept()
            return
        if isinstance(self.current_worker, (LocalDeleteWorker, LocalMoveWorker)):
            QMessageBox.information(
                self,
                "Local filesystem operation running",
                "Please wait for the current local filesystem operation to finish before closing the app.",
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
        self._pending_remote_path = None
        self.current_after_finish = None
        self.transfer_status_label.setText("Stopping active operations before closing...")
        if self.current_worker and hasattr(self.current_worker, "cancel"):
            self.current_worker.cancel()
        if self.remote_worker:
            self.remote_worker.cancel()
        if self.compare_worker:
            self.compare_worker.cancel()
        self.collection_pane.cancel()
        event.ignore()
        QTimer.singleShot(100, self._close_when_operations_stop)

    def _close_when_operations_stop(self) -> None:
        if not self._close_after_stop:
            return
        active = any(
            thread and thread.isRunning()
            for thread in (self.current_thread, self.remote_thread, self.compare_thread, self.collection_pane.list_thread)
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
        self.local_move_button.setEnabled(not running)
        self.local_delete_button.setEnabled(not running)
        self.remote_new_folder_button.setEnabled(not running and self.remote_thread is None)
        self.remote_rename_button.setEnabled(not running and self.remote_thread is None)
        self.remote_move_button.setEnabled(not running and self.remote_thread is None)
        self.remote_delete_button.setEnabled(not running and self.remote_thread is None)
        self.dry_run_button.setEnabled(not running)
        self.download_dry_run_button.setEnabled(not running)
        self.build_selection_button.setEnabled(not running and self.compare_thread is None)
        self.upload_button.setEnabled(not running)
        self.recall_button.setEnabled(not running)
        self.tape_status_button.setEnabled(not running)
        self.download_button.setEnabled(not running)
        self.transfer_scope_combo.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self._update_mode_controls()
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
        self.remote_move_button.setEnabled(not busy and self.current_thread is None)
        self.remote_delete_button.setEnabled(not busy and self.current_thread is None)
        self.transfer_scope_combo.setEnabled(not busy and self.current_thread is None and self.compare_thread is None)
        self.ssh_button.setEnabled(not busy and self.current_thread is None and self.compare_thread is None)
        self.dry_run_button.setEnabled(not busy and self.current_thread is None and self.compare_thread is None)
        self.download_dry_run_button.setEnabled(not busy and self.current_thread is None and self.compare_thread is None)
        self.upload_button.setEnabled(not busy and self.current_thread is None and self.compare_thread is None)
        self.download_button.setEnabled(not busy and self.current_thread is None and self.compare_thread is None)
        self.build_selection_button.setEnabled(not busy and self.current_thread is None and self.compare_thread is None)
        self.recall_button.setEnabled(not busy and self.current_thread is None and self.compare_thread is None)
        self.tape_status_button.setEnabled(not busy and self.current_thread is None and self.compare_thread is None)
        self._update_mode_controls()
        if not busy:
            self._update_transfer_scope_ui()

    def _set_compare_running(self, running: bool) -> None:
        self.profile_box.setEnabled(not running and self.current_thread is None and self.remote_thread is None)
        self.remote_path_edit.setEnabled(not running and self.current_thread is None and self.remote_thread is None)
        self.ssh_button.setEnabled(not running and self.current_thread is None)
        self.dry_run_button.setEnabled(not running and self.current_thread is None)
        self.download_dry_run_button.setEnabled(not running and self.current_thread is None)
        self.build_selection_button.setEnabled(not running and self.current_thread is None)
        self.upload_button.setEnabled(not running and self.current_thread is None)
        self.local_new_folder_button.setEnabled(not running and self.current_thread is None)
        self.local_rename_button.setEnabled(not running and self.current_thread is None)
        self.local_move_button.setEnabled(not running and self.current_thread is None)
        self.local_delete_button.setEnabled(not running and self.current_thread is None)
        self.recall_button.setEnabled(not running and self.current_thread is None)
        self.tape_status_button.setEnabled(not running and self.current_thread is None)
        self.remote_new_folder_button.setEnabled(not running and self.current_thread is None and self.remote_thread is None)
        self.remote_rename_button.setEnabled(not running and self.current_thread is None and self.remote_thread is None)
        self.remote_move_button.setEnabled(not running and self.current_thread is None and self.remote_thread is None)
        self.remote_delete_button.setEnabled(not running and self.current_thread is None and self.remote_thread is None)
        self.download_button.setEnabled(not running and self.current_thread is None)
        self.transfer_scope_combo.setEnabled(not running and self.current_thread is None)
        self.stop_button.setEnabled(running or self.current_thread is not None)
        self._update_mode_controls()

    def _update_transfer_scope_ui(self, *_args) -> None:
        if self._collection_mode():
            self._update_collection_scope_ui()
            return
        selected_scope = str(self.transfer_scope_combo.currentData()) == "selected"
        local_items = self._selected_local_paths() if selected_scope else []
        remote_files = self._selected_remote_files() if selected_scope else []
        if selected_scope:
            self.upload_button.setText(f"Upload selected items ({len(local_items):,})")
            self.download_button.setText(f"Download selected files ({len(remote_files):,})")
            self.dry_run_button.setText(f"Compare selected items ({len(local_items):,})")
            self.download_dry_run_button.setText(f"Compare selected files ({len(remote_files):,})")
            self.selection_status_label.setText(
                f"Selected: {len(local_items):,} local item(s) · {len(remote_files):,} remote file(s)"
            )
        else:
            self.upload_button.setText("Upload folder")
            self.download_button.setText("Download folder")
            self.dry_run_button.setText("Compare upload folder")
            self.download_dry_run_button.setText("Compare download folder")
            self.selection_status_label.setText("Current folder")

        idle = self.current_thread is None and self.compare_thread is None and self.remote_thread is None
        self.upload_button.setEnabled(idle and (not selected_scope or bool(local_items)))
        self.dry_run_button.setEnabled(idle and (not selected_scope or bool(local_items)))
        self.download_button.setEnabled(idle and (not selected_scope or bool(remote_files)))
        self.download_dry_run_button.setEnabled(idle and (not selected_scope or bool(remote_files)))

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
        setup_state = "profile needs a username" if not profile.username else "profile ready to test"
        self.status_label.setText(f"Setup: {setup_state} | Tools: rsync {rsync_state} | SSH {ssh_state}")
        self.status_label.setToolTip(f"rsync: {profile.rsync_path}\nSSH: {self.detected_ssh}")


def show() -> int:
    app = QApplication([])
    window = MainWindow()
    window.show()
    return app.exec()
