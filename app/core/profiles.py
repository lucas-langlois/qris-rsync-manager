from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from .paths import detect_rsync, profiles_path


DEFAULT_HOST = "ssh1.qriscloud.org.au"
QRISCLOUD_SSH_HOSTS = ["ssh1.qriscloud.org.au", "ssh2.qriscloud.org.au"]
_PROFILE_STRING_FIELDS = (
    "name",
    "username",
    "host",
    "collection_id",
    "remote_path",
    "ssh_key_path",
    "rsync_path",
)
_PROFILE_REQUIRED_FIELDS = frozenset((*_PROFILE_STRING_FIELDS, "ssh_port"))


@dataclass(frozen=True)
class ProfileLoadResult:
    """Profiles loaded from disk, plus non-fatal messages suitable for a UI log."""

    profiles: list["Profile"]
    diagnostics: tuple[str, ...] = ()
    source: Literal["primary", "backup", "default"] = "default"


@dataclass
class Profile:
    name: str = "Default QRIScloud"
    username: str = ""
    host: str = DEFAULT_HOST
    collection_id: str = "Q0101"
    remote_path: str = "/data/Q0101"
    ssh_port: int = 22
    ssh_key_path: str = ""
    rsync_path: str = ""

    def normalized(self) -> "Profile":
        collection_id = self.collection_id.strip().upper() or "Q0101"
        remote_path = self.remote_path.strip() or f"/data/{collection_id}"
        return Profile(
            name=self.name.strip() or collection_id,
            username=self.username.strip(),
            host=self.host.strip() or DEFAULT_HOST,
            collection_id=collection_id,
            remote_path=remote_path,
            ssh_port=int(self.ssh_port or 22),
            ssh_key_path=self.ssh_key_path.strip(),
            rsync_path=self.rsync_path.strip() or detect_rsync(),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Profile":
        missing = _PROFILE_REQUIRED_FIELDS.difference(data)
        if missing:
            raise ValueError(f"missing fields: {', '.join(sorted(missing))}")
        invalid_strings = [field for field in _PROFILE_STRING_FIELDS if not isinstance(data[field], str)]
        if invalid_strings:
            raise ValueError(f"fields must be strings: {', '.join(invalid_strings)}")
        port = data["ssh_port"]
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("ssh_port must be an integer from 1 to 65535")
        return cls(
            name=data["name"].strip() or "Default QRIScloud",
            username=data["username"].strip(),
            host=data["host"].strip() or DEFAULT_HOST,
            collection_id=data["collection_id"].strip().upper() or "Q0101",
            remote_path=data["remote_path"].strip(),
            ssh_port=port,
            ssh_key_path=data["ssh_key_path"].strip(),
            rsync_path=data["rsync_path"].strip() or detect_rsync(),
        ).normalized()


def default_profile() -> Profile:
    return Profile(rsync_path=detect_rsync()).normalized()


def load_profiles(path: str | Path | None = None) -> list[Profile]:
    """Load profiles while preserving the legacy list-only API."""
    return load_profiles_result(path).profiles


def load_profiles_result(path: str | Path | None = None) -> ProfileLoadResult:
    """Load profiles without allowing a corrupt settings file to stop startup."""
    profile_file = Path(path) if path else profiles_path()
    backup_file = profile_file.with_name(f"{profile_file.name}.bak")
    diagnostics: list[str] = []

    candidates: tuple[tuple[Path, Literal["primary", "backup"]], ...] = (
        (profile_file, "primary"),
        (backup_file, "backup"),
    )
    for candidate, source in candidates:
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError("top-level JSON value is not a list")
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            if candidate.exists() or not isinstance(exc, FileNotFoundError):
                diagnostics.append(f"Could not read {source} profiles: {exc}")
            continue

        profiles: list[Profile] = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                diagnostics.append(f"Skipped {source} profile {index + 1}: record is not an object")
                continue
            try:
                profiles.append(Profile.from_dict(item))
            except (TypeError, ValueError) as exc:
                diagnostics.append(f"Skipped {source} profile {index + 1}: {exc}")
        if profiles:
            return ProfileLoadResult(profiles, tuple(diagnostics), source)
        diagnostics.append(f"No valid profiles found in {source} file.")
        # A syntactically valid empty or partly-invalid primary is not a reason to
        # overwrite the user's settings from an older backup.
        if source == "primary":
            return ProfileLoadResult([default_profile()], tuple(diagnostics), "default")

    return ProfileLoadResult([default_profile()], tuple(diagnostics), "default")


def save_profiles(profiles: list[Profile], path: str | Path | None = None) -> None:
    profile_file = Path(path) if path else profiles_path()
    profile_file.parent.mkdir(parents=True, exist_ok=True)
    normalized = [profile.normalized() for profile in profiles]
    data = [asdict(profile) for profile in normalized]
    payload = json.dumps(data, indent=2).encode("utf-8")
    temporary_file: Path | None = None
    backup_temporary: Path | None = None
    backup_file = profile_file.with_name(f"{profile_file.name}.bak")

    try:
        temporary_file = _write_temporary(profile_file.parent, profile_file.name, payload)
        previous_payload = _recoverable_profile_payload(profile_file)
        backup_is_recoverable = _recoverable_profile_payload(backup_file) is not None
        if previous_payload is not None:
            backup_temporary = _write_temporary(profile_file.parent, backup_file.name, previous_payload)
            os.replace(backup_temporary, backup_file)
            backup_temporary = None
        elif not backup_is_recoverable:
            # Initial save (or two unusable files): establish a recovery copy
            # before publishing the new primary.
            backup_temporary = _write_temporary(profile_file.parent, backup_file.name, payload)
            os.replace(backup_temporary, backup_file)
            backup_temporary = None
        os.replace(temporary_file, profile_file)
        temporary_file = None
    finally:
        for temporary in (temporary_file, backup_temporary):
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass


def _write_temporary(directory: Path, prefix: str, payload: bytes) -> Path:
    handle, temporary_name = tempfile.mkstemp(prefix=f".{prefix}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(handle, "wb") as file_handle:
            file_handle.write(payload)
            file_handle.flush()
            os.fsync(file_handle.fileno())
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise
    return Path(temporary_name)


def _recoverable_profile_payload(path: Path) -> bytes | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(raw, list):
        return None
    valid: list[Profile] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            valid.append(Profile.from_dict(item))
        except (TypeError, ValueError):
            continue
    if not valid:
        return None
    return json.dumps([asdict(profile) for profile in valid], indent=2).encode("utf-8")


def upsert_profile(profiles: list[Profile], profile: Profile) -> list[Profile]:
    normalized = profile.normalized()
    result: list[Profile] = []
    replaced = False
    for existing in profiles:
        if existing.name == normalized.name:
            result.append(normalized)
            replaced = True
        else:
            result.append(existing.normalized())
    if not replaced:
        result.append(normalized)
    return result


def profile_with_host(profile: Profile, host: str) -> Profile:
    clean = profile.normalized()
    return Profile(
        name=clean.name,
        username=clean.username,
        host=host,
        collection_id=clean.collection_id,
        remote_path=clean.remote_path,
        ssh_port=clean.ssh_port,
        ssh_key_path=clean.ssh_key_path,
        rsync_path=clean.rsync_path,
    )


def fallback_hosts(profile: Profile) -> list[str]:
    host = profile.normalized().host
    if host in QRISCLOUD_SSH_HOSTS:
        return [host, *[candidate for candidate in QRISCLOUD_SSH_HOSTS if candidate != host]]
    return [host]
