#!/usr/bin/env python3
"""Run the semantic-routing corpus through routed and forced-model lanes."""

import argparse
import json
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
VSR_HEADERS = (
    "x-vsr-selected-model",
    "x-vsr-selected-decision",
    "x-vsr-selected-confidence",
    "x-vsr-selected-category",
    "x-vsr-selected-reasoning",
    "x-vsr-matched-keywords",
    "x-vsr-matched-embeddings",
    "x-vsr-matched-complexity",
    "x-vsr-matched-context",
    "x-vsr-matched-structure",
    "x-vsr-matched-projection",
)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run routed and forced-model LLM eval lanes through agentgateway."
    )
    parser.add_argument("--gateway-url", default="")
    parser.add_argument("--path", default="/v1/chat/completions")
    parser.add_argument("--dataset", default=ROOT_DIR / "data" / "eval-corpus.jsonl", type=Path)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--run-id", default=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    parser.add_argument("--lanes", default="routed,always_low_cost,always_expensive")
    parser.add_argument("--low-cost-model", default="gpt-5.4-nano")
    parser.add_argument("--expensive-model", default="gpt-5.5")
    parser.add_argument("--auto-model", default="auto")
    parser.add_argument("--system-prompt", default="You are a concise technical assistant. Answer directly.")
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--delay-sec", type=float, default=0.0)
    parser.add_argument("--capture-output", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def load_jsonl(path, limit):
    rows = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise SystemExit(f"{path}:{line_number}: invalid JSON: {error}") from error
            if limit and len(rows) >= limit:
                break
    return rows


def load_catalog(path):
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def catalog_models(catalog):
    return catalog.get("providers", {}).get("openai", {}).get("models", {})


def canonical_model(catalog, model):
    value = (model or "").lower()
    matches = [name for name in catalog_models(catalog) if value == name or value.startswith(name + "-")]
    return max(matches, key=len) if matches else value


def catalog_model(catalog, *models):
    available = catalog_models(catalog)
    for candidate in models:
        name = canonical_model(catalog, candidate)
        if name in available:
            return name, available[name]
    raise ValueError(f"catalog has no OpenAI rates for models: {models!r}")


def effective_rates(model, input_tokens):
    rates = dict(model.get("rates", {}))
    for tier in model.get("tiers", []):
        if input_tokens > float(tier["contextOver"]):
            rates.update(tier.get("rates", {}))
    return {name: float(value) for name, value in rates.items()}


def estimate_cost(catalog, request_model, response_model, usage):
    input_tokens = float(usage.get("input_tokens", 0) or 0)
    cached_tokens = min(float(usage.get("cached_input_tokens", 0) or 0), input_tokens)
    output_tokens = float(usage.get("output_tokens", 0) or 0)
    _, model = catalog_model(catalog, request_model, response_model)
    rates = effective_rates(model, input_tokens)
    return (
        (input_tokens - cached_tokens) * rates.get("input", 0.0)
        + cached_tokens * rates.get("cacheRead", 0.0)
        + output_tokens * rates.get("output", 0.0)
    ) / 1_000_000


def request_model(args, lane):
    if lane == "routed":
        return args.auto_model
    if lane == "always_low_cost":
        return args.low_cost_model
    if lane == "always_expensive":
        return args.expensive_model
    raise SystemExit(f"unknown lane: {lane}")


def request_url(gateway_url, path):
    if not gateway_url:
        raise SystemExit("set --gateway-url")
    return gateway_url.rstrip("/") + "/" + path.lstrip("/")


def request_headers(run_id, item, lane):
    headers = {
        "Content-Type": "application/json",
        "X-Request-ID": f"vsr-{run_id}-{lane}-{item['id']}",
        "X-Experiment-ID": run_id,
        "X-Eval-ID": item["id"],
        "X-Eval-Lane": lane,
        "X-User-ID": f"vsr-{run_id}-{lane}",
    }
    if lane == "routed":
        headers["X-VSR-Debug"] = "true"
    return headers


def post_json(url, payload, headers, timeout):
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, {key.lower(): value for key, value in response.headers.items()}, response.read(), "", (time.perf_counter() - started) * 1000
    except urllib.error.HTTPError as error:
        return error.code, {key.lower(): value for key, value in error.headers.items()}, error.read(), str(error), (time.perf_counter() - started) * 1000
    except Exception as error:  # Keep network failures in the result artifact.
        return 0, {}, b"", str(error), (time.perf_counter() - started) * 1000


def usage(body):
    raw = body.get("usage", {}) if isinstance(body, dict) else {}
    details = raw.get("prompt_tokens_details", {}) if isinstance(raw, dict) else {}
    return {
        "input_tokens": raw.get("prompt_tokens", raw.get("input_tokens", 0)) or 0,
        "cached_input_tokens": details.get("cached_tokens", raw.get("cached_input_tokens", 0)) or 0,
        "output_tokens": raw.get("completion_tokens", raw.get("output_tokens", 0)) or 0,
        "total_tokens": raw.get("total_tokens", 0) or 0,
        "raw": raw,
    }


def response_text(body):
    choices = body.get("choices", []) if isinstance(body, dict) else []
    message = choices[0].get("message", {}) if choices else {}
    content = message.get("content", "")
    return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)


