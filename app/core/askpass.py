from __future__ import annotations

import os
from pathlib import Path

from .paths import MSYS2_SSH_PATH, app_data_dir, is_msys2_executable, windows_path_to_msys


PASSPHRASE_ENV = "QRIS_RSYNC_MANAGER_PASSPHRASE"


def askpass_dir() -> Path:
    path = app_data_dir() / "askpass"
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_askpass_helper() -> Path:
    helper = askpass_dir() / "qris_ssh_askpass.sh"
    helper.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"${PASSPHRASE_ENV}\"\n",
        encoding="utf-8",
        newline="\n",
    )
    return helper


def build_askpass_environment(passphrase: str, ssh_path: str | Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    if not passphrase:
        return env

    helper = ensure_askpass_helper()
    if is_msys2_executable(ssh_path):
        askpass = windows_path_to_msys(helper)
        env["PATH"] = f"{MSYS2_SSH_PATH.parent}{os.pathsep}{env.get('PATH', '')}"
    else:
        askpass = str(helper)

    env.update(
        {
            PASSPHRASE_ENV: passphrase,
            "SSH_ASKPASS": askpass,
            "SSH_ASKPASS_REQUIRE": "force",
            "DISPLAY": env.get("DISPLAY") or "QRISRsyncManager",
        }
    )
    return env


def scrub_askpass_environment(env: dict[str, str]) -> None:
    env.pop(PASSPHRASE_ENV, None)


def passphrase_cache_key(key_path: str) -> str:
    """Identify an SSH key file so profiles sharing a key share its passphrase."""
    return os.path.normcase(os.path.normpath(os.path.expanduser(key_path.strip())))


# ssh prints this after it has tried the key (and any passphrase) and the
# server still refused it. A wrong passphrase ends the same way.
_KEY_REJECTED_MARKERS = ("permission denied (publickey", "incorrect passphrase", "bad passphrase")


def ssh_key_was_rejected(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _KEY_REJECTED_MARKERS)


def main() -> int:
    print(os.environ.get(PASSPHRASE_ENV, ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
