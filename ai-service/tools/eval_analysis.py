#!/usr/bin/env python3
"""Measure AI analysis quality against a live Ollama.

The unit tests cover parsing; they cannot tell you whether llama3.2:3b actually
produces *useful* analyses. This runs a fixed set of incident scenarios through
the real model and reports the numbers worth quoting: how often the response
parsed, how confident the model was, whether it recommended an action the
service can actually execute, and how long it took.

    cd ai-service
    python tools/eval_analysis.py --runs 3

Exits non-zero if the parse rate falls below --min-parse-rate, so it can gate a
release as well as inform a report.
"""

import argparse
import asyncio
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.ollama_client import OllamaClient

EXECUTABLE_ACTIONS = ("restart_pod", "rollback_deployment", "scale_deployment", "notify_sre")

SCENARIOS = [
    {
        "name": "crash-loop (OOMKilled)",
        "alert": {
            "labels": {
                "alertname": "PodCrashLooping", "severity": "critical",
                "app": "demo-app", "namespace": "kubesage",
                "pod": "demo-app-7d9f8b6c5d-x2k9p",
            },
            "annotations": {"description": "Pod demo-app-7d9f8b6c5d-x2k9p has restarted 5 times"},
        },
        "logs": [
            "ERROR 2026-09-03T10:00:01Z Starting checkout worker pool (size=32)",
            "ERROR 2026-09-03T10:00:04Z Allocating 512MB response cache",
            "FATAL 2026-09-03T10:00:07Z java.lang.OutOfMemoryError: Java heap space",
            "ERROR 2026-09-03T10:00:07Z Container demo-app exited with code 137 (OOMKilled)",
        ],
        "expect_in_root_cause": ("memory", "oom", "heap"),
    },
    {
        "name": "error-rate (database down)",
        "alert": {
            "labels": {
                "alertname": "HighErrorRate", "severity": "critical",
                "app": "demo-app", "namespace": "kubesage",
            },
            "annotations": {"description": "Application demo-app has error rate of 0.34"},
        },
        "logs": [
            "ERROR 2026-09-03T10:05:11Z psycopg2.OperationalError: could not connect to server: Connection refused",
            'ERROR 2026-09-03T10:05:11Z Is the server running on host "postgres-service" (10.96.4.21) and accepting TCP/IP connections on port 5432?',
            "ERROR 2026-09-03T10:05:12Z GET /api/checkout 500 - unhandled exception",
            "ERROR 2026-09-03T10:05:13Z GET /api/products 500 - unhandled exception",
        ],
        "expect_in_root_cause": ("database", "postgres", "connection", "db"),
    },
    {
        "name": "latency (slow queries)",
        "alert": {
            "labels": {
                "alertname": "HighLatency", "severity": "warning",
                "app": "demo-app", "namespace": "kubesage",
            },
            "annotations": {"description": "P95 latency is 3.4s"},
        },
        "logs": [
            "WARNING 2026-09-03T10:10:02Z Slow query (2841ms): SELECT * FROM products WHERE category = $1",
            "WARNING 2026-09-03T10:10:05Z Connection pool exhausted, 32/32 in use, 14 waiting",
            "ERROR 2026-09-03T10:10:09Z Request timed out after 5000ms",
        ],
        "expect_in_root_cause": ("latency", "slow", "pool", "query", "load"),
    },
    {
        # Taken from a live run: a crash while error traffic was flowing. The
        # crash line is old and rare, the timeouts are new and repeated, so a
        # "last N lines" prompt never shows the model the actual cause.
        "name": "crash amid error noise",
        "alert": {
            "labels": {
                "alertname": "PodCrashLooping", "severity": "critical",
                "app": "demo-app", "namespace": "kubesage",
            },
            "annotations": {"description": "Container demo-app restarted 1 time(s) in the last 5 minutes"},
        },
        "logs": ["CRITICAL:__main__:Simulated application crash triggered"] + [
            line
            for _ in range(150)
            for line in (
                "ERROR:__main__:Failed to fetch products: Database connection timeout",
                "ERROR:__main__:Checkout failed: Payment gateway timeout",
            )
        ],
        # Must cite the crash itself. "timeouts, leading to crashes" names the
        # word crash but blames the noise, so it does not count.
        "expect_in_root_cause": ("simulated", "crash triggered", "critical"),
        "reject_in_root_cause": ("timeout",),
    },
    {
        # The mirror case, also from the live run: the same logs, but the alert
        # is the error spike. The 5xx responses come from the timeouts; the
        # crash is unrelated. A prompt that over-weights severe lines blames it.
        "name": "error spike despite a crash",
        "alert": {
            "labels": {
                "alertname": "HighErrorRate", "severity": "critical",
                "app": "demo-app", "namespace": "kubesage",
            },
            "annotations": {"description": "Application demo-app is returning 2.14 errors per second (5xx)"},
        },
        "logs": ["CRITICAL:__main__:Simulated application crash triggered"] + [
            line
            for _ in range(150)
            for line in (
                "ERROR:__main__:Failed to fetch products: Database connection timeout",
                "ERROR:__main__:Checkout failed: Payment gateway timeout",
            )
        ],
        "expect_in_root_cause": ("timeout", "database", "payment"),
        "reject_in_root_cause": ("crash",),
    },
    {
        "name": "no logs available",
        "alert": {
            "labels": {
                "alertname": "PodCrashLooping", "severity": "critical",
                "app": "demo-app", "namespace": "kubesage",
            },
            "annotations": {"description": "Pod has restarted 3 times"},
        },
        "logs": [],
        # With nothing to go on the model should hedge, not invent a cause
        "expect_low_confidence": True,
    },
]


