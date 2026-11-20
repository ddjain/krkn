"""Unit tests for krkn.telemetry_helpers.

Covers all three exported helpers: collect_health_check_telemetry,
group_checks_by_phase, build_telemetry_record.  Behavioral focus: queue
draining, phase bucketing with missing/invalid phase values, timestamp
normalization and duration computation.
"""

import queue
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock

from krkn.telemetry_helpers import (
    collect_health_check_telemetry,
    group_checks_by_phase,
    build_telemetry_record,
)


# ===========================================================================
# collect_health_check_telemetry
# ===========================================================================
class TestCollectHealthCheckTelemetry(unittest.TestCase):
    def test_collects_all_queue_entries(self):
        telemetry_queue = queue.Queue()
        first_record = {"url": "https://first.example", "phase": "pre"}
        second_record = {"url": "https://second.example", "phase": "pre"}
        telemetry_queue.put(first_record)
        telemetry_queue.put(second_record)

        health_checks, object_state_checks = collect_health_check_telemetry(telemetry_queue)

        self.assertEqual(health_checks, [first_record, second_record])
        self.assertEqual(object_state_checks, [])
        self.assertTrue(telemetry_queue.empty())

    def test_empty_queue_returns_two_empty_lists(self):
        tq = queue.Queue()
        hc, osc = collect_health_check_telemetry(tq)
        self.assertEqual(hc, [])
        self.assertEqual(osc, [])


    def test_list_enqueued_is_flattened(self):
        tq = queue.Queue()
        tq.put([{"phase": "pre"}, {"phase": "post"}])
        hc, osc = collect_health_check_telemetry(tq)
        self.assertEqual(len(hc), 2)
        self.assertEqual(osc, [])

    def test_mixed_list_and_single_item(self):
        tq = queue.Queue()
        tq.put([{"phase": "pre"}])
        tq.put({"phase": "post"})
        hc, osc = collect_health_check_telemetry(tq)
        self.assertEqual(len(hc), 2)

    def test_object_state_check_routing(self):
        tq = queue.Queue()
        fake = MagicMock(spec=["check_name", "kind", "phase"])
        fake.check_name = "my-check"
        fake.kind = "Pod"
        tq.put(fake)
        hc, osc = collect_health_check_telemetry(tq)
        self.assertEqual(osc, [fake])
        self.assertEqual(hc, [])

    def test_non_dict_non_object_goes_to_health_checks(self):
        tq = queue.Queue()
        tq.put("plain string")
        hc, osc = collect_health_check_telemetry(tq)
        self.assertEqual(hc, ["plain string"])
        self.assertEqual(osc, [])

    def test_queue_is_drained_on_exit(self):
        tq = queue.Queue()
        tq.put({"a": 1})
        tq.put({"a": 2})
        collect_health_check_telemetry(tq)
        self.assertTrue(tq.empty())


