#!/usr/bin/env python3

"""
Test suite for ServiceDisruptionScenarioPlugin class

Usage:
    python -m coverage run -a -m unittest tests/test_service_disruption_scenario_plugin.py -v

Assisted By: Claude Code
"""

import logging
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import yaml
from krkn_lib.k8s import KrknKubernetes
from krkn_lib.models.telemetry import ScenarioTelemetry
from krkn_lib.telemetry.ocp import KrknTelemetryOpenshift

from krkn.scenario_plugins.service_disruption.service_disruption_scenario_plugin import ServiceDisruptionScenarioPlugin


class TestServiceDisruptionScenarioPlugin(unittest.TestCase):

    def setUp(self):
        """
        Set up test fixtures for ServiceDisruptionScenarioPlugin
        """
        self.plugin = ServiceDisruptionScenarioPlugin()

    def test_get_scenario_types(self):
        """
        Test get_scenario_types returns correct scenario type
        """
        result = self.plugin.get_scenario_types()

        self.assertEqual(result, ["service_disruption_scenarios"])
        self.assertEqual(len(result), 1)

# ---------------------------------------------------------------------------
# Tests for run(), delete_objects() and the delete_all_*_namespace helpers.
# Boundary (krkn-lib kube client, time.sleep, random) is mocked; the plugin
# logic itself runs unmodified.
# ---------------------------------------------------------------------------

# (plugin helper, kubecli getter, kubecli deleter) for the five clones
DELETE_HELPERS = [
    ("delete_all_deployment_namespace", "get_deployment_ns", "delete_deployment"),
    ("delete_all_daemonset_namespace", "get_daemonset", "delete_daemonset"),
    ("delete_all_statefulsets_namespace", "get_all_statefulset", "delete_statefulset"),
    ("delete_all_replicaset_namespace", "get_all_replicasets", "delete_replicaset"),
    ("delete_all_services_namespace", "get_all_services", "delete_services"),
]


class ServiceDisruptionTestBase(unittest.TestCase):
    """Shared fixtures: plugin, mocked krkn-lib boundary, temp scenario YAML."""

    def setUp(self):
        self.plugin = ServiceDisruptionScenarioPlugin()
        self.kubecli = MagicMock(spec=KrknKubernetes)
        self.lib_telemetry = MagicMock(spec=KrknTelemetryOpenshift)
        self.lib_telemetry.get_lib_kubernetes.return_value = self.kubecli
        self.scenario_telemetry = MagicMock(spec=ScenarioTelemetry)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def write_scenario(self, scenarios):
        """Write {'scenarios': [...]} to a temp YAML file; return its path."""
        path = os.path.join(self.tmpdir.name, "scenario.yaml")
        with open(path, "w") as f:
            yaml.safe_dump({"scenarios": scenarios}, f)
        return path

    def run_plugin(self, scenarios):
        """Run the plugin on a scenario file with time.sleep patched."""
        path = self.write_scenario(scenarios)
        with patch("time.sleep") as sleep:
            result = self.plugin.run(
                "run-uuid", path, self.lib_telemetry, self.scenario_telemetry
            )
        return result, sleep

    def set_namespace_contents(
        self,
        deployments=(),
        daemonsets=(),
        statefulsets=(),
        replicasets=(),
        services=(),
    ):
        """Configure the mocked kube client with per-kind object name lists."""
        self.kubecli.get_deployment_ns.return_value = list(deployments)
        self.kubecli.get_daemonset.return_value = list(daemonsets)
        self.kubecli.get_all_statefulset.return_value = list(statefulsets)
        self.kubecli.get_all_replicasets.return_value = list(replicasets)
        self.kubecli.get_all_services.return_value = list(services)


