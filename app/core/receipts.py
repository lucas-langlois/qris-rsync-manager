"""Transfer receipts: which files moved, when, and their SHA-256 checksums.

A receipt is written after every real upload or download that transferred at
least one file. It lists each transferred file with its size, modification time
and SHA-256, so a collection's history can be audited and any copy can be
checked later with ``sha256sum -c``.

Receipts live in a hidden ``.qris-receipts`` folder inside the synced folder:
locally, and for uploads also on QRIScloud. Downloads stay local-only so
QRIScloud remains read-only during a download.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Protocol

RECEIPT_DIR_NAME = ".qris-receipts"
RECEIPT_FORMAT = "qris-rsync-manager-receipt/1"
HASH_CHUNK_BYTES = 4 * 1_048_576
_ESCAPE = re.compile(r"\\#([0-7]{3})")


class CancelSignal(Protocol):
    def is_set(self) -> bool: ...


class ReceiptCancelled(RuntimeError):
    """Raised when checksum computation is cancelled."""


@dataclass(frozen=True)
class ReceiptFile:
    path: str
    remote_path: str
    local_path: str
    bytes: int | None
    modified_utc: str | None
    sha256: str | None
    note: str = ""


@dataclass
class TransferCollector:
    """Collect files rsync reports as transferred from ``--itemize-changes`` output.

    Uploads report sent files as ``<f...``; downloads report received files as
    ``>f...``. Output arrives in arbitrary chunks, so partial lines are kept
    until their terminator arrives.
    """

    direction: str
    names: dict[str, None] = field(default_factory=dict)
    _pending: str = ""

    def feed(self, text: str) -> None:
        data = self._pending + text
        lines = re.split(r"[\r\n]", data)
        self._pending = lines.pop()
        for line in lines:
            self._parse(line)

    def finish(self) -> list[str]:
        if self._pending:
            self._parse(self._pending)
            self._pending = ""
        return list(self.names)

    def _parse(self, line: str) -> None:
        marker = "<" if self.direction == "upload" else ">"
        # Itemize strings are 11 characters, e.g. "<f+++++++++ path" or "<f.st...... path".
        if len(line) > 12 and line[0] == marker and line[1] == "f" and line[11] == " ":
            name = unescape_rsync_name(line[12:])
            if name:
                self.names.pop(name, None)
                self.names[name] = None


def unescape_rsync_name(name: str) -> str:
    """Undo rsync's ``\\#ooo`` octal escaping of unprintable filename bytes."""
    if "\\#" not in name:
        return name
    output = bytearray()
    index = 0
    while index < len(name):
        match = _ESCAPE.match(name, index)
        if match:
            output.append(int(match.group(1), 8))
            index = match.end()
        else:
            output.extend(name[index].encode("utf-8"))
            index += 1
    return output.decode("utf-8", errors="replace")


