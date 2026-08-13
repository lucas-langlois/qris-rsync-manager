from __future__ import annotations

import subprocess
import sys
import threading
import time
import os
import signal
from pathlib import Path

from app.core.profiles import Profile
from app.core.sync_compare import RemoteManifestCollector
from app.gui.main_window import SyncCompareWorker


def _worker(tmp_path: Path) -> SyncCompareWorker:
    return SyncCompareWorker(
        Profile(name="test", username="user", host="host"),
        "ssh.exe",
        str(tmp_path),
        "/data/Q0101",
        "",
    )


def _capture(worker: SyncCompareWorker) -> list[tuple[object, str]]:
    results: list[tuple[object, str]] = []
    worker.finished.connect(lambda result, error: results.append((result, error)))
    return results


def test_cancel_before_scan_finishes_once_without_starting_process(monkeypatch, tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    results = _capture(worker)
    worker.cancel()
    monkeypatch.setattr("app.gui.main_window.scan_local_manifest", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("scan started")))
    monkeypatch.setattr("app.gui.main_window.subprocess.Popen", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("process started")))

    worker.run()

    assert results == [(None, "Sync comparison was cancelled.")]


def test_cancel_during_local_scan_never_starts_remote_process(monkeypatch, tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    results = _capture(worker)

    def cancel_scan(_path, cancel_event=None, progress_callback=None):
        assert cancel_event is worker._cancelled
        cancel_event.set()
        return {"partial.txt": object()}

    monkeypatch.setattr("app.gui.main_window.scan_local_manifest", cancel_scan)
    monkeypatch.setattr("app.gui.main_window.subprocess.Popen", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("process started")))

    worker.run()

    assert results == [(None, "Sync comparison was cancelled.")]


class _BlockingOutput:
    def __init__(self, released: threading.Event) -> None:
        self.released = released

    def __iter__(self):
        return self

    def __next__(self):
        self.released.wait(2)
        raise StopIteration

    def close(self) -> None:
        self.released.set()


class _HungProcess:
    def __init__(self) -> None:
        self.released = threading.Event()
        self.stdout = _BlockingOutput(self.released)
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_timeouts: list[float] = []

    def poll(self):
        return -9 if self.kill_calls else None

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1
        self.released.set()

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        if not self.kill_calls:
            raise subprocess.TimeoutExpired("ssh", timeout)
        return -9


