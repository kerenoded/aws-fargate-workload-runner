"""Build and push the AWFR Docker image to ECR.

Pushes two tags on every build:
  - A mutable primary tag (default: latest, override with --tag or IMAGE_TAG env var)
  - An immutable build-YYYYMMDDHHMMSS tag for reproducibility / rollback

Usage:
  python tools/build_push.py [--tag <tag>]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ is None and __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.tf_outputs import terraform_outputs

REPO_ROOT = Path(__file__).resolve().parents[1]
TF_DIR = REPO_ROOT / "infra" / "terraform"
DOCKERFILE = REPO_ROOT / "docker" / "Dockerfile"


def _run(cmd: list[str], **kwargs: object) -> None:
    print(f"$ {' '.join(cmd)}")
    subprocess.run(cmd, check=True, **kwargs)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--tag",
        default=os.environ.get("IMAGE_TAG", "latest"),
        help="Primary (mutable) image tag (default: IMAGE_TAG env var or 'latest')",
    )
    ap.add_argument("--platform", default="linux/amd64", help="Target platform (default: linux/amd64)")
    args = ap.parse_args()

    outs = terraform_outputs(TF_DIR)
    ecr_url = outs["ecr_repo_url"]
    region = ecr_url.split(".")[3]  # extract region from ECR URL

    # Primary mutable tag (e.g. latest)
    primary_image = f"{ecr_url}:{args.tag}"
    # Immutable build tag for reproducibility — matches the build-* lifecycle rule
    build_tag = "build-" + datetime.now(tz=timezone.utc).strftime("%Y%m%d%H%M%S")
    build_image = f"{ecr_url}:{build_tag}"

    print(f"Primary tag : {primary_image}")
    print(f"Build tag   : {build_image}")

    # Authenticate to ECR
    _run([
        "sh", "-c",
        f"aws ecr get-login-password --region {region} | "
        f"docker login --username AWS --password-stdin {ecr_url.split('/')[0]}",
    ])

    # Build and push both tags in a single buildx invocation
    _run([
        "docker", "buildx", "build",
        "--platform", args.platform,
        "--file", str(DOCKERFILE),
        "--tag", primary_image,
        "--tag", build_image,
        "--push",
        str(REPO_ROOT),
    ])

    print(f"Pushed: {primary_image}")
    print(f"Pushed: {build_image}")
    print(f"\nTo pin a run to this exact build:")
    print(f"  python tools/run_task.py --image-tag {build_tag} ...")


if __name__ == "__main__":
    main()
