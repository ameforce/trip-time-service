from __future__ import annotations

import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path

from trip_time_service import __version__

_DESCRIBE_PATTERN = re.compile(r"^(v\d+\.\d+\.\d+)-(\d+)-g[0-9a-f]+(?:-dirty)?$")
_SEMVER_TAG_PATTERN = re.compile(r"^v\d+\.\d+\.\d+$")


def _repo_root() -> Path:
    override = os.getenv("TTS_VERSION_REPO_ROOT")
    if override and override.strip():
        return Path(override).resolve()
    return Path(__file__).resolve().parents[2]


def _normalize_base_tag(version: str) -> str:
    raw = version.strip()
    if raw.startswith("v"):
        raw = raw[1:]

    parts = raw.split(".")
    if len(parts) < 3:
        parts.extend(["0"] * (3 - len(parts)))
    return f"v{parts[0]}.{parts[1]}.{parts[2]}"


def _run_git(*args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=_repo_root(),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if completed.returncode != 0:
        return None

    value = completed.stdout.strip()
    return value or None


@lru_cache(maxsize=1)
def resolve_display_version() -> str:
    override = os.getenv("TTS_VERSION")
    if override and override.strip():
        return override.strip()

    described = _run_git(
        "describe",
        "--tags",
        "--match",
        "v[0-9]*.[0-9]*.[0-9]*",
        "--long",
        "--abbrev=7",
    )
    if described:
        matched = _DESCRIBE_PATTERN.match(described)
        if matched:
            return f"{matched.group(1)}.{int(matched.group(2))}"

    latest_tags = _run_git(
        "tag",
        "--list",
        "v[0-9]*.[0-9]*.[0-9]*",
        "--sort=-v:refname",
    )
    if latest_tags:
        base_tag = latest_tags.splitlines()[0].strip()
        if _SEMVER_TAG_PATTERN.fullmatch(base_tag):
            commit_count = _run_git("rev-list", f"{base_tag}..HEAD", "--count")
            if commit_count and commit_count.isdigit():
                return f"{base_tag}.{int(commit_count)}"

    return f"{_normalize_base_tag(__version__)}.0"


__all__ = ["resolve_display_version"]
