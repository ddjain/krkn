#!/usr/bin/env python3

"""
Test suite for ContainerScenarioPlugin class

Usage:
    python -m coverage run -a -m unittest tests/test_container_scenario_plugin.py -v

Assisted By: Claude Code
"""

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import yaml
from krkn_lib.k8s import KrknKubernetes
from krkn_lib.models.telemetry import ScenarioTelemetry
from krkn_lib.telemetry.ocp import KrknTelemetryOpenshift

from krkn.scenario_plugins.container.container_scenario_plugin import ContainerScenarioPlugin


class TestContainerScenarioPlugin(unittest.TestCase):

    def setUp(self):
        """
        Set up test fixtures for ContainerScenarioPlugin
        """
        self.plugin = ContainerScenarioPlugin()

    def tearDown(self):
        """Clean up after each test to prevent state leakage"""
        self.plugin = None

    @staticmethod
    def _container_obj(name):
        """Build a mock container object exposing a ``name`` attribute."""
        container = MagicMock()
        container.name = name
        return container

    def _make_kubecli(self, pod_containers, namespace="test-ns"):
        """
        Build a mocked KrknKubernetes whose ``list_pods``/``get_pod_info``
        reflect the given ``{pod_name: [container_names]}`` mapping.
        """
        kubecli = MagicMock(spec=KrknKubernetes)
        kubecli.list_pods.return_value = list(pod_containers.keys())

        def _get_pod_info(pod, ns):
            info = MagicMock()
            info.containers = [
                self._container_obj(name) for name in pod_containers[pod]
            ]
            return info

        kubecli.get_pod_info.side_effect = _get_pod_info
        kubecli.exec_cmd_in_pod.return_value = ""
        return kubecli

    @staticmethod
    def _scenario(container_name="", count=1, namespace="test-ns"):
        return {
            "name": "test-container-scenario",
            "namespace": namespace,
            "label_selector": "app=test",
            "container_name": container_name,
            "count": count,
            "action": 1,
        }

    def test_get_scenario_types(self):
        """
        Test get_scenario_types returns correct scenario type
        """
        result = self.plugin.get_scenario_types()

        self.assertEqual(result, ["container_scenarios"])
        self.assertEqual(len(result), 1)

    def test_invalid_container_name_raises(self):
        """Invalid container name (absent in all pods) must raise RuntimeError."""
        kubecli = self._make_kubecli(
            {"pod1": ["c1", "c2"], "pod2": ["c1", "c2"]}
        )
        scenario = self._scenario(container_name="nonexistent", count=1)

        with patch.object(self.plugin, "retry_container_killing") as mock_kill:
            with self.assertRaises(RuntimeError) as ctx:
                self.plugin.container_killing_in_pod(scenario, kubecli)

        self.assertIn("nonexistent", str(ctx.exception))
        self.assertIn("not found in any matching pod", str(ctx.exception))
        mock_kill.assert_not_called()

    def test_valid_container_name_kills(self):
        """Valid container name present in all pods kills the right containers."""
        kubecli = self._make_kubecli(
            {
                "pod1": ["target", "sidecar"],
                "pod2": ["target", "sidecar"],
                "pod3": ["target", "sidecar"],
            }
        )
        scenario = self._scenario(container_name="target", count=2)

        with patch.object(self.plugin, "retry_container_killing") as mock_kill:
            killed = self.plugin.container_killing_in_pod(scenario, kubecli)

        self.assertEqual(len(killed), 2)
        self.assertEqual(mock_kill.call_count, 2)
        for entry in killed:
            self.assertEqual(entry[2], "target")

    def test_empty_container_name_kills_first(self):
        """Empty container name kills the first container of each selected pod."""
        kubecli = self._make_kubecli(
            {"pod1": ["c1", "c2"], "pod2": ["c1", "c2"]}
        )
        scenario = self._scenario(container_name="", count=2)

        with patch.object(self.plugin, "retry_container_killing") as mock_kill:
            killed = self.plugin.container_killing_in_pod(scenario, kubecli)

        self.assertEqual(len(killed), 2)
        self.assertEqual(mock_kill.call_count, 2)
        for entry in killed:
            self.assertEqual(entry[2], "c1")

    @patch("krkn.scenario_plugins.container.container_scenario_plugin.random.randint")
    def test_heterogeneous_pods_skips_non_matching(self, mock_randint):
        """Pods without the target container are skipped; kill still succeeds."""
        mock_randint.return_value = 0
        kubecli = self._make_kubecli(
            {
                "other1": ["c1"],
                "other2": ["c1"],
                "target-pod": ["target"],
            }
        )
        scenario = self._scenario(container_name="target", count=1)

        with patch.object(self.plugin, "retry_container_killing") as mock_kill:
            killed = self.plugin.container_killing_in_pod(scenario, kubecli)

        self.assertEqual(len(killed), 1)
        self.assertEqual(killed[0][0], "target-pod")
        self.assertEqual(killed[0][2], "target")
        self.assertEqual(mock_kill.call_count, 1)

    @patch("krkn.scenario_plugins.container.container_scenario_plugin.random.randint")
    def test_count_exceeds_pods_with_target_raises(self, mock_randint):
        """count higher than pods containing the target exhausts list and raises."""
        mock_randint.return_value = 0
        kubecli = self._make_kubecli(
            {
                "target-pod": ["target"],
                "other1": ["c1"],
                "other2": ["c1"],
            }
        )
        scenario = self._scenario(container_name="target", count=2)

        with patch.object(self.plugin, "retry_container_killing") as mock_kill:
            with self.assertRaises(RuntimeError):
                self.plugin.container_killing_in_pod(scenario, kubecli)

        self.assertEqual(mock_kill.call_count, 1)

    @patch("krkn.scenario_plugins.container.container_scenario_plugin.select_and_monitor_by_namespace_pattern_and_label")
    def test_start_monitoring_defaults_recovery_time_to_60(self, mock_monitor):
        """Missing expected_recovery_time waits 60 seconds, as documented."""
        scenario = {"namespace": "kube-system", "label_selector": "k8s-app=kube-dns"}

        self.plugin.start_monitoring(scenario, MagicMock())

        self.assertEqual(mock_monitor.call_args.kwargs["max_timeout"], 60)

    @patch("krkn.scenario_plugins.container.container_scenario_plugin.select_and_monitor_by_namespace_pattern_and_label")
    def test_start_monitoring_uses_expected_recovery_time(self, mock_monitor):
        """expected_recovery_time from the scenario is passed to the monitor."""
        scenario = {
            "namespace": "kube-system",
            "label_selector": "k8s-app=kube-dns",
            "expected_recovery_time": 120,
        }

        self.plugin.start_monitoring(scenario, MagicMock())

        self.assertEqual(mock_monitor.call_args.kwargs["max_timeout"], 120)


    @patch(
        "krkn.scenario_plugins.container.container_scenario_plugin."
        "select_and_monitor_by_namespace_pattern_and_label"
    )
    def test_start_monitoring_wildcard_namespace_matches_all(self, mock_monitor):
        """
        namespace "*" (documented as all namespaces) gives a valid pattern
        that matches any namespace
        """
        import re

        scenario = {
            "namespace": "*",
            "label_selector": "k8s-app=kube-dns",
            "expected_recovery_time": 60,
        }
        self.plugin.start_monitoring(scenario, MagicMock())

        pattern = mock_monitor.call_args.kwargs["namespace_pattern"]
        self.assertTrue(re.match(pattern, "kube-system"))
        self.assertTrue(re.match(pattern, "openshift-etcd"))

    @patch(
        "krkn.scenario_plugins.container.container_scenario_plugin."
        "select_and_monitor_by_namespace_pattern_and_label"
    )
    def test_start_monitoring_named_namespace_matches_exactly(self, mock_monitor):
        """
        A named namespace still matches only itself
        """
        import re

        scenario = {
            "namespace": "kube-system",
            "label_selector": "k8s-app=kube-dns",
            "expected_recovery_time": 60,
        }
        self.plugin.start_monitoring(scenario, MagicMock())

        pattern = mock_monitor.call_args.kwargs["namespace_pattern"]
        self.assertTrue(re.match(pattern, "kube-system"))
        self.assertFalse(re.match(pattern, "kube-system-2"))
        self.assertFalse(re.match(pattern, "kubexsystem"))

    @patch(
        "krkn.scenario_plugins.container.container_scenario_plugin."
        "select_and_monitor_by_namespace_pattern_and_label"
    )
    def test_start_monitoring_defaults_namespace_and_label_selector(self, mock_monitor):
        """
        Missing namespace and label_selector use the same defaults as
        container_killing_in_pod: all namespaces, no label filter
        """
        import re

        self.plugin.start_monitoring({"expected_recovery_time": 60}, MagicMock())

        kwargs = mock_monitor.call_args.kwargs
        self.assertTrue(re.match(kwargs["namespace_pattern"], "kube-system"))
        self.assertTrue(re.match(kwargs["namespace_pattern"], "default"))
        self.assertIsNone(kwargs["label_selector"])

    @patch(
        "krkn.scenario_plugins.container.container_scenario_plugin."
        "select_and_monitor_by_namespace_pattern_and_label"
    )
    def test_start_monitoring_dot_star_namespace_matches_all(self, mock_monitor):
        """
        namespace ".*" also means all namespaces
        """
        import re

        scenario = {"namespace": ".*", "expected_recovery_time": 60}
        self.plugin.start_monitoring(scenario, MagicMock())

        pattern = mock_monitor.call_args.kwargs["namespace_pattern"]
        self.assertTrue(re.match(pattern, "kube-system"))
        self.assertTrue(re.match(pattern, "default"))

    def _kill_all_namespaces(self, scenario):
        kubecli = MagicMock(spec=KrknKubernetes)
        kubecli.get_all_pods.return_value = [["pod1", "ns1"]]
        info = MagicMock()
        info.containers = [self._container_obj("c1")]
        kubecli.get_pod_info.return_value = info
        with patch.object(self.plugin, "retry_container_killing") as mock_kill:
            killed = self.plugin.container_killing_in_pod(scenario, kubecli)
        kubecli.get_all_pods.assert_called_once_with("app=test")
        kubecli.list_pods.assert_not_called()
        self.assertEqual(killed, [["pod1", "ns1", "c1"]])
        self.assertEqual(mock_kill.call_count, 1)

    def test_kill_dot_star_namespace_uses_all_pods(self):
        """namespace ".*" kills from all namespaces, like "*"."""
        self._kill_all_namespaces(self._scenario(namespace=".*"))

    def test_kill_star_namespace_uses_all_pods(self):
        """namespace "*" still kills from all namespaces."""
        self._kill_all_namespaces(self._scenario(namespace="*"))

    def test_kill_missing_namespace_uses_all_pods(self):
        """A scenario without namespace kills from all namespaces."""
        scenario = self._scenario()
        del scenario["namespace"]
        self._kill_all_namespaces(scenario)

    def test_kill_pod_names_with_dot_star_namespace_raises(self):
        """pod_names need a real namespace; ".*" is rejected like "*"."""
        scenario = self._scenario(namespace=".*")
        scenario["pod_names"] = ["pod1"]
        kubecli = MagicMock(spec=KrknKubernetes)
        with self.assertRaises(RuntimeError):
            self.plugin.container_killing_in_pod(scenario, kubecli)
        kubecli.get_pod_info.assert_not_called()

