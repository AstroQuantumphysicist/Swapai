"""Entry point for `swapai` / `python -m swapai`."""

from __future__ import annotations

import sys


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in ("serve", "--headless"):
        # Headless mode: run only the API server (no TUI).
        from . import config
        from .server import ServerThread
        import time
        config.load_env()
        srv = ServerThread()
        srv.start()
        print(f"SwapAI server on http://{srv.host}:{srv.port}/v1")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            srv.stop()
        return
    from .tui import run
    run()


if __name__ == "__main__":
    main()
