# QRIS Rsync Manager

QRIS Rsync Manager is a Windows desktop app for moving research datasets between a local Windows workstation and QRIScloud QRISdata collections using MSYS2 `rsync` over SSH. It can also copy data directly between two collections without the data passing through the workstation.

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
- WinSCP-style file management in every pane: right-click menus, F2/F5/F6/F7/Del/Backspace keys,
  a `..` row, Cut/Paste moves, and a Move to… folder picker
- Confirmed drag-and-drop moves within the local pane or within the displayed remote collection
- Create folders, rename, move, and safely remove selected items in any pane
- Local deletion uses the Windows Recycle Bin; remote deletion requires typed confirmation
- QRIScloud connection profiles
- Atomic profile saving, backup recovery, and isolation of malformed profile records
- SSH key authentication with in-app passphrase prompt
- Session-only passphrase reuse, shared by profiles that use the same SSH key; passphrases are not saved
- First-time setup guide for MSYS2, rsync, SSH keys, QRIScloud key registration, and profiles
- Per-user QRIScloud host-key trust with protection against unexpected server identity changes
- Automatic fallback between `ssh1.qriscloud.org.au` and `ssh2.qriscloud.org.au`
- Upload and download with rsync
- Collection ↔ Collection mode for server-side copies between QRISdata collections
- Compare/dry-run for upload and download
- WinSCP-style **Synchronise** window: side-by-side differences, tick boxes and per-file direction, archive-aware
- Automatic, uncompressed TAR packaging for folders with many files of one type, with inventory files
- QRIScloud Medici tape-recall requests for visible remote files
- Live rsync log panel
- Progress bar with speed and ETA where rsync reports it
- Responsive background scanning plus Stop/cancel for transfers, listings, archive preparation, recall, and sync selection
- Safe shutdown that cancels active operations and cleans up child processes
- Unicode and long Windows/UNC path support for packaged uploads
- Logs saved to `%APPDATA%\QRISRsyncManager\logs`

See [the v1.3.0 release notes](RELEASE_NOTES_v1.3.0.md) for the Synchronise window and folder trees, [the v1.2.0 release notes](RELEASE_NOTES_v1.2.0.md) for collection-to-collection copies and file management, [the v1.1.0 release notes](RELEASE_NOTES_v1.1.0.md) for transfer receipts and type-based archiving, [the v1.0.2 release notes](RELEASE_NOTES_v1.0.2.md) for recovery and Unicode fixes, and [the v1.0.0 release notes](RELEASE_NOTES_v1.0.0.md) for the complete feature list since v0.2.0.

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

The app may ask for the SSH key passphrase. It keeps the passphrase in memory for the current app session only; it is not saved into the profile. Profiles that use the same SSH key file share it, so switching profile does not ask again. If QRIScloud rejects the key, the remembered passphrase is cleared and you are asked again.

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

## Organising Files and Folders

Each pane (local, QRIScloud, and the second collection pane) works like a
WinSCP file panel. All panes are folder trees: click a folder's arrow to look
inside it without leaving the current folder, or double-click it to open it.
QRIScloud folders are listed the first time you expand them and then
remembered until you click **Refresh**. Right-click an item, or use the keyboard while the file list
has focus:

| Action | Key | What it does |
| --- | --- | --- |
| Open | Enter | Open the selected folder |
| Up one level | Backspace | Go to the parent folder |
| Refresh | F5 | Reload the folder listing |
| New folder… | F7 | Create a folder in the current folder |
| Rename… | F2 | Rename the selected item (name only) |
| Move to… | F6 | Pick a destination folder for the selected items |
| Cut / Paste | Ctrl+X / Ctrl+V | Cut items, open another folder, paste to move them there |
| Copy path | Ctrl+Shift+C | Copy the selected (or current) path to the clipboard |
| Delete… | Del | Delete the selected items |

Other ways to move items:

- Drag selected items onto a folder. In the QRIScloud panes you can drop them
  on the `..` row to move them up one level.
- Paste goes into the selected folder, or into the current folder when no
  single folder is selected.

Every move shows the exact source and destination and asks for confirmation.
Existing items are never overwritten.

Everything stays inside one pane:

- Dragging between panes does nothing, and you can only paste in the pane you
  cut from. Uploads, downloads and collection copies are always explicit
  button actions.
- QRIScloud moves stay inside the collection the items come from.
- Local Move to… and Paste can reach any folder on the same drive. Dragging
  stays within the displayed local folder.
- Keyboard shortcuts follow the buttons, so they do nothing while an operation
  is running.

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

