import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace


DEMO_DIR = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


assemble_summary = load_module(
    "assemble_summary", DEMO_DIR / "scripts" / "assemble_summary.py"
)
prometheus_report = load_module(
    "prometheus_report", DEMO_DIR / "scripts" / "prometheus_report.py"
)
verify_observability = load_module(
    "verify_observability", DEMO_DIR / "scripts" / "verify_observability.py"
)
render_evaluation_chart = load_module(
    "render_evaluation_chart",
    DEMO_DIR / "scripts" / "render_evaluation_chart.py",
)
run_eval = load_module("run_eval", DEMO_DIR / "scripts" / "run_eval.py")
dataset = load_module("dataset", DEMO_DIR / "scripts" / "dataset.py")
summarize_results = load_module(
    "summarize_results", DEMO_DIR / "scripts" / "summarize_results.py"
)


class PrometheusReportTest(unittest.TestCase):
    def test_builds_structured_report(self):
        def fake_query(_base_url, expression):
            if "cost_catalog_lookups" in expression:
                return [{
                    "metric": {
                        "eval_lane": "always_expensive",
                        "status": "Exact",
                        "gen_ai_request_model": "gpt-5.5",
                        "gen_ai_response_model": "gpt-5.5-2026-07-01",
                    },
                    "value": [0, "3"],
                }]
            if "token_usage" in expression:
                return [
                    {
                        "metric": {
                            "eval_lane": lane,
                            "gen_ai_request_model": model,
                            "gen_ai_response_model": response_model,
                            "gen_ai_token_type": token_type,
                        },
                        "value": [0, value],
                    }
                    for lane, model, response_model in (
                        ("routed", "gpt-5.4-nano", "gpt-5.4-nano-2026-07-01"),
                        ("always_expensive", "gpt-5.5", "gpt-5.5-2026-07-01"),
                    )
                    for token_type, value in (("input", "100"), ("output", "10"))
                ]
            return []

        catalog = {
            "providers": {
                "openai": {
                    "models": {
                        "gpt-5.4-nano": {
                            "rates": {"input": "0.2", "output": "1.25"}
                        },
                        "gpt-5.5": {
                            "rates": {"input": "5", "output": "30"}
                        },
                    }
                }
            }
        }
        original_query = prometheus_report.query
        prometheus_report.query = fake_query
        self.addCleanup(setattr, prometheus_report, "query", original_query)

        with tempfile.TemporaryDirectory() as directory:
            catalog_path = Path(directory) / "catalog.json"
            results_path = Path(directory) / "results.jsonl"
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            results_path.write_text(
                "\n".join(
                    json.dumps({
                        "run_id": "test-run",
                        "timestamp": f"2026-07-10T19:20:0{second}+00:00",
                        "latency_ms": 1000,
                        "lane": lane,
                    })
                    for second, lane in ((1, "routed"), (2, "always_expensive"))
                ) + "\n",
                encoding="utf-8",
            )
            report = prometheus_report.build_report(
                "http://prometheus", "test-run", catalog_path, results_path
            )

        self.assertEqual(report["scope"], "evaluation")
        self.assertEqual(report["evaluation_id"], "test-run")
        self.assertEqual(report["expected_requests"], 2)
        self.assertEqual(report["observed_catalog_lookups"], 3)
        self.assertEqual(
            report["evaluation_started_at"], "2026-07-10T19:20:00+00:00"
        )
        self.assertEqual(report["evaluation_ended_at"], "2026-07-10T19:20:02+00:00")
        self.assertNotIn("window", report)
        expensive = next(
            row
            for row in report["catalog_backed_realized_cost_by_lane"]
            if row["eval_lane"] == "always_expensive"
        )
        self.assertAlmostEqual(expensive["cost_usd"], 0.0008)
        self.assertEqual(report["model_catalog_lookups"][0]["lookups"], 3.0)
        self.assertIn("always_expensive", prometheus_report.render_report(report))

    def test_rejects_missing_evaluation_lanes(self):
        def fake_query(_base_url, expression):
            if "cost_catalog_lookups" in expression:
                return [{
                    "metric": {
                        "eval_lane": "routed",
                        "status": "Exact",
                        "gen_ai_request_model": "gpt-5.5",
                        "gen_ai_response_model": "gpt-5.5-2026-07-01",
                    },
                    "value": [0, "2"],
                }]
            return [{
                "metric": {
                    "eval_lane": "routed",
                    "gen_ai_request_model": "gpt-5.5",
                    "gen_ai_response_model": "gpt-5.5-2026-07-01",
                    "gen_ai_token_type": "input",
                },
                "value": [0, "100"],
            }]

        original_query = prometheus_report.query
        prometheus_report.query = fake_query
        self.addCleanup(setattr, prometheus_report, "query", original_query)
        with tempfile.TemporaryDirectory() as directory:
            catalog_path = Path(directory) / "catalog.json"
            results_path = Path(directory) / "results.jsonl"
            catalog_path.write_text(json.dumps({
                "providers": {"openai": {"models": {
                    "gpt-5.5": {"rates": {"input": "5"}}
                }}}
            }), encoding="utf-8")
            results_path.write_text(json.dumps({
                "run_id": "test-run",
                "timestamp": "2026-07-10T19:20:01+00:00",
                "latency_ms": 500,
                "lane": "routed",
            }) + "\n" + json.dumps({
                "run_id": "test-run",
                "timestamp": "2026-07-10T19:20:02+00:00",
                "latency_ms": 500,
                "lane": "always_expensive",
            }) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "missing evaluation lanes"):
                prometheus_report.build_report(
                    "http://prometheus", "test-run", catalog_path, results_path
                )

    def test_rejects_result_rows_from_another_evaluation(self):
        with tempfile.TemporaryDirectory() as directory:
            results_path = Path(directory) / "results.jsonl"
            results_path.write_text(json.dumps({
                "run_id": "another-run",
                "timestamp": "2026-07-10T19:20:01+00:00",
                "latency_ms": 500,
            }) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "do not match evaluation"):
                prometheus_report.load_result_metadata(results_path, "test-run")


