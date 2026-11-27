# QRIS Rsync Manager Agent Instructions

QRIS Rsync Manager is a Windows desktop PySide6 application for reliable rsync-based uploads, downloads, and comparison against QRIScloud QRISdata collections, plus server-side copies between collections.

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

Collection ↔ Collection copies (`build_collection_copy_command`) run rsync on the QRIScloud host over ssh, using `COLLECTION_COPY_RSYNC_OPTIONS`. They are copy only: no `--delete`, no `--remove-source-files`, no moves. Keep `--no-o --no-g` so the source collection's owner and group are not carried into the destination. Do not add `--timeout`, because reads can block on Medici tape recall. Keep `--exclude=.afm/`: each collection root has a root-owned, unreadable `.afm` folder that would otherwise fail a whole-collection copy. Both paths must be inside a QRISdata collection, source and destination must be in different collections (compared by collection ID, so `/data/Qxxxx` and `/QRISdata/Qxxxx` count as the same), and the destination must not be inside a source.

SSH keepalive defaults:

```text
ssh -o ServerAliveInterval=60 -o ServerAliveCountMax=10
```

## Security

Never:

- store passwords or passphrases in plain text
- log passwords, passphrases, or private key contents
- concatenate shell command strings for execution

Prefer SSH key authentication. Passphrases may be held in memory for the current app session only, and are passed to ssh through the askpass helper (`app/core/askpass.py`). The session cache is keyed by SSH key file (`passphrase_cache_key`), so profiles sharing a key share it. It must be cleared when ssh reports the key was rejected (`ssh_key_was_rejected`), so a mistyped passphrase is never reused.

QRIScloud host keys are trusted per user. Do not weaken the check that rejects an unexpected server identity.

Profile export and import (`export_profiles`, `read_profile_file`, `adapt_profile_to_this_computer`, `merge_imported_profiles` in `app/core/profiles.py`) must never include secrets or private keys. Import accepts the export format or a copied `profiles.json` (with or without a BOM) and points key and rsync paths at the current computer.

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
- Remote file actions are shared by both remote panes through the `side` argument (`"right"` or `"left"`) in `main_window.py`. Drag, Move to… and Paste all go through `_move_remote_items`, which reuses `build_remote_move_into_command`.
- Remote moves stay inside the collection the items come from. Collection roots and unsafe paths are protected (`is_dangerous_remote_path`). Rename is name-only.
- Remote panes are folder trees (`RemoteMoveTree` in `app/gui/drag_views.py`). Expanding a folder lists it in the background through `RemoteFolderLoader` (read-only, its own thread, results cached). Double-click opens a folder.
- Remote trees select entries by stored path, never by row position, because the `..` row and the "Loading…" note rows are display-only and never part of the entries list. Entries inside expanded folders are stored on their items; selected-file downloads keep their subfolder.
- Layout: upload buttons sit under the left pane, download buttons and the **Tape status and recall ▾** menu under the QRIScloud pane (outside the pane boxes, so they stay visible when Collection ↔ Collection mode swaps the left pane). The three tape menu items click hidden buttons, so they follow the same enabled rules. Test SSH is in the Profile box; Stop is by the progress bar.
- Right-click menus and keyboard shortcuts (`app/gui/pane_actions.py`) are scoped to the file list widget and gated on the matching button's enabled state.
- Cut/Paste is internal (not the system clipboard), bound to the pane it was cut from, consumed by any paste attempt in that pane (a refused paste in another pane keeps it), and cleared on profile change.
- Local drag moves stay inside the displayed folder. Local Move to… and Paste may reach any folder on the same drive and refuse other drives.
- Remote deletion requires the user to type `DELETE`.
- Local deletion sends items to the Windows Recycle Bin (`app/core/local_delete.py`) and never deletes permanently.
- Drag and drop, Cut/Paste and Move to… move items within one pane only. They never start an upload, download or collection copy.

Do not add chmod, mirror, or `--delete` behavior. Any new destructive action needs a confirmation at least as strong as the existing ones.

Medici tape recall (`app/core/medici.py`) only requests retrieval. It does not download files.

**Check tape status** (`build_tape_status_command`, `app/gui/tape_status.py`) must stay read-only: it lists size and allocated blocks with `find -printf` and never opens file data, because reading a file on tape starts a recall. A file over 4 KB that uses less than half its size on disk is on tape: tape stubs use 0-1 blocks (checked live on /data/Q5722, a 7.7 MB photo on tape used 1 block). Files of 4 KB or less are always counted as on disk, because GPFS keeps them in the inode.

Large recalls go to Bunya as a Slurm job (`write_bunya_recall_job`), because RCC requires recalls to run on a compute node, not a login node, with at most 4 concurrent recalls. The app writes the script and an explicit file list (recall_medici does not recurse reliably) with Unix line endings. **Submit to Bunya** (`app/core/bunya.py`, `BunyaSubmitWorker`) sends both over one SSH login and runs `sbatch`; nothing else runs on Bunya. Bunya needs key + MFA, so that connection uses `build_interactive_askpass_environment`: ssh runs the app itself with `--askpass` (`app/gui/askpass_prompt.py`), which shows the prompt and returns the answer on stdout. Never store or log MFA codes, and keep `NumberOfPasswordPrompts=1` so a wrong code cannot trigger repeated MFA pushes.

