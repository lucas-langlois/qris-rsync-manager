# QRIS Rsync Manager v1.0.1

Version 1.0.1 makes large packaged uploads recover cleanly when QRIScloud or the
network resets an SSH connection during transfer.

## Transfer recovery

- Automatically retries rsync socket, protocol, and timeout failures up to
  three times, with cancellable delays of 10, 30, and 60 seconds.
- Prefers the alternate QRIScloud SSH host after a connection failure.
- Keeps the fast whole-file mode for the initial transfer, then uses rsync block
  matching on recovery attempts so matching data in a retained partial file can
  be reused.
- Continues to replace stale remote payloads correctly, including files that
  already have the same size or are larger than the new archive.
- Does not automatically retry ambiguous SSH authentication failures, avoiding
  repeated rejected-key attempts and possible server throttling.
- Retains prepared local TAR packages when all automatic recovery attempts are
  exhausted and reports their location in the operation log.
- Removes temporary packages normally after a successful upload or explicit
  user cancellation.

## Validation

- Verified recovery behavior with the installed MSYS2 rsync, including complete,
  partial, stale same-size, and oversized destination files.
- Added deterministic tests for host fallback, retry exhaustion, delta recovery,
  retained packages, and cancellation cleanup.
- Full offline test suite: **156 passed, 4 skipped**.

Source files are never changed or deleted by packaged upload preparation. The
application still performs no automatic deletion of existing remote loose files.