class AssembleSummaryTest(unittest.TestCase):
    def test_writes_one_run_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = root / "run.jsonl"
            local_json = root / "local.json"
            local_text = root / "local.txt"
            prometheus_json = root / "prometheus.json"
            prometheus_text = root / "prometheus.txt"
            results.write_text(json.dumps({"run_id": "test-run"}) + "\n", encoding="utf-8")
            local_json.write_text(json.dumps({"routing": {"accuracy": 0.9}}), encoding="utf-8")
            local_text.write_text("Routing accuracy: 90.0%\n", encoding="utf-8")
            prometheus_json.write_text(
                json.dumps({"scope": "evaluation"}), encoding="utf-8"
            )
            prometheus_text.write_text("Catalog cost: 0.12500000\n", encoding="utf-8")
            args = SimpleNamespace(
                results=str(results),
                local_json=str(local_json),
                prometheus_json=str(prometheus_json),
                prometheus_status="collected",
                prometheus_reason="",
            )

            summary = assemble_summary.build_summary(args)
            rendered = assemble_summary.render_summary(
                summary,
                local_text.read_text(encoding="utf-8"),
                prometheus_text.read_text(encoding="utf-8"),
            )

            self.assertEqual(summary["run_id"], "test-run")
            self.assertEqual(summary["prometheus"]["status"], "collected")
            self.assertIn(
                "Catalog-backed Prometheus summary (evaluation-scoped)", rendered
            )
            self.assertIn("Routing accuracy: 90.0%", rendered)


class RenderEvaluationChartTest(unittest.TestCase):
    def test_uses_catalog_priced_costs_and_renders_key_metrics(self):
        summary = {
            "run_id": "test-run",
            "local": {
                "lanes": {
                    "routed": {
                        "cost_estimate_usd": 0.70,
                        "latency_ms": {"p50": 2500, "p95": 4000},
                    },
                    "always_expensive": {
                        "cost_estimate_usd": 1.00,
                        "latency_ms": {"p50": 2000, "p95": 3500},
                    },
                },
                "routing": {
                    "accuracy": 0.9,
                    "complex_prompt_escalation": {
                        "expected": 10,
                        "selected_expensive": 8,
                        "fraction": 0.8,
                    },
                },
                "routed_model_mix": {
                    "total": 20,
                    "models": [
                        {"model": "gpt-5.4-nano", "requests": 12, "fraction": 0.6},
                        {"model": "gpt-5.5", "requests": 8, "fraction": 0.4},
                    ],
                },
            },
            "prometheus": {
                "status": "collected",
                "report": {
                    "catalog_backed_realized_cost_by_lane": [
                        {"eval_lane": "routed", "cost_usd": 0.25},
                        {"eval_lane": "routed", "cost_usd": 0.35},
                        {"eval_lane": "always_expensive", "cost_usd": 1.00},
                    ]
                },
            },
        }

        costs, source = render_evaluation_chart.cost_data(summary)
        chart = render_evaluation_chart.render_chart(summary)

        self.assertEqual(source, "Catalog-priced agentgateway metrics")
        self.assertAlmostEqual(costs["routed"], 0.60)
        self.assertIn("40.0%", chart)
        self.assertIn("90.0%", chart)
        self.assertIn("8/10 complex prompts sent to GPT-5.5", chart)
        self.assertIn("12 gpt-5.4-nano | 8 gpt-5.5", chart)
        self.assertIn("2.50 s p50", chart)
        self.assertIn("Catalog-priced agentgateway metrics", chart)

    def test_falls_back_to_local_costs_and_uses_run_chart_name(self):
        summary = {
            "local": {
                "lanes": {
                    lane: {
                        "cost_estimate_usd": cost,
                        "latency_ms": {"p50": 1000, "p95": 2000},
                    }
                    for lane, cost in (
                        ("routed", 0.50),
                        ("always_expensive", 1.00),
                    )
                },
            },
            "prometheus": {"status": "disabled"},
        }

        costs, source = render_evaluation_chart.cost_data(summary)

        self.assertEqual(source, "Local token-cost estimate")
        self.assertAlmostEqual(costs["routed"], 0.50)
        self.assertEqual(
            render_evaluation_chart.chart_output_path(Path("run-summary.json")),
            Path("run-chart.svg"),
        )


