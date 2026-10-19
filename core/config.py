"""Repo paths and YAML config loading."""
import json
import os
import subprocess
from functools import cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"


@cache
def load_yaml(name: str) -> dict:
    return yaml.safe_load((CONFIG / name).read_text())


def load_json(name: str) -> dict:
    return json.loads((CONFIG / name).read_text())


def git_sha() -> str:
    if sha := os.environ.get("GITHUB_SHA"):
        return sha[:7]
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "local"
