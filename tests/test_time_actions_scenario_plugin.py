#!/usr/bin/env python3

"""
Test suite for TimeActionsScenarioPlugin class

Usage:
    python -m coverage run -a -m unittest tests/test_time_actions_scenario_plugin.py -v

Assisted By: Claude Code
"""

import unittest
from unittest.mock import MagicMock, patch
import datetime
import time

from krkn_lib.k8s import KrknKubernetes
from krkn_lib.telemetry.ocp import KrknTelemetryOpenshift

from krkn.scenario_plugins.time_actions.time_actions_scenario_plugin import TimeActionsScenarioPlugin


class TestTimeActionsScenarioPlugin(unittest.TestCase):

    def setUp(self):
        """
        Set up test fixtures for TimeActionsScenarioPlugin
        """
        self.plugin = TimeActionsScenarioPlugin()

    def tearDown(self):
        """Clean up after each test to prevent state leakage"""
        self.plugin = None

    def test_get_scenario_types(self):
        """
        Test get_scenario_types returns correct scenario type
        """
        result = self.plugin.get_scenario_types()

        self.assertEqual(result, ["time_scenarios"])
        self.assertEqual(len(result), 1)

    @patch("krkn.scenario_plugins.time_actions.time_actions_scenario_plugin.logging")
    @patch("builtins.open", side_effect=RuntimeError("disk quota exceeded"))
    def test_exception_variable_bound_in_except_handler(self, mock_open, mock_logging):
        """run() must bind exception variable so logging shows actual error, not NameError"""
        result = self.plugin.run(
            run_uuid="test-uuid",
            scenario="fake_scenario.yaml",
            lib_telemetry=MagicMock(),
            scenario_telemetry=MagicMock(),
        )

        self.assertEqual(result, 1)
        logged_msg = mock_logging.error.call_args[0][0]
        self.assertIn("disk quota exceeded", logged_msg)
        self.assertNotIn("NameError", logged_msg)
    @unittest.mock.patch('builtins.open', create=True)
    @unittest.mock.patch('yaml.safe_load')
    @unittest.mock.patch('logging.error')
    def test_run_exception_handling_with_variable(self, mock_logging_error, mock_yaml, mock_open):
        """
        Test that run() properly captures exception variable and logs it
        This tests the fix for the undefined variable 'e' bug
        """
        # Setup mock to raise exception
        mock_yaml.side_effect = RuntimeError("Test exception message")
        
        mock_lib_telemetry = MagicMock()
        mock_scenario_telemetry = MagicMock()
        
        # Execute the run method
        result = self.plugin.run(
            run_uuid="test-uuid",
            scenario="test_scenario.yaml",
            lib_telemetry=mock_lib_telemetry,
            scenario_telemetry=mock_scenario_telemetry
        )
        
        # Assert failure is returned
        self.assertEqual(result, 1)
        
        # Assert logging.error was called with the exception message
        mock_logging_error.assert_called_once()
        error_call_args = str(mock_logging_error.call_args)
        self.assertIn("Test exception message", error_call_args)
        self.assertIn("TimeActionsScenarioPlugin", error_call_args)

    @unittest.mock.patch('builtins.open', create=True)
    @unittest.mock.patch('yaml.safe_load')
    def test_run_with_skew_time_exception(self, mock_yaml, mock_open):
        """
        Test that run() handles exceptions from skew_time method
        """
        # Setup mock scenario config
        mock_yaml.return_value = {
            "time_scenarios": [
                {
                    "action": "skew_time",
                    "object_type": "node",
                    "object_name": ["test-node"]
                }
            ]
        }
        
        mock_lib_telemetry = MagicMock()
        mock_kubecli = MagicMock()
        mock_lib_telemetry.get_lib_kubernetes.return_value = mock_kubecli
        
        # Make skew_time raise an exception
        with unittest.mock.patch.object(self.plugin, 'skew_time', side_effect=Exception("Skew failed")):
            mock_scenario_telemetry = MagicMock()
                
            # Execute the run method
            result = self.plugin.run(
                run_uuid="test-uuid",
                scenario="test_scenario.yaml",
                    lib_telemetry=mock_lib_telemetry,
                scenario_telemetry=mock_scenario_telemetry
            )
            
            # Assert failure is returned
            self.assertEqual(result, 1)

    @patch('krkn.scenario_plugins.time_actions.time_actions_scenario_plugin.KrknKubernetes')
    def test_exec_with_shell_fallback_detects_persistent_shell_error(self, mock_kubecli_class):
        """Test that exec_with_shell_fallback returns False when both direct and shell execution fail persistently"""
        mock_kubecli = MagicMock()
        mock_kubecli_class.return_value = mock_kubecli
        mock_kubecli.exec_cmd_in_pod.side_effect = Exception("Command failed")
        
        result = self.plugin.exec_with_shell_fallback(
            "test", "test-pod", "default", "test-container", mock_kubecli
        )
        
        self.assertFalse(result)

    @patch('krkn.scenario_plugins.time_actions.time_actions_scenario_plugin.KrknKubernetes')
    def test_exec_with_shell_fallback_fails_after_max_retries(self, mock_kubecli_class):
        """Test that exec_with_shell_fallback returns False after exhausting all retries"""
        mock_kubecli = MagicMock()
        mock_kubecli_class.return_value = mock_kubecli
        mock_kubecli.exec_cmd_in_pod.side_effect = Exception("Command failed")
        
        result = self.plugin.exec_with_shell_fallback(
            "test", "test-pod", "default", "test-container", mock_kubecli, max_retries=2
        )
        
        self.assertFalse(result)

    @patch('krkn.scenario_plugins.time_actions.time_actions_scenario_plugin.KrknKubernetes')
    def test_exec_with_shell_fallback_retries_on_error(self, mock_kubecli_class):
        """Test that exec_with_shell_fallback succeeds after retrying"""
        mock_kubecli = MagicMock()
        mock_kubecli_class.return_value = mock_kubecli
        # First two calls fail, third succeeds
        mock_kubecli.exec_cmd_in_pod.side_effect = [
            Exception("First failure"),
            Exception("Second failure"), 
            "success"
        ]
        
        result = self.plugin.exec_with_shell_fallback(
            "test", "test-pod", "default", "test-container", mock_kubecli, max_retries=3
        )
        
        self.assertEqual(result, "success")