## Automatic Archives

When **Upload folder** or **Compare upload folder** scans a directory tree, each
folder is checked independently. A folder is automatically packaged when more
than 50 of the regular files directly inside it share one file type, for
example 704 `.png` tiles or 300 `.csv` files. Case is ignored, and `.jpg/.jpeg`,
`.tif/.tiff`, `.mpg/.mpeg`, and `.htm/.html` count as one type. Size alone never
triggers packaging, so a few large rasters or videos upload as ordinary files
and stay directly usable on QRIScloud.

Each qualifying folder gets one uncompressed TAR archive named after the folder:
a folder called `survey` produces `survey.tar`. It holds every file of each
qualifying type plus files that share their names, so shapefile sets
(`.shp/.shx/.dbf/.prj`), sidecars such as `IMG_0001.xmp` or `clip.mp4.json`, and
`.aux.xml` files stay together. All other files stay outside the TAR and are
uploaded normally. Each TAR has a UTF-8 `.inventory.txt` file listing its
filenames, sizes, and modification times.

When an archive would exceed 50 GB, it is split into numbered parts such as
`survey.part01.tar`, `survey.part02.tar`, and so on. Each part has its own
inventory, and related files always stay in the same part. A single file larger
than 50 GB gets a part of its own.

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
session-only cache that makes revisiting directories immediate. Its
**Collection** dropdown lists your saved profiles by name (those that use the
same username as the selected profile); picking one opens that collection in
the pane without changing the selected profile. **Refresh**
always requests a new listing from QRIScloud.

You can create folders, rename, move, and delete selected remote items (see
**Organising Files and Folders**). Remote moves are constrained to the
collection being displayed and require confirmation. Remote deletion requires typing `DELETE`; collection roots and
unsafe paths are protected. Drag and drop moves items only within the same pane.
It never starts an upload or download.

Local deletion moves explicitly selected files or folders to the Windows
Recycle Bin.

## Copy Between Collections

Switch **Mode** to **Collection ↔ Collection** to copy data from one QRISdata
collection to another, for example from `/data/Q0101` to `/data/Q0202`. The
local pane is replaced by a second QRIScloud browser. Pick a collection by
profile name from its **Collection** dropdown, or type any path, then click
**Load**. The right pane has the same dropdown, so either side can switch
collections quickly. Profiles only fill these lists; picking a collection does
not change the selected profile. The two panes must show different collections:
the collection open in one pane is greyed out in the other pane's dropdown, and
a copy within a single collection is refused (use **Move to…** or Cut/Paste for that).

The transfer buttons become **Compare/Copy left → right** and
**Compare/Copy right → left**. With **Current folder**, the source pane's
folder is copied into the destination pane's folder under its own name. With
**Selected items**, the selected files and folders are copied the same way.
The confirmation shows each source and where it will land.

How it works and what to expect:

- rsync runs on the QRIScloud SSH host, so the data never passes through your PC
  and your network speed does not matter.
- Both panes use the selected profile's account. That one account must have
  access to both collections.
- It is copy only. Nothing is deleted or moved, and `--delete` is never used.
  Files with the same name at the destination may be updated.
- The source's owner and group are not copied (`--no-o --no-g`). Copied files
  are owned the same way as files this account uploads.
- Files on Medici tape are recalled when rsync reads them, which can be slow.
  For large folders, use **Recall from tape** first.
- **Stop** closes the SSH connection. The server-side rsync normally stops
  within seconds, but may continue briefly, for example while it waits on a tape
  recall. Running the copy again skips files already copied.
- Transfer receipts are not written for collection copies.
- The `.afm` folder in each collection root is QRIScloud storage metadata that
  your account cannot read, so it is never copied.
- Both panes must be loaded before a copy starts, so the app knows both folders
  exist and are accessible.

## Recall from Tape

Use **Recall from tape** when visible files have been migrated from QRIScloud
disk cache to Medici tape. The action requests retrieval; it does not download
the files. Recall runs in cancellable batches, reports periodic status, and can
fall back between the two QRIScloud SSH hosts.

## Synchronise

Click **Synchronise…** to compare the current local folder with the current
QRIScloud folder side by side, like WinSCP's synchronise view. The app reads
both folders (name, size and modified time), then opens a window listing only
the differences:

| Row colour | Meaning | Default action |
| --- | --- | --- |
| Green | Only on this computer, or newer here | Ticked, **→ Upload** |
| Blue | Only on QRIScloud, or newer there | Ticked, **← Download** |
| Amber | Same time but a different size | Unticked: you decide |
| Purple 📦 | A folder that uploads as a `.tar` archive | Ticked, **→ Upload** |

