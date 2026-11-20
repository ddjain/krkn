import datetime
import importlib
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from unittest.mock import MagicMock

from krkn.prometheus import client


class TestMetricsQueryRouting(unittest.TestCase):
    def setUp(self):
        self.prom_cli = MagicMock()
        self.elastic = MagicMock()
        self.telemetry_json = json.dumps({"scenarios": [], "health_checks": [], "virt_checks": []})

    def _write_profile(self, metrics):
        profile = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
        profile.write(json.dumps({"metrics": metrics}))
        profile.close()
        return profile.name

    def test_range_query(self):
        path = self._write_profile([{"query": "up", "metricName": "target_up"}])
        try:
            self.prom_cli.process_prom_query_in_range.return_value = []
            client.metrics(self.prom_cli, self.elastic, "run", 1000, 1060, path, "metrics", self.telemetry_json)
            self.prom_cli.process_prom_query_in_range.assert_called_once()
            self.prom_cli.process_query.assert_not_called()
        finally:
            os.unlink(path)

    def test_instant_query(self):
        path = self._write_profile([{"query": "up", "metricName": "target_up", "instant": True}])
        try:
            self.prom_cli.process_query.return_value = []
            client.metrics(self.prom_cli, self.elastic, "run", 1000, 1060, path, "metrics", self.telemetry_json)
            self.prom_cli.process_query.assert_called_once()
            self.prom_cli.process_prom_query_in_range.assert_not_called()
        finally:
            os.unlink(path)

    def test_elapsed_placeholder_uses_rounded_run_duration(self):
        path = self._write_profile([
            {"query": "rate(requests_total[.elapsed])", "metricName": "request_rate"}
        ])
        try:
            self.prom_cli.process_prom_query_in_range.return_value = []
            client.metrics(self.prom_cli, self.elastic, "run", 1000, 1432, path, "metrics", self.telemetry_json)
            query = self.prom_cli.process_prom_query_in_range.call_args.args[0]
            self.assertEqual(query, "rate(requests_total[8m])")
        finally:
            os.unlink(path)

    def test_missing_metrics_profile_exits(self):
        # Regression for #1607: missing import sys used to raise NameError
        # instead of logging and exiting with code 1.
        with self.assertRaises(SystemExit) as cm:
            client.metrics(
                self.prom_cli,
                self.elastic,
                "run",
                1000,
                1060,
                "/nao/existe.yaml",
                "metrics",
                self.telemetry_json,
            )
        self.assertEqual(cm.exception.code, 1)

    def test_invalid_metrics_profile_exits(self):
        path = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
        path.write("metrics: {}\n")
        path.close()
        try:
            with self.assertRaises(SystemExit) as cm:
                client.metrics(
                    self.prom_cli,
                    self.elastic,
                    "run",
                    1000,
                    1060,
                    path.name,
                    "metrics",
                    self.telemetry_json,
                )
            self.assertEqual(cm.exception.code, 1)
        finally:
            os.unlink(path.name)

    def test_critical_alert_range_query_uses_datetimes(self):
        summary = MagicMock()
        self.prom_cli.process_prom_query_in_range.return_value = []
        self.prom_cli.process_query.return_value = []

        client.critical_alerts(
            self.prom_cli,
            summary,
            self.elastic,
            "run",
            "scenario",
            1000,
            1060,
            "alerts",
        )

        call = self.prom_cli.process_prom_query_in_range.call_args
        self.assertIsInstance(call.kwargs["start_time"], datetime.datetime)
        self.assertIsInstance(call.kwargs["end_time"], datetime.datetime)
        self.assertEqual(call.kwargs["start_time"].timestamp(), 1000)
        self.assertEqual(call.kwargs["end_time"].timestamp(), 1060)

    def test_critical_alert_range_query_accepts_datetime_end_time(self):
        summary = MagicMock()
        self.prom_cli.process_prom_query_in_range.return_value = []
        self.prom_cli.process_query.return_value = []
        end_time = datetime.datetime(2026, 10, 2, 15, 3)

        client.critical_alerts(
            self.prom_cli,
            summary,
            self.elastic,
            "run",
            "scenario",
            1000,
            end_time,
            "alerts",
        )

        call = self.prom_cli.process_prom_query_in_range.call_args
        self.assertEqual(call.kwargs["end_time"], end_time.replace(tzinfo=datetime.timezone.utc))

    def test_critical_alert_range_query_keeps_start_before_end(self):
        summary = MagicMock()
        self.prom_cli.process_prom_query_in_range.return_value = []
        self.prom_cli.process_query.return_value = []
        end_time = datetime.datetime.now(datetime.timezone.utc)

        client.critical_alerts(
            self.prom_cli,
            summary,
            self.elastic,
            "run",
            "scenario",
            int(end_time.timestamp()) - 60,
            end_time,
            "alerts",
        )

        call = self.prom_cli.process_prom_query_in_range.call_args
        self.assertLess(call.kwargs["start_time"], call.kwargs["end_time"])


