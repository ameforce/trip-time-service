from __future__ import annotations

import os
import subprocess
from contextlib import contextmanager
from pathlib import Path

import trip_time_service.versioning as versioning


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    return completed.stdout.strip()


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _build_repo(repo: Path) -> None:
    _git(repo, "init")
    _git(repo, "config", "user.name", "Trip Time Tests")
    _git(repo, "config", "user.email", "tests@example.com")
    _write(repo / "README.md", "# temp repo\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


@contextmanager
def _versioning_env(*, repo_root: Path | None = None, version: str | None = None):
    previous_repo_root = os.environ.get("TTS_VERSION_REPO_ROOT")
    previous_version = os.environ.get("TTS_VERSION")
    try:
        if repo_root is None:
            os.environ.pop("TTS_VERSION_REPO_ROOT", None)
        else:
            os.environ["TTS_VERSION_REPO_ROOT"] = str(repo_root)
        if version is None:
            os.environ.pop("TTS_VERSION", None)
        else:
            os.environ["TTS_VERSION"] = version
        versioning.resolve_display_version.cache_clear()
        yield
    finally:
        if previous_repo_root is None:
            os.environ.pop("TTS_VERSION_REPO_ROOT", None)
        else:
            os.environ["TTS_VERSION_REPO_ROOT"] = previous_repo_root
        if previous_version is None:
            os.environ.pop("TTS_VERSION", None)
        else:
            os.environ["TTS_VERSION"] = previous_version
        versioning.resolve_display_version.cache_clear()


def test_resolve_display_version_prefers_env_override() -> None:
    with _versioning_env(version="v9.9.9.42"):
        assert versioning.resolve_display_version() == "v9.9.9.42"


def test_resolve_display_version_reads_tagged_repo_commit_count(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "tagged"
    repo.mkdir()
    _build_repo(repo)
    _git(repo, "tag", "v1.2.3")
    _write(repo / "CHANGELOG.md", "follow-up\n")
    _git(repo, "add", "CHANGELOG.md")
    _git(repo, "commit", "-m", "after-tag")

    with _versioning_env(repo_root=repo):
        assert versioning.resolve_display_version() == "v1.2.3.1"


def test_resolve_display_version_falls_back_without_semver_tag(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "fallback"
    repo.mkdir()
    _build_repo(repo)

    with _versioning_env(repo_root=repo):
        assert versioning.resolve_display_version() == "v0.1.0.0"
