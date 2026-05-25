# QRIS Rsync Manager

QRIS Rsync Manager is a Windows desktop app for moving research datasets between a local Windows workstation and QRIScloud QRISdata collections using MSYS2 `rsync` over SSH.

The app is designed for practical QRIScloud workflows: side-by-side local/remote browsing, dry-run comparison, resumable uploads/downloads, live logs, progress display, and safe defaults.

## Features

- PySide6 Windows desktop GUI
- Side-by-side local and remote directory browser
- QRIScloud connection profiles
- SSH key authentication with in-app passphrase prompt
- Session-only passphrase reuse; passphrases are not saved
- Automatic fallback between `ssh1.qriscloud.org.au` and `ssh2.qriscloud.org.au`
- Upload and download with rsync
- Compare/dry-run for upload and download
- WinSCP-style upload selection for missing/changed files
- Live rsync log panel
- Progress bar with speed and ETA where rsync reports it
- Stop/cancel for transfers and sync-selection scans
- Logs saved to `%APPDATA%\QRISRsyncManager\logs`

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

## Install MSYS2, rsync, and SSH

The app expects MSYS2 tools at:

```text
C:\msys64\usr\bin\rsync.exe
C:\msys64\usr\bin\ssh.exe
```

### Option A: Install MSYS2 with winget

Open PowerShell:

```powershell
winget install MSYS2.MSYS2
```

Then open **MSYS2 MSYS** from the Start Menu and run:

```bash
pacman -Syu
```

Close the MSYS2 window if it asks you to, reopen **MSYS2 MSYS**, then run:

```bash
pacman -S --needed rsync openssh
```

Check from PowerShell:

```powershell
& "C:\msys64\usr\bin\rsync.exe" --version
& "C:\msys64\usr\bin\ssh.exe" -V
```

### Option B: Install from the MSYS2 website

Download and install MSYS2 from:

```text
https://www.msys2.org/
```

Install to the default location, `C:\msys64`, then run the same `pacman` commands above.

## SSH Key Setup

Create a QRIScloud SSH key:

```powershell
ssh-keygen -t ed25519 -f "$env:USERPROFILE\.ssh\qriscloud_ed25519"
```

Set the profile SSH key path to the private key:

```text
C:\Users\<you>\.ssh\qriscloud_ed25519
```

Do not use the `.pub` file in the app. The `.pub` file is the public key you provide to QRIScloud:

```text
C:\Users\<you>\.ssh\qriscloud_ed25519.pub
```

The app may ask for the SSH key passphrase. It keeps the passphrase in memory for the current app session only.

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

## Run From Source

This project uses Python 3.11+.

### Conda

```powershell
cd qris_rsync_manager
conda create -n qris-rsync-manager python=3.11 pip
conda activate qris-rsync-manager
python -m pip install -r requirements.txt
python -m app.main
```

### Project-local conda environment

```powershell
cd qris_rsync_manager
conda create -p .\envs\qris-rsync-manager python=3.11 pip
conda activate ".\envs\qris-rsync-manager"
python -m pip install -r requirements.txt
python -m app.main
```

## Basic Workflow

1. Start the app.
2. Create or select a profile.
3. Click **Test SSH**.
4. Select a local folder in the left pane.
5. Load and browse the remote folder in the right pane.
6. Use **Compare / dry-run** before upload.
7. Use **Upload** to upload the local folder contents to the remote path.
8. Use **Compare download** before download.
9. Use **Download** to download remote contents into the selected local folder.

## Sync Selection

Use **Build sync selection** to compare the current local folder against the current remote path.

The comparison checks:

- relative file path
- file size
- modified timestamp

It creates a temporary rsync `--files-from` list containing files that are missing remotely or appear changed.

Use **Upload selection** to upload only those files.

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

## Run Tests

```powershell
cd qris_rsync_manager
python -m pytest
```

Tests do not require live QRIScloud access, rsync execution, or network access.

## Build the EXE

Install packaging dependencies and build:

```powershell
cd qris_rsync_manager
python -m pip install -e ".[packaging]"
.\packaging\build_pyinstaller.ps1
```

The executable is written to:

```text
dist\QRISRsyncManager.exe
```

## Create a GitHub Release

After building the executable:

```powershell
git tag v0.1.0
git push origin main --tags
gh release create v0.1.0 .\dist\QRISRsyncManager.exe --title "QRIS Rsync Manager v0.1.0" --notes "Initial MVP release."
```

If GitHub CLI is not authenticated:

```powershell
gh auth login -h github.com
```

## Troubleshooting

### SSH test times out

Try again, or switch between `ssh1.qriscloud.org.au` and `ssh2.qriscloud.org.au`. The app also attempts automatic fallback between these hosts.

### Permission denied

Check:

- username
- SSH key path points to the private key, not `.pub`
- the public key is registered with QRIScloud
- passphrase is correct

### Progress bar does not move

The app uses rsync `--outbuf=N` and `--info=progress2`, but rsync may still spend time building the incremental file list before progress begins.

### Build sync selection seems slow

The remote manifest step recursively scans the selected remote path. This can be slow for large QRIScloud folders. Use **Stop** to cancel it.

