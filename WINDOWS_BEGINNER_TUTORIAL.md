# QRIS Rsync Manager
## A Windows setup and first-transfer guide

For researchers who have already been invited to their UQ QRIScloud collection.

You will install the transfer tools, connect your account, and practise with one small text file. You do not need Python or programming experience.

**Use this PDF alongside the app's First-time setup guide.** After opening the app in step 2, click **First-time setup** beside the **Profile** controls. The PDF explains its steps in more detail and then walks you through your first transfers.

Read one step, do it, then check the expected result before continuing.

### What the app does

An **upload** copies files from your computer to QRIScloud. A **download** copies them back. The app uses **rsync**, a file-copying program, over **SSH**, an encrypted connection to the server. MSYS2 supplies these programs on Windows.

The left browser shows **Local** files on your computer. The right browser shows **Remote** files on QRIScloud. A **path** is a file or folder's address, such as `C:\Users\Alex\Documents\Research`.

### Your route through this guide

1. Confirm your account details.
2. Download the Windows app.
3. Install and update MSYS2.
4. Install and check rsync and OpenSSH.
5. Create or reuse an SSH key safely.
6. Register the public key.
7. Create an app profile.
8. Test SSH and browse your collection.
9. Compare and upload a tiny test folder.
10. Download the test file into a separate folder.
11. Understand folder destinations and automatic archives.
12. Resolve common problems.

### Two windows for commands

A **terminal** is a window where you type commands. This guide labels every command with the window to use.

