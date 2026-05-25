from __future__ import annotations

from app.core.askpass import PASSPHRASE_ENV, build_askpass_environment, ensure_askpass_helper


def test_askpass_environment_sets_openssh_variables() -> None:
    env = build_askpass_environment("secret", ssh_path=r"C:\msys64\usr\bin\ssh.exe")

    assert env[PASSPHRASE_ENV] == "secret"
    assert env["SSH_ASKPASS_REQUIRE"] == "force"
    assert env["DISPLAY"] == "QRISRsyncManager"
    assert env["SSH_ASKPASS"].endswith("qris_ssh_askpass.sh")
    assert env["SSH_ASKPASS"].startswith("/")
    assert r"C:\msys64\usr\bin" in env["PATH"]


def test_askpass_helper_uses_direct_msys2_shell() -> None:
    helper = ensure_askpass_helper()

    assert helper.read_text(encoding="utf-8").startswith("#!/bin/sh\n")