class TestDeleteAllNamespaceHelpers(ServiceDisruptionTestBase):
    """Same behavior matrix applied to each of the five delete_all_* clones."""

    def test_deletes_every_object_with_correct_arguments(self):
        for helper, getter, deleter in DELETE_HELPERS:
            with self.subTest(helper=helper):
                getattr(self.kubecli, getter).return_value = ["obj-1", "obj-2"]
                result = getattr(self.plugin, helper)(self.kubecli, "target-ns")
                self.assertEqual(result, ["obj-1", "obj-2"])
                getattr(self.kubecli, getter).assert_called_once_with("target-ns")
                self.assertEqual(
                    getattr(self.kubecli, deleter).call_args_list,
                    [call("obj-1", "target-ns"), call("obj-2", "target-ns")],
                )

    def test_empty_namespace_returns_empty_list_without_deletes(self):
        for helper, getter, deleter in DELETE_HELPERS:
            with self.subTest(helper=helper):
                getattr(self.kubecli, getter).return_value = []
                result = getattr(self.plugin, helper)(self.kubecli, "target-ns")
                self.assertEqual(result, [])
                getattr(self.kubecli, deleter).assert_not_called()

    def test_getter_returning_none_raises_type_error(self):
        for helper, getter, deleter in DELETE_HELPERS:
            with self.subTest(helper=helper):
                getattr(self.kubecli, getter).return_value = None
                with self.assertLogs(level=logging.ERROR) as logs:
                    with self.assertRaises(TypeError):
                        getattr(self.plugin, helper)(self.kubecli, "target-ns")
                self.assertIn(helper, "".join(logs.output))

    def test_delete_failure_mid_list_reraises_and_keeps_prior_deletes(self):
        for helper, getter, deleter in DELETE_HELPERS:
            with self.subTest(helper=helper):
                getattr(self.kubecli, getter).return_value = ["obj-1", "obj-2"]
                getattr(self.kubecli, deleter).side_effect = [
                    None,
                    RuntimeError("delete boom"),
                ]
                with self.assertLogs(level=logging.ERROR):
                    with self.assertRaises(RuntimeError):
                        getattr(self.plugin, helper)(self.kubecli, "target-ns")
                self.assertEqual(getattr(self.kubecli, deleter).call_count, 2)

    def test_only_own_getter_and_deleter_pair_is_called(self):
        all_boundary_methods = {g for _, g, _ in DELETE_HELPERS} | {
            d for _, _, d in DELETE_HELPERS
        }
        for helper, getter, deleter in DELETE_HELPERS:
            with self.subTest(helper=helper):
                self.kubecli.reset_mock()
                getattr(self.kubecli, getter).return_value = ["obj-1"]
                getattr(self.plugin, helper)(self.kubecli, "target-ns")
                for method in sorted(all_boundary_methods):
                    expected = 1 if method in (getter, deleter) else 0
                    self.assertEqual(
                        getattr(self.kubecli, method).call_count,
                        expected,
                        method,
                    )


class TestDeleteObjects(ServiceDisruptionTestBase):
    """Tests for delete_objects() orchestration over the delete_all_* helpers."""

    def test_deletes_services_first_then_workloads_in_order(self):
        order = []
        self.kubecli.delete_services.side_effect = (
            lambda name, ns: order.append("services")
        )
        self.kubecli.delete_daemonset.side_effect = (
            lambda name, ns: order.append("daemonsets")
        )
        self.kubecli.delete_statefulset.side_effect = (
            lambda name, ns: order.append("statefulsets")
        )
        self.kubecli.delete_replicaset.side_effect = (
            lambda name, ns: order.append("replicasets")
        )
        self.kubecli.delete_deployment.side_effect = (
            lambda name, ns: order.append("deployments")
        )
        self.set_namespace_contents(
            deployments=["dep-1"],
            daemonsets=["ds-1"],
            statefulsets=["ss-1"],
            replicasets=["rs-1"],
            services=["svc-1"],
        )

        objects = self.plugin.delete_objects(self.kubecli, "target-ns")

        self.assertEqual(
            order,
            ["services", "daemonsets", "statefulsets", "replicasets", "deployments"],
        )
        self.assertEqual(
            objects,
            {
                "daemonsets": ["ds-1"],
                "deployments": ["dep-1"],
                "replicasets": ["rs-1"],
                "statefulsets": ["ss-1"],
                "services": ["svc-1"],
            },
        )
        self.kubecli.delete_services.assert_called_once_with("svc-1", "target-ns")
        self.kubecli.delete_deployment.assert_called_once_with("dep-1", "target-ns")

    def test_empty_namespace_returns_dict_of_empty_lists(self):
        self.set_namespace_contents()

        objects = self.plugin.delete_objects(self.kubecli, "target-ns")

        self.assertEqual(
            objects,
            {
                "daemonsets": [],
                "deployments": [],
                "replicasets": [],
                "statefulsets": [],
                "services": [],
            },
        )
        self.kubecli.delete_services.assert_not_called()
        self.kubecli.delete_deployment.assert_not_called()

    def test_daemonset_failure_propagates_after_services_deleted(self):
        self.set_namespace_contents(services=["svc-1"])
        self.kubecli.get_daemonset.side_effect = RuntimeError("daemonset boom")

        with self.assertLogs(level=logging.ERROR):
            with self.assertRaises(RuntimeError):
                self.plugin.delete_objects(self.kubecli, "target-ns")

        # services are deleted before the failure and are not rolled back
        self.kubecli.delete_services.assert_called_once_with("svc-1", "target-ns")

    def test_helper_failure_short_circuits_remaining_kinds(self):
        self.set_namespace_contents(
            deployments=["dep-1"],
            statefulsets=["ss-1"],
            replicasets=["rs-1"],
        )
        self.kubecli.get_daemonset.side_effect = RuntimeError("daemonset boom")

        with self.assertLogs(level=logging.ERROR):
            with self.assertRaises(RuntimeError):
                self.plugin.delete_objects(self.kubecli, "target-ns")

        self.kubecli.get_all_statefulset.assert_not_called()
        self.kubecli.get_all_replicasets.assert_not_called()
        self.kubecli.get_deployment_ns.assert_not_called()