- **PowerShell:** open Start, type `PowerShell`, and open Windows PowerShell. Its prompt usually begins with `PS C:\`.
- **MSYS2 MSYS:** open Start, type `MSYS2 MSYS`, and open that shortcut. Its prompt includes `MSYS` and usually ends with `$`.

Copy only the text inside a command box, paste it into the named terminal, and press Enter. Run command boxes in order. Do not type `PS C:\` or `$`. Keep quotation marks exactly as shown.

Example names such as **Alex**, **uqabcdef**, and **Q0101** are illustrations. Replace them with your details. PowerShell's `$env:USERPROFILE` automatically means your own Windows user folder; leave that text unchanged.

<!-- pagebreak -->

## 1. Confirm your account details

You have already been invited through [QRIScloud services](https://services.qriscloud.org.au/services). This guide assumes that your UQ account has access to the collection. An SSH key lets you log in; it does not grant collection access.

1. Have your invitation or collection information open.
2. Write down the **UQ login name** that received access. Use the login name, not your email address. If you have staff and student identities, use the one that was granted access.
3. Find the **collection ID**, a capital `Q` followed by four digits, such as `Q0101`. A collection is the shared storage area for your research data.
4. If the invitation shows only a project name, ask the person who invited you for its Q number and the exact login identity they added. Ask whether you have permission to upload, as well as to read files.

**Checkpoint:** you know your username, collection ID, and whether you can write to the collection. The official [UQ RDM collection guide](https://www.qriscloud.org.au/support/qriscloud-documentation/93-using-qrisdata-collections) explains why the account identity matters. This guide does not require you to create another portal account.

## 2. Download and open the app

**Window: your web browser, then File Explorer.**

1. Open the [latest QRIS Rsync Manager release](https://github.com/lucas-langlois/qris-rsync-manager/releases/latest).
2. Find **Assets** on that release. Click **QRISRsyncManager.exe**. Choose the executable, rather than either **Source code** archive.
3. In File Explorer, open **Downloads**. Move the executable into a folder where you can find it again, for example `Documents\QRIS Rsync Manager`.
4. Double-click the executable. It may take a moment to open.
5. If Windows shows a SmartScreen warning, check that the file came from the project's release page. If you trust that download and your institution permits it, choose **More info**, then **Run anyway**. If IT blocks it or the option is unavailable, ask IT for help.

**Expected result:** the app opens with profile controls at the top, Local and Remote browsers, transfer buttons, and a Log panel. It may report that rsync or SSH is missing; the next steps install them.

You can keep the app open while reading, but close it before updating MSYS2. The executable contains the application itself. The transfer programs are installed separately.

<!-- pagebreak -->

## Use the in-app setup guide

Click **First-time setup** beside the **Profile** selector at the top of the app. It opens **First-time QRIScloud setup**. Use **Next** and **Back** to move through its pages; the table shows where to find the matching explanations in this PDF.

| In-app setup page | Matching PDF step |
| --- | --- |
| 1. Enter the details you were given | Step 1: username and collection ID. |
| 2. Install rsync and SSH | Steps 3-4: MSYS2, updates, and tool checks. |
| 3. Create your SSH key | Step 5: check existing keys before creating one. |
| 4. Register the public key | Step 6: add the public key to your account. |
| 5. Check the profile | Step 7: review and save the connection settings. |
| 6. Test before transferring files | Step 8: Test SSH and load your collection; steps 9-10: practise transfers. |

The setup guide explains what to do and offers **Copy** buttons; it does not run those commands for you. Paste them into the terminal named in the instructions. Complete each action once, using the app or PDF instructions. After closing the app for MSYS2 updates, reopen **First-time setup** when you reach PDF step 7 to create the profile.

<!-- pagebreak -->

## 3. Install and update MSYS2

**Window: your web browser and the MSYS2 installer.**

1. Close QRIS Rsync Manager and any existing MSYS2 terminals.
2. Open the [official MSYS2 website](https://www.msys2.org/).
3. Download the installer offered for your computer. Follow the site's current Windows and hardware requirements. If you are unsure which installer matches your machine, ask IT.
4. Run the installer and keep the installation folder `C:\msys64`.
5. Complete installation. The installer may open a UCRT64 terminal. Close that window and open **MSYS2 MSYS** from Start for the commands below.

If MSYS2 is already installed at `C:\msys64`, update it rather than installing another copy. On a shared server, IT may need to do this because ordinary users cannot write to the installation folder. Each researcher should still use their own Windows account and SSH key.

**Window: MSYS2 MSYS.** Paste and run:

```bash
pacman -Syu
```

`pacman` is MSYS2's software installer and updater. This command updates the installed software. When asked whether to proceed, type `Y` and press Enter.

If it says all MSYS2 processes must close, accept that request. Reopen **MSYS2 MSYS**, then run the same command again:

```bash
pacman -Syu
```

**Checkpoint:** the update completes without errors. After any requested restart, the second run must also finish. It may say there is nothing to do. Do not skip that second run after a core update.

The restart procedure follows [MSYS2's updating instructions](https://www.msys2.org/docs/updating/). If you see permission, download, or signature errors, use step 12 before continuing.

## 4. Install rsync and OpenSSH

**Window: MSYS2 MSYS.** Run:

```bash
pacman -S --needed rsync openssh
```

Type `Y` if asked. OpenSSH supplies `ssh.exe` and `ssh-keygen.exe`; the latter creates your login key. The official package pages list [rsync](https://packages.msys2.org/packages/rsync) and [OpenSSH](https://packages.msys2.org/packages/openssh).

**Window: PowerShell.** Run these checks, one line at a time:

```powershell
& "C:\msys64\usr\bin\rsync.exe" --version
& "C:\msys64\usr\bin\ssh.exe" -V
```

The `&` tells PowerShell to run the program at the quoted address.

**Expected result:** the first command prints rsync version information; the second prints an OpenSSH version. Version numbers will vary. If either file is missing, stop here and check the installation path and package command.

<!-- pagebreak -->

## 5. Create or safely reuse an SSH key

An **SSH key pair** consists of two files. The app uses the private file to prove your identity; the server holds the matching public file.

| File | What to do with it |
| --- | --- |
| `qriscloud_ed25519` | Private key. Keep it on your computer and choose it in the app. Never email, upload to your research collection, or share it. |
| `qriscloud_ed25519.pub` | Public key. Register this file with QRIScloud. It ends in `.pub`. |

`Ed25519` is the name of the key type. `qriscloud_ed25519` is a suggested filename, not your username. A **passphrase** protects the private key if someone obtains the file. It is separate from your UQ password. [Microsoft's key guide](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_keymanagement) explains the pair and why the private key must stay private.

### Check first

**Window: PowerShell.** Run:

```powershell
Get-ChildItem -Force "$env:USERPROFILE\.ssh" -ErrorAction SilentlyContinue
```

This lists names, not private key contents. No output can simply mean the folder does not yet exist.

1. If you already have an OpenSSH private key and its matching `.pub` file for this account, you can reuse them. You must know its passphrase, if one was set. Skip creation and use those filenames in steps 6 and 7.
2. If `qriscloud_ed25519` or `qriscloud_ed25519.pub` exists but you do not know what it is, leave it alone. Ask IT or the person helping with setup. Use a different filename for a new pair if needed, then use that name consistently.
3. A `.pub` file alone cannot log you in. A PuTTY `.ppk` file is not the OpenSSH private-key format used by these instructions; ask for help converting or creating a separate pair.

### Create a new pair only if those names are unused

**Window: PowerShell.** Copy the whole box:

```powershell
$qrisKey = "$env:USERPROFILE\.ssh\qriscloud_ed25519"
New-Item -ItemType Directory -Force "$env:USERPROFILE\.ssh" | Out-Null
if ((Test-Path -LiteralPath $qrisKey) -or
    (Test-Path -LiteralPath "$qrisKey.pub")) {
    Write-Host "STOP: a key file already exists. Nothing was overwritten."
} else {
    & "C:\msys64\usr\bin\ssh-keygen.exe" -t ed25519 -f $qrisKey
}
```

When asked for a passphrase, enter a memorable one and press Enter. Enter it again when asked. Nothing appears while you type; this is normal. Store it in your password manager. If an overwrite question appears, answer **n** and stop.

**Expected result:** messages say the identification and public key were saved, followed by a fingerprint and a small text picture. The folder now contains both files. Do not paste either file's contents into the app's path field.

<!-- pagebreak -->

## 6. Register your public key with QRIScloud

Creating a key on Windows does not register it on the server. This step adds the **public** key to your account's list of accepted keys. It does not send your private key.

The app's built-in guide uses SSH for this registration. QRIScloud's [UQ RDM documentation](https://www.qriscloud.org.au/support/qriscloud-documentation/93-using-qrisdata-collections) confirms password access and public-key authentication; [OpenSSH documents the accepted-key file](https://man.openbsd.org/sshd.8#AUTHORIZED_KEYS_FILE_FORMAT). This is a command-line method, not a claim that the services portal has an SSH-key upload button.

**Window: PowerShell.** Run this first:

```powershell
Get-Content "$env:USERPROFILE\.ssh\qriscloud_ed25519.pub"
```

**Expected result:** one long line beginning `ssh-ed25519`, followed by letters and numbers. Copy the whole line if support asks for your public key. If you reused a differently named key, replace `qriscloud_ed25519.pub` with its actual public filename.

### Add it to your account

**Window: PowerShell.** Copy the whole box. It asks you to type your real UQ username, so no example username is submitted by accident.

```powershell
$qrisUser = Read-Host "Enter the UQ username granted collection access"
$qrisPublicKey = "$env:USERPROFILE\.ssh\qriscloud_ed25519.pub"
if ($qrisUser -notmatch '^[A-Za-z0-9._-]+$') {
    Write-Host "STOP: enter your login name only, without spaces or @."
} elseif (-not (Test-Path -LiteralPath $qrisPublicKey)) {
    Write-Host "STOP: the public key file was not found. Check its name."
} else {
    $qrisRegisterCommand = "umask 077; mkdir -p ~/.ssh; " +
        "printf '\n' >> ~/.ssh/authorized_keys; " +
        "cat >> ~/.ssh/authorized_keys; " +
        "chmod 700 ~/.ssh; chmod 600 ~/.ssh/authorized_keys"
    Get-Content -LiteralPath $qrisPublicKey |
        & "C:\msys64\usr\bin\ssh.exe" -T "$qrisUser@ssh1.qriscloud.org.au" $qrisRegisterCommand
    Write-Host "Registration command exit code: $LASTEXITCODE"
}
```

For an existing key with another name, edit the `$qrisPublicKey` line before running. Copy the whole box so the multi-line command stays together.

1. On a first connection, SSH may ask whether you trust the server and show a **fingerprint**, a short identifier for its key. Confirm that the hostname is `ssh1.qriscloud.org.au`. Obtain the expected fingerprint from QRIScloud support or IT and compare it before typing `yes`.
2. If asked for `username@ssh1.qriscloud.org.au's password`, enter your **UQ account password**, then press Enter. Characters remain invisible.
3. Wait for the PowerShell prompt to return. An exit code of **0** means the command completed. Continue to **Test SSH** to confirm that key login actually works.