# test_logging_and_code_quality.py installs MagicMock stubs for krkn_lib
# submodules in sys.modules at collection time, so depending on collection
# order the top-level import of krkn.prometheus.client (and any krkn_lib
# names bound here) may refer to those stubs. setUpModule corrects this for
# this module only: stub entries are dropped from sys.modules inside
# mock.patch.dict(), the real krkn_lib model modules are imported, and the
# names used by these tests are patched to the real classes. Everything is
# restored at module teardown via addModuleCleanup, so no stub or real-module
# state leaks into other test files.
_MODEL_MODULES = (
    "krkn_lib",
    "krkn_lib.models",
    "krkn_lib.models.krkn",
    "krkn_lib.models.elastic",
    "krkn_lib.models.elastic.models",
)

# Bound to the real krkn_lib classes by setUpModule.
ElasticAlert = ChaosRunAlert = ChaosRunAlertSummary = None


def setUpModule():
    modules_patcher = mock.patch.dict(sys.modules)
    modules_patcher.start()
    unittest.addModuleCleanup(modules_patcher.stop)
    for name in _MODEL_MODULES:
        loaded = sys.modules.get(name)
        if loaded is not None and not isinstance(
            loaded.__dict__.get("__file__"), str
        ):
            del sys.modules[name]
    elastic_models = importlib.import_module("krkn_lib.models.elastic.models")
    krkn_models = importlib.import_module("krkn_lib.models.krkn")
    real_classes = {
        "ElasticAlert": elastic_models.ElasticAlert,
        "ChaosRunAlert": krkn_models.ChaosRunAlert,
        "ChaosRunAlertSummary": krkn_models.ChaosRunAlertSummary,
    }
    for target in (globals(), vars(client)):
        patcher = mock.patch.dict(target, real_classes)
        patcher.start()
        unittest.addModuleCleanup(patcher.stop)


def tearDownModule():
    # pytest does not run unittest module cleanups, so run them explicitly here;
    # under unittest the runner calls doModuleCleanups again afterwards (no-op)
    unittest.doModuleCleanups()


# Prometheus API result fixtures (the data.result list entries).
ALERT_RESULT = {
    "metric": {
        "alertname": "KubeNodeNotReady",
        "alertstate": "firing",
        "namespace": "default",
        "severity": "critical",
    },
    "value": [1600000000, "1"],
}
RANGE_RESULT = {
    "metric": {"__name__": "up", "job": "api"},
    "values": [[1600000000, "1"], [1600000030, "0.5"]],
}
INSTANT_RESULT = {
    "metric": {"__name__": "up", "job": "api"},
    "value": [1600000000, "0.5"],
}
EMPTY_TELEMETRY = json.dumps(
    {"scenarios": [], "health_checks": [], "virt_checks": []}
)


class TestAsPrometheusDatetime(unittest.TestCase):
    def test_float_timestamp_is_converted_to_utc_datetime(self):
        converted = client._as_prometheus_datetime(1000.5)
        self.assertEqual(converted.timestamp(), 1000.5)
        self.assertEqual(converted.tzinfo, datetime.timezone.utc)

    def test_naive_datetime_is_assumed_utc(self):
        naive = datetime.datetime(2026, 10, 2, 15, 3)
        converted = client._as_prometheus_datetime(naive)
        self.assertEqual(converted, naive.replace(tzinfo=datetime.timezone.utc))

    def test_string_timestamp_raises_type_error(self):
        # Characterization: the helper only normalizes Unix timestamps and
        # datetimes; a numeric string fails inside fromtimestamp() with a
        # bare TypeError instead of being parsed or rejected up front.
        with self.assertRaisesRegex(TypeError, "cannot be interpreted as an integer"):
            client._as_prometheus_datetime("1600000000")


