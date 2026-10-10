#!/usr/bin/env python3
"""Readiness assessment regressions that run without GPU, model, or network access."""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("readiness", ROOT / "scripts" / "readiness.py")
assert SPEC is not None and SPEC.loader is not None
readiness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(readiness)


def good_model() -> dict:
    return {
        "found": True,
        "probe_error": False,
        "selection": "exact",
        "size_match": "YES",
        "trusted_sha_match": "YES",
    }


def good_facts() -> dict:
    return {
        "mode": "quick",
        "manifest": {"status": "PASS"},
        "required_gpu_count": 2,
        "gpu_min_vram_required_mib": None,
        "gpu_count": 2,
        "gpu_min_vram_observed_mib": None,
        "target": good_model(),
        "draft": good_model(),
        "runtime": {
            "error": False,
            "prebuilt_present": True,
            "prebuilt_trusted": True,
            "source_build": False,
            "source_mode": "kaggle",
        },
        "token": {"state": "PRESENT"},
        "cloudflared": {"present": True, "required": False},
        "named": {},
    }


class ReadinessAssessmentTests(unittest.TestCase):
    def test_valid_facts_pass(self):
        result = readiness.assess(good_facts())
        self.assertEqual(result["start_readiness"], "PASS")
        self.assertEqual(result["blockers"], [])

    def test_target_probe_error_fails_closed(self):
        facts = good_facts()
        facts["target"] = {"found": False, "probe_error": True}
        result = readiness.assess(facts)
        self.assertEqual(result["start_readiness"], "ERROR")
        self.assertEqual(result["target"]["status"], "ERROR")

    def test_draft_probe_error_fails_closed(self):
        facts = good_facts()
        facts["draft"] = {"found": False, "probe_error": True}
        result = readiness.assess(facts)
        self.assertEqual(result["start_readiness"], "ERROR")
        self.assertEqual(result["draft"]["status"], "ERROR")

    def test_missing_model_is_a_blocker_not_probe_error(self):
        facts = good_facts()
        facts["target"] = {"found": False, "probe_error": False}
        result = readiness.assess(facts)
        self.assertEqual(result["start_readiness"], "FAIL")
        self.assertIn("TARGET_MODEL_MISSING", result["blockers"])

    def test_identity_mismatch_fails_closed(self):
        facts = good_facts()
        facts["draft"]["trusted_sha_match"] = "NO"
        result = readiness.assess(facts)
        self.assertEqual(result["start_readiness"], "FAIL")
        self.assertIn("DFLASH_IDENTITY", result["blockers"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
