from __future__ import annotations

import shlex
from pathlib import Path

from .paths import (
    detect_ssh,
    directory_source_for_rsync,
    file_source_for_rsync,
    path_for_rsync,
    WINDOWS_SYSTEM_FOLDER_NAMES,
    ssh_host_key_options,
)
from .profiles import Profile
from .receipts import RECEIPT_DIR_NAME


DEFAULT_RSYNC_OPTIONS = [
    "-a",
    "-v",
    "-h",
    "--progress",
    "--partial",
    "-W",
    "--timeout=120",
    "--outbuf=N",
    "--info=progress2",
    "--human-readable",
]

REMOTE_SHELL_SPECIALS = frozenset(" \t'\"\\$`;&|<>*?[](){}!#~")

SSH_KEEPALIVE_OPTIONS = [
    "-o",
    "ServerAliveInterval=60",
    "-o",
    "ServerAliveCountMax=10",
]

DRY_RUN_COMPARE_OPTIONS = [
    "--itemize-changes",
]


def remote_target(profile: Profile, remote_path: str | None = None) -> str:
    clean = profile.normalized()
    path = _validated_remote_path((remote_path or clean.remote_path).strip() or f"/data/{clean.collection_id}")
    return f"{clean.username}@{clean.host}:{path.rstrip('/')}/"


def remote_source(profile: Profile, remote_path: str | None = None, is_file: bool = False) -> str:
    clean = profile.normalized()
    path = _validated_remote_path((remote_path or clean.remote_path).strip() or f"/data/{clean.collection_id}")
    if is_file:
        return f"{clean.username}@{clean.host}:{path.rstrip('/')}"
    return remote_target(clean, remote_path=path)


def local_destination(path: str | Path, rsync_path: str | Path | None = None) -> str:
    return directory_source_for_rsync(path, rsync_path)


def build_ssh_transport(profile: Profile, ssh_path: str | None = None, batch_mode: bool = True) -> str:
    clean = profile.normalized()
    source_executable = ssh_path or detect_ssh()
    executable = path_for_rsync(source_executable, clean.rsync_path)
    args = [
        executable,
        "-p",
        str(clean.ssh_port),
        *SSH_KEEPALIVE_OPTIONS,
        *ssh_host_key_options(source_executable),
    ]
    if batch_mode:
        args.extend(["-o", "BatchMode=yes"])
    if clean.ssh_key_path:
        args.extend(["-i", path_for_rsync(clean.ssh_key_path, clean.rsync_path)])
    return shlex.join(args)


def validate_transfer_inputs(
    profile: Profile,
    local_folder: str | Path,
    remote_path: str | None = None,
    direction: str = "upload",
) -> list[str]:
    clean = profile.normalized()
    errors: list[str] = []
    if not clean.username:
        errors.append("Username is required.")
    if not clean.host:
        errors.append("Host is required.")
    if not clean.rsync_path:
        errors.append("rsync executable path is required.")
    local_path = Path(local_folder).expanduser()
    if direction == "upload":
        if not local_path.exists():
            errors.append("Local file or folder must exist.")
    elif direction == "download":
        if not local_path.is_dir():
            errors.append("Local download folder must exist.")
    else:
        errors.append("direction must be 'upload' or 'download'.")
    if not (remote_path or clean.remote_path).strip():
        errors.append("Remote path is required.")
    return errors