class TestCriticalAlerts(unittest.TestCase):
    def setUp(self):
        self.prom_cli = MagicMock()
        self.elastic = MagicMock()
        self.elastic.push_alert.return_value = 0
        self.summary = ChaosRunAlertSummary()

    _UNSET = object()

    def _run(self, start_time=1000, end_time=1060, index="alerts", elastic=_UNSET):
        client.critical_alerts(
            self.prom_cli,
            self.summary,
            self.elastic if elastic is TestCriticalAlerts._UNSET else elastic,
            "run-1",
            "scenario-a",
            start_time,
            end_time,
            index,
        )

    def test_during_chaos_alert_is_added_to_chaos_alerts(self):
        self.prom_cli.process_prom_query_in_range.return_value = [ALERT_RESULT]
        self.prom_cli.process_query.return_value = []

        self._run()

        self.assertEqual(self.summary.scenario, "scenario-a")
        self.assertEqual(self.summary.run_id, "run-1")
        self.assertEqual(len(self.summary.chaos_alerts), 1)
        alert = self.summary.chaos_alerts[0]
        self.assertIsInstance(alert, ChaosRunAlert)
        self.assertEqual(alert.alertname, "KubeNodeNotReady")
        self.assertEqual(alert.alertstate, "firing")
        self.assertEqual(alert.namespace, "default")
        self.assertEqual(alert.severity, "critical")
        self.assertEqual(self.summary.post_chaos_alerts, [])
        # during-chaos alerts are collected in the summary only: they are
        # never pushed to elasticsearch
        self.assertFalse(
            self.elastic.push_alert.called,
            "during-chaos alerts must not be pushed to elasticsearch",
        )

    def test_post_chaos_alert_is_added_to_post_chaos_alerts(self):
        self.prom_cli.process_prom_query_in_range.return_value = []
        self.prom_cli.process_query.return_value = [ALERT_RESULT]

        self._run()

        self.assertEqual(self.summary.chaos_alerts, [])
        self.assertEqual(len(self.summary.post_chaos_alerts), 1)
        self.assertEqual(
            self.summary.post_chaos_alerts[0].alertname, "KubeNodeNotReady"
        )

    def test_post_chaos_alert_is_pushed_to_elastic(self):
        end_time = datetime.datetime(2026, 10, 2, 15, 3, tzinfo=datetime.timezone.utc)
        self.prom_cli.process_prom_query_in_range.return_value = []
        self.prom_cli.process_query.return_value = [ALERT_RESULT]

        with self.assertLogs(level="INFO") as logs:
            self._run(end_time=end_time, index="alerts-index")

        self.elastic.push_alert.assert_called_once()
        elastic_alert, pushed_index = self.elastic.push_alert.call_args.args
        self.assertEqual(pushed_index, "alerts-index")
        self.assertIsInstance(elastic_alert, ElasticAlert)
        self.assertEqual(elastic_alert.run_uuid, "run-1")
        self.assertEqual(elastic_alert.severity, "critical")
        self.assertEqual(elastic_alert.alert, "KubeNodeNotReady")
        self.assertEqual(elastic_alert.namespace, "default")
        self.assertEqual(elastic_alert.alertstate, "firing")
        self.assertEqual(elastic_alert.phase, "post_chaos")
        self.assertEqual(elastic_alert.created_at, end_time)
        # a firing post-chaos alert must not log "no alerts firing"
        self.assertNotIn("No critical alerts are firing!!", "".join(logs.output))

    def test_multiple_post_chaos_alerts_are_each_pushed(self):
        second_alert = {
            "metric": {
                "alertname": "KubePodCrashLooping",
                "alertstate": "firing",
                "namespace": "kube-system",
                "severity": "critical",
            },
            "value": [1600000000, "1"],
        }
        self.prom_cli.process_prom_query_in_range.return_value = []
        # a raw string result is skipped but does not stop the loop
        self.prom_cli.process_query.return_value = [
            ALERT_RESULT,
            "garbage",
            second_alert,
        ]

        self._run()

        self.assertEqual(len(self.summary.post_chaos_alerts), 2)
        self.assertEqual(self.elastic.push_alert.call_count, 2)
        pushed_names = [
            call.args[0].alert for call in self.elastic.push_alert.call_args_list
        ]
        self.assertEqual(
            pushed_names, ["KubeNodeNotReady", "KubePodCrashLooping"]
        )

    def test_push_alert_failure_logs_error_without_raising(self):
        self.elastic.push_alert.return_value = -1
        self.prom_cli.process_prom_query_in_range.return_value = []
        self.prom_cli.process_query.return_value = [ALERT_RESULT]

        with self.assertLogs(level="ERROR") as logs:
            self._run()

        self.assertIn(
            "failed to save alert on ElasticSearch", "".join(logs.output)
        )
        # the alert is kept in the summary even when the push fails
        self.assertEqual(len(self.summary.post_chaos_alerts), 1)

    def test_missing_elastic_client_keeps_post_chaos_alerts(self):
        self.prom_cli.process_prom_query_in_range.return_value = []
        # two post alerts: the first one must not stop the loop when the
        # elastic push is skipped
        self.prom_cli.process_query.return_value = [ALERT_RESULT, ALERT_RESULT]

        self._run(elastic=None)

        self.assertEqual(len(self.summary.post_chaos_alerts), 2)

    def test_missing_alert_labels_default_to_none(self):
        partial = {"metric": {"alertname": "OnlyName"}, "value": [1600000000, "1"]}
        self.prom_cli.process_prom_query_in_range.return_value = [partial]
        self.prom_cli.process_query.return_value = [partial]

        self._run()

        for alert in self.summary.chaos_alerts + self.summary.post_chaos_alerts:
            self.assertEqual(alert.alertname, "OnlyName")
            self.assertEqual(alert.alertstate, "none")
            self.assertEqual(alert.namespace, "none")
            self.assertEqual(alert.severity, "none")

    def test_result_without_metric_key_is_skipped_but_counted(self):
        # a raw string in the result list is skipped by the "metric" guard
        # but still counts towards the firing-alerts flag
        self.prom_cli.process_prom_query_in_range.return_value = ["garbage", ALERT_RESULT]
        self.prom_cli.process_query.return_value = []

        with self.assertLogs(level="INFO") as logs:
            self._run()

        self.assertEqual(len(self.summary.chaos_alerts), 1)
        self.assertNotIn("No critical alerts are firing!!", "".join(logs.output))

    def test_no_alerts_at_all_logs_no_firing_message(self):
        self.prom_cli.process_prom_query_in_range.return_value = []
        self.prom_cli.process_query.return_value = []

        with self.assertLogs(level="INFO") as logs:
            self._run()

        self.assertIn("No critical alerts are firing!!", "".join(logs.output))

    def test_range_query_exception_propagates(self):
        self.prom_cli.process_prom_query_in_range.side_effect = RuntimeError("prom down")

        with self.assertRaises(RuntimeError):
            self._run()

    def test_post_query_exception_leaves_partial_summary(self):
        # the during-chaos alerts are already stored on the summary when the
        # post-chaos query fails: the partial state is kept, not rolled back
        self.prom_cli.process_prom_query_in_range.return_value = [ALERT_RESULT]
        self.prom_cli.process_query.side_effect = RuntimeError("prom down")

        with self.assertRaises(RuntimeError):
            self._run()

        self.assertEqual(len(self.summary.chaos_alerts), 1)

    def test_post_query_returning_none_is_skipped(self):
        self.prom_cli.process_prom_query_in_range.return_value = [ALERT_RESULT]
        self.prom_cli.process_query.return_value = None

        self._run()

        self.assertEqual(len(self.summary.chaos_alerts), 1)
        self.assertEqual(self.summary.post_chaos_alerts, [])

    def test_repeated_calls_accumulate_alerts(self):
        # Characterization: alerts are appended with no deduplication, so
        # calling critical_alerts twice on the same summary doubles them.
        self.prom_cli.process_prom_query_in_range.return_value = [ALERT_RESULT]
        self.prom_cli.process_query.return_value = [ALERT_RESULT]

        self._run()
        self._run()

        self.assertEqual(len(self.summary.chaos_alerts), 2)
        self.assertEqual(len(self.summary.post_chaos_alerts), 2)


