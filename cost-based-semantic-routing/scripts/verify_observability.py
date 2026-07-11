#!/usr/bin/env python3
import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from corpus import expected_models


def get_json(base_url, path, params=None):
    url = f"{base_url.rstrip('/')}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def result_values(result):
    values = []
    for item in result if isinstance(result, list) else []:
        if isinstance(item, dict) and isinstance(item.get("value"), list):
            values.append(float(item["value"][1]))
        elif isinstance(item, dict) and isinstance(item.get("values"), list):
            values.extend(float(value[1]) for value in item["values"])
    if isinstance(result, list) and len(result) == 2 and not isinstance(result[0], dict):
        values.append(float(result[1]))
    return values


def verify_prometheus(args):
    payload = get_json(args.url, "/api/v1/query", {"query": args.query})
    if payload.get("status") != "success":
        raise ValueError(f"Prometheus query failed: {payload}")
    result = payload.get("data", {}).get("result", [])
    series = 1 if payload.get("data", {}).get("resultType") == "scalar" else len(result)
    values = result_values(result)
    if series < args.min_series:
        raise ValueError(f"expected at least {args.min_series} series, found {series}")
    if args.min_value is not None and (not values or sum(values) < args.min_value):
        raise ValueError(
            f"expected a total value of at least {args.min_value}, found {sum(values):g}"
        )
    print(f"Prometheus check passed: series={series} total={sum(values):g}")


def json_contains(payload, expected):
    serialized = json.dumps(payload, ensure_ascii=False)
    return all(value in serialized for value in expected)


def verify_models(args):
    payload = get_json(args.url, "/config/router")
    expected = expected_models(args.corpus)
    missing = [model for model in expected if model and not json_contains(payload, [model])]
    if missing:
        raise ValueError("model API is missing: " + ", ".join(missing))
    print("Router model configuration check passed: " + ", ".join(expected))


def verify_loki(args):
    payload = get_json(
        args.url,
        "/loki/api/v1/query_range",
        {
            "query": args.query,
            "since": args.since,
            "limit": args.limit,
            "direction": "backward",
        },
    )
    if payload.get("status") != "success":
        raise ValueError(f"Loki query failed: {payload}")
    streams = payload.get("data", {}).get("result", [])
    if not streams:
        raise ValueError("Loki query returned no access logs")
    if not json_contains(payload, args.contains):
        raise ValueError(f"Loki access logs do not contain: {', '.join(args.contains)}")
    print(f"Loki check passed: streams={len(streams)}")


def verify_tempo(args):
    params = {"limit": args.limit}
    if args.tags:
        params["tags"] = args.tags
    payload = get_json(args.url, "/api/search", params)
    traces = payload.get("traces", [])
    if not traces:
        raise ValueError("Tempo search returned no traces")

    for item in traces:
        trace_id = item.get("traceID") or item.get("traceId")
        if not trace_id:
            continue
        try:
            trace = get_json(args.url, f"/api/traces/{trace_id}")
        except Exception:
            continue
        if json_contains(trace, args.contains):
            print(f"Tempo check passed: trace_id={trace_id}")
            return
    raise ValueError(f"Tempo traces do not contain: {', '.join(args.contains)}")


def main():
    parser = argparse.ArgumentParser(description="Verify semantic-routing observability signals.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prometheus = subparsers.add_parser("prometheus")
    prometheus.add_argument("--url", required=True)
    prometheus.add_argument("--query", required=True)
    prometheus.add_argument("--min-series", type=int, default=1)
    prometheus.add_argument("--min-value", type=float)
    prometheus.set_defaults(handler=verify_prometheus)

    models = subparsers.add_parser("models")
    models.add_argument("--url", required=True)
    models.add_argument("--corpus", required=True)
    models.set_defaults(handler=verify_models)

    loki = subparsers.add_parser("loki")
    loki.add_argument("--url", required=True)
    loki.add_argument("--query", default='{service_name=~".+"}')
    loki.add_argument("--since", default="15m")
    loki.add_argument("--limit", type=int, default=1000)
    loki.add_argument("--contains", action="append", required=True)
    loki.set_defaults(handler=verify_loki)

    tempo = subparsers.add_parser("tempo")
    tempo.add_argument("--url", required=True)
    tempo.add_argument("--tags", default="service.name=agentgateway-proxy")
    tempo.add_argument("--limit", type=int, default=100)
    tempo.add_argument("--contains", action="append", required=True)
    tempo.set_defaults(handler=verify_tempo)

    args = parser.parse_args()
    try:
        args.handler(args)
    except Exception as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
