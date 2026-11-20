#!/usr/bin/env python3

"""
Test suite for PodDisruptionScenarioPlugin class

Usage:
    python -m coverage run -a -m unittest tests/test_pod_disruption_scenario_plugin.py -v

Assisted By: Claude Code
"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, call, patch

from krkn_lib.k8s import KrknKubernetes
from krkn_lib.models.pod_monitor.models import AffectedPod, PodsStatus
from krkn_lib.models.telemetry import ScenarioTelemetry
from krkn_lib.telemetry.ocp import KrknTelemetryOpenshift

from krkn.scenario_plugins.pod_disruption.pod_disruption_scenario_plugin import PodDisruptionScenarioPlugin
from krkn.scenario_plugins.pod_disruption.models.models import InputParams


MODULE = "krkn.scenario_plugins.pod_disruption.pod_disruption_scenario_plugin"


class TestPodDisruptionScenarioPlugin(unittest.TestCase):

    def setUp(self):
        """
        Set up test fixtures for PodDisruptionScenarioPlugin
        """
        self.plugin = PodDisruptionScenarioPlugin()

    def tearDown(self):
        """Clean up after each test to prevent state leakage"""
        self.plugin = None

    def test_get_scenario_types(self):
        """
        Test get_scenario_types returns correct scenario type
        """
        result = self.plugin.get_scenario_types()

        self.assertEqual(result, ["pod_disruption_scenarios"])
        self.assertEqual(len(result), 1)

class TestKillingPodsMode(unittest.TestCase):
    def setUp(self):
        """Set up test fixtures for killing_pods mode tests."""
        self.plugin = PodDisruptionScenarioPlugin()
        self.kubecli = MagicMock(spec=KrknKubernetes)
        self.plugin.get_pods = MagicMock()
        self.plugin.wait_for_pods = MagicMock(return_value=0)

    def tearDown(self):
        """Clean up after each test to prevent state leakage."""
        self.plugin = None
        self.kubecli = None

    # --- InputParams.execution parsing ---

    def test_execution_defaults_to_serial(self):
        """execution defaults to 'serial' when not specified in config."""
        params = InputParams({"kill": 2})
        self.assertEqual(params.execution, "serial")

    def test_execution_serial_explicit(self):
        """execution is correctly parsed when explicitly set to 'serial'."""
        params = InputParams({"kill": 2, "execution": "serial"})
        self.assertEqual(params.execution, "serial")

    def test_execution_parallel_explicit(self):
        """execution is correctly parsed when explicitly set to 'parallel'."""
        params = InputParams({"kill": 2, "execution": "parallel"})
        self.assertEqual(params.execution, "parallel")

    def test_execution_invalid_raises_value_error(self):
        """execution raises ValueError on unknown values."""
        with self.assertRaises(ValueError) as context:
            InputParams({"kill": 2, "execution": "invalid_mode"})
        
        self.assertIn("Unknown execution 'invalid_mode'", str(context.exception))

    # --- InputParams.kill validation ---

    def test_kill_invalid_negative_raises_value_error(self):
        """kill raises ValueError when negative in config."""
        with self.assertRaises(ValueError) as context:
            InputParams({"kill": -1})

        self.assertIn("Must be a non-negative integer", str(context.exception))

    def test_kill_invalid_type_raises_value_error(self):
        """kill raises ValueError when not an integer (e.g. string or boolean)."""
        with self.assertRaises(ValueError) as context:
            InputParams({"kill": "two"})

        self.assertIn("Must be a non-negative integer", str(context.exception))

        with self.assertRaises(ValueError) as context:
            InputParams({"kill": True})

        self.assertIn("Must be a non-negative integer", str(context.exception))

    # --- killing_pods() behaviour ---

    def test_negative_kill_count_returns_error(self):
        """Returns 1 and never calls delete_pod when config.kill is negative."""
        config = MagicMock(spec=InputParams)
        config.namespace_pattern = "ns1"
        config.name_pattern = ""
        config.label_selector = "app=test"
        config.exclude_label = ""
        config.node_label_selector = ""
        config.node_names = []
        config.kill = -1
        self.plugin.get_pods.return_value = [("pod1", "ns1"), ("pod2", "ns1")]

        result = self.plugin.killing_pods(config, self.kubecli)

        self.assertEqual(result, 1)
        self.kubecli.delete_pod.assert_not_called()

    def test_not_enough_pods_returns_error(self):
        """Returns 1 and never calls delete_pod when fewer pods exist than kill count."""
        config = InputParams({"namespace_pattern": "ns1", "kill": 3, "execution": "serial"})
        self.plugin.get_pods.return_value = [("pod1", "ns1"), ("pod2", "ns1")]

        result = self.plugin.killing_pods(config, self.kubecli)

        self.assertEqual(result, 1)
        self.kubecli.delete_pod.assert_not_called()

    def test_serial_mode_calls_delete_in_order(self):
        """Serial mode deletes all selected pods one at a time."""
        config = InputParams({"namespace_pattern": "ns1", "kill": 2, "execution": "serial"})
        self.plugin.get_pods.return_value = [("pod1", "ns1"), ("pod2", "ns1")]

        result = self.plugin.killing_pods(config, self.kubecli)

        self.assertEqual(result, 0)
        self.assertEqual(self.kubecli.delete_pod.call_count, 2)
        self.kubecli.delete_pod.assert_any_call("pod1", "ns1")
        self.kubecli.delete_pod.assert_any_call("pod2", "ns1")

    def test_parallel_mode_calls_delete_concurrently(self):
        """Parallel mode deletes all selected pods and calls delete_pod for each concurrently."""
        config = InputParams({"namespace_pattern": "ns1", "kill": 2, "execution": "parallel"})
        pods = [("pod1", "ns1"), ("pod2", "ns1")]
        self.plugin.get_pods.return_value = pods

        # Use a barrier to prove threads run concurrently. If they run serially, 
        # the first thread will block forever waiting for the second.
        import threading
        barrier = threading.Barrier(2, timeout=5)
        
        def side_effect(name, namespace):
            barrier.wait()

        self.kubecli.delete_pod.side_effect = side_effect

        result = self.plugin.killing_pods(config, self.kubecli)

        self.assertEqual(result, 0)
        self.assertEqual(self.kubecli.delete_pod.call_count, 2)
        self.kubecli.delete_pod.assert_any_call("pod1", "ns1")
        self.kubecli.delete_pod.assert_any_call("pod2", "ns1")

    def test_parallel_mode_propagates_delete_exception(self):
        """Exceptions raised during parallel deletion bubble up correctly."""
        config = InputParams({"namespace_pattern": "ns1", "kill": 2, "execution": "parallel"})
        self.plugin.get_pods.return_value = [("pod1", "ns1"), ("pod2", "ns1")]

        def side_effect(name, namespace):
            if name == "pod1":
                raise RuntimeError("failed to delete")

        self.kubecli.delete_pod.side_effect = side_effect

        with self.assertRaises(Exception) as context:
            self.plugin.killing_pods(config, self.kubecli)

        self.assertIn("parallel pod deletion failed", str(context.exception))
        self.assertIn("failed to delete", str(context.exception))

    def test_excluded_pods_are_not_deleted_in_serial_mode(self):
        """Pods matched by exclude_label are skipped and never passed to delete_pod (serial)."""
        config = InputParams({"namespace_pattern": "ns1", "kill": 2, "execution": "serial", "exclude_label": "protected=true"})
        # get_pods is called twice: first for target pods, then for excluded pods
        self.plugin.get_pods.side_effect = [
            [("pod1", "ns1"), ("pod2", "ns1"), ("pod3", "ns1")],  # target pods
            [("pod1", "ns1")],                                    # excluded pods
        ]

        result = self.plugin.killing_pods(config, self.kubecli)

        self.assertEqual(result, 0)
        self.assertEqual(self.kubecli.delete_pod.call_count, 2)
        self.kubecli.delete_pod.assert_any_call("pod2", "ns1")
        self.kubecli.delete_pod.assert_any_call("pod3", "ns1")
        for call in self.kubecli.delete_pod.call_args_list:
            self.assertNotEqual(call.args[0], "pod1")

    def test_excluded_pods_are_not_deleted_in_parallel_mode(self):
        """Pods matched by exclude_label are skipped and never passed to delete_pod (parallel)."""
        config = InputParams({"namespace_pattern": "ns1", "kill": 2, "execution": "parallel", "exclude_label": "protected=true"})
        self.plugin.get_pods.side_effect = [
            [("pod1", "ns1"), ("pod2", "ns1"), ("pod3", "ns1")],  # target pods
            [("pod1", "ns1")],                                    # excluded pods
        ]

        result = self.plugin.killing_pods(config, self.kubecli)

        self.assertEqual(result, 0)
        self.assertEqual(self.kubecli.delete_pod.call_count, 2)
        self.kubecli.delete_pod.assert_any_call("pod2", "ns1")
        self.kubecli.delete_pod.assert_any_call("pod3", "ns1")
        for call in self.kubecli.delete_pod.call_args_list:
            self.assertNotEqual(call.args[0], "pod1")

    def test_not_enough_eligible_pods_after_exclusion_returns_error(self):
        """Returns 1 and never calls delete_pod when remaining eligible pods after exclusion are fewer than kill count."""
        config = InputParams({"namespace_pattern": "ns1", "kill": 2, "execution": "serial", "exclude_label": "protected=true"})
        self.plugin.get_pods.side_effect = [
            [("pod1", "ns1"), ("pod2", "ns1"), ("pod3", "ns1")],  # target pods
            [("pod1", "ns1"), ("pod2", "ns1")],                    # excluded pods
        ]

        result = self.plugin.killing_pods(config, self.kubecli)

        self.assertEqual(result, 1)
        self.kubecli.delete_pod.assert_not_called()

    def test_same_named_pods_in_different_namespaces_not_mistakenly_excluded(self):
        """An excluded pod in one namespace does not exclude a same-named pod in another namespace."""
        config = InputParams({"namespace_pattern": "ns1", "kill": 1, "execution": "serial", "exclude_label": "protected=true"})
        # pod1 exists in both ns1 and ns2; only the instance in ns1 matches exclude_label
        self.plugin.get_pods.side_effect = [
            [("pod1", "ns1"), ("pod1", "ns2")],  # target pods
            [("pod1", "ns1")],                    # excluded pods
        ]

        result = self.plugin.killing_pods(config, self.kubecli)

        self.assertEqual(result, 0)
        self.kubecli.delete_pod.assert_called_once_with("pod1", "ns2")

    # --- force deletion tests ---

    def test_force_defaults_to_false(self):
        """force defaults to False when not specified in config."""
        params = InputParams({"kill": 1})
        self.assertFalse(params.force)

    def test_force_invalid_type_raises_value_error(self):
        """force raises ValueError when given a non-boolean value like a string."""
        with self.assertRaises(ValueError) as context:
            InputParams({"kill": 1, "force": "false"})

        self.assertIn("Must be a boolean", str(context.exception))

    def test_serial_mode_force_passes_grace_period_zero(self):
        """Serial mode with force=True calls delete_pod with grace_period_seconds=0."""
        config = InputParams({"namespace_pattern": "ns1", "kill": 2, "execution": "serial", "force": True})
        self.plugin.get_pods.return_value = [("pod1", "ns1"), ("pod2", "ns1")]

        result = self.plugin.killing_pods(config, self.kubecli)

        self.assertEqual(result, 0)
        self.assertEqual(self.kubecli.delete_pod.call_count, 2)
        self.kubecli.delete_pod.assert_any_call("pod1", "ns1", grace_period_seconds=0)
        self.kubecli.delete_pod.assert_any_call("pod2", "ns1", grace_period_seconds=0)

    def test_serial_mode_graceful_no_grace_period_kwarg(self):
        """Serial mode with force=False (default) calls delete_pod without grace_period_seconds."""
        config = InputParams({"namespace_pattern": "ns1", "kill": 1, "execution": "serial", "force": False})
        self.plugin.get_pods.return_value = [("pod1", "ns1")]

        result = self.plugin.killing_pods(config, self.kubecli)

        self.assertEqual(result, 0)
        self.kubecli.delete_pod.assert_called_once_with("pod1", "ns1")

    def test_parallel_mode_force_passes_grace_period_zero(self):
        """Parallel mode with force=True calls delete_pod with grace_period_seconds=0."""
        config = InputParams({"namespace_pattern": "ns1", "kill": 2, "execution": "parallel", "force": True})
        self.plugin.get_pods.return_value = [("pod1", "ns1"), ("pod2", "ns1")]

        result = self.plugin.killing_pods(config, self.kubecli)

        self.assertEqual(result, 0)
        self.assertEqual(self.kubecli.delete_pod.call_count, 2)
        self.kubecli.delete_pod.assert_any_call("pod1", "ns1", grace_period_seconds=0)
        self.kubecli.delete_pod.assert_any_call("pod2", "ns1", grace_period_seconds=0)

    def test_parallel_mode_graceful_no_grace_period_kwarg(self):
        """Parallel mode with force=False (default) calls delete_pod without grace_period_seconds."""
        config = InputParams({"namespace_pattern": "ns1", "kill": 2, "execution": "parallel", "force": False})
        self.plugin.get_pods.return_value = [("pod1", "ns1"), ("pod2", "ns1")]

        result = self.plugin.killing_pods(config, self.kubecli)

        self.assertEqual(result, 0)
        self.assertEqual(self.kubecli.delete_pod.call_count, 2)
        self.kubecli.delete_pod.assert_any_call("pod1", "ns1")
        self.kubecli.delete_pod.assert_any_call("pod2", "ns1")


class TestRunScenario(unittest.TestCase):
    """Tests for PodDisruptionScenarioPlugin.run()."""

    def setUp(self):
        self.plugin = PodDisruptionScenarioPlugin()
        self.scenario_telemetry = ScenarioTelemetry()
        self.lib_telemetry = MagicMock(spec=KrknTelemetryOpenshift)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _scenario_file(self, content):
        """Write scenario YAML to a temp file and return its path."""
        path = os.path.join(self._tmp.name, "scenario.yml")
        with open(path, "w") as f:
            f.write(content)
        return path

    def _future(self, pods_status):
        """Build a monitor future stand-in resolving to a snapshot."""
        snapshot = MagicMock()
        snapshot.get_pods_status.return_value = pods_status
        future = MagicMock()
        future.done.return_value = True
        future.result.return_value = snapshot
        return future

    @staticmethod
    def _pods_status(unrecovered=None):
        status = PodsStatus()
        for pod in unrecovered or []:
            status.unrecovered.append(pod)
        return status

    def _mock_phases(self, future, kill_result):
        self.plugin.start_monitoring = MagicMock(return_value=future)
        self.plugin.killing_pods = MagicMock(return_value=kill_result)

    def test_run_happy_path_returns_zero_and_sets_telemetry(self):
        """Successful scenario returns 0 and records the snapshot status on telemetry."""
        status = self._pods_status()
        future = self._future(status)
        self._mock_phases(future, 0)
        scenario = self._scenario_file(
            "- config:\n    kill: 1\n    namespace_pattern: ns1\n    label_selector: app=test\n"
        )

        result = self.plugin.run("uuid", scenario, self.lib_telemetry, self.scenario_telemetry)

        self.assertEqual(result, 0)
        self.assertIs(self.scenario_telemetry.affected_pods, status)
        future.result.assert_called_once_with()

    def test_run_unrecovered_pods_returns_one(self):
        """Unrecovered pods in the monitoring snapshot make run fail with 1."""
        status = self._pods_status(unrecovered=[AffectedPod(pod_name="pod1", namespace="ns1")])
        future = self._future(status)
        self._mock_phases(future, 0)
        scenario = self._scenario_file(
            "- config:\n    kill: 1\n    namespace_pattern: ns1\n    label_selector: app=test\n"
        )

        result = self.plugin.run("uuid", scenario, self.lib_telemetry, self.scenario_telemetry)

        self.assertEqual(result, 1)
        self.assertIs(self.scenario_telemetry.affected_pods, status)

    def test_run_kill_phase_failure_still_waits_for_monitoring(self):
        """killing_pods returning 1 still blocks on the monitoring future before failing."""
        future = self._future(self._pods_status())
        self._mock_phases(future, 1)
        scenario = self._scenario_file(
            "- config:\n    kill: 1\n    namespace_pattern: ns1\n    label_selector: app=test\n"
        )

        result = self.plugin.run("uuid", scenario, self.lib_telemetry, self.scenario_telemetry)

        self.assertEqual(result, 1)
        future.result.assert_called_once_with()

    # characterizes a branch that is currently unreachable in production: killing_pods only returns 0/1
    def test_run_config_error_cancels_monitoring_and_collects_snapshot(self):
        """killing_pods returning 2 cancels the future, gathers partial data and fails."""
        status = self._pods_status()
        future = self._future(status)
        self._mock_phases(future, 2)
        scenario = self._scenario_file(
            "- config:\n    kill: 1\n    namespace_pattern: ns1\n    label_selector: app=test\n"
        )

        result = self.plugin.run("uuid", scenario, self.lib_telemetry, self.scenario_telemetry)

        self.assertEqual(result, 1)
        future.cancel.assert_called_once_with()
        self.assertIs(self.scenario_telemetry.affected_pods, status)

    # characterizes a branch that is currently unreachable in production: killing_pods only returns 0/1
    @patch(f"{MODULE}.time.sleep")
    def test_run_config_error_busy_wait_polls_until_future_completes(self, mock_sleep):
        """The cancel busy-wait polls done() once per second until the future completes."""
        future = self._future(self._pods_status())
        future.done.side_effect = [False, False, True]
        self._mock_phases(future, 2)
        scenario = self._scenario_file(
            "- config:\n    kill: 1\n    namespace_pattern: ns1\n    label_selector: app=test\n"
        )

        result = self.plugin.run("uuid", scenario, self.lib_telemetry, self.scenario_telemetry)

        self.assertEqual(result, 1)
        self.assertEqual(mock_sleep.call_count, 2)
        mock_sleep.assert_called_with(1)
        self.assertEqual(future.done.call_count, 3)

    def test_run_killing_pods_exception_returns_one_and_cancels_monitoring(self):
        """An exception from killing_pods exits with 1 and cancels the monitoring future."""
        future = self._future(self._pods_status())
        self.plugin.start_monitoring = MagicMock(return_value=future)
        self.plugin.killing_pods = MagicMock(side_effect=RuntimeError("kill failed"))
        scenario = self._scenario_file(
            "- config:\n    kill: 1\n    namespace_pattern: ns1\n    label_selector: app=test\n"
        )

        result = self.plugin.run("uuid", scenario, self.lib_telemetry, self.scenario_telemetry)

        self.assertEqual(result, 1)
        future.cancel.assert_called_once()

    def test_run_monitor_setup_failure_returns_one_without_killing(self):
        """A scenario start_monitoring cannot handle fails with 1 before any pod is touched."""
        self.plugin.killing_pods = MagicMock(return_value=0)
        # label_selector without namespace_pattern: start_monitoring cannot pick a monitor
        scenario = self._scenario_file("- config:\n    kill: 1\n    label_selector: app=test\n")

        result = self.plugin.run("uuid", scenario, self.lib_telemetry, self.scenario_telemetry)

        self.assertEqual(result, 1)
        self.plugin.killing_pods.assert_not_called()

    def test_run_missing_scenario_file_returns_one(self):
        """A missing scenario file is swallowed and reported as failure 1."""
        result = self.plugin.run(
            "uuid", "/nonexistent/scenario.yml", self.lib_telemetry, self.scenario_telemetry
        )

        self.assertEqual(result, 1)

    def test_run_invalid_yaml_returns_one(self):
        """Malformed YAML is swallowed and reported as failure 1."""
        scenario = self._scenario_file("::: bad yaml")

        result = self.plugin.run("uuid", scenario, self.lib_telemetry, self.scenario_telemetry)

        self.assertEqual(result, 1)

    def test_run_empty_file_returns_one(self):
        """An empty file yields None from safe_load and fails with 1."""
        scenario = self._scenario_file("")

        result = self.plugin.run("uuid", scenario, self.lib_telemetry, self.scenario_telemetry)

        self.assertEqual(result, 1)

    def test_run_empty_scenario_list_returns_zero(self):
        """An empty scenario list skips the loop entirely and reports success.

        Characterization of current behavior: an empty (but valid) scenario
        file is treated as a successful run.
        """
        scenario = self._scenario_file("[]")

        result = self.plugin.run("uuid", scenario, self.lib_telemetry, self.scenario_telemetry)

        self.assertEqual(result, 0)

    def test_run_entry_missing_config_key_returns_one(self):
        """A scenario entry without the config key fails with 1."""
        scenario = self._scenario_file("- kill: 1\n")

        result = self.plugin.run("uuid", scenario, self.lib_telemetry, self.scenario_telemetry)

        self.assertEqual(result, 1)

    def test_run_scalar_entry_returns_one(self):
        """A scalar scenario entry fails with 1 instead of raising."""
        scenario = self._scenario_file("- foo\n")

        result = self.plugin.run("uuid", scenario, self.lib_telemetry, self.scenario_telemetry)

        self.assertEqual(result, 1)

    def test_run_invalid_config_value_returns_one_before_monitoring(self):
        """Invalid config values (kill: 'two') fail with 1 before monitoring starts."""
        self.plugin.start_monitoring = MagicMock()
        scenario = self._scenario_file('- config:\n    kill: two\n')

        result = self.plugin.run("uuid", scenario, self.lib_telemetry, self.scenario_telemetry)

        self.assertEqual(result, 1)
        self.plugin.start_monitoring.assert_not_called()

    def test_run_first_scenario_failure_skips_remaining_scenarios(self):
        """A failing scenario aborts the whole run; later entries are never processed."""
        future = self._future(self._pods_status())
        self.plugin.start_monitoring = MagicMock(return_value=future)
        self.plugin.killing_pods = MagicMock(side_effect=[1, 0])
        scenario = self._scenario_file(
            "- config:\n    kill: 1\n    namespace_pattern: ns1\n    label_selector: app=a\n"
            "- config:\n    kill: 1\n    namespace_pattern: ns2\n    label_selector: app=b\n"
        )

        result = self.plugin.run("uuid", scenario, self.lib_telemetry, self.scenario_telemetry)

        self.assertEqual(result, 1)
        self.assertEqual(self.plugin.killing_pods.call_count, 1)
        self.assertEqual(self.plugin.start_monitoring.call_count, 1)

    def test_run_two_scenarios_both_succeed(self):
        """All scenarios in the file are processed sequentially and each is monitored."""
        future1 = self._future(self._pods_status())
        future2 = self._future(self._pods_status())
        self.plugin.start_monitoring = MagicMock(side_effect=[future1, future2])
        self.plugin.killing_pods = MagicMock(return_value=0)
        scenario = self._scenario_file(
            "- config:\n    kill: 1\n    namespace_pattern: ns1\n    label_selector: app=a\n"
            "- config:\n    kill: 1\n    namespace_pattern: ns2\n    label_selector: app=b\n"
        )

        result = self.plugin.run("uuid", scenario, self.lib_telemetry, self.scenario_telemetry)

        self.assertEqual(result, 0)
        self.assertEqual(self.plugin.start_monitoring.call_count, 2)
        self.assertEqual(self.plugin.killing_pods.call_count, 2)
        future1.result.assert_called_once_with()
        future2.result.assert_called_once_with()


class TestStartMonitoring(unittest.TestCase):
    """Tests for PodDisruptionScenarioPlugin.start_monitoring()."""

    def setUp(self):
        self.plugin = PodDisruptionScenarioPlugin()
        self.kubecli = MagicMock(spec=KrknKubernetes)
        self.kubecli.cli = object()
        self.lib_telemetry = MagicMock(spec=KrknTelemetryOpenshift)
        self.lib_telemetry.get_lib_kubernetes.return_value = self.kubecli

    @patch(f"{MODULE}.select_and_monitor_by_namespace_pattern_and_label")
    @patch(f"{MODULE}.select_and_monitor_by_name_pattern_and_namespace_pattern")
    def test_namespace_and_label_uses_label_monitor(self, mock_name_monitor, mock_label_monitor):
        """namespace_pattern + label_selector starts the label-based monitor with its future."""
        config = InputParams(
            {"kill": 1, "namespace_pattern": "ns1", "label_selector": "app=x",
             "krkn_pod_recovery_time": 300}
        )

        future = self.plugin.start_monitoring(config, self.lib_telemetry)

        self.assertIs(future, mock_label_monitor.return_value)
        mock_label_monitor.assert_called_once_with(
            namespace_pattern="ns1", label_selector="app=x",
            max_timeout=300, v1_client=self.kubecli.cli,
        )
        mock_name_monitor.assert_not_called()

    @patch(f"{MODULE}.select_and_monitor_by_namespace_pattern_and_label")
    @patch(f"{MODULE}.select_and_monitor_by_name_pattern_and_namespace_pattern")
    def test_namespace_and_name_uses_name_monitor_with_default_recovery_time(
        self, mock_name_monitor, mock_label_monitor
    ):
        """namespace_pattern + name_pattern starts the name-based monitor; recovery defaults to 120."""
        config = InputParams(
            {"kill": 1, "namespace_pattern": "ns1", "name_pattern": "pod.*"}
        )

        future = self.plugin.start_monitoring(config, self.lib_telemetry)

        self.assertIs(future, mock_name_monitor.return_value)
        mock_name_monitor.assert_called_once_with(
            pod_name_pattern="pod.*", namespace_pattern="ns1",
            max_timeout=120, v1_client=self.kubecli.cli,
        )
        mock_label_monitor.assert_not_called()

    @patch(f"{MODULE}.select_and_monitor_by_namespace_pattern_and_label")
    @patch(f"{MODULE}.select_and_monitor_by_name_pattern_and_namespace_pattern")
    def test_label_takes_precedence_when_name_also_set(self, mock_name_monitor, mock_label_monitor):
        """With namespace, label and name all set only the label-based monitor runs."""
        config = InputParams(
            {"kill": 1, "namespace_pattern": "ns1", "label_selector": "app=x",
             "name_pattern": "pod.*"}
        )

        future = self.plugin.start_monitoring(config, self.lib_telemetry)

        self.assertIs(future, mock_label_monitor.return_value)
        mock_name_monitor.assert_not_called()

    @patch(f"{MODULE}.select_and_monitor_by_namespace_pattern_and_label")
    @patch(f"{MODULE}.select_and_monitor_by_name_pattern_and_namespace_pattern")
    def test_label_without_namespace_raises(self, mock_name_monitor, mock_label_monitor):
        """label_selector alone cannot select a monitor and raises."""
        config = InputParams({"kill": 1, "label_selector": "app=x"})

        with self.assertRaises(Exception) as context:
            self.plugin.start_monitoring(config, self.lib_telemetry)

        self.assertIn("impossible to determine monitor parameters", str(context.exception))
        mock_label_monitor.assert_not_called()
        mock_name_monitor.assert_not_called()

    @patch(f"{MODULE}.select_and_monitor_by_namespace_pattern_and_label")
    @patch(f"{MODULE}.select_and_monitor_by_name_pattern_and_namespace_pattern")
    def test_name_without_namespace_raises(self, mock_name_monitor, mock_label_monitor):
        """name_pattern alone cannot select a monitor and raises."""
        config = InputParams({"kill": 1, "name_pattern": "pod.*"})

        with self.assertRaises(Exception) as context:
            self.plugin.start_monitoring(config, self.lib_telemetry)

        self.assertIn("impossible to determine monitor parameters", str(context.exception))
        mock_label_monitor.assert_not_called()
        mock_name_monitor.assert_not_called()

    @patch(f"{MODULE}.select_and_monitor_by_namespace_pattern_and_label")
    @patch(f"{MODULE}.select_and_monitor_by_name_pattern_and_namespace_pattern")
    def test_configuration_without_selectors_raises(self, mock_name_monitor, mock_label_monitor):
        """A config with no namespace/label/name patterns cannot select a monitor and raises."""
        config = InputParams({"kill": 1})

        with self.assertRaises(Exception) as context:
            self.plugin.start_monitoring(config, self.lib_telemetry)

        self.assertIn("impossible to determine monitor parameters", str(context.exception))
        mock_label_monitor.assert_not_called()
        mock_name_monitor.assert_not_called()

    @patch(f"{MODULE}.select_and_monitor_by_namespace_pattern_and_label")
    @patch(f"{MODULE}.select_and_monitor_by_name_pattern_and_namespace_pattern")
    def test_empty_string_namespace_does_not_start_monitoring(
        self, mock_name_monitor, mock_label_monitor
    ):
        """An explicitly empty namespace_pattern is falsy and fails monitor selection."""
        config = InputParams(
            {"kill": 1, "namespace_pattern": "", "label_selector": "app=x"}
        )

        with self.assertRaises(Exception) as context:
            self.plugin.start_monitoring(config, self.lib_telemetry)

        self.assertIn("impossible to determine monitor parameters", str(context.exception))
        mock_label_monitor.assert_not_called()


class TestSelectPodsWithFieldSelector(unittest.TestCase):
    """Tests for PodDisruptionScenarioPlugin._select_pods_with_field_selector()."""

    def setUp(self):
        self.plugin = PodDisruptionScenarioPlugin()
        self.kubecli = MagicMock(spec=KrknKubernetes)

    def test_label_selector_uses_label_selection(self):
        """A label selector selects pods by namespace and label, returning the kubecli result."""
        self.kubecli.select_pods_by_namespace_pattern_and_label.return_value = [("pod1", "ns1")]

        pods = self.plugin._select_pods_with_field_selector(
            "", "app=x", "ns1", self.kubecli, "status.phase=Running"
        )

        self.assertEqual(pods, [("pod1", "ns1")])
        self.kubecli.select_pods_by_namespace_pattern_and_label.assert_called_once_with(
            label_selector="app=x", namespace_pattern="ns1", field_selector="status.phase=Running"
        )
        self.kubecli.select_pods_by_name_pattern_and_namespace_pattern.assert_not_called()

    def test_name_selector_uses_name_selection(self):
        """Without a label selector, pods are selected by name pattern."""
        self.kubecli.select_pods_by_name_pattern_and_namespace_pattern.return_value = [
            ("pod1", "ns1")
        ]

        pods = self.plugin._select_pods_with_field_selector(
            "pod.*", "", "ns1", self.kubecli, "status.phase=Running"
        )

        self.assertEqual(pods, [("pod1", "ns1")])
        self.kubecli.select_pods_by_name_pattern_and_namespace_pattern.assert_called_once_with(
            pod_name_pattern="pod.*", namespace_pattern="ns1", field_selector="status.phase=Running"
        )
        self.kubecli.select_pods_by_namespace_pattern_and_label.assert_not_called()

    def test_node_name_combines_with_existing_field_selector(self):
        """A node name is appended to an existing field selector as a combined selector."""
        self.plugin._select_pods_with_field_selector(
            "", "app=x", "ns1", self.kubecli, "status.phase=Running", "node1"
        )

        self.kubecli.select_pods_by_namespace_pattern_and_label.assert_called_once_with(
            label_selector="app=x", namespace_pattern="ns1",
            field_selector="status.phase=Running,spec.nodeName=node1",
        )

    def test_node_name_without_field_selector_targets_node_only(self):
        """A node name without a field selector targets only the node."""
        self.plugin._select_pods_with_field_selector(
            "pod.*", "", "ns1", self.kubecli, None, "node1"
        )

        self.kubecli.select_pods_by_name_pattern_and_namespace_pattern.assert_called_once_with(
            pod_name_pattern="pod.*", namespace_pattern="ns1", field_selector="spec.nodeName=node1"
        )


class TestGetPods(unittest.TestCase):
    """Tests for PodDisruptionScenarioPlugin.get_pods()."""

    def setUp(self):
        self.plugin = PodDisruptionScenarioPlugin()
        self.kubecli = MagicMock(spec=KrknKubernetes)

    def test_label_only_uses_standard_label_selection(self):
        """Label-only selection makes a single call without node targeting."""
        self.kubecli.select_pods_by_namespace_pattern_and_label.return_value = [("pod1", "ns1")]

        pods = self.plugin.get_pods("", "app=x", "ns1", self.kubecli)

        self.assertEqual(pods, [("pod1", "ns1")])
        self.kubecli.select_pods_by_namespace_pattern_and_label.assert_called_once_with(
            label_selector="app=x", namespace_pattern="ns1", field_selector=None
        )
        self.kubecli.list_nodes.assert_not_called()

    def test_name_only_uses_standard_name_selection(self):
        """Name-only selection passes the field selector through to the name-based call."""
        self.kubecli.select_pods_by_name_pattern_and_namespace_pattern.return_value = [
            ("pod1", "ns1")
        ]

        pods = self.plugin.get_pods("pod.*", "", "ns1", self.kubecli, field_selector="status.phase=Running")

        self.assertEqual(pods, [("pod1", "ns1")])
        self.kubecli.select_pods_by_name_pattern_and_namespace_pattern.assert_called_once_with(
            pod_name_pattern="pod.*", namespace_pattern="ns1", field_selector="status.phase=Running"
        )

    def test_both_name_and_label_returns_empty_without_kube_call(self):
        """Specifying both name and label selectors is rejected before any kube call."""
        pods = self.plugin.get_pods("pod.*", "app=x", "ns1", self.kubecli)

        self.assertEqual(pods, [])
        self.kubecli.select_pods_by_namespace_pattern_and_label.assert_not_called()
        self.kubecli.select_pods_by_name_pattern_and_namespace_pattern.assert_not_called()
        self.kubecli.list_nodes.assert_not_called()

    def test_neither_name_nor_label_returns_empty_without_kube_call(self):
        """Specifying neither selector is rejected before any kube call."""
        pods = self.plugin.get_pods("", "", "ns1", self.kubecli)

        self.assertEqual(pods, [])
        self.kubecli.select_pods_by_namespace_pattern_and_label.assert_not_called()
        self.kubecli.select_pods_by_name_pattern_and_namespace_pattern.assert_not_called()
        self.kubecli.list_nodes.assert_not_called()

    def test_node_names_target_each_node_with_combined_field_selector(self):
        """Explicit node names produce one selection call per node, results concatenated in order."""
        self.kubecli.select_pods_by_name_pattern_and_namespace_pattern.side_effect = [
            [("pod-n1", "ns1")],
            [("pod-n2", "ns1")],
        ]

        pods = self.plugin.get_pods(
            "pod.*", "", "ns1", self.kubecli,
            field_selector="status.phase=Running", node_names=["n1", "n2"],
        )

        self.assertEqual(pods, [("pod-n1", "ns1"), ("pod-n2", "ns1")])
        self.assertEqual(
            self.kubecli.select_pods_by_name_pattern_and_namespace_pattern.call_count, 2
        )
        self.kubecli.select_pods_by_name_pattern_and_namespace_pattern.assert_any_call(
            pod_name_pattern="pod.*", namespace_pattern="ns1",
            field_selector="status.phase=Running,spec.nodeName=n1",
        )
        self.kubecli.select_pods_by_name_pattern_and_namespace_pattern.assert_any_call(
            pod_name_pattern="pod.*", namespace_pattern="ns1",
            field_selector="status.phase=Running,spec.nodeName=n2",
        )
        self.kubecli.list_nodes.assert_not_called()

    def test_node_names_without_field_selector_target_node_only(self):
        """Node names without a field selector select purely by node."""
        self.kubecli.select_pods_by_namespace_pattern_and_label.return_value = [("pod-n1", "ns1")]

        pods = self.plugin.get_pods("", "app=x", "ns1", self.kubecli, node_names=["n1"])

        self.assertEqual(pods, [("pod-n1", "ns1")])
        self.kubecli.select_pods_by_namespace_pattern_and_label.assert_called_once_with(
            label_selector="app=x", namespace_pattern="ns1", field_selector="spec.nodeName=n1"
        )

    def test_node_names_skip_nodes_with_no_matching_pods(self):
        """Empty per-node results are not appended to the aggregated list."""
        self.kubecli.select_pods_by_name_pattern_and_namespace_pattern.side_effect = [
            [],
            [("pod-n2", "ns1")],
        ]

        pods = self.plugin.get_pods("pod.*", "", "ns1", self.kubecli, node_names=["n1", "n2"])

        self.assertEqual(pods, [("pod-n2", "ns1")])

    def test_empty_node_names_list_falls_back_to_standard_selection(self):
        """An empty node_names list is falsy and does not trigger node targeting."""
        self.kubecli.select_pods_by_namespace_pattern_and_label.return_value = [("pod1", "ns1")]

        pods = self.plugin.get_pods("", "app=x", "ns1", self.kubecli, node_names=[])

        self.assertEqual(pods, [("pod1", "ns1")])
        self.kubecli.select_pods_by_namespace_pattern_and_label.assert_called_once_with(
            label_selector="app=x", namespace_pattern="ns1", field_selector=None
        )

    def test_node_label_selector_targets_each_matching_node(self):
        """A node label selector resolves nodes first, then selects pods per node."""
        self.kubecli.list_nodes.return_value = ["n1", "n2"]
        self.kubecli.select_pods_by_name_pattern_and_namespace_pattern.side_effect = [
            [("pod-n1", "ns1")],
            [("pod-n2", "ns1")],
        ]

        pods = self.plugin.get_pods(
            "pod.*", "", "ns1", self.kubecli,
            field_selector="status.phase=Running", node_label_selector="zone=a",
        )

        self.assertEqual(pods, [("pod-n1", "ns1"), ("pod-n2", "ns1")])
        self.kubecli.list_nodes.assert_called_once_with(label_selector="zone=a")
        self.kubecli.select_pods_by_name_pattern_and_namespace_pattern.assert_any_call(
            pod_name_pattern="pod.*", namespace_pattern="ns1",
            field_selector="status.phase=Running,spec.nodeName=n1",
        )
        self.kubecli.select_pods_by_name_pattern_and_namespace_pattern.assert_any_call(
            pod_name_pattern="pod.*", namespace_pattern="ns1",
            field_selector="status.phase=Running,spec.nodeName=n2",
        )

    def test_node_label_selector_with_no_matching_nodes_returns_empty(self):
        """A node label selector matching no nodes returns no pods and selects nothing."""
        self.kubecli.list_nodes.return_value = []

        pods = self.plugin.get_pods(
            "pod.*", "", "ns1", self.kubecli, node_label_selector="zone=missing"
        )

        self.assertEqual(pods, [])
        self.kubecli.select_pods_by_name_pattern_and_namespace_pattern.assert_not_called()

    def test_node_label_selector_skips_nodes_with_no_matching_pods(self):
        """Nodes resolved by label that host no matching pods contribute nothing."""
        self.kubecli.list_nodes.return_value = ["n1", "n2"]
        self.kubecli.select_pods_by_name_pattern_and_namespace_pattern.side_effect = [
            [],
            [("pod-n2", "ns1")],
        ]

        pods = self.plugin.get_pods(
            "pod.*", "", "ns1", self.kubecli, node_label_selector="zone=a"
        )

        self.assertEqual(pods, [("pod-n2", "ns1")])


class TestWaitForPods(unittest.TestCase):
    """Tests for PodDisruptionScenarioPlugin.wait_for_pods()."""

    def setUp(self):
        self.plugin = PodDisruptionScenarioPlugin()
        self.plugin.get_pods = MagicMock()
        self.kubecli = MagicMock(spec=KrknKubernetes)

    @patch(f"{MODULE}.time.sleep")
    @patch(f"{MODULE}.datetime")
    def test_returns_immediately_when_pod_count_matches(self, mock_datetime, mock_sleep):
        """A stable pod count returns 0 without sleeping."""
        t0 = datetime(2026, 1, 1, 12, 0, 0)
        mock_datetime.now.side_effect = [t0]
        self.plugin.get_pods.return_value = [("pod1", "ns1"), ("pod2", "ns1")]

        result = self.plugin.wait_for_pods("app=x", "", "ns1", 2, 10, 30, self.kubecli, "", [])

        self.assertEqual(result, 0)
        mock_sleep.assert_not_called()
        self.plugin.get_pods.assert_called_once()

    @patch(f"{MODULE}.time.sleep")
    @patch(f"{MODULE}.datetime")
    def test_polls_with_duration_until_pods_recover(self, mock_datetime, mock_sleep):
        """Missing pods are re-checked every duration seconds until the count matches."""
        t0 = datetime(2026, 1, 1, 12, 0, 0)
        mock_datetime.now.side_effect = [t0, t0 + timedelta(seconds=10)]
        self.plugin.get_pods.side_effect = [
            [("pod1", "ns1")],
            [("pod1", "ns1"), ("pod2", "ns1")],
        ]

        result = self.plugin.wait_for_pods("app=x", "", "ns1", 2, 10, 30, self.kubecli, "", [])

        self.assertEqual(result, 0)
        mock_sleep.assert_called_once_with(10)

    @patch(f"{MODULE}.time.sleep")
    @patch(f"{MODULE}.datetime")
    def test_exceeding_wait_timeout_returns_one(self, mock_datetime, mock_sleep):
        """Pods never recovering past wait_timeout return 1."""
        t0 = datetime(2026, 1, 1, 12, 0, 0)
        mock_datetime.now.side_effect = [t0, t0 + timedelta(seconds=31)]
        self.plugin.get_pods.return_value = [("pod1", "ns1")]

        result = self.plugin.wait_for_pods("app=x", "", "ns1", 2, 10, 30, self.kubecli, "", [])

        self.assertEqual(result, 1)
        mock_sleep.assert_called_once_with(10)

    @patch(f"{MODULE}.time.sleep")
    @patch(f"{MODULE}.datetime")
    def test_elapsed_time_equal_to_timeout_keeps_polling(self, mock_datetime, mock_sleep):
        """The timeout comparison is strict: elapsed == wait_timeout polls once more."""
        t0 = datetime(2026, 1, 1, 12, 0, 0)
        mock_datetime.now.side_effect = [t0, t0 + timedelta(seconds=30)]
        self.plugin.get_pods.side_effect = [
            [("pod1", "ns1")],
            [("pod1", "ns1"), ("pod2", "ns1")],
        ]

        result = self.plugin.wait_for_pods("app=x", "", "ns1", 2, 10, 30, self.kubecli, "", [])

        self.assertEqual(result, 0)
        self.assertEqual(self.plugin.get_pods.call_count, 2)
        mock_sleep.assert_called_once_with(10)

    @patch(f"{MODULE}.time.sleep")
    @patch(f"{MODULE}.datetime")
    def test_passes_selection_args_to_get_pods(self, mock_datetime, mock_sleep):
        """Node targeting and selectors are forwarded to get_pods on every check."""
        t0 = datetime(2026, 1, 1, 12, 0, 0)
        mock_datetime.now.side_effect = [t0]
        self.plugin.get_pods.return_value = [("pod1", "ns1")]

        self.plugin.wait_for_pods(
            "app=x", "pod.*", "ns1", 1, 10, 30, self.kubecli, "zone=a", ["n1"]
        )

        self.plugin.get_pods.assert_called_once_with(
            name_pattern="pod.*", label_selector="app=x", namespace="ns1",
            field_selector="status.phase=Running", kubecli=self.kubecli,
            node_label_selector="zone=a", node_names=["n1"],
        )

    @patch(f"{MODULE}.time.sleep")
    @patch(f"{MODULE}.datetime")
    def test_elapsed_time_beyond_one_day_times_out(self, mock_datetime, mock_sleep):
        """An elapsed time over a full day must still count against wait_timeout."""
        t0 = datetime(2026, 1, 1, 12, 0, 0)
        beyond_a_day = t0 + timedelta(days=1, seconds=10)
        mock_datetime.now.side_effect = [t0] + [beyond_a_day] * 5
        self.plugin.get_pods.return_value = [("pod1", "ns1")]

        result = self.plugin.wait_for_pods("app=x", "", "ns1", 2, 10, 30, self.kubecli, "", [])

        self.assertEqual(result, 1)


class TestKillingPodsSelection(unittest.TestCase):
    """killing_pods tests exercising the real get_pods selection path (kubecli mocked)."""

    def setUp(self):
        self.plugin = PodDisruptionScenarioPlugin()
        self.plugin.wait_for_pods = MagicMock(return_value=0)
        self.kubecli = MagicMock(spec=KrknKubernetes)

    def test_selects_running_pods_and_deletes_them(self):
        """Target pods are selected among Running pods only, then deleted."""
        config = InputParams({"kill": 2, "label_selector": "app=x", "namespace_pattern": "ns1"})
        self.kubecli.select_pods_by_namespace_pattern_and_label.return_value = [
            ("pod1", "ns1"), ("pod2", "ns1")
        ]

        result = self.plugin.killing_pods(config, self.kubecli)

        self.assertEqual(result, 0)
        self.kubecli.select_pods_by_namespace_pattern_and_label.assert_called_once_with(
            label_selector="app=x", namespace_pattern="ns1", field_selector="status.phase=Running"
        )
        self.assertEqual(self.kubecli.delete_pod.call_count, 2)

    def test_node_targeting_is_forwarded_to_selection(self):
        """node_names restrict the pod selection to pods Running on those nodes."""
        config = InputParams(
            {"kill": 1, "label_selector": "app=x", "namespace_pattern": "ns1",
             "node_names": ["n1"]}
        )
        self.kubecli.select_pods_by_namespace_pattern_and_label.return_value = [("pod1", "ns1")]

        result = self.plugin.killing_pods(config, self.kubecli)

        self.assertEqual(result, 0)
        self.kubecli.select_pods_by_namespace_pattern_and_label.assert_called_once_with(
            label_selector="app=x", namespace_pattern="ns1",
            field_selector="status.phase=Running,spec.nodeName=n1",
        )
        self.kubecli.delete_pod.assert_called_once_with("pod1", "ns1")

    def test_node_label_selector_is_resolved_to_nodes(self):
        """node_label_selector pods are selected only on nodes carrying the label."""
        config = InputParams(
            {"kill": 1, "label_selector": "app=x", "namespace_pattern": "ns1",
             "node_label_selector": "zone=a"}
        )
        self.kubecli.list_nodes.return_value = ["n1"]
        self.kubecli.select_pods_by_namespace_pattern_and_label.return_value = [("pod1", "ns1")]

        result = self.plugin.killing_pods(config, self.kubecli)

        self.assertEqual(result, 0)
        self.kubecli.list_nodes.assert_called_once_with(label_selector="zone=a")
        self.kubecli.select_pods_by_namespace_pattern_and_label.assert_called_once_with(
            label_selector="app=x", namespace_pattern="ns1",
            field_selector="status.phase=Running,spec.nodeName=n1",
        )

    def test_exclusion_selects_by_exclude_label_with_empty_name_pattern(self):
        """The exclusion lookup uses only the exclude label with no name pattern."""
        config = InputParams(
            {"kill": 2, "label_selector": "app=x", "namespace_pattern": "ns1",
             "exclude_label": "protected=true"}
        )
        self.kubecli.select_pods_by_namespace_pattern_and_label.side_effect = [
            [("pod1", "ns1"), ("pod2", "ns1"), ("pod3", "ns1")],
            [("pod1", "ns1")],
        ]

        result = self.plugin.killing_pods(config, self.kubecli)

        self.assertEqual(result, 0)
        calls = self.kubecli.select_pods_by_namespace_pattern_and_label.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].kwargs["label_selector"], "app=x")
        self.assertEqual(calls[1].kwargs["label_selector"], "protected=true")
        self.assertEqual(self.kubecli.delete_pod.call_count, 2)
        self.assertCountEqual(
            self.kubecli.delete_pod.call_args_list,
            [call("pod2", "ns1"), call("pod3", "ns1")],
        )

    def test_exclusion_leaving_too_few_eligible_pods_returns_one_without_deleting(self):
        """Returns 1 and deletes nothing when exclusion leaves fewer eligible pods than kill."""
        config = InputParams(
            {"kill": 3, "label_selector": "app=x", "namespace_pattern": "ns1",
             "exclude_label": "protected=true"}
        )
        self.kubecli.select_pods_by_namespace_pattern_and_label.side_effect = [
            [("pod1", "ns1"), ("pod2", "ns1"), ("pod3", "ns1")],
            [("pod1", "ns1")],
        ]

        result = self.plugin.killing_pods(config, self.kubecli)

        self.assertEqual(result, 1)
        self.kubecli.delete_pod.assert_not_called()

    def test_waits_for_matched_pod_count_before_exclusion(self):
        """wait_for_pods receives the full matched pod count, not the eligible count."""
        config = InputParams(
            {"kill": 2, "label_selector": "app=x", "namespace_pattern": "ns1",
             "exclude_label": "protected=true", "duration": 10, "timeout": 120}
        )
        self.kubecli.select_pods_by_namespace_pattern_and_label.side_effect = [
            [("pod1", "ns1"), ("pod2", "ns1"), ("pod3", "ns1")],
            [("pod1", "ns1")],
        ]

        result = self.plugin.killing_pods(config, self.kubecli)

        self.assertEqual(result, 0)
        self.plugin.wait_for_pods.assert_called_once_with(
            "app=x", "", "ns1", 3, 10, 120, self.kubecli, "", []
        )

    def test_kill_zero_deletes_nothing_but_still_waits(self):
        """kill=0 skips deletion entirely but still waits for the matched pods."""
        config = InputParams({"kill": 0, "label_selector": "app=x", "namespace_pattern": "ns1"})
        self.kubecli.select_pods_by_namespace_pattern_and_label.return_value = [
            ("pod1", "ns1"), ("pod2", "ns1")
        ]

        result = self.plugin.killing_pods(config, self.kubecli)

        self.assertEqual(result, 0)
        self.kubecli.delete_pod.assert_not_called()
        self.plugin.wait_for_pods.assert_called_once_with(
            "app=x", "", "ns1", 2, 10, 120, self.kubecli, "", []
        )

    @patch(f"{MODULE}.random.shuffle")
    def test_eligible_pods_are_shuffled_before_killing(self, mock_shuffle):
        """The eligible pod list is shuffled before choosing the victims."""
        config = InputParams({"kill": 2, "label_selector": "app=x", "namespace_pattern": "ns1"})
        pods = [("pod1", "ns1"), ("pod2", "ns1")]
        self.kubecli.select_pods_by_namespace_pattern_and_label.return_value = pods

        result = self.plugin.killing_pods(config, self.kubecli)

        self.assertEqual(result, 0)
        mock_shuffle.assert_called_once_with(pods)

    def test_missing_namespace_proceeds_with_empty_pattern(self):
        """A missing namespace must not become a cluster-wide match-all selection.

        Expected: killing_pods fails (non-zero or raises) and nothing is selected or deleted.
        """
        config = InputParams({"kill": 1, "label_selector": "app=x"})
        self.kubecli.select_pods_by_namespace_pattern_and_label.return_value = [("pod1", "ns1")]

        try:
            result = self.plugin.killing_pods(config, self.kubecli)
        except Exception:
            result = 1

        self.assertNotEqual(result, 0)
        self.kubecli.select_pods_by_namespace_pattern_and_label.assert_not_called()
        self.kubecli.select_pods_by_name_pattern_and_namespace_pattern.assert_not_called()
        self.kubecli.delete_pod.assert_not_called()

    def test_selection_failure_propagates(self):
        """Exceptions from the pod selection bubble up out of killing_pods."""
        config = InputParams({"kill": 1, "label_selector": "app=x", "namespace_pattern": "ns1"})
        self.kubecli.select_pods_by_namespace_pattern_and_label.side_effect = RuntimeError(
            "api down"
        )

        with self.assertRaises(RuntimeError) as context:
            self.plugin.killing_pods(config, self.kubecli)

        self.assertIn("api down", str(context.exception))
        self.kubecli.delete_pod.assert_not_called()









if __name__ == "__main__":
    unittest.main()
