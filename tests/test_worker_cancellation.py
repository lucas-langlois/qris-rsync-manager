import threading
from pathlib import Path

from app.core.profiles import Profile
from app.core.rsync_runner import RsyncRunner
from app.gui import main_window
from app.gui.remote_browser_dialog import RemoteListWorker
from app.gui.main_window import RecallMediciWorker
from app.core.ssh_test import CommandResult


def test_fallback_worker_does_not_start_preflight_when_already_cancelled(monkeypatch) -> None:
    profile = Profile(name="test", host="ssh1.example", username="researcher")
    worker = main_window.FallbackCommandWorker(profile, lambda _: (_ for _ in ()).throw(AssertionError("factory called")), "test")
    monkeypatch.setattr(main_window, "new_log_file", lambda _: Path("transfer.log"))
    worker.cancel()

    codes: list[int] = []
    output: list[str] = []
    worker.finished.connect(codes.append)
    worker.output.connect(output.append)
    worker.run()

    assert codes == [130]
    assert any("cancelled before rsync" in line for line in output)


def test_fallback_worker_does_not_build_command_after_cancelled_preflight(monkeypatch) -> None:
    profile = Profile(name="test", host="ssh1.example", username="researcher")
    worker = main_window.FallbackCommandWorker(profile, lambda _: (_ for _ in ()).throw(AssertionError("factory called")), "test")
    monkeypatch.setattr(main_window, "new_log_file", lambda _: Path("transfer.log"))

    def cancel_during_preflight(*_args, **_kwargs):
        worker.cancel()
        return type("Result", (), {"returncode": 0, "output": ""})()

    monkeypatch.setattr(main_window, "run_ssh_test", cancel_during_preflight)
    codes: list[int] = []
    worker.finished.connect(codes.append)
    worker.run()

    assert codes == [130]


def test_fallback_worker_does_not_start_rsync_when_cancelled_by_command_factory(monkeypatch) -> None:
    profile = Profile(name="test", host="ssh1.example", username="researcher")
    worker = None

    def command_factory(_profile):
        assert worker is not None
        worker.cancel()
        return ["rsync"]

    worker = main_window.FallbackCommandWorker(profile, command_factory, "test")
    monkeypatch.setattr(main_window, "new_log_file", lambda _: Path("transfer.log"))
    monkeypatch.setattr(
        main_window,
        "run_ssh_test",
        lambda *_args, **_kwargs: type("Result", (), {"returncode": 0, "output": ""})(),
    )
    monkeypatch.setattr(worker.runner, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("rsync started")))
    codes: list[int] = []
    worker.finished.connect(codes.append)

    worker.run()

    assert codes == [130]


def test_rsync_runner_stops_when_cancel_arrives_during_process_start(monkeypatch, tmp_path) -> None:
    cancel_event = threading.Event()

    class FakeProcess:
        stdout = None

        def __init__(self) -> None:
            self.terminated = False
            self.returncode = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode if self.returncode is not None else 0

    process = FakeProcess()

    def start_process(*_args, **_kwargs):
        cancel_event.set()
        return process

    monkeypatch.setattr("app.core.rsync_runner.subprocess.Popen", start_process)
    runner = RsyncRunner()

    code = runner.run(["rsync"], tmp_path / "transfer.log", cancel_event=cancel_event)

    assert code == 130
    assert process.terminated


def test_remote_listing_cancel_escalates_to_kill_without_blocking(monkeypatch) -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.killed = threading.Event()
            self.terminate_calls = 0

        def poll(self):
            return None if not self.killed.is_set() else -9

        def terminate(self):
            self.terminate_calls += 1

        def kill(self):
            self.killed.set()

    worker = RemoteListWorker(["unused"], "")
    worker.STOP_GRACE_SECONDS = 0.01
    process = FakeProcess()
    worker._process = process

    worker.cancel()

    assert process.terminate_calls == 1
    assert process.killed.wait(1)


def test_recall_cancel_during_preflight_never_starts_batches(monkeypatch, tmp_path) -> None:
    worker = RecallMediciWorker(Profile(name="test", username="user"), ["/data/Q0101/a.txt"], "ssh.exe")
    observed_event = None

    def cancel_preflight(*_args, cancel_event=None, **_kwargs):
        nonlocal observed_event
        observed_event = cancel_event
        worker.cancel()
        return CommandResult(130, "cancelled")

    monkeypatch.setattr("app.gui.main_window.run_ssh_test", cancel_preflight)
    monkeypatch.setattr(worker, "_run_batches", lambda *_args: (_ for _ in ()).throw(AssertionError("batches started")))

    code = worker._run_with_fallback(tmp_path / "recall.log")

    assert observed_event is worker._cancelled
    assert code == 130
