# QRIS Rsync Manager v1.0.0

Version 1.0.0 is the first feature-complete release of QRIS Rsync Manager for
routine Windows-to-QRIScloud collection workflows. It adds safer file
management, guided setup, selective transfers, large-media packaging, faster
remote navigation, tape recall, and substantial reliability improvements.

## Highlights

- Transfer a displayed folder or explicitly selected files and folders.
- Browse local and remote directories side by side, with cached repeat remote
  navigation and reliable double-click handling.
- Create folders, rename or move items, delete selected items, and drag items
  into folders within the local or remote pane.
- Automatically package large flat photo/video folders as uncompressed TAR
  archives with human-readable inventory sidecars.
- Guide new users through MSYS2, rsync, SSH key creation, public-key
  registration, host trust, and QRIScloud profile setup.
- Find missing or changed files and upload only the generated selection.
- Request Medici tape recall for remote files before downloading them.

## Transfers and file selection

- Added explicit **Current folder** and **Selected items** transfer scopes.
- Added multi-file and multi-folder upload, download, and dry-run comparison.
- Folder uploads preserve the source folder name beneath the selected remote
  destination.
- Selected-item actions show counts and reject empty or ambiguous selections.
- Local folder scans run in the background with progress and cancellation, so
  large trees do not freeze the interface.
- Existing safe rsync defaults remain in place: resumable partial files, whole
  file transfer, live progress, no mirror mode, and no automatic destination
  deletion.

## Automatic media packaging

- Each directory is packaged when either its directly contained regular files
  exceed 200 items or their combined size exceeds 10 GB.
- Photos and videos are separated into distinct uncompressed TAR archives.
- Recognizable media sidecars are placed beside their corresponding media in
  the archive; unrelated files remain loose and upload normally.
- Every TAR includes a UTF-8 `.inventory.txt` sidecar with filename, size, and
  modification time.
- The confirmation dialog shows the proposed archives and required temporary
  space before work begins.
- Windows' built-in native `tar.exe` is preferred, with MSYS2 tar as a fallback,
  for significantly faster archive creation and large buffered I/O.
- Source files are never modified or removed. Temporary payloads are cleaned up
  after success, failure, or cancellation.
- Added support for archive and inventory paths beyond the traditional Windows
  260-character limit, including UNC network shares.
- Packaging output and pre-rsync failures are now saved to the operation log.
- Folder-preserving exclusions are anchored to rsync's actual transfer root, so
  archived originals are not also uploaded as individual files.
- Compare mode prepares exclusions and reports planned payloads without writing
  source-sized temporary TAR files.
- The source plan is revalidated immediately before payload transfer, and TARs
  plus inventories are always refreshed to prevent equal-size/equal-time stale
  payloads from being skipped.
- Indexed sidecar association removes quadratic matching costs for very large
  flat media folders.

## Local and remote file management

- Added new-folder, rename/move, and delete actions to both panes.
- Added confirmed drag-and-drop moves within the local pane and within the
  active remote collection.
- Cross-pane drag and drop is deliberately disabled; uploads and downloads
  remain explicit actions.
- Local deletion sends only explicitly selected top-level items to the Windows
  Recycle Bin and avoids traversing links or junctions.
- Remote rename, move, and delete commands use argument-safe quoting and reject
  dot segments, collection roots, and other dangerous paths.
- Remote deletion requires typed `DELETE` confirmation.

## Remote browsing and SSH setup

- Added a bounded, profile-specific, session-only cache for immediate repeat
  navigation; **Refresh** bypasses the cache.
- Coalesced rapid/double-click navigation so stale listing results cannot
  replace the requested directory.
- Added automatic fallback between `ssh1.qriscloud.org.au` and
  `ssh2.qriscloud.org.au` for listings and operations.
- Added explicit UTF-8 subprocess decoding with safe replacement for malformed
  output, preventing non-ASCII filenames from crashing workers.
- Added an in-app first-time setup guide with tool detection and copyable SSH
  key registration commands.
- Explained that Ed25519 is the key type and that `qriscloud_ed25519` is only a
  suggested filename.
- Added automatic per-user QRIScloud host-key trust. New hosts are accepted on
  first use, while unexpected identity changes remain blocked.
- SSH key passphrases can be reused in memory for the current app session and
  are never stored in profiles or logs.

## Sync comparison and tape recall

- Added **Find changed files** and **Upload changed files** using relative path,
  size, and modification time.
- Remote manifests are parsed incrementally with bounded memory use and fail
  closed on malformed or incomplete output.
- Sync scans enforce cancellation and timeouts even when SSH produces no output.
- Selection lists are published atomically and removed when replaced or no
  longer applicable.
- Added Medici recall for visible remote files, including batching, heartbeat
  messages, host fallback, retry handling, cancellation, and process cleanup.

## Reliability and safety

- Hardened cancellation before, during, and immediately after process launch so
  stopped operations cannot start later.
- Added bounded terminate-to-kill cleanup for SSH, rsync, listings, comparison,
  and recall processes.
- Added safe application shutdown that cancels active work and waits for worker
  threads instead of destroying live threads.
- Prevented stale remote results and old thread completions from affecting a
  newer operation or profile/path.
- Unified busy-state handling to prevent overlapping transfers, listings, and
  comparisons.
- Made profile saves atomic with a known-good backup, recovery diagnostics, and
  per-record validation so one malformed profile cannot prevent startup.
- Added robust handling for logging failures, read-only locations, Unicode
  output, connection errors, and malformed remote records.
- Expanded offline regression coverage for process lifecycle, cancellation,
  path safety, long paths, archive planning, profile recovery, GUI state, and
  command construction.

## Installation and upgrade notes

- Windows users download `QRISRsyncManager.exe` from this release.
- MSYS2 remains a separate prerequisite. The app expects `rsync.exe` and
  `ssh.exe` under `C:\msys64\usr\bin` by default. Windows' built-in `tar.exe` is
  used for archive creation when available.
- Existing profiles remain compatible without a manual migration step.
- On a shared Windows server or VM, IT can install MSYS2 once for all users, but
  each user must create and register their own SSH key and profile.
- The executable is not code-signed, so Windows may show a security warning on
  first launch.

## Verification

- Full offline test suite: 151 passed, 4 skipped.
- PyInstaller one-file Windows executable built successfully.
- Packaged executable startup smoke test passed.