class EvaluationToolingTest(unittest.TestCase):
    def setUp(self):
        self.catalog = {
            "providers": {
                "openai": {
                    "models": {
                        "gpt-cheap": {"rates": {"input": "1", "cacheRead": "0.5", "output": "2"}},
                        "gpt-expensive": {"rates": {"input": "5", "cacheRead": "2.5", "output": "10"}},
                    }
                }
            }
        }

    def test_evaluator_uses_the_generated_catalog_for_model_versions(self):
        usage = {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 10}

        cost = run_eval.estimate_cost(
            self.catalog, "gpt-cheap", "gpt-cheap-2026-07-01", usage
        )

        self.assertAlmostEqual(cost, 0.00011)
        self.assertEqual(
            run_eval.canonical_model(self.catalog, "gpt-cheap-2026-07-01"),
            "gpt-cheap",
        )

    def test_evaluator_prices_cache_read_and_write_tokens(self):
        catalog = {
            "providers": {"openai": {"models": {
                "gpt-cheap": {"rates": {
                    "input": "1", "cacheRead": "0.5", "cacheWrite": "1.25", "output": "2",
                }},
            }}}
        }
        usage = {
            "input_tokens": 100,
            "cached_input_tokens": 20,
            "cache_write_tokens": 30,
            "output_tokens": 10,
        }

        components = run_eval.estimate_cost_components(
            catalog, "gpt-cheap", "gpt-cheap", usage
        )

        self.assertAlmostEqual(components["uncached_input"], 0.00005)
        self.assertAlmostEqual(components["cache_read"], 0.00001)
        self.assertAlmostEqual(components["cache_write"], 0.0000375)
        self.assertAlmostEqual(components["output"], 0.00002)

    def test_sequential_jobs_preserve_conversation_turn_order(self):
        items = [
            {"id": "b-2", "conversation_id": "b", "turn": 2},
            {"id": "a-1", "conversation_id": "a", "turn": 1},
            {"id": "b-1", "conversation_id": "b", "turn": 1},
            {"id": "a-2", "conversation_id": "a", "turn": 2},
        ]

        jobs = run_eval.evaluation_jobs(
            items, ["routed", "always_expensive"], True, seed=7
        )

        self.assertEqual(
            [(item["id"], lane) for item, lane in jobs],
            [
                ("b-1", "routed"), ("b-2", "routed"),
                ("a-1", "routed"), ("a-2", "routed"),
                ("b-1", "always_expensive"), ("b-2", "always_expensive"),
                ("a-1", "always_expensive"), ("a-2", "always_expensive"),
            ],
        )

    def test_default_dataset_has_expected_model_mix(self):
        dataset_path = DEMO_DIR / "data" / "demo-dataset.jsonl"
        rows = dataset.load_dataset(dataset_path)

        self.assertEqual(len({row["id"] for row in rows}), len(rows))
        self.assertEqual(len(rows), 24)
        self.assertEqual(
            sum(row["expected_model"] == "gpt-5.4-nano" for row in rows), 12
        )
        self.assertEqual(
            sum(row["expected_model"] == "gpt-5.5" for row in rows), 12
        )
        self.assertEqual(
            {row["language"] for row in rows}, {"go", "rust"}
        )
        self.assertEqual(sum(row["language"] == "go" for row in rows), 12)
        self.assertEqual(sum(row["language"] == "rust" for row in rows), 12)
        self.assertEqual({row["max_tokens"] for row in rows if "routine" in row["family"]}, {256})
        self.assertEqual({row["max_tokens"] for row in rows if "advanced" in row["family"]}, {1024})
        for row in rows:
            self.assertEqual(row["messages"][-1]["role"], "user")
            self.assertEqual(len(row["messages"]), 1)

    def test_evaluator_preserves_dataset_history(self):
        item = {
            "id": "conversation-turn-2",
            "messages": [
                {"role": "user", "content": "Initial request"},
                {"role": "assistant", "content": "Initial response"},
                {"role": "user", "content": "Follow-up request"},
            ],
        }

        messages = run_eval.request_messages(
            SimpleNamespace(system_prompt="System instructions"), item
        )

        self.assertEqual(messages[0], {"role": "system", "content": "System instructions"})
        self.assertEqual(messages[1:], item["messages"])

    def test_summary_uses_catalog_pricing_for_counterfactual(self):
        rows = [
            {
                "lane": "routed", "ok": True, "routing_correct": True,
                "expected_model": "gpt-cheap", "selected_model": "gpt-cheap",
                "cost_estimate_usd": 0.0001, "latency_ms": 1000,
                "usage": {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 10},
            },
            {
                "lane": "always_expensive", "ok": True,
                "cost_estimate_usd": 0.0006, "latency_ms": 2000,
                "usage": {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 10},
            },
        ]

        summary = summarize_results.build_summary(
            rows, self.catalog, "gpt-expensive"
        )

        self.assertEqual(summary["routing"]["accuracy"], 1.0)
        self.assertAlmostEqual(
            summary["savings"]["counterfactual_on_routed_tokens"]["always_expensive_cost_usd"],
            0.0006,
        )

    def test_summary_groups_cache_transitions(self):
        rows = [
            {
                "lane": "routed", "ok": True,
                "previous_selected_model": "gpt-cheap",
                "selected_model": "gpt-expensive-2026-07-01",
                "usage": {"input_tokens": 100, "cached_input_tokens": 20, "cache_write_tokens": 0},
                "cost_components_usd": {"uncached_input": 0.0004, "cache_read": 0.00005},
            },
        ]

        transitions = summarize_results.cache_transition_summary(rows, self.catalog)

        self.assertEqual(transitions[0]["transition"], "gpt-cheap->gpt-expensive")
        self.assertTrue(transitions[0]["model_switch"])
        self.assertEqual(transitions[0]["cached_input_tokens"], 20)

