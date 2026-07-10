import importlib.util
import json
import tempfile
import unittest
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


class PrometheusReportTest(unittest.TestCase):
    def test_builds_structured_report(self):
        def fake_query(_base_url, expression):
            if "cost_catalog_lookups" in expression:
                return [{
                    "metric": {
                        "status": "Exact",
                        "gen_ai_request_model": "gpt-5.5",
                        "gen_ai_response_model": "gpt-5.5-2026-07-01",
                    },
                    "value": [0, "3"],
                }]
            if "eval_lane" in expression:
                return [{
                    "metric": {
                        "eval_lane": "always_expensive",
                        "gen_ai_request_model": "gpt-5.5",
                        "gen_ai_response_model": "gpt-5.5-2026-07-01",
                    },
                    "value": [0, "0.125"],
                }]
            return []

        original_query = prometheus_report.query
        prometheus_report.query = fake_query
        self.addCleanup(setattr, prometheus_report, "query", original_query)

        report = prometheus_report.build_report("http://prometheus", "30m")

        self.assertEqual(report["window"], "30m")
        self.assertEqual(
            report["catalog_backed_realized_cost_by_lane"][0]["cost_usd"],
            0.125,
        )
        self.assertEqual(report["model_catalog_lookups"][0]["lookups"], 3.0)
        self.assertIn("always_expensive", prometheus_report.render_report(report))


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


if __name__ == "__main__":
    unittest.main()