def build_rsync_command(
    profile: Profile,
    local_folder: str | Path,
    remote_path: str | None = None,
    dry_run: bool = False,
    ssh_path: str | None = None,
    batch_mode: bool = True,
    files_from: str | Path | None = None,
    exclude_from: str | Path | None = None,
    direction: str = "upload",
    remote_is_file: bool = False,
    include_source_directory: bool = False,
    ignore_times: bool = False,
    resume_partial: bool = False,
    itemize: bool = False,
) -> list[str]:
    clean = profile.normalized()
    local_path = Path(local_folder).expanduser()
    options = [option for option in DEFAULT_RSYNC_OPTIONS if not (resume_partial and option == "-W")]
    command = [clean.rsync_path, *options]
    if resume_partial:
        # Force rsync's block-matching algorithm on recovery attempts. Unlike
        # --append-verify, this also repairs a stale destination that is already
        # the same size as, or larger than, the source.
        command.append("--no-whole-file")
    effective_remote_path = _validated_remote_path(
        (remote_path or clean.remote_path).strip() or f"/data/{clean.collection_id}"
    )
    if any(character in REMOTE_SHELL_SPECIALS for character in effective_remote_path):
        # Keep normal collection paths compatible with older rsync receivers,
        # and request protected argument transport only when the path needs it.
        command.append("--protect-args")
    if dry_run:
        command.append("--dry-run")
        command.extend(DRY_RUN_COMPARE_OPTIONS)
    elif itemize:
        # Real transfers itemize so the app can record exactly which files
        # were sent or received in a transfer receipt.
        command.append("--itemize-changes")
    if ignore_times:
        command.append("--ignore-times")
    # Receipts are written into synced folders by the app itself; never sync
    # them as ordinary data, or they would be re-sent and listed as transfers.
    command.append(f"--exclude={RECEIPT_DIR_NAME}/")
    if direction == "upload":
        # File-server system folders are unreadable and never research data.
        command.extend(f"--exclude={name}/" for name in WINDOWS_SYSTEM_FOLDER_NAMES)
    if files_from:
        command.append(f"--files-from={path_for_rsync(files_from, clean.rsync_path)}")
    if exclude_from:
        command.append(f"--exclude-from={path_for_rsync(exclude_from, clean.rsync_path)}")
    command.extend(["-e", build_ssh_transport(clean, ssh_path=ssh_path, batch_mode=batch_mode)])
    if direction == "upload":
        if local_path.is_file() and not files_from:
            command.append(file_source_for_rsync(local_path, clean.rsync_path))
        elif include_source_directory and not files_from:
            command.append(file_source_for_rsync(local_path, clean.rsync_path))
        else:
            command.append(directory_source_for_rsync(local_folder, clean.rsync_path))
        command.append(remote_target(clean, remote_path=remote_path))
    elif direction == "download":
        command.append(remote_source(clean, remote_path=remote_path, is_file=remote_is_file))
        command.append(local_destination(local_folder, clean.rsync_path))
    else:
        raise ValueError("direction must be 'upload' or 'download'.")
    return command


# Collection-to-collection copies run rsync on the QRIScloud SSH host itself,
# so data never travels through the workstation. These flags are parsed by
# the server's rsync, not MSYS2's, so keep them to long-standing options.
# --no-o/--no-g: do not carry the source collection's owner/group into the
# destination collection; files are owned as for an upload by this account.
# No --timeout: a source file waiting on a Medici tape recall can block reads
# for minutes; the SSH keepalive still detects a dead connection.
COLLECTION_COPY_RSYNC_OPTIONS = [
    "-a",
    "--no-o",
    "--no-g",
    "-v",
    "-h",
    "--partial",
    "--outbuf=N",
    "--info=progress2",
]

COLLECTION_ROOT_PREFIXES = frozenset({"data", "QRISdata", "RDS"})

# QRIScloud storage metadata found in each collection root; not user data.
QRISCLOUD_SYSTEM_DIR = ".afm"


def collection_of_remote_path(remote_path: str) -> str:
    """Return the collection root (for example ``/data/Q0101``) holding a path.

    Raise ValueError when the path is not inside a QRISdata collection.
    """
    from .remote_ops import clean_remote_path

    clean = clean_remote_path(remote_path)
    parts = [part for part in clean.split("/") if part]
    if len(parts) < 2 or parts[0] not in COLLECTION_ROOT_PREFIXES or not parts[1].upper().startswith("Q"):
        raise ValueError(f"Path is not inside a QRISdata collection (for example /data/Q0101/...): {clean}")
    return f"/{parts[0]}/{parts[1]}"


