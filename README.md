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

Creating the key files does not register the key with the server. The in-app guide provides a PowerShell command that logs in once with your normal UQ password and adds only the public `.pub` key to your remote account. Never share the private key file.

See the official [QRISdata collection guide](https://www.qriscloud.org.au/support/qriscloud-documentation/93-using-qrisdata-collections) for account, collection path, host, and support details, and [Microsoft's OpenSSH key guide](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_keymanagement) for Windows key-pair details.

## First Run

1. Start `QRISRsyncManager.exe`.
2. Create or select a profile.
3. Click **Test SSH**.
4. Select a local folder in the left pane.
5. Load and browse the remote folder in the right pane.
6. Use **Compare upload** before upload.
7. Use **Upload** to upload the local folder contents to the remote path.
8. Use **Compare download** before download.
9. Use **Download** to download remote contents into the selected local folder.

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
git tag v0.1.0
git push origin main --tags
gh release create v0.1.0 .\dist\QRISRsyncManager.exe --title "QRIS Rsync Manager v0.1.0" --notes "Initial MVP release."
```

If GitHub CLI is not authenticated:

```powershell
gh auth login -h github.com
```
