from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read_repo_text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _deploy_success_block(script: str) -> str:
    return script.split("if check_health; then", 1)[1].split("exit 0", 1)[0]


def test_dockerfile_runs_as_non_root_with_playwright_chromium() -> None:
    dockerfile = _read_repo_text("Dockerfile")
    dev_env_example = _read_repo_text("deploy/enm/env/dev.env.example")
    prod_env_example = _read_repo_text("deploy/enm/env/prod.env.example")

    assert "FROM python:3.14-slim" in dockerfile
    assert "USER appuser" in dockerfile
    assert "useradd --create-home" in dockerfile
    assert "playwright install --with-deps chromium" in dockerfile
    assert "apt chromium" not in dockerfile.replace("\n", " ")
    assert "chromium-sandbox" not in dockerfile
    assert "TTS_CHROME_BINARY_PATH" not in dockerfile
    assert "TTS_CHROME_NO_SANDBOX" not in dockerfile
    assert "TTS_CHROME_USER_DATA_DIR" not in dockerfile
    assert "TTS_PROVIDER=naver_playwright" in dev_env_example
    assert "TTS_PROVIDER=naver_playwright" in prod_env_example
    assert "TTS_CHROME_NO_SANDBOX" not in dev_env_example
    assert "TTS_CHROME_NO_SANDBOX" not in prod_env_example
    assert "TTS_CHROME_BINARY_PATH" not in dev_env_example
    assert "TTS_CHROME_BINARY_PATH" not in prod_env_example


def test_enm_deploy_scripts_pass_chrome_no_sandbox_to_runtime() -> None:
    for script_name in ("deploy/enm/deploy.sh", "deploy/enm/rollback.sh"):
        script = _read_repo_text(script_name)

        assert 'TTS_CHROME_NO_SANDBOX="${TTS_CHROME_NO_SANDBOX:-1}"' in script
        assert (
            'TTS_CHROME_NO_SANDBOX="$(printf \'%q\' "${TTS_CHROME_NO_SANDBOX}")"'
            in script
        )
        assert '--env "TTS_CHROME_NO_SANDBOX=\\${TTS_CHROME_NO_SANDBOX}"' in script


def test_enm_deploy_scripts_override_tts_provider_to_playwright() -> None:
    for script_name in ("deploy/enm/deploy.sh", "deploy/enm/rollback.sh"):
        script = _read_repo_text(script_name)

        assert 'TTS_PROVIDER="${TTS_PROVIDER:-naver_playwright}"' in script
        assert 'TTS_PROVIDER="$(printf \'%q\' "${TTS_PROVIDER}")"' in script
        assert '--env "TTS_PROVIDER=\\${TTS_PROVIDER}"' in script


def test_deploy_scripts_verify_host_keys_by_default() -> None:
    for script_name in ("deploy/enm/deploy.sh", "deploy/enm/rollback.sh"):
        script = _read_repo_text(script_name)
        assert (
            'SSH_STRICT_HOST_KEY_CHECKING="${SSH_STRICT_HOST_KEY_CHECKING:-yes}"'
            in script
        )
        assert "UserKnownHostsFile=${SSH_KNOWN_HOSTS_FILE}" in script
        assert 'SSH_STRICT_HOST_KEY_CHECKING:-no' not in script


def test_playwright_web_server_command_is_posix_compatible() -> None:
    config = _read_repo_text("playwright.config.ts")

    assert "cmd /c" not in config
    assert "powershell -NoProfile" not in config
    assert (
        "const e2eProvider = process.env.TTS_PROVIDER ?? 'naver_playwright'"
        in config
    )
    assert "TTS_PROVIDER: e2eProvider" in config
    assert "command: 'uv run trip-time-service'" in config
    assert "env: serverEnv" in config


def test_live_e2e_scripts_are_split_by_operational_mode() -> None:
    package_json = _read_repo_text("package.json")

    assert '"e2e:live:smoke"' in package_json
    assert '"e2e:live:diagnose"' in package_json
    assert '"e2e:live:extended"' in package_json
    assert "node tests/e2e/run-live.mjs smoke" in package_json
    assert "node tests/e2e/run-live.mjs diagnose" in package_json
    assert "node tests/e2e/run-live.mjs extended" in package_json


def test_jenkins_live_policy_archives_only_sanitized_summary_by_default() -> None:
    jenkinsfile = _read_repo_text("Jenkinsfile")

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


def test_jenkins_dev_deploy_has_nonempty_enm_defaults_and_known_hosts() -> None:
    jenkinsfile = _read_repo_text("Jenkinsfile")

    assert 'string(name: "ENM_HOST", defaultValue: "enmsoftware.com"' in jenkinsfile
    assert 'defaultValue: "enm-server-ssh-key"' in jenkinsfile
    assert "env.EFFECTIVE_ENM_HOST = params.ENM_HOST?.trim()" in jenkinsfile
    assert (
        'ssh-keyscan -p "$EFFECTIVE_ENM_PORT" "$EFFECTIVE_ENM_HOST"'
        in jenkinsfile
    )
    assert '"SSH_STRICT_HOST_KEY_CHECKING=yes"' in jenkinsfile
    assert '"SSH_KNOWN_HOSTS_FILE=${env.DEPLOY_KNOWN_HOSTS}"' in jenkinsfile
    assert '"ENM_HOST=${env.EFFECTIVE_ENM_HOST}"' in jenkinsfile
    assert '"ENM_HOST=${params.ENM_HOST}"' not in jenkinsfile


