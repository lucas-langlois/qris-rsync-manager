from __future__ import annotations

import shlex
from pathlib import Path

from .paths import detect_ssh, directory_source_for_rsync, path_for_rsync
from .profiles import Profile


DEFAULT_RSYNC_OPTIONS = [
    "-a",
    "-v",
    "-h",
    "--progress",
    "--partial",
    "-W",
    "--outbuf=N",
    "--info=progress2",
    "--human-readable",
]

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
    path = (remote_path or clean.remote_path).strip() or f"/data/{clean.collection_id}"
    return f"{clean.username}@{clean.host}:{path.rstrip('/')}/"


def local_destination(path: str | Path, rsync_path: str | Path | None = None) -> str:
    return directory_source_for_rsync(path, rsync_path)


def build_ssh_transport(profile: Profile, ssh_path: str | None = None, batch_mode: bool = True) -> str:
    clean = profile.normalized()
    executable = path_for_rsync(ssh_path or detect_ssh(), clean.rsync_path)
    args = [
        executable,
        "-p",
        str(clean.ssh_port),
        *SSH_KEEPALIVE_OPTIONS,
    ]
    if batch_mode:
        args.extend(["-o", "BatchMode=yes"])
    if clean.ssh_key_path:
        args.extend(["-i", path_for_rsync(clean.ssh_key_path, clean.rsync_path)])
    return shlex.join(args)


def validate_transfer_inputs(profile: Profile, local_folder: str | Path, remote_path: str | None = None) -> list[str]:
    clean = profile.normalized()
    errors: list[str] = []
    if not clean.username:
        errors.append("Username is required.")
    if not clean.host:
        errors.append("Host is required.")
    if not clean.rsync_path:
        errors.append("rsync executable path is required.")
    if not Path(local_folder).expanduser().is_dir():
        errors.append("Local folder must exist.")
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
    direction: str = "upload",
) -> list[str]:
    clean = profile.normalized()
    command = [clean.rsync_path, *DEFAULT_RSYNC_OPTIONS]
    if dry_run:
        command.append("--dry-run")
        command.extend(DRY_RUN_COMPARE_OPTIONS)
    if files_from:
        command.append(f"--files-from={path_for_rsync(files_from, clean.rsync_path)}")
    command.extend(["-e", build_ssh_transport(clean, ssh_path=ssh_path, batch_mode=batch_mode)])
    if direction == "upload":
        command.append(directory_source_for_rsync(local_folder, clean.rsync_path))
        command.append(remote_target(clean, remote_path=remote_path))
    elif direction == "download":
        command.append(remote_target(clean, remote_path=remote_path))
        command.append(local_destination(local_folder, clean.rsync_path))
    else:
        raise ValueError("direction must be 'upload' or 'download'.")
    return command
