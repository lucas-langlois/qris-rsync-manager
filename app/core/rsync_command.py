from __future__ import annotations

import shlex
from pathlib import Path

from .paths import (
    detect_ssh,
    directory_source_for_rsync,
    file_source_for_rsync,
    path_for_rsync,
    ssh_host_key_options,
)
from .profiles import Profile


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
) -> list[str]:
    clean = profile.normalized()
    local_path = Path(local_folder).expanduser()
    command = [clean.rsync_path, *DEFAULT_RSYNC_OPTIONS]
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
    if ignore_times:
        command.append("--ignore-times")
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


def _validated_remote_path(path: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in path):
        raise ValueError("Remote paths cannot contain control characters.")
    return path
