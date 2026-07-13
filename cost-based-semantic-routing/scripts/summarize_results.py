#!/usr/bin/env python3
"""Summarize one local semantic-routing experiment result file."""

import argparse
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


def load_quality_review(path):
    if not path:
        return None
    with Path(path).open(encoding="utf-8") as stream:
        review = json.load(stream)
    required = {"reviewed", "total", "acceptance_comparison", "pairwise"}
    missing = sorted(required - set(review))
    if missing:
        raise ValueError(f"{path}: quality review is missing: {', '.join(missing)}")
    return review


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


def routed_model_mix(rows, catalog):
    routed = [row for row in rows if row.get("lane") == "routed" and row.get("ok")]
    if not routed:
        return None
    counts = defaultdict(int)
    for row in routed:
        counts[canonical_model(catalog, row.get("selected_model"))] += 1
    return {
        "total": len(routed),
        "models": [
            {"model": model, "requests": count, "fraction": count / len(routed)}
            for model, count in sorted(counts.items())
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


def build_summary(rows, catalog, expensive_model, quality_review=None):
    return {
        "lanes": lane_summary(rows),
        "routing": routing_summary(rows, catalog),
        "routed_model_mix": routed_model_mix(rows, catalog),
        "savings": savings(rows, catalog, expensive_model),
        "quality_review": quality_review,
    }


def render_summary(summary):
    lines = ["Lane summary", "lane,requests,ok,input_tokens,output_tokens,cost_estimate,p50_ms,p95_ms"]
    for lane, values in summary["lanes"].items():
        lines.append(f"{lane},{values['requests']},{values['ok']},{values['input_tokens']},{values['output_tokens']},${values['cost_estimate_usd']:.6f},{values['latency_ms']['p50']:.1f},{values['latency_ms']['p95']:.1f}")
    routing = summary["routing"]
    if routing:
        lines.extend(["", f"Corpus-label selection agreement (diagnostic): {routing['correct']}/{routing['total']} = {routing['accuracy']:.1%}", "expected_model,selected_model,count"])
        lines.extend(f"{item['expected_model']},{item['selected_model']},{item['count']}" for item in routing["confusion_matrix"])
    for label, values in (("Counterfactual savings on routed token counts", summary["savings"]["counterfactual_on_routed_tokens"]), ("Actual lane savings", summary["savings"]["actual_lanes"])):
        if values:
            lines.append(f"{label}: ${values['always_expensive_cost_usd']:.6f} always_expensive vs ${values['routed_cost_usd']:.6f} routed = {values['savings_fraction']:.1%}")
    quality_review = summary["quality_review"]
    if quality_review:
        acceptance = quality_review["acceptance_comparison"]
        pairwise = quality_review["pairwise"]
        quality_scores = quality_review.get("quality_scores", {})
        acceptance_value = acceptance.get("fraction")
        acceptance_text = "unavailable" if acceptance_value is None else f"{acceptance_value:.1%}"
        lines.extend((
            "\nBlinded answer spot check",
            f"Review coverage: {quality_review['reviewed']}/{quality_review['total']} = {quality_review['coverage_fraction']:.1%}",
            "Acceptance compared with always-expensive baseline: "
            f"{acceptance['routed_acceptable']}/{acceptance['always_expensive_acceptable']} "
            f"= {acceptance_text}",
            "Routed materially worse than the expensive baseline: "
            f"{pairwise['routed_materially_worse_than_expensive']}/{pairwise['reviewed']}",
        ))
        if quality_scores:
            lines.append(
                "Average reviewer quality score: "
                f"routed={quality_scores['routed']['average']:.2f}, "
                f"always_expensive={quality_scores['always_expensive']['average']:.2f}"
            )
    else:
        lines.append("\nBlinded answer spot check: pending")
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
    parser.add_argument("--quality-review", default="")
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--text-output", type=Path)
    args = parser.parse_args()
    summary = build_summary(
        load_results(args.results),
        load_catalog(args.catalog),
        args.expensive_model,
        load_quality_review(args.quality_review),
    )
    rendered = render_summary(summary)
    if args.json_output:
        write(args.json_output, summary, lambda value: json.dumps(value, indent=2, sort_keys=True) + "\n")
    if args.text_output:
        write(args.text_output, rendered, lambda value: value)
    print(rendered, end="")


if __name__ == "__main__":
    main()
