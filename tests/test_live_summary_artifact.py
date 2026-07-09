from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run_summary_writer(artifact_root: Path) -> dict[str, object]:
    script = (
        "import { writeLiveSummary } from './tests/e2e/live-summary.mjs';"
        f"writeLiveSummary({str(artifact_root)!r});"
    )
    subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        check=True,
    )
    return json.loads((artifact_root / "e2e-live-summary.json").read_text())


def test_passing_live_summary_does_not_count_success_reports_as_failures(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "live"
    report_dir = artifact_root / "test-results" / "successful-route-test"
    report_dir.mkdir(parents=True)
    (artifact_root / "e2e-runtime.json").write_text(
        json.dumps(
            {
                "baseURL": "http://127.0.0.1:39080",
                "live": True,
                "fixture": False,
                "strict": True,
                "TTS_PROVIDER": "naver_playwright",
            }
        )
    )
    (artifact_root / "test-results" / ".last-run.json").write_text(
        json.dumps({"status": "passed", "failedTests": []})
    )
    (report_dir / "arrival-blocking-1-report.json").write_text(
        json.dumps({"suite": "arrival-route", "phase": "blocking"})
    )

    summary = _run_summary_writer(artifact_root)

    assert summary["status"] == "completed"
    assert summary["failed_count"] == 0
    assert summary["first_failure_bucket"] is None
    assert all(count == 0 for count in summary["bucket_counts"].values())
    assert "arrival-blocking-1-report.json" in summary["report_paths"][0]
    assert "arrival-route" not in json.dumps(summary, ensure_ascii=False)


def test_failed_live_summary_classifies_only_failed_tests(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "live"
    report_dir = artifact_root / "test-results" / "successful-route-test"
    report_dir.mkdir(parents=True)
    (artifact_root / "e2e-runtime.json").write_text(
        json.dumps(
            {
                "baseURL": "http://127.0.0.1:39080",
                "live": True,
                "fixture": False,
                "strict": True,
                "TTS_PROVIDER": "naver_playwright",
            }
        )
    )
    (artifact_root / "test-results" / ".last-run.json").write_text(
        json.dumps(
            {
                "status": "failed",
                "failedTests": [
                    "autocomplete.spec.ts › blocking corpus: 강남역",
                ],
            }
        )
    )
    (report_dir / "arrival-blocking-1-report.json").write_text(
        json.dumps({"suite": "arrival-route", "phase": "blocking"})
    )

    summary = _run_summary_writer(artifact_root)

    assert summary["status"] == "failed"
    assert summary["failed_count"] == 1
    assert summary["first_failure_bucket"] in {
        "panel_parse_timeout",
        "provider_retry_exhausted",
    }
    assert summary["bucket_counts"]["panel_parse_timeout"] == 1
    assert summary["bucket_counts"]["provider_retry_exhausted"] == 0


def test_failed_live_summary_uses_error_contexts_for_opaque_last_run_ids(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "live"
    test_results = artifact_root / "test-results"
    (test_results / "autocomplete-live-autocomplete-smoke-blocking-corpus").mkdir(
        parents=True
    )
    (test_results / "arrival-mode-live-arrival-blocking").mkdir(parents=True)
    (artifact_root / "e2e-runtime.json").write_text(
        json.dumps(
            {
                "baseURL": "http://127.0.0.1:39080",
                "live": True,
                "fixture": False,
                "strict": True,
                "TTS_PROVIDER": "naver_playwright",
            }
        )
    )
    (test_results / ".last-run.json").write_text(
        json.dumps(
            {
                "status": "failed",
                "failedTests": [
                    "bd00a4a879b496db81ab-059e8fc7ae0aa2daca73",
                    "80c6a7b146ec1d93ea93-c7e731c0ab5cc2db9b24",
                ],
            }
        )
    )
    (
        test_results
        / "autocomplete-live-autocomplete-smoke-blocking-corpus"
        / "error-context.md"
    ).write_text("# Page snapshot\n")
    (
        test_results / "arrival-mode-live-arrival-blocking" / "error-context.md"
    ).write_text("교통 정보 제공자 호출 중 오류가 발생했습니다.\n")

    summary = _run_summary_writer(artifact_root)

    assert summary["status"] == "failed"
    assert summary["failed_count"] == 2
    assert summary["first_failure_bucket"] in {
        "panel_parse_timeout",
        "provider_retry_exhausted",
    }
    assert summary["bucket_counts"]["panel_parse_timeout"] == 1
    assert summary["bucket_counts"]["provider_retry_exhausted"] == 1
    assert summary["bucket_counts"]["unknown"] == 0


def test_live_summary_uses_runtime_mode_and_policy_without_parent_env(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "live"
    report_dir = artifact_root / "test-results" / "successful-diagnose-test"
    report_dir.mkdir(parents=True)
    (artifact_root / "e2e-runtime.json").write_text(
        json.dumps(
            {
                "baseURL": "http://127.0.0.1:39080",
                "live": True,
                "fixture": False,
                "strict": True,
                "TTS_PROVIDER": "naver_playwright",
                "TTS_LIVE_MODE": "diagnose",
                "LIVE_E2E_POLICY": "blocking",
            }
        )
    )
    (artifact_root / "test-results" / ".last-run.json").write_text(
        json.dumps({"status": "passed", "failedTests": []})
    )
    (report_dir / "diagnose-report.json").write_text(json.dumps({"ok": True}))

    summary = _run_summary_writer(artifact_root)

    assert summary["mode"] == "diagnose"
    assert summary["policy"] == "blocking"