def collection_key(remote_path: str) -> tuple[str, str]:
    """Return (collection id, path inside the collection) for a remote path.

    /data/Q0101/x and /QRISdata/Q0101/x name the same storage, so collections
    are compared by their ID, never by the path prefix. Raise ValueError when
    the path is not inside a collection.
    """
    from .remote_ops import clean_remote_path

    collection_of_remote_path(remote_path)
    parts = [part for part in clean_remote_path(remote_path).split("/") if part]
    return parts[1].upper(), "/".join(parts[2:])


def collection_copy_destinations(source_paths: list[str], destination_directory: str) -> list[str]:
    """Validate a collection copy and return where each source will land.

    Each source is copied *into* the destination folder, keeping its own name.
    """
    import posixpath

    from .remote_ops import clean_remote_path

    destination = clean_remote_path(_validated_remote_path(destination_directory))
    destination_id, _inside = collection_key(destination)
    sources = [clean_remote_path(_validated_remote_path(path)) for path in source_paths]
    if not sources:
        raise ValueError("Select at least one remote file or folder to copy.")
    results: list[str] = []
    for source in sources:
        # Copies are between collections only; reorganising inside one
        # collection is what Rename/move is for.
        # Comparing IDs also rules out copying a folder into itself through
        # the other prefix (/data/Q0101/a into /QRISdata/Q0101/a/sub).
        if collection_key(source)[0] == destination_id:
            raise ValueError(
                f"Source and destination are both in {destination_id}. "
                "Choose a different collection in one of the panes."
            )
        if destination == source or destination.startswith(source.rstrip("/") + "/"):
            raise ValueError(f"Cannot copy a folder into itself or one of its subfolders: {source}")
        landing = posixpath.join(destination, posixpath.basename(source))
        if landing == source:
            raise ValueError(f"Source and destination are the same: {source}")
        results.append(landing)
    if len(set(results)) != len(results):
        raise ValueError("Selected items would have duplicate names in the destination folder.")
    return results


def build_collection_copy_command(
    profile: Profile,
    source_paths: list[str],
    destination_directory: str,
    dry_run: bool = False,
    ssh_path: str | None = None,
    batch_mode: bool = True,
) -> list[str]:
    """Build an ssh command that runs rsync on the QRIScloud host to copy
    between collections. Copy only: never deletes or moves anything."""
    from .remote_dirs import build_remote_ssh_base
    from .remote_ops import clean_remote_path

    collection_copy_destinations(source_paths, destination_directory)
    sources = [clean_remote_path(path) for path in source_paths]
    destination = clean_remote_path(destination_directory)
    remote_args = ["rsync", *COLLECTION_COPY_RSYNC_OPTIONS]
    if dry_run:
        remote_args.append("--dry-run")
        remote_args.extend(DRY_RUN_COMPARE_OPTIONS)
    else:
        remote_args.append("--itemize-changes")
    remote_args.append(f"--exclude={RECEIPT_DIR_NAME}/")
    # Every collection root holds a root-owned, unreadable .afm folder
    # (QRIScloud storage metadata). Copying a whole collection would fail on
    # it with a permission error, so it is never copied.
    remote_args.append(f"--exclude={QRISCLOUD_SYSTEM_DIR}/")
    # "--" stops option parsing; sources have no trailing slash so each one
    # lands inside the destination under its own name.
    remote_args.append("--")
    remote_args.extend(sources)
    remote_args.append(destination.rstrip("/") + "/")
    return [*build_remote_ssh_base(profile, ssh_path=ssh_path, batch_mode=batch_mode), shlex.join(remote_args)]


def _validated_remote_path(path: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in path):
        raise ValueError("Remote paths cannot contain control characters.")
    return path
