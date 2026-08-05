from __future__ import annotations

import sys


def main() -> int:
    """Dispatch GUI mode or the isolated Steam worker mode."""
    if "--steam-worker" in sys.argv:
        from .worker import main as worker_main

        return worker_main()

    from .app import run

    return run()


if __name__ == "__main__":
    raise SystemExit(main())