def test_silent_remote_timeout_escalates_to_kill_and_finishes_once(monkeypatch, tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    worker.REMOTE_TIMEOUT_SECONDS = 0.05
    worker.STOP_GRACE_SECONDS = 0.01
    results = _capture(worker)
    process = _HungProcess()
    monkeypatch.setattr("app.gui.main_window.scan_local_manifest", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("app.gui.main_window.fallback_hosts", lambda _profile: ["host"])
    monkeypatch.setattr("app.gui.main_window.build_askpass_environment", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("app.gui.main_window.scrub_askpass_environment", lambda _env: None)
    monkeypatch.setattr("app.gui.main_window.subprocess.Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr("app.gui.main_window.write_files_from", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("selection written")))

    worker.run()

    assert len(results) == 1
    assert results[0][0] is None
    assert "timed out" in results[0][1]
    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.wait_timeouts == [0.01, 0.01]


class _CompletedProcess:
    def __init__(self, lines: list[str]) -> None:
        self.stdout = iter(lines)

    def poll(self):
        return 0


def test_remote_manifest_is_parsed_incrementally(monkeypatch, tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    results = _capture(worker)
    process = _CompletedProcess(["a.txt\t1\t10.0\n", "nested/b.txt\t2\t11.0\n"])
    selection_file = tmp_path / "selection.txt"
    monkeypatch.setattr("app.gui.main_window.scan_local_manifest", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("app.gui.main_window.fallback_hosts", lambda _profile: ["host"])
    monkeypatch.setattr("app.gui.main_window.build_askpass_environment", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("app.gui.main_window.scrub_askpass_environment", lambda _env: None)
    monkeypatch.setattr("app.gui.main_window.subprocess.Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr("app.gui.main_window.write_files_from", lambda *_args, **_kwargs: selection_file)

    worker.run()

    assert len(results) == 1
    assert results[0][1] == ""
    assert results[0][0]["selected"] == 0
    assert results[0][0]["file_list"] == str(selection_file)


def test_exit_zero_without_stdout_eof_fails_instead_of_using_partial_manifest(monkeypatch, tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    worker.REMOTE_TIMEOUT_SECONDS = 1
    worker.OUTPUT_DRAIN_SECONDS = 0.05
    results = _capture(worker)
    process = _HungProcess()
    process.poll = lambda: 0
    monkeypatch.setattr("app.gui.main_window.scan_local_manifest", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("app.gui.main_window.fallback_hosts", lambda _profile: ["host"])
    monkeypatch.setattr("app.gui.main_window.build_askpass_environment", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("app.gui.main_window.scrub_askpass_environment", lambda _env: None)
    monkeypatch.setattr("app.gui.main_window.subprocess.Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr("app.gui.main_window.write_files_from", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("selection written")))

    worker.run()

    assert len(results) == 1
    assert results[0][0] is None
    assert "incomplete data" in results[0][1]


def test_cancel_before_success_commit_removes_selection_file(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    results = _capture(worker)
    file_list = tmp_path / "selection.txt"
    file_list.write_text("a.txt\n", encoding="utf-8")
    worker.cancel()

    worker._finish_success(file_list, {"selected": 1})

    assert results == [(None, "Sync comparison was cancelled.")]
    assert not file_list.exists()


def test_exit_zero_with_inherited_stdout_handle_returns_within_drain_deadline(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    worker.OUTPUT_DRAIN_SECONDS = 0.05
    worker.READER_JOIN_SECONDS = 0.5
    pid_file = tmp_path / "inherited-pipe-child.pid"
    command = [
        sys.executable,
        "-c",
        (
            "import pathlib,subprocess,sys; "
            "child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
            "stdout=sys.stdout, stderr=sys.stderr); "
            f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid)); "
            "print('a.txt\\t1\\t10.0', flush=True)"
        ),
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        shell=False,
    )
    started = time.monotonic()

    try:
        code, error = worker._collect_remote_manifest(
            process,
            RemoteManifestCollector(),
            "local-test",
            time.monotonic() + 2,
        )

        elapsed = time.monotonic() - started
        assert code == 1
        assert "incomplete data" in error
        assert elapsed < 0.75
        assert not any(thread.name == "sync-manifest-reader" and thread.is_alive() for thread in threading.enumerate())
    finally:
        if pid_file.exists():
            os.kill(int(pid_file.read_text()), signal.SIGTERM)


def test_terminate_oserror_still_attempts_kill(tmp_path: Path) -> None:
    worker = _worker(tmp_path)

    class Process:
        killed = False

        def poll(self):
            return -9 if self.killed else None

        def terminate(self):
            raise OSError("access denied")

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            return -9

    process = Process()

    assert worker._stop_process(process)
    assert process.killed


def test_live_process_that_closes_stdout_is_stopped(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    worker.STOP_GRACE_SECONDS = 0.5
    command = [
        sys.executable,
        "-c",
        "import os,time; os.close(1); os.close(2); time.sleep(30)",
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        shell=False,
    )

    code, error = worker._collect_remote_manifest(
        process,
        RemoteManifestCollector(),
        "local-test",
        time.monotonic() + 2,
    )

    assert code == 1
    assert "Could not read remote manifest output" in error
    assert process.poll() is not None


def test_successful_process_with_malformed_manifest_fails_closed(monkeypatch, tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    results = _capture(worker)
    process = _CompletedProcess(["a.txt\t1\t10.0\n", "filename-with-tab\tbad\trow\n"])
    monkeypatch.setattr("app.gui.main_window.scan_local_manifest", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("app.gui.main_window.fallback_hosts", lambda _profile: ["host"])
    monkeypatch.setattr("app.gui.main_window.build_askpass_environment", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("app.gui.main_window.scrub_askpass_environment", lambda _env: None)
    monkeypatch.setattr("app.gui.main_window.subprocess.Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr("app.gui.main_window.write_files_from", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("selection written")))

    worker.run()

    assert len(results) == 1
    assert results[0][0] is None
    assert "malformed line" in results[0][1]
