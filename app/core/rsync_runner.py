from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

from .askpass import build_askpass_environment, scrub_askpass_environment


OutputCallback = Callable[[str], None]


class RsyncRunner:
    def __init__(self) -> None:
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._cancel_requested = False

    def run(
        self,
        command: list[str],
        log_file: Path,
        on_output: OutputCallback | None = None,
        passphrase: str = "",
        ssh_path: str | None = None,
    ) -> int:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        self._cancel_requested = False
        env = build_askpass_environment(passphrase, ssh_path=ssh_path or self._ssh_path_from_command(command))
        creationflags = 0
        if sys.platform.startswith("win"):
            creationflags = subprocess.CREATE_NO_WINDOW
        try:
            with log_file.open("a", encoding="utf-8", errors="replace") as log:
                self._emit(log, on_output, f"Running: {self._display_command(command)}\n")
                with subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    shell=False,
                    creationflags=creationflags,
                    env=env,
                ) as process:
                    with self._lock:
                        self._process = process
                    if process.stdout:
                        while True:
                            chunk = process.stdout.read(1)
                            if not chunk:
                                break
                            self._emit(log, on_output, chunk)
                    returncode = process.wait()
                    if self._cancel_requested:
                        self._emit(log, on_output, "\nProcess cancelled by user.\n")
                    self._emit(log, on_output, f"\nProcess exited with code {returncode}\n")
                    return returncode
        except FileNotFoundError as exc:
            message = f"Failed to start rsync. Executable not found: {exc}\n"
            log_file.write_text(message, encoding="utf-8")
            if on_output:
                on_output(message)
            return 127
        except OSError as exc:
            message = f"Failed to start rsync: {exc}\n"
            log_file.write_text(message, encoding="utf-8")
            if on_output:
                on_output(message)
            return 1
        finally:
            scrub_askpass_environment(env)
            with self._lock:
                self._process = None

    def cancel(self) -> None:
        with self._lock:
            process = self._process
        self._cancel_requested = True
        if process and process.poll() is None:
            process.terminate()

    @staticmethod
    def _emit(log, on_output: OutputCallback | None, text: str) -> None:
        log.write(text)
        log.flush()
        if on_output:
            on_output(text)

    @staticmethod
    def _display_command(command: list[str]) -> str:
        return " ".join(f'"{part}"' if " " in part else part for part in command)

    @staticmethod
    def _ssh_path_from_command(command: list[str]) -> str | None:
        try:
            transport = command[command.index("-e") + 1]
            return transport.split(" ", 1)[0].strip("'\"") or None
        except (ValueError, IndexError):
            return command[0] if command else None