class TestContainerScenarioPluginRun(unittest.TestCase):
    """Tests for ContainerScenarioPlugin.run()."""

    def setUp(self):
        self.plugin = ContainerScenarioPlugin()
        self.kubecli = MagicMock(spec=KrknKubernetes)
        self.tel = MagicMock(spec=KrknTelemetryOpenshift)
        self.tel.get_lib_kubernetes.return_value = self.kubecli
        self.scenario_telemetry = MagicMock(spec=ScenarioTelemetry)
        self.snapshot = MagicMock()
        self.pods_status = SimpleNamespace(unrecovered=[], recovered=[])
        self.snapshot.get_pods_status.return_value = self.pods_status
        self.future = MagicMock()
        self.future.result.return_value = self.snapshot
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def _write_config(self, data):
        """Write ``data`` as YAML to a temp file and return its path."""
        path = os.path.join(self.tmpdir.name, "scenario.yaml")
        with open(path, "w") as f:
            yaml.safe_dump(data, f)
        return path

    def _scenario_block(self, name="scenario-1"):
        return {
            "name": name,
            "namespace": "test-ns",
            "label_selector": "app=test",
            "actions": [{"kill": {"containers": [{"name": "c1"}]}}],
        }

    def _patch_run_collaborators(self, kill=None):
        monitor_patch = patch.object(
            self.plugin, "start_monitoring", return_value=self.future
        )
        kill_patch = patch.object(
            self.plugin, "container_killing_in_pod", side_effect=kill
        )
        self.mock_monitor = monitor_patch.start()
        self.mock_kill = kill_patch.start()
        self.addCleanup(monitor_patch.stop)
        self.addCleanup(kill_patch.stop)

    def test_run_success_returns_zero(self):
        """A fully recovered scenario exits 0 and records affected pods."""
        path = self._write_config({"scenarios": [self._scenario_block()]})
        self._patch_run_collaborators()

        result = self.plugin.run(
            "uuid", path, self.tel, self.scenario_telemetry
        )

        self.assertEqual(result, 0)
        self.assertIs(self.scenario_telemetry.affected_pods, self.pods_status)
        self.mock_monitor.assert_called_once()
        self.assertEqual(self.mock_monitor.call_args.args[0], self._scenario_block())
        self.assertIs(self.mock_monitor.call_args.args[1], self.tel)
        self.mock_kill.assert_called_once_with(
            self._scenario_block(), self.kubecli
        )

    def test_run_unrecovered_pods_returns_one(self):
        """Unrecovered pods after the kill make run() exit 1."""
        self.pods_status.unrecovered = [MagicMock(name="pod-1")]
        path = self._write_config(
            {"scenarios": [self._scenario_block("s1"), self._scenario_block("s2")]}
        )
        self._patch_run_collaborators()

        result = self.plugin.run(
            "uuid", path, self.tel, self.scenario_telemetry
        )

        # CHARACTERIZATION: the first scenario with unrecovered containers ends
        # the whole run, so the second scenario is never monitored or killed.
        self.assertEqual(result, 1)
        self.assertIs(self.scenario_telemetry.affected_pods, self.pods_status)
        self.mock_monitor.assert_called_once()
        self.mock_kill.assert_called_once_with(
            self._scenario_block("s1"), self.kubecli
        )

    def test_run_kill_raises_returns_one(self):
        """An exception from container_killing_in_pod exits 1 before waiting."""
        path = self._write_config({"scenarios": [self._scenario_block()]})
        self._patch_run_collaborators(kill=RuntimeError("kill failed"))

        result = self.plugin.run(
            "uuid", path, self.tel, self.scenario_telemetry
        )

        self.assertEqual(result, 1)
        self.future.result.assert_not_called()

    def test_run_monitor_future_raises_returns_one(self):
        """An exception from the monitoring future result exits 1."""
        self.future.result.side_effect = RuntimeError("monitor failed")
        path = self._write_config({"scenarios": [self._scenario_block()]})
        self._patch_run_collaborators()

        result = self.plugin.run(
            "uuid", path, self.tel, self.scenario_telemetry
        )

        self.assertEqual(result, 1)


    def test_run_missing_scenarios_key_returns_one(self):
        """A config without the 'scenarios' key is caught and exits 1."""
        # CHARACTERIZATION: the KeyError is caught by run()'s broad handler and
        # reported through logging.error; it is not raised to the caller.
        path = self._write_config({})
        self._patch_run_collaborators()

        with self.assertLogs(level="ERROR") as logs:
            result = self.plugin.run(
                "uuid", path, self.tel, self.scenario_telemetry
            )

        self.assertEqual(result, 1)
        self.mock_monitor.assert_not_called()
        self.assertRegex(
            "\n".join(logs.output), "exiting due to Exception 'scenarios'"
        )

    def test_run_multiple_scenarios_all_executed_in_order(self):
        """Every scenario is monitored and killed before its result check."""
        path = self._write_config(
            {"scenarios": [self._scenario_block("s1"), self._scenario_block("s2")]}
        )
        events = []

        def _monitor(kill_scenario, tel):
            events.append("monitor:" + kill_scenario["name"])
            return self.future

        def _kill(kill_scenario, kubecli):
            events.append("kill:" + kill_scenario["name"])

        def _result():
            events.append("result")
            return self.snapshot

        with patch.object(self.plugin, "start_monitoring", side_effect=_monitor):
            with patch.object(
                self.plugin, "container_killing_in_pod", side_effect=_kill
            ):
                self.future.result.side_effect = _result
                result = self.plugin.run(
                    "uuid", path, self.tel, self.scenario_telemetry
                )

        self.assertEqual(result, 0)
        self.assertEqual(
            events,
            [
                "monitor:s1",
                "kill:s1",
                "result",
                "monitor:s2",
                "kill:s2",
                "result",
            ],
        )

    def test_run_empty_scenarios_list_returns_zero(self):
        """An empty scenarios list exits 0 without any monitoring or kills."""
        path = self._write_config({"scenarios": []})
        self._patch_run_collaborators()

        result = self.plugin.run(
            "uuid", path, self.tel, self.scenario_telemetry
        )

        self.assertEqual(result, 0)
        self.mock_monitor.assert_not_called()
        self.mock_kill.assert_not_called()

    def test_run_empty_or_null_config_returns_one(self):
        """An empty or null scenario file has no scenarios and exits 1."""
        self._patch_run_collaborators()
        for content in ("", "null\n"):
            with self.subTest(content=content):
                path = os.path.join(self.tmpdir.name, "empty.yaml")
                with open(path, "w") as f:
                    f.write(content)

                result = self.plugin.run(
                    "uuid", path, self.tel, self.scenario_telemetry
                )

                self.assertEqual(result, 1)
                self.mock_monitor.assert_not_called()


