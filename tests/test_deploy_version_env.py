from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = REPO_ROOT / "deploy" / "enm" / "deploy.sh"
ENV_EXAMPLES = (
    REPO_ROOT / "deploy" / "enm" / "env" / "dev.env.example",
    REPO_ROOT / "deploy" / "enm" / "env" / "prod.env.example",
)


def test_deploy_script_overrides_runtime_tts_version() -> None:
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert '--env "TTS_VERSION=\\${APP_VERSION}"' in script


def test_deploy_script_overrides_runtime_tts_provider() -> None:
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert 'TTS_PROVIDER="${TTS_PROVIDER:-naver_playwright}"' in script
    assert '--env "TTS_PROVIDER=\\${TTS_PROVIDER}"' in script


def test_env_examples_do_not_pin_tts_version_placeholder() -> None:
    for env_file in ENV_EXAMPLES:
        lines = env_file.read_text(encoding="utf-8").splitlines()
        assert all(not line.startswith("TTS_VERSION=") for line in lines), env_file
