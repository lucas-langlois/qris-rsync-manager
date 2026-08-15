# QRIS Rsync Manager

QRIS Rsync Manager is a Windows desktop app for moving research datasets between a local Windows workstation and QRIScloud QRISdata collections using MSYS2 `rsync` over SSH.

The app is designed for practical QRIScloud workflows: side-by-side local/remote browsing, dry-run comparison, resumable uploads/downloads, live logs, progress display, and safe defaults.

Most users should download and run the `.exe` from the latest GitHub release. You do not need to install Python unless you want to develop or build the app yourself.

## Download the App

Download the latest Windows executable from:

```text
https://github.com/lucas-langlois/qris-rsync-manager/releases/latest
```

Use:

```text
QRISRsyncManager.exe
```

The `.exe` contains the Python application. You still need MSYS2 `rsync` and `ssh` installed separately because those are the transfer tools used by QRIScloud.

On first launch, Windows may show a security warning because this is not yet a code-signed application. Choose **More info** and **Run anyway** if you trust the downloaded release.

## Features

- Windows desktop GUI
- Side-by-side local and remote directory browser
- Fast repeat navigation with a bounded, session-only remote directory cache
- Current-folder or multi-item selection for uploads, downloads, and comparisons
- Confirmed drag-and-drop moves within the local pane or within the active remote collection
- Create folders, rename/move items, and safely remove selected items in either pane
- Local deletion uses the Windows Recycle Bin; remote deletion requires typed confirmation
- QRIScloud connection profiles
- Atomic profile saving, backup recovery, and isolation of malformed profile records
- SSH key authentication with in-app passphrase prompt
- Session-only passphrase reuse; passphrases are not saved
- First-time setup guide for MSYS2, rsync, SSH keys, QRIScloud key registration, and profiles
- Per-user QRIScloud host-key trust with protection against unexpected server identity changes
- Automatic fallback between `ssh1.qriscloud.org.au` and `ssh2.qriscloud.org.au`
- Upload and download with rsync
- Compare/dry-run for upload and download
- WinSCP-style upload selection for missing/changed files
- Automatic, uncompressed TAR packaging for large flat photo/video folders, with inventory sidecars
- QRIScloud Medici tape-recall requests for visible remote files
- Live rsync log panel
- Progress bar with speed and ETA where rsync reports it
- Responsive background scanning plus Stop/cancel for transfers, listings, archive preparation, recall, and sync selection
- Safe shutdown that cancels active operations and cleans up child processes
- Unicode and long Windows/UNC path support for packaged uploads
- Logs saved to `%APPDATA%\QRISRsyncManager\logs`

See [the v1.0.1 release notes](RELEASE_NOTES_v1.0.1.md) for the latest transfer-recovery improvements and [the v1.0.0 release notes](RELEASE_NOTES_v1.0.0.md) for the complete feature list since v0.2.0.

## Install MSYS2, rsync, and SSH

Install MSYS2 before using the app. The app expects these tools at:

```text
C:\msys64\usr\bin\rsync.exe
C:\msys64\usr\bin\ssh.exe
```

If you are unfamiliar with these tools, open the app and click **First-time setup**. The wizard checks whether both programs are present and explains the installation and verification steps.

### Option A: Install MSYS2 with winget

Open PowerShell:

```powershell
winget install MSYS2.MSYS2
```

Then open **MSYS2 MSYS** from the Start Menu and run:

```bash
pacman -Syu
```

If MSYS2 asks you to close the terminal, close it, reopen **MSYS2 MSYS**, then run:

```bash
pacman -S --needed rsync openssh
```

Check from PowerShell:

```powershell
& "C:\msys64\usr\bin\rsync.exe" --version
& "C:\msys64\usr\bin\ssh.exe" -V
```

### Shared Windows servers and VMs

IT can install MSYS2 once at `C:\msys64` for all users of a shared Windows
machine. The `rsync.exe` and `ssh.exe` programs can be shared, but each user must
create and register their own SSH key and will have their own app profiles and
host-trust file. Package installation and updates may require IT or an
administrator to run the MSYS2 terminal; ordinary users can receive
`Permission denied` errors from `pacman` when the shared installation is not
writable.

### Option B: Install from the MSYS2 website

Download and install MSYS2 from:

```text
https://www.msys2.org/
```

Install to the default location, `C:\msys64`, then run the same `pacman` commands above.

## SSH Key Setup

The instructions below assume you already have a QRIScloud/UQ account and that this account has been granted access to the Q collection. In the app, click **First-time setup** beside the profile controls for a beginner-friendly walkthrough covering rsync, SSH, the key pair, and a pre-filled profile form.

Have your UQ username and collection ID (for example `Q0101`) ready. If you have both staff and student identities, use the exact identity that was granted collection access.