class TestContainerKillingValidation(unittest.TestCase):
    """Validation branches of container_killing_in_pod()."""

    def setUp(self):
        self.plugin = ContainerScenarioPlugin()

    @staticmethod
    def _scenario():
        return {
            "name": "test-container-scenario",
            "namespace": "test-ns",
            "label_selector": "app=test",
            "container_name": "c1",
            "count": 1,
            "action": 1,
        }

    def test_action_non_integer_raises_before_kube_calls(self):
        """Non-integer action values are rejected before any kube call."""
        for action in ("1", 1.5):
            with self.subTest(action=action):
                kubecli = MagicMock(spec=KrknKubernetes)
                scenario = self._scenario()
                scenario["action"] = action

                with self.assertLogs(level="ERROR") as logs:
                    with self.assertRaises(RuntimeError):
                        self.plugin.container_killing_in_pod(scenario, kubecli)
                self.assertRegex(
                    "\n".join(logs.output),
                    "action parameter defined in the config is an integer",
                )

                kubecli.list_pods.assert_not_called()
                kubecli.get_all_pods.assert_not_called()
                kubecli.get_pod_info.assert_not_called()

    def test_action_out_of_range_raises(self):
        """Kill signals outside the supported 1-15 range are rejected."""
        for action in (0, 16, -3):
            with self.subTest(action=action):
                kubecli = MagicMock(spec=KrknKubernetes)
                scenario = self._scenario()
                scenario["action"] = action

                with self.assertLogs(level="ERROR") as logs:
                    with self.assertRaises(RuntimeError):
                        self.plugin.container_killing_in_pod(scenario, kubecli)
                self.assertIn("Only 1-15 kill signals are supported.", logs.output[0])

    def test_signal_action_forwarded_in_kill_command(self):
        """Scenario action N is sent to the pod as the command 'kill N'."""
        for action in (1, 9, 15):
            with self.subTest(action=action):
                kubecli = MagicMock(spec=KrknKubernetes)
                info = MagicMock()
                info.containers = [SimpleNamespace(name="c1")]
                kubecli.get_pod_info.return_value = info
                kubecli.exec_cmd_in_pod.return_value = ""
                scenario = {
                    "name": "s",
                    "namespace": "test-ns",
                    "container_name": "c1",
                    "count": 1,
                    "action": action,
                    "pod_names": ["pod1"],
                }

                self.plugin.container_killing_in_pod(scenario, kubecli)

                kubecli.exec_cmd_in_pod.assert_called_once_with(
                    "kill " + str(action), "pod1", "test-ns", "c1"
                )

    @patch("krkn.scenario_plugins.container.container_scenario_plugin.time.sleep")
    def test_action_bool_rejected_as_non_signal(self, mock_sleep):
        """A boolean action is rejected instead of sending 'kill True'."""
        kubecli = MagicMock(spec=KrknKubernetes)
        kubecli.list_pods.return_value = ["pod1"]
        info = MagicMock()
        info.containers = [SimpleNamespace(name="c1")]
        kubecli.get_pod_info.return_value = info
        kubecli.exec_cmd_in_pod.return_value = ""
        scenario = self._scenario()
        scenario["action"] = True

        with self.assertRaises(RuntimeError):
            self.plugin.container_killing_in_pod(scenario, kubecli)

        for sent in kubecli.exec_cmd_in_pod.call_args_list:
            self.assertNotEqual(sent.args[0], "kill True")

    def test_pod_names_not_a_list_raises(self):
        """pod_names that is not a list is rejected before any pod lookup."""
        kubecli = MagicMock(spec=KrknKubernetes)
        scenario = self._scenario()
        scenario["pod_names"] = "pod1"

        with self.assertLogs(level="ERROR") as logs:
            with self.assertRaises(RuntimeError):
                self.plugin.container_killing_in_pod(scenario, kubecli)
        self.assertRegex("\n".join(logs.output), "pod_names are in a list format")

        kubecli.get_pod_info.assert_not_called()



