#!/usr/bin/env python3

"""
Test suite for NetworkChaosScenarioPlugin class

Usage:
    python -m coverage run -a -m unittest tests/test_network_chaos_scenario_plugin.py -v

Assisted By: Claude Code
"""

import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import yaml
from jinja2 import Environment, FileSystemLoader
from krkn_lib.k8s import KrknKubernetes
from krkn_lib.models.telemetry import ScenarioTelemetry
from krkn_lib.telemetry.ocp import KrknTelemetryOpenshift

import krkn.scenario_plugins.network_chaos.network_chaos_scenario_plugin as ncs_plugin
from krkn.scenario_plugins.network_chaos.network_chaos_scenario_plugin import NetworkChaosScenarioPlugin


class TestNetworkChaosScenarioPlugin(unittest.TestCase):

    def setUp(self):
        """
        Set up test fixtures for NetworkChaosScenarioPlugin
        """
        self.plugin = NetworkChaosScenarioPlugin()

    def test_get_scenario_types(self):
        """
        Test get_scenario_types returns correct scenario type
        """
        result = self.plugin.get_scenario_types()

        self.assertEqual(result, ["network_chaos_scenarios"])
        self.assertEqual(len(result), 1)

    def test_get_job_pods_empty_list_raises_exception(self):
        """
        Test get_job_pods raises descriptive error when no pods match the label selector
        """
        mock_kubecli = MagicMock(spec=KrknKubernetes)
        mock_kubecli.list_pods.return_value = []

        mock_api_response = MagicMock()
        mock_api_response.metadata.labels = {"controller-uid": "test-uid-123"}

        with self.assertRaises(Exception) as context:
            self.plugin.get_job_pods(mock_api_response, mock_kubecli)

        self.assertIn("No pods found matching label selector", str(context.exception))
        self.assertIn("controller-uid=test-uid-123", str(context.exception))

    def test_get_job_pods_returns_first_pod(self):
        """
        Test get_job_pods returns the first pod when pods are found
        """
        mock_kubecli = MagicMock(spec=KrknKubernetes)
        mock_kubecli.list_pods.return_value = ["pod-1", "pod-2"]

        mock_api_response = MagicMock()
        mock_api_response.metadata.labels = {"controller-uid": "test-uid-456"}

        result = self.plugin.get_job_pods(mock_api_response, mock_kubecli)

        self.assertEqual(result, "pod-1")
        mock_kubecli.list_pods.assert_called_once_with(
            label_selector="controller-uid=test-uid-456", namespace="default"
        )


# ---------------------------------------------------------------------------
# Tests for run(), verify_interface(), wait_for_job(), delete_job() and
# get_egress_cmd(). Mocks only the krkn-lib Kubernetes boundary.


class NetworkChaosScenarioTestBase(unittest.TestCase):
    """Shared mock harness for network chaos scenario plugin behavior tests."""

    def setUp(self):
        self.plugin = NetworkChaosScenarioPlugin()
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

    def _make_cli(self, nodes=("node-1",)):
        cli = MagicMock(spec=KrknKubernetes)
        cli.list_killable_nodes.return_value = list(nodes)
        cli.create_pod.return_value = object()
        cli.exec_cmd_in_pod.return_value = "lo,eth0,"
        cli.create_job.return_value = SimpleNamespace(
            metadata=SimpleNamespace(name="created-job")
        )
        cli.get_job_status.return_value = SimpleNamespace(
            status=SimpleNamespace(succeeded=1, failed=None)
        )
        cli.list_pods.return_value = ["jobpod-1"]
        cli.read_pod.return_value = SimpleNamespace(
            status=SimpleNamespace(container_statuses=["cs"])
        )
        cli.get_pod_log.return_value = SimpleNamespace(data=b"job log")
        return cli

    def _make_telemetry(self, cli):
        telemetry = MagicMock(spec=KrknTelemetryOpenshift)
        telemetry.get_lib_kubernetes.return_value = cli
        scenario_telemetry = MagicMock(spec=ScenarioTelemetry)
        scenario_telemetry.exit_status = 0
        return telemetry, scenario_telemetry

    def _write_scenario(self, **config):
        scenario = os.path.join(self.tmpdir, "network_chaos.yaml")
        with open(scenario, "w") as scenario_file:
            yaml.safe_dump({"network_chaos": config}, scenario_file)
        return scenario

    def _run_plugin(self, cli, scenario):
        telemetry, scenario_telemetry = self._make_telemetry(cli)
        exit_code = self.plugin.run("run-uuid", scenario, telemetry, scenario_telemetry)
        return exit_code, scenario_telemetry

    def _record_wait_calls(self):
        """wait_for_job mutates its list argument; snapshot args before the call."""
        wait_calls = []
        real_wait = self.plugin.wait_for_job

        def record_wait(joblst, kubecli, timeout=300):
            wait_calls.append((list(joblst), timeout))
            return real_wait(joblst, kubecli, timeout)

        return patch.object(self.plugin, "wait_for_job", new=record_wait), wait_calls

    def _clock_expiring_after_polls(self, cli, polls):
        """Fake time.time that passes every deadline once cli.get_job_status has
        been called `polls` times. Keyed to status polls, not to time.time calls,
        so logging's own time.time() reads cannot consume the fake ticks.
        Success-path tests pass polls=5, so a loop that never completes hits the
        deadline after a few polls instead of spinning until the real timeout."""

        def fake_time():
            return 99999.0 if cli.get_job_status.call_count >= polls else 1000.0

        return patch("time.time", new=fake_time)


