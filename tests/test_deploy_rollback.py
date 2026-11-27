from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHA = "1" * 40
OLD_ID = "a" * 64
OLD_IMAGE_ID = "sha256:" + "b" * 64
OLD_VERSION = "v0.4.2.1-hotfix-v0.4.3-39c8c736"
CANDIDATE_ID = "d" * 64
RESTORED_ID = "e" * 64
CANDIDATE = f"{CANDIDATE_ID}|triptime:new|job-43|triptime:old|sha256:new|{OLD_IMAGE_ID}"
PREVIOUS = f"{OLD_ID}|triptime:old|||{OLD_IMAGE_ID}"
RESTORE_BASE = f"{RESTORED_ID}|{OLD_IMAGE_ID}|||{OLD_IMAGE_ID}||"
RESTORED_STOPPED = RESTORE_BASE + "job-43|triptime:new|triptime:old|false"
RESTORED_RUNNING = RESTORE_BASE + "job-43|triptime:new|triptime:old|true"
MANUALLY_RESTORED = RESTORE_BASE + "||triptime:old|true"
REPLACEMENT = "new-id|triptime:new|job-44|triptime:old|sha256:new"
MOCK_DOCKER = r"""#!/usr/bin/env bash
echo "$*" >> "$MOCK_LOG"
IFS='|' read -r id image attempt previous image_id previous_image_id \
  restore_attempt restore_candidate restored_ref running < "$MOCK_STATE"
running="${running:-true}"
case "$1 $2" in
  "container inspect")
    [[ -n "$id" && ("$5" == "$id" || "$5" == triptime-dev) ]] || exit 1
    case "$4" in
      *println*)
        printf '%s\n' "$id" "$image" "$attempt" "$previous" "$previous_image_id"
        printf '%s\n' "$image_id" "$restore_attempt" "$restore_candidate"
        printf '%s\n' "$restored_ref" "$running"
        ;;
      '{{.Image}}') printf '%s\n' "$image_id" ;;
      '{{.Id}}|{{.State.Running}}') printf '%s|%s\n' "$id" "$running" ;;
      '{{.Id}}|{{.Image}}|{{.State.Running}}')
        printf '%s|%s|%s\n' "$id" "$image_id" "$running"
        ;;
      *)
        printf '%s|' "$id" "$image" "$image_id" "$restored_ref" "$previous"
        printf '%s\n' "$previous_image_id"
        ;;
    esac
    ;;
  "image inspect")
    if [[ "$MOCK_MODE" == "changed-while-restoring" ]]; then
      printf '%s\n' "$MOCK_REPLACEMENT" > "$MOCK_STATE"
    fi
    if [[ "$MOCK_MODE" == "registry" && "$3" == "triptime:new" ]]; then exit 1; fi
    if [[ "$3" == "$MOCK_OLD_IMAGE_ID" || "$5" == "$MOCK_OLD_IMAGE_ID" ]]; then
      if [[ "$MOCK_MODE" == "image-unavailable" ]]; then exit 1; fi
      if [[ "$MOCK_MODE" == "replacement-during-empty-recovery" ]]; then
        printf '%s\n' "$MOCK_REPLACEMENT" > "$MOCK_STATE"
      fi
    fi
    case "$4" in
      '{{.Id}}')
        if [[ "$5" == "triptime:new" ]]; then
          printf 'sha256:new\n'
        elif [[ "$MOCK_MODE" == *previous-tag-moved ]]; then
          printf '%s\n' "$MOCK_MOVED_IMAGE_ID"
        else
          printf '%s\n' "$MOCK_OLD_IMAGE_ID"
        fi
        ;;
      *revision*) printf 'sha256:new|%s\n' "$MOCK_REVISION" ;;
      *TTS_VERSION=*)
        if [[ "$5" == "$MOCK_OLD_IMAGE_ID" ]]; then
          printf '%s' "$MOCK_OLD_VERSION_ENV"
        else
          printf 'TTS_VERSION=v0.9.9.0\n'
        fi
        ;;
    esac
    ;;
  "rm -f")
    [[ "$3" == "$id" ]] || exit 1
    : > "$MOCK_STATE"
    if [[ "$MOCK_MODE" == "interrupted-after-removal" ]]; then kill -TERM "$PPID"; fi
    ;;
  "container ls")
    [[ "$MOCK_MODE" != "daemon-uncertain" ]]
    ;;
  "create --name")
    [[ -z "$id" ]] || exit 1
    if [[ "${@: -1}" == "triptime:new" ]]; then
      if [[ "$MOCK_MODE" == "changed-while-creating" ]]; then
        printf '%s\n' "$MOCK_CHANGED_IMAGE" > "$MOCK_STATE"
      else
        printf '%s\n' "$MOCK_CANDIDATE" > "$MOCK_STATE"
      fi
      echo "$MOCK_CANDIDATE_ID"
    else
      [[ "${@: -1}" == "$MOCK_OLD_IMAGE_ID" ]] || exit 1
      [[ "$MOCK_MODE" != "restore-create-fails" ]] || exit 1
      for arg in "$@"; do
        case "$arg" in
          triptime_restore_attempt=*) restore_attempt="${arg#*=}" ;;
          triptime_restore_candidate=*) restore_candidate="${arg#*=}" ;;
          triptime_restored_image_ref=*) restored_ref="${arg#*=}" ;;
        esac
      done
      printf '%s|%s|||%s||%s|%s|%s|false\n' "$MOCK_RESTORED_ID" "$MOCK_OLD_IMAGE_ID" \
        "$MOCK_OLD_IMAGE_ID" "$restore_attempt" "$restore_candidate" "$restored_ref" \
        > "$MOCK_STATE"
      [[ "$MOCK_MODE" != *restore-create-response-lost ]] || exit 1
      echo "$MOCK_RESTORED_ID"
    fi
    ;;
  "start $MOCK_CANDIDATE_ID")
    [[ "$MOCK_MODE" != start-fails* ]]
    ;;
  "start $MOCK_RESTORED_ID"|"restart $MOCK_RESTORED_ID")
    [[ "$2" == "$id" && "$MOCK_MODE" != *restore-start-fails ]] || exit 1
    printf '%s|%s|||%s||%s|%s|%s|true\n' "$id" "$image" "$image_id" \
      "$restore_attempt" "$restore_candidate" "$restored_ref" > "$MOCK_STATE"
    ;;
esac
"""
MOCK_CURL = r"""#!/usr/bin/env bash
state="$(cat "$MOCK_STATE")"
[[ -n "$state" && "$state" != *'|false' ]] || exit 1
if [[ "$state" == "$MOCK_RESTORED_ID|"* ]]; then
  [[ "$MOCK_MODE" != *restore-unhealthy ]] || exit 1
  if [[ "$MOCK_MODE" == "restore-needs-restart" ]]; then
    grep -Fxq "restart $MOCK_RESTORED_ID" "$MOCK_LOG" || exit 1
  fi
fi
"""


