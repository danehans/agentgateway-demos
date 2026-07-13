#!/usr/bin/env python3
"""Create an anonymized answer-quality review package from one eval run."""

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


LANES = ("routed", "always_low_cost", "always_expensive")
ANSWER_LETTERS = ("a", "b", "c")
REVIEW_FIELDS = (
    "review_id",
    "conversation",
    "answer_a",
    "answer_b",
    "answer_c",
    "answer_a_quality_1_to_5",
    "answer_b_quality_1_to_5",
    "answer_c_quality_1_to_5",
    "answer_a_acceptable_yes_no",
    "answer_b_acceptable_yes_no",
    "answer_c_acceptable_yes_no",
    "materially_best_answer_a_b_c_none_unclear",
    "reviewer_id",
    "notes",
)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Create a blinded A/B/C answer-quality review CSV."
    )
    parser.add_argument("results", type=Path)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--key-output", required=True, type=Path)
    parser.add_argument("--instructions-output", required=True, type=Path)
    parser.add_argument("--seed", type=int, default=7)
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


def review_rows(results, items, seed):
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
        review_id = f"review-{index:04d}"
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
                    if field
                    not in {"review_id", "conversation", "answer_a", "answer_b", "answer_c"}
                },
            }
        )
        key.append(
            {
                "review_id": review_id,
                "id": item_id,
                "conversation_id": item.get("conversation_id", item_id),
                "turn": item.get("turn", 1),
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
    digest = hashlib.sha256(results_path.read_bytes()).hexdigest()
    value = {
        "schema_version": 1,
        "run_id": run_id,
        "result_file": results_path.name,
        "result_file_sha256": digest,
        "reviews": key_rows,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def write_instructions(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """# Blinded Answer-Quality Review

Each row contains one developer conversation and three anonymously shuffled
answers. Do not try to infer the model or routing lane. Assess the answer a
developer would receive.

For every answer:

1. Enter a quality score from `1` (unusable) to `5` (correct, complete, and
   directly actionable).
2. Enter `yes` in the acceptance column only when the answer clears the quality
   bar for this task; otherwise enter `no`.
3. In `materially_best_answer_a_b_c_none_unclear`, enter `a`, `b`, or `c` only
   when that answer is materially better for completing the task. Enter `none`
   when the answers are effectively equivalent, or `unclear` when the task
   cannot be rated from the supplied context.

Do not alter the review ID, transcript, answer text, or column names. Leave a
row blank only when it cannot be reviewed. Do not receive the blind-key JSON
until every assigned review is complete.
""",
        encoding="utf-8",
    )


def main():
    args = parse_args()
    review, key, run_id = review_rows(
        load_results(args.results), load_corpus(args.dataset), args.seed
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