class TestContainerKillingSelection(unittest.TestCase):
    """Pod-selection branches of container_killing_in_pod()."""

    def setUp(self):
        self.plugin = ContainerScenarioPlugin()

    @staticmethod
    def _container(name):
        return SimpleNamespace(name=name)

    def _kubecli(self, pod_containers):
        kubecli = MagicMock(spec=KrknKubernetes)
        kubecli.list_pods.return_value = list(pod_containers.keys())

        def _get_pod_info(pod, ns):
            info = MagicMock()
            info.containers = [
                self._container(n) for n in pod_containers[pod]
            ]
            return info

        kubecli.get_pod_info.side_effect = _get_pod_info
        return kubecli

    @staticmethod
    def _scenario(**overrides):
        scenario = {
            "name": "test-container-scenario",
            "namespace": "test-ns",
            "label_selector": "app=test",
            "container_name": "c1",
            "count": 1,
            "action": 1,
        }
        scenario.update(overrides)
        return scenario

    def test_exclude_label_set_passed_to_list_pods(self):
        """A non-empty exclude_label is forwarded to list_pods."""
        kubecli = self._kubecli({"pod1": ["c1"]})
        scenario = self._scenario(exclude_label="foo=bar")

        with patch.object(self.plugin, "retry_container_killing"):
            self.plugin.container_killing_in_pod(scenario, kubecli)

        kubecli.list_pods.assert_called_once_with(
            namespace="test-ns",
            label_selector="app=test",
            exclude_label="foo=bar",
        )

    def test_exclude_label_empty_passed_as_none(self):
        """An empty exclude_label is forwarded to list_pods as None."""
        kubecli = self._kubecli({"pod1": ["c1"]})
        scenario = self._scenario(exclude_label="")

        with patch.object(self.plugin, "retry_container_killing"):
            self.plugin.container_killing_in_pod(scenario, kubecli)

        self.assertIsNone(kubecli.list_pods.call_args.kwargs["exclude_label"])

    def test_label_selector_absent_defaults_to_none(self):
        """A scenario without label_selector passes None, not a crash."""
        kubecli = self._kubecli({"pod1": ["c1"]})
        scenario = self._scenario()
        del scenario["label_selector"]

        with patch.object(self.plugin, "retry_container_killing"):
            self.plugin.container_killing_in_pod(scenario, kubecli)

        self.assertIsNone(kubecli.list_pods.call_args.kwargs["label_selector"])

    def test_pod_names_named_namespace_kills_listed_pods_only(self):
        """pod_names with a named namespace kills exactly the listed pods."""
        kubecli = self._kubecli({"podA": ["c1"], "podB": ["c1"]})
        scenario = self._scenario(pod_names=["podA", "podB"], count=2)

        with patch.object(self.plugin, "retry_container_killing") as mock_kill:
            killed = self.plugin.container_killing_in_pod(scenario, kubecli)

        kubecli.list_pods.assert_not_called()
        kubecli.get_all_pods.assert_not_called()
        self.assertEqual(
            sorted(entry[0] for entry in killed), ["podA", "podB"]
        )
        for entry in killed:
            self.assertEqual(entry[1], "test-ns")
        self.assertEqual(mock_kill.call_count, 2)

    def test_empty_pod_names_falls_through_to_list_pods(self):
        """An empty pod_names list selects pods via list_pods like an absent one."""
        kubecli = self._kubecli({"pod1": ["c1"]})
        scenario = self._scenario(pod_names=[])

        with patch.object(self.plugin, "retry_container_killing") as mock_kill:
            killed = self.plugin.container_killing_in_pod(scenario, kubecli)

        kubecli.list_pods.assert_called_once_with(
            namespace="test-ns",
            label_selector="app=test",
            exclude_label=None,
        )
        self.assertEqual(killed, [["pod1", "test-ns", "c1"]])
        mock_kill.assert_called_once()

    def test_pod_info_none_propagates_attribute_error(self):
        """A missing pod (get_pod_info None) surfaces as AttributeError."""
        # CHARACTERIZATION: container_killing_in_pod does not guard a None pod
        # info, so AttributeError escapes. run() catches any exception and
        # turns it into exit code 1.
        kubecli = MagicMock(spec=KrknKubernetes)
        kubecli.list_pods.return_value = ["gone-pod"]
        kubecli.get_pod_info.return_value = None
        scenario = self._scenario()

        with patch.object(self.plugin, "retry_container_killing") as mock_kill:
            with self.assertRaisesRegex(
                AttributeError, "'NoneType' object has no attribute 'containers'"
            ):
                self.plugin.container_killing_in_pod(scenario, kubecli)

        mock_kill.assert_not_called()

    def test_count_zero_raises(self):
        """count < 1 is rejected: a destructive scenario must not succeed with no kills."""
        kubecli = self._kubecli({"pod1": ["c1"]})
        scenario = self._scenario(count=0)

        with patch.object(self.plugin, "retry_container_killing") as mock_kill:
            with self.assertRaisesRegex(RuntimeError, "count"):
                self.plugin.container_killing_in_pod(scenario, kubecli)

        mock_kill.assert_not_called()

    def test_count_non_integer_raises_runtime_error(self):
        """A non-integer count is rejected with RuntimeError, not a bare TypeError."""
        kubecli = self._kubecli({"pod1": ["c1"]})
        scenario = self._scenario(count="2")

        with patch.object(self.plugin, "retry_container_killing") as mock_kill:
            with self.assertRaisesRegex(RuntimeError, "count"):
                self.plugin.container_killing_in_pod(scenario, kubecli)

        mock_kill.assert_not_called()

    def test_container_name_matches_exactly_no_substring(self):
        """container_name must match exactly; "c" does not match "c1"."""
        kubecli = self._kubecli({"pod1": ["c1", "c2"], "pod2": ["c1"]})
        scenario = self._scenario(container_name="c")

        with patch.object(self.plugin, "retry_container_killing") as mock_kill:
            with self.assertRaisesRegex(
                RuntimeError, "Container 'c' not found in any matching pod"
            ):
                self.plugin.container_killing_in_pod(scenario, kubecli)

        mock_kill.assert_not_called()

    def test_container_name_longer_than_pod_container_not_matched(self):
        """container_name "c12" must not match a pod container "c1" (no reverse substring match)."""
        kubecli = self._kubecli({"pod1": ["c1"]})
        scenario = self._scenario(container_name="c12")

        with patch.object(self.plugin, "retry_container_killing") as mock_kill:
            with self.assertRaisesRegex(
                RuntimeError, "Container 'c12' not found in any matching pod"
            ):
                self.plugin.container_killing_in_pod(scenario, kubecli)

        mock_kill.assert_not_called()
        kubecli.exec_cmd_in_pod.assert_not_called()