class TestGetListRunningPods(ServiceDisruptionTestBase):
    """Tests for get_list_running_pods() filtering logic."""

    def test_returns_only_pods_in_running_phase(self):
        self.kubecli.list_pods.return_value = ["p1", "p2", "p3"]
        self.kubecli.get_pod_info.side_effect = [
            SimpleNamespace(status="Running"),
            SimpleNamespace(status="Pending"),
            SimpleNamespace(status="Succeeded"),
        ]

        running = self.plugin.get_list_running_pods(self.kubecli, "target-ns")

        self.assertEqual(running, ["p1"])
        self.kubecli.get_pod_info.assert_any_call("p1", "target-ns")

    def test_skips_pods_whose_info_is_none(self):
        self.kubecli.list_pods.return_value = ["gone", "p2"]
        self.kubecli.get_pod_info.side_effect = [
            None,
            SimpleNamespace(status="Running"),
        ]

        running = self.plugin.get_list_running_pods(self.kubecli, "target-ns")

        self.assertEqual(running, ["p2"])

    def test_empty_pod_list_returns_empty_list(self):
        self.kubecli.list_pods.return_value = []

        running = self.plugin.get_list_running_pods(self.kubecli, "target-ns")

        self.assertEqual(running, [])
        self.kubecli.get_pod_info.assert_not_called()

    def test_truthy_pod_info_with_non_running_status_is_excluded(self):
        # a truthy info object whose .status is not "Running" must be excluded:
        # the filter reads the .status attribute, not the truthiness of the info
        self.kubecli.list_pods.return_value = ["p1"]
        self.kubecli.get_pod_info.return_value = SimpleNamespace(status="Failed")

        running = self.plugin.get_list_running_pods(self.kubecli, "target-ns")

        self.assertEqual(running, [])


class RunTestBase(ServiceDisruptionTestBase):
    """Base for run() tests: helpers for raw YAML docs and arbitrary paths."""

    def write_yaml_doc(self, doc):
        """Write an arbitrary YAML document to a temp file; return its path."""
        path = os.path.join(self.tmpdir.name, "doc.yaml")
        with open(path, "w") as f:
            yaml.safe_dump(doc, f)
        return path

    def run_path(self, path):
        """Run the plugin on the given path with time.sleep patched."""
        with patch("time.sleep") as sleep:
            result = self.plugin.run(
                "run-uuid", path, self.lib_telemetry, self.scenario_telemetry
            )
        return result, sleep


class TestRunConfigValidation(RunTestBase):
    """Tests for run() scenario configuration validation."""

    def test_run_rejects_namespace_and_label_selector_both_set(self):
        with self.assertLogs(level=logging.ERROR) as logs:
            result, _ = self.run_plugin(
                [{"namespace": "ns1", "label_selector": "app=x"}]
            )

        self.assertEqual(result, 1)
        self.assertEqual(len(logs.output), 3)
        self.assertIn("only have namespace or label", "".join(logs.output))
        self.kubecli.check_namespaces.assert_not_called()
        self.kubecli.delete_services.assert_not_called()
        self.kubecli.delete_deployment.assert_not_called()

    def test_run_both_blank_selects_every_namespace(self):
        # neither namespace nor label_selector set: run() must reject this
        # instead of handing an empty regex and empty selector to krkn-lib
        self.kubecli.check_namespaces.return_value = ["ns1"]
        self.set_namespace_contents(services=["svc-1"])

        result, _ = self.run_plugin([{"namespace": "", "label_selector": ""}])

        self.assertEqual(result, 1)
        self.kubecli.delete_services.assert_not_called()
        self.kubecli.delete_daemonset.assert_not_called()
        self.kubecli.delete_statefulset.assert_not_called()
        self.kubecli.delete_replicaset.assert_not_called()
        self.kubecli.delete_deployment.assert_not_called()

    def test_run_whitespace_only_namespace_skips_conflict_check_and_forwards_raw_value(self):
        self.kubecli.check_namespaces.return_value = ["ns1"]

        result, _ = self.run_plugin([{"namespace": "   ", "label_selector": "app=x"}])

        self.assertEqual(result, 0)
        self.kubecli.check_namespaces.assert_called_once_with(["   "], "app=x")

    def test_run_non_string_namespace_returns_error(self):
        for bad_namespace in [123, False, ["a", "b"]]:
            with self.subTest(namespace=bad_namespace):
                with self.assertLogs(level=logging.ERROR) as logs:
                    result, _ = self.run_plugin([{"namespace": bad_namespace}])
                self.assertEqual(result, 1)
                self.assertIn("exiting due to Exception", "".join(logs.output))
                self.kubecli.check_namespaces.assert_not_called()


