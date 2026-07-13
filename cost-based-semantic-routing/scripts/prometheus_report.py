#!/usr/bin/env python3
import argparse
import json
import os
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta


def query(base_url, expression):
    url = f"{base_url.rstrip('/')}/api/v1/query?{urllib.parse.urlencode({'query': expression})}"
    with urllib.request.urlopen(url, timeout=20) as response:
        payload = json.load(response)
    if payload.get("status") != "success":
        raise RuntimeError(payload)
    return payload["data"]["result"]


def prom_value(row):
    return float(row["value"][1])


def prom_string(value):
    return json.dumps(value)


def normalize_vector(rows, label_order, value_name):
    normalized = []
    for row in sorted(rows, key=lambda item: tuple(item["metric"].get(k, "") for k in label_order)):
        item = {
            name: row["metric"][name]
            for name in label_order
            if row["metric"].get(name)
        }
        item[value_name] = prom_value(row)
        normalized.append(item)
    return normalized


def load_catalog(path):
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def load_result_metadata(path, evaluation_id):
    rows = []
    with open(path, encoding="utf-8") as stream:
        rows = [json.loads(line) for line in stream if line.strip()]
    if not rows:
        raise RuntimeError(f"result file is empty: {path}")
    run_ids = {row.get("run_id") for row in rows}
    if run_ids != {evaluation_id}:
        raise RuntimeError(
            f"result run IDs {sorted(str(value) for value in run_ids)} "
            f"do not match evaluation {evaluation_id}"
        )
    requests = []
    for row in rows:
        timestamp = row.get("timestamp")
        latency_ms = row.get("latency_ms")
        if not timestamp or latency_ms is None:
            raise RuntimeError(
                f"result file contains rows without timestamps or latency: {path}"
            )
        completed_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        started_at = completed_at - timedelta(milliseconds=float(latency_ms))
        requests.append((started_at, completed_at))
    return {
        "expected_requests": len(rows),
        "expected_lanes": sorted({row.get("lane", "") for row in rows if row.get("lane")}),
        "started_at": min(started for started, _ in requests).isoformat(),
        "ended_at": max(completed for _, completed in requests).isoformat(),
    }


def find_model(catalog, request_model, response_model):
    models = catalog.get("providers", {}).get("openai", {}).get("models", {})
    for candidate in (request_model, response_model):
        if candidate in models:
            return candidate, models[candidate]
        matches = [name for name in models if candidate.startswith(name + "-")]
        if matches:
            name = max(matches, key=len)
            return name, models[name]
    raise RuntimeError(
        f"catalog has no OpenAI rates for request={request_model!r}, response={response_model!r}"
    )


def effective_rates(model, input_tokens):
    rates = dict(model.get("rates", {}))
    for tier in model.get("tiers", []):
        if input_tokens > tier["contextOver"]:
            rates.update(tier.get("rates", {}))
    return {name: float(value) for name, value in rates.items()}


def price_tokens(catalog, request_model, response_model, tokens):
    input_tokens = tokens.get("input", 0.0)
    cache_read = min(tokens.get("input_cache_read", 0.0), input_tokens)
    remaining_input = max(input_tokens - cache_read, 0.0)
    cache_write = min(tokens.get("input_cache_write", 0.0), remaining_input)
    uncached_input = max(remaining_input - cache_write, 0.0)
    catalog_model, model = find_model(catalog, request_model, response_model)
    rates = effective_rates(model, input_tokens)
    cost = (
        uncached_input * rates.get("input", 0.0)
        + cache_read * rates.get("cacheRead", 0.0)
        + cache_write * rates.get("cacheWrite", 0.0)
        + tokens.get("output", 0.0) * rates.get("output", 0.0)
    ) / 1_000_000
    return catalog_model, cost


def calculate_costs(rows, catalog):
    grouped = defaultdict(lambda: defaultdict(float))
    for row in rows:
        labels = row["metric"]
        key = (
            labels.get("eval_lane", ""),
            labels.get("gen_ai_request_model", ""),
            labels.get("gen_ai_response_model", ""),
        )
        grouped[key][labels.get("gen_ai_token_type", "unknown")] += prom_value(row)

    lane_costs = []
    model_costs = defaultdict(float)
    for (lane, request_model, response_model), tokens in sorted(grouped.items()):
        catalog_model, cost = price_tokens(
            catalog, request_model, response_model, tokens
        )
        lane_costs.append({
            "eval_lane": lane,
            "gen_ai_request_model": request_model,
            "gen_ai_response_model": response_model,
            "catalog_model": catalog_model,
            "cost_usd": cost,
        })
        model_costs[(request_model, response_model, catalog_model)] += cost

    by_model = [
        {
            "gen_ai_request_model": request_model,
            "gen_ai_response_model": response_model,
            "catalog_model": catalog_model,
            "cost_usd": cost,
        }
        for (request_model, response_model, catalog_model), cost in sorted(model_costs.items())
    ]
    return lane_costs, by_model


