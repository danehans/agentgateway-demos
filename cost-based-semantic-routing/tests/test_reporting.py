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
                        ("always_low_cost", "gpt-5.4-nano", "gpt-5.4-nano-2026-07-01"),
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
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            report = prometheus_report.build_report(
                "http://prometheus", "30m", "test-run", catalog_path, 3
            )

        self.assertEqual(report["window"], "30m")
        self.assertEqual(report["experiment_id"], "test-run")
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
                    "value": [0, "1"],
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
            catalog_path.write_text(json.dumps({
                "providers": {"openai": {"models": {
                    "gpt-5.5": {"rates": {"input": "5"}}
                }}}
            }), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "missing evaluation lanes"):
                prometheus_report.build_report(
                    "http://prometheus", "30m", "test-run", catalog_path, 1
                )


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
            prometheus_json.write_text(json.dumps({"window": "30m"}), encoding="utf-8")
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
            self.assertIn("Catalog-backed Prometheus summary (30m window)", rendered)
            self.assertIn("Routing accuracy: 90.0%", rendered)


class VerifyObservabilityTest(unittest.TestCase):
    def test_extracts_prometheus_values(self):
        vector = [{"metric": {}, "value": [0, "2.5"]}]
        scalar = [0, "3"]

        self.assertEqual(verify_observability.result_values(vector), [2.5])
        self.assertEqual(verify_observability.result_values(scalar), [3.0])

    def test_finds_correlated_signal_values(self):
        payload = {
            "resource": {"service.name": "agentgateway-proxy"},
            "attributes": {"experiment.id": "test-run"},
        }

        self.assertTrue(
            verify_observability.json_contains(
                payload, ["agentgateway-proxy", "test-run"]
            )
        )
        self.assertFalse(verify_observability.json_contains(payload, ["missing-run"]))

    def test_verifies_every_corpus_model(self):
        original_get_json = verify_observability.get_json
        verify_observability.get_json = lambda *_args, **_kwargs: {
            "data": [{"id": "gpt-cheap"}, {"id": "gpt-expensive"}]
        }
        self.addCleanup(setattr, verify_observability, "get_json", original_get_json)
        with tempfile.TemporaryDirectory() as directory:
            corpus = Path(directory) / "corpus.jsonl"
            corpus.write_text(
                json.dumps({"expected_model": "gpt-cheap"}) + "\n" +
                json.dumps({"expected_model": "gpt-expensive"}) + "\n",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                verify_observability.verify_models(
                    SimpleNamespace(url="http://router", corpus=str(corpus))
                )


if __name__ == "__main__":
    unittest.main()