@pytest.fixture
def deploy_mock(tmp_path):
    if os.name == "nt":
        program_files = Path(os.environ.get("ProgramFiles", "C:/Program Files"))
        bash = program_files / "Git/bin/bash.exe"
        if not bash.is_file():
            pytest.skip("Git Bash is required for this shell fixture")
    elif not (bash := shutil.which("bash")):
        pytest.skip("Bash is required for this shell fixture")

    state = tmp_path / "state"
    log = tmp_path / "actions"
    env_file = tmp_path / "dev.env"
    env_file.write_text("TTS_VERSION=v0.0.0.0\n", encoding="utf-8")
    markers = tmp_path / "rollback/dev"
    markers.mkdir(parents=True)
    (markers / "previous-image.txt").write_text("triptime:stale", encoding="utf-8")
    for command, source in {
        "ssh": "#!/usr/bin/env bash\nexec bash\n",
        "curl": MOCK_CURL,
        "docker": MOCK_DOCKER,
    }.items():
        stub = tmp_path / command
        stub.write_text(source, encoding="utf-8", newline="\n")
        stub.chmod(0o755)
    if os.name == "nt" or not shutil.which("flock"):
        flock = tmp_path / "flock"
        flock.write_text(
            '#!/usr/bin/env bash\n[[ "$*" == "-w 120 9" ]]\n',
            encoding="utf-8",
            newline="\n",
        )
        flock.chmod(0o755)

    def run(
        script,
        initial,
        *,
        mode="",
        revision=SHA,
        expected_revision=SHA,
        old_version_env=f"TTS_VERSION={OLD_VERSION}\n",
        expected_attempt="job-43",
        expected_image="triptime:new",
        rollback_ref="",
        deploy_attempt="job-43",
    ):
        state.write_text(initial + "\n", encoding="utf-8", newline="\n")
        log.write_text("", encoding="utf-8")
        env = {
            **os.environ,
            "MOCK_TOOLS": tmp_path.as_posix(),
            "MOCK_STATE": state.as_posix(),
            "MOCK_LOG": log.as_posix(),
            "MOCK_MODE": mode,
            "MOCK_REVISION": revision,
            "MOCK_CANDIDATE": CANDIDATE.replace("|job-43|", f"|{deploy_attempt}|"),
            "MOCK_CANDIDATE_ID": CANDIDATE_ID,
            "MOCK_CHANGED_IMAGE": CANDIDATE.replace("sha256:new", "sha256:other"),
            "MOCK_RESTORED_ID": RESTORED_ID,
            "MOCK_OLD_IMAGE_ID": OLD_IMAGE_ID,
            "MOCK_MOVED_IMAGE_ID": "sha256:" + "c" * 64,
            "MOCK_OLD_VERSION_ENV": old_version_env,
            "MOCK_REPLACEMENT": REPLACEMENT,
            "DEPLOY_ENV": "dev",
            "ENM_HOST": "mock",
            "ENM_PORT": "22",
            "ENM_USER": "mock",
            "ENM_SSH_KEY": "in-memory-test",
            "ENV_FILE_DEV": env_file.as_posix(),
            "REMOTE_APP_ROOT": tmp_path.as_posix(),
            "HEALTHCHECK_RETRIES": "1",
            "HEALTHCHECK_DELAY_SECONDS": "0",
            "IMAGE_REF": "triptime:new",
            "APP_REVISION": expected_revision,
            "APP_VERSION": "v0.4.3.0",
            "DEPLOY_ATTEMPT_ID": deploy_attempt,
            "ROLLBACK_EXPECTED_IMAGE_REF": expected_image,
            "ROLLBACK_EXPECTED_ATTEMPT_ID": expected_attempt,
            "ROLLBACK_IMAGE_REF": rollback_ref,
        }
        bootstrap = """
mock_bin="$MOCK_TOOLS"
if command -v cygpath >/dev/null; then mock_bin="$(cygpath -u "$mock_bin")"; fi
export PATH="$mock_bin:$PATH"
for tool in ssh curl docker; do
  test "$(command -v "$tool")" = "$mock_bin/$tool" || exit 99
done
exec bash "$1"
"""
        result = subprocess.run(
            [bash, "-c", bootstrap, "deploy-test", f"deploy/enm/{script}.sh"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            encoding="utf-8",
            check=False,
            timeout=20,
        )
        return result, state.read_text().strip(), log.read_text().splitlines()

    return run


@pytest.mark.parametrize(
    ("initial", "mode", "status", "active", "removal"),
    [
        (CANDIDATE, "", 0, RESTORED_RUNNING, f"rm -f {CANDIDATE_ID}"),
        ("", "", 3, "", None),
        (PREVIOUS, "", 3, PREVIOUS, None),
        (RESTORED_RUNNING, "", 3, RESTORED_RUNNING, None),
        (REPLACEMENT, "", 3, REPLACEMENT, None),
        (
            CANDIDATE,
            "changed-while-restoring",
            1,
            REPLACEMENT,
            f"rm -f {CANDIDATE_ID}",
        ),
    ],
)
def test_rollback_binds_the_attempt_and_container_id(
    deploy_mock, initial, mode, status, active, removal
) -> None:
    result, state, actions = deploy_mock("rollback", initial, mode=mode)
    assert result.returncode == status, result
    assert state == active
    assert [action for action in actions if action.startswith("rm -f")] == (
        [removal] if removal else []
    )


@pytest.mark.parametrize(
    ("initial", "revision", "mode", "reason"),
    [
        (PREVIOUS, "", "", "image revision does not match"),
        (PREVIOUS, "2" * 40, "", "image revision does not match"),
        (PREVIOUS, "2" * 40, "registry", "image revision does not match"),
        (
            CANDIDATE.replace("sha256:new", "sha256:original"),
            SHA,
            "",
            "different bytes",
        ),
    ],
)
def test_deploy_rejects_unverified_image_before_switch(
    deploy_mock, initial, revision, mode, reason
) -> None:
    result, state, actions = deploy_mock(
        "deploy", initial, revision=revision, mode=mode
    )
    assert result.returncode == 1 and reason in result.stderr, result
    assert state == initial
    assert not any(
        action.startswith(("rm -f", "start ", "create ")) for action in actions
    )
    assert ("pull triptime:new" in actions) is (mode == "registry")


def test_deploy_never_starts_a_container_from_a_tag_changed_after_verification(
    deploy_mock,
) -> None:
    result, state, actions = deploy_mock(
        "deploy", PREVIOUS, mode="changed-while-creating"
    )
    assert result.returncode == 1
    assert "created from a different image ID" in result.stderr
    assert state == RESTORED_RUNNING
    assert f"start {CANDIDATE_ID}" not in actions
    assert f"rm -f {CANDIDATE_ID}" in actions


def test_deploy_auto_rollback_uses_failed_container_id_without_new_config(
    deploy_mock,
) -> None:
    result, state, actions = deploy_mock(
        "deploy", PREVIOUS, mode="start-fails-previous-tag-moved"
    )
    assert result.returncode == 1 and "rollback health check passed" in result.stdout
    assert state == RESTORED_RUNNING
    assert f"rm -f {CANDIDATE_ID}" in actions
    restores = [action for action in actions if action.startswith("create ")]
    assert len(restores) == 2
    assert "--label triptime_deploy_attempt=job-43" in restores[0]
    assert "--label triptime_previous_image=triptime:old" in restores[0]
    assert f"--label triptime_previous_image_id={OLD_IMAGE_ID}" in restores[0]
    assert restores[1].endswith(OLD_IMAGE_ID)
    assert restores[1].index("--env-file ") < restores[1].index(
        f"--env TTS_VERSION={OLD_VERSION}"
    )
    assert "TTS_VERSION=v0.4.3.0" not in restores[1]
    assert "TTS_PROVIDER" not in restores[1]


def test_active_candidate_rollback_uses_prior_id_and_overrides_placeholder(
    deploy_mock,
) -> None:
    result, state, actions = deploy_mock(
        "rollback", CANDIDATE, mode="previous-tag-moved"
    )
    assert result.returncode == 0 and state == RESTORED_RUNNING, result
    assert f"rm -f {CANDIDATE_ID}" in actions
    assert not any(action.endswith(" triptime:old") for action in actions)
    restore = next(action for action in actions if action.startswith("create "))
    assert restore.endswith(OLD_IMAGE_ID)
    assert restore.index("--env-file ") < restore.index(
        f"--env TTS_VERSION={OLD_VERSION}"
    )
    assert "TTS_PROVIDER" not in restore
    assert OLD_VERSION not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "old_version_env",
    [
        "",
        "TTS_VERSION=v0.0.0.0\n",
        "TTS_VERSION=arbitrary\n",
        "TTS_VERSION=v1.2.3\nTTS_VERSION=v2.3.4\n",
    ],
)
def test_rollback_keeps_legacy_defaults_when_image_version_is_invalid(
    deploy_mock, old_version_env
) -> None:
    result, state, actions = deploy_mock(
        "rollback", CANDIDATE, old_version_env=old_version_env
    )
    assert result.returncode == 0 and state == RESTORED_RUNNING, result
    restore = next(action for action in actions if action.startswith("create "))
    assert "TTS_VERSION" not in restore
    assert "TTS_PROVIDER" not in restore