def utc_stamp(moment: datetime | None = None) -> str:
    return (moment or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")


def utc_iso(moment: datetime | None = None) -> str:
    return (moment or datetime.now(timezone.utc)).replace(microsecond=0).isoformat()


def hash_transferred_files(
    names: Iterable[str],
    local_base: str | Path,
    remote_base: str,
    *,
    relative_prefix: str = "",
    cancel_event: CancelSignal | None = None,
    progress: Callable[[str], None] | None = None,
) -> list[ReceiptFile]:
    """Hash each transferred file on the local side.

    *names* are paths relative to *local_base* and *remote_base*, as rsync
    reports them. *relative_prefix* is prepended to the recorded path when the
    receipt root is above *remote_base* (packaged archive payloads).
    """
    base = Path(local_base)
    remote_root = remote_base.rstrip("/") or "/"
    files: list[ReceiptFile] = []
    name_list = list(names)
    total = len(name_list)
    last_report = time.monotonic()
    for index, name in enumerate(name_list, start=1):
        if cancel_event and cancel_event.is_set():
            raise ReceiptCancelled("Receipt checksums were cancelled.")
        relative = str(PurePosixPath(relative_prefix) / name) if relative_prefix else name
        local_path = base.joinpath(*PurePosixPath(name).parts)
        remote_path = f"{remote_root.rstrip('/')}/{name}"
        try:
            info = os.stat(local_path)
            digest = _sha256(local_path, cancel_event)
            modified = datetime.fromtimestamp(info.st_mtime_ns / 1_000_000_000, timezone.utc)
            files.append(
                ReceiptFile(relative, remote_path, str(local_path), info.st_size, utc_iso(modified), digest)
            )
        except ReceiptCancelled:
            raise
        except OSError as exc:
            files.append(
                ReceiptFile(relative, remote_path, str(local_path), None, None, None, f"not hashed: {exc.strerror or exc}")
            )
        now = time.monotonic()
        if progress and (now - last_report >= 5 or index == total):
            last_report = now
            progress(f"Receipt checksums: {index:,} of {total:,} files")
    return files


def build_receipt(
    *,
    direction: str,
    status: str,
    profile_name: str,
    collection_id: str,
    host: str,
    local_source: str,
    remote_destination: str,
    started_utc: str,
    files: list[ReceiptFile],
    app_version: str,
) -> dict:
    return {
        "format": RECEIPT_FORMAT,
        "status": status,
        "direction": direction,
        "started_utc": started_utc,
        "finished_utc": utc_iso(),
        "profile": profile_name,
        "collection_id": collection_id,
        "host": host,
        "local": local_source,
        "remote": remote_destination,
        "app_version": app_version,
        "checksum_algorithm": "sha256",
        "file_count": len(files),
        "total_bytes": sum(item.bytes or 0 for item in files),
        "files": [
            {
                "path": item.path,
                "remote_path": item.remote_path,
                "local_path": item.local_path,
                "bytes": item.bytes,
                "modified_utc": item.modified_utc,
                "sha256": item.sha256,
                **({"note": item.note} if item.note else {}),
            }
            for item in files
        ],
    }


def sha256sum_text(files: list[ReceiptFile]) -> str:
    """Return ``sha256sum``-compatible lines, paths relative to the receipt root."""
    lines = []
    for item in files:
        if not item.sha256:
            continue
        path = item.path
        if "\\" in path or "\n" in path:
            # GNU coreutils convention for names containing backslash or newline.
            escaped = path.replace("\\", "\\\\").replace("\n", "\\n")
            lines.append(f"\\{item.sha256}  {escaped}")
        else:
            lines.append(f"{item.sha256}  {path}")
    return "\n".join(lines) + ("\n" if lines else "")


def write_receipt(receipt: dict, files: list[ReceiptFile], directory: str | Path, base_name: str) -> list[Path]:
    """Write ``<base_name>.json`` and ``<base_name>.sha256``; never overwrite."""
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    name = base_name
    suffix = 1
    while (folder / f"{name}.json").exists() or (folder / f"{name}.sha256").exists():
        suffix += 1
        name = f"{base_name}-{suffix}"
    json_path = folder / f"{name}.json"
    sums_path = folder / f"{name}.sha256"
    json_path.write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    sums_path.write_text(sha256sum_text(files), encoding="utf-8", newline="\n")
    return [json_path, sums_path]


def remote_receipt_dir(remote_destination: str) -> str:
    return f"{remote_destination.rstrip('/')}/{RECEIPT_DIR_NAME}"


def app_version() -> str:
    # Read from the package itself so source runs and PyInstaller builds agree;
    # installed-package metadata can be stale or missing.
    from app import __version__

    return __version__


def _sha256(path: Path, cancel_event: CancelSignal | None) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            if cancel_event and cancel_event.is_set():
                raise ReceiptCancelled("Receipt checksums were cancelled.")
            chunk = handle.read(HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()