The command appends to `authorized_keys`, preserving existing key entries, and restricts access to that file. Run it once after success. If password login is refused, ask support to register the `.pub` key; do not keep guessing passwords. After repeated failed logins, QRIScloud advises waiting ten minutes before trying again.

<!-- pagebreak -->

## 7. Create your app profile

**Window: QRIS Rsync Manager.** A **profile** saves connection settings so you do not have to retype them.

1. Open `QRISRsyncManager.exe`.
2. Reopen **First-time setup** beside the **Profile** controls. This is the same in-app guide introduced in PDF step 2. Enter your actual UQ username and collection ID.
3. Work through the pages. You have already done the installation and key steps; do not create another key or repeat successful registration.
4. At the end, click **Create profile**. Review the profile form and click **OK**.

Alternatively, click **New** beside the Profile selector to enter the same settings directly.

| Field | Enter this |
| --- | --- |
| Profile name | A label you recognise, such as `Reef survey collection`. |
| Username | Your actual UQ login name that has collection access. No email address or `@host`. |
| Host | `ssh1.qriscloud.org.au` |
| Collection ID | Your actual Q number, for example `Q0101`. |
| Remote path | `/data/` followed by your Q number; for example `/data/Q0101`. |
| SSH port | `22` |
| SSH key path | Your private key file, without `.pub`. Use **Browse** to select it. |
| rsync executable | `C:\msys64\usr\bin\rsync.exe` |

