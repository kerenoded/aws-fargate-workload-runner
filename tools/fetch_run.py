"""Laptop CLI: download run artifacts from S3.

Usage:
  python tools/fetch_run.py <RUN_ID> [--out-dir test-results]

Downloads summary.json and metrics.jsonl for the given RUN_ID into
  <out-dir>/<RUN_ID>/
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import boto3

if __package__ is None and __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.tf_outputs import terraform_outputs

TF_DIR = Path(__file__).resolve().parents[1] / "infra" / "terraform"
ARTIFACTS = ["summary.json", "metrics.jsonl"]


def main() -> int:
    ap = argparse.ArgumentParser(description="Download run artifacts from S3")
    ap.add_argument("run_id", help="RUN_ID (uuid4) to fetch")
    ap.add_argument("--out-dir", default="test-results", help="Local output directory (default: test-results)")
    args = ap.parse_args()

    outs = terraform_outputs(TF_DIR)
    bucket = outs["artifacts_bucket_name"]
    s3 = boto3.client("s3")

    out_dir = Path(args.out_dir) / args.run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    fetched = 0
    for artifact in ARTIFACTS:
        key = f"runs/{args.run_id}/{artifact}"
        dest = out_dir / artifact
        try:
            print(f"Downloading s3://{bucket}/{key} → {dest}")
            s3.download_file(bucket, key, str(dest))
            fetched += 1
        except s3.exceptions.NoSuchKey:
            print(f"  Not found (skipping): {key}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR fetching {key}: {exc}", file=sys.stderr)

    if fetched == 0:
        print(f"No artifacts found for run {args.run_id}.", file=sys.stderr)
        return 1

    print(f"\nFetched {fetched}/{len(ARTIFACTS)} artifact(s) to {out_dir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
