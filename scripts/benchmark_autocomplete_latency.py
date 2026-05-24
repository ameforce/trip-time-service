from __future__ import annotations

import argparse
import collections
import json
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_DATASET = Path("tests/live/data/autocomplete-blocking.json")
DEFAULT_REPORT = Path(".artifacts/live/autocomplete-benchmark.json")


def _request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> object:
    body = None
    headers = {"User-Agent": "TripTimeBenchmark/2.0"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read()
    return json.loads(raw)


def _percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    idx = int((len(sorted_values) - 1) * ratio)
    return sorted_values[idx]


def _normalize_text(value: str) -> str:
    return "".join(str(value or "").lower().split())


def _load_dataset(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise SystemExit(f"Dataset must be a JSON array: {path}")
    cases: list[dict[str, Any]] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        query = str(entry.get("query") or "").strip()
        if not query:
            continue
        cases.append(
            {
                "query": query,
                "expected_any": [str(token) for token in entry.get("expected_any", [])],
                "min_results": int(entry.get("min_results", 1)),
                "require_coords": bool(entry.get("require_coords", False)),
                "retry_once_on_empty": bool(entry.get("retry_once_on_empty", True)),
                "category": str(entry.get("category") or "unknown"),
            }
        )
    if not cases:
        raise SystemExit(f"No valid cases found in dataset: {path}")
    return cases


def _clear_cache(base_url: str) -> None:
    _request_json(f"{base_url}/api/debug/autocomplete/cache-clear", method="POST")


def _matches_expected(items: list[dict[str, Any]], case: dict[str, Any]) -> bool:
    top_items = items[:5]
    normalized_tokens = [_normalize_text(token) for token in case["expected_any"]]
    if normalized_tokens:
        token_match = any(
            token in _normalize_text(
                f"{item.get('display_name', '')} {item.get('address', '')}"
            )
            for token in normalized_tokens
            for item in top_items
        )
        if not token_match:
            return False

    if len(items) < case["min_results"]:
        return False

    if not case["require_coords"]:
        return True

    return any(
        _is_valid_coord(item.get("lat")) and _is_valid_coord(item.get("lon"))
        for item in top_items
    )


def _is_valid_coord(value: object) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return number == number and number not in (float("inf"), float("-inf"))


def _run_case(base_url: str, case: dict[str, Any]) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    max_attempts = 2 if case["retry_once_on_empty"] else 1

    for attempt in range(1, max_attempts + 1):
        encoded_query = urllib.parse.quote(case["query"], safe="")
        url = f"{base_url}/api/autocomplete?q={encoded_query}"
        started = time.perf_counter()
        try:
            payload = _request_json(url)
            elapsed_ms = (time.perf_counter() - started) * 1000
            items = payload if isinstance(payload, list) else []
            dict_items = [item for item in items if isinstance(item, dict)]
            sources = collections.Counter(
                str(item.get("source") or "unknown") for item in dict_items
            )
            attempt_report = {
                "attempt": attempt,
                "latency_ms": round(elapsed_ms, 1),
                "count": len(dict_items),
                "sources": dict(sources),
                "top_hit_token_match": _matches_expected(dict_items, case),
                "top_items": [
                    {
                        "display_name": item.get("display_name"),
                        "address": item.get("address"),
                        "source": item.get("source"),
                        "lat": item.get("lat"),
                        "lon": item.get("lon"),
                    }
                    for item in dict_items[:5]
                ],
                "error": None,
            }
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000
            dict_items = []
            attempt_report = {
                "attempt": attempt,
                "latency_ms": round(elapsed_ms, 1),
                "count": 0,
                "sources": {},
                "top_hit_token_match": False,
                "top_items": [],
                "error": str(exc),
            }
        attempts.append(attempt_report)
        if dict_items and attempt_report["top_hit_token_match"]:
            break
        if attempt < max_attempts:
            _clear_cache(base_url)

    final_attempt = attempts[-1]
    return {
        "query": case["query"],
        "category": case["category"],
        "attempts": attempts,
        "attempts_used": len(attempts),
        "retried_after_empty": len(attempts) > 1,
        "retry_recovered": len(attempts) > 1
        and final_attempt["count"] >= case["min_results"]
        and final_attempt["top_hit_token_match"],
        "final_count": final_attempt["count"],
        "final_latency_ms": final_attempt["latency_ms"],
        "final_sources": final_attempt["sources"],
        "top_hit_token_match": final_attempt["top_hit_token_match"],
        "final_error": final_attempt["error"],
        "success": final_attempt["count"] >= case["min_results"]
        and final_attempt["top_hit_token_match"],
    }


def _print_summary(title: str, reports: list[dict[str, Any]]) -> None:
    latencies = [report["final_latency_ms"] for report in reports]
    success_count = sum(1 for report in reports if report["success"])
    retried_count = sum(1 for report in reports if report["retried_after_empty"])
    retry_recovered_count = sum(1 for report in reports if report["retry_recovered"])
    print(f"{title}:")
    print(f"  cases    : {len(reports)}")
    print(f"  success  : {success_count}/{len(reports)}")
    print(f"  retried  : {retried_count}")
    print(f"  recovered: {retry_recovered_count}")
    if latencies:
        print(f"  avg_ms   : {statistics.mean(latencies):.1f}")
    else:
        print("  avg_ms   : 0.0")
    print(f"  p50_ms   : {_percentile(latencies, 0.50):.1f}")
    print(f"  p95_ms   : {_percentile(latencies, 0.95):.1f}")
    print(f"  max_ms   : {max(latencies):.1f}" if latencies else "  max_ms   : 0.0")


def _run_rounds(
    base_url: str, cases: list[dict[str, Any]], rounds: int
) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for _ in range(max(1, rounds)):
        for case in cases:
            reports.append(_run_case(base_url, case))
    return reports


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Measure live /api/autocomplete quality and latency from dataset.",
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8500",
        help="Service base URL (default: http://127.0.0.1:8500)",
    )
    parser.add_argument(
        "--dataset",
        default=str(DEFAULT_DATASET),
        help="Dataset JSON path",
    )
    parser.add_argument(
        "--report",
        default=str(DEFAULT_REPORT),
        help="Output report path",
    )
    parser.add_argument(
        "--cold-rounds",
        type=int,
        default=1,
        help="How many cold rounds to execute",
    )
    parser.add_argument(
        "--hot-rounds",
        type=int,
        default=2,
        help="How many hot rounds to execute",
    )
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    dataset_path = Path(args.dataset)
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    cases = _load_dataset(dataset_path)
    print(f"Base URL : {base_url}")
    print(f"Dataset  : {dataset_path}")
    print(f"Cases    : {len(cases)}")

    try:
        _clear_cache(base_url)
        print("Cache clear: OK")
    except urllib.error.URLError as exc:
        print(f"Cache clear: failed ({exc})")

    cold_reports = _run_rounds(base_url, cases, args.cold_rounds)
    _print_summary("Cold run", cold_reports)

    hot_reports = _run_rounds(base_url, cases, args.hot_rounds)
    _print_summary("Hot run", hot_reports)

    source_counter = collections.Counter()
    category_counter = collections.Counter()
    for report in cold_reports + hot_reports:
        source_counter.update(report["final_sources"])
        category_counter.update([report["category"]])

    payload = {
        "base_url": base_url,
        "dataset": str(dataset_path),
        "cold_rounds": args.cold_rounds,
        "hot_rounds": args.hot_rounds,
        "source_distribution": dict(source_counter),
        "category_distribution": dict(category_counter),
        "cold": cold_reports,
        "hot": hot_reports,
    }
    report_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Report   : {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
