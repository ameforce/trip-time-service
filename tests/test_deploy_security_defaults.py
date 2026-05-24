from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_runs_as_non_root_and_does_not_force_chrome_no_sandbox() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    dev_env_example = (ROOT / "deploy/enm/env/dev.env.example").read_text()

    assert "USER appuser" in dockerfile
    assert "useradd --create-home" in dockerfile
    assert "chromium-sandbox" in dockerfile
    assert "TTS_CHROME_NO_SANDBOX" not in dockerfile
    assert "TTS_CHROME_USER_DATA_DIR" not in dockerfile
    assert "TTS_CHROME_NO_SANDBOX=1" in dev_env_example


def test_deploy_scripts_verify_host_keys_by_default() -> None:
    for script_name in ("deploy/enm/deploy.sh", "deploy/enm/rollback.sh"):
        script = (ROOT / script_name).read_text()
        assert (
            'SSH_STRICT_HOST_KEY_CHECKING="${SSH_STRICT_HOST_KEY_CHECKING:-yes}"'
            in script
        )
        assert "UserKnownHostsFile=${SSH_KNOWN_HOSTS_FILE}" in script
        assert 'SSH_STRICT_HOST_KEY_CHECKING:-no' not in script


def test_playwright_web_server_command_is_posix_compatible() -> None:
    config = (ROOT / "playwright.config.ts").read_text()

    assert "cmd /c" not in config
    assert "const e2eProvider = process.env.TTS_PROVIDER ?? 'naver_selenium'" in config
    assert "`TTS_PROVIDER=${shellQuote(e2eProvider)} `" in config


def test_live_e2e_scripts_are_split_by_operational_mode() -> None:
    package_json = (ROOT / "package.json").read_text()

    assert '"e2e:live:smoke"' in package_json
    assert '"e2e:live:diagnose"' in package_json
    assert '"e2e:live:extended"' in package_json
    assert "node tests/e2e/run-live.mjs smoke" in package_json
    assert "node tests/e2e/run-live.mjs diagnose" in package_json
    assert "node tests/e2e/run-live.mjs extended" in package_json


def test_jenkins_live_policy_archives_only_sanitized_summary_by_default() -> None:
    jenkinsfile = (ROOT / "Jenkinsfile").read_text()

    assert "LIVE_E2E_POLICY" in jenkinsfile
    assert 'choices: ["off", "advisory", "blocking"]' in jenkinsfile
    assert "npm run e2e:live:smoke" in jenkinsfile
    assert "npm run e2e:live:diagnose" in jenkinsfile
    assert (
        ".artifacts/live/e2e-runtime.json,.artifacts/live/e2e-live-summary.json"
        in jenkinsfile
    )
    assert ".artifacts/live/test-results/**" not in jenkinsfile
    assert ".artifacts/live/playwright-report/**" not in jenkinsfile


def test_jenkins_live_e2e_stage_exports_uv_path() -> None:
    jenkinsfile = (ROOT / "Jenkinsfile").read_text()

    assert jenkinsfile.count('export PATH="$HOME/.local/bin:$PATH"') >= 3
    assert re.search(
        r'export PATH="\$HOME/\.local/bin:\$PATH"\s+'
        r"export TTS_CHROME_NO_SANDBOX=1\s+"
        r"LIVE_E2E_POLICY=advisory npm run e2e:live:smoke",
        jenkinsfile,
    )
    assert re.search(
        r'export PATH="\$HOME/\.local/bin:\$PATH"\s+'
        r"export TTS_CHROME_NO_SANDBOX=1\s+"
        r"LIVE_E2E_POLICY=blocking npm run e2e:live:smoke",
        jenkinsfile,
    )


def test_jenkins_live_e2e_stage_uses_agent_chrome_sandbox_override() -> None:
    jenkinsfile = (ROOT / "Jenkinsfile").read_text()

    assert jenkinsfile.count("export TTS_CHROME_NO_SANDBOX=1") == 2
    assert (
        "export TTS_CHROME_NO_SANDBOX=1\n"
        "                  LIVE_E2E_POLICY=advisory npm run e2e:live:smoke"
        in jenkinsfile
    )
    assert (
        "export TTS_CHROME_NO_SANDBOX=1\n"
        "                LIVE_E2E_POLICY=blocking npm run e2e:live:smoke"
        in jenkinsfile
    )


def test_live_summary_writer_uses_bucket_counts_not_raw_route_payloads() -> None:
    summary_writer = (ROOT / "tests/e2e/live-summary.mjs").read_text()

    assert "e2e-live-summary.json" in summary_writer
    assert "bucket_counts" in summary_writer
    assert "safeReportReference" in summary_writer
    forbidden_raw_fields = [
        "origin_query",
        "destination_query",
        "clicked_text",
        "selected_value",
        "CommandLine",
    ]
    for field in forbidden_raw_fields:
        assert field not in summary_writer


def test_live_e2e_wrapper_does_not_rewrite_summary_for_list_only() -> None:
    run_live = (ROOT / "tests/e2e/run-live.mjs").read_text()

    assert "const listOnly = passThroughArgs.includes('--list')" in run_live
    assert "`.artifacts/live-list/${mode}`" in run_live
    assert "if (!listOnly)" in run_live


def test_fixture_e2e_uses_separate_artifact_root_from_live_summary() -> None:
    package_json = (ROOT / "package.json").read_text()

    assert "TTS_E2E_ARTIFACTS_DIR=.artifacts/e2e-ci" in package_json