Bunya and QRIScloud share the same disk cache (checked live on 26 Nov 2026: identical block counts from both sides, and files recalled on Bunya appear on disk on QRIScloud). So **Tape status and recall → Recall jobs on Bunya… → Check progress** (`app/gui/recall_jobs.py`) checks a job's own file list from QRIScloud with the normal key and no MFA; only **Show Bunya job log** logs in to Bunya. Submitted jobs are recorded in `%APPDATA%\QRISRsyncManager\bunya_jobs\jobs.json`. Send file lists to ssh as bytes with `\n` line endings: text-mode pipes on Windows add `\r` to every path. Paths handed to `recall_medici` are passed through `glob_escape`, because it expands every argument as a glob pattern.

For sync selection, compare:

- relative path
- file size
- modified timestamp

The Synchronise window (`app/gui/sync_dialog.py`, rules in `build_sync_plan` in `app/core/sync_compare.py`):

- Never deletes. One-sided rows can only be copied across or skipped.
- Defaults: the newer copy wins; conflicts (same time, different size) and archives that exist only on QRIScloud start unticked.
- A folder that qualifies for automatic archiving is one row. Its members are never uploaded loose; uploading it builds the archive through `prepare_upload_package` (see `SyncUploadWorker`).
- An archive is up to date when the remote inventory's size and mtime match what `archive_expectations` computes locally. Do not read remote archives or inventories: they may be on tape.
- The remote listing prunes `.afm` and `.qris-receipts`.
- Uploads run first. Downloads start only if the upload exited 0. Both write receipts.

No live QRIScloud access should be required for unit tests.

## File Count Warnings

Before upload, scan local folders and warn when there are very high file counts or many tiny files. Recommend archiving when appropriate.

## Automatic Archives

Planning and building live in `app/core/archive_upload.py`. The README documents the thresholds (more than 50 direct files of one type per folder, 50 GB parts). Keep README and code in sync when they change.

Rules:

- Never modify or delete source files.
- Name archives `<folder>_<types>.tar` (`PILOT_txt.tar`, `.partNN.tar` for parts) so the name never suggests the whole folder. Synchronise must keep recognising the pre-1.3.3 `<folder>.tar` names (`legacy_archive_name`) so old archives are not re-sent.
- Use uncompressed `.tar` with a UTF-8 `.inventory.txt` sidecar. Keep files that share a base name (sidecars, shapefile sets) in the same archive and part.
- Prefer Windows `tar.exe` and fall back to MSYS2 tar.
- Compare/dry-run must not build TAR files.
- Remove temporary packages after success or cancel. Keep them after retries are exhausted and log where they are.
- Real uploads send loose files first, then build and upload archives in batches (`plan_archive_batches`, `PackagedUploadWorker._upload_archives_in_batches`, up to 20 GB or one bigger part each), deleting each batch before building the next and hashing it for the receipt first (`hash_phase_now`). Only a failed batch is kept. Batches are built beside the source, or inside it (hidden `.<name>.qris-upload-*`, excluded from scans and rsync) when the parent is read-only, as share roots are. Keep `ARCHIVE_TEMP_RESERVE_BYTES` free on that drive for colleagues; parts that cannot fit are skipped and reported, never fatal.
- Never use `tempfile.mkdtemp` on a share: on Windows it retries "access denied" 10,000 times (looks like a hang). Use `_make_work_dir`.
- The loose-file pass leaves out archived files with a few rules per archived folder (`_folder_filter_rules`: keep subfolders, keep its loose files, leave out the rest), never one exclude line per archived file. rsync checks every path against every rule: 775,000 per-file lines cost ~8 ms per path (hours on a 1.2M-file share).
- Analysis must not stat every ancestor of every folder: on a network share each stat is a round trip. Only the root is validated during the scan; scandir entries already prove subfolders are normal directories.
- A folder that changes during the upload is skipped and reported (final code 23); the rest continues. rsync 23/24 in the loose phase do not stop the archives.
- Windows Server deduplicated files (reparse tag 0x80000013) are ordinary files. Use `is_link_or_junction` (`app/core/paths.py`), never the bare reparse-point attribute: only name-surrogate tags (symlinks, junctions) are links. `os.scandir` reports the attribute on dedup files and `os.stat` does not. `DfsrPrivate`, `System Volume Information` and `$RECYCLE.BIN` are left out of scans and uploads.
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

### Shared distribution folders

Users run the app from shared team folders, not from GitHub downloads. After every new executable build that is meant for users, copy both `dist\QRISRsyncManager.exe` and `dist\QRIS_Rsync_Manager_Windows_Guide.pdf` to **both** folders:

```text
L:\RES_MEG\NESP\How To Do Everything\Data Storage Hard-drives\QrisCloud\QRISCloud transfer app
L:\RES_MEG\NFCSecure\Custom Apps\QRISCloud transfer app
```

- Rebuild the guide PDF (`packaging\build_tutorial_pdf.py`) first if the tutorial changed.
- Run the tests and smoke-test the new executable before copying.
- Copy to a temporary `.incoming` name, check the SHA-256 hash against `dist\`, then rename over the old file, so users never open a half-copied executable.
- People run the app straight from these folders, so the old executable may be open. Replacing it then fails, and Windows can leave the old file "delete pending" with no runnable `.exe` in the folder until that person closes the app. Do not force it. Keep retrying the rename in the background, and tell the user which folder is waiting.
- Verify the final hash in both folders and report the result.
- The beginner guide tells users to open the app from the same shared folder as the guide. Do not put these paths in the guide.

## Development Guidance

When uncertain, choose the simpler, safer, more maintainable implementation. This is a research utility tool; reliability matters more than cleverness.