class TestNetworkChaosRun(NetworkChaosScenarioTestBase):
    def test_run_serial_latency_single_node_creates_waits_deletes_job(self):
        cli = self._make_cli(nodes=("node-1",))
        scenario = self._write_scenario(
            duration=60, interfaces=["eth0"], egress={"latency": "100ms"}
        )
        wait_patch, wait_calls = self._record_wait_calls()
        with patch("time.sleep"), wait_patch, self._clock_expiring_after_polls(cli, polls=5):
            exit_code, scenario_telemetry = self._run_plugin(cli, scenario)
        self.assertEqual(exit_code, 0)
        self.assertEqual(scenario_telemetry.exit_status, 0)
        self.assertEqual(cli.create_job.call_count, 1)
        job_body = cli.create_job.call_args[0][0]
        self.assertEqual(job_body["spec"]["template"]["spec"]["nodeName"], "node-1")
        cmd = job_body["spec"]["template"]["spec"]["containers"][0]["command"][2]
        self.assertIn("netem delay 100ms", cmd)
        job_name = job_body["metadata"]["name"]
        self.assertTrue(job_name.startswith("chaos-latency"))
        self.assertEqual(len(wait_calls), 1)
        self.assertEqual(wait_calls[0][1], 60 + 300)
        self.assertEqual(wait_calls[0][0], [job_name])
        cli.delete_job.assert_called_once_with(name=job_name, namespace="default")

    def test_run_parallel_two_nodes_creates_jobs_for_first_mod_only(self):
        cli = self._make_cli(nodes=("node-1", "node-2"))
        scenario = self._write_scenario(
            duration=60,
            interfaces=["eth0"],
            execution="parallel",
            instance_count=2,
            egress={"loss": "5", "latency": "1ms"},
        )
        wait_patch, wait_calls = self._record_wait_calls()
        with patch("time.sleep"), wait_patch, self._clock_expiring_after_polls(cli, polls=5):
            exit_code, scenario_telemetry = self._run_plugin(cli, scenario)
        self.assertEqual(exit_code, 0)
        self.assertEqual(cli.create_job.call_count, 2)
        for call in cli.create_job.call_args_list:
            cmd = call[0][0]["spec"]["template"]["spec"]["containers"][0]["command"][2]
            self.assertIn("delay 1ms", cmd)
            self.assertIn("loss 5", cmd)
        self.assertEqual(len(wait_calls), 1)
        self.assertEqual(len(wait_calls[0][0]), 2)
        self.assertEqual(cli.delete_job.call_count, 2)

    def test_run_create_job_returns_none_returns_one_and_cleans_jobs(self):
        cli = self._make_cli(nodes=("node-1", "node-2"))
        cli.create_job.return_value = None
        scenario = self._write_scenario(
            instance_count=2, interfaces=["eth0"], egress={"latency": "100ms"}
        )
        with patch("time.sleep"):
            exit_code, _ = self._run_plugin(cli, scenario)
        self.assertEqual(exit_code, 1)
        self.assertEqual(cli.create_job.call_count, 1)
        # inner-failure path: jobs created so far are still deleted via finally
        self.assertEqual(cli.delete_job.call_count, 1)

    def test_run_create_job_raises_returns_one_and_cleans_first_job(self):
        cli = self._make_cli(nodes=("node-1", "node-2"))
        cli.create_job.side_effect = [
            SimpleNamespace(metadata=SimpleNamespace(name="created-job")),
            Exception("job api failure"),
        ]
        scenario = self._write_scenario(
            instance_count=2, interfaces=["eth0"], egress={"latency": "100ms"}
        )
        with patch("time.sleep"):
            exit_code, _ = self._run_plugin(cli, scenario)
        self.assertEqual(exit_code, 1)
        self.assertEqual(cli.create_job.call_count, 2)
        # job name is appended before create_job raises: both names get deleted
        self.assertEqual(cli.delete_job.call_count, 2)

    def test_run_wait_for_job_timeout_returns_one_and_cleans_jobs(self):
        cli = self._make_cli(nodes=("node-1",))
        cli.get_job_status.return_value = SimpleNamespace(
            status=SimpleNamespace(succeeded=None, failed=None)
        )
        scenario = self._write_scenario(
            duration=60, interfaces=["eth0"], egress={"latency": "100ms"}
        )
        with patch("time.sleep"), self._clock_expiring_after_polls(cli, polls=1):
            exit_code, _ = self._run_plugin(cli, scenario)
        self.assertEqual(exit_code, 1)
        self.assertEqual(cli.create_job.call_count, 1)
        self.assertEqual(cli.delete_job.call_count, 1)

    def test_run_delete_job_raises_sets_exit_status_and_returns_one(self):
        cli = self._make_cli(nodes=("node-1",))
        cli.delete_job.side_effect = Exception("delete failed")
        scenario = self._write_scenario(
            duration=60, interfaces=["eth0"], egress={"latency": "100ms"}
        )
        with patch("time.sleep"), self._clock_expiring_after_polls(cli, polls=5):
            exit_code, scenario_telemetry = self._run_plugin(cli, scenario)
        # failure in the finally block reaches the outer except: telemetry IS set
        self.assertEqual(exit_code, 1)
        self.assertEqual(scenario_telemetry.exit_status, 1)

    def test_run_unknown_node_name_returns_one_and_sets_exit_status(self):
        cli = self._make_cli(nodes=("node-1",))
        scenario = self._write_scenario(
            node_name="ghost-node", interfaces=["eth0"], egress={"latency": "100ms"}
        )
        exit_code, scenario_telemetry = self._run_plugin(cli, scenario)
        self.assertEqual(exit_code, 1)
        self.assertEqual(scenario_telemetry.exit_status, 1)
        cli.create_job.assert_not_called()
        cli.delete_job.assert_not_called()

    def test_run_label_selector_matches_no_nodes_returns_one(self):
        cli = self._make_cli(nodes=())
        scenario = self._write_scenario(
            label_selector="app=nothing", egress={"latency": "100ms"}
        )
        exit_code, scenario_telemetry = self._run_plugin(cli, scenario)
        self.assertEqual(exit_code, 1)
        self.assertEqual(scenario_telemetry.exit_status, 1)
        cli.create_job.assert_not_called()

    # suspected bug: instance_count=0 is treated as "every matching node"
    # (common_node_functions.get_node returns all nodes when the count is 0)
    @unittest.expectedFailure
    def test_run_instance_count_zero_selects_all_nodes(self):
        cli = self._make_cli(nodes=("node-1", "node-2", "node-3"))
        scenario = self._write_scenario(
            instance_count=0, interfaces=["eth0"], egress={"latency": "100ms"}
        )
        with patch("time.sleep"), self._clock_expiring_after_polls(cli, polls=5):
            exit_code, _ = self._run_plugin(cli, scenario)
        # expected: 0 selects no nodes, so the run fails or creates no jobs
        self.assertTrue(
            exit_code != 0 or cli.create_job.call_count == 0,
            "instance_count=0 must not select every matching node",
        )

    def test_run_execution_typo_skips_wait_and_deletes_jobs_immediately(self):
        cli = self._make_cli(nodes=("node-1", "node-2"))
        scenario = self._write_scenario(
            duration=60,
            interfaces=["eth0"],
            execution="Parallel",
            instance_count=2,
            egress={"latency": "100ms"},
        )
        wait_patch, wait_calls = self._record_wait_calls()
        with patch("time.sleep"), wait_patch:
            exit_code, _ = self._run_plugin(cli, scenario)
        # expected: the typo is rejected (non-zero exit) or the jobs are waited on
        self.assertTrue(
            exit_code != 0 or len(wait_calls) > 0,
            "execution='Parallel' silently skipped waiting for its jobs",
        )

    def test_run_missing_egress_key_returns_one_without_creating_jobs(self):
        cli = self._make_cli(nodes=("node-1",))
        scenario = self._write_scenario(interfaces=["eth0"])
        with patch("time.sleep"):
            exit_code, _ = self._run_plugin(cli, scenario)
        self.assertEqual(exit_code, 0)
        self.assertEqual(cli.create_job.call_count, 1)
        body = cli.create_job.call_args[0][0]
        cmd = body["spec"]["template"]["spec"]["containers"][0]["command"][2]
        self.assertIn("rate 100mbit", cmd)

    def test_run_empty_egress_dict_is_silent_no_op_success(self):
        cli = self._make_cli(nodes=("node-1",))
        scenario = self._write_scenario(interfaces=["eth0"], egress={})
        with patch("time.sleep"):
            exit_code, _ = self._run_plugin(cli, scenario)
        # expected: an empty egress config is rejected with a non-zero exit
        self.assertNotEqual(exit_code, 0)

    def test_run_create_job_returns_none_sets_exit_status(self):
        cli = self._make_cli(nodes=("node-1", "node-2"))
        cli.create_job.return_value = None
        scenario = self._write_scenario(
            instance_count=2, interfaces=["eth0"], egress={"latency": "100ms"}
        )
        with patch("time.sleep"):
            _, scenario_telemetry = self._run_plugin(cli, scenario)
        self.assertEqual(scenario_telemetry.exit_status, 1)

    def test_run_create_job_raises_sets_exit_status(self):
        cli = self._make_cli(nodes=("node-1", "node-2"))
        cli.create_job.side_effect = Exception("job api failure")
        scenario = self._write_scenario(
            instance_count=2, interfaces=["eth0"], egress={"latency": "100ms"}
        )
        with patch("time.sleep"):
            _, scenario_telemetry = self._run_plugin(cli, scenario)
        self.assertEqual(scenario_telemetry.exit_status, 1)

    def test_run_wait_for_job_timeout_sets_exit_status(self):
        cli = self._make_cli(nodes=("node-1",))
        cli.get_job_status.return_value = SimpleNamespace(
            status=SimpleNamespace(succeeded=None, failed=None)
        )
        scenario = self._write_scenario(
            duration=60, interfaces=["eth0"], egress={"latency": "100ms"}
        )
        with patch("time.sleep"), self._clock_expiring_after_polls(cli, polls=1):
            _, scenario_telemetry = self._run_plugin(cli, scenario)
        self.assertEqual(scenario_telemetry.exit_status, 1)


