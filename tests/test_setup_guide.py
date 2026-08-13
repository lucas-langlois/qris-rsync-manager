from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QApplication, QLabel, QTextEdit

from app.gui.profile_dialog import ProfileDialog
from app.gui.setup_guide_dialog import (
    MSYS2_CHECK_COMMANDS,
    MSYS2_INSTALL_COMMANDS,
    SetupGuideDialog,
    create_key_command,
    register_key_command,
)


def _application() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_setup_guide_has_complete_beginner_flow() -> None:
    _application()
    dialog = SetupGuideDialog()

    assert dialog.pageIds() == [0, 1, 2, 3, 4, 5]
    assert [dialog.page(page_id).title() for page_id in dialog.pageIds()] == [
        "1. Enter the details you were given",
        "2. Install rsync and SSH",
        "3. Create your SSH key",
        "4. Register the public key",
        "5. Check the profile",
        "6. Test before transferring files",
    ]
    dialog.close()


def test_commands_keep_private_and_public_key_roles_separate() -> None:
    create = create_key_command()
    register = register_key_command("uqabcdef")

    assert "qriscloud_ed25519.pub" not in create
    assert "qriscloud_ed25519.pub" in register
    assert "uqabcdef@ssh1.qriscloud.org.au" in register
    assert "password" not in register.lower()


def test_key_page_explains_ed25519_and_suggested_filename() -> None:
    _application()
    dialog = SetupGuideDialog()
    key_page_text = " ".join(
        label.text() for label in dialog.page(2).findChildren(QLabel)
    )

    assert "name of the secure key type" in key_page_text
    assert "suggested filename" in key_page_text
    assert "not an account name or a QRIScloud requirement" in key_page_text
    dialog.close()


def test_msys2_commands_install_and_verify_required_tools() -> None:
    assert "pacman -S --needed rsync openssh" in MSYS2_INSTALL_COMMANDS
    assert r"C:\msys64\usr\bin\rsync.exe" in MSYS2_CHECK_COMMANDS
    assert r"C:\msys64\usr\bin\ssh.exe" in MSYS2_CHECK_COMMANDS


def test_command_boxes_preserve_visible_line_breaks() -> None:
    _application()
    dialog = SetupGuideDialog()
    command_texts = [editor.toPlainText() for editor in dialog.findChildren(QTextEdit)]

    assert MSYS2_INSTALL_COMMANDS in command_texts
    assert create_key_command() in command_texts
    assert all("\n" in command for command in (MSYS2_INSTALL_COMMANDS, create_key_command()))
    dialog.close()


def test_unsafe_username_is_not_inserted_into_shell_command() -> None:
    command = register_key_command("person; Remove-Item C:\\data")

    assert "Remove-Item" not in command
    assert "<your-UQ-username>" in command


def test_first_page_validates_uq_username_and_q_number(monkeypatch) -> None:
    _application()
    dialog = SetupGuideDialog()
    dialog.show()
    QApplication.processEvents()
    dialog.username_edit.setText("user@example.com")
    dialog.collection_edit.setText("collection")
    warnings: list[str] = []
    monkeypatch.setattr(
        "app.gui.setup_guide_dialog.QMessageBox.warning",
        lambda _parent, _title, message: warnings.append(message),
    )

    assert not dialog.validateCurrentPage()
    assert "without spaces or @ symbols" in warnings[0]
    assert "Q followed by four digits" in warnings[0]
    dialog.close()


def test_guide_builds_prefilled_profile() -> None:
    _application()
    dialog = SetupGuideDialog()
    dialog.username_edit.setText("uqabcdef")
    dialog.collection_edit.setText("q8940")

    profile = dialog.profile_template()

    assert profile.username == "uqabcdef"
    assert profile.collection_id == "Q8940"
    assert profile.remote_path == "/data/Q8940"
    assert profile.host == "ssh1.qriscloud.org.au"
    assert Path(profile.ssh_key_path).name == "qriscloud_ed25519"
    dialog.close()


def test_profile_summary_uses_entered_values() -> None:
    _application()
    dialog = SetupGuideDialog()
    dialog.show()
    QApplication.processEvents()
    dialog.username_edit.setText("uqabcdef")
    dialog.collection_edit.setText("q8940")

    dialog.next()
    dialog.next()
    dialog.next()
    dialog.next()
    QApplication.processEvents()

    assert dialog.currentId() == 4
    assert "uqabcdef" in dialog.profile_summary_label.text()
    assert "/data/Q8940" in dialog.profile_summary_label.text()
    dialog.close()


def test_profile_rejects_public_key_and_explains_fields() -> None:
    _application()
    dialog = ProfileDialog()
    dialog.username_edit.setText("uqabcdef")
    dialog.key_path_edit.setText(r"C:\Users\person\.ssh\qriscloud_ed25519.pub")

    assert any("private key" in error for error in dialog.validation_errors())
    assert "granted access" in dialog.username_edit.toolTip()
    assert "not .pub" in dialog.key_path_edit.placeholderText()
    assert "suggested filename" in dialog.key_path_edit.toolTip()
    dialog.close()
