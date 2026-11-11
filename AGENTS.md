# QRIS Rsync Manager Agent Instructions

QRIS Rsync Manager is a Windows desktop PySide6 application for reliable rsync-based uploads, downloads, and comparison against QRIScloud QRISdata collections.

Primary users are researchers moving large datasets between Windows workstations, QRIScloud storage, and HPC environments. The app should feel familiar to WinSCP/FileZilla users, but it is deliberately narrower and safer.

## Priorities

Always prioritize:

1. Reliability
2. Simplicity
3. Safe transfers
4. Windows compatibility
5. Maintainability
6. UI polish

Do not over-engineer. Keep the first working solution simple, testable, and resilient.

## Technical Stack

Use:

- Python 3.11+
- PySide6
- `subprocess` with argument lists and `shell=False`
- JSON settings/profiles
- PyInstaller packaging
- MSYS2 rsync/ssh as the preferred transfer tools

Avoid:

- Electron or web frontends
- Docker or WSL requirements
- admin-only workflows
- Linux-only assumptions

The app must run as a normal Windows executable.

## QRIScloud Defaults

QRIScloud collections use remote paths like:

```text
/data/Q0101
```

Generate the default remote path from the collection ID:

```python
f"/data/{collection_id}"
```

Preferred hosts:

- `ssh1.qriscloud.org.au`
- `ssh2.qriscloud.org.au`

Also support `data.qriscloud.org.au`, but prefer ssh1/ssh2 for direct host fallback.

## MSYS2 and Paths

Assume MSYS2 is installed at:

```text
C:\msys64
```

Detect:

```text
C:\msys64\usr\bin\rsync.exe
C:\msys64\usr\bin\ssh.exe
```

Path handling belongs in `app/core/paths.py`. Support spaces, Unicode, and long Windows paths. Convert Windows paths to MSYS-style paths where needed, for example:

```text
C:\Users\Lucas\Data
/c/Users/Lucas/Data
```

## Rsync Rules

Build rsync commands in `app/core/rsync_command.py` only. Always use subprocess argument lists and never `shell=True`.

Default transfer flags:

```text
-a -v -h --progress --partial -W --outbuf=N --info=progress2 --human-readable
```

Do not use `-c` by default. QRIScloud documentation warns checksum/delta behavior can be costly for stale data.

Do not use `--delete` or mirror mode unless explicitly designed later with strong confirmations.

Do not combine `--append-verify` with `-W`. Current MSYS2 rsync rejects that combination.

The one sanctioned exception to `-W` is recovery: when `resume_partial=True` (automatic retry after a dropped connection), `-W` is removed and `--no-whole-file` is added so rsync can reuse partial remote data. Keep this limited to retries.

Add `--protect-args` only when the remote path contains shell-special characters, so normal collection paths stay compatible with older receivers.

SSH keepalive defaults:

```text
ssh -o ServerAliveInterval=60 -o ServerAliveCountMax=10
```

## Security

Never:

- store passwords or passphrases in plain text
- log passwords, passphrases, or private key contents
- concatenate shell command strings for execution

Prefer SSH key authentication. Passphrases may be held in memory for the current app session only, and are passed to ssh through the askpass helper (`app/core/askpass.py`).

QRIScloud host keys are trusted per user. Do not weaken the check that rejects an unexpected server identity.

## GUI and Background Work

Keep GUI code in `app/gui/` and core logic in `app/core/`.

Long-running work must never freeze the GUI. Use worker threads for:

- SSH tests
- remote directory listing
- rsync upload/download/dry-run
- sync comparison scans

Stream live output into the log panel and save logs to:

```text
%APPDATA%\QRISRsyncManager\logs\
```

The UI should be lightweight, responsive, and practical. Prefer obvious controls and actionable messages over decorative UI.

## File Management and Sync

Remote and local file management already exists. Keep the current guardrails when changing it:

- Remote commands (mkdir, rename/move, delete) are built in `app/core/remote_ops.py` with `shlex` quoting and path validation.
- Remote moves stay inside the active collection. Collection roots and unsafe paths are protected (`is_dangerous_remote_path`).
- Remote deletion requires the user to type `DELETE`.
- Local deletion sends items to the Windows Recycle Bin (`app/core/local_delete.py`) and never deletes permanently.
- Drag and drop moves items within one pane only. It never starts an upload or download.

Do not add chmod, mirror, or `--delete` behavior. Any new destructive action needs a confirmation at least as strong as the existing ones.

Medici tape recall (`app/core/medici.py`) only requests retrieval. It does not download files.

For sync selection, compare:

- relative path
- file size
- modified timestamp

No live QRIScloud access should be required for unit tests.

## File Count Warnings

Before upload, scan local folders and warn when there are very high file counts or many tiny files. Recommend archiving when appropriate.

## Automatic Archives

Planning and building live in `app/core/archive_upload.py`. The README documents the thresholds (more than 50 direct files of one type per folder, 50 GB parts). Keep README and code in sync when they change.

Rules:

- Never modify or delete source files.
- Use uncompressed `.tar` with a UTF-8 `.inventory.txt` sidecar. Keep files that share a base name (sidecars, shapefile sets) in the same archive and part.
- Prefer Windows `tar.exe` and fall back to MSYS2 tar.
- Compare/dry-run must not build TAR files.
- Remove temporary packages after success or cancel. Keep them after retries are exhausted and log where they are.
- Do not retry authentication failures automatically.
- Selected-file uploads are exact and are never packaged.

## Error Handling

The app should not crash on:

- invalid paths
- missing rsync/ssh
- SSH permission errors
- network timeouts
- cancelled transfers
- malformed rsync progress output

Make errors clear and actionable.

## Tests

Keep tests focused and offline. At minimum cover:

- profile save/load
- rsync command generation
- SSH command generation
- remote listing parsing
- sync comparison logic
- progress parsing
- file scan warnings
- archive planning and packaged-upload recovery
- remote/local file operation guards
- worker cancellation and GUI shutdown

Run tests with:

```powershell
.\run_tests.ps1
```

The wrapper manages a project-local pytest temp directory and cleans it up.

## Packaging

Package with PyInstaller using:

```powershell
.\packaging\build_pyinstaller.ps1
```

Target executable:

```text
dist\QRISRsyncManager.exe
```

`run_tests.ps1` and the build script prefer the project-local environment at `envs\qris-rsync-manager`.

The packaged app should detect MSYS2 rsync/ssh on startup and allow custom executable paths.

## Development Guidance

When uncertain, choose the simpler, safer, more maintainable implementation. This is a research utility tool; reliability matters more than cleverness.
