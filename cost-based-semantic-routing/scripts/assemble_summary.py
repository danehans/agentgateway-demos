#!/usr/bin/env python3
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def load_json(path):
    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream)


def read_run_id(path):
    with Path(path).open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                return json.loads(line).get("run_id") or Path(path).stem
    raise ValueError(f"result file is empty: {path}")


def build_summary(args):
    prometheus = {
        "status": args.prometheus_status,
        "reason": args.prometheus_reason or None,
        "report": None,
    }
    if args.prometheus_json:
        prometheus["report"] = load_json(args.prometheus_json)

    return {
        "schema_version": 1,
        "run_id": read_run_id(args.results),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "result_file": Path(args.results).name,
        "local": load_json(args.local_json),
        "prometheus": prometheus,
    }


def render_summary(summary, local_text, prometheus_text):
    prometheus = summary["prometheus"]
    prometheus_heading = "Catalog-backed Prometheus summary (experiment-scoped)"
    lines = [
        "Semantic routing experiment summary",
        f"run_id={summary['run_id']}",
        f"result_file={summary['result_file']}",
        f"generated_at={summary['generated_at']}",
        "",
        "Local evaluation summary",
        local_text.strip(),
        "",
        prometheus_heading,
    ]
    if prometheus["status"] == "collected":
        lines.append(prometheus_text.strip())
    else:
        message = f"status={prometheus['status']}"
        if prometheus["reason"]:
            message += f" reason={prometheus['reason']}"
        lines.append(message)
    return "\n".join(lines) + "\n"


def write_json(path, value):
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")


def write_text(path, value):
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(value, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Assemble persisted semantic routing summaries.")
    parser.add_argument("--results", required=True)
    parser.add_argument("--local-json", required=True)
    parser.add_argument("--local-text", required=True)
    parser.add_argument("--prometheus-json", default="")
    parser.add_argument("--prometheus-text", default="")
    parser.add_argument(
        "--prometheus-status",
        required=True,
        choices=("collected", "disabled", "unavailable"),
    )
    parser.add_argument("--prometheus-reason", default="")
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-text", required=True)
    args = parser.parse_args()

    local_text = Path(args.local_text).read_text(encoding="utf-8")
    prometheus_text = ""
    if args.prometheus_text:
        prometheus_text = Path(args.prometheus_text).read_text(encoding="utf-8")
    summary = build_summary(args)
    write_json(args.output_json, summary)
    write_text(args.output_text, render_summary(summary, local_text, prometheus_text))


if __name__ == "__main__":
    main()
