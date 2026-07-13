#!/usr/bin/env python3
import argparse
import json
from collections import Counter


def main():
    parser = argparse.ArgumentParser(description="Validate semantic routing eval results.")
    parser.add_argument("results")
    parser.add_argument("--expected-lanes", default="routed,always_expensive")
    args = parser.parse_args()

    with open(args.results, encoding="utf-8") as stream:
        rows = [json.loads(line) for line in stream if line.strip()]

    if not rows:
        raise SystemExit("result file is empty")

    failures = [row for row in rows if not row.get("ok")]
    if failures:
        print(f"{len(failures)} request(s) failed:")
        for row in failures[:10]:
            body = row.get("error_body") or row.get("error") or "unknown error"
            print(f"- {row.get('lane')} {row.get('id')}: status={row.get('status')} {body}")
        raise SystemExit(1)

    expected_lanes = {lane for lane in args.expected_lanes.split(",") if lane}
    actual_lanes = {row.get("lane") for row in rows}
    missing_lanes = expected_lanes - actual_lanes
    if missing_lanes:
        raise SystemExit(f"missing eval lanes: {', '.join(sorted(missing_lanes))}")

    missing_decisions = [
        row for row in rows
        if row.get("lane") == "routed" and not row.get("vsr_headers", {}).get("x-vsr-selected-decision")
    ]
    if missing_decisions:
        raise SystemExit(
            f"{len(missing_decisions)} routed request(s) did not return Semantic Router decision headers"
        )

    counts = Counter(row["lane"] for row in rows)
    print("validated results: " + ", ".join(f"{lane}={counts[lane]}" for lane in sorted(counts)))


if __name__ == "__main__":
    main()
