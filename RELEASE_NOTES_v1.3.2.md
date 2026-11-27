# QRIS Rsync Manager v1.3.2

Version 1.3.2 fixes uploads from Windows file shares that use data
deduplication, and makes very large backups safer and lighter on disk space.

## Fixed

- **Deduplicated files on file shares are uploaded properly.** On shares that
  use Windows Server data deduplication (such as some JCU `L:` shares), most
  files were wrongly treated as links. They were reported as "could not be
  scanned", left out of the archive plan and the size estimate, and sent to
  rsync loose instead of archived. They are now scanned, archived and uploaded
  like any other file. Found on a live share where three quarters of the files
  were deduplicated. rsync and tar were checked to read them correctly.
- **File-server system folders are left out.** `DfsrPrivate`,
  `System Volume Information` and `$RECYCLE.BIN` are no longer scanned or
  uploaded. They are unreadable and made uploads end with an error.

## Large backups

- **Archives are built and uploaded in batches** of about 100 GB, after the
  loose files. Each batch is deleted once it is on QRIScloud, so a multi-TB
  backup needs temporary space for one batch, not all of them, and archives
  start uploading early.
- **Less network traffic for network shares.** For a source on a network
  drive, batches are built on this computer when there is room, instead of
  being written back to the share and read again.
- **A share in use no longer stops the whole upload.** A folder whose files
  change during the upload is skipped and listed in the log, and the rest
  carries on. Files that vanish or cannot be read during the loose-file phase
  (rsync 24 or 23) no longer stop the archives either. Run **Synchronise…**
  later to send what was skipped.
- **A failure costs at most one batch.** Only the failed batch's archives are
  kept; earlier batches are already on QRIScloud.
- Each batch is checksummed for the transfer receipt before it is deleted.
- **Much less rsync overhead for big trees.** Archived files are now left out of
  the loose-file pass with a few rules per archived folder instead of one rule
  per file. On a 1.2-million-file share that cuts rsync's filtering work from
  about 3 hours to about 10 minutes, for uploads and for **Compare upload**.
- **Faster upload analysis on network shares.** The scan no longer re-checks
  every parent folder of every folder, one network round trip each. A
  48,000-file folder on `L:` now takes under a minute, where before it was
  still running after four.

## Advice for large shared workspaces

Upload one top-level folder at a time, and use **Synchronise…** to resume or
refresh a backup: it skips archives that are already up to date.
