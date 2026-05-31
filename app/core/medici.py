from __future__ import annotations

import shlex

from .profiles import Profile
from .remote_dirs import build_remote_ssh_base


def medici_path_for_remote_path(remote_path: str) -> str:
    clean = remote_path.strip().rstrip("/")
    if not clean:
        raise ValueError("Remote path is required.")
    if clean.startswith("/QRISdata/"):
        return clean
    if clean.startswith("/RDS/"):
        return clean
    if clean.startswith("/data/"):
        return "/QRISdata/" + clean.removeprefix("/data/")
    raise ValueError("MeDiCI recall paths must start with /data/, /QRISdata/, or /RDS/.")


def recall_file_path_for_remote_path(remote_path: str) -> str:
    clean = remote_path.strip().rstrip("/")
    if not clean:
        raise ValueError("Remote file path is required.")
    if clean.startswith(("/data/", "/QRISdata/", "/RDS/")):
        return clean
    raise ValueError("MeDiCI recall file paths must start with /data/, /QRISdata/, or /RDS/.")


def build_recall_medici_command(
    profile: Profile,
    remote_path: str,
    ssh_path: str | None = None,
    batch_mode: bool = True,
) -> list[str]:
    medici_path = medici_path_for_remote_path(remote_path)
    remote_command = (
        "if command -v recall_medici >/dev/null 2>&1; then "
        f"recall_medici {shlex.quote(medici_path)}; "
        "else echo 'recall_medici was not found on this host. Try a QRIScloud/RCC HPC login node or contact UQ ITS.' >&2; "
        "exit 127; fi"
    )
    return [*build_remote_ssh_base(profile, ssh_path=ssh_path, batch_mode=batch_mode), remote_command]


def build_recall_medici_files_command(
    profile: Profile,
    remote_files: list[str],
    ssh_path: str | None = None,
    batch_mode: bool = True,
    batch_size: int = 25,
) -> list[str]:
    if not remote_files:
        raise ValueError("At least one remote file is required.")
    medici_files = [recall_file_path_for_remote_path(path) for path in remote_files]
    batches: list[str] = []
    size = max(1, int(batch_size))
    for index in range(0, len(medici_files), size):
        batch = medici_files[index : index + size]
        batches.append("recall_medici " + " ".join(shlex.quote(path) for path in batch))
    remote_command = (
        "if command -v recall_medici >/dev/null 2>&1; then "
        + "; ".join(batches)
        + "; else echo 'recall_medici was not found on this host. Try a QRIScloud/RCC HPC login node or contact UQ ITS.' >&2; "
        "exit 127; fi"
    )
    return [*build_remote_ssh_base(profile, ssh_path=ssh_path, batch_mode=batch_mode), remote_command]