class TestRunScenarioFileHandling(RunTestBase):
    """Tests for run() scenario file loading and config value handling."""

    def test_run_missing_scenario_file_returns_error(self):
        missing = os.path.join(self.tmpdir.name, "does-not-exist.yaml")

        with self.assertLogs(level=logging.ERROR) as logs:
            result, _ = self.run_path(missing)

        self.assertEqual(result, 1)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("exiting due to Exception", "".join(logs.output))
        self.kubecli.check_namespaces.assert_not_called()

    def test_run_empty_scenario_file_returns_error(self):
        path = os.path.join(self.tmpdir.name, "empty.yaml")
        with open(path, "w") as f:
            f.write("")

        with self.assertLogs(level=logging.ERROR):
            result, _ = self.run_path(path)

        self.assertEqual(result, 1)
        self.kubecli.check_namespaces.assert_not_called()

    def test_run_scenario_file_without_scenarios_key_returns_error(self):
        path = self.write_yaml_doc({"something-else": True})

        with self.assertLogs(level=logging.ERROR):
            result, _ = self.run_path(path)

        self.assertEqual(result, 1)
        self.kubecli.check_namespaces.assert_not_called()

    def test_run_empty_scenarios_list_returns_success_without_calls(self):
        result, _ = self.run_plugin([])

        self.assertEqual(result, 0)
        self.kubecli.check_namespaces.assert_not_called()
        self.kubecli.delete_services.assert_not_called()

    def test_run_runs_zero_skips_execution(self):
        result, _ = self.run_plugin([{"namespace": "ns1", "runs": 0}])

        self.assertEqual(result, 0)
        self.kubecli.check_namespaces.assert_not_called()

    def test_run_delete_count_zero_still_lists_namespaces(self):
        self.kubecli.check_namespaces.return_value = ["ns1"]

        result, sleep = self.run_plugin([{"namespace": "ns1", "delete_count": 0}])

        self.assertEqual(result, 0)
        self.kubecli.check_namespaces.assert_called_once_with(["ns1"], "")
        self.kubecli.delete_services.assert_not_called()
        sleep.assert_not_called()

    def test_run_non_integer_runs_or_delete_count_returns_error(self):
        for scenario in [{"runs": "3"}, {"delete_count": 1.5}]:
            with self.subTest(scenario=scenario):
                self.kubecli.check_namespaces.return_value = ["ns1"]
                with self.assertLogs(level=logging.ERROR):
                    result, _ = self.run_plugin(
                        [{"namespace": "ns1", **scenario}]
                    )
                self.assertEqual(result, 1)
                self.kubecli.delete_services.assert_not_called()

    def test_run_null_runs_falls_back_to_default_one(self):
        self.kubecli.check_namespaces.return_value = ["ns1"]

        result, _ = self.run_plugin([{"namespace": "ns1", "runs": None}])

        self.assertEqual(result, 0)
        self.kubecli.check_namespaces.assert_called_once_with(["ns1"], "")

    def test_run_invalid_sleep_value_returns_error(self):
        # time.sleep(-1) raises ValueError and time.sleep("10") raises
        # TypeError in the stdlib; the side effects mirror that contract
        # without real sleeping
        for sleep_value, error in [(-1, ValueError), ("10", TypeError)]:
            with self.subTest(sleep=sleep_value):
                self.kubecli.check_namespaces.return_value = ["ns1"]
                self.set_namespace_contents(services=["svc-1"])
                path = self.write_scenario(
                    [{"namespace": "ns1", "sleep": sleep_value}]
                )
                # the sleep mock raises, so this proves run() converts a sleep exception into return 1
                with patch("time.sleep", side_effect=error("bad sleep")):
                    with self.assertLogs(level=logging.ERROR):
                        result = self.plugin.run(
                            "run-uuid",
                            path,
                            self.lib_telemetry,
                            self.scenario_telemetry,
                        )
                self.assertEqual(result, 1)

    def test_run_second_invalid_scenario_aborts_after_first_executed(self):
        self.kubecli.check_namespaces.return_value = ["ns1"]
        self.set_namespace_contents(services=["svc-1"])

        with self.assertLogs(level=logging.ERROR):
            result, _ = self.run_plugin(
                [
                    {"namespace": "ns1"},
                    {"namespace": "ns1", "label_selector": "app=x"},
                ]
            )

        self.assertEqual(result, 1)
        self.kubecli.check_namespaces.assert_called_once_with(["ns1"], "")
        self.kubecli.delete_services.assert_called_once_with("svc-1", "ns1")


