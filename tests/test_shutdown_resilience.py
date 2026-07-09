from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

KST = ZoneInfo("Asia/Seoul")


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_healthz(base_url: str, timeout_seconds: float = 120.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/healthz", timeout=3) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(1.0)
    raise AssertionError(f"service did not become ready: {base_url}")


def _request_json(
    url: str,
    payload: dict | None = None,
    *,
    method: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> object:
    body = None
    headers = {"User-Agent": "TripTimeShutdownTest/1.0"}
    if extra_headers:
        headers.update(extra_headers)
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.loads(response.read())


def _wait_for_autocomplete_warmup_overlap(
    base_url: str,
    debug_headers: dict[str, str],
    timeout_seconds: float = 15.0,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    last_metrics: dict[str, object] = {}
    while time.monotonic() < deadline:
        payload = _request_json(
            f"{base_url}/api/debug/autocomplete/runtime",
            extra_headers=debug_headers,
        )
        if isinstance(payload, dict):
            last_metrics = payload
            if int(payload.get("warmup_active") or 0) > 0:
                return payload
        time.sleep(0.25)
    serialized_metrics = json.dumps(last_metrics, ensure_ascii=False)
    raise AssertionError(
        f"autocomplete warmup did not become active: {serialized_metrics}"
    )


def _terminate_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=15)
    except Exception:
        proc.kill()
        proc.wait(timeout=10)


def _list_residual_browser_processes(user_data_dir: Path) -> list[str]:
    user_data_dir_text = str(user_data_dir).lower()
    if os.name == "nt":
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
                "Get-CimInstance Win32_Process | "
                "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Depth 3",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=20,
        )
        payload = (completed.stdout or "").strip() or "[]"
        rows = json.loads(payload)
        if isinstance(rows, dict):
            rows = [rows]
        return [
            f"{row.get('ProcessId')}:{row.get('Name')}:{row.get('CommandLine')}"
            for row in rows
            if isinstance(row, dict)
            and str(row.get("CommandLine") or "").lower().find(user_data_dir_text) >= 0
            and str(row.get("Name") or "").lower()
            in {"chrome.exe", "chromedriver.exe", "chromium.exe"}
        ]

    completed = subprocess.run(
        ["ps", "-eo", "pid=,args="],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    lines = completed.stdout.splitlines()
    residuals: list[str] = []
    for line in lines:
        lower = line.lower()
        if user_data_dir_text not in lower:
            continue
        if "chromedriver" in lower or "chrome" in lower or "chromium" in lower:
            residuals.append(line.strip())
    return residuals


@pytest.mark.skipif(
    not os.getenv("TTS_LIVE_EXTENDED"),
    reason="live shutdown smoke requires TTS_LIVE_EXTENDED=1",
)
def test_graceful_shutdown_cleans_up_chromium_processes(tmp_path: Path) -> None:
    user_data_dir = tmp_path / "chrome-profile"
    user_data_dir.mkdir(parents=True, exist_ok=True)
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env.update(
        {
            "TTS_PROVIDER": "naver_playwright",
            "TTS_RELOAD": "false",
            "TTS_HEADLESS": "true",
            "TTS_PORT": str(port),
            "TTS_CHROME_USER_DATA_DIR": str(user_data_dir),
            "TTS_ENABLE_DEBUG_ROUTES": "1",
            "TTS_DEBUG_TOKEN": "shutdown-test-token",
            "PYTHONUNBUFFERED": "1",
        }
    )
    debug_headers = {"X-TTS-Debug-Token": "shutdown-test-token"}

    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    proc = subprocess.Popen(
        [sys.executable, "-m", "trip_time_service"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        creationflags=creationflags,
    )

    try:
        _wait_for_healthz(base_url)
        tomorrow = (datetime.now(tz=KST) + timedelta(days=1)).replace(
            hour=9,
            minute=0,
            second=0,
            microsecond=0,
        )
        departure_time = tomorrow.isoformat()
        autocomplete_query = urllib.parse.quote("강남역")
        autocomplete_payload = _request_json(
            f"{base_url}/api/autocomplete?q={autocomplete_query}"
        )
        assert isinstance(autocomplete_payload, list)
        assert autocomplete_payload

        arrival_payload = _request_json(
            f"{base_url}/v1/trip/arrival-time",
            payload={
                "origin": "강남역",
                "destination": "판교역",
                "departure_time": departure_time,
            },
        )
        assert isinstance(arrival_payload, dict)
        assert int(arrival_payload["duration_seconds"]) > 0

        warmup_payload = _request_json(
            f"{base_url}/api/autocomplete/warmup",
            payload={
                "queries": [f"강남역 {index}" for index in range(1, 9)],
                "blocking": False,
            },
            method="POST",
        )
        assert isinstance(warmup_payload, dict)
        assert int(warmup_payload["queued"]) > 0
        runtime_payload = _wait_for_autocomplete_warmup_overlap(
            base_url,
            debug_headers,
        )
        assert int(runtime_payload.get("warmup_active") or 0) > 0

        cache_clear_payload = _request_json(
            f"{base_url}/api/debug/autocomplete/cache-clear",
            method="POST",
            extra_headers=debug_headers,
        )
        assert isinstance(cache_clear_payload, dict)
        assert cache_clear_payload.get("ok") is True
    finally:
        _terminate_process(proc)

    deadline = time.monotonic() + 15.0
    residuals = _list_residual_browser_processes(user_data_dir)
    while residuals and time.monotonic() < deadline:
        time.sleep(1.0)
        residuals = _list_residual_browser_processes(user_data_dir)

    assert not residuals, residuals
