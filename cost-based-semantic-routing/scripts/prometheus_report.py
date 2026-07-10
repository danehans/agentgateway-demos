#!/usr/bin/env python3
import argparse
import json
import os
import urllib.parse
import urllib.request


def query(base_url, expression):
    url = f"{base_url.rstrip('/')}/api/v1/query?{urllib.parse.urlencode({'query': expression})}"
    with urllib.request.urlopen(url, timeout=20) as response:
        payload = json.load(response)
    if payload.get("status") != "success":
        raise RuntimeError(payload)
    return payload["data"]["result"]


def normalize_vector(rows, label_order, value_name):
    normalized = []
    for row in sorted(rows, key=lambda item: tuple(item["metric"].get(k, "") for k in label_order)):
        item = {
            name: row["metric"][name]
            for name in label_order
            if row["metric"].get(name)
        }
        item[value_name] = float(row["value"][1])
        normalized.append(item)
    return normalized


def render_vector(title, rows, label_order, value_name):
    lines = ["", title]
    if not rows:
        lines.append("  no matching samples")
        return lines
    for row in rows:
        labels = ", ".join(f"{name}={row[name]}" for name in label_order if row.get(name))
        lines.append(f"  {labels or 'total'}: {row[value_name]:.8f}")
    return lines


def build_report(base_url, window):
    selector = 'namespace="agentgateway-system",gateway="agentgateway-proxy"'
    lane_labels = ["eval_lane", "gen_ai_request_model", "gen_ai_response_model"]
    model_labels = ["gen_ai_request_model", "gen_ai_response_model"]
    lookup_labels = ["status", "gen_ai_request_model", "gen_ai_response_model"]
    lane_cost = query(
        base_url,
        "sum by (eval_lane, gen_ai_request_model, gen_ai_response_model) "
        f"(increase(agentgateway_gen_ai_client_cost_usd_total{{{selector}}}[{window}]))",
    )
    model_cost = query(
        base_url,
        "sum by (gen_ai_request_model, gen_ai_response_model) "
        f"(increase(agentgateway_gen_ai_client_cost_usd_total{{{selector}}}[{window}]))",
    )
    lookups = query(
        base_url,
        "sum by (status, gen_ai_request_model, gen_ai_response_model) "
        f"(increase(agentgateway_cost_catalog_lookups_total{{{selector}}}[{window}]))",
    )
    return {
        "window": window,
        "catalog_backed_realized_cost_by_lane": normalize_vector(
            lane_cost, lane_labels, "cost_usd"
        ),
        "catalog_backed_realized_cost_by_model": normalize_vector(
            model_cost, model_labels, "cost_usd"
        ),
        "model_catalog_lookups": normalize_vector(lookups, lookup_labels, "lookups"),
    }


def render_report(report):
    lane_labels = ["eval_lane", "gen_ai_request_model", "gen_ai_response_model"]
    model_labels = ["gen_ai_request_model", "gen_ai_response_model"]
    lookup_labels = ["status", "gen_ai_request_model", "gen_ai_response_model"]
    lane_cost = report["catalog_backed_realized_cost_by_lane"]
    lines = render_vector(
        "Catalog-backed realized cost by lane (USD)",
        lane_cost,
        lane_labels,
        "cost_usd",
    )
    if not lane_cost:
        lines.extend(render_vector(
            "Catalog-backed realized cost by model (USD)",
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
    return "\n".join(lines).lstrip("\n") + "\n"


def write_json(path, report):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, sort_keys=True)
        stream.write("\n")


def main():
    parser = argparse.ArgumentParser(description="Query agentgateway eval metrics from Prometheus.")
    parser.add_argument("--url", default="http://127.0.0.1:19090")
    parser.add_argument("--window", default="30m")
    parser.add_argument("--json-output", default="", help="Write the report as JSON.")
    args = parser.parse_args()

    report = build_report(args.url, args.window)
    if args.json_output:
        write_json(args.json_output, report)
    print(render_report(report), end="")


if __name__ == "__main__":
    main()
