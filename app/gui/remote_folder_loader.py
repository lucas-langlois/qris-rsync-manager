"""Lists QRIScloud folders in the background when they are expanded in a tree."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QThread, Slot

from app.core.profiles import Profile, fallback_hosts, profile_with_host
from app.core.remote_dirs import RemoteDirectoryCache, build_list_remote_entries_command
from app.gui.drag_views import RemoteMoveTree
from app.gui.remote_browser_dialog import RemoteListWorker


class RemoteFolderLoader(QObject):
    """Fill expanded folders of a :class:`RemoteMoveTree`, one listing at a time.

    Listings are read-only and use their own thread, so expanding a folder
    never blocks transfers or the main listing. Results go into the shared
    directory cache, so reopening a folder is instant.
    """

    def __init__(
        self,
        tree: RemoteMoveTree,
        profile_provider: Callable[[], Profile | None],
        passphrase_provider: Callable[[Profile], str | None],
        ssh_path_provider: Callable[[], str],
        cache: RemoteDirectoryCache,
        on_error: Callable[[str], None] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.tree = tree
        self._profile_provider = profile_provider
        self._passphrase_provider = passphrase_provider
        self._ssh_path_provider = ssh_path_provider
        self.cache = cache
        self._on_error = on_error
        self._queue: list[str] = []
        self.thread: QThread | None = None
        self.worker: RemoteListWorker | None = None
        tree.folderExpandRequested.connect(self.request)

    def is_busy(self) -> bool:
        return self.thread is not None

    @Slot(str)
    def request(self, path: str) -> None:
        profile = self._profile_provider()
        if profile is None:
            self.tree.set_children(path, None, "No profile is selected.")
            return
        cached = self.cache.get(profile, path)
        if cached is not None:
            self.tree.set_children(path, list(cached))
            return
        if path not in self._queue:
            self._queue.append(path)
        self._start_next()

    def _start_next(self) -> None:
        if self.thread is not None or not self._queue:
            return
        profile = self._profile_provider()
        if profile is None:
            self._queue.clear()
            return
        path = self._queue.pop(0)
        cached = self.cache.get(profile, path)
        if cached is not None:
            self.tree.set_children(path, list(cached))
            self._start_next()
            return
        passphrase = self._passphrase_provider(profile)
        if passphrase is None:
            for pending in (path, *self._queue):
                self.tree.set_children(pending, None, "Cancelled: no SSH key passphrase.")
            self._queue.clear()
            return
        ssh_path = self._ssh_path_provider()
        try:
            attempts = [
                (
                    host,
                    build_list_remote_entries_command(
                        profile_with_host(profile, host), path, ssh_path=ssh_path, batch_mode=not bool(passphrase)
                    ),
                )
                for host in fallback_hosts(profile)
            ]
        except ValueError as exc:
            self.tree.set_children(path, None, str(exc))
            self._start_next()
            return
        worker = RemoteListWorker(attempts[0][1], passphrase, attempts=attempts)
        worker.cache_profile = profile
        worker.cache_path = path
        worker.generation = self.tree.generation
        thread = QThread(self)
        worker.moveToThread(thread)
        worker.finished.connect(self._finished)
        thread.started.connect(worker.run)
        thread.finished.connect(self._thread_finished)
        thread.finished.connect(thread.deleteLater)
        self.thread = thread
        self.worker = worker
        thread.start()

    @Slot(object, str)
    def _finished(self, entries: object, error: str) -> None:
        worker = self.sender()
        if not isinstance(worker, RemoteListWorker):
            return
        try:
            if error:
                if self._on_error is not None:
                    self._on_error(error)
                if worker.generation == self.tree.generation:
                    self.tree.set_children(worker.cache_path, None, error)
                return
            listed = list(entries)
            self.cache.put(worker.cache_profile, worker.cache_path, listed)
            current = self._profile_provider()
            # The tree may have been redrawn for another folder or profile.
            if worker.generation == self.tree.generation and current is not None and current.name == worker.cache_profile.name:
                self.tree.set_children(worker.cache_path, listed)
        finally:
            worker.deleteLater()
            if self.thread is not None:
                self.thread.quit()

    @Slot()
    def _thread_finished(self) -> None:
        self.thread = None
        self.worker = None
        self._start_next()

    def cancel(self) -> None:
        self._queue.clear()
        if self.worker is not None:
            self.worker.cancel()

    def shutdown(self, wait_ms: int = 6000) -> None:
        """Stop any listing before the window closes."""
        thread = self.thread
        self.cancel()
        if thread is not None and thread.isRunning():
            thread.quit()
            thread.wait(wait_ms)