def build_report(base_url, evaluation_id, catalog_path, results_path):
    result_metadata = load_result_metadata(results_path, evaluation_id)
    expected_requests = result_metadata["expected_requests"]
    selector = (
        'namespace="agentgateway-system",'
        'gateway="agentgateway-system/agentgateway-proxy",'
        f"evaluation_id={prom_string(evaluation_id)}"
    )
    lookup_labels = [
        "eval_lane",
        "status",
        "gen_ai_request_model",
        "gen_ai_response_model",
    ]
    token_rows = query(
        base_url,
        "sum by (eval_lane, gen_ai_request_model, gen_ai_response_model, gen_ai_token_type) "
        f"(agentgateway_gen_ai_client_token_usage_sum{{{selector}}})",
    )
    lookup_rows = query(
        base_url,
        "sum by (eval_lane, status, gen_ai_request_model, gen_ai_response_model) "
        f"(agentgateway_cost_catalog_lookups_total{{{selector}}})",
    )
    lookups = normalize_vector(lookup_rows, lookup_labels, "lookups")
    non_exact = [row for row in lookups if row.get("status", "").lower() != "exact"]
    if not lookups:
        raise RuntimeError(f"no model catalog lookups found for {evaluation_id}")
    if non_exact:
        raise RuntimeError(f"non-exact model catalog lookups found: {non_exact}")
    observed_lookups = sum(row["lookups"] for row in lookups)
    if observed_lookups < expected_requests:
        raise RuntimeError(
            f"only {observed_lookups:g} of {expected_requests} catalog lookups are available "
            f"for {evaluation_id}"
        )

    lane_costs, model_costs = calculate_costs(token_rows, load_catalog(catalog_path))
    observed_lanes = {row["eval_lane"] for row in lane_costs}
    expected_lanes = set(result_metadata["expected_lanes"])
    if not expected_lanes.issubset(observed_lanes):
        missing = sorted(expected_lanes - observed_lanes)
        raise RuntimeError("cost metrics are missing evaluation lanes: " + ", ".join(missing))
    if sum(row["cost_usd"] for row in lane_costs) <= 0:
        raise RuntimeError(f"catalog-priced token cost is zero for {evaluation_id}")

    return {
        "scope": "evaluation",
        "evaluation_id": evaluation_id,
        "evaluation_started_at": result_metadata["started_at"],
        "evaluation_ended_at": result_metadata["ended_at"],
        "expected_requests": expected_requests,
        "expected_lanes": sorted(expected_lanes),
        "observed_catalog_lookups": observed_lookups,
        "cost_source": "agentgateway token metrics priced with the loaded model catalog",
        "catalog_backed_realized_cost_by_lane": lane_costs,
        "catalog_backed_realized_cost_by_model": model_costs,
        "model_catalog_lookups": lookups,
    }


def render_vector(title, rows, label_order, value_name):
    lines = ["", title]
    if not rows:
        lines.append("  no matching samples")
        return lines
    for row in rows:
        labels = ", ".join(f"{name}={row[name]}" for name in label_order if row.get(name))
        lines.append(f"  {labels or 'total'}: {row[value_name]:.8f}")
    return lines


def render_report(report):
    lane_labels = [
        "eval_lane",
        "gen_ai_request_model",
        "gen_ai_response_model",
        "catalog_model",
    ]
    model_labels = [
        "gen_ai_request_model",
        "gen_ai_response_model",
        "catalog_model",
    ]
    lookup_labels = [
        "eval_lane",
        "status",
        "gen_ai_request_model",
        "gen_ai_response_model",
    ]
    lane_cost = report["catalog_backed_realized_cost_by_lane"]
    lines = [
        f"Scope: {report['scope']}",
        f"Evaluation: {report['evaluation_id']}",
        f"Requests observed: {report['observed_catalog_lookups']:g}/{report['expected_requests']}",
        f"Started: {report['evaluation_started_at']}",
        f"Ended: {report['evaluation_ended_at']}",
        f"Cost source: {report['cost_source']}",
    ]
    lines.extend(render_vector(
        "Catalog-priced realized cost by lane (USD)",
        lane_cost,
        lane_labels,
        "cost_usd",
    ))
    lines.extend(render_vector(
        "Catalog-priced realized cost by model (USD)",
        report["catalog_backed_realized_cost_by_model"],
        model_labels,
        "cost_usd",
    ))
    lines.extend(render_vector(
        "Model catalog lookups",
        report["model_catalog_lookups"],
        lookup_labels,
        "lookups",
    ))
    return "\n".join(lines) + "\n"


def write_json(path, report):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, sort_keys=True)
        stream.write("\n")


def main():
    parser = argparse.ArgumentParser(description="Query agentgateway eval metrics from Prometheus.")
    parser.add_argument("--url", default="http://127.0.0.1:19090")
    parser.add_argument("--evaluation-id", required=True)
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--results", required=True)
    parser.add_argument("--json-output", default="", help="Write the report as JSON.")
    args = parser.parse_args()

    report = build_report(
        args.url,
        args.evaluation_id,
        args.catalog,
        args.results,
    )
    if args.json_output:
        write_json(args.json_output, report)
    print(render_report(report), end="")


if __name__ == "__main__":
    main()