class TestVerifyInterface(NetworkChaosScenarioTestBase):
    def test_verify_interface_explicit_interface_found_returns_interfaces(self):
        cli = self._make_cli(nodes=("node-1",))
        with patch("random.randint", side_effect=lambda a, b: a):
            result = self.plugin.verify_interface(
                ["eth0"], ["node-1"], _pod_template(), cli, image="img:1"
            )
        self.assertEqual(result, ["eth0"])
        body = cli.create_pod.call_args[0][0]
        cli.create_pod.assert_called_once_with(body, "default", 300)
        self.assertEqual(body["metadata"]["name"], "fedtools-0")
        self.assertEqual(body["spec"]["nodeName"], "node-1")
        self.assertEqual(body["spec"]["containers"][0]["image"], "img:1")
        self.assertIn("ip -br addr show", cli.exec_cmd_in_pod.call_args[0][0])
        cli.delete_pod.assert_called_once_with("fedtools-0", "default")

    def test_verify_interface_empty_list_auto_detects_default_route(self):
        cli = self._make_cli(nodes=("node-1",))
        cli.exec_cmd_in_pod.return_value = "eth0\n"
        with patch("random.randint", side_effect=lambda a, b: a):
            result = self.plugin.verify_interface(
                [], ["node-1"], _pod_template(), cli, image="img:1"
            )
        self.assertEqual(result, ["eth0"])
        self.assertIn("ip r | grep default", cli.exec_cmd_in_pod.call_args[0][0])
        cli.delete_pod.assert_called_once_with("fedtools-0", "default")

    def test_verify_interface_missing_interface_raises_and_deletes_pod(self):
        cli = self._make_cli(nodes=("node-1",))
        cli.exec_cmd_in_pod.return_value = "lo,eth1,"
        with patch("random.randint", side_effect=lambda a, b: a):
            with self.assertRaises(RuntimeError):
                self.plugin.verify_interface(
                    ["eth0"], ["node-1"], _pod_template(), cli, image="img:1"
                )
        cli.delete_pod.assert_called_once_with("fedtools-0", "default")

    def test_verify_interface_exec_raises_propagates_and_deletes_pod(self):
        cli = self._make_cli(nodes=("node-1",))
        cli.exec_cmd_in_pod.side_effect = Exception("exec boom")
        with patch("random.randint", side_effect=lambda a, b: a):
            with self.assertRaisesRegex(Exception, "exec boom"):
                self.plugin.verify_interface(
                    ["eth0"], ["node-1"], _pod_template(), cli, image="img:1"
                )
        cli.delete_pod.assert_called_once_with("fedtools-0", "default")