Create a QRIScloud SSH key if you do not already have one. `Ed25519` is simply the
secure type of SSH key being created; users do not need to configure or understand
the cryptography. `qriscloud_ed25519` is a suggested descriptive filename, not a
QRIScloud requirement:

```powershell
ssh-keygen -t ed25519 -f "$env:USERPROFILE\.ssh\qriscloud_ed25519"
```

Set the app profile SSH key path to the private key:

```text
C:\Users\<you>\.ssh\qriscloud_ed25519
```

Do not use the `.pub` file in the app. The `.pub` file is the public key you provide to QRIScloud:

```text
C:\Users\<you>\.ssh\qriscloud_ed25519.pub
```

The app may ask for the SSH key passphrase. It keeps the passphrase in memory for the current app session only; it is not saved into the profile.

On the first connection, the app automatically records the QRIScloud server identity in a per-user file under `%APPDATA%\QRISRsyncManager`. A new host is trusted once; an unexpected identity change is rejected. Users do not need to configure MSYS2 `known_hosts` manually, including on shared Windows servers.

Creating the key files does not register the key with the server. The in-app guide provides a PowerShell command that logs in once with your normal UQ password and adds only the public `.pub` key to your remote account. Never share the private key file.

See the official [QRISdata collection guide](https://www.qriscloud.org.au/support/qriscloud-documentation/93-using-qrisdata-collections) for account, collection path, host, and support details, and [Microsoft's OpenSSH key guide](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_keymanagement) for Windows key-pair details.

## First Run

1. Start `QRISRsyncManager.exe`.
2. Create or select a profile.
3. Click **Test SSH**.
4. Select a local folder in the left pane.
5. Load and browse the remote folder in the right pane.
6. Choose **Current folder** or **Selected items** under **Transfer scope**.
7. Use **Compare upload** before uploading.
8. Use **Upload folder** or **Upload selected items** to transfer data without deleting destination files.
9. Use **Compare download** before downloading.
10. Use **Download folder** or **Download selected files** to transfer data into the displayed local folder.

Within either browser pane, selected items can be dragged onto a folder to move
them. The app shows the exact source and destination before moving. Dragging
between the local and remote panes is intentionally disabled; uploads and
downloads remain explicit button actions.

## Profile Setup

Create or edit a profile with:

- Profile name, for example `Q8940`
- Username, for example your QRIScloud login
- Host, usually `ssh1.qriscloud.org.au`
- Collection ID, for example `Q8940`
- Remote path, usually `/data/Q8940`
- SSH port, usually `22`
- SSH key path
- rsync executable path, usually `C:\msys64\usr\bin\rsync.exe`

If the host is `ssh1.qriscloud.org.au` or `ssh2.qriscloud.org.au`, the app automatically tries the other host as a fallback.

## Automatic Media Archives

When **Upload folder** or **Compare upload folder** scans a directory tree, each
folder is checked independently. A folder is automatically packaged when either
of these conditions is true:

- it contains more than 200 regular files directly inside that folder; or
- those direct files total more than 10 GB.

Photos and videos are placed into separate, uncompressed TAR archives. Files
whose names clearly match a media filename, such as an XMP or JSON sidecar, are
kept with that media file. Ambiguous or unrelated files stay outside the TAR and
are uploaded normally. Each TAR has a UTF-8 `.inventory.txt` file listing its
filenames, sizes, and modification times.

The confirmation window shows the proposed archives before work begins. The
source files are never modified or deleted. TAR files are built in a temporary
folder beside the upload source and uploaded with rsync. Successful and cancelled
operations remove the temporary package. If recovery from a transfer failure is
exhausted, the package is retained and its location is shown in the log. Because
media files are normally already compressed, the app uses `.tar` rather than
`.tar.gz` to avoid costly compression with little storage benefit.

**Compare upload folder** does not build the potentially very large TAR files.
It compares the non-archived files and reports every planned TAR and inventory
as a payload that the real upload will create. During a real upload, archived
originals are excluded from the loose-file phase, the source is revalidated
before the payload phase, and generated TAR/inventory files are always refreshed
without an expensive remote checksum.

If QRIScloud resets a connection during a packaged upload, the app keeps the
partial remote file and retries automatically with increasing delays. It tries
the other QRIScloud SSH host first and uses rsync block matching on recovery so
matching data already received can be reused. Authentication failures are not
automatically retried.

Automatic packaging applies to the current folder and to one explicitly selected
folder. Uploading individually selected files remains an exact selected-file
operation and does not package them. Folder uploads preserve the selected or
displayed folder name under the chosen remote destination.
Existing individual files already stored remotely are not deleted automatically.

Archive creation prefers Windows' built-in native `tar.exe`, with MSYS2 tar as a
fallback, for much faster streaming of large media collections. The packaged
build supports long local and UNC paths beyond the traditional Windows
260-character limit. Packaging progress and failures are recorded in the
operation log even if rsync has not started.

## Remote File Management

The remote browser supports double-click navigation, **Up**, **Refresh**, and a
session-only cache that makes revisiting directories immediate. **Refresh**
always requests a new listing from QRIScloud.

You can create folders, rename or move selected items, and delete selected
remote items. Remote moves are constrained to the active collection and require
confirmation. Remote deletion requires typing `DELETE`; collection roots and
unsafe paths are protected. Drag and drop moves items only within the same pane.
It never starts an upload or download.

Local deletion moves explicitly selected files or folders to the Windows
Recycle Bin.

## Recall from Tape

Use **Recall from tape** when visible files have been migrated from QRIScloud
disk cache to Medici tape. The action requests retrieval; it does not download
the files. Recall runs in cancellable batches, reports periodic status, and can
fall back between the two QRIScloud SSH hosts.

## Sync Selection

Use **Find changed files** to compare the current local folder against the current remote path.

The comparison checks:

- relative file path
- file size
- modified timestamp

It creates a temporary rsync `--files-from` list containing files that are missing remotely or appear changed.

Use **Upload changed files** to upload only those files. This differs from **Compare upload**: it creates a reusable list of missing or changed files for a selective upload.

Notes:

- This does not delete remote files.
- This does not mirror folders.
- Empty directories are not included yet.
- Very large remote trees can take time to scan.

## Logs

Logs are saved to:

```text
%APPDATA%\QRISRsyncManager\logs
```

Other app data, including profiles and temporary file lists, is saved under:

```text
%APPDATA%\QRISRsyncManager
```

## Safety Defaults

Default rsync flags:

```text
-a -v -h --progress --partial -W --outbuf=N --info=progress2 --human-readable
```

Important QRIScloud choices:

- `-W` is enabled to avoid rsync checksum/delta behavior that can be costly for stale QRIScloud data.
- `-c` is not used by default.
- `--delete` is not implemented.
- Mirror/delete mode is intentionally absent.
- `--append-verify` is not used with `-W` because rsync rejects that flag combination.

SSH keepalive options:

```text
ssh -o ServerAliveInterval=60 -o ServerAliveCountMax=10
```

## Troubleshooting

### SSH test times out

Try again, or switch between `ssh1.qriscloud.org.au` and `ssh2.qriscloud.org.au`. The app also attempts automatic fallback between these hosts.

### Connection reset by peer

First confirm that WinSCP or MSYS2 SSH can connect from the same machine. A
temporary QRIScloud connection limit can cause the first attempt to reset; wait
briefly and try **Test SSH** again. The app automatically tries both QRIScloud
SSH hosts. If MSYS2 SSH has never connected for this Windows user, use
**First-time setup** to register the key and let the app establish its per-user
host trust.

### Permission denied

Check:

- username
- SSH key path points to the private key, not `.pub`
- the public key is registered with QRIScloud
- passphrase is correct

### Progress bar does not move

The app uses rsync `--outbuf=N` and `--info=progress2`, but rsync may still spend time building the incremental file list before progress begins.

### Find changed files seems slow

The remote manifest step recursively scans the selected remote path. This can be slow for large QRIScloud folders. Use **Stop** to cancel it.

## Developer Setup

Most users do not need this section. Use it only if you want to run the app from source, change the code, run tests, or build a new `.exe`.

This project uses Python 3.11+.

### Run From Source

With conda:

```powershell
cd qris_rsync_manager
conda create -n qris-rsync-manager python=3.11 pip
conda activate qris-rsync-manager
python -m pip install -r requirements.txt
python -m app.main
```

With a project-local conda environment:

```powershell
cd qris_rsync_manager
conda create -p .\envs\qris-rsync-manager python=3.11 pip
conda activate ".\envs\qris-rsync-manager"
python -m pip install -r requirements.txt
python -m app.main
```

### Run Tests

```powershell
cd qris_rsync_manager
.\run_tests.ps1
```

Tests do not require live QRIScloud access, rsync execution, or network access.

The test wrapper creates a project-local temporary directory for pytest and removes it when the test run finishes. This avoids Windows/OneDrive temp-directory permission issues.

### Build the EXE

Install packaging dependencies and build:

```powershell
cd qris_rsync_manager
.\packaging\build_pyinstaller.ps1
```

The build script uses the project-local conda environment at `envs\qris-rsync-manager`.
For a fresh environment, install the project into that environment first:

```powershell
.\packaging\build_pyinstaller.ps1 -Install
```

The executable is written to:

```text
dist\QRISRsyncManager.exe
```

### Create a GitHub Release

After building the executable:

```powershell
git tag v1.0.1
git push origin main --tags
gh release create v1.0.1 .\dist\QRISRsyncManager.exe --title "QRIS Rsync Manager v1.0.1" --notes-file .\RELEASE_NOTES_v1.0.1.md
```

If GitHub CLI is not authenticated:

```powershell
gh auth login -h github.com
```
