#!/usr/bin/env python3
"""Score a completed blinded answer-quality review without exposing its key."""

import argparse
import csv
import json
from collections import Counter
from pathlib import Path


LANES = ("routed", "always_low_cost", "always_expensive")
YES = {"yes", "y", "true", "1"}
NO = {"no", "n", "false", "0"}
BEST_VALUES = {"a", "b", "c", "none", "unclear"}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Score a completed blinded semantic-routing quality review."
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


def normalize_yes_no(value, review_id, field):
    normalized = value.strip().lower()
    if normalized in YES:
        return True
    if normalized in NO:
        return False
    raise ValueError(f"{review_id}: {field} must be yes or no")


def normalize_best(value, review_id):
    normalized = value.strip().lower()
    if normalized not in BEST_VALUES:
        raise ValueError(
            f"{review_id}: materially_best_answer_a_b_c_none_unclear must be "
            "a, b, c, none, or unclear"
        )
    return normalized


def normalize_score(value, review_id, field):
    try:
        score = int(value.strip())
    except ValueError as error:
        raise ValueError(f"{review_id}: {field} must be an integer from 1 to 5") from error
    if score < 1 or score > 5:
        raise ValueError(f"{review_id}: {field} must be an integer from 1 to 5")
    return score


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
            for letter in ("a", "b", "c")
            for field in (
                f"answer_{letter}_quality_1_to_5",
                f"answer_{letter}_acceptable_yes_no",
            )
        ] + ["materially_best_answer_a_b_c_none_unclear"]
        if all(not row.get(field, "").strip() for field in required):
            continue
        missing = [field for field in required if not row.get(field, "").strip()]
        if missing:
            raise ValueError(f"{review_id}: incomplete review; missing {', '.join(missing)}")
        acceptance = {
            letter: normalize_yes_no(
                row[f"answer_{letter}_acceptable_yes_no"],
                review_id,
                f"answer_{letter}_acceptable_yes_no",
            )
            for letter in ("a", "b", "c")
        }
        scores = {
            letter: normalize_score(
                row[f"answer_{letter}_quality_1_to_5"],
                review_id,
                f"answer_{letter}_quality_1_to_5",
            )
            for letter in ("a", "b", "c")
        }
        mapping = key_by_id[review_id]["answer_mapping"]
        by_lane = {
            mapping[letter]["lane"]: acceptance[letter] for letter in ("a", "b", "c")
        }
        if set(by_lane) != set(LANES):
            raise ValueError(f"{review_id}: blind key does not contain every lane")
        best_letter = normalize_best(
            row["materially_best_answer_a_b_c_none_unclear"], review_id
        )
        best_lane = mapping[best_letter]["lane"] if best_letter in mapping else best_letter
        completed.append(
            {
                "review_id": review_id,
                "id": key_by_id[review_id]["id"],
                "acceptance": by_lane,
                "scores": {
                    mapping[letter]["lane"]: scores[letter]
                    for letter in ("a", "b", "c")
                },
                "materially_best_lane": best_lane,
                "response_models": {
                    mapping[letter]["lane"]: mapping[letter].get("response_model", "")
                    or mapping[letter].get("selected_model", "")
                    for letter in ("a", "b", "c")
                },
            }
        )
    return completed


def score(completed, total, run_id):
    accepted = Counter()
    score_totals = Counter()
    routed_materially_worse = 0
    high_required = 0
    high_required_routed_low = 0
    low_eligible = 0
    low_eligible_routed_high = 0
    uncertain = 0

    for review in completed:
        acceptance = review["acceptance"]
        for lane in LANES:
            accepted[lane] += int(acceptance[lane])
            score_totals[lane] += review["scores"][lane]
        best_lane = review["materially_best_lane"]
        if best_lane == "always_expensive":
            routed_materially_worse += 1
        routed_model = review["response_models"]["routed"]
        low_model = review["response_models"]["always_low_cost"]
        expensive_model = review["response_models"]["always_expensive"]
        routed_to_low = (
            bool(routed_model)
            and routed_model == low_model
        )
        routed_to_high = (
            bool(routed_model)
            and routed_model == expensive_model
        )
        if acceptance["always_expensive"] and not acceptance["always_low_cost"]:
            high_required += 1
            if routed_to_low:
                high_required_routed_low += 1
        elif acceptance["always_expensive"] and acceptance["always_low_cost"]:
            low_eligible += 1
            if routed_to_high:
                low_eligible_routed_high += 1
        else:
            uncertain += 1

    expensive_accepted = accepted["always_expensive"]
    reviewed = len(completed)
    return {
        "schema_version": 1,
        "run_id": run_id,
        "reviewed": reviewed,
        "total": total,
        "coverage_fraction": reviewed / total if total else 0.0,
        "quality_retention": {
            "routed_acceptable": accepted["routed"],
            "always_expensive_acceptable": expensive_accepted,
            "fraction": (
                accepted["routed"] / expensive_accepted if expensive_accepted else None
            ),
        },
        "pairwise": {
            "routed_materially_worse_than_expensive": routed_materially_worse,
            "reviewed": reviewed,
            "fraction": routed_materially_worse / reviewed if reviewed else None,
        },
        "quality_floor": {
            "always_low_cost_acceptable": accepted["always_low_cost"],
            "routed_acceptable": accepted["routed"],
            "always_expensive_acceptable": expensive_accepted,
        },
        "quality_scores": {
            lane: {
                "average": score_totals[lane] / reviewed if reviewed else None,
                "count": reviewed,
            }
            for lane in LANES
        },
        "capability_need": {
            "high_required": high_required,
            "high_required_routed_low_cost": high_required_routed_low,
            "low_eligible": low_eligible,
            "low_eligible_routed_expensive": low_eligible_routed_high,
            "uncertain": uncertain,
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
