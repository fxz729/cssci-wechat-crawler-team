"""Small atomic stop-control file used by long client rollout commands."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path


def _write(path: Path, payload: dict) -> dict:
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
    temporary.replace(path)
    return payload


def reset_stop(path: str | Path) -> dict:
    return _write(
        Path(path),
        {
            "schema_version": 1,
            "stop_requested": False,
            "reset_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def request_stop(path: str | Path) -> dict:
    return _write(
        Path(path),
        {
            "schema_version": 1,
            "stop_requested": True,
            "requested_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def read_control(path: str | Path) -> dict:
    path = Path(path).resolve()
    if not path.exists():
        return {"schema_version": 1, "stop_requested": False, "exists": False}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RuntimeError("run_control_invalid") from None
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or type(value.get("stop_requested")) is not bool
    ):
        raise RuntimeError("run_control_invalid")
    return value


def is_stop_requested(path: str | Path) -> bool:
    return read_control(path)["stop_requested"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("reset", "request", "status"))
    parser.add_argument("--path", required=True)
    args = parser.parse_args(argv)
    if args.action == "reset":
        result = reset_stop(args.path)
    elif args.action == "request":
        result = request_stop(args.path)
    else:
        result = read_control(args.path)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
