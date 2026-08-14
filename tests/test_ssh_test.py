from __future__ import annotations

from app.core.profiles import Profile
from app.core.ssh_test import build_ssh_test_command


def test_ssh_test_command_limits_prompt_loops(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    command = build_ssh_test_command(Profile(username="user"), ssh_path="ssh.exe")

    assert "ConnectionAttempts=1" in command
    assert "NumberOfPasswordPrompts=1" in command
    assert "ConnectTimeout=15" in command
    assert "StrictHostKeyChecking=accept-new" in command
    assert f"UserKnownHostsFile={tmp_path / 'QRISRsyncManager' / 'known_hosts'}" in command
