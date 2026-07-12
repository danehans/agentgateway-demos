#!/usr/bin/env python3
"""Load and validate the multi-turn semantic-routing evaluation corpus."""

import json
import random
from pathlib import Path


def _fail(path, line_number, message):
    raise ValueError(f"{path}:{line_number}: {message}")


def _text(value, path, line_number, field):
    if not isinstance(value, str) or not value.strip():
        _fail(path, line_number, f"{field} must be a non-empty string")
    return value


def _turn_item(conversation, turn, turn_number, history, path, line_number):
    conversation_id = _text(conversation.get("id"), path, line_number, "id")
    language = _text(conversation.get("language"), path, line_number, "language")
    family = _text(turn.get("family"), path, line_number, "turn family")
    expected_model = _text(
        turn.get("expected_model"), path, line_number, "turn expected_model"
    )
    user = _text(turn.get("user"), path, line_number, "turn user")
    max_tokens = turn.get("max_tokens")
    if not isinstance(max_tokens, int) or max_tokens <= 0:
        _fail(path, line_number, "turn max_tokens must be a positive integer")

    item = {
        "id": f"{conversation_id}-turn-{turn_number}",
        "conversation_id": conversation_id,
        "turn": turn_number,
        "language": language,
        "family": family,
        "expected_model": expected_model,
        "max_tokens": max_tokens,
        "messages": [*history, {"role": "user", "content": user}],
    }
    assistant_context = turn.get("assistant_context", "")
    if assistant_context:
        history.extend(
            [
                {"role": "user", "content": user},
                {
                    "role": "assistant",
                    "content": _text(
                        assistant_context,
                        path,
                        line_number,
                        "turn assistant_context",
                    ),
                },
            ]
        )
    elif turn_number != len(conversation["turns"]):
        _fail(path, line_number, "every non-final turn requires assistant_context")
    return item


def _conversation_items(conversation, path, line_number):
    turns = conversation.get("turns")
    if not isinstance(turns, list) or not turns:
        _fail(path, line_number, "turns must be a non-empty list")
    history = []
    return [
        _turn_item(conversation, turn, index, history, path, line_number)
        for index, turn in enumerate(turns, 1)
    ]


def _legacy_item(row, path, line_number):
    """Keep the loader usable with a single-turn JSONL corpus."""
    prompt = _text(row.get("prompt"), path, line_number, "prompt")
    _text(row.get("id"), path, line_number, "id")
    _text(row.get("expected_model"), path, line_number, "expected_model")
    return {**row, "messages": [{"role": "user", "content": prompt}]}


def load_corpus(path, limit=0):
    path = Path(path)
    items = []
    conversation_ids = set()
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {error}") from error
            if not isinstance(row, dict):
                _fail(path, line_number, "each row must be an object")
            if "turns" in row:
                conversation_id = _text(row.get("id"), path, line_number, "id")
                if conversation_id in conversation_ids:
                    _fail(path, line_number, f"duplicate conversation id: {conversation_id}")
                conversation_ids.add(conversation_id)
                expanded = _conversation_items(row, path, line_number)
            else:
                expanded = [_legacy_item(row, path, line_number)]
            items.extend(expanded)
            if limit and len(items) >= limit:
                return items[:limit]
    if not items:
        raise ValueError(f"{path}: corpus is empty")
    ids = [item["id"] for item in items]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{path}: duplicate evaluation item id")
    return items


def expected_models(path):
    return sorted({item["expected_model"] for item in load_corpus(path)})


def balanced_subset(items, limit, seed=7):
    """Select a deterministic, model-balanced subset without changing the corpus."""
    if limit < 0:
        raise ValueError("limit must not be negative")
    if not limit or limit >= len(items):
        return items

    groups = {}
    for index, item in enumerate(items):
        groups.setdefault(item["expected_model"], []).append((index, item))

    models = sorted(groups)
    base, remainder = divmod(limit, len(models))
    targets = {
        model: base + int(index < remainder)
        for index, model in enumerate(models)
    }
    selected = []
    randomizer = random.Random(seed)
    for model in models:
        candidates = list(groups[model])
        randomizer.shuffle(candidates)
        target = min(targets[model], len(candidates))
        selected.extend(candidates[:target])
        targets[model] -= target

    remaining = limit - len(selected)
    if remaining:
        for model in models:
            candidates = [
                candidate
                for candidate in groups[model]
                if candidate not in selected
            ]
            randomizer.shuffle(candidates)
            selected.extend(candidates[:remaining])
            remaining = limit - len(selected)
            if not remaining:
                break
    if remaining:
        raise ValueError(f"requested {limit} corpus items, but only found {len(items)}")

    return [item for _index, item in sorted(selected)]