- Tick or untick any row. Click the **Action** cell to swap a file's direction.
- **Direction** at the top resets the ticks: **Both ways** (newer file wins),
  **Upload only**, or **Download only**.
- Select several rows (Shift/Ctrl-click) and use **Tick**, **Untick**,
  **→ Upload** or **← Download**, or the right-click menu.
- Filter by name, or tick **Show only ticked** to review what will happen.
- The summary shows how many items and bytes go each way, and warns when a
  ticked file would replace a newer copy.

**Synchronise** asks for confirmation, then uploads the ticked files and, if
that succeeds, downloads the ticked files. Both write transfer receipts.

Automatic archives are handled the same way as **Upload folder**:

- A folder that qualifies for archiving appears as one 📦 row, not one row per
  file. Uploading it packs it into `folder.tar` first.
- The app knows an archive on QRIScloud is up to date without opening it: the
  uploaded inventory is generated from the files, so its size and time are
  compared with what the local files would produce. A matching archive is
  counted as "already the same".
- If any file in the folder changed, the row says **Archive out of date** and
  uploading rebuilds the archive.
- Archives that exist only on QRIScloud start unticked. Downloading one copies
  the `.tar`; the app does not unpack it.

Nothing is ever deleted on either side. Files that exist on only one side are
copied across or left alone.

If nothing matches and the QRIScloud folder contains a folder with the same
name as your local folder, the window offers to compare with that folder
instead. This catches the common slip of opening the parent folder you
uploaded into.

Notes:

- Empty folders are not included.
- Very large QRIScloud folders can take a while to list. Use **Stop** to cancel.
- **Compare upload** and **Compare download** still run an rsync dry run and
  show its output in the log.

## Logs

Logs are saved to:

```text
%APPDATA%\QRISRsyncManager\logs
```

Other app data, including profiles and temporary file lists, is saved under:

```text
%APPDATA%\QRISRsyncManager
```

## Transfer Receipts

Every real upload or download that transfers at least one file writes a
receipt: the date, profile, collection, host, and each transferred file with its
size, modification time, and SHA-256 checksum. Packaged uploads list the TAR and
inventory files they sent. Compare/dry-run operations write no receipt.

Each receipt has two files with the same name, for example
`20261111T120922Z_upload.json` and `20261111T120922Z_upload.sha256`. Receipts
are never overwritten, so they build up a history of transfers.

Receipts are stored in a hidden `.qris-receipts` folder inside the folder that
was synced, and nowhere else:

- **Uploads**: in the local folder and in the matching QRIScloud folder. For
  example, uploading `D:\Surveys\img` to `/data/Q0101/project` writes receipts
  to `D:\Surveys\img\.qris-receipts\` and
  `/data/Q0101/project/img/.qris-receipts/`.
- **Downloads**: in the local download folder only. Nothing is written to
  QRIScloud during a download.
- **Interrupted transfers**: a local receipt ending in `_incomplete` records
  what was sent before the failure. Nothing is uploaded for it.

`.qris-receipts` folders are never transferred as ordinary data and are ignored
by **Synchronise**.

File paths in a receipt are relative to the synced folder. rsync already
verifies each file it transfers with its own checksum. The receipt adds a
permanent SHA-256 record that you can check at any time. On QRIScloud or an HPC
system, run this from the synced folder:

```bash
sha256sum -c .qris-receipts/20261111T120922Z_upload.sha256
```

Computing checksums reads every transferred file once more on the local side,
so very large transfers take a little longer to finish.

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

### Synchronise seems slow

The comparison recursively lists the selected QRIScloud folder. This can be slow for large QRIScloud folders. Use **Stop** to cancel it.

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

### Build the PDF Guide

The printable beginner guide is generated from `WINDOWS_BEGINNER_TUTORIAL.md`.
Edit the Markdown, then rebuild (requires the `packaging` extras, which include
ReportLab):

```powershell
.\envs\qris-rsync-manager\python.exe packaging\build_tutorial_pdf.py
```

The PDF is written to:

```text
dist\QRIS_Rsync_Manager_Windows_Guide.pdf
```

### Create a GitHub Release

After building the executable:

```powershell
git tag v1.3.0
git push origin main v1.3.0
gh release create v1.3.0 .\dist\QRISRsyncManager.exe --title "QRIS Rsync Manager v1.3.0" --notes-file .\RELEASE_NOTES_v1.3.0.md
```

If GitHub CLI is not authenticated:

```powershell
gh auth login -h github.com
```
