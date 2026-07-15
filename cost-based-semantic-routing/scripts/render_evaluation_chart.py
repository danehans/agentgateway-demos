#!/usr/bin/env python3
"""Render a self-contained SVG chart from one semantic-routing summary."""

import argparse
import json
from html import escape
from pathlib import Path


LANES = (
    ("routed", "Semantic routing", "#0f766e"),
    ("always_expensive", "Always expensive", "#475569"),
)


def load_summary(path):
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def chart_output_path(summary_path):
    stem = summary_path.stem
    if stem.endswith("-summary"):
        stem = stem[:-len("-summary")]
    return summary_path.with_name(f"{stem}-chart.svg")


def cost_data(summary):
    prometheus = summary.get("prometheus", {})
    report = prometheus.get("report", {})
    if prometheus.get("status") == "collected":
        costs = {}
        for row in report.get("catalog_backed_realized_cost_by_lane", []):
            lane = row.get("eval_lane")
            if lane:
                costs[lane] = costs.get(lane, 0.0) + float(row["cost_usd"])
        if all(lane in costs for lane, _, _ in LANES):
            return costs, "Catalog-priced agentgateway metrics"

    lanes = summary.get("local", {}).get("lanes", {})
    costs = {
        lane: float(lanes[lane]["cost_estimate_usd"])
        for lane, _, _ in LANES
        if lane in lanes and "cost_estimate_usd" in lanes[lane]
    }
    if not all(lane in costs for lane, _, _ in LANES):
        raise ValueError("summary does not contain costs for routed and always-expensive lanes")
    return costs, "Local token-cost estimate"


def metric_data(summary):
    local = summary.get("local", {})
    lanes = local.get("lanes", {})
    if not all(lane in lanes for lane, _, _ in LANES):
        raise ValueError("summary does not contain routed and always-expensive latency")
    return {
        "routing": local.get("routing"),
        "model_mix": local.get("routed_model_mix"),
        "routed_p50": float(lanes["routed"]["latency_ms"]["p50"]) / 1000,
        "routed_p95": float(lanes["routed"]["latency_ms"]["p95"]) / 1000,
        "expensive_p50": float(lanes["always_expensive"]["latency_ms"]["p50"]) / 1000,
        "expensive_p95": float(lanes["always_expensive"]["latency_ms"]["p95"]) / 1000,
    }


def text(value):
    return escape(str(value), quote=True)


def routing_metrics(routing):
    if not routing:
        return "Unavailable", "No successful routed requests", 0.0
    escalation = routing.get("complex_prompt_escalation", {})
    fraction = escalation.get("fraction")
    if fraction is None:
        detail = "No complex prompts in this sample"
        fraction = 0.0
    else:
        detail = (
            f"{escalation['selected_expensive']}/{escalation['expected']} "
            "complex prompts sent to GPT-5.5"
        )
    return f"{routing['accuracy']:.1%}", detail, max(0, min(1, fraction))


def model_mix_detail(model_mix):
    if not model_mix:
        return "No successful routed requests", 0.0
    models = model_mix.get("models", [])
    if not models:
        return "No selected models recorded", 0.0
    parts = [f"{item['requests']} {item['model']}" for item in models]
    cheap_fraction = next(
        (item["fraction"] for item in models if item["model"] == "gpt-5.4-nano"),
        0.0,
    )
    return " | ".join(parts), cheap_fraction


def cache_transition_data(summary):
    transitions = summary.get("local", {}).get("cache_transitions", [])
    total = sum(int(item.get("requests", 0)) for item in transitions)
    switches = [item for item in transitions if item.get("model_switch")]
    switch_count = sum(int(item.get("requests", 0)) for item in switches)
    cached_tokens = sum(int(item.get("cached_input_tokens", 0)) for item in transitions)
    if not transitions:
        return {
            "total": 0,
            "switches": 0,
            "cached_tokens": 0,
            "cache_read_lines": ["No ordered multi-turn transitions in this run"],
        }
    cache_reads = []
    for item in transitions:
        cached = int(item.get("cached_input_tokens", 0))
        if not cached:
            continue
        previous, selected = item["transition"].split("->", maxsplit=1)
        previous = previous.removeprefix("gpt-").replace("5.4-nano", "nano")
        selected = selected.removeprefix("gpt-").replace("5.4-nano", "nano")
        lane = item.get("lane", "routed").replace("always_", "").replace("_", " ")
        cache_reads.append((
            cached,
            f"{lane}: {previous} -> {selected}, {cached:,} ({item['requests']})",
        ))
    cache_reads.sort(key=lambda item: (-item[0], item[1]))
    return {
        "total": total,
        "switches": switch_count,
        "cached_tokens": cached_tokens,
        "cache_read_lines": [item[1] for item in cache_reads]
        or ["No provider cache reads observed"],
    }