class TestGetEgressCmd(NetworkChaosScenarioTestBase):
    def test_get_egress_cmd_serial_single_interface_exact_command(self):
        cmd = self.plugin.get_egress_cmd(
            "serial", ["eth0"], "latency", {"latency": "100ms"}, duration=60
        )
        self.assertEqual(
            cmd,
            "sleep 30; tc qdisc add dev eth0 root netem delay 100ms ;"
            "  tc qdisc ls dev eth0 ; sleep 60; tc qdisc del dev eth0 root ;"
            " sleep 20; tc qdisc ls dev eth0 ;",
        )

    def test_get_egress_cmd_serial_maps_each_mod_to_netem_keyword(self):
        for mod, keyword in [
            ("latency", "delay"),
            ("loss", "loss"),
            ("bandwidth", "rate"),
        ]:
            with self.subTest(mod=mod):
                cmd = self.plugin.get_egress_cmd(
                    "serial", ["eth0"], mod, {mod: "7"}, duration=60
                )
                self.assertIn("root netem %s 7 ;" % keyword, cmd)

    def test_get_egress_cmd_parallel_appends_all_params_in_dict_order(self):
        cmd = self.plugin.get_egress_cmd(
            "parallel",
            ["eth0"],
            "latency",
            {"latency": "1ms", "loss": "5", "bandwidth": "1gbit"},
            duration=60,
        )
        self.assertIn(
            "tc qdisc add dev eth0 root netem delay 1ms  loss 5  rate 1gbit ;", cmd
        )
        self.assertEqual(cmd.count("tc qdisc add"), 1)
        self.assertIn("sleep 60;", cmd)

    def test_get_egress_cmd_serial_multiple_interfaces_renders_per_interface(self):
        cmd = self.plugin.get_egress_cmd(
            "serial", ["eth0", "eth1"], "loss", {"loss": "5"}, duration=30
        )
        self.assertEqual(
            cmd,
            "sleep 30; tc qdisc add dev eth0 root netem loss 5 ;"
            " tc qdisc add dev eth1 root netem loss 5 ;"
            "  tc qdisc ls dev eth0 ; tc qdisc ls dev eth1 ;"
            " sleep 30; tc qdisc del dev eth0 root ; tc qdisc del dev eth1 root ;"
            " sleep 20; tc qdisc ls dev eth0 ; tc qdisc ls dev eth1 ;",
        )

    def test_get_egress_cmd_unknown_serial_mod_raises_key_error(self):
        with self.assertRaisesRegex(KeyError, "jitter"):
            self.plugin.get_egress_cmd("serial", ["eth0"], "jitter", {"jitter": "1"})

    def test_get_egress_cmd_unknown_parallel_key_raises_key_error(self):
        with self.assertRaisesRegex(KeyError, "jitter"):
            self.plugin.get_egress_cmd(
                "parallel", ["eth0"], "latency", {"latency": "1ms", "jitter": "1"}
            )


class TestWaitForJob(NetworkChaosScenarioTestBase):
    def test_wait_for_job_all_succeed_returns_after_polling_each_job(self):
        cli = self._make_cli()
        with patch("time.sleep") as sleep_mock, self._clock_expiring_after_polls(cli, polls=5):
            result = self.plugin.wait_for_job(["job-a", "job-b"], cli, timeout=60)
        self.assertIsNone(result)
        self.assertEqual(cli.get_job_status.call_count, 2)
        polled = {call.args[0] for call in cli.get_job_status.call_args_list}
        self.assertEqual(polled, {"job-a", "job-b"})
        self.assertGreaterEqual(sleep_mock.call_count, 1)

    def test_wait_for_job_failed_job_counts_as_done(self):
        cli = self._make_cli()
        cli.get_job_status.return_value = SimpleNamespace(
            status=SimpleNamespace(succeeded=None, failed=1)
        )
        with patch("time.sleep"), self._clock_expiring_after_polls(cli, polls=5):
            result = self.plugin.wait_for_job(["job-a"], cli, timeout=60)
        self.assertIsNone(result)
        cli.get_job_status.assert_called_once_with("job-a", namespace="default")

    def test_wait_for_job_pending_then_terminal_polls_second_pass(self):
        cli = self._make_cli()
        cli.get_job_status.side_effect = [
            SimpleNamespace(status=SimpleNamespace(succeeded=None, failed=None)),
            SimpleNamespace(status=SimpleNamespace(succeeded=1, failed=None)),
        ]
        with patch("time.sleep"), self._clock_expiring_after_polls(cli, polls=5):
            result = self.plugin.wait_for_job(["job-a"], cli, timeout=60)
        self.assertIsNone(result)
        self.assertEqual(cli.get_job_status.call_count, 2)

    def test_wait_for_job_empty_list_returns_without_polling(self):
        cli = self._make_cli()
        with patch("time.sleep") as sleep_mock:
            result = self.plugin.wait_for_job([], cli, timeout=60)
        self.assertIsNone(result)
        cli.get_job_status.assert_not_called()
        sleep_mock.assert_not_called()

    def test_wait_for_job_successful_jobs_complete_in_one_pass(self):
        cli = self._make_cli()
        with patch("time.sleep") as sleep_mock, self._clock_expiring_after_polls(
            cli, polls=3
        ):
            result = self.plugin.wait_for_job(["job-a", "job-b"], cli, timeout=60)
        self.assertIsNone(result)
        # one status poll per job, in order: a single pass, no second pass
        self.assertEqual(
            cli.get_job_status.call_args_list,
            [call("job-a", namespace="default"), call("job-b", namespace="default")],
        )
        # one sleep per job polled in that single pass
        self.assertEqual(sleep_mock.call_count, 2)


