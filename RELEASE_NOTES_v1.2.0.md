# QRIS Rsync Manager v1.2.0

Version 1.2.0 adds copies between QRISdata collections that run entirely on
the QRIScloud server, and WinSCP-style tools for organising files in every
pane.

## New

- **Collection ↔ Collection mode.** Switch **Mode** to
  **Collection ↔ Collection** to copy data from one collection to another, for
  example from `/data/Q0101` to `/data/Q0202`. The local pane becomes a second
  QRIScloud browser. rsync runs on the QRIScloud SSH host, so the data never
  passes through your PC.
  - Copy only: nothing is deleted or moved, and `--delete` is never used.
  - The source's owner and group are not carried over (`--no-o --no-g`).
  - The two panes must show different collections. `/data/Qxxxx` and
    `/QRISdata/Qxxxx` count as the same collection.
  - Each collection's unreadable `.afm` storage-metadata folder and
    `.qris-receipts` folders are never copied.
  - Running the same copy again skips files already copied.
- **Collection dropdowns.** Both QRIScloud panes have a **Collection** dropdown
  filled from your profiles. Picking one opens that collection and does not
  change the selected profile. The collection open in one pane is greyed out in
  the other.
- **Organising files and folders.** Every pane now has:
  - a right-click menu
  - keyboard shortcuts: Enter, Backspace, F2 (rename), F5 (refresh), F6
    (move to), F7 (new folder), Del, Ctrl+X/Ctrl+V and Ctrl+Shift+C (copy path)
  - a `..` row in the QRIScloud panes; drop items on it to move them up a level
  - **Move to…**, which opens a folder picker
  - Cut/Paste to move items to another folder in the same pane

  Every move shows the source and destination and asks for confirmation.
  Existing items are never overwritten. Moves stay inside one pane, and
  QRIScloud moves stay inside the collection the items come from.

## Changed

- **Rename/move** is now two buttons: **Rename** (name only) and **Move to…**.
- After a refresh, the same files stay selected. Opening another folder clears
  the selection, so an action never applies to an item you can no longer see.
- Folder listings refresh after every remote operation, including one that
  failed or was stopped.
- Receipts record the collection that was actually used, even when the
  dropdown has moved away from the profile's own collection.

## Validation

- Full offline test suite: **250 passed, 4 skipped**. One test,
  `test_live_process_that_closes_stdout_is_stopped`, fails in this development
  environment on v1.1.0 as well.
- A small collection-to-collection copy and its re-run were checked live on
  QRIScloud. The new move, rename, new-folder and delete actions, and a
  whole-collection copy, have not yet been run against a live collection.

Download `QRISRsyncManager.exe` below. MSYS2 rsync and SSH remain separate
prerequisites.
