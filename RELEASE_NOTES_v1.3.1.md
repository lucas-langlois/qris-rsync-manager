# QRIS Rsync Manager v1.3.1

Version 1.3.1 shows which files are on Medici tape and recalls them on UQ
RCC's Bunya cluster, so a large recall keeps going after you close the app.
It also tidies the window layout and lets you export and import profiles.

## New

- **Check tape status.** It checks the selected QRIScloud items, or the open
  folder, and shows how many files are on disk and ready to download, and
  which are on tape and how big they are.
  - It reads only file details, never file contents, so it never starts a
    recall itself.
  - How it tells: a file on tape still shows its full size but leaves a tiny
    stub on disk. Files of 4 KB or less are always counted as on disk.
  - Checked live on a QRISdata collection with files on tape.
- **Submit to Bunya.** It sends a Slurm recall job to Bunya, following RCC's
  guidance: the job runs on a compute node and recalls at most 4 files at a
  time. The recall continues after you close the app or turn off your
  computer.
  - Bunya's MFA (an Okta push or a code) and your key passphrase are asked for
    in small app windows, once per submission. They are never stored.
  - Your Bunya account (default `a_jcu_hpc`), username, key and folder (default
    `/scratch/user/<username>/qris_recall`) are remembered.
  - **Save job files only…** saves the script and file list so you can submit
    them yourself.
- **Recall jobs on Bunya.**
  - **Check progress** shows how many of a job's files are back on disk. It
    uses your normal QRIScloud key, so no MFA is needed. This works because
    Bunya and QRIScloud share the same disk cache, which was confirmed live.
  - **Show Bunya job log** shows the Slurm state and the end of the job log,
    after one MFA approval.
- **Better recall job logs.** They show progress every 5 minutes, a
  `…_done.txt` file lists each file as OK or FAILED, failed files are listed
  at the end, and the job ends with status 1 if any file failed.
- **Export and import profiles.** The new **Profiles ▾** menu in the Profile box
  has **Export profiles…** and **Import profiles…**, for using your profiles on
  another computer such as a VM.
  - The export contains no passwords or passphrases, and SSH keys are never
    included.
  - On import, key paths point to the same key file name in that user's `.ssh`
    folder, and rsync points to that computer's MSYS2.
  - If a profile with the same name already exists, you choose to replace yours
    or keep yours.

## Changed

- **Save profiles** moved into the **Profiles ▾** menu.
- **Window layout:**
  - Upload buttons sit under the local pane.
  - Download buttons and a single **Tape status and recall ▾** menu sit under
    the QRIScloud pane.
  - **Test SSH** moved to the Profile box, and **Stop** sits by the progress
    bar.
- **Recall files in this folder now** (the former **Recall from tape**) is
  still available, for small recalls while the app is open.

## Fixed

- Recalls of file names containing `[`, `*` or `?` failed, because
  `recall_medici` treats every name as a wildcard pattern. Names are now
  escaped.
- OpenSSH's post-quantum notice no longer clutters the recall log.

## Validation

- Full offline test suite: **317 passed, 4 skipped**.
- Live:
  - tape status checked on `/data/Q5722`
  - a Bunya recall job submitted with Okta MFA, then seen running
  - files recalled on Bunya confirmed back on disk from QRIScloud