class TestDeleteJob(NetworkChaosScenarioTestBase):
    def _failed_job_response(self):
        return SimpleNamespace(
            metadata=SimpleNamespace(labels={"controller-uid": "uid-1"}),
            status=SimpleNamespace(succeeded=None, failed=1),
        )

    def test_delete_job_failed_job_collects_logs_then_deletes(self):
        cli = self._make_cli()
        cli.get_job_status.return_value = self._failed_job_response()
        self.plugin.delete_job(["job-a"], cli)
        cli.list_pods.assert_called_once_with(
            label_selector="controller-uid=uid-1", namespace="default"
        )
        cli.read_pod.assert_called_once_with(name="jobpod-1", namespace="default")
        cli.get_pod_log.assert_called_once_with(name="jobpod-1", namespace="default")
        cli.delete_job.assert_called_once_with(name="job-a", namespace="default")

    def test_delete_job_succeeded_job_deletes_without_log_collection(self):
        cli = self._make_cli()
        self.plugin.delete_job(["job-a"], cli)
        cli.list_pods.assert_not_called()
        cli.read_pod.assert_not_called()
        cli.get_pod_log.assert_not_called()
        cli.delete_job.assert_called_once_with(name="job-a", namespace="default")

    def test_delete_job_status_raises_still_deletes(self):
        cli = self._make_cli()
        cli.get_job_status.side_effect = Exception("status boom")
        self.plugin.delete_job(["job-a"], cli)
        cli.delete_job.assert_called_once_with(name="job-a", namespace="default")

    def test_delete_job_no_job_pods_found_swallowed_still_deletes(self):
        cli = self._make_cli()
        cli.get_job_status.return_value = self._failed_job_response()
        cli.list_pods.return_value = []
        self.plugin.delete_job(["job-a"], cli)
        cli.read_pod.assert_not_called()
        cli.delete_job.assert_called_once_with(name="job-a", namespace="default")

    def test_delete_job_kube_delete_raises_propagates(self):
        cli = self._make_cli()
        cli.delete_job.side_effect = Exception("delete boom")
        with self.assertRaisesRegex(Exception, "delete boom"):
            self.plugin.delete_job(["job-a"], cli)


class TestWaitForJobEdgeCases(NetworkChaosScenarioTestBase):
    def test_wait_for_job_timeout_raises_starting_pod_failed(self):
        cli = self._make_cli()
        cli.get_job_status.return_value = SimpleNamespace(
            status=SimpleNamespace(succeeded=None, failed=None)
        )
        with patch("time.sleep"), self._clock_expiring_after_polls(cli, polls=1):
            with self.assertRaisesRegex(Exception, "Starting pod failed"):
                self.plugin.wait_for_job(["job-a"], cli, timeout=60)

    def test_wait_for_job_status_always_raises_polls_until_timeout(self):
        cli = self._make_cli()
        cli.get_job_status.side_effect = Exception("status boom")
        with patch("time.sleep"), self._clock_expiring_after_polls(cli, polls=3):
            with self.assertRaisesRegex(Exception, "Starting pod failed"):
                self.plugin.wait_for_job(["job-a"], cli, timeout=30)
        # status failures are warned per pass and polling continues until timeout
        self.assertEqual(cli.get_job_status.call_count, 3)

    def test_wait_for_job_remove_while_iterating_requires_extra_pass(self):
        cli = self._make_cli()
        with patch("time.sleep"), self._clock_expiring_after_polls(cli, polls=5):
            self.plugin.wait_for_job(["j1", "j2", "j3"], cli, timeout=60)
        polled = [call.args[0] for call in cli.get_job_status.call_args_list]
        # expected: one pass polls every job, in order
        self.assertEqual(polled, ["j1", "j2", "j3"])

    def test_wait_for_job_empties_caller_owned_list(self):
        cli = self._make_cli()
        jobs = ["job-a"]
        with patch("time.sleep"), self._clock_expiring_after_polls(cli, polls=5):
            self.plugin.wait_for_job(jobs, cli, timeout=60)
        # CHARACTERIZATION: wait_for_job empties the caller's list (run() passes joblst[:])
        self.assertEqual(jobs, [])

    def test_wait_for_job_succeeded_zero_counts_as_done(self):
        cli = self._make_cli()
        cli.get_job_status.return_value = SimpleNamespace(
            status=SimpleNamespace(succeeded=0, failed=None)
        )
        with patch("time.sleep"), self._clock_expiring_after_polls(cli, polls=5):
            result = self.plugin.wait_for_job(["job-a"], cli, timeout=60)
        self.assertIsNone(result)
        cli.get_job_status.assert_called_once_with("job-a", namespace="default")

    def test_wait_for_job_zero_timeout_raises_on_first_check(self):
        cli = self._make_cli()
        cli.get_job_status.return_value = SimpleNamespace(
            status=SimpleNamespace(succeeded=None, failed=None)
        )
        with patch("time.sleep"), self._clock_expiring_after_polls(cli, polls=1):
            with self.assertRaisesRegex(Exception, "Starting pod failed"):
                self.plugin.wait_for_job(["job-a"], cli, timeout=0)


