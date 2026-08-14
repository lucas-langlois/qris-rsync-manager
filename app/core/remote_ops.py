from __future__ import annotations

import posixpath
import shlex

from .profiles import Profile
from .remote_dirs import build_remote_ssh_base


def remote_child_path(parent_path: str, name: str) -> str:
    clean_parent = clean_remote_path(parent_path)
    clean_name = name.strip().strip("/")
    if not clean_name:
        raise ValueError("Name is required.")
    if "/" in clean_name:
        raise ValueError("Name must not contain '/'.")
    return posixpath.join(clean_parent, clean_name)


def build_remote_mkdir_command(
    profile: Profile,
    parent_path: str,
    name: str,
    ssh_path: str | None = None,
    batch_mode: bool = True,
) -> list[str]:
    target = remote_child_path(parent_path, name)
    remote_command = f"mkdir -p -- {shlex.quote(target)}"
    return [*build_remote_ssh_base(profile, ssh_path=ssh_path, batch_mode=batch_mode), remote_command]


def build_remote_move_command(
    profile: Profile,
    source_path: str,
    destination_path: str,
    ssh_path: str | None = None,
    batch_mode: bool = True,
) -> list[str]:
    source = clean_remote_path(source_path)
    destination = clean_remote_path(destination_path)
    if source == destination:
        raise ValueError("Source and destination are the same.")
    if is_dangerous_remote_path(source):
        raise ValueError(f"Refusing to move unsafe remote path: {source}")
    exists_message = shlex.quote(f"Destination already exists: {destination}")
    remote_command = (
        f"if [ -e {shlex.quote(destination)} ]; then "
        f"echo {exists_message} >&2; exit 73; "
        f"else mv -- {shlex.quote(source)} {shlex.quote(destination)}; fi"
    )
    return [*build_remote_ssh_base(profile, ssh_path=ssh_path, batch_mode=batch_mode), remote_command]


def build_remote_move_into_command(
    profile: Profile,
    source_paths: list[str],
    destination_directory: str,
    collection_root: str,
    ssh_path: str | None = None,
    batch_mode: bool = True,
) -> list[str]:
    root = clean_remote_path(collection_root)
    destination_dir = clean_remote_path(destination_directory)
    sources = [clean_remote_path(path) for path in source_paths]
    if not sources:
        raise ValueError("At least one remote source path is required.")
    if not _is_within_remote_root(destination_dir, root):
        raise ValueError("Remote destination must stay inside the active collection.")
    destinations: list[str] = []
    for source in sources:
        if source == root or not _is_within_remote_root(source, root):
            raise ValueError("Remote source must stay inside the active collection and cannot be its root.")
        if destination_dir == source or destination_dir.startswith(source.rstrip("/") + "/"):
            raise ValueError("A remote folder cannot be moved into itself or one of its subfolders.")
        destinations.append(posixpath.join(destination_dir, posixpath.basename(source)))
    if len(set(destinations)) != len(destinations):
        raise ValueError("Selected remote items would have duplicate destination names.")
    unchanged = [source for source, destination in zip(sources, destinations) if source == destination]
    if unchanged:
        raise ValueError("One or more selected items are already in that remote folder.")

    checks = " || ".join(f"[ -e {shlex.quote(path)} ]" for path in destinations)
    message = shlex.quote("One or more remote destinations already exist.")
    moves = "; ".join(
        f"mv -n -- {shlex.quote(source)} {shlex.quote(destination)}; "
        f"if [ -e {shlex.quote(source)} ] || [ -L {shlex.quote(source)} ]; then "
        f"echo {message} >&2; exit 73; fi"
        for source, destination in zip(sources, destinations)
    )
    remote_command = f"if {checks}; then echo {message} >&2; exit 73; fi; {moves}"
    return [*build_remote_ssh_base(profile, ssh_path=ssh_path, batch_mode=batch_mode), remote_command]


def build_remote_delete_command(
    profile: Profile,
    paths: list[str],
    ssh_path: str | None = None,
    batch_mode: bool = True,
) -> list[str]:
    clean_paths = [clean_remote_path(path) for path in paths]
    if not clean_paths:
        raise ValueError("At least one remote path is required.")
    unsafe = [path for path in clean_paths if is_dangerous_remote_path(path)]
    if unsafe:
        raise ValueError(f"Refusing to delete unsafe remote path: {unsafe[0]}")
    remote_command = "rm -rf -- " + " ".join(shlex.quote(path) for path in clean_paths)
    return [*build_remote_ssh_base(profile, ssh_path=ssh_path, batch_mode=batch_mode), remote_command]


def clean_remote_path(remote_path: str) -> str:
    raw = remote_path.strip()
    if any(part in {".", ".."} for part in raw.split("/")):
        raise ValueError("Remote path must not contain '.' or '..' components.")
    if raw == "/":
        return "/"
    clean = raw.rstrip("/")
    if not clean:
        raise ValueError("Remote path is required.")
    if not clean.startswith("/"):
        raise ValueError("Remote path must start with '/'.")
    normalized = posixpath.normpath(clean)
    if not normalized.startswith("/"):
        raise ValueError("Remote path must start with '/'.")
    return normalized or "/"


def is_dangerous_remote_path(remote_path: str) -> bool:
    clean = clean_remote_path(remote_path)
    if clean == "/":
        return True
    parts = [part for part in clean.split("/") if part]
    if parts in (["data"], ["QRISdata"], ["RDS"]):
        return True
    if len(parts) == 2 and parts[0] in {"data", "QRISdata", "RDS"} and parts[1].upper().startswith("Q"):
        return True
    return False


def _is_within_remote_root(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")
