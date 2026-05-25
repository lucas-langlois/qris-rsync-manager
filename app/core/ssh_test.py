from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass

from .askpass import build_askpass_environment, scrub_askpass_environment
from .paths import detect_ssh
from .profiles import Profile
from .rsync_command import SSH_KEEPALIVE_OPTIONS


@dataclass
class CommandResult:
    returncode: int
    output: str


def build_ssh_test_command(profile: Profile, ssh_path: str | None = None, batch_mode: bool = True) -> list[str]:
    clean = profile.normalized()
    command = [
        ssh_path or detect_ssh(),
        "-p",
        str(clean.ssh_port),
        *SSH_KEEPALIVE_OPTIONS,
        "-o",
        "ConnectTimeout=15",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        "NumberOfPasswordPrompts=1",
    ]
    if batch_mode:
        command.extend(["-o", "BatchMode=yes"])
    if clean.ssh_key_path:
        command.extend(["-i", clean.ssh_key_path])
    command.extend([f"{clean.username}@{clean.host}", "echo QRIS_SSH_OK"])
    return command


def run_ssh_test(
    profile: Profile,
    ssh_path: str | None = None,
    timeout: int = 90,
    passphrase: str = "",
) -> CommandResult:
    startupinfo = None
    creationflags = 0
    if sys.platform.startswith("win"):
        creationflags = subprocess.CREATE_NO_WINDOW
    try:
        command = build_ssh_test_command(profile, ssh_path=ssh_path, batch_mode=not bool(passphrase))
        env = build_askpass_environment(passphrase, ssh_path=command[0])
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            shell=False,
            creationflags=creationflags,
            startupinfo=startupinfo,
            env=env,
        )
        scrub_askpass_environment(env)
        return CommandResult(completed.returncode, completed.stdout)
    except FileNotFoundError as exc:
        return CommandResult(127, f"SSH executable was not found: {exc}")
    except subprocess.TimeoutExpired:
        return CommandResult(124, f"SSH test timed out after {timeout} seconds.")
    except OSError as exc:
        return CommandResult(1, f"SSH test failed to start: {exc}")