class TestRunExecution(RunTestBase):
    """Tests for run() namespace selection and deletion execution."""

    def test_run_happy_path_deletes_objects_and_sleeps_between_deletions(self):
        self.kubecli.check_namespaces.return_value = ["ns1"]
        self.set_namespace_contents(
            deployments=["dep-1"],
            daemonsets=["ds-1"],
            statefulsets=["ss-1"],
            replicasets=["rs-1"],
            services=["svc-1"],
        )

        result, sleep = self.run_plugin([{"namespace": "ns1", "sleep": 7}])

        self.assertEqual(result, 0)
        self.kubecli.check_namespaces.assert_called_once_with(["ns1"], "")
        self.kubecli.delete_services.assert_called_once_with("svc-1", "ns1")
        self.kubecli.delete_daemonset.assert_called_once_with("ds-1", "ns1")
        self.kubecli.delete_statefulset.assert_called_once_with("ss-1", "ns1")
        self.kubecli.delete_replicaset.assert_called_once_with("rs-1", "ns1")
        self.kubecli.delete_deployment.assert_called_once_with("dep-1", "ns1")
        # trailing sleep after the only deletion (default runs=1, delete_count=1)
        sleep.assert_called_once_with(7)

    def test_run_label_only_selection_applies_default_counts(self):
        self.kubecli.check_namespaces.return_value = ["ns1", "ns2"]
        self.set_namespace_contents(services=["svc-1"])

        with patch("random.randint", return_value=0):
            result, sleep = self.run_plugin(
                [{"namespace": "", "label_selector": "app=x"}]
            )

        self.assertEqual(result, 0)
        self.kubecli.check_namespaces.assert_called_once_with([""], "app=x")
        # defaults: delete_count=1 (only one of the two matches deleted),
        # runs=1, sleep=10
        self.kubecli.delete_services.assert_called_once_with("svc-1", "ns1")
        sleep.assert_called_once_with(10)

    def test_run_selected_namespace_removed_from_pool_before_next_pick(self):
        self.kubecli.check_namespaces.return_value = ["ns1", "ns2", "ns3"]
        self.set_namespace_contents(services=["svc-1"])

        with patch("random.randint", side_effect=[0, 1]) as randint:
            result, sleep = self.run_plugin(
                [{"namespace": "ns1", "delete_count": 2}]
            )

        self.assertEqual(result, 0)
        self.assertEqual(randint.call_args_list, [call(0, 2), call(0, 1)])
        # first pick: index 0 of [ns1, ns2, ns3] -> ns1; after removal the
        # second pick is index 1 of [ns2, ns3] -> ns3; ns2 is never touched
        self.assertEqual(
            self.kubecli.delete_services.call_args_list,
            [call("svc-1", "ns1"), call("svc-1", "ns3")],
        )
        self.assertEqual(sleep.call_count, 2)

    def test_run_delete_count_exceeding_namespaces_returns_error(self):
        self.kubecli.check_namespaces.return_value = ["ns1"]
        self.set_namespace_contents(services=["svc-1"])

        with self.assertLogs(level=logging.ERROR) as logs:
            result, sleep = self.run_plugin(
                [{"namespace": "ns1", "delete_count": 2}]
            )

        self.assertEqual(result, 1)
        self.assertIn("Couldn't delete", "".join(logs.output))
        # the only matching namespace was fully deleted before failing
        self.kubecli.delete_services.assert_called_once_with("svc-1", "ns1")
        sleep.assert_called_once_with(10)



class TestRunFailureHandling(RunTestBase):
    """Tests for run() failure paths."""

    def test_run_not_enough_namespaces_error_reports_delete_count(self):
        self.kubecli.check_namespaces.return_value = ["ns1"]
        self.set_namespace_contents(services=["svc-1"])

        with self.assertLogs(level=logging.ERROR) as logs:
            result, _ = self.run_plugin(
                [{"namespace": "ns1", "delete_count": 3, "runs": 1}]
            )

        self.assertEqual(result, 1)
        self.assertIn("Couldn't delete 3 namespaces", "".join(logs.output))

    def test_run_check_namespaces_failure_returns_error(self):
        self.kubecli.check_namespaces.side_effect = RuntimeError("bad label")

        with self.assertLogs(level=logging.ERROR) as logs:
            result, _ = self.run_plugin([{"namespace": "ns1"}])

        self.assertEqual(result, 1)
        self.assertIn("exiting due to Exception", "".join(logs.output))
        self.kubecli.delete_services.assert_not_called()

    def test_run_delete_objects_failure_returns_error_without_rollback(self):
        self.kubecli.check_namespaces.return_value = ["ns1"]
        self.set_namespace_contents(services=["svc-1"])
        self.kubecli.get_daemonset.side_effect = RuntimeError("daemonset boom")

        with self.assertLogs(level=logging.INFO) as logs:
            result, sleep = self.run_plugin([{"namespace": "ns1"}])

        self.assertEqual(result, 1)
        output = "".join(logs.output)
        self.assertIn("was unsuccessful", output)
        self.assertIn("Namespace action error:", output)
        # services were deleted before the failure and are not rolled back
        self.kubecli.delete_services.assert_called_once_with("svc-1", "ns1")
        sleep.assert_not_called()

    def test_run_multiple_runs_requery_namespaces(self):
        self.kubecli.check_namespaces.return_value = ["ns1", "ns2"]
        self.set_namespace_contents(services=["svc-1"])

        with patch("random.randint", return_value=0):
            result, sleep = self.run_plugin([{"namespace": "ns1", "runs": 2}])

        self.assertEqual(result, 0)
        self.assertEqual(self.kubecli.check_namespaces.call_count, 2)
        self.assertEqual(
            self.kubecli.delete_services.call_args_list,
            [call("svc-1", "ns1"), call("svc-1", "ns2")],
        )
        self.assertEqual(sleep.call_count, 2)

    def test_run_never_waits_for_object_recovery(self):
        self.kubecli.check_namespaces.return_value = ["ns1"]
        self.set_namespace_contents(services=["svc-1"])

        with patch.object(self.plugin, "check_all_running_deployment") as deployment_check:
            result, _ = self.run_plugin([{"namespace": "ns1", "wait_time": 30}])

        self.assertEqual(result, 0)
        deployment_check.assert_called_once_with(
            {
                "ns1": {
                    "daemonsets": [],
                    "deployments": [],
                    "replicasets": [],
                    "statefulsets": [],
                    "services": ["svc-1"],
                }
            },
            30,
            self.kubecli,
        )