**Example only:** if your Windows account is Alex, the private key could be `C:\Users\Alex\.ssh\qriscloud_ed25519`. Choose the file under your own Windows account. Paths entered in the app do not need enclosing quotation marks.

The setup wizard pre-fills the suggested key filename. If you reused another key, change **SSH key path** to its real private file. The app saves settings, but does not save your UQ password or key passphrase.

### Where is the SSH executable field?

This version has a profile field for **rsync executable**, but no editable SSH executable field. It automatically looks for `C:\msys64\usr\bin\ssh.exe`, then certain Git/OpenSSH installation paths. Keeping MSYS2 at its default location is the simplest setup.

If MSYS2 is installed elsewhere, **Browse** can change the rsync path, but that alone does not tell the app where SSH is. Ask IT to help with a supported installation; do not assume both tools can be set in the profile form.

**Checkpoint:** select your saved profile in the top selector. Check the app's tool status. Both rsync and SSH should be found. If you need to correct a value, click **Edit** and then **OK**.

<!-- pagebreak -->

## 8. Test SSH and browse your collection

**Window: QRIS Rsync Manager.**

1. Select your profile and click **Test SSH**.
2. If asked for your **SSH key passphrase**, enter the one you chose in step 5. This is not your UQ password. The app keeps it only for the current session.
3. Read the Log panel. Look for `QRIS_SSH_OK` and `SSH test succeeded using ...`. An exit code of **0** means success.
4. In the **Remote** panel, check that **Path** is `/data/` plus your actual collection ID. Click **Load**.

**Expected result:** the Remote table shows items in your collection, or an empty listing if the collection is empty. A successful SSH test confirms login, but loading the collection is the separate check that your account can access its path.

The app tries the other direct host if the first one fails. It records a new server key automatically in its own per-user trust file. That is a first-use trust policy, not an independent fingerprint verification. Later connections reject an unexpected server-key change. The terminal in step 6 and the app can use different trust files, so their first-connection behavior differs.

### Learn the browsers before transferring

1. In **Local**, click **Browse** to choose a Windows folder.
2. Double-click a folder in either table to open it. **Up** moves to its parent folder.
3. Use **Refresh** to request an up-to-date view. Revisiting a remote folder can show a cached listing until you refresh it.
4. Before any transfer, read both **Path** boxes. They tell you which local and remote folders you are working with.

**Checkpoint:** you can see your intended remote destination. If it says permission denied or cannot find the collection, check the username and Q number with the person who invited you.

### Controls to leave alone during this practice

The current app also has **New folder**, **Rename/move**, and **Delete** controls. They can change stored data. Dragging within a pane can move items after confirmation. Dragging between panes does not upload or download. Use the explicit transfer buttons for this tutorial.

**Transfer scope** determines what a transfer copies:

- **Current folder:** the open folder, including its subfolders.
- **Selected items:** highlighted local files, or one highlighted local folder, for upload. For download, select files only. Selected remote folders are not supported; open the remote folder and use **Current folder** instead.

