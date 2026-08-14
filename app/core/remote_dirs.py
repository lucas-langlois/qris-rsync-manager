from __future__ import annotations

import shlex
from collections import OrderedDict
from dataclasses import dataclass

from .paths import detect_ssh, ssh_host_key_options
from .profiles import Profile
from .rsync_command import SSH_KEEPALIVE_OPTIONS


@dataclass
class RemoteEntry:
    kind: str
    name: str
    size: int
    modified: str
    path: str

    @property
    def is_dir(self) -> bool:
        return self.kind == "d"

    @property
    def type_label(self) -> str:
        if self.kind == "d":
            return "File Folder"
        if self.kind == "l":
            return "Symbolic Link"
        return "File"

    @property
    def size_label(self) -> str:
        if self.is_dir:
            return ""
        return format_bytes(self.size)


class RemoteDirectoryCache:
    """Bounded, session-only cache of remote directory listings."""

    def __init__(self, max_directories: int = 64, max_entries: int = 50_000) -> None:
        if max_directories < 1 or max_entries < 1:
            raise ValueError("Remote directory cache limits must be positive.")
        self.max_directories = max_directories
        self.max_entries = max_entries
        self._items: OrderedDict[tuple[tuple[object, ...], str], tuple[RemoteEntry, ...]] = OrderedDict()
        self._entry_count = 0

    def get(self, profile: Profile, remote_path: str) -> list[RemoteEntry] | None:
        key = (remote_cache_namespace(profile), _clean_remote_path(remote_path))
        entries = self._items.get(key)
        if entries is None:
            return None
        self._items.move_to_end(key)
        return list(entries)

    def put(self, profile: Profile, remote_path: str, entries: list[RemoteEntry]) -> None:
        key = (remote_cache_namespace(profile), _clean_remote_path(remote_path))
        previous = self._items.pop(key, None)
        if previous is not None:
            self._entry_count -= len(previous)
        if len(entries) > self.max_entries:
            return
        stored = tuple(entries)
        self._items[key] = stored
        self._entry_count += len(stored)
        while len(self._items) > self.max_directories or self._entry_count > self.max_entries:
            _, removed = self._items.popitem(last=False)
            self._entry_count -= len(removed)

    def invalidate(self, profile: Profile, remote_path: str, subdirectories: bool = False) -> None:
        namespace = remote_cache_namespace(profile)
        path = _clean_remote_path(remote_path)
        prefix = path.rstrip("/") + "/"
        keys = [
            key
            for key in self._items
            if key[0] == namespace and (key[1] == path or (subdirectories and key[1].startswith(prefix)))
        ]
        for key in keys:
            self._entry_count -= len(self._items.pop(key))

    def clear_profile(self, profile: Profile) -> None:
        namespace = remote_cache_namespace(profile)
        keys = [key for key in self._items if key[0] == namespace]
        for key in keys:
            self._entry_count -= len(self._items.pop(key))

    def clear(self) -> None:
        self._items.clear()
        self._entry_count = 0

    def __len__(self) -> int:
        return len(self._items)


def remote_cache_namespace(profile: Profile) -> tuple[object, ...]:
    clean = profile.normalized()
    return (clean.name, clean.username, clean.host, clean.ssh_port, clean.ssh_key_path)


def build_remote_ssh_base(profile: Profile, ssh_path: str | None = None, batch_mode: bool = True) -> list[str]:
    clean = profile.normalized()
    executable = ssh_path or detect_ssh()
    command = [
        executable,
        "-p",
        str(clean.ssh_port),
        *SSH_KEEPALIVE_OPTIONS,
        *ssh_host_key_options(executable),
        "-o",
        "ConnectTimeout=15",
    ]
    if batch_mode:
        command.extend(["-o", "BatchMode=yes"])
    if clean.ssh_key_path:
        command.extend(["-i", clean.ssh_key_path])
    command.append(f"{clean.username}@{clean.host}")
    return command


def build_list_remote_entries_command(
    profile: Profile,
    remote_path: str,
    ssh_path: str | None = None,
    batch_mode: bool = True,
) -> list[str]:
    clean_path = _clean_remote_path(remote_path)
    remote_command = (
        f"find {shlex.quote(clean_path)} -mindepth 1 -maxdepth 1 "
        "-printf '%y\\t%f\\t%s\\t%TY-%Tm-%Td %TH:%TM\\t%p\\n'"
    )
    return [*build_remote_ssh_base(profile, ssh_path=ssh_path, batch_mode=batch_mode), remote_command]


def parse_remote_entries(output: str) -> list[RemoteEntry]:
    entries: list[RemoteEntry] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("** WARNING:"):
            continue
        parts = line.split("\t", 4)
        if len(parts) != 5:
            continue
        kind, name, size_text, modified, path = parts
        try:
            size = int(size_text)
        except ValueError:
            size = 0
        entries.append(RemoteEntry(kind=kind, name=name, size=size, modified=modified, path=path))
    entries.sort(key=lambda item: (not item.is_dir, item.name.lower()))
    return entries


def format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    value = float(size)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        value /= 1024
        if value < 1024:
            return f"{value:.2f} {unit}"
    return f"{value:.2f} PiB"


def build_list_remote_dirs_command(
    profile: Profile,
    remote_path: str,
    ssh_path: str | None = None,
    batch_mode: bool = True,
) -> list[str]:
    clean_path = _clean_remote_path(remote_path)
    remote_command = f"find {shlex.quote(clean_path)} -mindepth 1 -maxdepth 1 -type d -print | sort"
    return [*build_remote_ssh_base(profile, ssh_path=ssh_path, batch_mode=batch_mode), remote_command]


def build_find_remote_dirs_command(
    profile: Profile,
    remote_path: str,
    query: str,
    ssh_path: str | None = None,
    batch_mode: bool = True,
    max_depth: int = 6,
    limit: int = 500,
) -> list[str]:
    clean_path = _clean_remote_path(remote_path)
    clean_query = query.strip()
    if not clean_query:
        raise ValueError("Search text is required.")
    depth = max(1, int(max_depth))
    count = max(1, int(limit))
    pattern = f"*{clean_query}*"
    remote_command = (
        f"find {shlex.quote(clean_path)} -maxdepth {depth} -type d "
        f"-iname {shlex.quote(pattern)} -print | sort | head -{count}"
    )
    return [*build_remote_ssh_base(profile, ssh_path=ssh_path, batch_mode=batch_mode), remote_command]


def _clean_remote_path(remote_path: str) -> str:
    clean = remote_path.strip()
    if not clean:
        raise ValueError("Remote path is required.")
    if not clean.startswith("/"):
        raise ValueError("Remote path must start with '/'.")
    return clean.rstrip("/") or "/"
