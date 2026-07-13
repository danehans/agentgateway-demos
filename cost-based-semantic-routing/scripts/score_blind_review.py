#!/usr/bin/env python3
"""Score a completed blinded A/B answer spot check."""

import argparse
import csv
import json
from collections import Counter
from pathlib import Path


LANES = ("routed", "always_expensive")
ANSWER_LETTERS = ("a", "b")
YES = {"yes", "y", "true", "1"}
NO = {"no", "n", "false", "0"}
BETTER_VALUES = {"a", "b", "none", "unclear"}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Score a blinded semantic-routing answer spot check."
    )
    parser.add_argument("--review", required=True, type=Path)
    parser.add_argument("--key", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def load_key(path):
    with path.open(encoding="utf-8") as stream:
        value = json.load(stream)
    reviews = value.get("reviews") if isinstance(value, dict) else None
    if not isinstance(reviews, list) or not reviews:
        raise ValueError(f"{path}: reviews must be a non-empty list")
    return value


def yes_no(value, review_id, field):
    normalized = value.strip().lower()
    if normalized in YES:
        return True
    if normalized in NO:
        return False
    raise ValueError(f"{review_id}: {field} must be yes or no")


def score_value(value, review_id, field):
    try:
        score = int(value.strip())
    except ValueError as error:
        raise ValueError(f"{review_id}: {field} must be an integer from 1 to 5") from error
    if not 1 <= score <= 5:
        raise ValueError(f"{review_id}: {field} must be an integer from 1 to 5")
    return score


def better_answer(value, review_id):
    normalized = value.strip().lower()
    if normalized not in BETTER_VALUES:
        raise ValueError(
            f"{review_id}: materially_better_answer_a_b_none_unclear must be "
            "a, b, none, or unclear"
        )
    return normalized


def load_completed_reviews(path, key):
    key_by_id = {entry["review_id"]: entry for entry in key["reviews"]}
    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    review_ids = [row.get("review_id", "") for row in rows]
    if len(review_ids) != len(set(review_ids)):
        raise ValueError(f"{path}: duplicate review_id")
    unexpected = sorted(set(review_ids) - set(key_by_id))
    if unexpected:
        raise ValueError(f"{path}: review_id not found in key: {', '.join(unexpected)}")

    completed = []
    for row in rows:
        review_id = row.get("review_id", "")
        if not review_id:
            continue
        required = [
            field
            for letter in ANSWER_LETTERS
            for field in (
                f"answer_{letter}_quality_1_to_5",
                f"answer_{letter}_acceptable_yes_no",
            )
        ] + ["materially_better_answer_a_b_none_unclear"]
        if all(not row.get(field, "").strip() for field in required):
            continue
        missing = [field for field in required if not row.get(field, "").strip()]
        if missing:
            raise ValueError(f"{review_id}: incomplete review; missing {', '.join(missing)}")
        mapping = key_by_id[review_id]["answer_mapping"]
        acceptance = {
            mapping[letter]["lane"]: yes_no(
                row[f"answer_{letter}_acceptable_yes_no"],
                review_id,
                f"answer_{letter}_acceptable_yes_no",
            )
            for letter in ANSWER_LETTERS
        }
        scores = {
            mapping[letter]["lane"]: score_value(
                row[f"answer_{letter}_quality_1_to_5"],
                review_id,
                f"answer_{letter}_quality_1_to_5",
            )
            for letter in ANSWER_LETTERS
        }
        if set(acceptance) != set(LANES):
            raise ValueError(f"{review_id}: blind key does not contain both lanes")
        better = better_answer(
            row["materially_better_answer_a_b_none_unclear"], review_id
        )
        completed.append(
            {
                "acceptance": acceptance,
                "scores": scores,
                "materially_better_lane": mapping[better]["lane"] if better in mapping else better,
            }
        )
    return completed


def score(completed, total, run_id):
    accepted = Counter()
    score_totals = Counter()
    routed_materially_worse = 0
    for review in completed:
        for lane in LANES:
            accepted[lane] += int(review["acceptance"][lane])
            score_totals[lane] += review["scores"][lane]
        if review["materially_better_lane"] == "always_expensive":
            routed_materially_worse += 1

    reviewed = len(completed)
    expensive_accepted = accepted["always_expensive"]
    return {
        "schema_version": 1,
        "run_id": run_id,
        "reviewed": reviewed,
        "total": total,
        "coverage_fraction": reviewed / total if total else 0.0,
        "acceptance_comparison": {
            "routed_acceptable": accepted["routed"],
            "always_expensive_acceptable": expensive_accepted,
            "fraction": accepted["routed"] / expensive_accepted if expensive_accepted else None,
        },
        "pairwise": {
            "routed_materially_worse_than_expensive": routed_materially_worse,
            "reviewed": reviewed,
            "fraction": routed_materially_worse / reviewed if reviewed else None,
        },
        "quality_scores": {
            lane: {
                "average": score_totals[lane] / reviewed if reviewed else None,
                "count": reviewed,
            }
            for lane in LANES
        },
    }


def main():
    args = parse_args()
    key = load_key(args.key)
    completed = load_completed_reviews(args.review, key)
    value = score(completed, len(key["reviews"]), key["run_id"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        raise SystemExit(str(error)) from error
