from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

from app.core.paths import MSYS2_RSYNC_PATH, MSYS2_SSH_PATH, detect_rsync, is_executable_file
from app.core.profiles import DEFAULT_HOST, Profile


QRIS_COLLECTION_GUIDE_URL = (
    "https://www.qriscloud.org.au/support/qriscloud-documentation/"
    "93-using-qrisdata-collections"
)
WINDOWS_SSH_KEY_GUIDE_URL = (
    "https://learn.microsoft.com/en-us/windows-server/administration/openssh/"
    "openssh_keymanagement"
)
QRIS_SUPPORT_URL = "https://qriscloud.zendesk.com/hc/en-us/requests/new"
MSYS2_URL = "https://www.msys2.org/"
MSYS2_INSTALL_COMMANDS = "pacman -Syu\npacman -S --needed rsync openssh"
MSYS2_CHECK_COMMANDS = (
    '& "C:\\msys64\\usr\\bin\\rsync.exe" --version\n'
    '& "C:\\msys64\\usr\\bin\\ssh.exe" -V'
)


def key_path() -> Path:
    return Path.home() / ".ssh" / "qriscloud_ed25519"


def create_key_command() -> str:
    return (
        'New-Item -ItemType Directory -Force "$env:USERPROFILE\\.ssh"\n'
        'ssh-keygen -t ed25519 -f "$env:USERPROFILE\\.ssh\\qriscloud_ed25519"'
    )


def register_key_command(username: str) -> str:
    clean = username.strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", clean):
        clean = "<your-UQ-username>"
    return (
        'Get-Content "$env:USERPROFILE\\.ssh\\qriscloud_ed25519.pub" | '
        f'ssh {clean}@{DEFAULT_HOST} '
        '"umask 077; mkdir -p ~/.ssh; cat >> ~/.ssh/authorized_keys"'
    )


