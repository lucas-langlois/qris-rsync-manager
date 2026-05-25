from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .paths import detect_rsync, profiles_path


DEFAULT_HOST = "ssh1.qriscloud.org.au"
QRISCLOUD_SSH_HOSTS = ["ssh1.qriscloud.org.au", "ssh2.qriscloud.org.au"]


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
        return cls(
            name=str(data.get("name", "")).strip() or "Default QRIScloud",
            username=str(data.get("username", "")).strip(),
            host=str(data.get("host", "")).strip() or DEFAULT_HOST,
            collection_id=str(data.get("collection_id", "")).strip().upper() or "Q0101",
            remote_path=str(data.get("remote_path", "")).strip(),
            ssh_port=int(data.get("ssh_port", 22) or 22),
            ssh_key_path=str(data.get("ssh_key_path", "")).strip(),
            rsync_path=str(data.get("rsync_path", "")).strip() or detect_rsync(),
        ).normalized()


def default_profile() -> Profile:
    return Profile(rsync_path=detect_rsync()).normalized()


def load_profiles(path: str | Path | None = None) -> list[Profile]:
    profile_file = Path(path) if path else profiles_path()
    if not profile_file.exists():
        return [default_profile()]
    try:
        raw = json.loads(profile_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [default_profile()]
    if not isinstance(raw, list):
        return [default_profile()]
    profiles = [Profile.from_dict(item) for item in raw if isinstance(item, dict)]
    return profiles or [default_profile()]


def save_profiles(profiles: list[Profile], path: str | Path | None = None) -> None:
    profile_file = Path(path) if path else profiles_path()
    profile_file.parent.mkdir(parents=True, exist_ok=True)
    normalized = [profile.normalized() for profile in profiles]
    data = [asdict(profile) for profile in normalized]
    profile_file.write_text(json.dumps(data, indent=2), encoding="utf-8")


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