class TestTimeActionsScenarioPluginExtended(unittest.TestCase):
    """Extended coverage for TimeActionsScenarioPlugin: run, pod_exec,
    exec_with_shell_fallback, get_container_name, skew_node, skew_time,
    parse_string_date, string_to_date and check_date_time."""

    MOD = "krkn.scenario_plugins.time_actions.time_actions_scenario_plugin"

    def setUp(self):
        self.plugin = TimeActionsScenarioPlugin()

    def _kube(self):
        return MagicMock(spec=KrknKubernetes)

    def _run_with_config(self, scenario_config, lib_telemetry=None):
        if lib_telemetry is None:
            lib_telemetry = MagicMock()
        with patch("builtins.open", create=True):
            with patch("yaml.safe_load", return_value=scenario_config):
                return self.plugin.run(
                    "test-uuid",
                    "test_scenario.yaml",
                    lib_telemetry,
                    MagicMock(),
                )

    def test_run_single_scenario_returns_zero(self):
        """run() returns 0 when every scenario succeeds and passes the
        lib kubernetes client to skew_time and check_date_time."""
        scenario = {"action": "skew_time", "object_type": "pod"}
        kube = self._kube()
        lib_telemetry = MagicMock()
        lib_telemetry.get_lib_kubernetes.return_value = kube
        with patch.object(self.plugin, "skew_time", return_value=("pod", [["p1", "ns1", "c1"]])) as mock_skew:
            with patch.object(self.plugin, "check_date_time", return_value=[]) as mock_check:
                result = self._run_with_config(
                    {"time_scenarios": [scenario]}, lib_telemetry
                )
        self.assertEqual(result, 0)
        mock_skew.assert_called_once_with(scenario, kube)
        mock_check.assert_called_once_with("pod", [["p1", "ns1", "c1"]], kube)
        self.assertEqual(lib_telemetry.get_lib_kubernetes.call_count, 2)

    def test_run_not_reset_objects_logs_and_still_succeeds(self):
        """run() logs when check_date_time reports objects not reset but
        still treats the scenario as successful."""
        # CHARACTERIZATION: a not-reset outcome is only logged; the scenario still succeeds.
        with patch.object(self.plugin, "skew_time", return_value=("pod", [["p1", "ns1", "c1"]])):
            with patch.object(self.plugin, "check_date_time", return_value=["p1"]):
                with self.assertLogs(level="INFO") as logs:
                    result = self._run_with_config(
                        {"time_scenarios": [{"action": "skew_time", "object_type": "pod"}]}
                    )
        self.assertEqual(result, 0)
        self.assertTrue(
            any("Object times were not reset" in message for message in logs.output)
        )

    def test_run_missing_time_scenarios_key_returns_one(self):
        """run() swallows the KeyError for a config without the
        time_scenarios key and returns 1."""
        self.assertEqual(self._run_with_config({}), 1)

    def test_run_empty_yaml_file_returns_one(self):
        """run() swallows the TypeError from an empty yaml file (None
        config) and returns 1."""
        lib_telemetry = MagicMock()
        with patch("builtins.open", create=True):
            with patch("yaml.safe_load", return_value=None):
                result = self.plugin.run(
                    "test-uuid", "empty.yaml", lib_telemetry, MagicMock()
                )
        self.assertEqual(result, 1)

    def test_run_empty_scenarios_list_returns_zero_without_kube_calls(self):
        """run() skips the loop entirely for an empty time_scenarios
        list and returns 0."""
        lib_telemetry = MagicMock()
        self.assertEqual(
            self._run_with_config({"time_scenarios": []}, lib_telemetry), 0
        )
        lib_telemetry.get_lib_kubernetes.assert_not_called()

    def test_run_multiple_scenarios_processed_in_order(self):
        """run() processes each time_scenarios entry exactly once."""
        scenarios = [
            {"action": "skew_time", "object_type": "pod"},
            {"action": "skew_date", "object_type": "node"},
        ]
        lib_telemetry = MagicMock()
        lib_telemetry.get_lib_kubernetes.return_value = self._kube()
        with patch.object(self.plugin, "skew_time", return_value=("node", [])) as mock_skew:
            with patch.object(self.plugin, "check_date_time", return_value=[]):
                result = self._run_with_config(
                    {"time_scenarios": scenarios}, lib_telemetry
                )
        self.assertEqual(result, 0)
        self.assertEqual(
            [call.args[0] for call in mock_skew.call_args_list], scenarios
        )

    def test_pod_exec_success_first_attempt(self):
        """pod_exec returns the first non-empty response without sleeping."""
        kube = self._kube()
        kube.exec_cmd_in_pod.return_value = "out"
        with patch(f"{self.MOD}.time") as mock_time:
            response = self.plugin.pod_exec("pod", "cmd", "ns", "ctr", kube)
        self.assertEqual(response, "out")
        kube.exec_cmd_in_pod.assert_called_once_with("cmd", "pod", "ns", "ctr")
        mock_time.sleep.assert_not_called()

    def test_pod_exec_retries_on_empty_and_auth_error_responses(self):
        """pod_exec sleeps 2s and retries once on empty output, on
        'unauthorized'/'authorization' output and case-insensitively."""
        bad_responses = [
            "",
            "Unauthorized: token expired",
            "403 authorization needed",
            "UNAUTHORIZED",
        ]
        for bad in bad_responses:
            with self.subTest(bad=bad):
                kube = self._kube()
                kube.exec_cmd_in_pod.side_effect = [bad, "ok"]
                with patch(f"{self.MOD}.time") as mock_time:
                    response = self.plugin.pod_exec(
                        "pod", "cmd", "ns", "ctr", kube
                    )
                self.assertEqual(response, "ok")
                self.assertEqual(kube.exec_cmd_in_pod.call_count, 2)
                mock_time.sleep.assert_called_once_with(2)

    def test_pod_exec_returns_last_empty_response_after_five_attempts(self):
        """pod_exec gives up after 5 attempts and returns the last (empty)
        response rather than False or None."""
        kube = self._kube()
        kube.exec_cmd_in_pod.side_effect = [""] * 5
        with patch(f"{self.MOD}.time") as mock_time:
            response = self.plugin.pod_exec("pod", "cmd", "ns", "ctr", kube)
        self.assertEqual(response, "")
        self.assertEqual(kube.exec_cmd_in_pod.call_count, 5)
        # every failed attempt sleeps, including the last one
        self.assertEqual(mock_time.sleep.call_count, 5)
        mock_time.sleep.assert_called_with(2)

    def test_pod_exec_returns_unauthorized_string_after_five_attempts(self):
        """After five persistent unauthorized responses pod_exec must give
        up with False, not return the unauthorized string as a result."""
        kube = self._kube()
        kube.exec_cmd_in_pod.side_effect = ["unauthorized"] * 5
        with patch(f"{self.MOD}.time"):
            response = self.plugin.pod_exec("pod", "cmd", "ns", "ctr", kube)
        self.assertEqual(kube.exec_cmd_in_pod.call_count, 5)
        self.assertIs(response, False)

    def test_pod_exec_retries_legit_output_containing_authorization(self):
        """Legitimate output merely containing 'authorization' is treated
        as an auth error, retried 5 times and reported as failure (False)."""
        kube = self._kube()
        output = "user authorization list: ok"
        kube.exec_cmd_in_pod.side_effect = [output] * 5
        with patch(f"{self.MOD}.time"):
            response = self.plugin.pod_exec("pod", "cmd", "ns", "ctr", kube)
        self.assertIs(response, False)
        self.assertEqual(kube.exec_cmd_in_pod.call_count, 5)

    def test_pod_exec_propagates_client_exception(self):
        """pod_exec has no error handling of its own: client exceptions
        propagate to the caller."""
        kube = self._kube()
        kube.exec_cmd_in_pod.side_effect = Exception("connection refused")
        with patch(f"{self.MOD}.time"):
            with self.assertRaisesRegex(Exception, "connection refused"):
                self.plugin.pod_exec("pod", "cmd", "ns", "ctr", kube)

    def test_exec_with_shell_fallback_direct_empty_uses_shell(self):
        """Empty direct output falls back to /bin/sh -c and its response
        is returned."""
        kube = self._kube()
        kube.exec_cmd_in_pod.side_effect = ["", "out"]
        result = self.plugin.exec_with_shell_fallback(
            "date", "pod", "ns", "ctr", kube
        )
        self.assertEqual(result, "out")
        self.assertEqual(kube.exec_cmd_in_pod.call_count, 2)
        self.assertEqual(
            kube.exec_cmd_in_pod.call_args_list[1],
            unittest.mock.call(["/bin/sh", "-c", "date"], "pod", "ns", "ctr"),
        )

    def test_exec_with_shell_fallback_direct_unauthorized_uses_shell(self):
        """Unauthorized direct output falls back to the shell command."""
        kube = self._kube()
        kube.exec_cmd_in_pod.side_effect = ["unauthorized", "ok"]
        result = self.plugin.exec_with_shell_fallback(
            "date", "pod", "ns", "ctr", kube
        )
        self.assertEqual(result, "ok")
        self.assertEqual(kube.exec_cmd_in_pod.call_count, 2)

    def test_exec_with_shell_fallback_shell_unauthorized_returns_false(self):
        """Persistent auth errors on both direct and shell execution
        return the literal False sentinel."""
        kube = self._kube()
        kube.exec_cmd_in_pod.side_effect = [
            "unauthorized",
            "403 authorization failed",
        ] * 2
        with patch(f"{self.MOD}.time"):
            result = self.plugin.exec_with_shell_fallback(
                "date", "pod", "ns", "ctr", kube, max_retries=2
            )
        self.assertIs(result, False)
        self.assertEqual(kube.exec_cmd_in_pod.call_count, 4)

    def test_exec_with_shell_fallback_list_command_appended_to_shell(self):
        """A list command is extended element-wise onto /bin/sh -c."""
        kube = self._kube()
        kube.exec_cmd_in_pod.side_effect = [Exception("direct failed"), "ok"]
        command = ["date", "--date", "01:01:01"]
        result = self.plugin.exec_with_shell_fallback(
            command, "pod", "ns", "ctr", kube
        )
        self.assertEqual(result, "ok")
        self.assertEqual(
            kube.exec_cmd_in_pod.call_args_list[1],
            unittest.mock.call(
                ["/bin/sh", "-c", "date", "--date", "01:01:01"],
                "pod",
                "ns",
                "ctr",
            ),
        )

    def test_exec_with_shell_fallback_zero_retries_returns_false(self):
        """max_retries=0 never executes anything and returns False."""
        kube = self._kube()
        result = self.plugin.exec_with_shell_fallback(
            "date", "pod", "ns", "ctr", kube, max_retries=0
        )
        self.assertIs(result, False)
        kube.exec_cmd_in_pod.assert_not_called()

    def test_exec_with_shell_fallback_direct_success_skips_shell(self):
        """A good direct response is returned without the shell attempt."""
        kube = self._kube()
        kube.exec_cmd_in_pod.return_value = "ok"
        result = self.plugin.exec_with_shell_fallback(
            "date", "pod", "ns", "ctr", kube
        )
        self.assertEqual(result, "ok")
        kube.exec_cmd_in_pod.assert_called_once_with("date", "pod", "ns", "ctr")

    def test_exec_with_shell_fallback_sleep_cadence_between_attempts(self):
        """Only the non-final attempts sleep 1 second."""
        kube = self._kube()
        kube.exec_cmd_in_pod.side_effect = Exception("down")
        with patch(f"{self.MOD}.time") as mock_time:
            result = self.plugin.exec_with_shell_fallback(
                "date", "pod", "ns", "ctr", kube, max_retries=3
            )
        self.assertIs(result, False)
        self.assertEqual(mock_time.sleep.call_count, 2)
        mock_time.sleep.assert_called_with(1)

    def test_exec_with_shell_fallback_direct_exception_swallowed(self):
        """A direct-execution exception is swallowed and the shell
        fallback result is returned."""
        kube = self._kube()
        kube.exec_cmd_in_pod.side_effect = [Exception("direct blew up"), "ok"]
        result = self.plugin.exec_with_shell_fallback(
            "date", "pod", "ns", "ctr", kube, max_retries=1
        )
        self.assertEqual(result, "ok")

    def _skew_node_with_names(self, node_response, action="skew_time", kube=None):
        """Run skew_node with deterministic generated pod names
        (status pod 'stat1', skew pod 'skew1')."""
        if kube is None:
            kube = self._kube()
        if isinstance(node_response, Exception):
            kube.exec_command_on_node.side_effect = node_response
        else:
            kube.exec_command_on_node.return_value = node_response
        with patch(f"{self.MOD}.get_random_string", side_effect=["stat1", "skew1"]):
            with patch(f"{self.MOD}.time"):
                self.plugin.skew_node("node-1", action, kube)
        return kube

    def _assert_command_reaches_node(self, kube, command):
        """Assert that command was run on node-1 via exec_command_on_node."""
        node_commands = [
            call.args[1]
            for call in kube.exec_command_on_node.call_args_list
            if call.args[0] == "node-1"
        ]
        self.assertIn(command, node_commands)

    def test_skew_node_ntp_active_skips_skew_and_deletes_status_pod(self):
        """With NTP active on the node no skew command runs, only the
        status pod is deleted, and the node status is queried through
        the status pod in the default namespace."""
        kube = self._skew_node_with_names("       Network time on: yes\n")
        kube.exec_cmd_in_pod.assert_not_called()
        kube.exec_command_on_node.assert_called_once_with(
            "node-1", ["timedatectl"], "time-skew-pod-stat1", "default"
        )
        self.assertEqual(
            kube.delete_pod.call_args_list,
            [unittest.mock.call("time-skew-pod-stat1", "default")],
        )

    def test_skew_node_ntp_inactive_skews_time_on_skew_pod(self):
        """With NTP off the skew command must run on the node itself."""
        kube = self._kube()
        kube.exec_cmd_in_pod.return_value = "ok"
        self._skew_node_with_names("Network time on: no", kube=kube)
        self._assert_command_reaches_node(
            kube, ["timedatectl", "set-time", "01:01:01"]
        )

    def test_skew_node_ntp_inactive_deletes_status_and_skew_pods(self):
        """With NTP off both the status pod and the skew pod are deleted
        in the finally block."""
        kube = self._kube()
        kube.exec_cmd_in_pod.return_value = "ok"
        self._skew_node_with_names("Network time on: no", kube=kube)
        self.assertEqual(
            kube.delete_pod.call_args_list,
            [
                unittest.mock.call("time-skew-pod-stat1", "default"),
                unittest.mock.call("time-skew-pod-skew1", "default"),
            ],
        )

    def test_skew_node_ntp_inactive_skew_date_command(self):
        """action=skew_date must set the full date on the node."""
        kube = self._kube()
        kube.exec_cmd_in_pod.return_value = "ok"
        self._skew_node_with_names("Network time on: no", action="skew_date", kube=kube)
        self._assert_command_reaches_node(
            kube, ["timedatectl", "set-time", "2001-01-01"]
        )

    def test_skew_node_none_status_response_logs_error(self):
        """A None node status response triggers a TypeError that is
        caught by the generic handler; the status pod is still deleted."""
        with self.assertLogs(level="ERROR") as logs:
            kube = self._skew_node_with_names(None)
        kube.exec_cmd_in_pod.assert_not_called()
        self.assertTrue(
            any("failed to execute skew command in pod" in m for m in logs.output)
        )
        self.assertEqual(
            kube.delete_pod.call_args_list,
            [unittest.mock.call("time-skew-pod-stat1", "default")],
        )

    def test_skew_node_api_exception_silently_swallowed(self):
        """A kubernetes ApiException is silently ignored: no error log,
        no skew command, and only the status pod is deleted."""
        # CHARACTERIZATION: ApiException is swallowed on purpose; no error is logged.
        from kubernetes.client import ApiException

        with self.assertNoLogs(level="ERROR"):
            kube = self._skew_node_with_names(ApiException())
        kube.exec_cmd_in_pod.assert_not_called()
        self.assertEqual(
            kube.delete_pod.call_args_list,
            [unittest.mock.call("time-skew-pod-stat1", "default")],
        )

    def test_skew_node_generic_exception_logged_and_pods_cleaned(self):
        """A generic exception from the node exec is logged and the
        status pod is still deleted in the finally block."""
        with self.assertLogs(level="ERROR") as logs:
            kube = self._skew_node_with_names(RuntimeError("node exec exploded"))
        self.assertTrue(
            any("failed to execute skew command in pod" in m for m in logs.output)
        )
        self.assertEqual(
            kube.delete_pod.call_args_list,
            [unittest.mock.call("time-skew-pod-stat1", "default")],
        )

    def test_skew_node_delete_pod_failure_propagates(self):
        """An exception raised while deleting the status pod in the
        finally block propagates to the caller."""
        # CHARACTERIZATION: a failing status-pod delete escapes skew_node unhandled.
        kube = self._kube()
        kube.exec_command_on_node.return_value = "Network time on: yes"
        kube.delete_pod.side_effect = RuntimeError("delete failed")
        with patch(f"{self.MOD}.get_random_string", side_effect=["stat1", "skew1"]):
            with self.assertRaisesRegex(RuntimeError, "delete failed"):
                self.plugin.skew_node("node-1", "skew_time", kube)

    def test_skew_time_invalid_action_raises_runtime_error(self):
        """An unknown action is rejected with the action name in the
        error message."""
        with self.assertRaises(RuntimeError) as ctx:
            self.plugin.skew_time(
                {"action": "skew_datetime", "object_type": "node"}, self._kube()
            )
        self.assertIn("skew_datetime", str(ctx.exception))
        self.assertIn("not a valid time skew action", str(ctx.exception))

    def test_skew_time_missing_action_key_raises_key_error(self):
        """A scenario without the action key raises KeyError (swallowed
        upstream by run())."""
        with self.assertRaises(KeyError):
            self.plugin.skew_time({"object_type": "node"}, self._kube())

    def test_skew_time_node_object_name_skews_each_node(self):
        """Explicit node names are skewed directly and returned as the
        target list."""
        kube = self._kube()
        with patch.object(self.plugin, "skew_node") as mock_skew_node:
            object_type, names = self.plugin.skew_time(
                {
                    "action": "skew_time",
                    "object_type": "node",
                    "object_name": ["n1"],
                },
                kube,
            )
        self.assertEqual((object_type, names), ("node", ["n1"]))
        mock_skew_node.assert_called_once_with("n1", "skew_time", kube)

    def test_skew_time_node_label_selector_lists_nodes(self):
        """A label selector resolves node names through list_nodes."""
        kube = self._kube()
        kube.list_nodes.return_value = ["n1", "n2"]
        with patch.object(self.plugin, "skew_node"):
            object_type, names = self.plugin.skew_time(
                {
                    "action": "skew_date",
                    "object_type": "node",
                    "label_selector": "sel",
                },
                kube,
            )
        kube.list_nodes.assert_called_once_with("sel")
        self.assertEqual((object_type, names), ("node", ["n1", "n2"]))

    def test_skew_time_node_label_selector_with_exclude_label(self):
        """Nodes matching the exclude_label selector are filtered out
        and only the remaining nodes are skewed."""
        kube = self._kube()
        kube.list_nodes.side_effect = [["n1", "n2"], ["n1"]]
        with patch.object(self.plugin, "skew_node") as mock_skew_node:
            _, names = self.plugin.skew_time(
                {
                    "action": "skew_time",
                    "object_type": "node",
                    "label_selector": "sel",
                    "exclude_label": "excl",
                },
                kube,
            )
        self.assertEqual(
            kube.list_nodes.call_args_list,
            [unittest.mock.call("sel"), unittest.mock.call("excl")],
        )
        self.assertEqual(names, ["n2"])
        mock_skew_node.assert_called_once_with("n2", "skew_time", kube)

    def test_skew_time_node_no_targets_returns_empty_list(self):
        """A node scenario with no selectors returns an empty target
        list without raising (asymmetric with the pod branch)."""
        # CHARACTERIZATION: an empty node selection is a no-op, not an error.
        with patch.object(self.plugin, "skew_node") as mock_skew_node:
            object_type, names = self.plugin.skew_time(
                {"action": "skew_time", "object_type": "node"}, self._kube()
            )
        self.assertEqual((object_type, names), ("node", []))
        mock_skew_node.assert_not_called()

    def test_skew_time_node_empty_object_name_falls_through(self):
        """An empty object_name list is falsy, so nothing is selected."""
        with patch.object(self.plugin, "skew_node") as mock_skew_node:
            _, names = self.plugin.skew_time(
                {"action": "skew_time", "object_type": "node", "object_name": []},
                self._kube(),
            )
        self.assertEqual(names, [])
        mock_skew_node.assert_not_called()

    def test_skew_time_object_type_substring_match(self):
        """object_type is matched by substring: 'nodes' selects the
        node branch."""
        with patch.object(self.plugin, "skew_node"):
            object_type, _ = self.plugin.skew_time(
                {
                    "action": "skew_time",
                    "object_type": "nodes",
                    "object_name": ["n1"],
                },
                self._kube(),
            )
        self.assertEqual(object_type, "node")

    def test_skew_time_pod_object_name_without_namespace_raises(self):
        """Using pod object names without a namespace raises a bare
        RuntimeError (with a 'Need to set namespace' error log)."""
        with self.assertLogs(level="ERROR") as logs:
            with self.assertRaises(RuntimeError):
                self.plugin.skew_time(
                    {
                        "action": "skew_time",
                        "object_type": "pod",
                        "object_name": ["p1"],
                    },
                    self._kube(),
                )
        self.assertTrue(any("Need to set namespace" in m for m in logs.output))

    def test_skew_time_pod_object_name_with_namespace(self):
        """Named pods are skewed in their namespace via pod_exec with
        the selected container appended to each target."""
        kube = self._kube()
        kube.get_containers_in_pod.return_value = ["c1"]
        kube.exec_cmd_in_pod.return_value = "ok"
        result = self.plugin.skew_time(
            {
                "action": "skew_time",
                "object_type": "pod",
                "object_name": ["p1"],
                "namespace": "ns1",
            },
            kube,
        )
        self.assertEqual(result, ("pod", [["p1", "ns1", "c1"]]))
        kube.exec_cmd_in_pod.assert_called_once_with(
            "date --date 01:01:01", "p1", "ns1", "c1"
        )

    def test_skew_time_pod_skew_date_command_variant(self):
        """action=skew_date on pods uses the date syntax."""
        kube = self._kube()
        kube.get_containers_in_pod.return_value = ["c1"]
        kube.exec_cmd_in_pod.return_value = "ok"
        result = self.plugin.skew_time(
            {
                "action": "skew_date",
                "object_type": "pod",
                "object_name": ["p1"],
                "namespace": "ns1",
            },
            kube,
        )
        self.assertEqual(result, ("pod", [["p1", "ns1", "c1"]]))
        kube.exec_cmd_in_pod.assert_called_once_with(
            "date --date 00-01-01", "p1", "ns1", "c1"
        )

    def test_skew_time_pod_exec_false_response_raises_runtime_error(self):
        """A literal False pod_exec response is the only failure the
        caller checks and raises RuntimeError."""
        kube = self._kube()
        kube.get_containers_in_pod.return_value = ["c1"]
        with patch.object(self.plugin, "pod_exec", return_value=False) as mock_exec:
            with self.assertRaises(RuntimeError):
                self.plugin.skew_time(
                    {
                        "action": "skew_time",
                        "object_type": "pod",
                        "object_name": ["p1"],
                        "namespace": "ns1",
                    },
                    kube,
                )
        mock_exec.assert_called_once_with(
            "p1", "date --date 01:01:01", "ns1", "c1", kube
        )

    def test_skew_time_pod_exec_none_response_should_raise(self):
        """A None pod_exec response means the skew command produced no
        output and must fail the scenario (correct behavior; currently
        only `is False` is checked)."""
        kube = self._kube()
        kube.get_containers_in_pod.return_value = ["c1"]
        with patch.object(self.plugin, "pod_exec", return_value=None):
            with self.assertRaises(RuntimeError):
                self.plugin.skew_time(
                    {
                        "action": "skew_time",
                        "object_type": "pod",
                        "object_name": ["p1"],
                        "namespace": "ns1",
                    },
                    kube,
                )

    def test_skew_time_pod_object_name_string_iterates_characters(self):
        """A plain string object_name should be rejected or treated as a
        single pod name, never iterated character by character."""
        kube = self._kube()
        kube.get_containers_in_pod.return_value = ["c1"]
        kube.exec_cmd_in_pod.return_value = "ok"
        result = None
        rejected = False
        try:
            result = self.plugin.skew_time(
                {
                    "action": "skew_time",
                    "object_type": "pod",
                    "object_name": "web",
                    "namespace": "ns1",
                },
                kube,
            )
        except (RuntimeError, TypeError, ValueError):
            rejected = True
        self.assertTrue(rejected or result == ("pod", [["web", "ns1", "c1"]]))

    def test_skew_time_pod_namespace_lists_all_pods(self):
        """A namespace without label_selector lists every pod in it and
        converts the names to [pod, namespace] pairs in order."""
        kube = self._kube()
        kube.list_pods.return_value = ["p1", "p2"]
        kube.get_containers_in_pod.return_value = ["c1"]
        kube.exec_cmd_in_pod.return_value = "ok"
        result = self.plugin.skew_time(
            {
                "action": "skew_time",
                "object_type": "pod",
                "namespace": "ns1",
            },
            kube,
        )
        kube.list_pods.assert_called_once_with("ns1")
        self.assertEqual(
            result,
            ("pod", [["p1", "ns1", "c1"], ["p2", "ns1", "c1"]]),
        )

    def test_skew_time_pod_namespace_with_label_selector(self):
        """Namespace plus label_selector lists pods with both
        arguments."""
        kube = self._kube()
        kube.list_pods.return_value = ["p1"]
        kube.get_containers_in_pod.return_value = ["c1"]
        kube.exec_cmd_in_pod.return_value = "ok"
        result = self.plugin.skew_time(
            {
                "action": "skew_time",
                "object_type": "pod",
                "namespace": "ns1",
                "label_selector": "sel",
            },
            kube,
        )
        kube.list_pods.assert_called_once_with("ns1", "sel")
        self.assertEqual(result, ("pod", [["p1", "ns1", "c1"]]))

    def test_skew_time_pod_label_selector_only_uses_pairs(self):
        """A label_selector without namespace uses get_all_pods pairs
        [pod, namespace] directly."""
        kube = self._kube()
        kube.get_all_pods.return_value = [["p1", "ns1"]]
        kube.get_containers_in_pod.return_value = ["c1"]
        kube.exec_cmd_in_pod.return_value = "ok"
        result = self.plugin.skew_time(
            {
                "action": "skew_time",
                "object_type": "pod",
                "label_selector": "sel",
            },
            kube,
        )
        kube.get_all_pods.assert_called_once_with("sel")
        self.assertEqual(result, ("pod", [["p1", "ns1", "c1"]]))

    def test_skew_time_pod_label_selector_with_exclude_label(self):
        """Pods matching the exclude_label selector are filtered out of
        the get_all_pods result."""
        kube = self._kube()
        kube.get_all_pods.side_effect = [
            [["p1", "ns1"], ["p2", "ns2"]],
            [["p2", "ns2"]],
        ]
        kube.get_containers_in_pod.return_value = ["c1"]
        kube.exec_cmd_in_pod.return_value = "ok"
        result = self.plugin.skew_time(
            {
                "action": "skew_time",
                "object_type": "pod",
                "label_selector": "sel",
                "exclude_label": "excl",
            },
            kube,
        )
        self.assertEqual(
            kube.get_all_pods.call_args_list,
            [unittest.mock.call("sel"), unittest.mock.call("excl")],
        )
        self.assertEqual(result, ("pod", [["p1", "ns1", "c1"]]))

    def test_skew_time_pod_no_targets_raises_runtime_error(self):
        """An empty pod target list raises RuntimeError after an info
        log asking to check the namespace/label_selector."""
        kube = self._kube()
        kube.list_pods.return_value = []
        with self.assertLogs(level="INFO") as logs:
            with self.assertRaises(RuntimeError):
                self.plugin.skew_time(
                    {
                        "action": "skew_time",
                        "object_type": "pod",
                        "namespace": "ns1",
                    },
                    kube,
                )
        self.assertTrue(
            any("Cannot find pods matching" in m for m in logs.output)
        )

    def test_skew_time_unknown_object_type_returns_none(self):
        """An object_type matching neither node nor pod returns None
        (run() then fails on the tuple unpack and returns 1)."""
        result = self.plugin.skew_time(
            {"action": "skew_time", "object_type": "cluster"}, self._kube()
        )
        self.assertIsNone(result)

    def test_skew_time_pod_single_element_pair_raises_key_error(self):
        """A [pod] pair without namespace takes the single-element
        branch, which dereferences the absent 'namespace' key and
        raises KeyError (characterization of the dead branch)."""
        kube = self._kube()
        kube.get_all_pods.return_value = [["p1"]]
        with self.assertRaises(KeyError):
            self.plugin.skew_time(
                {
                    "action": "skew_time",
                    "object_type": "pod",
                    "label_selector": "sel",
                },
                kube,
            )

    def test_parse_string_date_matching_strings_returned_verbatim(self):
        """Canonical and near-canonical date strings are returned
        unchanged."""
        for date_string in [
            "Fri Nov 20 12:34:56 UTC 2026",
            "fri nov 20 12:34:56 utc 2026",
            "Mon Jan 5 01:01:01 UTC 2001",
        ]:
            with self.subTest(date_string=date_string):
                self.assertEqual(
                    self.plugin.parse_string_date(date_string), date_string
                )

    def test_parse_string_date_whitespace_normalization(self):
        """Runs of 2+ whitespace characters collapse to one space and
        leading/trailing whitespace is stripped."""
        self.assertEqual(
            self.plugin.parse_string_date("Fri  Nov   20 12:34:56 UTC 2026"),
            "Fri Nov 20 12:34:56 UTC 2026",
        )
        self.assertEqual(
            self.plugin.parse_string_date(
                "\n Fri Nov 20 12:34:56 UTC 2026 \n"
            ),
            "Fri Nov 20 12:34:56 UTC 2026",
        )

    def test_parse_string_date_should_extract_only_the_date_line(self):
        """parse_string_date must extract the date from surrounding
        command output (docstring: 'From kubectl/oc command get time
        output'); the current regex returns the whole string."""
        self.assertEqual(
            self.plugin.parse_string_date(
                "prefix noise\nFri Nov 20 12:34:56 UTC 2026\ntrailer"
            ),
            "Fri Nov 20 12:34:56 UTC 2026",
        )
        self.assertEqual(
            self.plugin.parse_string_date("noise\nFri Nov 20 12:34:56 UTC 2026"),
            "Fri Nov 20 12:34:56 UTC 2026",
        )

    def test_parse_string_date_non_matching_and_bad_inputs_return_empty(self):
        """Non-matching strings and non-string inputs return ''."""
        for bad in [
            "Fri Nov 20 12:34:56 UTC 26",
            "Fri Nov 20 12:34:56 2026",
            "",
            None,
            12345,
            b"Fri Nov 20 12:34:56 UTC 2026",
        ]:
            with self.subTest(bad=bad):
                self.assertEqual(self.plugin.parse_string_date(bad), "")

    def test_string_to_date_valid_utc_string(self):
        """A canonical UTC string parses to a naive datetime."""
        result = self.plugin.string_to_date("Fri Nov 20 12:34:56 UTC 2026")
        self.assertEqual(result, datetime.datetime(2026, 11, 20, 12, 34, 56))
        self.assertIsNone(result.tzinfo)

    def test_string_to_date_trailing_newline_still_parses(self):
        """A trailing newline is stripped by parse_string_date first."""
        result = self.plugin.string_to_date("Fri Nov 20 12:34:56 UTC 2026\n")
        self.assertEqual(result, datetime.datetime(2026, 11, 20, 12, 34, 56))

    def test_string_to_date_invalid_inputs_return_minyear_sentinel(self):
        """Unparsable input returns the datetime(1, 1, 1) sentinel
        instead of raising."""
        sentinel = datetime.datetime(datetime.MINYEAR, 1, 1)
        for bad in ["garbage", "", None]:
            with self.subTest(bad=bad):
                self.assertEqual(self.plugin.string_to_date(bad), sentinel)

    # Skipped on hosts whose local zone name is IST.
    @unittest.skipIf("IST" in time.tzname, "host zone name is IST")
    def test_string_to_date_ist_abbreviation_parses(self):
        """A non-UTC abbreviation should parse to a real datetime rather
        than the MINYEAR sentinel."""
        result = self.plugin.string_to_date("Fri Nov 20 12:34:56 IST 2026")
        self.assertNotEqual(result, datetime.datetime(datetime.MINYEAR, 1, 1))

    def test_string_to_date_unknown_abbreviation_returns_minyear(self):
        """Any three-letter zone abbreviation is ignored and the date parses."""
        result = self.plugin.string_to_date("Fri Nov 20 12:34:56 XYZ 2026")
        self.assertEqual(result, datetime.datetime(2026, 11, 20, 12, 34, 56))

    @staticmethod
    def _fake_datetime_module():
        """A stand-in for the datetime module whose datetime.now()
        advances one hour per call, making the reset window
        (first_date_time < reported time < now) deterministic: the 1st
        now() is 2026-11-20 11:00, the 2nd is 12:00, so a pod/node
        reading of 11:30 falls inside the window."""
        # Fake clock assumption: first_date_time is the first now() call of each
        # check; every loop condition evaluation consumes one more call.
        base = datetime.datetime(2026, 11, 20, 10, 0, 0)
        calls = {"count": 0}

        class FakeDateTime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                calls["count"] += 1
                value = base + datetime.timedelta(hours=calls["count"])
                if tz is not None:
                    return value.replace(tzinfo=tz)
                return value

        real_datetime = datetime

        class FakeDatetimeModule:
            datetime = FakeDateTime
            timezone = real_datetime.timezone
            MINYEAR = real_datetime.MINYEAR

        return FakeDatetimeModule()

    def test_check_date_time_node_reset_within_window(self):
        """A node reporting the current time is considered reset: the
        check pod is deleted and nothing is reported."""
        kube = self._kube()
        kube.exec_command_on_node.return_value = "Fri Nov 20 11:30:00 UTC 2026"
        kube.exec_cmd_in_pod.return_value = "Fri Nov 20 11:30:00 UTC 2026"
        with patch(f"{self.MOD}.get_random_string", return_value="chk1"):
            with patch(f"{self.MOD}.time"):
                with patch(f"{self.MOD}.datetime", self._fake_datetime_module()):
                    not_reset = self.plugin.check_date_time("node", ["n1"], kube)
        self.assertEqual(not_reset, [])
        kube.exec_command_on_node.assert_called_once_with(
            "n1", ["date"], "time-skew-pod-chk1"
        )
        kube.delete_pod.assert_called_once_with("time-skew-pod-chk1")

    def test_check_date_time_node_never_reset_after_max_retries(self):
        """A node stuck on an old time is retried 31 times (10s sleep
        each) before being reported as not reset; the check pod is
        still deleted."""
        kube = self._kube()
        old = "Fri Nov 20 12:34:56 UTC 2020"
        kube.exec_command_on_node.return_value = old
        kube.exec_cmd_in_pod.return_value = old
        with patch(f"{self.MOD}.get_random_string", return_value="chk1"):
            with patch(f"{self.MOD}.time") as mock_time:
                with self.assertLogs(level="ERROR") as logs:
                    not_reset = self.plugin.check_date_time("node", ["n1"], kube)
        self.assertEqual(not_reset, ["n1"])
        self.assertEqual(kube.exec_cmd_in_pod.call_count, 31)
        mock_time.sleep.assert_called_with(10)
        self.assertTrue(
            any("didn't reset properly" in m for m in logs.output)
        )
        kube.delete_pod.assert_called_once_with("time-skew-pod-chk1")

    def test_check_date_time_node_resets_on_retry(self):
        """A node whose first reading is stale but whose retry reading
        is current is not reported."""
        kube = self._kube()
        kube.exec_command_on_node.return_value = "Fri Nov 20 12:34:56 UTC 2020"
        kube.exec_cmd_in_pod.return_value = "Fri Nov 20 11:30:00 UTC 2026"
        with patch(f"{self.MOD}.get_random_string", return_value="chk1"):
            with patch(f"{self.MOD}.time") as mock_time:
                with patch(f"{self.MOD}.datetime", self._fake_datetime_module()):
                    not_reset = self.plugin.check_date_time("node", ["n1"], kube)
        self.assertEqual(not_reset, [])
        kube.exec_cmd_in_pod.assert_called_once_with(
            ["date"], "time-skew-pod-chk1", "default"
        )
        mock_time.sleep.assert_called_once_with(10)
        kube.delete_pod.assert_called_once_with("time-skew-pod-chk1")

    def test_check_date_time_node_reading_later_than_now_never_reset(self):
        """A node reporting a time later than every fake-clock reading is
        never inside the reset window: it is retried 31 times and reported
        as not reset."""
        kube = self._kube()
        kube.exec_command_on_node.return_value = "Fri Nov 20 12:34:56 UTC 2099"
        kube.exec_cmd_in_pod.return_value = "Fri Nov 20 12:34:56 UTC 2099"
        with patch(f"{self.MOD}.get_random_string", return_value="chk1"):
            with patch(f"{self.MOD}.time") as mock_time:
                with patch(f"{self.MOD}.datetime", self._fake_datetime_module()):
                    with self.assertLogs(level="ERROR") as logs:
                        not_reset = self.plugin.check_date_time("node", ["n1"], kube)
        self.assertEqual(not_reset, ["n1"])
        # 31 retries after the initial reading taken via exec_command_on_node
        self.assertEqual(kube.exec_cmd_in_pod.call_count, 31)
        mock_time.sleep.assert_called_with(10)
        self.assertTrue(
            any("didn't reset properly" in m for m in logs.output)
        )
        kube.delete_pod.assert_called_once_with("time-skew-pod-chk1")

    def test_check_date_time_pod_reset_within_window(self):
        """A pod reporting the current time is considered reset and
        not reported."""
        kube = self._kube()
        kube.exec_cmd_in_pod.return_value = "Fri Nov 20 11:30:00 UTC 2026"
        with patch(f"{self.MOD}.time"):
            with patch(f"{self.MOD}.datetime", self._fake_datetime_module()):
                not_reset = self.plugin.check_date_time(
                    "pod", [["p1", "ns1", "c1"]], kube
                )
        self.assertEqual(not_reset, [])
        kube.exec_cmd_in_pod.assert_called_once_with("date", "p1", "ns1", "c1")

    def test_check_date_time_pod_never_reset_after_max_retries(self):
        """A pod whose 'date' output is unparsable falls back to the
        MINYEAR sentinel (suspected bug B4), never enters the window
        and is reported as not reset after 31 retries."""
        # CHARACTERIZATION: unparsable 'date' output is treated as never reset
        # (MINYEAR sentinel, see suspected bug B4).
        kube = self._kube()
        kube.exec_cmd_in_pod.return_value = "garbage"
        with patch(f"{self.MOD}.time") as mock_time:
            with self.assertLogs(level="ERROR") as logs:
                not_reset = self.plugin.check_date_time(
                    "pod", [["p1", "ns1", "c1"]], kube
                )
        self.assertEqual(not_reset, ["p1"])
        # one initial reading plus 31 retries
        self.assertEqual(kube.exec_cmd_in_pod.call_count, 32)
        mock_time.sleep.assert_called_with(10)
        self.assertTrue(
            any("didn't reset properly" in m for m in logs.output)
        )

    def test_check_date_time_pod_reading_later_than_now_never_reset(self):
        """A pod reporting a time later than every fake-clock reading is
        never inside the reset window: it is retried 31 times and reported
        as not reset."""
        kube = self._kube()
        kube.exec_cmd_in_pod.return_value = "Fri Nov 20 12:34:56 UTC 2030"
        with patch(f"{self.MOD}.time") as mock_time:
            with patch(f"{self.MOD}.datetime", self._fake_datetime_module()):
                with self.assertLogs(level="ERROR") as logs:
                    not_reset = self.plugin.check_date_time(
                        "pod", [["p1", "ns1", "c1"]], kube
                    )
        self.assertEqual(not_reset, ["p1"])
        # one initial reading plus 31 retries
        self.assertEqual(kube.exec_cmd_in_pod.call_count, 32)
        mock_time.sleep.assert_called_with(10)
        self.assertTrue(
            any("didn't reset properly" in m for m in logs.output)
        )

    def test_check_date_time_pod_resets_on_retry(self):
        """A pod whose first reading is stale but whose retry reading
        is current is not reported."""
        kube = self._kube()
        kube.exec_cmd_in_pod.side_effect = [
            "Fri Nov 20 12:34:56 UTC 2020",
            "Fri Nov 20 11:30:00 UTC 2026",
        ]
        with patch(f"{self.MOD}.time") as mock_time:
            with patch(f"{self.MOD}.datetime", self._fake_datetime_module()):
                not_reset = self.plugin.check_date_time(
                    "pod", [["p1", "ns1", "c1"]], kube
                )
        self.assertEqual(not_reset, [])
        self.assertEqual(kube.exec_cmd_in_pod.call_count, 2)
        mock_time.sleep.assert_called_once_with(10)

    def test_check_date_time_empty_target_lists_do_nothing(self):
        """Empty node or pod target lists short-circuit both loops."""
        kube = self._kube()
        self.assertEqual(self.plugin.check_date_time("node", [], kube), [])
        self.assertEqual(self.plugin.check_date_time("pod", [], kube), [])
        kube.exec_command_on_node.assert_not_called()
        kube.exec_cmd_in_pod.assert_not_called()
        kube.delete_pod.assert_not_called()

    def test_check_date_time_unknown_object_type_returns_empty(self):
        """An unknown object_type matches neither branch and returns an
        empty list without touching the cluster."""
        kube = self._kube()
        self.assertEqual(
            self.plugin.check_date_time("cluster", ["n1"], kube), []
        )
        kube.exec_command_on_node.assert_not_called()
        kube.exec_cmd_in_pod.assert_not_called()

    def test_skew_time_pod_no_selectors_at_all_raises(self):
        """A pod scenario with neither object_name, namespace nor
        label_selector finds no pods and raises RuntimeError."""
        with self.assertLogs(level="INFO") as logs:
            with self.assertRaises(RuntimeError):
                self.plugin.skew_time(
                    {"action": "skew_time", "object_type": "pod"}, self._kube()
                )
        self.assertTrue(
            any("Cannot find pods matching" in m for m in logs.output)
        )

    def test_skew_time_pod_single_element_pair_with_falsy_namespace(self):
        """A falsy namespace plus 1-element [pod] pairs takes the
        single-element loop branch: the pod NAME is the whole [pod]
        list and the namespace is the empty string
        (characterization of the dead-ish branch)."""
        kube = self._kube()
        kube.get_all_pods.return_value = [["p1"]]
        kube.get_containers_in_pod.return_value = ["c1"]
        kube.exec_cmd_in_pod.return_value = "ok"
        result = self.plugin.skew_time(
            {
                "action": "skew_time",
                "object_type": "pod",
                "namespace": "",
                "label_selector": "sel",
            },
            kube,
        )
        # the mock keeps a reference to the pod list, which the loop
        # mutates afterwards by appending the container name
        kube.exec_cmd_in_pod.assert_called_once_with(
            "date --date 01:01:01", ["p1", "c1"], "", "c1"
        )
        self.assertEqual(result, ("pod", [["p1", "c1"]]))

    def test_skew_time_pod_single_element_pair_exec_false_raises(self):
        """A literal False pod_exec response in the single-element
        branch raises RuntimeError."""
        kube = self._kube()
        kube.get_all_pods.return_value = [["p1"]]
        kube.get_containers_in_pod.return_value = ["c1"]
        with patch.object(self.plugin, "pod_exec", return_value=False):
            with self.assertRaises(RuntimeError):
                self.plugin.skew_time(
                    {
                        "action": "skew_time",
                        "object_type": "pod",
                        "namespace": "",
                        "label_selector": "sel",
                    },
                    kube,
                )

    def test_run_skew_time_none_return_returns_one(self):
        """skew_time returning None (unknown object_type) makes run()
        fail on the tuple unpack and return 1."""
        with patch.object(self.plugin, "skew_time", return_value=None):
            with patch.object(self.plugin, "check_date_time") as mock_check:
                result = self._run_with_config(
                    {
                        "time_scenarios": [
                            {"action": "skew_time", "object_type": "cluster"}
                        ]
                    }
                )
        self.assertEqual(result, 1)
        mock_check.assert_not_called()

    def test_get_container_name_explicit_present(self):
        """An explicit container name present in the pod is returned
        without consulting the random module."""
        kube = self._kube()
        kube.get_containers_in_pod.return_value = ["c1", "c2"]
        with patch(f"{self.MOD}.random") as mock_random:
            result = self.plugin.get_container_name("pod", "ns", kube, "c2")
        self.assertEqual(result, "c2")
        mock_random.randint.assert_not_called()
        kube.get_containers_in_pod.assert_called_once_with("pod", "ns")

    def test_get_container_name_explicit_absent_logs_error(self):
        """An explicit container name not in the pod logs an error and
        raises RuntimeError."""
        kube = self._kube()
        kube.get_containers_in_pod.return_value = ["c1"]
        with self.assertLogs(level="ERROR") as logs:
            with self.assertRaises(RuntimeError):
                self.plugin.get_container_name("pod", "ns", kube, "cX")
        self.assertTrue(
            any("not an existing container" in message for message in logs.output)
        )

    def test_get_container_name_empty_explicit_single_container(self):
        """With no explicit name and a single container, that container
        is returned."""
        kube = self._kube()
        kube.get_containers_in_pod.return_value = ["only"]
        self.assertEqual(
            self.plugin.get_container_name("pod", "ns", kube), "only"
        )

    def test_get_container_name_empty_explicit_random_pick(self):
        """With no explicit name a container is picked randomly within
        [0, len(containers) - 1]."""
        kube = self._kube()
        kube.get_containers_in_pod.return_value = ["a", "b"]
        with patch(f"{self.MOD}.random.randint", return_value=1) as mock_randint:
            result = self.plugin.get_container_name("pod", "ns", kube)
        self.assertEqual(result, "b")
        mock_randint.assert_called_once_with(0, 1)

    def test_get_container_name_no_containers_raises(self):
        """A pod reporting no containers makes random.randint(0, -1)
        raise ValueError."""
        kube = self._kube()
        kube.get_containers_in_pod.return_value = []
        with self.assertRaises(ValueError):
            self.plugin.get_container_name("pod", "ns", kube)

    def test_get_container_name_none_container_name(self):
        """container_name=None differs from the '' default, so the explicit
        branch runs; the lookup must not silently return None."""
        kube = self._kube()
        kube.get_containers_in_pod.return_value = ["c1"]
        rejected = False
        try:
            container = self.plugin.get_container_name("pod", "ns", kube, None)
        except RuntimeError:
            rejected = True
        self.assertTrue(rejected or container is not None)

    def _assert_no_exec_with_none_container(self, scenario, kube):
        """Run skew_time for scenario: it may raise RuntimeError, but it
        must never exec a command with a None container name."""
        try:
            self.plugin.skew_time(scenario, kube)
        except RuntimeError:
            pass
        for call in kube.exec_cmd_in_pod.call_args_list:
            self.assertIsNotNone(call.args[3])

    def test_skew_time_explicit_absent_container_never_execs_with_none(self):
        """An explicit container_name absent from the pod must not be exec'd
        as None: the scenario should fail rather than skew another container."""
        kube = self._kube()
        kube.get_containers_in_pod.return_value = ["c1"]
        kube.exec_cmd_in_pod.return_value = "ok"
        self._assert_no_exec_with_none_container(
            {
                "action": "skew_time",
                "object_type": "pod",
                "object_name": ["p1"],
                "namespace": "ns1",
                "container_name": "cX",
            },
            kube,
        )

    def test_skew_time_pod_explicit_container_name_passed_through(self):
        """An explicit container_name present in the pod is used for the
        exec and reported in the target list."""
        kube = self._kube()
        kube.get_containers_in_pod.return_value = ["c1", "c2"]
        kube.exec_cmd_in_pod.return_value = "ok"
        result = self.plugin.skew_time(
            {
                "action": "skew_time",
                "object_type": "pod",
                "object_name": ["p1"],
                "namespace": "ns1",
                "container_name": "c2",
            },
            kube,
        )
        self.assertEqual(result, ("pod", [["p1", "ns1", "c2"]]))
        kube.exec_cmd_in_pod.assert_called_once_with(
            "date --date 01:01:01", "p1", "ns1", "c2"
        )


if __name__ == "__main__":
    unittest.main()
