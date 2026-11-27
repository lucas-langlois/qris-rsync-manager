# QRIS Rsync Manager v1.3.4

## Changed

- **Receipts no longer re-read your data.** The checksum for each transferred
  file now comes from rsync itself: rsync computes it while sending the file,
  and the receiving side checks it. Before, the app read every transferred file
  a second time after the upload to compute its checksum. For a 3.2 TB backup
  from a network share, that was a second 3.2 TB read. Archive batches no
  longer re-read their TAR files either.
- **Receipts use MD5.** Each receipt now comes with a `.md5` file; check it with
  `md5sum -c`. MD5 is what rsync can report, and it is ample for spotting
  damaged or incomplete copies. Receipts from earlier versions keep their
  `.sha256` file.
- Checked against QRIScloud, which runs rsync 3.4.1, and with a test that runs
  the real rsync and compares its checksums with `md5sum`.