class TestRunConfigErrors(NetworkChaosScenarioTestBase):
    def test_run_missing_scenario_file_returns_one(self):
        cli = self._make_cli(nodes=("node-1",))
        missing = os.path.join(self.tmpdir, "does-not-exist.yaml")
        exit_code, scenario_telemetry = self._run_plugin(cli, missing)
        self.assertEqual(exit_code, 1)
        self.assertEqual(scenario_telemetry.exit_status, 1)

    def test_run_scenario_without_network_chaos_key_returns_one(self):
        cli = self._make_cli(nodes=("node-1",))
        scenario = os.path.join(self.tmpdir, "other.yaml")
        with open(scenario, "w") as scenario_file:
            yaml.safe_dump({"other_scenario": {}}, scenario_file)
        exit_code, scenario_telemetry = self._run_plugin(cli, scenario)
        self.assertEqual(exit_code, 1)
        self.assertEqual(scenario_telemetry.exit_status, 1)

    def test_run_empty_scenario_file_returns_one(self):
        cli = self._make_cli(nodes=("node-1",))
        scenario = os.path.join(self.tmpdir, "empty.yaml")
        with open(scenario, "w") as scenario_file:
            scenario_file.write("")
        exit_code, scenario_telemetry = self._run_plugin(cli, scenario)
        self.assertEqual(exit_code, 1)
        self.assertEqual(scenario_telemetry.exit_status, 1)

    def test_run_duration_with_time_unit_returns_one(self):
        cli = self._make_cli(nodes=("node-1",))
        scenario = self._write_scenario(
            duration="10m", interfaces=["eth0"], egress={"latency": "100ms"}
        )
        exit_code, scenario_telemetry = self._run_plugin(cli, scenario)
        self.assertEqual(exit_code, 1)
        self.assertEqual(scenario_telemetry.exit_status, 1)
        cli.create_job.assert_not_called()

    def _job_cmd(self, cli):
        body = cli.create_job.call_args[0][0]
        return body["spec"]["template"]["spec"]["containers"][0]["command"][2]

    def test_run_numeric_string_duration_accepted_and_used_in_wait(self):
        cli = self._make_cli(nodes=("node-1",))
        scenario = self._write_scenario(
            duration="600", interfaces=["eth0"], egress={"latency": "100ms"}
        )
        wait_patch, wait_calls = self._record_wait_calls()
        with patch("time.sleep"), wait_patch, self._clock_expiring_after_polls(cli, polls=5):
            exit_code, _ = self._run_plugin(cli, scenario)
        self.assertEqual(exit_code, 0)
        self.assertEqual(wait_calls[0][1], 600 + 300)
        self.assertIn("sleep 600;", self._job_cmd(cli))

    def test_run_float_duration_truncated_to_integer(self):
        cli = self._make_cli(nodes=("node-1",))
        scenario = self._write_scenario(
            duration=1.5, interfaces=["eth0"], egress={"latency": "100ms"}
        )
        wait_patch, wait_calls = self._record_wait_calls()
        with patch("time.sleep"), wait_patch, self._clock_expiring_after_polls(cli, polls=5):
            exit_code, _ = self._run_plugin(cli, scenario)
        self.assertEqual(exit_code, 0)
        self.assertEqual(wait_calls[0][1], 1 + 300)
        self.assertIn("sleep 1;", self._job_cmd(cli))

    def test_run_negative_duration_succeeds_with_invalid_sleep_command(self):
        cli = self._make_cli(nodes=("node-1",))
        scenario = self._write_scenario(
            duration=-10, interfaces=["eth0"], egress={"latency": "100ms"}
        )
        with patch("time.sleep"), self._clock_expiring_after_polls(cli, polls=5):
            exit_code, _ = self._run_plugin(cli, scenario)
        # expected: a negative duration is rejected with a non-zero exit
        self.assertNotEqual(exit_code, 0)

    def test_run_egress_as_list_returns_one(self):
        cli = self._make_cli(nodes=("node-1",))
        scenario = self._write_scenario(
            interfaces=["eth0"], egress=["latency"]
        )
        exit_code, scenario_telemetry = self._run_plugin(cli, scenario)
        self.assertEqual(exit_code, 1)
        cli.create_job.assert_not_called()

    def test_run_egress_as_list_sets_exit_status(self):
        cli = self._make_cli(nodes=("node-1",))
        scenario = self._write_scenario(interfaces=["eth0"], egress=["latency"])
        _, scenario_telemetry = self._run_plugin(cli, scenario)
        self.assertEqual(scenario_telemetry.exit_status, 1)

    def test_run_egress_value_none_deploys_malformed_delay_command(self):
        cli = self._make_cli(nodes=("node-1",))
        scenario = self._write_scenario(interfaces=["eth0"], egress={"latency": None})
        with patch("time.sleep"), self._clock_expiring_after_polls(cli, polls=5):
            exit_code, _ = self._run_plugin(cli, scenario)
        # expected: a null egress value is rejected with a non-zero exit
        self.assertNotEqual(exit_code, 0)

    def test_run_interfaces_as_string_returns_one(self):
        cli = self._make_cli(nodes=("node-1",))
        scenario = self._write_scenario(interfaces="eth0", egress={"latency": "100ms"})
        exit_code, scenario_telemetry = self._run_plugin(cli, scenario)
        # iterating the string's characters never matches the interface list
        self.assertEqual(exit_code, 1)
        self.assertEqual(scenario_telemetry.exit_status, 1)
        cli.create_job.assert_not_called()

    def test_run_empty_interfaces_auto_detected_from_node(self):
        cli = self._make_cli(nodes=("node-1",))
        cli.exec_cmd_in_pod.return_value = "eth0\n"
        scenario = self._write_scenario(egress={"latency": "100ms"})
        wait_patch, wait_calls = self._record_wait_calls()
        with patch("time.sleep"), wait_patch, self._clock_expiring_after_polls(cli, polls=5):
            exit_code, _ = self._run_plugin(cli, scenario)
        self.assertEqual(exit_code, 0)
        self.assertIn("dev eth0 root netem", self._job_cmd(cli))

    def test_run_node_name_with_trailing_space_returns_one(self):
        cli = self._make_cli(nodes=("node-1",))
        scenario = self._write_scenario(
            node_name="node-1, node-2",
            interfaces=["eth0"],
            egress={"latency": "100ms"},
        )
        exit_code, scenario_telemetry = self._run_plugin(cli, scenario)
        self.assertEqual(exit_code, 1)
        self.assertEqual(scenario_telemetry.exit_status, 1)
        cli.create_job.assert_not_called()