def run_one(args, catalog, url, item, lane):
    model = request_model(args, lane)
    headers = request_headers(args.run_id, item, lane)
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": args.system_prompt}, {"role": "user", "content": item["prompt"]}],
        "max_tokens": item.get("max_tokens", 180),
    }
    if args.temperature is not None:
        payload["temperature"] = args.temperature
    status, response_headers, raw, error, latency_ms = post_json(url, payload, headers, args.timeout)
    try:
        body = json.loads(raw.decode("utf-8")) if raw else {}
    except json.JSONDecodeError:
        body = {"raw_body": raw.decode("utf-8", errors="replace")}
    response_model = body.get("model", "") if isinstance(body, dict) else ""
    selected_model = response_headers.get("x-vsr-selected-model") or response_model or model
    response_usage = usage(body)
    cost_error = ""
    try:
        cost = estimate_cost(catalog, selected_model, response_model, response_usage)
    except ValueError as catalog_error:
        cost, cost_error = None, str(catalog_error)
    record = {
        "run_id": args.run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "id": item["id"],
        "family": item.get("family", ""),
        "lane": lane,
        "expected_model": item.get("expected_model", ""),
        "request_model": model,
        "selected_model": selected_model,
        "response_model": response_model,
        "status": status,
        "ok": 200 <= status < 300,
        "latency_ms": round(latency_ms, 3),
        "usage": response_usage,
        "cost_estimate_usd": cost,
        "cost_error": cost_error,
        "routing_correct": canonical_model(catalog, selected_model) == canonical_model(catalog, item.get("expected_model", "")) if lane == "routed" else None,
        "request_headers": {key.lower(): value for key, value in headers.items() if key.lower() != "content-type"},
        "vsr_headers": {name: response_headers.get(name, "") for name in VSR_HEADERS},
        "error": error,
    }
    if args.capture_output:
        record["response_text"] = response_text(body)
    elif not record["ok"]:
        record["error_body"] = body
    return record


def main():
    args = parse_args()
    catalog = load_catalog(args.catalog)
    items = load_jsonl(args.dataset, args.limit)
    lanes = [lane.strip() for lane in args.lanes.split(",") if lane.strip()]
    url = request_url(args.gateway_url, args.path)
    jobs = [(item, lane) for item in items for lane in lanes]
    random.Random(args.seed).shuffle(jobs)
    print(f"run_id={args.run_id}\nurl={url}\ndataset_items={len(items)} lanes={','.join(lanes)} total_requests={len(jobs)}\noutput={args.output}")
    if args.dry_run:
        for item, lane in jobs[:10]:
            print(f"dry-run {lane} {item['id']} model={request_model(args, lane)}")
        return
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as stream:
        for index, (item, lane) in enumerate(jobs, 1):
            record = run_one(args, catalog, url, item, lane)
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()
            print(f"{index:03d}/{len(jobs)} {lane:15s} {item['id']:14s} status={record['status']} selected={record['selected_model'] or '-'} latency_ms={record['latency_ms']:.1f}")
            if args.delay_sec > 0 and index < len(jobs):
                time.sleep(args.delay_sec)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