def test_manual_rollback_accepts_an_explicit_legacy_image_ref(deploy_mock) -> None:
    result, state, actions = deploy_mock(
        "rollback",
        CANDIDATE,
        expected_attempt="",
        expected_image="",
        rollback_ref="triptime:old",
    )
    assert result.returncode == 0 and state == MANUALLY_RESTORED, result
    assert "image inspect --format {{.Id}} triptime:old" in actions
    assert any(
        action.startswith("create ") and action.endswith(OLD_IMAGE_ID)
        for action in actions
    )


def test_explicit_image_only_deploy_without_expected_revision_remains_supported(
    deploy_mock,
) -> None:
    result, state, _ = deploy_mock(
        "deploy", PREVIOUS, expected_revision="", revision=""
    )
    assert result.returncode == 0, result
    assert state == CANDIDATE


HANDOFF = (
    "\n".join(("job-43", "triptime:new", OLD_ID, "triptime:old", OLD_IMAGE_ID)) + "\n"
)


@pytest.mark.parametrize(
    "mode", ["interrupted-after-removal", "restore-create-fails"]
)
def test_rollback_retry_restores_the_previous_image_after_candidate_removal(
    deploy_mock, tmp_path, mode
) -> None:
    handoff = tmp_path / "rollback/dev/deploy-handoff.txt"
    result, state, actions = deploy_mock("rollback", CANDIDATE, mode=mode)

    assert result.returncode != 0 and state == "", result
    assert f"rm -f {CANDIDATE_ID}" in actions
    assert handoff.read_text(encoding="utf-8") == HANDOFF.replace(OLD_ID, CANDIDATE_ID)

    result, state, actions = deploy_mock("rollback", state, mode="previous-tag-moved")

    assert result.returncode == 0 and state == RESTORED_RUNNING, result
    assert not handoff.exists()
    assert not any(action.startswith("rm -f ") for action in actions)
    restore = next(action for action in actions if action.startswith("create "))
    assert restore.endswith(OLD_IMAGE_ID)
    assert f"--env TTS_VERSION={OLD_VERSION}" in restore


