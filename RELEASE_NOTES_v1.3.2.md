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

- **Archives are built and uploaded one batch at a time** after the loose
  files: up to 20 GB of small archives, or one bigger archive part. Each batch
  is deleted before the next is built, so a multi-TB backup needs little
  temporary space. Before, every archive was built first (1.5 TB for one real
  share).
- **Works from the top of a share.** Batches are built beside the source
  folder, or in a hidden folder inside it when the parent is read-only, as the
  root of `L:` is. Before, the app could seem to hang there.
- **Leaves room for colleagues.** 5 GB is always left free on the drive. An
  archive part too big for the free space is skipped and listed in the log
  instead of stopping the upload.
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
