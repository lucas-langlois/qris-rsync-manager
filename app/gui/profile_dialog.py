from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from app.core.paths import detect_rsync
from app.core.profiles import DEFAULT_HOST, Profile
from app.gui.setup_guide_dialog import SetupGuideDialog


class ProfileDialog(QDialog):
    def __init__(self, profile: Profile | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("QRIScloud Profile")
        self._previous_collection = (profile.collection_id if profile else "Q0101").upper()

        current = profile or Profile(host=DEFAULT_HOST, rsync_path=detect_rsync())
        current = current.normalized()

        self.name_edit = QLineEdit(current.name)
        self.username_edit = QLineEdit(current.username)
        self.host_edit = QLineEdit(current.host)
        self.collection_edit = QLineEdit(current.collection_id)
        self.remote_path_edit = QLineEdit(current.remote_path)
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(current.ssh_port)
        self.key_path_edit = QLineEdit(current.ssh_key_path)
        self.rsync_path_edit = QLineEdit(current.rsync_path or detect_rsync())

        self.name_edit.setPlaceholderText("Example: Q0101 reef data")
        self.username_edit.setPlaceholderText("The UQ identity granted collection access")
        self.host_edit.setPlaceholderText(DEFAULT_HOST)
        self.collection_edit.setPlaceholderText("Example: Q0101")
        self.remote_path_edit.setPlaceholderText("Example: /data/Q0101")
        self.key_path_edit.setPlaceholderText(r"C:\Users\you\.ssh\qriscloud_ed25519 (not .pub)")
        self.rsync_path_edit.setPlaceholderText(r"C:\msys64\usr\bin\rsync.exe")
        self.username_edit.setToolTip("Use the exact UQ identity that was granted access to this collection.")
        self.key_path_edit.setToolTip("Choose the private key file. The public .pub file cannot authenticate the app.")
        self.remote_path_edit.setToolTip("QRISdata collections are normally available at /data/Qnnnn.")

        self.collection_edit.textEdited.connect(self._collection_changed)

        layout = QVBoxLayout(self)
        intro = QLabel(
            "New to SSH? The setup guide explains the key pair, every field below, and how to test safely."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        help_button = QPushButton("Open first-time setup guide")
        help_button.clicked.connect(self._open_setup_guide)
        layout.addWidget(help_button)
        form = QFormLayout()
        form.addRow("Profile name", self.name_edit)
        form.addRow("Username", self.username_edit)
        form.addRow("Host", self.host_edit)
        form.addRow("Collection ID", self.collection_edit)
        form.addRow("Remote path", self.remote_path_edit)
        form.addRow("SSH port", self.port_spin)
        form.addRow("SSH key path", self._file_row(self.key_path_edit, self._browse_key))
        form.addRow("rsync executable", self._file_row(self.rsync_path_edit, self._browse_rsync))
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def profile(self) -> Profile:
        return Profile(
            name=self.name_edit.text(),
            username=self.username_edit.text(),
            host=self.host_edit.text(),
            collection_id=self.collection_edit.text(),
            remote_path=self.remote_path_edit.text(),
            ssh_port=self.port_spin.value(),
            ssh_key_path=self.key_path_edit.text(),
            rsync_path=self.rsync_path_edit.text(),
        ).normalized()

    def _collection_changed(self, value: str) -> None:
        new_collection = value.strip().upper()
        previous_default = f"/data/{self._previous_collection}"
        if self.remote_path_edit.text().strip() in {"", previous_default} and new_collection:
            self.remote_path_edit.setText(f"/data/{new_collection}")
        if new_collection:
            self._previous_collection = new_collection

    def _file_row(self, edit: QLineEdit, browse_slot) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(edit)
        button = QPushButton("Browse")
        button.clicked.connect(browse_slot)
        row.addWidget(button)
        return row

    def _browse_key(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select SSH key", str(Path.home()))
        if path:
            self.key_path_edit.setText(path)

    def _browse_rsync(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select rsync.exe", str(Path(r"C:\msys64\usr\bin")), "Executables (*.exe);;All files (*)")
        if path:
            self.rsync_path_edit.setText(path)

    def validation_errors(self) -> list[str]:
        profile = self.profile()
        errors: list[str] = []
        if not profile.username:
            errors.append("Enter the UQ username that was granted access to the collection.")
        if not profile.remote_path.startswith("/"):
            errors.append("Remote path must start with '/', for example /data/Q0101.")
        if profile.ssh_key_path.lower().endswith(".pub"):
            errors.append("SSH key path must be the private key, not the public .pub file.")
        return errors

    def _validate_and_accept(self) -> None:
        errors = self.validation_errors()
        if errors:
            QMessageBox.warning(self, "Check profile details", "\n".join(f"• {error}" for error in errors))
            return
        self.accept()

    def _open_setup_guide(self) -> None:
        SetupGuideDialog(self, offer_profile_creation=False).exec()