Button labels change with scope and selection. In this guide, use **Current folder** unless a step says otherwise.

<!-- pagebreak -->

## 9. Compare and upload one tiny test folder

Use a practice folder containing only a harmless text file. Do not start with your whole Documents folder or the real dataset.

**Window: File Explorer and Notepad.**

1. In Documents, create a folder called `QRIS practice upload`. If that name is already in use locally or in the remote destination, choose a fresh practice name and use it throughout these steps.
2. Open Notepad and type `This is my QRIScloud transfer test.`
3. Save it as `hello-qris.txt` inside that practice folder. Close Notepad.

**Window: QRIS Rsync Manager.**

4. In Local, click **Browse** and choose `QRIS practice upload`. You should see only `hello-qris.txt`.
5. In Remote, open the collection folder where you have permission to put this practice upload. Ask the collection owner if you are unsure. Keep that destination open; do not create a remote folder with the practice name first.
6. Set **Transfer scope** to **Current folder**.
7. Click **Compare upload folder**. Some screens/help text call this **Compare upload**.
8. Read the confirmation. Check the source, destination, and the **Dry run / compare** mode. Click **Yes** only when those are right.
9. Wait for the comparison to finish and read the Log. It should identify your practice folder/file as something that would be copied.

A **dry run** previews a transfer. It does not copy file data or change destination files. rsync compares file size and modification time by default; a preview is not a byte-by-byte proof that two files are identical.

**Checkpoint:** the comparison finishes with exit code **0**, and lists only the intended practice material. If it lists unexpected files, choose the correct local folder and compare again.

10. Click **Upload folder**. Review the confirmation again. Real transfers may update existing files with the same destination names. They do not delete extra destination files automatically.
11. Click **Yes** and wait. Watch the progress and Log. Do not close the app or let the computer sleep.
12. When the operation finishes with exit code **0**, click **Refresh** in Remote. Open the newly uploaded `QRIS practice upload` folder. Confirm that `hello-qris.txt` is there.

**Folder address example only:** with a remote destination of `/data/Q0101`, this upload produces `/data/Q0101/QRIS practice upload/hello-qris.txt`. The app adds the local folder name under the destination. Use your real Q number.

For a small text file the transfer may finish too fast to show a useful speed or estimated time. The completion result and visible file matter more than the moving progress bar.

<!-- pagebreak -->

## 10. Download into a separate empty folder

Keep the uploaded practice folder open in Remote. Download into a fresh local folder so you can check the result without overwriting the original test file.

**Window: File Explorer.**

1. In Documents, create an empty folder named `QRIS practice download`. Use another unused name if it already exists.

**Window: QRIS Rsync Manager.**

2. In Local, click **Browse** and choose that new empty folder.
3. In Remote, check that you are **inside** `QRIS practice upload`, and can see `hello-qris.txt`. Do not leave the whole collection open for this download.
4. Set **Transfer scope** to **Current folder**.
5. Click **Compare download folder**. Review the source and destination, confirm the dry run, then read the Log. It should propose downloading the one test file.
6. After a successful comparison, click **Download folder**. Check the confirmation and click **Yes**.
7. Wait for exit code **0**. Click Local **Refresh**. Open the download folder in File Explorer and double-click `hello-qris.txt`.

**Expected result:** Notepad displays the sentence you wrote. Your original upload folder still exists locally, and a second copy is in the download folder.

### Folder destinations at a glance

These examples use Alex and Q0101 only to show how names fit together.

```text
UPLOAD: the local folder name is included

Local open folder:
  C:\Users\Alex\Documents\QRIS practice upload
Remote open destination:
  /data/Q0101
Result:
  /data/Q0101/QRIS practice upload/hello-qris.txt

DOWNLOAD: the open remote folder's contents are copied

Remote open source:
  /data/Q0101/QRIS practice upload
Local open destination:
  C:\Users\Alex\Documents\QRIS practice download
Result:
  C:\Users\Alex\Documents\QRIS practice download\hello-qris.txt
```

To update the same uploaded dataset later, keep the remote **parent destination** open when uploading its local folder. If you open the already uploaded folder and upload the same named local folder again, you can create an unwanted nested folder.

For one-file downloads, select **Selected items**, highlight a remote file, then use the compare/download buttons whose labels include **selected files**. Files go into the displayed local folder. This also lets you retrieve an individual archive.