@pytest.mark.parametrize("entrypoint", ["rollback", "deploy"])
@pytest.mark.parametrize(
    ("failure", "partial_state", "retry_mode", "retry_command"),
    [
        ("restore-create-response-lost", RESTORED_STOPPED, "", "start"),
        ("restore-start-fails", RESTORED_STOPPED, "", "start"),
        ("restore-unhealthy", RESTORED_RUNNING, "restore-needs-restart", "restart"),
    ],
)
def test_same_attempt_resumes_a_created_or_running_restore_by_id(
    deploy_mock, tmp_path, entrypoint, failure, partial_state, retry_mode, retry_command
) -> None:
    handoff = tmp_path / "rollback/dev/deploy-handoff.txt"
    initial, mode = (
        (PREVIOUS, f"start-fails-{failure}")
        if entrypoint == "deploy"
        else (CANDIDATE, failure)
    )
    result, state, actions = deploy_mock(entrypoint, initial, mode=mode)

    assert result.returncode == 1 and state == partial_state, result
    assert handoff.read_text(encoding="utf-8") == HANDOFF.replace(OLD_ID, CANDIDATE_ID)
    creation = next(
        action
        for action in actions
        if action.startswith("create ") and action.endswith(OLD_IMAGE_ID)
    )
    assert "--label triptime_restore_attempt=job-43" in creation
    assert "--label triptime_restore_candidate=triptime:new" in creation
    assert f"--env TTS_VERSION={OLD_VERSION}" in creation

    result, state, retry_actions = deploy_mock("rollback", state, mode=retry_mode)

    assert result.returncode == 0 and state == RESTORED_RUNNING, result
    assert f"{retry_command} {RESTORED_ID}" in retry_actions
    assert not any(action.startswith(("rm -f ", "create ")) for action in retry_actions)
    assert not handoff.exists()


