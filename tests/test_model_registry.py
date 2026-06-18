import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.strategy.confluence_scorer import (
    ConfluenceScorer,
    activate_model_version,
    build_model_id,
    list_model_versions,
    save_model_version,
)


class ModelRegistryTests(unittest.TestCase):
    def test_naming_rule_contains_date_trainer_and_description(self):
        model_id = build_model_id(
            "2026-06-18T12:44:50",
            "codex",
            "RR3 Band4 MinTF2 production baseline",
        )
        self.assertEqual(
            model_id,
            "20260618_codex_rr3-band4-mintf2-production-baseline",
        )

    def test_versions_append_and_can_be_reactivated(self):
        with TemporaryDirectory() as tmp:
            active = Path(tmp) / "confluence_scorer.json"
            first = ConfluenceScorer(
                weights={"rr": 1.0},
                meta={
                    "trained": True,
                    "trained_at": "2026-06-18T10:00:00",
                    "cfg": {"rr": 3, "band_ticks": 4, "min_distinct_tf": 2},
                },
            )
            first_id, first_path = save_model_version(
                first, "codex", "RR3 baseline", active_path=active,
            )
            second = ConfluenceScorer(
                weights={"rr": 2.0},
                meta={
                    "trained": True,
                    "trained_at": "2026-06-18T11:00:00",
                    "cfg": {"rr": 3, "band_ticks": 4, "min_distinct_tf": 2},
                },
            )
            second_id, second_path = save_model_version(
                second, "claude", "RR3 baseline", active_path=active,
            )

            self.assertTrue(first_path.exists())
            self.assertTrue(second_path.exists())
            self.assertNotEqual(first_id, second_id)
            versions, active_id = list_model_versions(active)
            self.assertEqual(len(versions), 2)
            self.assertEqual(active_id, second_id)

            activate_model_version(first_id, active)
            payload = json.loads(active.read_text(encoding="utf-8"))
            self.assertEqual(payload["meta"]["model_id"], first_id)
            self.assertEqual(payload["weights"]["rr"], 1.0)

    def test_duplicate_name_gets_numeric_suffix(self):
        with TemporaryDirectory() as tmp:
            active = Path(tmp) / "confluence_scorer.json"
            meta = {"trained": True, "trained_at": "2026-06-18T10:00:00"}
            first_id, _ = save_model_version(
                ConfluenceScorer(meta=dict(meta)),
                "codex",
                "same description",
                active_path=active,
            )
            second_id, _ = save_model_version(
                ConfluenceScorer(meta=dict(meta)),
                "codex",
                "same description",
                active_path=active,
            )
            self.assertEqual(first_id, "20260618_codex_same-description")
            self.assertEqual(second_id, "20260618_codex_same-description-02")


if __name__ == "__main__":
    unittest.main()