class TestMetricsResultParsing(unittest.TestCase):
    def setUp(self):
        self.prom_cli = MagicMock()
        self.elastic = MagicMock()
        self.elastic.upload_metrics_to_elasticsearch.return_value = 0
        self.telemetry_json = EMPTY_TELEMETRY

    def _write_profile(self, metrics):
        profile = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
        profile.write(json.dumps({"metrics": metrics}))
        profile.close()
        return profile.name

    def _run(self, path):
        return client.metrics(
            self.prom_cli,
            self.elastic,
            "run-1",
            1000,
            1060,
            path,
            "metrics-idx",
            self.telemetry_json,
        )

    def test_range_result_produces_one_entry_per_sample(self):
        path = self._write_profile([{"query": "up", "metricName": "target_up"}])
        try:
            self.prom_cli.process_prom_query_in_range.return_value = [RANGE_RESULT]

            result = self._run(path)

            self.assertEqual(len(result), 2)
            first, second = result
            self.assertEqual(first["query"], "up")
            self.assertEqual(first["metricName"], "target_up")
            self.assertEqual(first["__name__"], "up")
            self.assertEqual(first["job"], "api")
            self.assertEqual(
                first["timestamp"],
                str(datetime.datetime.fromtimestamp(1600000000)),
            )
            self.assertEqual(first["value"], 1.0)
            self.assertEqual(second["value"], 0.5)
        finally:
            os.unlink(path)

    def test_instant_result_produces_one_entry(self):
        path = self._write_profile(
            [{"query": "up", "metricName": "target_up", "instant": True}]
        )
        try:
            self.prom_cli.process_query.return_value = [INSTANT_RESULT]

            result = self._run(path)

            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["value"], 0.5)
            self.assertEqual(result[0]["job"], "api")
            self.assertEqual(
                result[0]["timestamp"],
                str(datetime.datetime.fromtimestamp(1600000000)),
            )
        finally:
            os.unlink(path)

    def test_instant_non_numeric_value_is_dropped(self):
        path = self._write_profile(
            [{"query": "up", "metricName": "target_up", "instant": True}]
        )
        try:
            malformed = {
                "metric": {"__name__": "up"},
                "value": [1600000000, "not-a-number"],
            }
            self.prom_cli.process_query.return_value = [malformed]

            result = self._run(path)

            self.assertEqual(result, [])
        finally:
            os.unlink(path)

    def test_non_numeric_sample_value_is_dropped(self):
        path = self._write_profile([{"query": "up", "metricName": "target_up"}])
        try:
            malformed = {
                "metric": {"__name__": "up"},
                "values": [[1600000000, "not-a-number"], [1600000030, "0.5"]],
            }
            self.prom_cli.process_prom_query_in_range.return_value = [malformed]

            result = self._run(path)

            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["value"], 0.5)
        finally:
            os.unlink(path)

    def test_string_sample_timestamp_is_dropped(self):
        path = self._write_profile([{"query": "up", "metricName": "target_up"}])
        try:
            malformed = {
                "metric": {"__name__": "up"},
                "values": [["1600000000", "1"], [1600000030, "0.5"]],
            }
            self.prom_cli.process_prom_query_in_range.return_value = [malformed]

            result = self._run(path)

            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["value"], 0.5)
        finally:
            os.unlink(path)

    def test_nan_sample_value_is_forwarded(self):
        # Characterization: float("NaN") parses fine, so NaN samples are
        # appended and reach elasticsearch / the local metrics file.
        path = self._write_profile([{"query": "up", "metricName": "target_up"}])
        try:
            nan_result = {
                "metric": {"__name__": "up"},
                "values": [[1600000000, "NaN"]],
            }
            self.prom_cli.process_prom_query_in_range.return_value = [nan_result]

            result = self._run(path)

            self.assertEqual(len(result), 1)
            self.assertNotEqual(result[0]["value"], result[0]["value"])
        finally:
            os.unlink(path)

    def test_result_without_samples_is_skipped(self):
        path = self._write_profile([{"query": "up", "metricName": "target_up"}])
        try:
            self.prom_cli.process_prom_query_in_range.return_value = [
                {"metric": {"__name__": "up"}}
            ]

            result = self._run(path)

            self.assertEqual(result, [])
        finally:
            os.unlink(path)

    def test_empty_values_list_is_skipped(self):
        path = self._write_profile([{"query": "up", "metricName": "target_up"}])
        try:
            self.prom_cli.process_prom_query_in_range.return_value = [
                {"metric": {"__name__": "up"}, "values": []}
            ]

            result = self._run(path)

            self.assertEqual(result, [])
        finally:
            os.unlink(path)

    def test_result_without_metric_key_is_skipped(self):
        path = self._write_profile(
            [{"query": "up", "metricName": "target_up", "instant": True}]
        )
        try:
            self.prom_cli.process_query.return_value = [{"value": [1600000000, "1"]}]

            result = self._run(path)

            self.assertEqual(result, [])
        finally:
            os.unlink(path)

    def test_metric_labels_override_query_keys(self):
        path = self._write_profile([{"query": "up", "metricName": "target_up"}])
        try:
            poisoned = {
                "metric": {"query": "evil", "metricName": "evil_name"},
                "values": [[1600000000, "1"]],
            }
            self.prom_cli.process_prom_query_in_range.return_value = [poisoned]

            result = self._run(path)

            self.assertEqual(result[0]["query"], "up")
            self.assertEqual(result[0]["metricName"], "target_up")
        finally:
            os.unlink(path)