def test_new_deploy_waits_for_the_previous_attempt_to_finish_recovery(
    deploy_mock, tmp_path
) -> None:
    handoff = tmp_path / "rollback/dev/deploy-handoff.txt"
    handoff.write_text(HANDOFF, encoding="utf-8", newline="\n")

    result, state, actions = deploy_mock(
        "deploy", RESTORED_RUNNING, deploy_attempt="job-44"
    )
    assert result.returncode == 1 and state == RESTORED_RUNNING, result
    assert "outstanding recovery record" in result.stderr
    assert not any(action.startswith(("rm -f ", "create ")) for action in actions)

    result, state, actions = deploy_mock("rollback", state)
    assert result.returncode == 0 and state == RESTORED_RUNNING, result
    assert not handoff.exists()
    assert not any(
        action.startswith(("rm -f ", "create ", "start ", "restart "))
        for action in actions
    )

    result, state, actions = deploy_mock("deploy", state, deploy_attempt="job-44")
    assert result.returncode == 0, result
    assert state == CANDIDATE.replace("job-43", "job-44")
    assert f"rm -f {RESTORED_ID}" in actions
    assert not handoff.exists()


def test_restore_attempt_label_does_not_authorize_a_different_image(
    deploy_mock, tmp_path
) -> None:
    handoff = tmp_path / "rollback/dev/deploy-handoff.txt"
    handoff.write_text(HANDOFF, encoding="utf-8", newline="\n")
    foreign = RESTORED_RUNNING.replace(OLD_IMAGE_ID, "sha256:" + "c" * 64)

    result, state, actions = deploy_mock("rollback", foreign)

    assert result.returncode == 1 and state == foreign, result
    assert "does not match the recovery record" in result.stderr
    assert handoff.read_text(encoding="utf-8") == HANDOFF
    assert not any(
        action.startswith(("rm -f ", "create ", "start ", "restart "))
        for action in actions
    )