class TestCheckAllRunningPods(ServiceDisruptionTestBase):
    """Tests for check_all_running_pods() wait loop."""

    @staticmethod
    def _pod_info(status, name):
        return SimpleNamespace(status=status, name=name)

    def test_all_pods_running_or_succeeded_returns_without_sleeping(self):
        self.kubecli.list_pods.return_value = ["p1", "p2"]
        self.kubecli.get_pod_info.side_effect = [
            self._pod_info("Running", "p1"),
            self._pod_info("Succeeded", "p2"),
        ]

        with patch("time.sleep") as sleep:
            result = self.plugin.check_all_running_pods(
                self.kubecli, "target-ns", 30
            )

        self.assertIsNone(result)
        sleep.assert_not_called()

    def test_pod_vanishing_mid_wait_is_treated_as_not_running(self):
        self.kubecli.list_pods.return_value = ["p1", "p2"]
        self.kubecli.get_pod_info.side_effect = [
            self._pod_info("Running", "p1"),
            None,
            self._pod_info("Running", "p1"),
            self._pod_info("Running", "p2"),
        ]

        with patch("time.sleep") as sleep:
            result = self.plugin.check_all_running_pods(
                self.kubecli, "target-ns", 10
            )

        self.assertIsNone(result)
        sleep.assert_called_once_with(5)

    def test_pods_recovering_after_delay_stop_the_wait(self):
        self.kubecli.list_pods.return_value = ["p1", "p2"]
        self.kubecli.get_pod_info.side_effect = [
            self._pod_info("Pending", "p1"),
            self._pod_info("Running", "p1"),
            self._pod_info("Running", "p2"),
        ]

        with patch("time.sleep") as sleep:
            result = self.plugin.check_all_running_pods(
                self.kubecli, "target-ns", 30
            )

        self.assertIsNone(result)
        sleep.assert_called_once_with(5)

    def test_wait_time_expiry_exits_silently_without_error(self):
        self.kubecli.list_pods.return_value = ["p1"]
        self.kubecli.get_pod_info.return_value = self._pod_info("Pending", "p1")

        with patch("time.sleep") as sleep:
            result = self.plugin.check_all_running_pods(
                self.kubecli, "target-ns", 10
            )

        self.assertIsNone(result)
        self.assertEqual(sleep.call_count, 2)
        sleep.assert_has_calls([call(5), call(5)])

    def test_wait_time_zero_returns_without_checking_pods(self):
        with patch("time.sleep") as sleep:
            result = self.plugin.check_all_running_pods(
                self.kubecli, "target-ns", 0
            )

        self.assertIsNone(result)
        self.kubecli.list_pods.assert_not_called()
        sleep.assert_not_called()

    def test_empty_pod_list_returns_immediately(self):
        self.kubecli.list_pods.return_value = []

        with patch("time.sleep") as sleep:
            result = self.plugin.check_all_running_pods(
                self.kubecli, "target-ns", 30
            )

        self.assertIsNone(result)
        sleep.assert_not_called()

    def test_wait_time_not_multiple_of_five_overshoots(self):
        # 12s wait_time polls at timer 0, 5 and 10: 15s of sleep in total
        self.kubecli.list_pods.return_value = ["p1"]
        self.kubecli.get_pod_info.return_value = self._pod_info("Pending", "p1")

        with patch("time.sleep") as sleep:
            self.plugin.check_all_running_pods(self.kubecli, "target-ns", 12)

        self.assertEqual(sleep.call_count, 3)

    def test_non_running_pod_short_circuits_iteration(self):
        # p2 not running stops the for-loop: p3 is not polled that iteration
        self.kubecli.list_pods.return_value = ["p1", "p2", "p3"]
        self.kubecli.get_pod_info.side_effect = [
            self._pod_info("Running", "p1"),
            self._pod_info("Pending", "p2"),
        ]

        with patch("time.sleep") as sleep:
            self.plugin.check_all_running_pods(self.kubecli, "target-ns", 5)

        self.assertEqual(self.kubecli.get_pod_info.call_count, 2)
        sleep.assert_called_once_with(5)


