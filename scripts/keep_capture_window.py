#!/usr/bin/env python
"""Keep an enabled Fiddler capture window rolling without stealing focus."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from capture.setup import renew_capture_window


def _stop_requested(path: Path | None) -> bool:
    if path is None or not path.is_file():
        return False
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False
    return bool(value.get("stop_requested") or value.get("action") in {"stop", "pause"})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--run-control", type=Path)
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument("--minutes", type=int, default=60)
    parser.add_argument("--renew-before-minutes", type=int, default=45)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)

    if args.interval_seconds < 10:
        raise SystemExit("interval_seconds_must_be_at_least_10")
    output_dir = args.output_dir.resolve()
    run_control = args.run_control.resolve() if args.run_control else None
    print(
        json.dumps(
            {
                "event": "started",
                "pid": os.getpid(),
                "output_dir": str(output_dir),
                "run_control": str(run_control) if run_control else "",
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    while True:
        if _stop_requested(run_control):
            print(json.dumps({"event": "stopped", "reason": "stop_requested"}), flush=True)
            return 0
        try:
            renewed = renew_capture_window(
                output_dir,
                minutes=args.minutes,
                renew_before_minutes=args.renew_before_minutes,
            )
            if renewed:
                print(
                    json.dumps(
                        {
                            "event": "renewed",
                            "at": datetime.now(timezone.utc).isoformat(),
                            "minutes": args.minutes,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
        except FileNotFoundError:
            pass
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(json.dumps({"event": "error", "error": str(exc)}, ensure_ascii=False), flush=True)
        if args.once:
            return 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
