from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHA = "1" * 40
CANDIDATE = "candidate-id|triptime:new|job-43|triptime:old|sha256:new"
PREVIOUS = "old-id|triptime:old|||sha256:old"
REPLACEMENT = "new-id|triptime:new|job-44|triptime:old|sha256:new"
MOCK_DOCKER = r"""#!/usr/bin/env bash
echo "$*" >> "$MOCK_LOG"
IFS='|' read -r id image attempt previous image_id < "$MOCK_STATE"
case "$1 $2" in
  "container inspect")
    [[ -n "$id" ]] || exit 1
    case "$4" in
      *println*) printf '%s\n' "$id" "$image" "$attempt" "$previous" ;;
      '{{.Image}}') printf '%s\n' "$image_id" ;;
      *) printf '%s %s %s\n' "$id" "$image" "$image_id" ;;
    esac
    ;;
  "image inspect")
    if [[ "$MOCK_MODE" == "changed-while-restoring" ]]; then
      printf '%s\n' "$MOCK_REPLACEMENT" > "$MOCK_STATE"
    fi
    if [[ "$MOCK_MODE" == "registry" && "$3" == "triptime:new" ]]; then exit 1; fi
    case "$4" in
      '{{.Id}}') printf 'sha256:new\n' ;;
      *revision*) printf 'sha256:new|%s\n' "$MOCK_REVISION" ;;
    esac
    ;;
  "rm -f")
    [[ "$3" == "$id" ]] || exit 1
    : > "$MOCK_STATE"
    ;;
  "create --name")
    [[ -z "$id" ]] || exit 1
    if [[ "${@: -1}" == "triptime:new" ]]; then
      if [[ "$MOCK_MODE" == "changed-while-creating" ]]; then
        printf '%s\n' "$MOCK_CHANGED_IMAGE" > "$MOCK_STATE"
      else
        printf '%s\n' "$MOCK_CANDIDATE" > "$MOCK_STATE"
      fi
      echo candidate-id
    else
      printf '%s\n' "$MOCK_PREVIOUS" > "$MOCK_STATE"
      echo old-id
    fi
    ;;
  "start candidate-id")
    [[ "$MOCK_MODE" != "start-fails" ]]
    ;;
  "run --detach")
    [[ -z "$id" && "${@: -1}" == "triptime:old" ]] || exit 1
    printf '%s\n' "$MOCK_PREVIOUS" > "$MOCK_STATE"
    ;;
esac
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
    env_file.touch()
    markers = tmp_path / "rollback/dev"
    markers.mkdir(parents=True)
    (markers / "previous-image.txt").write_text("triptime:stale", encoding="utf-8")
    for command, source in {
        "ssh": "#!/usr/bin/env bash\nexec bash\n",
        "curl": '#!/usr/bin/env bash\ntest -s "$MOCK_STATE"\n',
        "docker": MOCK_DOCKER,
    }.items():
        stub = tmp_path / command
        stub.write_text(source, encoding="utf-8", newline="\n")
        stub.chmod(0o755)

    def run(script, initial, *, mode="", revision=SHA, expected_revision=SHA):
        state.write_text(initial + "\n", encoding="utf-8", newline="\n")
        log.touch()
        env = {
            **os.environ,
            "MOCK_TOOLS": tmp_path.as_posix(),
            "MOCK_STATE": state.as_posix(),
            "MOCK_LOG": log.as_posix(),
            "MOCK_MODE": mode,
            "MOCK_REVISION": revision,
            "MOCK_CANDIDATE": CANDIDATE,
            "MOCK_CHANGED_IMAGE": CANDIDATE.replace("sha256:new", "sha256:other"),
            "MOCK_PREVIOUS": PREVIOUS,
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
            "DEPLOY_ATTEMPT_ID": "job-43",
            "ROLLBACK_EXPECTED_IMAGE_REF": "triptime:new",
            "ROLLBACK_EXPECTED_ATTEMPT_ID": "job-43",
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
        (CANDIDATE, "", 0, PREVIOUS, "rm -f candidate-id"),
        ("", "", 3, "", None),
        (PREVIOUS, "", 3, PREVIOUS, None),
        (REPLACEMENT, "", 3, REPLACEMENT, None),
        (CANDIDATE, "changed-while-restoring", 1, REPLACEMENT, "rm -f candidate-id"),
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
    assert state == PREVIOUS
    assert "start candidate-id" not in actions
    assert "rm -f candidate-id" in actions


def test_deploy_auto_rollback_uses_failed_container_id_without_new_config(
    deploy_mock,
) -> None:
    result, state, actions = deploy_mock("deploy", PREVIOUS, mode="start-fails")
    assert result.returncode == 1 and "rollback health check passed" in result.stdout
    assert state == PREVIOUS
    assert "rm -f candidate-id" in actions
    restores = [action for action in actions if action.startswith("create ")]
    assert len(restores) == 2
    assert "--label triptime_deploy_attempt=job-43" in restores[0]
    assert "--label triptime_previous_image=triptime:old" in restores[0]
    assert "TTS_VERSION" not in restores[1]
    assert "TTS_PROVIDER" not in restores[1]


def test_explicit_image_only_deploy_without_expected_revision_remains_supported(
    deploy_mock,
) -> None:
    result, state, _ = deploy_mock(
        "deploy", PREVIOUS, expected_revision="", revision=""
    )
    assert result.returncode == 0, result
    assert state == CANDIDATE