class TestMetricsProfileEntries(unittest.TestCase):
    def setUp(self):
        self.prom_cli = MagicMock()
        self.elastic = MagicMock()
        self.elastic.upload_metrics_to_elasticsearch.return_value = 0
        self.telemetry_json = EMPTY_TELEMETRY

    def _write_profile(self, metrics):
        profile = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
        profile.write(json.dumps({"metrics": metrics}))
        profile.close()
        return profile.name

    def _write_raw_profile(self, content):
        profile = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
        profile.write(content)
        profile.close()
        return profile.name

    def _run(self, path, start_time=1000, end_time=1060):
        return client.metrics(
            self.prom_cli,
            self.elastic,
            "run-1",
            start_time,
            end_time,
            path,
            "metrics-idx",
            self.telemetry_json,
        )

    def test_range_query_uses_30s_granularity_and_epoch_boundaries(self):
        path = self._write_profile([{"query": "up", "metricName": "target_up"}])
        try:
            self.prom_cli.process_prom_query_in_range.return_value = []

            self._run(path)

            call = self.prom_cli.process_prom_query_in_range.call_args
            self.assertEqual(call.kwargs["granularity"], 30)
            self.assertEqual(call.kwargs["start_time"].timestamp(), 1000)
            self.assertEqual(call.kwargs["end_time"].timestamp(), 1060)
        finally:
            os.unlink(path)

    def test_range_query_uses_utc_aware_datetimes(self):
        path = self._write_profile([{"query": "up", "metricName": "target_up"}])
        try:
            self.prom_cli.process_prom_query_in_range.return_value = []

            self._run(path)

            call = self.prom_cli.process_prom_query_in_range.call_args
            self.assertEqual(call.kwargs["start_time"].tzinfo, datetime.timezone.utc)
            self.assertEqual(call.kwargs["end_time"].tzinfo, datetime.timezone.utc)
        finally:
            os.unlink(path)

    def test_instant_false_entry_is_dropped(self):
        path = self._write_profile(
            [{"query": "up", "metricName": "target_up", "instant": False}]
        )
        try:
            self._run(path)

            self.prom_cli.process_prom_query_in_range.assert_called_once()
            call = self.prom_cli.process_prom_query_in_range.call_args
            self.assertEqual(call.kwargs["granularity"], 30)
            self.prom_cli.process_query.assert_not_called()
        finally:
            os.unlink(path)

    def test_extra_key_on_range_entry_is_dropped(self):
        path = self._write_profile(
            [{"query": "up", "metricName": "target_up", "threshold": 5}]
        )
        try:
            with self.assertLogs(level="INFO") as logs:
                result = self._run(path)

            self.assertEqual(result, [])
            self.assertIn("didn't match keys", "".join(logs.output))
            self.prom_cli.process_query.assert_not_called()
            self.prom_cli.process_prom_query_in_range.assert_not_called()
        finally:
            os.unlink(path)

    def test_instant_false_string_is_truthy(self):
        # Characterization: YAML users writing instant: "false" get an
        # instant query, because only the key presence and truthiness of
        # the raw value are checked.
        path = self._write_profile(
            [{"query": "up", "metricName": "target_up", "instant": "false"}]
        )
        try:
            self.prom_cli.process_query.return_value = []

            self._run(path)

            self.prom_cli.process_query.assert_called_once_with("up")
            self.prom_cli.process_prom_query_in_range.assert_not_called()
        finally:
            os.unlink(path)

    def test_instant_entry_without_metric_name_is_rejected(self):
        path = self._write_profile([{"query": "up", "instant": True}])
        try:
            self.prom_cli.process_query.return_value = [INSTANT_RESULT]

            result = self._run(path)

            self.assertEqual(result, [])
        finally:
            os.unlink(path)

    def test_elapsed_placeholder_in_instant_query(self):
        # mirrors config/metrics-aggregated.yaml: instant queries may use
        # the .elapsed placeholder too
        path = self._write_profile(
            [
                {
                    "query": "max_over_time(up[.elapsed:])",
                    "metricName": "target_up",
                    "instant": True,
                }
            ]
        )
        try:
            self.prom_cli.process_query.return_value = []

            self._run(path)

            self.prom_cli.process_query.assert_called_once_with(
                "max_over_time(up[1m:])"
            )
        finally:
            os.unlink(path)

    def test_elapsed_placeholder_replaces_all_occurrences(self):
        path = self._write_profile(
            [{"query": "a[.elapsed] + b[.elapsed]", "metricName": "combined"}]
        )
        try:
            self.prom_cli.process_prom_query_in_range.return_value = []

            self._run(path)

            query = self.prom_cli.process_prom_query_in_range.call_args.args[0]
            self.assertEqual(query, "a[1m] + b[1m]")
        finally:
            os.unlink(path)

    def test_elapsed_placeholder_rounds_up_to_whole_minutes(self):
        path = self._write_profile([{"query": "up[.elapsed]", "metricName": "up"}])
        try:
            self.prom_cli.process_prom_query_in_range.return_value = []

            for end_time, expected in ((1000, "up[0m]"), (1030, "up[1m]"), (1061, "up[2m]")):
                with self.subTest(end_time=end_time):
                    self._run(path, end_time=end_time)
                    query = (
                        self.prom_cli.process_prom_query_in_range.call_args.args[0]
                    )
                    self.assertEqual(query, expected)
        finally:
            os.unlink(path)

    def test_entry_without_query_key_is_rejected(self):
        path = self._write_profile([{"metricName": "target_up"}])
        try:
            result = self._run(path)

            self.assertEqual(result, [])
        finally:
            os.unlink(path)

    def test_empty_metrics_list_exits(self):
        path = self._write_profile([])
        try:
            with self.assertRaises(SystemExit) as cm:
                self._run(path)

            self.assertEqual(cm.exception.code, 1)
        finally:
            os.unlink(path)

    def test_empty_profile_file_exits(self):
        path = self._write_raw_profile("")
        try:
            with self.assertRaises(SystemExit) as cm:
                self._run(path)

            self.assertEqual(cm.exception.code, 1)
        finally:
            os.unlink(path)

    def test_profile_without_metrics_key_exits(self):
        path = self._write_raw_profile("foo: bar\n")
        try:
            with self.assertRaises(SystemExit) as cm:
                self._run(path)

            self.assertEqual(cm.exception.code, 1)
        finally:
            os.unlink(path)

    def test_datetime_start_time_raises_type_error(self):
        # Characterization: the metrics contract is Unix timestamps (the
        # caller in run_kraken.py passes ints); a datetime start_time
        # fails on the elapsed-minutes computation.
        path = self._write_profile([{"query": "up", "metricName": "target_up"}])
        try:
            with self.assertRaisesRegex(TypeError, "must be real number, not datetime.timedelta"):
                self._run(
                    path,
                    start_time=datetime.datetime(2026, 10, 2, 15, 0),
                    end_time=datetime.datetime(2026, 10, 2, 15, 1),
                )
        finally:
            os.unlink(path)

    def test_directory_profile_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(SystemExit) as cm:
                self._run(directory)
            self.assertEqual(cm.exception.code, 1)

    def test_string_metric_entry_is_rejected(self):
        path = self._write_profile(["up"])
        try:
            with self.assertRaises(SystemExit) as cm:
                self._run(path)

            self.assertEqual(cm.exception.code, 1)
        finally:
            os.unlink(path)


