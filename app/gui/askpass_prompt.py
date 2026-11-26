"""Graphical SSH_ASKPASS: ssh runs ``QRISRsyncManager --askpass "<prompt>"``.

Used for logins that need an answer typed during the connection, such as
Bunya's key passphrase and MFA code. The answer goes straight back to ssh on
standard output and is never stored or logged.
"""

from __future__ import annotations

import os
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QInputDialog, QLineEdit

SECRET_WORDS = ("passphrase", "password")


def _write_answer(text: str) -> None:
    data = (text + "\n").encode("utf-8")
    try:
        os.write(1, data)
        return
    except OSError:
        pass
    if sys.platform.startswith("win"):
        # A windowed program has no Python stdout, but ssh still gave it a pipe.
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        written = wintypes.DWORD()
        kernel32.WriteFile(handle, data, len(data), ctypes.byref(written), None)


def main(arguments: list[str]) -> int:
    prompt = (arguments[0] if arguments else "").strip() or "Enter the response:"
    app = QApplication.instance() or QApplication([])
    secret = any(word in prompt.lower() for word in SECRET_WORDS)
    dialog = QInputDialog()
    dialog.setWindowTitle("Bunya login")
    dialog.setLabelText(prompt)
    dialog.setTextEchoMode(QLineEdit.Password if secret else QLineEdit.Normal)
    dialog.setWindowFlag(Qt.WindowStaysOnTopHint, True)
    dialog.resize(420, dialog.sizeHint().height())
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    if dialog.exec() != QInputDialog.Accepted:
        return 1
    _write_answer(dialog.textValue())
    app.processEvents()
    return 0