def render_chart(summary):
    costs, cost_source = cost_data(summary)
    metrics = metric_data(summary)
    routed_cost = costs["routed"]
    expensive_cost = costs["always_expensive"]
    savings = 0.0 if expensive_cost == 0 else 1 - routed_cost / expensive_cost
    p50_change = 0.0 if metrics["expensive_p50"] == 0 else (
        metrics["routed_p50"] / metrics["expensive_p50"] - 1
    )
    agreement, escalation_detail, escalation_fraction = routing_metrics(metrics["routing"])
    mix_detail, cheap_fraction = model_mix_detail(metrics["model_mix"])
    cache_transitions = cache_transition_data(summary)
    run_id = summary.get("run_id", "semantic-routing-evaluation")
    max_cost = max(costs.values())
    cache_read_rows = "".join(
        f'<text class="cache-detail" x="673" y="{580 + index * 19}">{text(line)}</text>'
        for index, line in enumerate(cache_transitions["cache_read_lines"])
    )
    cache_section_bottom = max(613, 580 + (len(cache_transitions["cache_read_lines"]) - 1) * 19)
    footer_y = cache_section_bottom + 26
    chart_height = footer_y + 22

    bars = []
    for index, (lane, label, color) in enumerate(LANES):
        y = 252 + index * 45
        bar_width = 8 if max_cost == 0 else max(8, 420 * costs[lane] / max_cost)
        bars.extend((
            f'<text class="lane" x="48" y="{y + 17}">{text(label)}</text>',
            f'<rect x="225" y="{y}" width="420" height="24" fill="#e2e8f0"/>',
            f'<rect x="225" y="{y}" width="{bar_width:.1f}" height="24" fill="{color}"/>',
            f'<text class="value" x="665" y="{y + 17}">${costs[lane]:.4f}</text>',
        ))

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="960" height="{chart_height}" viewBox="0 0 960 {chart_height}" role="img" aria-labelledby="title description">
  <title id="title">Semantic routing evaluation results</title>
  <desc id="description">Semantic routing spend, routing agreement, model mix, latency, and cache transitions for run {text(run_id)}.</desc>
  <style>
    text {{ font-family: Arial, Helvetica, sans-serif; fill: #0f172a; }}
    .title {{ font-size: 26px; font-weight: 700; }}
    .subtitle, .note {{ font-size: 13px; fill: #475569; }}
    .kpi-label, .section {{ font-size: 12px; font-weight: 700; fill: #475569; letter-spacing: .8px; }}
    .kpi-value {{ font-size: 25px; font-weight: 700; }}
    .lane {{ font-size: 14px; font-weight: 600; }}
    .value {{ font-size: 14px; font-weight: 700; }}
    .metric {{ font-size: 18px; font-weight: 700; }}
    .metric-detail {{ font-size: 13px; fill: #475569; }}
    .cache-detail {{ font-size: 12px; fill: #475569; }}
  </style>
  <rect width="960" height="{chart_height}" fill="#ffffff"/>
  <text class="title" x="48" y="46">Semantic routing evaluation</text>
  <text class="subtitle" x="48" y="70">Run {text(run_id)} | {text(cost_source)}</text>
  <line x1="48" y1="91" x2="912" y2="91" stroke="#cbd5e1"/>

  <text class="kpi-label" x="48" y="121">SPEND REDUCTION</text>
  <text class="kpi-value" x="48" y="151">{savings:.1%}</text>
  <text class="subtitle" x="48" y="173">Routed versus always expensive</text>
  <line x1="337" y1="112" x2="337" y2="177" stroke="#cbd5e1"/>
  <text class="kpi-label" x="370" y="121">ROUTING AGREEMENT</text>
  <text class="kpi-value" x="370" y="151">{text(agreement)}</text>
  <text class="subtitle" x="370" y="173">Expected tiers in this coding sample</text>
  <line x1="655" y1="112" x2="655" y2="177" stroke="#cbd5e1"/>
  <text class="kpi-label" x="688" y="121">ROUTED P50 LATENCY</text>
  <text class="kpi-value" x="688" y="151">{metrics['routed_p50']:.2f} s</text>
  <text class="subtitle" x="688" y="173">{p50_change:+.1%} versus always expensive</text>

  <text class="section" x="48" y="218">COST PER DEMO RUN (USD)</text>
  {''.join(bars)}
  <line x1="48" y1="360" x2="912" y2="360" stroke="#cbd5e1"/>

  <text class="section" x="48" y="395">ROUTED MODEL MIX</text>
  <rect x="48" y="410" width="230" height="18" fill="#475569"/>
  <rect x="48" y="410" width="{230 * cheap_fraction:.1f}" height="18" fill="#0f766e"/>
  <text class="metric" x="48" y="460">{cheap_fraction:.0%} lower-cost</text>
  <text class="metric-detail" x="48" y="481">{text(mix_detail)}</text>

  <line x1="337" y1="386" x2="337" y2="505" stroke="#cbd5e1"/>
  <text class="section" x="370" y="395">COMPLEX-PROMPT ESCALATION</text>
  <rect x="370" y="410" width="230" height="18" fill="#e2e8f0"/>
  <rect x="370" y="410" width="{230 * escalation_fraction:.1f}" height="18" fill="#0f766e"/>
  <text class="metric" x="370" y="460">{escalation_fraction:.0%} to GPT-5.5</text>
  <text class="metric-detail" x="370" y="481">{text(escalation_detail)}</text>

  <line x1="655" y1="386" x2="655" y2="505" stroke="#cbd5e1"/>
  <text class="section" x="688" y="395">END-TO-END LATENCY</text>
  <text class="metric" x="688" y="429">{metrics['routed_p50']:.2f} s p50</text>
  <text class="metric-detail" x="688" y="454">{metrics['routed_p95']:.2f} s p95 semantic routing</text>
  <text class="metric-detail" x="688" y="481">Always expensive: {metrics['expensive_p50']:.2f} s p50, {metrics['expensive_p95']:.2f} s p95</text>

  <line x1="48" y1="515" x2="912" y2="515" stroke="#cbd5e1"/>
  <text class="section" x="48" y="550">CONVERSATION CACHE TRANSITIONS</text>
  <text class="metric" x="48" y="584">{cache_transitions['switches']} switches</text>
  <text class="metric-detail" x="48" y="607">{cache_transitions['total']} ordered continuation turns</text>

  <line x1="337" y1="541" x2="337" y2="{cache_section_bottom + 6}" stroke="#cbd5e1"/>
  <text class="section" x="370" y="550">PROVIDER CACHE READS</text>
  <text class="metric" x="370" y="584">{cache_transitions['cached_tokens']:,} tokens</text>

  <line x1="655" y1="541" x2="655" y2="{cache_section_bottom + 6}" stroke="#cbd5e1"/>
  <text class="section" x="673" y="550">CACHE READS BY TRANSITION</text>
  {cache_read_rows}

  <text class="note" x="48" y="{footer_y}">Agreement uses expected tiers in the checked-in coding sample; cache tokens are reported by the upstream provider.</text>
</svg>
'''


def write_chart(summary_path, output_path):
    chart = render_chart(load_summary(summary_path))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(chart, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(
        description="Render an SVG chart from a semantic-routing summary JSON file."
    )
    parser.add_argument("summary", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output_path = args.output or chart_output_path(args.summary)
    write_chart(args.summary, output_path)
    print(output_path)


if __name__ == "__main__":
    main()