class TestMetricsTelemetry(unittest.TestCase):
    def setUp(self):
        self.prom_cli = MagicMock()
        self.prom_cli.process_prom_query_in_range.return_value = []
        self.elastic = MagicMock()
        self.elastic.upload_metrics_to_elasticsearch.return_value = 0

    def _write_profile(self):
        profile = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
        profile.write(json.dumps({"metrics": [{"query": "up", "metricName": "up"}]}))
        profile.close()
        return profile.name

    def _telemetry(self, scenarios=None, health_checks=None, virt_checks=None):
        return json.dumps(
            {
                "scenarios": scenarios if scenarios is not None else [],
                "health_checks": health_checks if health_checks is not None else [],
                "virt_checks": virt_checks if virt_checks is not None else [],
            }
        )

    def _run(self, telemetry_json):
        path = self._write_profile()
        try:
            return client.metrics(
                self.prom_cli,
                self.elastic,
                "run-1",
                1000,
                1060,
                path,
                "metrics-idx",
                telemetry_json,
            )
        finally:
            os.unlink(path)

    def test_affected_nodes_entries_collected(self):
        scenario = {
            "affected_pods": {},
            "affected_vmis": {},
            "affected_nodes": [
                {"node_name": "node-1", "total_recovery_time": 5.0}
            ],
        }
        fixed_now = datetime.datetime(2026, 11, 20, 12, 0, 0)

        class fixed_datetime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now

        class fixed_datetime_module:
            timezone = datetime.timezone
            datetime = fixed_datetime

        # datetime.datetime is immutable, so patch the module attribute the
        # client binds instead of the class itself
        with mock.patch.object(client, "datetime", fixed_datetime_module):
            result = self._run(self._telemetry(scenarios=[scenario]))

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["metricName"], "affected_nodes_recovery")
        self.assertEqual(result[0]["node_name"], "node-1")
        self.assertEqual(result[0]["total_recovery_time"], 5.0)
        self.assertEqual(result[0]["timestamp"], str(fixed_now))

    def test_health_check_entries_collected(self):
        result = self._run(
            self._telemetry(health_checks=[{"check": "service", "passed": True}])
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["metricName"], "health_check_recovery")
        self.assertEqual(result[0]["check"], "service")
        self.assertTrue(result[0]["passed"])
        self.assertIn("timestamp", result[0])

    def test_virt_check_entries_collected(self):
        result = self._run(
            self._telemetry(virt_checks=[{"check": "vmi", "passed": True}])
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["metricName"], "virt_check_recovery")
        self.assertEqual(result[0]["check"], "vmi")
        self.assertTrue(result[0]["passed"])
        self.assertIn("timestamp", result[0])

    def test_scenario_without_affected_pods_is_skipped(self):
        scenario = {
            "affected_nodes": [{"node_name": "node-1", "total_recovery_time": 5.0}]
        }

        result = self._run(self._telemetry(scenarios=[scenario]))

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["metricName"], "affected_nodes_recovery")

    def test_scenario_without_affected_nodes_is_skipped(self):
        scenario = {"affected_pods": {}}

        result = self._run(self._telemetry(scenarios=[scenario]))

        self.assertEqual(result, [])

    def test_affected_pods_non_list_value_is_skipped(self):
        scenario = {
            "affected_pods": {"disrupted": "pod-1"},
            "affected_vmis": {},
            "affected_nodes": [],
        }

        result = self._run(self._telemetry(scenarios=[scenario]))

        self.assertEqual(result, [])

    def test_affected_vmis_non_list_value_is_skipped(self):
        scenario = {
            "affected_pods": {},
            "affected_vmis": {"recovered": 3},
            "affected_nodes": [],
        }

        result = self._run(self._telemetry(scenarios=[scenario]))

        self.assertEqual(result, [])

    def test_affected_vmis_entries_collected(self):
        scenario = {
            "affected_pods": {},
            "affected_vmis": {"recovered": [{"vmi_name": "vmi-1"}]},
            "affected_nodes": [],
        }

        result = self._run(self._telemetry(scenarios=[scenario]))

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["metricName"], "affected_vmis_recovery")
        self.assertEqual(result[0]["type"], "recovered")
        self.assertEqual(result[0]["vmi_name"], "vmi-1")
        self.assertIn("timestamp", result[0])

    def test_multiple_pods_produce_one_entry_each(self):
        scenario = {
            "affected_pods": {
                "disrupted": [{"pod_name": "pod-1"}, {"pod_name": "pod-2"}]
            },
            "affected_vmis": {},
            "affected_nodes": [],
        }

        result = self._run(self._telemetry(scenarios=[scenario]))

        self.assertEqual(len(result), 2)
        for entry in result:
            self.assertEqual(entry["metricName"], "affected_pods_recovery")
            self.assertEqual(entry["type"], "disrupted")
            self.assertIn("timestamp", entry)
        self.assertEqual(result[0]["pod_name"], "pod-1")
        self.assertEqual(result[1]["pod_name"], "pod-2")

    def test_invalid_telemetry_json_crashes_after_queries_ran(self):
        # Characterization: telemetry is parsed only after every profile
        # query has already run, so malformed JSON wastes the queries and
        # crashes the run.
        with self.assertRaisesRegex(json.JSONDecodeError, "Expecting value"):
            self._run("not json")

    def test_telemetry_without_scenarios_key_crashes(self):
        # Characterization: run_kraken always passes ChaosRunTelemetry.to_json()
        # which contains "scenarios"; anything else fails with KeyError.
        with self.assertRaisesRegex(KeyError, "scenarios"):
            self._run(json.dumps({"health_checks": [], "virt_checks": []}))

    def test_telemetry_dict_instead_of_string_crashes(self):
        # Characterization: the telemetry contract is a JSON string
        # (run_kraken passes to_json()); a dict fails inside json.loads.
        with self.assertRaisesRegex(TypeError, "must be str, bytes or bytearray, not dict"):
            self._run({"scenarios": [], "health_checks": [], "virt_checks": []})