# ===========================================================================
# group_checks_by_phase
# ===========================================================================
class TestGroupChecksByPhase(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual(group_checks_by_phase([]), {})

    def test_groups_by_explicit_phase(self):
        checks = [
            {"phase": "pre", "url": "a"},
            {"phase": "post", "url": "b"},
            {"phase": "pre", "url": "c"},
        ]
        result = group_checks_by_phase(checks)
        self.assertCountEqual(result["pre"], [{"phase": "pre", "url": "a"}, {"phase": "pre", "url": "c"}])
        self.assertEqual(result["post"], [{"phase": "post", "url": "b"}])

    def test_missing_phase_defaults_to_during(self):
        result = group_checks_by_phase([{"url": "a"}])
        self.assertEqual(len(result["during"]), 1)

    def test_none_phase_bucket(self):
        # phase is explicitly None inside the dict value — should still group under None key
        result = group_checks_by_phase([{"phase": None}])
        self.assertIn(None, result)

    def test_object_phase_attribute(self):
        fake = MagicMock()
        fake.phase = "post"
        result = group_checks_by_phase([fake])
        self.assertIn("post", result)
        self.assertIn(fake, result["post"])

    def test_object_missing_phase_defaults_to_during(self):
        fake = MagicMock(spec=[])  # no phase attr at all on spec, but getattr fallback
        # MagicMock returns another MagicMock for missing attrs — simulate absence
        del fake.phase
        result = group_checks_by_phase([fake])
        self.assertIn("during", result)

    def test_mixed_dict_and_object(self):
        obj = MagicMock()
        obj.phase = "during"
        checks = [{"phase": "pre"}, obj, {"url": "x"}]
        result = group_checks_by_phase(checks)
        self.assertIn("pre", result)
        self.assertIn("during", result)
        self.assertEqual(len(result["during"]), 2)


# ===========================================================================
# build_telemetry_record
# ===========================================================================
class TestBuildTelemetryRecord(unittest.TestCase):
    def test_datetime_timestamps_are_iso_formatted(self):
        start = datetime(2026, 1, 1, 10, 0, 0)
        end = datetime(2026, 1, 1, 10, 0, 30)
        rec = build_telemetry_record({"url": "u"}, True, start, end)
        self.assertEqual(rec["start_timestamp"], start.isoformat())
        self.assertEqual(rec["end_timestamp"], end.isoformat())
        self.assertAlmostEqual(rec["duration"], 30.0)
        self.assertTrue(rec["status"])
        self.assertEqual(rec["phase"], "during")

    def test_string_timestamps_pass_through(self):
        rec = build_telemetry_record({"url": "u"}, False, "2026-01-01T10:00:00", "2026-01-01T10:01:00")
        self.assertEqual(rec["start_timestamp"], "2026-01-01T10:00:00")
        self.assertEqual(rec["end_timestamp"], "2026-01-01T10:01:00")
        self.assertEqual(rec["duration"], 0.0)

    def test_end_defaults_to_now_when_none(self):
        start = datetime(2026, 1, 1, 10, 0, 0)
        rec = build_telemetry_record({"url": "u"}, True, start, None)
        self.assertIsNotNone(rec["end_timestamp"])

    def test_explicit_duration_overrides_computed(self):
        start = datetime(2026, 1, 1, 10, 0, 0)
        end = datetime(2026, 1, 1, 10, 0, 30)
        rec = build_telemetry_record({"url": "u"}, True, start, end, duration=99.0)
        self.assertEqual(rec["duration"], 99.0)

    def test_string_start_plus_datetime_end_does_not_compute_duration(self):
        start = "2026-01-01T10:00:00"
        end = datetime(2026, 1, 1, 10, 0, 30)
        rec = build_telemetry_record({"url": "u"}, True, start, end)
        # duration should remain 0.0 because start is not datetime
        self.assertEqual(rec["duration"], 0.0)

    def test_phase_parameter_respected(self):
        rec = build_telemetry_record({}, True, datetime.now(), datetime.now(), phase="post")
        self.assertEqual(rec["phase"], "post")

    def test_base_data_merged(self):
        rec = build_telemetry_record({"check_name": "my-check", "extra": 123}, True, datetime.now(), datetime.now())
        self.assertEqual(rec["check_name"], "my-check")
        self.assertEqual(rec["extra"], 123)

    def test_mixed_datetime_and_string(self):
        start = datetime(2026, 6, 1, 12, 0, 0)
        end = "2026-06-01T12:05:00"
        rec = build_telemetry_record({}, True, start, end)
        self.assertEqual(rec["start_timestamp"], start.isoformat())
        self.assertEqual(rec["end_timestamp"], "2026-06-01T12:05:00")
        # Computed duration only when both are datetime, but end is str here -> 0.0
        self.assertEqual(rec["duration"], 0.0)

class TestExtractScenarioTypes(unittest.TestCase):
    """Coverage for extract_scenario_types — previously 0% tested.

    This function is used for plugin validation logging in run_kraken.py;
    untested branches include explicit-format detection and non-dict skipping.
    """

    def test_old_format_extracts_key(self):
        from krkn.scenario_config_parser import extract_scenario_types
        scenarios = [
            {"pod_disruption_scenarios": ["a.yml"]},
            {"hog_scenarios": ["b.yml"]},
        ]
        result = extract_scenario_types(scenarios)
        self.assertEqual(result, {"pod_disruption_scenarios", "hog_scenarios"})

    def test_weighted_old_format_extracts_key(self):
        from krkn.scenario_config_parser import extract_scenario_types
        scenarios = [
            {"pod_disruption_scenarios": {"weight": 3, "files": ["a.yml"]}},
        ]
        result = extract_scenario_types(scenarios)
        self.assertEqual(result, {"pod_disruption_scenarios"})

    def test_explicit_format_extracts_scenario_type(self):
        from krkn.scenario_config_parser import extract_scenario_types
        scenarios = [
            {"scenario_type": "service_disruption_scenarios", "weight": 2, "files": ["a.yml"]},
        ]
        result = extract_scenario_types(scenarios)
        self.assertEqual(result, {"service_disruption_scenarios"})

    def test_mixed_formats(self):
        from krkn.scenario_config_parser import extract_scenario_types
        scenarios = [
            {"pod_disruption_scenarios": ["a.yml"]},
            {"scenario_type": "hog_scenarios", "weight": 1, "files": ["b.yml"]},
            {"hog_scenarios": {"weight": 2, "files": ["c.yml"]}},
        ]
        result = extract_scenario_types(scenarios)
        self.assertEqual(result, {"pod_disruption_scenarios", "hog_scenarios"})

    def test_non_dict_entries_skipped(self):
        from krkn.scenario_config_parser import extract_scenario_types
        scenarios = [
            {"pod_disruption_scenarios": ["a.yml"]},
            "not a dict",
            None,
            123,
        ]
        result = extract_scenario_types(scenarios)
        self.assertEqual(result, {"pod_disruption_scenarios"})

    def test_empty_list_returns_empty_set(self):
        from krkn.scenario_config_parser import extract_scenario_types
        self.assertEqual(extract_scenario_types([]), set())

    def test_deduplicates_repeated_types(self):
        from krkn.scenario_config_parser import extract_scenario_types
        scenarios = [
            {"pod_disruption_scenarios": ["a.yml"]},
            {"pod_disruption_scenarios": ["b.yml"]},
        ]
        result = extract_scenario_types(scenarios)
        self.assertEqual(result, {"pod_disruption_scenarios"})


class TestValidateWeightEdgeCases(unittest.TestCase):
    """Edge cases for _validate_weight not covered by test_weight_validation.py.

    The prod code uses float() coercion; nan/inf semantics are subtle:
      float('nan') <= 0 is False  -> nan passes the >0 check and is returned
      float('inf') <= 0 is False  -> inf passes and is returned
    Both would poison resiliency scoring downstream.  These tests lock the
    observable behavior (not demanding a fix, just documenting it).
    """

    def test_float_nan_passthrough(self):
        from krkn.scenario_config_parser import _validate_weight
        import math
        result = _validate_weight(float("nan"), "svc")
        self.assertTrue(math.isnan(result), f"expected nan passthrough, got {result}")

    def test_float_inf_passthrough(self):
        from krkn.scenario_config_parser import _validate_weight
        import math
        result = _validate_weight(float("inf"), "svc")
        self.assertTrue(math.isinf(result))

    def test_string_nan_passthrough(self):
        from krkn.scenario_config_parser import _validate_weight
        import math
        result = _validate_weight("nan", "svc")
        self.assertTrue(math.isnan(result))

    def test_string_inf_passthrough(self):
        from krkn.scenario_config_parser import _validate_weight
        import math
        result = _validate_weight("inf", "svc")
        self.assertTrue(math.isinf(result))

    def test_bool_true_coerced_to_one(self):
        from krkn.scenario_config_parser import _validate_weight
        # bool is subclass of int: True -> float(True) == 1.0
        self.assertEqual(_validate_weight(True, "svc"), 1.0)

    def test_bool_false_defaults_to_one(self):
        from krkn.scenario_config_parser import _validate_weight
        # False -> float(False) == 0.0 -> <=0 -> defaults
        self.assertEqual(_validate_weight(False, "svc"), 1.0)

    def test_empty_string_weight_defaults_to_one(self):
        from krkn.scenario_config_parser import _validate_weight
        self.assertEqual(_validate_weight("", "svc"), 1.0)

    def test_whitespace_string_weight_defaults_to_one(self):
        from krkn.scenario_config_parser import _validate_weight
        self.assertEqual(_validate_weight("   ", "svc"), 1.0)

    def test_zero_string_defaults_to_one(self):
        from krkn.scenario_config_parser import _validate_weight
        self.assertEqual(_validate_weight("0", "svc"), 1.0)



if __name__ == "__main__":
    unittest.main()
