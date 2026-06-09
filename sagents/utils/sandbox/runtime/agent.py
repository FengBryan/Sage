from __future__ import annotations

import argparse
import os
import signal
import time


def reap_children() -> int:
    reaped = 0
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return reaped
        except InterruptedError:
            continue
        if pid == 0:
            return reaped
        reaped += 1


def run_forever() -> int:
    running = True

    def stop(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    def reap(_signum: int, _frame: object) -> None:
        reap_children()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGCHLD, reap)
    print("sage-sandbox-metric-agent started", flush=True)
    while running:
        reap_children()
        time.sleep(1)
    reap_children()
    print("sage-sandbox-metric-agent stopped", flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sage-sandbox-metric-agent")
    parser.add_argument("--check", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.check:
        print("sage-sandbox-metric-agent ok")
        return 0
    return run_forever()


if __name__ == "__main__":
    raise SystemExit(main())