def test_interrupted_deploy_recovers_exact_previous_image_with_no_candidate(
    deploy_mock, tmp_path
) -> None:
    handoff = tmp_path / "rollback/dev/deploy-handoff.txt"
    result, state, actions = deploy_mock(
        "deploy", PREVIOUS, mode="interrupted-after-removal"
    )
    assert result.returncode != 0 and state == "", result
    assert f"rm -f {OLD_ID}" in actions
    assert not any(action.startswith("create ") for action in actions)
    assert handoff.read_text(encoding="utf-8") == HANDOFF

    result, state, actions = deploy_mock("rollback", state)

    assert result.returncode == 0 and state == RESTORED_RUNNING, result
    assert not handoff.exists()
    assert not any(action.startswith("rm -f ") for action in actions)
    run = next(action for action in actions if action.startswith("create "))
    assert run.endswith(OLD_IMAGE_ID)
    assert "--label triptime_restored_image_ref=triptime:old" in run
    assert "TTS_PROVIDER" not in run
    assert f"--env TTS_VERSION={OLD_VERSION}" in run


@pytest.mark.parametrize(
    "record",
    [
        None,
        HANDOFF.replace("job-43", "job-44"),
        HANDOFF.replace("triptime:new", "triptime:other"),
        HANDOFF.replace(OLD_IMAGE_ID, "sha256:incomplete"),
        HANDOFF + "extra-line\n",
    ],
)
def test_empty_recovery_requires_a_matching_complete_handoff(
    deploy_mock, tmp_path, record
) -> None:
    handoff = tmp_path / "rollback/dev/deploy-handoff.txt"
    if record is not None:
        handoff.write_text(record, encoding="utf-8", newline="\n")

    result, state, actions = deploy_mock("rollback", "")

    assert result.returncode == 3 and state == "", result
    assert not any(action.startswith(("rm -f ", "create ")) for action in actions)
    assert handoff.exists() is (record is not None)


@pytest.mark.parametrize(
    ("initial", "mode", "status", "active", "retired"),
    [
        (PREVIOUS, "", 3, PREVIOUS, True),
        (REPLACEMENT, "", 3, REPLACEMENT, False),
        ("", "replacement-during-empty-recovery", 1, REPLACEMENT, False),
        ("", "image-unavailable", 1, "", False),
        ("", "daemon-uncertain", 1, "", False),
    ],
)
def test_recovery_leaves_replacements_and_uncertain_state_untouched(
    deploy_mock, tmp_path, initial, mode, status, active, retired
) -> None:
    handoff = tmp_path / "rollback/dev/deploy-handoff.txt"
    handoff.write_text(HANDOFF, encoding="utf-8", newline="\n")

    result, state, actions = deploy_mock("rollback", initial, mode=mode)

    assert result.returncode == status and state == active, result
    assert not any(action.startswith("rm -f ") for action in actions)
    assert (not handoff.exists()) is retired


def test_another_deploy_keeps_the_owner_handoff_available_for_recovery(
    deploy_mock, tmp_path
) -> None:
    record = HANDOFF.replace("job-43", "job-44")
    handoff = tmp_path / "rollback/dev/deploy-handoff.txt"
    handoff.write_text(record, encoding="utf-8", newline="\n")

    result, state, actions = deploy_mock("deploy", "")

    assert result.returncode == 1 and state == "", result
    assert "outstanding recovery record" in result.stderr
    assert handoff.read_text(encoding="utf-8") == record
    assert not any(action.startswith(("rm -f ", "create ")) for action in actions)


def test_deploy_does_not_overwrite_an_invalid_pending_handoff(
    deploy_mock, tmp_path
) -> None:
    handoff = tmp_path / "rollback/dev/deploy-handoff.txt"
    handoff.mkdir()

    result, state, actions = deploy_mock("deploy", PREVIOUS)

    assert result.returncode == 1 and state == PREVIOUS, result
    assert "outstanding recovery record" in result.stderr
    assert not any(action.startswith(("rm -f ", "create ")) for action in actions)
    assert not list(handoff.parent.glob(".deploy-handoff.*"))
