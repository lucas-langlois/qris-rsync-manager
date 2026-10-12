# QRIS Rsync Manager v1.0.2

Version 1.0.2 fixes packaged-upload recovery during temporary connection outages
and archive creation with supplementary Unicode characters in filenames.

## Fixes

- After an interrupted transfer, known transient SSH host-check failures now
  consume the remaining bounded retry attempts instead of stopping recovery
  immediately. Authentication and ambiguous SSH failures stop retries.
- TAR command batching now measures Windows UTF-16 code units, preventing valid
  Unicode filenames from producing an oversized command line. A single member
  that cannot fit produces an explicit error before starting TAR.

## Validation

- Added regression tests for transient recovery, non-network failures, retry
  exhaustion, cancellation, Unicode batching, and an oversized single member.
- Full offline test suite: **165 passed, 4 skipped**.

Download `QRISRsyncManager.exe` below. MSYS2 rsync and SSH remain separate
prerequisites.