class _EmptyIndexError(Exception):
    """Stands in for the error KrknElastic raises before its own try/except."""


class TestMetricsPersistence(unittest.TestCase):
    def setUp(self):
        self.prom_cli = MagicMock()
        self.prom_cli.process_prom_query_in_range.return_value = [RANGE_RESULT]
        self.elastic = MagicMock()
        self.elastic.upload_metrics_to_elasticsearch.return_value = 0
        self.telemetry_json = EMPTY_TELEMETRY
        # redirect the local-save directory away from the real /tmp
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        patcher = mock.patch.object(
            client.tempfile, "gettempdir", return_value=self.tmp_dir.name
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_profile(self):
        profile = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
        profile.write(json.dumps({"metrics": [{"query": "up", "metricName": "up"}]}))
        profile.close()
        return profile.name

    def _run(self, use_elastic=True, index="metrics-idx"):
        path = self._write_profile()
        try:
            return client.metrics(
                self.prom_cli,
                self.elastic if use_elastic else None,
                "run-1",
                1000,
                1060,
                path,
                index,
                self.telemetry_json,
            )
        finally:
            os.unlink(path)

    def _local_file(self, name):
        return os.path.join(self.tmp_dir.name, "krkn_metrics", name)

    def test_successful_upload_skips_local_file(self):
        self._run()

        self.elastic.upload_metrics_to_elasticsearch.assert_called_once()
        self.assertFalse(os.path.exists(self._local_file("metrics-idx_run-1.json")))

    def test_upload_failure_falls_back_to_local_file(self):
        self.elastic.upload_metrics_to_elasticsearch.return_value = -1

        with self.assertLogs(level="ERROR") as logs:
            result = self._run()

        self.assertIn(
            "failed to save metrics on ElasticSearch", "".join(logs.output)
        )
        self.elastic.upload_metrics_to_elasticsearch.assert_called_once_with(
            run_uuid="run-1", index="metrics-idx", raw_data=result
        )
        local_file = self._local_file("metrics-idx_run-1.json")
        self.assertTrue(os.path.exists(local_file))
        with open(local_file) as f:
            saved = json.load(f)
        self.assertEqual(saved["run_uuid"], "run-1")
        self.assertEqual(saved["metrics"], result)
        self.assertEqual(len(result), 2)

    def test_missing_elastic_client_saves_metrics_locally(self):
        result = self._run(use_elastic=False)

        self.elastic.upload_metrics_to_elasticsearch.assert_not_called()
        local_file = self._local_file("metrics-idx_run-1.json")
        self.assertTrue(os.path.exists(local_file))
        with open(local_file) as f:
            saved = json.load(f)
        self.assertEqual(saved["metrics"], result)

    def test_missing_index_skips_upload_and_saves_locally(self):
        # Characterization: a missing index disables the upload and the
        # metrics are written to the local fallback directory.
        self._run(index=None)

        self.elastic.upload_metrics_to_elasticsearch.assert_not_called()
        local_files = os.listdir(os.path.join(self.tmp_dir.name, "krkn_metrics"))
        self.assertEqual(len(local_files), 1)
        self.assertTrue(local_files[0].endswith("_run-1.json"))

    def test_missing_index_local_filename_has_no_none(self):
        self._run(index=None)

        local_files = os.listdir(os.path.join(self.tmp_dir.name, "krkn_metrics"))
        self.assertEqual(len(local_files), 1)
        self.assertNotIn("None", local_files[0])

    def test_empty_index_falls_back_to_local_file(self):
        # mirrors KrknElastic.upload_metrics_to_elasticsearch, which raises
        # before its own try/except when the index is empty
        self.elastic.upload_metrics_to_elasticsearch.side_effect = _EmptyIndexError(
            "index cannot be None or empty"
        )

        self._run(index="")

        self.assertTrue(os.path.exists(self._local_file("_run-1.json")))

    def test_local_write_failure_logs_error_and_returns_metrics(self):
        real_open = open

        def failing_open(file, *args, **kwargs):
            if "krkn_metrics" in str(file):
                raise OSError("disk full")
            return real_open(file, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=failing_open):
            with self.assertLogs(level="ERROR") as logs:
                result = self._run(use_elastic=False)

        self.assertIn("Failed to save metrics to", "".join(logs.output))
        self.assertEqual(len(result), 2)


if __name__ == "__main__":
    unittest.main()