class TestCheckAllRunningDeployment(ServiceDisruptionTestBase):
    """Tests for check_all_running_deployment() recovery wait loop."""

    def check_deployment(self, killed_namespaces, wait_time):
        """Run check_all_running_deployment with sleep and pod-wait mocked."""
        with patch.object(
            self.plugin, "check_all_running_pods"
        ) as pods_check, patch("time.sleep") as sleep:
            result = self.plugin.check_all_running_deployment(
                killed_namespaces, wait_time, self.kubecli
            )
        return result, pods_check, sleep

    def test_all_objects_restored_returns_empty_list_and_waits_for_pods(self):
        killed = {"ns1": {"deployments": ["d1"], "services": ["s1"]}}
        self.kubecli.get_deployment_ns.return_value = ["d1"]
        self.kubecli.get_all_services.return_value = ["s1"]

        result, pods_check, sleep = self.check_deployment(killed, 30)

        self.assertEqual(result, [])
        # the same wait_time is delegated to the pod check, not a remainder
        pods_check.assert_called_once_with(self.kubecli, "ns1", 30)
        sleep.assert_not_called()

    def test_recovery_after_one_retry_round_sleeps_ten_seconds(self):
        killed = {"ns1": {"deployments": ["d1"]}}
        self.kubecli.get_deployment_ns.side_effect = [[], ["d1"]]

        result, pods_check, sleep = self.check_deployment(killed, 30)

        self.assertEqual(result, [])
        sleep.assert_called_once_with(10)
        pods_check.assert_called_once_with(self.kubecli, "ns1", 30)

    def test_timeout_returns_missing_namespaces_with_error_logs(self):
        killed = {"ns1": {"deployments": ["d1"]}}
        self.kubecli.get_deployment_ns.return_value = []

        with self.assertLogs(level=logging.ERROR) as logs:
            result, pods_check, sleep = self.check_deployment(killed, 20)

        self.assertEqual(result, {"ns1": {"deployments": ["d1"]}})
        output = "".join(logs.output)
        self.assertIn("still not ready", output)
        self.assertIn("Non active namespaces", output)
        pods_check.assert_not_called()
        self.assertEqual(sleep.call_count, 2)

    def test_more_current_objects_than_killed_does_not_recover(self):
        # recovery is strict equality: a surplus of current objects must not
        # count as recovered (a '<=' comparison would wrongly pass here)
        killed = {"ns1": {"deployments": ["d1"]}}
        self.kubecli.get_deployment_ns.return_value = ["d1", "d2"]

        with self.assertLogs(level=logging.ERROR):
            result, pods_check, sleep = self.check_deployment(killed, 20)

        self.assertEqual(result, {"ns1": {"deployments": ["d1"]}})
        pods_check.assert_not_called()
        self.assertEqual(sleep.call_count, 2)

    def test_recovery_by_count_ignores_object_names(self):
        # characterization test: current recovery is count-equality only
        # recovery is decided by len(killed) == len(current) only: a
        # differently-named replacement object counts as recreated
        killed = {"ns1": {"deployments": ["old-dep"]}}
        self.kubecli.get_deployment_ns.return_value = ["new-dep"]

        result, pods_check, sleep = self.check_deployment(killed, 30)

        self.assertEqual(result, [])
        pods_check.assert_called_once_with(self.kubecli, "ns1", 30)
        sleep.assert_not_called()

    def test_unknown_object_key_never_recovers(self):
        # no elif branch matches 'customkind': it is never popped from the
        # missing objects, so the namespace can never be considered ready
        killed = {"ns1": {"customkind": ["x"], "deployments": ["d1"]}}
        self.kubecli.get_deployment_ns.return_value = ["d1"]

        with self.assertLogs(level=logging.ERROR):
            result, pods_check, sleep = self.check_deployment(killed, 20)

        # the unknown key is never popped, so ns1 never recovers
        self.assertEqual(list(result), ["ns1"])
        self.assertIn("customkind", result["ns1"])
        pods_check.assert_not_called()
        self.assertEqual(sleep.call_count, 2)

    def test_empty_input_falls_into_error_branch(self):
        # an empty killed_namespaces dict skips the wait loop but still hits
        # the "still not ready" error logs and returns the empty dict
        with self.assertLogs(level=logging.ERROR) as logs:
            result, pods_check, sleep = self.check_deployment({}, 30)

        self.assertEqual(result, {})
        self.assertIn("still not ready", "".join(logs.output))
        pods_check.assert_not_called()
        sleep.assert_not_called()

    def test_wait_time_zero_never_checks_objects(self):
        killed = {"ns1": {"deployments": ["d1"]}}

        with self.assertLogs(level=logging.ERROR):
            result, pods_check, sleep = self.check_deployment(killed, 0)

        self.assertEqual(result, {"ns1": {"deployments": ["d1"]}})
        self.kubecli.get_deployment_ns.assert_not_called()
        pods_check.assert_not_called()
        sleep.assert_not_called()

    def test_wait_time_not_multiple_of_ten_overshoots(self):
        # a 25s wait_time polls at timer 0, 10 and 20: 30s of sleep in total
        killed = {"ns1": {"deployments": ["d1"]}}
        self.kubecli.get_deployment_ns.return_value = []

        with self.assertLogs(level=logging.ERROR):
            _, pods_check, sleep = self.check_deployment(killed, 25)

        self.assertEqual(sleep.call_count, 3)
        sleep.assert_has_calls([call(10), call(10), call(10)])
        pods_check.assert_not_called()

    def test_partial_kind_recovery_keeps_namespace_waiting(self):
        killed = {"ns1": {"deployments": ["d1"], "services": ["s1"]}}
        self.kubecli.get_deployment_ns.return_value = ["d1"]
        self.kubecli.get_all_services.return_value = []

        with self.assertLogs(level=logging.INFO) as logs:
            result, pods_check, sleep = self.check_deployment(killed, 20)

        # the services count never matches, so ns1 stays in the wait loop
        self.assertEqual(list(result), ["ns1"])
        self.assertIn("services", result["ns1"])
        self.assertIn("'services'", "".join(logs.output))
        pods_check.assert_not_called()
        self.assertEqual(sleep.call_count, 2)

    def test_objects_value_none_raises_attribute_error(self):
        with self.assertRaises(AttributeError):
            self.plugin.check_all_running_deployment(
                {"ns1": None}, 30, self.kubecli
            )

    def test_kubecli_getter_failure_propagates_uncaught(self):
        killed = {"ns1": {"deployments": ["d1"]}}
        self.kubecli.get_deployment_ns.side_effect = RuntimeError("api down")

        with self.assertRaises(RuntimeError):
            self.plugin.check_all_running_deployment(killed, 30, self.kubecli)

    def test_multi_namespace_mixed_recovery(self):
        killed = {
            "ns1": {"deployments": ["d1"]},
            "ns2": {"deployments": ["d2"]},
        }
        self.kubecli.get_deployment_ns.side_effect = (
            lambda ns: {"ns1": ["d1"], "ns2": []}[ns]
        )

        with self.assertLogs(level=logging.ERROR):
            result, pods_check, sleep = self.check_deployment(killed, 30)

        # ns1 recovers and gets its pod wait; ns2 never does
        self.assertEqual(result, {"ns2": {"deployments": ["d2"]}})
        pods_check.assert_called_once_with(self.kubecli, "ns1", 30)
        self.assertEqual(sleep.call_count, 3)

    def test_all_five_object_kinds_checked_for_recovery(self):
        killed = {
            "ns1": {
                "deployments": ["d1"],
                "daemonsets": ["ds1"],
                "statefulsets": ["ss1"],
                "replicasets": ["rs1"],
                "services": ["svc1"],
            }
        }
        self.kubecli.get_deployment_ns.return_value = ["d1"]
        self.kubecli.get_daemonset.return_value = ["ds1"]
        self.kubecli.get_all_statefulset.return_value = ["ss1"]
        self.kubecli.get_all_replicasets.return_value = ["rs1"]
        self.kubecli.get_all_services.return_value = ["svc1"]

        result, pods_check, sleep = self.check_deployment(killed, 30)

        self.assertEqual(result, [])
        pods_check.assert_called_once_with(self.kubecli, "ns1", 30)
        sleep.assert_not_called()

    def test_workload_kinds_with_wrong_counts_keep_namespace_waiting(self):
        killed = {
            "ns1": {
                "daemonsets": ["ds1"],
                "statefulsets": ["ss1"],
                "replicasets": ["rs1"],
            }
        }
        # the cluster shows one extra object of each kind: counts never match
        self.kubecli.get_daemonset.return_value = ["ds1", "ds2"]
        self.kubecli.get_all_statefulset.return_value = ["ss1", "ss2"]
        self.kubecli.get_all_replicasets.return_value = ["rs1", "rs2"]

        with self.assertLogs(level=logging.ERROR):
            result, pods_check, sleep = self.check_deployment(killed, 20)

        self.assertEqual(result, killed)
        pods_check.assert_not_called()
        self.assertEqual(sleep.call_count, 2)

if __name__ == "__main__":
    unittest.main()