<!-- pagebreak -->

## 11. Before uploading a real dataset

Use **Compare upload folder**, read the confirmation, and check the expected folder destination before every new dataset. Keep source files unchanged during scanning, packaging, and transfer.

### Automatic packaging for many photos or videos

An **archive** bundles many files into one file. This app uses uncompressed `.tar` archives: they group files without trying to make the data smaller. Fewer separate files can be easier for research storage to handle.

When you upload the current folder, or one selected local folder, the app checks each folder independently. A folder qualifies for packaging if the regular files **directly inside it** number more than **200**, or total more than **10 GB** (10,000,000,000 bytes). Files inside subfolders are checked in those subfolders, rather than added to the parent's direct count.

In qualifying folders, supported photos and videos go into separate archives. Files clearly associated by name with a media file, such as certain metadata sidecars, go with that file. Unrelated or ambiguous files stay separate. A folder full of non-media files can therefore still upload as individual files.

**Illustrative example:** a folder named `Dive photos` with 300 supported JPEG photos can produce `Dive photos__photos.tar` and `Dive photos__photos.tar.inventory.txt`. The inventory is a readable list of filenames, sizes, and modification times. Use the actual names shown in the app's archive plan; long or unusual folder names can be adjusted. If the photos or videos in one folder add up to more than 50 GB, the app splits them into numbered parts, such as `Dive photos__photos.part001.tar` and `Dive photos__photos.part002.tar`, and each part gets its own inventory.

1. Read the proposed archive list in the confirmation before agreeing.
2. Check the temporary-space estimate. Archives are built beside the local source folder, so that drive needs roughly enough extra space for the packaged media plus overhead. A TAR is not a compressed space-saving backup.
3. Leave the source data alone until the operation completes.
4. After upload, expect the TAR and inventory on QRIScloud rather than separate copies of those archived originals. The source files on your computer remain unchanged.

The comparison reports planned archives without building the large TAR files. It compares the loose files, then describes the archive payload the real upload will create. A real packaged upload refreshes generated archives and inventories; it is not necessarily a quick upload of only the changed photo.

Successful or cancelled operations remove the temporary package. If transfer recovery fails, the package can remain; the Log shows its location. Do not remove it while an operation is active. Existing loose copies on QRIScloud are not automatically deleted when you later upload an archive.

Selecting individual local files uploads those selected files without automatic packaging. That is useful when you intentionally need a few separate files, but does not solve storage file-count limits for a large dataset.

### Getting files out of an archive