class TestRetryContainerKilling(unittest.TestCase):
    """Retry loop of retry_container_killing()."""

    def setUp(self):
        self.plugin = ContainerScenarioPlugin()
        self.kubecli = MagicMock(spec=KrknKubernetes)

    def _retry(self, action=1):
        return self.plugin.retry_container_killing(
            "kill " + str(action), "pod1", "ns1", "c1", self.kubecli
        )

    @patch("krkn.scenario_plugins.container.container_scenario_plugin.time.sleep")
    def test_blank_first_response_single_exec_no_sleep(self, mock_sleep):
        """A blank first response stops the retry loop immediately."""
        self.kubecli.exec_cmd_in_pod.return_value = ""

        self._retry()

        self.assertEqual(self.kubecli.exec_cmd_in_pod.call_count, 1)
        mock_sleep.assert_not_called()

    @patch("krkn.scenario_plugins.container.container_scenario_plugin.time.sleep")
    def test_unauthorized_then_success_retries_once(self, mock_sleep):
        """An unauthorized response retries after 2s and stops on blank."""
        self.kubecli.exec_cmd_in_pod.side_effect = ["Unauthorized", ""]

        self._retry()

        self.assertEqual(self.kubecli.exec_cmd_in_pod.call_count, 2)
        mock_sleep.assert_called_once_with(2)

    @patch("krkn.scenario_plugins.container.container_scenario_plugin.time.sleep")
    def test_authorization_spelling_also_retries(self, mock_sleep):
        """'authorization denied' hits the same retry branch as 'unauthorized'."""
        self.kubecli.exec_cmd_in_pod.side_effect = [
            "authorization denied",
            "",
        ]

        self._retry()

        self.assertEqual(self.kubecli.exec_cmd_in_pod.call_count, 2)
        mock_sleep.assert_called_once_with(2)

    @patch("krkn.scenario_plugins.container.container_scenario_plugin.time.sleep")
    def test_persistent_stderr_capped_at_five_attempts(self, mock_sleep):
        """Non-auth stderr never sleeps and stops after 5 attempts."""
        # CHARACTERIZATION: bound of the retry loop; the loop gives up after
        # exactly 5 attempts and raises RuntimeError.
        self.kubecli.exec_cmd_in_pod.return_value = "OCI runtime exec failed"

        with self.assertRaises(RuntimeError):
            self._retry()

        self.assertEqual(self.kubecli.exec_cmd_in_pod.call_count, 5)
        mock_sleep.assert_not_called()

    @patch("krkn.scenario_plugins.container.container_scenario_plugin.time.sleep")
    def test_persistent_stderr_exhausted_raises(self, mock_sleep):
        """Exhausted retries raise so a kill that never happened is not reported."""
        self.kubecli.exec_cmd_in_pod.return_value = "OCI runtime exec failed"

        with self.assertRaises(RuntimeError):
            self._retry()

    @patch("krkn.scenario_plugins.container.container_scenario_plugin.time.sleep")
    def test_unauthorized_capped_at_five_attempts_with_sleeps(self, mock_sleep):
        """Unauthorized responses sleep 2s between attempts and stop after 5."""
        # CHARACTERIZATION: bound of the retry loop for auth failures; five
        # attempts, five 2-second sleeps, then RuntimeError.
        self.kubecli.exec_cmd_in_pod.return_value = "Unauthorized"

        with self.assertRaises(RuntimeError):
            self._retry()

        self.assertEqual(self.kubecli.exec_cmd_in_pod.call_count, 5)
        self.assertEqual(mock_sleep.call_args_list, [call(2)] * 5)

    @patch("krkn.scenario_plugins.container.container_scenario_plugin.time.sleep")
    def test_unauthorized_exhausted_raises(self, mock_sleep):
        """Five consecutive unauthorized responses raise instead of reporting a kill."""
        self.kubecli.exec_cmd_in_pod.return_value = "Unauthorized"

        with self.assertRaises(RuntimeError):
            self._retry()
        self.assertEqual(self.kubecli.exec_cmd_in_pod.call_count, 5)

    def test_exec_raises_propagates(self):
        """An exception from exec_cmd_in_pod is not swallowed."""
        self.kubecli.exec_cmd_in_pod.side_effect = Exception("connection refused")

        with self.assertRaisesRegex(Exception, "connection refused"):
            self._retry()


