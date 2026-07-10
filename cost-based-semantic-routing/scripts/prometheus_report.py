#!/usr/bin/env python3
import argparse
import json
import urllib.parse
import urllib.request


def query(base_url, expression):
    url = f"{base_url.rstrip('/')}/api/v1/query?{urllib.parse.urlencode({'query': expression})}"
    with urllib.request.urlopen(url, timeout=20) as response:
        payload = json.load(response)
    if payload.get("status") != "success":
        raise RuntimeError(payload)
    return payload["data"]["result"]


def print_vector(title, rows, label_order):
    print(f"\n{title}")
    if not rows:
        print("  no matching samples")
        return
    for row in sorted(rows, key=lambda item: tuple(item["metric"].get(k, "") for k in label_order)):
        labels = ", ".join(
            f"{name}={row['metric'][name]}" for name in label_order if row["metric"].get(name)
        )
        print(f"  {labels or 'total'}: {float(row['value'][1]):.8f}")


def main():
    parser = argparse.ArgumentParser(description="Query agentgateway eval metrics from Prometheus.")
    parser.add_argument("--url", default="http://127.0.0.1:19090")
    parser.add_argument("--window", default="30m")
    args = parser.parse_args()

    selector = 'namespace="agentgateway-system",gateway="agentgateway-proxy"'
    lane_cost = query(
        args.url,
        "sum by (eval_lane, gen_ai_request_model, gen_ai_response_model) "
        f"(increase(agentgateway_gen_ai_client_cost_usd_total{{{selector}}}[{args.window}]))",
    )
    model_cost = query(
        args.url,
        "sum by (gen_ai_request_model, gen_ai_response_model) "
        f"(increase(agentgateway_gen_ai_client_cost_usd_total{{{selector}}}[{args.window}]))",
    )
    lookups = query(
        args.url,
        "sum by (status, gen_ai_request_model, gen_ai_response_model) "
        f"(increase(agentgateway_cost_catalog_lookups_total{{{selector}}}[{args.window}]))",
    )

    print_vector(
        "Catalog-backed realized cost by lane (USD)",
        lane_cost,
        ["eval_lane", "gen_ai_request_model", "gen_ai_response_model"],
    )
    if not lane_cost:
        print_vector(
            "Catalog-backed realized cost by model (USD)",
            model_cost,
            ["gen_ai_request_model", "gen_ai_response_model"],
        )
    print_vector(
        "Model catalog lookups",
        lookups,
        ["status", "gen_ai_request_model", "gen_ai_response_model"],
    )


if __name__ == "__main__":
    main()
