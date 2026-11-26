from __future__ import annotations

import sys


def main() -> int:
    # ssh starts the app as its SSH_ASKPASS helper for interactive logins (Bunya MFA).
    if len(sys.argv) >= 2 and sys.argv[1] == "--askpass":
        from app.gui.askpass_prompt import main as askpass_main

        return askpass_main(sys.argv[2:])
    from app.gui.main_window import show

    return show()


if __name__ == "__main__":
    raise SystemExit(main())