class TestCheckFailedContainers(unittest.TestCase):
    """Recovery-wait loop of check_failed_containers()."""

    def setUp(self):
        self.plugin = ContainerScenarioPlugin()

    @staticmethod
    def _containers(*specs):
        """Build a pod_info-like object from (name, ready) specs."""
        info = MagicMock()
        info.containers = [
            SimpleNamespace(name=name, ready=ready) for name, ready in specs
        ]
        return info

    def _kubecli(self, pod_info_by_call):
        """get_pod_info returns pod_info_by_call[pod][call_index]."""
        kubecli = MagicMock(spec=KrknKubernetes)
        counts = {}

        def _get_pod_info(pod, ns):
            counts[pod] = counts.get(pod, 0)
            info = pod_info_by_call[pod][counts[pod]]
            counts[pod] += 1
            return info

        kubecli.get_pod_info.side_effect = _get_pod_info
        return kubecli

    @patch("krkn.scenario_plugins.container.container_scenario_plugin.time.sleep")
    def test_all_ready_first_pass_returns_empty(self, mock_sleep):
        """A ready container is removed on the first pass."""
        kubecli = self._kubecli(
            {"pod1": [self._containers(("c1", True))]}
        )

        result = self.plugin.check_failed_containers(
            [["pod1", "ns1", "c1"]], 0, kubecli
        )

        self.assertEqual(result, [])
        kubecli.get_pod_info.assert_called_once_with("pod1", "ns1")
        mock_sleep.assert_not_called()

    @patch("krkn.scenario_plugins.container.container_scenario_plugin.time.sleep")
    def test_ready_on_later_poll_returns_empty(self, mock_sleep):
        """A container becoming ready on the second poll ends the wait."""
        kubecli = self._kubecli(
            {
                "pod1": [
                    self._containers(("c1", False)),
                    self._containers(("c1", True)),
                ]
            }
        )

        result = self.plugin.check_failed_containers(
            [["pod1", "ns1", "c1"]], 10, kubecli
        )

        self.assertEqual(result, [])
        self.assertEqual(kubecli.get_pod_info.call_count, 2)
        mock_sleep.assert_called_once_with(5)

    @patch("krkn.scenario_plugins.container.container_scenario_plugin.time.sleep")
    def test_never_ready_wait_time_zero_single_pass(self, mock_sleep):
        """wait_time=0 runs exactly one pass and returns the entry."""
        kubecli = self._kubecli(
            {"pod1": [self._containers(("c1", False))]}
        )
        killed = [["pod1", "ns1", "c1"]]

        result = self.plugin.check_failed_containers(
            killed, 0, kubecli
        )

        self.assertEqual(result, [["pod1", "ns1", "c1"]])
        self.assertEqual(kubecli.get_pod_info.call_count, 1)
        mock_sleep.assert_called_once_with(5)

    @patch("krkn.scenario_plugins.container.container_scenario_plugin.time.sleep")
    def test_poll_cadence_five_second_steps(self, mock_sleep):
        """wait_time=12 polls at t=0,5,10 with a 5s sleep between passes."""
        kubecli = self._kubecli(
            {"pod1": [self._containers(("c1", False))] * 4}
        )

        result = self.plugin.check_failed_containers(
            [["pod1", "ns1", "c1"]], 12, kubecli
        )

        self.assertEqual(result, [["pod1", "ns1", "c1"]])
        self.assertEqual(kubecli.get_pod_info.call_count, 3)
        self.assertEqual(
            mock_sleep.call_args_list, [call(5), call(5), call(5)]
        )

    @patch("krkn.scenario_plugins.container.container_scenario_plugin.time.sleep")
    def test_container_gone_from_pod_stays_unrecovered(self, mock_sleep):
        """A killed container missing from pod_info is never marked ready."""
        kubecli = self._kubecli(
            {"pod1": [self._containers(("other", True))]}
        )

        result = self.plugin.check_failed_containers(
            [["pod1", "ns1", "gone-c"]], 0, kubecli
        )

        self.assertEqual(result, [["pod1", "ns1", "gone-c"]])

    @patch("krkn.scenario_plugins.container.container_scenario_plugin.time.sleep")
    def test_several_passes_before_recovery_returns_empty(self, mock_sleep):
        """A container unready for two polls and ready on the third ends the wait."""
        kubecli = self._kubecli(
            {
                "pod1": [
                    self._containers(("c1", False)),
                    self._containers(("c1", False)),
                    self._containers(("c1", True)),
                ]
            }
        )

        result = self.plugin.check_failed_containers(
            [["pod1", "ns1", "c1"]], 10, kubecli
        )

        self.assertEqual(result, [])
        self.assertEqual(kubecli.get_pod_info.call_count, 3)
        self.assertEqual(mock_sleep.call_args_list, [call(5), call(5)])

    @patch("krkn.scenario_plugins.container.container_scenario_plugin.time.sleep")
    def test_all_ready_same_pass_recovers_every_container(self, mock_sleep):
        """Containers recovering in the same pass are all removed."""
        kubecli = self._kubecli(
            {
                "podA": [self._containers(("c1", True))],
                "podB": [self._containers(("c1", True))],
            }
        )

        result = self.plugin.check_failed_containers(
            [["podA", "ns1", "c1"], ["podB", "ns1", "c1"]], 0, kubecli
        )

        self.assertEqual(result, [])

    @patch("krkn.scenario_plugins.container.container_scenario_plugin.time.sleep")
    def test_partial_recovery_returns_unrecovered_container(self, mock_sleep):
        """Only the recovered container is dropped; unrecovered ones are returned."""
        kubecli = self._kubecli(
            {
                "podA": [self._containers(("c1", True))],
                "podB": [self._containers(("c1", False))],
            }
        )

        result = self.plugin.check_failed_containers(
            [["podA", "ns1", "c1"], ["podB", "ns1", "c1"]], 0, kubecli
        )

        self.assertEqual(result, [["podB", "ns1", "c1"]])


if __name__ == "__main__":
    unittest.main()
