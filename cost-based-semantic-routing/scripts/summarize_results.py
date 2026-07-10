#!/usr/bin/env python3
"""Summarize one local semantic-routing experiment result file."""

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from run_eval import canonical_model, estimate_cost, load_catalog


def percentile(values, percentile_value):
    values = sorted(value for value in values if value is not None and not math.isnan(value))
    if not values:
        return 0.0
    rank = (len(values) - 1) * percentile_value
    lower, upper = math.floor(rank), math.ceil(rank)
    if lower == upper:
        return values[lower]
    return values[lower] + (values[upper] - values[lower]) * (rank - lower)


def load_results(path):
    with Path(path).open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def load_ratings(path):
    if not path:
        return {}
    with Path(path).open(encoding="utf-8") as stream:
        return {(row["id"], row["lane"]): row for row in csv.DictReader(stream)}


def lane_summary(rows):
    summary = {}
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["lane"]].append(row)
    for lane, lane_rows in sorted(grouped.items()):
        ok_rows = [row for row in lane_rows if row.get("ok")]
        usage = [row.get("usage", {}) for row in ok_rows]
        summary[lane] = {
            "requests": len(lane_rows),
            "ok": len(ok_rows),
            "input_tokens": sum(item.get("input_tokens", 0) or 0 for item in usage),
            "output_tokens": sum(item.get("output_tokens", 0) or 0 for item in usage),
            "cost_estimate_usd": sum(row.get("cost_estimate_usd", 0) or 0 for row in ok_rows),
            "latency_ms": {
                "p50": percentile([row.get("latency_ms") for row in ok_rows], 0.50),
                "p95": percentile([row.get("latency_ms") for row in ok_rows], 0.95),
            },
        }
    return summary


def routing_summary(rows, catalog):
    routed = [row for row in rows if row.get("lane") == "routed" and row.get("ok")]
    if not routed:
        return None
    confusion = defaultdict(int)
    for row in routed:
        confusion[(canonical_model(catalog, row.get("expected_model")), canonical_model(catalog, row.get("selected_model")))] += 1
    correct = sum(row.get("routing_correct") is True for row in routed)
    return {
        "correct": correct,
        "total": len(routed),
        "accuracy": correct / len(routed),
        "confusion_matrix": [
            {"expected_model": expected, "selected_model": selected, "count": count}
            for (expected, selected), count in sorted(confusion.items())
        ],
    }


def savings(rows, catalog, expensive_model):
    grouped = defaultdict(list)
    for row in rows:
        if row.get("ok"):
            grouped[row["lane"]].append(row)
    routed = grouped.get("routed", [])
    counterfactual = None
    if routed:
        expensive_cost = sum(
            estimate_cost(catalog, expensive_model, expensive_model, row.get("usage", {}))
            for row in routed
        )
        routed_cost = sum(row.get("cost_estimate_usd", 0) or 0 for row in routed)
        if expensive_cost:
            counterfactual = {
                "always_expensive_cost_usd": expensive_cost,
                "routed_cost_usd": routed_cost,
                "savings_fraction": 1 - routed_cost / expensive_cost,
            }
    actual = None
    if routed and grouped.get("always_expensive"):
        routed_cost = sum(row.get("cost_estimate_usd", 0) or 0 for row in routed)
        expensive_cost = sum(row.get("cost_estimate_usd", 0) or 0 for row in grouped["always_expensive"])
        if expensive_cost:
            actual = {
                "always_expensive_cost_usd": expensive_cost,
                "routed_cost_usd": routed_cost,
                "savings_fraction": 1 - routed_cost / expensive_cost,
            }
    return {"counterfactual_on_routed_tokens": counterfactual, "actual_lanes": actual}


def satisfaction_summary(rows, ratings):
    if not ratings:
        return None
    scores, right_model = defaultdict(list), []
    for row in rows:
        rating = ratings.get((row["id"], row["lane"]))
        if not rating:
            continue
        if rating.get("satisfaction"):
            scores[row["lane"]].append(float(rating["satisfaction"]))
        if row["lane"] == "routed" and rating.get("right_model"):
            right_model.append(rating["right_model"].strip().lower() in ("1", "true", "yes", "y"))
    return {
        "lanes": {lane: {"average": sum(values) / len(values), "count": len(values)} for lane, values in sorted(scores.items())},
        "human_right_model": {"correct": sum(right_model), "total": len(right_model), "rate": sum(right_model) / len(right_model)} if right_model else None,
    }


def build_summary(rows, ratings, catalog, expensive_model):
    return {
        "lanes": lane_summary(rows),
        "routing": routing_summary(rows, catalog),
        "savings": savings(rows, catalog, expensive_model),
        "satisfaction": satisfaction_summary(rows, ratings),
    }


def render_summary(summary):
    lines = ["Lane summary", "lane,requests,ok,input_tokens,output_tokens,cost_estimate,p50_ms,p95_ms"]
    for lane, values in summary["lanes"].items():
        lines.append(f"{lane},{values['requests']},{values['ok']},{values['input_tokens']},{values['output_tokens']},${values['cost_estimate_usd']:.6f},{values['latency_ms']['p50']:.1f},{values['latency_ms']['p95']:.1f}")
    routing = summary["routing"]
    if routing:
        lines.extend(["", f"Routing accuracy: {routing['correct']}/{routing['total']} = {routing['accuracy']:.1%}", "expected_model,selected_model,count"])
        lines.extend(f"{item['expected_model']},{item['selected_model']},{item['count']}" for item in routing["confusion_matrix"])
    for label, values in (("Counterfactual savings on routed token counts", summary["savings"]["counterfactual_on_routed_tokens"]), ("Actual lane savings", summary["savings"]["actual_lanes"])):
        if values:
            lines.append(f"{label}: ${values['always_expensive_cost_usd']:.6f} always_expensive vs ${values['routed_cost_usd']:.6f} routed = {values['savings_fraction']:.1%}")
    satisfaction = summary["satisfaction"]
    if satisfaction:
        lines.append("\nSatisfaction")
        lines.extend(f"{lane}: avg={values['average']:.2f} n={values['count']}" for lane, values in satisfaction["lanes"].items())
    return "\n".join(lines) + "\n"


def write(path, value, serialize):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialize(value), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Summarize semantic-routing eval JSONL results.")
    parser.add_argument("results", type=Path)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--expensive-model", default="gpt-5.5")
    parser.add_argument("--ratings", default="")
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--text-output", type=Path)
    args = parser.parse_args()
    summary = build_summary(load_results(args.results), load_ratings(args.ratings), load_catalog(args.catalog), args.expensive_model)
    rendered = render_summary(summary)
    if args.json_output:
        write(args.json_output, summary, lambda value: json.dumps(value, indent=2, sort_keys=True) + "\n")
    if args.text_output:
        write(args.text_output, rendered, lambda value: value)
    print(rendered, end="")


if __name__ == "__main__":
    main()
