#!/usr/bin/env python3
"""Create a compact blinded A/B answer spot check from one demo run."""

import argparse
import csv
import hashlib
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from corpus import load_corpus


LANES = ("routed", "always_expensive")
ANSWER_LETTERS = ("a", "b")
REVIEW_FIELDS = (
    "review_id",
    "conversation",
    "answer_a",
    "answer_b",
    "answer_a_quality_1_to_5",
    "answer_b_quality_1_to_5",
    "answer_a_acceptable_yes_no",
    "answer_b_acceptable_yes_no",
    "materially_better_answer_a_b_none_unclear",
    "reviewer_id",
    "notes",
)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Create a blinded A/B answer-quality spot-check CSV."
    )
    parser.add_argument("results", type=Path)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--key-output", required=True, type=Path)
    parser.add_argument("--instructions-output", required=True, type=Path)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument(
        "--limit",
        type=int,
        default=12,
        help="Number of randomized prompts to include; 0 includes every prompt.",
    )
    return parser.parse_args()


def load_results(path):
    with path.open(encoding="utf-8") as stream:
        rows = [json.loads(line) for line in stream if line.strip()]
    if not rows:
        raise ValueError(f"{path}: result file is empty")
    return rows


def transcript(messages):
    names = {"user": "USER", "assistant": "ASSISTANT"}
    return "\n\n".join(
        f"{names[message['role']]}:\n{message['content']}"
        for message in messages
    )


def review_rows(results, items, seed, limit=0):
    items_by_id = {item["id"]: item for item in items}
    grouped = defaultdict(dict)
    run_ids = set()
    for row in results:
        if row.get("id") not in items_by_id:
            raise ValueError(
                f"{row.get('id', '<missing id>')}: not found in review dataset"
            )
        lane = row.get("lane")
        if lane not in LANES:
            continue
        if lane in grouped[row["id"]]:
            raise ValueError(f"{row['id']}: duplicate {lane} result")
        if not row.get("ok"):
            raise ValueError(f"{row['id']} {lane}: request did not succeed")
        if not isinstance(row.get("response_text"), str) or not row["response_text"].strip():
            raise ValueError(
                f"{row['id']} {lane}: response text is missing; rerun with CAPTURE_OUTPUT=true"
            )
        grouped[row["id"]][lane] = row
        run_ids.add(row.get("run_id", ""))
    if len(run_ids) != 1:
        raise ValueError("result file must contain exactly one run_id")

    review, key = [], []
    randomizer = random.Random(seed)
    item_ids = sorted(grouped)
    randomizer.shuffle(item_ids)
    if limit < 0:
        raise ValueError("review limit must not be negative")
    if limit:
        item_ids = item_ids[:limit]
    for index, item_id in enumerate(item_ids, 1):
        lane_rows = grouped[item_id]
        missing = [lane for lane in LANES if lane not in lane_rows]
        if missing:
            raise ValueError(f"{item_id}: missing lanes: {', '.join(missing)}")
        shuffled_lanes = list(LANES)
        randomizer.shuffle(shuffled_lanes)
        answer_mapping = {
            letter: {
                "lane": lane,
                "selected_model": lane_rows[lane].get("selected_model", ""),
                "response_model": lane_rows[lane].get("response_model", ""),
            }
            for letter, lane in zip(ANSWER_LETTERS, shuffled_lanes)
        }
        item = items_by_id[item_id]
        review_id = f"review-{index:03d}"
        review.append(
            {
                "review_id": review_id,
                "conversation": transcript(item["messages"]),
                **{
                    f"answer_{letter}": lane_rows[answer_mapping[letter]["lane"]]["response_text"]
                    for letter in ANSWER_LETTERS
                },
                **{
                    field: ""
                    for field in REVIEW_FIELDS
                    if field not in {"review_id", "conversation", "answer_a", "answer_b"}
                },
            }
        )
        key.append(
            {
                "review_id": review_id,
                "id": item_id,
                "answer_mapping": answer_mapping,
            }
        )
    return review, key, run_ids.pop()


def write_review(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=REVIEW_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def write_key(path, run_id, results_path, key_rows):
    value = {
        "schema_version": 1,
        "run_id": run_id,
        "result_file": results_path.name,
        "result_file_sha256": hashlib.sha256(results_path.read_bytes()).hexdigest(),
        "reviews": key_rows,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def write_instructions(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """# Blinded Answer Spot Check

Each row contains one developer prompt and two shuffled answers. Do not infer
the model or routing lane. Assess the answer a developer would receive.

For each answer, enter a score from `1` (unusable) to `5` (correct, complete,
and directly actionable), then enter `yes` or `no` for whether it is acceptable
for the task. In `materially_better_answer_a_b_none_unclear`, choose `a` or `b`
only when one answer would materially improve task completion. Use `none` when
they are effectively equivalent and `unclear` when the prompt cannot be rated.

Do not alter the review ID, prompt, answer text, or column names. Do not receive
the blind-key JSON until all assigned reviews are complete.
""",
        encoding="utf-8",
    )


def main():
    args = parse_args()
    review, key, run_id = review_rows(
        load_results(args.results), load_corpus(args.dataset), args.seed, args.limit
    )
    write_review(args.output, review)
    write_key(args.key_output, run_id, args.results, key)
    write_instructions(args.instructions_output)
    print(f"review={args.output}")
    print(f"blind_key={args.key_output}")
    print(f"instructions={args.instructions_output}")
    print(f"reviews={len(review)}")


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        raise SystemExit(str(error)) from error