class TestRunJobHandling(NetworkChaosScenarioTestBase):
    def test_run_serial_two_mods_waits_twice_with_cumulative_job_list(self):
        cli = self._make_cli(nodes=("node-1",))
        scenario = self._write_scenario(
            duration=60,
            interfaces=["eth0"],
            egress={"latency": "100ms", "loss": "5"},
        )
        wait_patch, wait_calls = self._record_wait_calls()
        with patch("time.sleep"), wait_patch, self._clock_expiring_after_polls(cli, polls=5):
            exit_code, _ = self._run_plugin(cli, scenario)
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(wait_calls), 2)
        # the second wait receives the cumulative list (both jobs)
        self.assertEqual(len(wait_calls[0][0]), 1)
        self.assertEqual(len(wait_calls[1][0]), 2)
        self.assertEqual(cli.delete_job.call_count, 2)

    def test_run_custom_image_propagated_to_pod_and_job_bodies(self):
        cli = self._make_cli(nodes=("node-1",))
        scenario = self._write_scenario(
            interfaces=["eth0"], egress={"latency": "100ms"}, image="myimg:1"
        )
        with patch("time.sleep"), self._clock_expiring_after_polls(cli, polls=5):
            exit_code, _ = self._run_plugin(cli, scenario)
        self.assertEqual(exit_code, 0)
        pod_body = cli.create_pod.call_args[0][0]
        self.assertEqual(pod_body["spec"]["containers"][0]["image"], "myimg:1")
        job_body = cli.create_job.call_args[0][0]
        self.assertEqual(
            job_body["spec"]["template"]["spec"]["containers"][0]["image"], "myimg:1"
        )

    def test_run_instance_count_ignored_when_node_name_given(self):
        cli = self._make_cli(nodes=("node-1", "node-2"))
        scenario = self._write_scenario(
            node_name="node-1,node-2",
            instance_count=1,
            interfaces=["eth0"],
            egress={"latency": "100ms"},
        )
        wait_patch, wait_calls = self._record_wait_calls()
        with patch("time.sleep"), wait_patch, self._clock_expiring_after_polls(cli, polls=5):
            exit_code, _ = self._run_plugin(cli, scenario)
        # CHARACTERIZATION: instance_count is ignored when node_name is given
        self.assertEqual(exit_code, 0)
        self.assertEqual(cli.create_job.call_count, 2)

    def test_run_identical_node_hash_produces_colliding_job_names(self):
        cli = self._make_cli(nodes=("node-1", "node-2"))
        scenario = self._write_scenario(
            node_name="node-1,node-2",
            interfaces=["eth0"],
            execution="parallel",
            egress={"latency": "100ms"},
        )
        with patch("time.sleep"), patch("builtins.hash", return_value=12345), self._clock_expiring_after_polls(cli, polls=5):
            exit_code, _ = self._run_plugin(cli, scenario)
        self.assertEqual(exit_code, 0)
        names = [c[0][0]["metadata"]["name"] for c in cli.create_job.call_args_list]
        self.assertEqual(len(set(names)), len(names), "job names must be unique per node")


def _pod_template():
    env = Environment(
        loader=FileSystemLoader(os.path.dirname(ncs_plugin.__file__)),
        autoescape=True,
    )
    return env.get_template("pod.j2")