Download the `.tar` to your workstation first. In File Explorer, use an extraction option if available, or ask IT for an approved TAR extraction tool. Extract into a separate local folder with enough disk space. The app does not automatically unpack a download. QRIScloud advises against unpacking archives into the collection itself. [Official collection guidance](https://www.qriscloud.org.au/support/qriscloud-documentation/93-using-qrisdata-collections).

<!-- pagebreak -->

## 12. Troubleshooting checkpoints

Read the **first specific error** in the Log as well as the final exit code. A nonzero exit code means an error or cancellation; do not assume a partly visible file is a complete transfer.

### Installation and key setup

| What you see | What to check or do |
| --- | --- |
| `pacman` is not recognised | Open **MSYS2 MSYS**, not PowerShell. Run the package command there. |
| MSYS2 update closes the terminal | Reopen **MSYS2 MSYS** and run `pacman -Syu` again before installing packages. |
| `Permission denied` during package installation | The MSYS2 installation may be shared or protected. Ask IT to update/install the packages. |
| Package download or signature error | Check internet access and consult [MSYS2's updating page](https://www.msys2.org/docs/updating/). Ask IT if it persists; do not disable signature checks. |
| rsync, SSH, or ssh-keygen file not found | Check `C:\msys64\usr\bin` and rerun the package installation in step 4. Restart the app after installation. |
| The key-creation box says `STOP` | A named key file exists. Keep it; reuse a known pair or choose another name. Do not overwrite it. |
| No visible letters while entering a password/passphrase | Normal terminal behavior. Type it and press Enter. |
| Public key is missing | Confirm the filename and folder. Do not send the private file as a substitute. |

### Connecting and transferring

| What you see | What to check or do |
| --- | --- |
| `Permission denied (publickey)` | Check the exact UQ identity, private key path, and registration of its matching `.pub` key. |
| Wrong passphrase or unable to load key | Use the private file without `.pub`. An encrypted key needs its original passphrase; a forgotten passphrase cannot be recovered by the app. Ask for help arranging a replacement key. |
| SSH test succeeds, but collection access fails | Check the Q number and `/data/Qnnnn` path. Ask the owner to confirm access for that exact identity. |
| Browsing works, but upload is denied | You may have read-only access. Ask the owner to confirm write permission. |
| Timeout, hostname cannot be resolved, or connection refused | Check internet access and the host spelling. The app can fall back between ssh1 and ssh2. Ask IT about outbound SSH on port 22 or required network access. |
| Server key changed / host identification warning | Stop and ask IT or QRIScloud to verify the change. Do not erase trust files or disable the check to make it go away. |
| Transfer button is disabled | Wait for the active operation to finish. For **Selected items**, select supported items in the correct pane. |
| Files appear in the wrong folder | Check both Path boxes and the upload/download examples in step 10 before repeating the transfer. |
| Not enough disk space during packaging | Check free space on the source drive against the temporary-space estimate, not just the destination quota. |
| Upload reports quota or too many files | Contact the collection owner/support. Group suitable files into archives; do not delete shared files to make room without agreement. |
| Download waits a long time or reports I/O errors | Some data may need retrieval from tape. **Recall from tape** requests retrieval for visible files; it does not download them. Contact support for persistent delays. |

**Need to stop?** Click **Stop** and wait for cancellation to finish. Source files remain; partial destination files can remain too. Compare the same source/destination again before retrying. Packaged uploads can retry interrupted connections automatically; authentication failures need correction rather than repeated retries.

<!-- pagebreak -->

## Getting help and keeping this guide

### Find the saved log

**Window: File Explorer.** Click its address bar, paste this, and press Enter:

```text
%APPDATA%\QRISRsyncManager\logs
```

Choose the log from the failed operation. The Log panel also shows the saved log path. For UQ RDM collection issues, contact **UQ ITS** first; other SSH-service issues can go to [QRIScloud support](https://qriscloud.zendesk.com/hc/en-us/requests/new). Official [UQ RDM support guidance](https://www.qriscloud.org.au/support/qriscloud-documentation/93-using-qrisdata-collections) explains this route.

Include your app version, username, Q number, operation, exact error, and relevant log. Review logs before sharing because paths and filenames can identify research material. Never include your private key, UQ password, or key passphrase.

### A short checklist for your next transfer

1. Select the correct profile and check the local and remote paths.
2. Choose **Current folder** or a supported selection.
3. Run the compare button for the direction you intend.
4. Read the confirmation, including archive and space notices.
5. Start the real transfer and check that it finishes with exit code **0**.
6. Refresh the destination and check the expected result.

### Verified sources and scope

Links in the PDF are clickable. Installation and account instructions were checked against these official sources on **12 October 2026**:

- [Project releases](https://github.com/lucas-langlois/qris-rsync-manager/releases/latest) for the downloadable executable.
- [MSYS2 installation](https://www.msys2.org/) and [updating](https://www.msys2.org/docs/updating/) for installer and restart instructions.
- MSYS2's [rsync package](https://packages.msys2.org/packages/rsync) and [OpenSSH package](https://packages.msys2.org/packages/openssh) for tool names and installation.
- [QRIScloud UQ RDM collection guide](https://www.qriscloud.org.au/support/qriscloud-documentation/93-using-qrisdata-collections) for account identity, SSH access, storage behavior, and support.
- [Microsoft OpenSSH key management](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_keymanagement) for key-pair and passphrase guidance.
- [OpenSSH authorized-key documentation](https://man.openbsd.org/sshd.8#AUTHORIZED_KEYS_FILE_FORMAT) for public-key registration on the server.

App labels, profile fields, folder destinations, and archive behavior were checked against the repository's README, built-in setup guide, and implementation at **aa876b2**. The registration command adapts the built-in method to use the installed MSYS2 SSH program explicitly and set accepted-key permissions. It has not been run against a live QRIScloud account for this guide.

The guide uses text path diagrams rather than simulated app screenshots. The editable source is `WINDOWS_BEGINNER_TUTORIAL.md` in the repository; the printable guide is `output/pdf/QRIS_Rsync_Manager_Windows_Guide.pdf`.
