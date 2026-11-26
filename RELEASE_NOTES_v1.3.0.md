# QRIS Rsync Manager v1.3.0

Version 1.3.0 replaces "Find changed files" with a WinSCP-style
**Synchronise** window and makes the QRIScloud panes folder trees like the
local pane.

## New

- **Synchronise window.** Click **Synchronise…** to compare the local and
  QRIScloud folders side by side. Only the differences are listed, colour-coded:
  - green: upload
  - blue: download
  - amber: same time, different size, so you decide
  - purple 📦: a folder that is uploaded as a `.tar` archive

  Tick or untick any row, and click its **Action** cell to swap direction.
  **Both ways** (newer file wins), **Upload only** and **Download only** set
  the defaults. A summary shows how many items and bytes go each way and warns
  before a newer copy is replaced. Nothing is ever deleted.
- **Archive-aware comparison.** A folder that the app packs into `folder.tar`
  shows as one row, not one row per file.
  - The app knows whether the archive on QRIScloud is up to date without
    opening it, so no tape recall is triggered.
  - Uploading an out-of-date archive folder rebuilds its archive.
  - Archives that exist only on QRIScloud start unticked.
- **Folder trees for QRIScloud.** Both QRIScloud panes now look like the local
  pane, with folder icons and ▸ arrows.
  - Expanding a folder lists it in the background, and the listing is then
    remembered until **Refresh**.
  - Files inside expanded folders can be renamed, moved, deleted or downloaded
    with **Selected items**. They download into the matching subfolder.

## Fixed

- Comparisons failed with "malformed line(s)" with OpenSSH 10, which prints
  a three-line post-quantum notice. These notices are now ignored, and any
  unexpected lines are shown in the error.
- The QRIScloud file list skips each collection's unreadable `.afm` folder
  and the app's `.qris-receipts` folders.
- Transfer-receipt folders are never packed into automatic archives.

## Changed

- **Find changed files** and **Upload changed files** are replaced by
  **Synchronise…**. **Compare upload** and **Compare download** still run an
  rsync dry run.

## Validation

- Full offline test suite: **277 passed, 4 skipped**.
- The Synchronise comparison and folder-tree expansion were checked against a
  live QRISdata collection.