class VerifyObservabilityTest(unittest.TestCase):
    def test_extracts_prometheus_values(self):
        vector = [{"metric": {}, "value": [0, "2.5"]}]
        scalar = [0, "3"]

        self.assertEqual(verify_observability.result_values(vector), [2.5])
        self.assertEqual(verify_observability.result_values(scalar), [3.0])

    def test_finds_correlated_signal_values(self):
        payload = {
            "resource": {"service.name": "agentgateway-proxy"},
            "attributes": {"evaluation.id": "test-run"},
        }

        self.assertTrue(
            verify_observability.json_contains(
                payload, ["agentgateway-proxy", "test-run"]
            )
        )
        self.assertFalse(verify_observability.json_contains(payload, ["missing-run"]))

    def test_verifies_every_dataset_model(self):
        original_get_json = verify_observability.get_json
        verify_observability.get_json = lambda *_args, **_kwargs: {
            "data": [{"id": "gpt-cheap"}, {"id": "gpt-expensive"}]
        }
        self.addCleanup(setattr, verify_observability, "get_json", original_get_json)
        with tempfile.TemporaryDirectory() as directory:
            dataset = Path(directory) / "dataset.jsonl"
            dataset.write_text(
                json.dumps({
                    "id": "test-conversation",
                    "language": "go",
                    "turns": [
                        {
                            "family": "routine_go",
                            "expected_model": "gpt-cheap",
                            "max_tokens": 100,
                            "user": "Initial request",
                            "assistant_context": "Initial response",
                        },
                        {
                            "family": "advanced_go",
                            "expected_model": "gpt-expensive",
                            "max_tokens": 100,
                            "user": "Follow-up request",
                        },
                    ],
                }) + "\n",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                verify_observability.verify_models(
                    SimpleNamespace(url="http://router", dataset=str(dataset))
                )


if __name__ == "__main__":
    unittest.main()
