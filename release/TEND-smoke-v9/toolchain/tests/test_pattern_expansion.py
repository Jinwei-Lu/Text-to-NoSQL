from pathlib import Path
from unittest.mock import patch
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tend_construct.phase_b.intents import build_structured_intent, SeedTuple
from tend_construct.phase_c.materializer import compile_mql
from tend_construct.phase_d.external_runner import (
    DEFAULT_API_KEY,
    DEFAULT_LLM_MODEL,
    NoopExternalModelRunner,
    OpenAICompatibleExternalModelRunner,
    build_external_model_runner,
    normalize_openai_base_url,
)
from tend_construct.phase_d.failure_bank import load_failure_bank
from tend_core import CanonicalFormSet


SCHEMA = {
    "events": {
        "type": "OBJECT",
        "fields": {
            "name": {"type": "TEXT"},
            "city": {"type": "TEXT"},
            "year": {"type": "INT"},
            "attendance": {"type": "INT"},
            "ticket_revenue": {"type": "REAL"},
            "fan_scores": {"type": "ARRAY", "items": {"type": "INT"}},
        },
    }
}


class PatternExpansionTest(unittest.TestCase):
    def test_failure_bank_loads_multiple_pattern_families(self) -> None:
        bank = load_failure_bank(Path(__file__).resolve().parents[1] / "assets" / "failure_mode_bank")
        self.assertIn("simple_filter", bank)
        self.assertIn("window_function_with_facet_filter", bank)
        self.assertIn("existential_quantifier", bank)

    def test_compile_all_23_patterns(self) -> None:
        patterns = [
            ("cross_group_comparison", "group_then_aggregate"),
            ("rare_event", "existential_quantifier"),
            ("temporal_trend", "anomaly_vs_baseline"),
            ("temporal_trend", "window_function_with_facet_filter"),
            ("null_cluster", "facet_split"),
            ("null_cluster", "simple_filter"),
            ("outlier", "top_k_by_aggregate"),
            ("temporal_trend", "time_window_aggregate"),
            ("null_cluster", "filter_then_aggregate"),
            ("high_cardinality", "project_only"),
            ("null_cluster", "null_vs_missing_disambig"),
            ("null_cluster", "coalesce_with_default"),
            ("type_drift", "polymorphic_branch"),
            ("type_drift", "type_introspection"),
            ("high_cardinality", "dynamic_key_expansion"),
            ("rare_event", "array_positional_select"),
            ("rare_event", "array_reshape"),
            ("cross_group_comparison", "lookup_join"),
            ("hierarchical_nesting", "graph_recursive_deep"),
            ("outlier", "percentile_approximation"),
            ("rare_event", "universal_quantifier"),
            ("temporal_trend", "window_function"),
            ("null_cluster", "filter_then_count"),
        ]
        for index, (phenomenon_class, pattern_family) in enumerate(patterns):
            si = build_structured_intent(
                db_id="events_001",
                record_id=2000 + index,
                seed_tuple=SeedTuple(
                    phenomenon={
                        "phenomenon_id": f"{phenomenon_class}@attendance",
                        "phenomenon_class": phenomenon_class,
                        "witness_evidence": {"collection": "events", "path": "attendance"},
                    },
                    persona={"persona_id": "analyst"},
                    pattern_family=pattern_family,
                ),
                schema_payload=SCHEMA,
            )
            si = si.__class__(
                meta=si.meta,
                intent=si.intent,
                output=si.output,
                properties=si.properties,
                noise_policies=si.noise_policies,
                nosql_nativeness=si.nosql_nativeness,
                canonical_form_set=CanonicalFormSet((), (), (), ()),
            )
            mql = compile_mql(si)
            self.assertTrue(mql.startswith("db.events.aggregate("))

    def test_noop_external_runner_returns_empty_candidates(self) -> None:
        runner = NoopExternalModelRunner()
        result = runner.generate("v_correct_neighborhood", {"prompt": "hello"})
        self.assertEqual(result["candidates"], [])

    def test_normalize_openai_base_url_appends_v1(self) -> None:
        self.assertEqual(
            normalize_openai_base_url("https://api.openai-proxy.org"),
            "https://api.openai-proxy.org/v1",
        )

    @patch("tend_construct.phase_d.external_runner.OpenAI")
    def test_openai_sdk_runner_parses_candidates(self, openai_cls) -> None:
        fake_client = openai_cls.return_value
        fake_client.chat.completions.create.return_value = type(
            "Completion",
            (),
            {
                "choices": [
                    type(
                        "Choice",
                        (),
                        {
                            "message": type(
                                "Message",
                                (),
                                {"content": "{\"candidates\": [\"db.foo.aggregate([])\"]}"},
                            )()
                        },
                    )()
                ]
            },
        )()
        runner = OpenAICompatibleExternalModelRunner(
            base_url="https://api.openai-proxy.org",
            model=DEFAULT_LLM_MODEL,
            api_key="test-key",
        )
        result = runner.generate("phase_c_nlq_x5", {"prompt": "hello"})
        self.assertEqual(result["candidates"], ["db.foo.aggregate([])"])

    @patch("tend_construct.phase_d.external_runner.OpenAI")
    def test_openai_runner_uses_hardcoded_default_key_and_model(self, openai_cls) -> None:
        fake_client = openai_cls.return_value
        fake_client.chat.completions.create.return_value = type(
            "Completion",
            (),
            {
                "choices": [
                    type(
                        "Choice",
                        (),
                        {
                            "message": type(
                                "Message",
                                (),
                                {"content": "{\"candidates\": []}"},
                            )()
                        },
                    )()
                ]
            },
        )()
        runner = build_external_model_runner(
            runner_kind="openai-compatible",
            base_url="https://api.openai-proxy.org",
        )
        self.assertIsInstance(runner, OpenAICompatibleExternalModelRunner)
        self.assertEqual(runner.model, DEFAULT_LLM_MODEL)
        openai_cls.assert_called_once_with(
            base_url="https://api.openai-proxy.org/v1",
            api_key=DEFAULT_API_KEY,
        )


if __name__ == "__main__":
    unittest.main()