def _paragraph(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextFormat(Qt.RichText)
    label.setOpenExternalLinks(True)
    return label


class SetupGuideDialog(QWizard):
    """Beginner walkthrough that produces a pre-filled QRIScloud profile."""

    def __init__(self, parent=None, *, offer_profile_creation: bool = True) -> None:
        super().__init__(parent)
        self.offer_profile_creation = offer_profile_creation
        self.setWindowTitle("First-time QRIScloud setup")
        self.setMinimumSize(720, 580)
        self.setWizardStyle(QWizard.ModernStyle)
        self.setOption(QWizard.NoBackButtonOnStartPage)

        self.addPage(self._details_page())
        self.addPage(self._rsync_page())
        self.addPage(self._key_page())
        self.addPage(self._register_page())
        self.addPage(self._profile_page())
        self.addPage(self._test_page())
        self.setButtonText(QWizard.FinishButton, "Create profile" if offer_profile_creation else "Close")
        self.currentIdChanged.connect(self._page_changed)

    @property
    def create_profile_after_finish(self) -> bool:
        return self.offer_profile_creation

    def profile_template(self) -> Profile:
        collection = self.collection_edit.text().strip().upper() or "Q0101"
        return Profile(
            name=collection,
            username=self.username_edit.text().strip(),
            host=DEFAULT_HOST,
            collection_id=collection,
            remote_path=f"/data/{collection}",
            ssh_port=22,
            ssh_key_path=str(key_path()),
            rsync_path=detect_rsync(),
        ).normalized()

    def validateCurrentPage(self) -> bool:
        if self.currentId() != 0:
            return super().validateCurrentPage()
        username = self.username_edit.text().strip()
        collection = self.collection_edit.text().strip().upper()
        errors: list[str] = []
        if not re.fullmatch(r"[A-Za-z0-9._-]+", username):
            errors.append("Enter only your UQ username, without spaces or @ symbols.")
        if not re.fullmatch(r"Q\d{4}", collection):
            errors.append("Collection ID must be Q followed by four digits, for example Q0101.")
        if errors:
            QMessageBox.warning(self, "Check your details", "\n".join(f"• {error}" for error in errors))
            return False
        self.collection_edit.setText(collection)
        return super().validateCurrentPage()

    def _details_page(self) -> QWizardPage:
        page = QWizardPage()
        page.setTitle("1. Enter the details you were given")
        page.setSubTitle("This guide assumes your account already has access to the collection.")
        layout = QVBoxLayout(page)
        layout.addWidget(
            _paragraph(
                "<p><b>QRIScloud</b> stores the remote research data. This app uses <b>SSH</b>, an encrypted "
                "connection, and <b>rsync</b>, the file-copying tool.</p>"
                "<p>Use the exact UQ identity that was granted access. If you have both staff and student "
                "accounts, they are not interchangeable.</p>"
            )
        )
        layout.addWidget(QLabel("UQ username (for example uqabcdef or s1234567)"))
        self.username_edit = QLineEdit()
        self.username_edit.setPlaceholderText("Your UQ username")
        page.registerField("username*", self.username_edit)
        layout.addWidget(self.username_edit)
        layout.addWidget(QLabel("Collection ID (for example Q0101)"))
        self.collection_edit = QLineEdit()
        self.collection_edit.setPlaceholderText("Q0101")
        self.collection_edit.setMaxLength(20)
        page.registerField("collection*", self.collection_edit)
        layout.addWidget(self.collection_edit)
        layout.addWidget(
            _paragraph(
                f'<p><a href="{QRIS_COLLECTION_GUIDE_URL}">Open the official QRISdata collection guide</a></p>'
            )
        )
        layout.addStretch()
        return page

    def _rsync_page(self) -> QWizardPage:
        page = QWizardPage()
        page.setTitle("2. Install rsync and SSH")
        page.setSubTitle("Windows needs MSYS2 to provide the file-transfer programs used by this app.")
        layout = QVBoxLayout(page)
        rsync_found = is_executable_file(MSYS2_RSYNC_PATH)
        ssh_found = is_executable_file(MSYS2_SSH_PATH)
        if rsync_found and ssh_found:
            status = (
                "<p style='color:#16733a'><b>Ready:</b> rsync and SSH were found in "
                "<code>C:\\msys64\\usr\\bin</code>. You can continue.</p>"
            )
        else:
            missing = " and ".join(
                name for name, found in (("rsync", rsync_found), ("SSH", ssh_found)) if not found
            )
            status = f"<p style='color:#9a4d00'><b>Setup needed:</b> {missing} was not found.</p>"
        self.msys2_status_label = _paragraph(status)
        layout.addWidget(self.msys2_status_label)
        layout.addWidget(
            _paragraph(
                "<ol><li>Download MSYS2 from its official website and install it in the default "
                "<code>C:\\msys64</code> location. Ask local IT if you cannot install software.</li>"
                "<li>Open <b>MSYS2 MSYS</b> from the Start menu—not UCRT64 or MINGW64.</li>"
                "<li>Run the first command below. If told to close the window, close it, reopen "
                "<b>MSYS2 MSYS</b>, and run the first command again.</li>"
                "<li>Run the second command. Press <b>Y</b> when asked to install the packages.</li></ol>"
            )
        )
        website_button = QPushButton("Open the official MSYS2 website")
        website_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(MSYS2_URL)))
        layout.addWidget(website_button)
        layout.addWidget(self._command_box(MSYS2_INSTALL_COMMANDS, "Copy MSYS2 install commands"))
        layout.addWidget(
            _paragraph(
                "<p>After installation, close MSYS2, open <b>PowerShell</b>, and run these checks. "
                "Both should print version information.</p>"
            )
        )
        layout.addWidget(self._command_box(MSYS2_CHECK_COMMANDS, "Copy rsync/SSH checks"))
        layout.addStretch()
        return page

    def _key_page(self) -> QWizardPage:
        page = QWizardPage()
        page.setTitle("3. Create your SSH key")
        page.setSubTitle("The key proves who you are without saving your UQ password in this app.")
        layout = QVBoxLayout(page)
        layout.addWidget(
            _paragraph(
                "<p>An SSH key is a pair of files:</p><ul>"
                "<li><b>Private key</b>: <code>qriscloud_ed25519</code>. Treat it like a password. Never share it.</li>"
                "<li><b>Public key</b>: <code>qriscloud_ed25519.pub</code>. This is safe to register with QRIScloud.</li>"
                "</ul><p>Open <b>PowerShell</b> from the Windows Start menu, paste the commands below, and press "
                "Enter. Choose a memorable passphrase when asked. The cursor does not move while you type it; "
                "that is normal. If asked to overwrite an existing key, answer <b>no</b> and ask local IT for "
                "help using the key you already have.</p>"
            )
        )
        layout.addWidget(self._command_box(create_key_command(), "Copy key-creation commands"))
        layout.addWidget(
            _paragraph(
                "<p>If <code>ssh-keygen</code> is not recognised, ask local IT to enable the Windows OpenSSH "
                "Client in <b>Settings → System → Optional features</b>.</p>"
                f'<p><a href="{WINDOWS_SSH_KEY_GUIDE_URL}">Open Microsoft\'s SSH-key guide</a></p>'
            )
        )
        layout.addStretch()
        return page

    def _register_page(self) -> QWizardPage:
        page = QWizardPage()
        page.setTitle("4. Register the public key")
        page.setSubTitle("The server must know your public key before the app can use the private key.")
        layout = QVBoxLayout(page)
        layout.addWidget(
            _paragraph(
                "<p>Paste the command below into PowerShell. On the first connection, type <b>yes</b> if the "
                "host shown is <code>ssh1.qriscloud.org.au</code>, then enter your normal UQ account password. "
                "The command adds only your public key to your remote account.</p>"
                "<p><b>Your password and private key are not copied by this command.</b></p>"
            )
        )
        self.register_command_edit = QTextEdit()
        self.register_command_edit.setReadOnly(True)
        self.register_command_edit.setAcceptRichText(False)
        self.register_command_edit.setMaximumHeight(92)
        layout.addWidget(self.register_command_edit)
        self.username_edit.textChanged.connect(self._update_register_command)
        self._update_register_command()
        copy_button = QPushButton("Copy registration command")
        copy_button.clicked.connect(lambda: self._copy_text(self.register_command_edit.toPlainText()))
        layout.addWidget(copy_button)
        layout.addWidget(
            _paragraph(
                "<p>If the command is rejected, do not keep retrying. Wait ten minutes after repeated failed "
                "logins, then ask UQ ITS (for a UQ RDM collection) or QRIScloud support for help registering "
                "the <code>.pub</code> key. Never send the private file.</p>"
            )
        )
        support_button = QPushButton("Open QRIScloud support")
        support_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(QRIS_SUPPORT_URL)))
        layout.addWidget(support_button)
        layout.addStretch()
        return page

    def _profile_page(self) -> QWizardPage:
        page = QWizardPage()
        page.setTitle("5. Check the profile")
        page.setSubTitle("The app will pre-fill these values when this guide finishes.")
        layout = QVBoxLayout(page)
        self.profile_summary_label = _paragraph("")
        layout.addWidget(self.profile_summary_label)
        layout.addStretch()
        return page

    def _test_page(self) -> QWizardPage:
        page = QWizardPage()
        page.setTitle("6. Test before transferring files")
        page.setSubTitle("A successful test confirms the username, key, host, and network connection.")
        layout = QVBoxLayout(page)
        layout.addWidget(
            _paragraph(
                "<ol><li>Review and save the profile form.</li><li>Select it and click <b>Test SSH</b>.</li>"
                "<li>Enter the SSH-key passphrase you created. It stays in memory only for this app session.</li>"
                "<li>After the test succeeds, click <b>Load</b> in the Remote panel.</li>"
                "<li>Confirm the path is your collection and use <b>Compare upload</b> or "
                "<b>Compare download</b> before the first transfer.</li></ol>"
            )
        )
        layout.addStretch()
        return page

    def _command_box(self, command: str, button_text: str) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        editor = QTextEdit()
        editor.setAcceptRichText(False)
        editor.setPlainText(command)
        editor.setReadOnly(True)
        editor.setMaximumHeight(76)
        layout.addWidget(editor)
        button = QPushButton(button_text)
        button.clicked.connect(lambda: self._copy_text(command))
        layout.addWidget(button)
        return container

    def _update_register_command(self) -> None:
        if hasattr(self, "register_command_edit"):
            self.register_command_edit.setPlainText(register_key_command(self.username_edit.text()))

    def _page_changed(self, page_id: int) -> None:
        if page_id != 4 or not hasattr(self, "profile_summary_label"):
            return
        profile = self.profile_template()
        self.profile_summary_label.setText(
            "<table cellspacing='8'>"
            f"<tr><td><b>Profile name</b></td><td><code>{profile.name}</code></td></tr>"
            f"<tr><td><b>Username</b></td><td><code>{profile.username}</code></td></tr>"
            f"<tr><td><b>Host</b></td><td><code>{profile.host}</code>; the app also tries ssh2.</td></tr>"
            f"<tr><td><b>Remote path</b></td><td><code>{profile.remote_path}</code></td></tr>"
            f"<tr><td><b>SSH port</b></td><td><code>{profile.ssh_port}</code></td></tr>"
            f"<tr><td><b>SSH key path</b></td><td><code>{profile.ssh_key_path}</code> "
            "(the private file without <code>.pub</code>).</td></tr>"
            f"<tr><td><b>rsync executable</b></td><td><code>{profile.rsync_path}</code></td></tr>"
            "</table><p>The profile does not store your UQ password or SSH-key passphrase.</p>"
        )

    def _copy_text(self, text: str) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is None:
            QMessageBox.warning(self, "Clipboard unavailable", "Could not access the Windows clipboard.")
            return
        clipboard.setText(text)
