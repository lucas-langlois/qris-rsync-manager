# QRIS Rsync Manager v1.1.0

Version 1.1.0 adds transfer receipts with SHA-256 checksums, makes automatic
archiving smarter, and splits very large archives into parts.

## New

- **Transfer receipts.** Every upload or download that moves files writes a
  receipt into a hidden `.qris-receipts` folder inside the synced folder. It
  records the date, profile, collection, and each transferred file with its
  size, modification time, and SHA-256 checksum, as JSON plus a
  `sha256sum`-compatible `.sha256` file. Uploads keep a copy both locally and
  on QRIScloud; downloads keep it locally only. Check any copy later with
  `sha256sum -c .qris-receipts/<receipt>.sha256` from the synced folder.
- **Archive parts.** An archive larger than 50 GB is split into numbered parts
  (`folder.part01.tar`, `folder.part02.tar`, ...), each with its own inventory.
- **SSH key suggestion.** New and edited profiles pre-fill an empty SSH key
  from your other profiles or a single QRIScloud key in `~/.ssh`, and warn
  before saving a profile without a key.

## Changed

- **Archiving is based on file type, not size.** A folder is archived when
  more than 50 of its direct files share one type, for example 704 `.png`
  tiles. A few large files, such as GeoTIFF rasters or videos, now upload as
  ordinary files and stay directly usable on QRIScloud. Previously any folder
  over 10 GB was archived. Any file type can qualify, not only photos and
  videos.
- **Archive names.** Each qualifying folder gets one archive named after the
  folder, `folder.tar`. Files that share a base name, such as shapefile sets
  and `.xmp` or `.json` sidecars, stay in the same archive and part.
- `.qris-receipts` folders are never transferred as ordinary data and are
  ignored by **Find changed files**.

Re-uploading a folder that was archived by an older version creates the new
archive names alongside the old ones. The app never deletes remote files, so
remove old archives yourself if they are no longer needed.

## Validation

- Full offline test suite: **197 passed, 4 skipped**.
- Verified end to end with a 36 GB packaged upload to a QRISdata collection:
  2 archives, 383 files, and receipts written locally and on QRIScloud.

Download `QRISRsyncManager.exe` below. MSYS2 rsync and SSH remain separate
prerequisites.
