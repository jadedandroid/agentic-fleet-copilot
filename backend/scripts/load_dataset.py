"""Load the device telemetry NDJSON dataset into a running backend through the public API.

Usage (from backend/):
    AFC_BOOTSTRAP_ADMIN_KEY=... AFC_DATASET_ADMIN_PASSWORD=... \
        python scripts/load_dataset.py --file ../device-telemetry-dataset.ndjson --base-url http://127.0.0.1:8000

Secrets come from environment variables only, never from argv.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.dataset_adapter import DatasetRejected, build_snapshots, read_ndjson  # noqa: E402
from app.dataset_loader import load_snapshots  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", type=Path, required=True, help="Path to the NDJSON dataset")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()

    bootstrap_key = os.environ.get("AFC_BOOTSTRAP_ADMIN_KEY", "")
    admin_password = os.environ.get("AFC_DATASET_ADMIN_PASSWORD", "")
    if not bootstrap_key or len(admin_password) < 12:
        print("Set AFC_BOOTSTRAP_ADMIN_KEY and AFC_DATASET_ADMIN_PASSWORD (12+ characters).", file=sys.stderr)
        return 2

    records, parse_errors = read_ndjson(args.file)
    try:
        snapshots = build_snapshots(records, parse_errors)
    except DatasetRejected as error:
        print(json.dumps({"rejected": [e.__dict__ for e in error.errors[:20]]}, indent=2), file=sys.stderr)
        return 1

    with httpx.Client(base_url=args.base_url, timeout=30.0) as client:
        summary = load_snapshots(client, snapshots, bootstrap_key, admin_password)

    print(json.dumps({"records": len(records), "companies": summary}, indent=2))
    return 0 if all(item["status"] != "failed" for item in summary.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