def test_jenkins_dev_runtime_version_uses_deploy_image_tag() -> None:
    jenkinsfile = _read_repo_text("Jenkinsfile")
    deploy_stage = jenkinsfile.split('stage("Deploy To ENM")', 1)[1].split(
        'stage("Smoke Verify")', 1
    )[0]

    assert "env.DEPLOY_APP_VERSION = env.IMAGE_TAG" in jenkinsfile
    assert '"APP_VERSION=${env.DEPLOY_APP_VERSION}"' in deploy_stage
    assert '"APP_VERSION=${env.APP_VERSION}"' not in deploy_stage


def test_jenkins_passes_enm_image_retention_count_to_deploy() -> None:
    # Given: Jenkins owns the operator-facing retention knob.
    jenkinsfile = _read_repo_text("Jenkinsfile")
    deploy_stage = jenkinsfile.split('stage("Deploy To ENM")', 1)[1].split(
        'stage("Smoke Verify")', 1
    )[0]

    # Then: deploy.sh receives the same bounded policy as an environment value.
    assert 'string(name: "IMAGE_RETENTION_COUNT", defaultValue: "5"' in jenkinsfile
    assert '"IMAGE_RETENTION_COUNT=${params.IMAGE_RETENTION_COUNT}"' in deploy_stage


def test_enm_deploy_image_retention_runs_only_after_successful_health_check() -> None:
    # Given: deploy.sh has separate success and failure paths.
    script = _read_repo_text("deploy/enm/deploy.sh")
    success_block = _deploy_success_block(script)
    failure_block = script.split(
        'echo "[deploy] health check failed for image=\\${IMAGE_REF}"', 1
    )[1]

    # Then: cleanup runs after current-image.txt is updated, never on failed deploys.
    current_marker_write = (
        'printf \'%s\\n\' "\\${IMAGE_REF}" '
        '> "\\${ROLLBACK_DIR}/current-image.txt"'
    )
    assert current_marker_write in success_block
    assert 'cleanup_deploy_images "\\${IMAGE_REF}"' in success_block
    assert "cleanup_deploy_build_dir" in success_block
    assert "cleanup_deploy_images" not in failure_block
    assert "cleanup_deploy_build_dir" not in failure_block


def test_enm_deploy_image_retention_policy_is_bounded_and_explicit() -> None:
    # Given: only same-repository deployed image tags may be cleanup candidates.
    script = _read_repo_text("deploy/enm/deploy.sh")

    # Then: the policy preserves rollback and live images and avoids broad prune APIs.
    assert 'IMAGE_RETENTION_COUNT="${IMAGE_RETENTION_COUNT:-5}"' in script
    retention_count_export = (
        'IMAGE_RETENTION_COUNT="$(printf \'%q\' "${IMAGE_RETENTION_COUNT}")"'
    )
    assert retention_count_export in script
    assert "image_repository_for_ref()" in script
    assert "collect_protected_images()" in script
    assert 'docker ps --format \'{{.Image}}\'' in script
    assert "previous-image.txt" in script
    assert "current-image.txt" in script
    assert 'docker image ls --format \'{{.Repository}}:{{.Tag}}\'' in script
    assert 'docker image rm "\\${candidate_image}"' in script
    assert "docker image prune" not in script
    assert "docker system prune" not in script


def test_enm_runbook_documents_post_success_image_retention_policy() -> None:
    # Given: operators need to know what the deploy cleanup can and cannot delete.
    runbook = _read_repo_text("docs/deploy/enm-triptime-runbook.md")

    # Then: the runbook documents the bounded post-success retention policy.
    assert "배포 성공 후" in runbook
    assert "IMAGE_RETENTION_COUNT" in runbook
    assert "`current-image.txt`" in runbook
    assert "`previous-image.txt`" in runbook
    assert "실행 중인 컨테이너" in runbook
    assert "`docker image prune`" in runbook


def test_jenkins_live_e2e_stage_exports_uv_path() -> None:
    jenkinsfile = _read_repo_text("Jenkinsfile")

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
    jenkinsfile = _read_repo_text("Jenkinsfile")

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
    summary_writer = _read_repo_text("tests/e2e/live-summary.mjs")

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
    run_live = _read_repo_text("tests/e2e/run-live.mjs")

    assert "const listOnly = passThroughArgs.includes('--list')" in run_live
    assert "`.artifacts/live-list/${mode}`" in run_live
    assert "if (!listOnly)" in run_live


def test_fixture_e2e_uses_separate_artifact_root_from_live_summary() -> None:
    package_json = _read_repo_text("package.json")

    assert "TTS_E2E_ARTIFACTS_DIR=.artifacts/e2e-ci" in package_json
