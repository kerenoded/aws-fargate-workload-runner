"""Read Terraform outputs from the infra/terraform directory."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


def terraform_outputs(tf_dir: Path) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["terraform", "output", "-json"],
            cwd=tf_dir,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        hint = "\nHint: run 'terraform init' and 'terraform apply' in infra/terraform first."
        raise SystemExit(f"Failed to read Terraform outputs from {tf_dir}.\n{stderr}{hint}") from exc

    raw = json.loads(result.stdout)
    return {k: v["value"] for k, v in raw.items()}
