from __future__ import annotations

from app.core.profiles import Profile
from app.core.ssh_test import build_ssh_test_command


def test_ssh_test_command_limits_prompt_loops() -> None:
    command = build_ssh_test_command(Profile(username="user"), ssh_path="ssh.exe")

    assert "ConnectionAttempts=1" in command
    assert "NumberOfPasswordPrompts=1" in command
    assert "ConnectTimeout=15" in command

