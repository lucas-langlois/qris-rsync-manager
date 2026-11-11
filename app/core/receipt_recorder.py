"""Write transfer receipts into the synced folders.

The receipt lives in a hidden ``.qris-receipts`` folder inside the folder that
was synced: locally, and for completed uploads also on QRIScloud. Paths in the
receipt are relative to that synced folder, so ``sha256sum -c`` works from it
on either side. Downloads write locally only, keeping QRIScloud read-only.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from .profiles import Profile
from .receipts import (
    RECEIPT_DIR_NAME,
    CancelSignal,
    ReceiptCancelled,
    ReceiptFile,
    TransferCollector,
    app_version,
    build_receipt,
    hash_transferred_files,
    remote_receipt_dir,
    utc_iso,
    utc_stamp,
    write_receipt,
)
from .rsync_command import build_rsync_command


@dataclass
class ReceiptPhase:
    """One rsync pass: the folders its reported names are relative to."""

    collector: TransferCollector
    local_base: Path
    remote_base: str


@dataclass
class ReceiptPlan:
    """*local_root* and *remote_root* are the synced folders that hold the receipt."""

    direction: str
    local_root: Path
    remote_root: str
    started_utc: str = field(default_factory=utc_iso)
    phases: list[ReceiptPhase] = field(default_factory=list)

    def add_phase(self, local_base: str | Path, remote_base: str) -> TransferCollector:
        collector = TransferCollector(self.direction)
        self.phases.append(ReceiptPhase(collector, Path(local_base), remote_base))
        return collector


def upload_local_base(local_path: str | Path, *, files_from: bool, include_source_directory: bool) -> Path:
    """Return the folder that rsync's reported upload names are relative to."""
    path = Path(local_path).expanduser()
    if files_from:
        return path
    if path.is_file() or include_source_directory:
        return path.parent
    return path


def upload_receipt_roots(
    local_path: str | Path,
    remote_path: str,
    *,
    files_from: bool,
    include_source_directory: bool,
) -> tuple[Path, str]:
    """Return the (local, remote) synced folders for an upload's receipt."""
    path = Path(local_path).expanduser()
    remote = remote_path.rstrip("/") or "/"
    if not files_from and include_source_directory and path.is_dir():
        return path, f"{remote.rstrip('/')}/{path.name}"
    if not files_from and path.is_file():
        return path.parent, remote
    return path, remote


def record_transfer_receipt(
    plan: ReceiptPlan,
    profile: Profile,
    *,
    completed: bool,
    emit: Callable[[str], None],
    run_command: Callable[[list[str]], int] | None = None,
    ssh_path: str | None = None,
    batch_mode: bool = True,
    cancel_event: CancelSignal | None = None,
) -> list[Path]:
    """Hash transferred files and write the receipt. Never raises.

    Returns the local receipt files written. *run_command* uploads the receipt
    to QRIScloud; it is used only for completed uploads.
    """
    try:
        names_by_phase = [phase.collector.finish() for phase in plan.phases]
        if not any(names_by_phase):
            emit("No files were transferred, so no receipt was written.\n")
            return []
        count = sum(len(names) for names in names_by_phase)
        emit(f"Computing SHA-256 checksums of {count:,} transferred file(s) for the receipt...\n")
        files: list[ReceiptFile] = []
        for phase, names in zip(plan.phases, names_by_phase):
            hashed = hash_transferred_files(
                names,
                phase.local_base,
                phase.remote_base,
                cancel_event=cancel_event,
                progress=lambda message: emit(message + "\n"),
            )
            files.extend(replace(item, path=_relative_to_root(item, plan)) for item in hashed)
        clean = profile.normalized()
        receipt = build_receipt(
            direction=plan.direction,
            status="complete" if completed else "incomplete",
            profile_name=clean.name,
            collection_id=clean.collection_id,
            host=clean.host,
            local_source=str(plan.local_root),
            remote_destination=plan.remote_root,
            started_utc=plan.started_utc,
            files=files,
            app_version=app_version(),
        )
        base_name = f"{utc_stamp()}_{plan.direction}" + ("" if completed else "_incomplete")
        written: list[Path] = []
        try:
            written = write_receipt(receipt, files, plan.local_root / RECEIPT_DIR_NAME, base_name)
            base_name = written[0].stem
            emit(f"Receipt saved locally: {written[0]}\n")
        except OSError as exc:
            emit(f"Warning: could not save the receipt in {plan.local_root}: {exc}\n")
        if plan.direction == "upload" and completed and run_command is not None:
            try:
                _upload_receipt(receipt, files, base_name, plan, clean, emit, run_command, ssh_path, batch_mode)
            except Exception as exc:
                emit(f"Warning: the receipt could not be uploaded to QRIScloud: {exc}\n")
        elif plan.direction == "upload" and not completed:
            emit("The transfer did not complete, so the receipt was kept locally only.\n")
        missing = sum(1 for item in files if item.sha256 is None)
        if missing:
            emit(f"Warning: {missing:,} file(s) could not be hashed; see the receipt for details.\n")
        return written
    except ReceiptCancelled:
        emit("Receipt not written: checksum computation was cancelled.\n")
    except Exception as exc:  # A receipt problem must never fail the transfer itself.
        emit(f"Warning: could not write the transfer receipt: {exc}\n")
    return []


def _relative_to_root(item: ReceiptFile, plan: ReceiptPlan) -> str:
    """Express a file's path relative to the synced folder holding the receipt."""
    if plan.direction == "upload":
        root = plan.remote_root.rstrip("/") + "/"
        if item.remote_path.startswith(root):
            return item.remote_path[len(root):]
    else:
        try:
            return Path(item.local_path).relative_to(plan.local_root).as_posix()
        except ValueError:
            pass
    return item.path


def _upload_receipt(receipt, files, base_name, plan, profile, emit, run_command, ssh_path, batch_mode) -> None:
    staging = Path(tempfile.mkdtemp(prefix="qris-receipt-"))
    try:
        write_receipt(receipt, files, staging, base_name)
        destination = remote_receipt_dir(plan.remote_root)
        command = build_rsync_command(
            profile,
            staging,
            remote_path=destination,
            ssh_path=ssh_path,
            batch_mode=batch_mode,
            direction="upload",
        )
        emit(f"Uploading receipt to {destination}/...\n")
        code = run_command(command)
        if code == 0:
            emit(f"Receipt saved on QRIScloud: {destination}/{base_name}.json\n")
        else:
            emit(
                f"Warning: the receipt could not be uploaded to QRIScloud (rsync exit code {code}). "
                "The local receipt is still available.\n"
            )
    finally:
        shutil.rmtree(staging, ignore_errors=True)
