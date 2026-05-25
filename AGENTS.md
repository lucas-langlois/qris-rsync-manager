# QRIS Rsync Manager Agent Notes

Follow the repository-level `AGENT.md` instructions strictly.

MVP priorities:

1. Reliability
2. Simplicity
3. Safe transfers
4. Windows compatibility
5. Maintainability

Keep GUI code separate from rsync logic, centralize path handling in `app/core/paths.py`, and always build subprocess commands as argument lists with `shell=False`.

Never store or log passwords, passphrases, or private key contents. Do not add remote browsing, delete/mirror mode, or extra features until the MVP is complete.