async def evaluate(runs: int, min_parse_rate: float) -> int:
    client = OllamaClient()

    health = await client.health()
    print(f"Ollama: {health['status']} - {health['detail']}")
    if health["status"] in ("unreachable", "unhealthy"):
        print("\nCannot evaluate without a reachable Ollama. Start it with `ollama serve`.")
        await client.close()
        return 2

    print(f"Model: {client.model}   Runs per scenario: {runs}\n")

    parsed_total = 0
    attempts_total = 0
    all_latencies = []
    rows = []

    for scenario in SCENARIOS:
        parsed = 0
        grounded = 0
        actionable = 0
        confidences = []
        latencies = []

        for _ in range(runs):
            started = time.monotonic()
            analysis = await client.analyze_logs(scenario["logs"], scenario["alert"])
            latencies.append(time.monotonic() - started)
            attempts_total += 1

            if analysis.root_cause in (
                "Failed to parse AI response",
            ) or analysis.root_cause.startswith("AI analysis unavailable"):
                continue

            parsed += 1
            parsed_total += 1
            confidences.append(analysis.confidence_score)

            root_cause = analysis.root_cause.lower()
            if "expect_in_root_cause" in scenario:
                cited = any(term in root_cause for term in scenario["expect_in_root_cause"])
                blamed_noise = any(
                    term in root_cause for term in scenario.get("reject_in_root_cause", ())
                )
                if cited and not blamed_noise:
                    grounded += 1
            elif scenario.get("expect_low_confidence"):
                # hedging is the correct behaviour when there is nothing to read
                if analysis.confidence_score <= 60:
                    grounded += 1

            if any(action in analysis.recommended_fix.lower() for action in EXECUTABLE_ACTIONS):
                actionable += 1

        all_latencies.extend(latencies)
        rows.append({
            "name": scenario["name"],
            "parsed": f"{parsed}/{runs}",
            "grounded": f"{grounded}/{runs}",
            "actionable": f"{actionable}/{runs}",
            "confidence": f"{statistics.mean(confidences):.0f}" if confidences else "-",
            "latency": f"{statistics.mean(latencies):.1f}s" if latencies else "-",
        })

    header = f"{'scenario':28} {'parsed':>8} {'grounded':>9} {'actionable':>11} {'conf':>6} {'latency':>8}"
    print(header)
    print("-" * len(header))
    for row in rows:
        print(
            f"{row['name']:28} {row['parsed']:>8} {row['grounded']:>9} "
            f"{row['actionable']:>11} {row['confidence']:>6} {row['latency']:>8}"
        )

    parse_rate = parsed_total / attempts_total if attempts_total else 0.0
    print(f"\nparse rate      {parse_rate:.0%} ({parsed_total}/{attempts_total})")
    if all_latencies:
        ordered = sorted(all_latencies)
        p95 = ordered[min(int(len(ordered) * 0.95), len(ordered) - 1)]
        print(f"latency p50/p95 {statistics.median(all_latencies):.1f}s / {p95:.1f}s")

    print(
        "\ngrounded = root cause mentions a term the logs support (and not the noise, "
        "where a scenario defines it); "
        "actionable = recommended fix names an action the service can run"
    )

    await client.close()

    if parse_rate < min_parse_rate:
        print(f"\nFAIL: parse rate {parse_rate:.0%} is below the {min_parse_rate:.0%} threshold")
        return 1

    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3, help="runs per scenario (default 3)")
    parser.add_argument(
        "--min-parse-rate", type=float, default=0.9,
        help="fail below this parse rate (default 0.9)"
    )
    args = parser.parse_args()

    sys.exit(asyncio.run(evaluate(args.runs, args.min_parse_rate)))


if __name__ == "__main__":
    main()