class TestVerifyInterfaceParsing(NetworkChaosScenarioTestBase):
    def test_verify_interface_nodelst_none_raises_type_error(self):
        cli = self._make_cli()
        with self.assertRaisesRegex(TypeError, "NoneType"):
            self.plugin.verify_interface(["eth0"], None, _pod_template(), cli, "img:1")
        cli.create_pod.assert_not_called()

    def test_verify_interface_empty_nodelst_raises_value_error(self):
        cli = self._make_cli()
        with self.assertRaisesRegex(ValueError, "empty range"):
            self.plugin.verify_interface(["eth0"], [], _pod_template(), cli, "img:1")
        cli.create_pod.assert_not_called()

    def test_verify_interface_trailing_newline_hides_spurious_empty_entry(self):
        cli = self._make_cli(nodes=("node-1",))
        cli.exec_cmd_in_pod.return_value = "lo,eth0,\n"
        with patch("random.randint", side_effect=lambda a, b: a):
            with self.assertRaises(RuntimeError):
                self.plugin.verify_interface(
                    [""], ["node-1"], _pod_template(), cli, "img:1"
                )

    def test_verify_interface_output_without_trailing_comma_eats_last_char(self):
        cli = self._make_cli(nodes=("node-1",))
        cli.exec_cmd_in_pod.return_value = "lo,eth0"
        with patch("random.randint", side_effect=lambda a, b: a):
            result = self.plugin.verify_interface(
                ["eth0"], ["node-1"], _pod_template(), cli, "img:1"
            )
        self.assertEqual(result, ["eth0"])

    def test_verify_interface_multiple_default_routes_merges_interfaces(self):
        cli = self._make_cli(nodes=("node-1",))
        cli.exec_cmd_in_pod.return_value = "eth0\neth1"
        with patch("random.randint", side_effect=lambda a, b: a):
            result = self.plugin.verify_interface(
                [], ["node-1"], _pod_template(), cli, "img:1"
            )
        self.assertNotIn("eth0eth1", result)


class TestDeleteJobEdgeCases(NetworkChaosScenarioTestBase):
    def test_delete_job_log_data_as_str_swallowed_still_deletes(self):
        cli = self._make_cli()
        cli.get_job_status.return_value = SimpleNamespace(
            metadata=SimpleNamespace(labels={"controller-uid": "uid-1"}),
            status=SimpleNamespace(succeeded=None, failed=1),
        )
        cli.get_pod_log.return_value = SimpleNamespace(data="job log")
        self.plugin.delete_job(["job-a"], cli)
        cli.delete_job.assert_called_once_with(name="job-a", namespace="default")

    def test_delete_job_multiple_jobs_preserves_order(self):
        cli = self._make_cli()
        self.plugin.delete_job(["job-a", "job-b"], cli)
        self.assertEqual(
            [call.kwargs["name"] for call in cli.delete_job.call_args_list],
            ["job-a", "job-b"],
        )

    def test_delete_job_empty_list_makes_no_kube_calls(self):
        cli = self._make_cli()
        self.plugin.delete_job([], cli)
        cli.get_job_status.assert_not_called()
        cli.delete_job.assert_not_called()

    def test_delete_job_missing_controller_uid_label_still_deletes(self):
        cli = self._make_cli()
        cli.get_job_status.return_value = SimpleNamespace(
            metadata=SimpleNamespace(labels={}),
            status=SimpleNamespace(succeeded=None, failed=1),
        )
        self.plugin.delete_job(["job-a"], cli)
        cli.list_pods.assert_not_called()
        cli.delete_job.assert_called_once_with(name="job-a", namespace="default")


class TestVerifyInterfaceProbeSelection(NetworkChaosScenarioTestBase):
    def test_verify_interface_empty_exec_output_returns_empty_interface(self):
        cli = self._make_cli(nodes=("node-1",))
        cli.exec_cmd_in_pod.return_value = ""
        with patch("random.randint", side_effect=lambda a, b: a):
            result = self.plugin.verify_interface(
                [], ["node-1"], _pod_template(), cli, "img:1"
            )
        self.assertNotEqual(result, [""])

    def test_verify_interface_probes_only_one_randomly_chosen_node(self):
        cli = self._make_cli(nodes=("node-1", "node-2"))
        with patch("random.randint", side_effect=[1, 7]):
            result = self.plugin.verify_interface(
                ["eth0"], ["node-1", "node-2"], _pod_template(), cli, "img:1"
            )
        self.assertEqual(result, ["eth0"])
        body = cli.create_pod.call_args[0][0]
        # CHARACTERIZATION: only one randomly chosen node is probed, although chaos targets all nodes
        self.assertEqual(body["spec"]["nodeName"], "node-2")
        self.assertEqual(cli.exec_cmd_in_pod.call_args[0][1], "fedtools-7")


class TestGetEgressCmdDefaults(NetworkChaosScenarioTestBase):
    def test_get_egress_cmd_omitted_duration_defaults_to_30(self):
        cmd = self.plugin.get_egress_cmd("serial", ["eth0"], "latency", {"latency": "1ms"})
        self.assertIn("sleep 30;", cmd)
        self.assertNotIn("sleep 60", cmd)

    def test_get_egress_cmd_execution_match_is_case_sensitive(self):
        cmd = self.plugin.get_egress_cmd(
            "Serial", ["eth0"], "latency", {"latency": "1ms", "loss": "5"}, duration=60
        )
        # CHARACTERIZATION: only exact lowercase "parallel" takes the parallel branch
        self.assertIn("netem delay 1ms ;", cmd)
        self.assertNotIn("loss 5", cmd)

    def test_get_egress_cmd_parallel_empty_dict_renders_bare_netem(self):
        cmd = self.plugin.get_egress_cmd("parallel", ["eth0"], "latency", {})
        # no limits are applied: bare netem qdisc (silent no-op degradation)
        self.assertIn("tc qdisc add dev eth0 root netem;", cmd)

    def test_get_egress_cmd_interfaces_as_string_iterates_characters(self):
        cmd = self.plugin.get_egress_cmd("serial", "eth", "latency", {"latency": "1ms"})
        # CHARACTERIZATION: a string is iterated character by character
        self.assertIn("dev e root netem", cmd)
        self.assertIn("dev t root netem", cmd)
        self.assertIn("dev h root netem", cmd)


if __name__ == "__main__":
    unittest.main()
