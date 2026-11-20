#!/usr/bin/env python3

"""
Test suite for HogsScenarioPlugin class

Usage:
    python -m coverage run -a -m unittest tests/test_hogs_scenario_plugin.py -v

Assisted By: Claude Code
"""

import itertools
import os
import queue
import shutil
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

import yaml
from krkn_lib.k8s import KrknKubernetes
from krkn_lib.models.k8s import NodeResources
from krkn_lib.models.krkn import HogConfig, HogType
from krkn_lib.telemetry.ocp import KrknTelemetryOpenshift

import krkn.scenario_plugins.hogs.hogs_scenario_plugin as hogs_mod
from krkn.rollback.config import RollbackContent
from krkn.scenario_plugins.hogs.hogs_scenario_plugin import HogsScenarioPlugin


class TestHogsScenarioPlugin(unittest.TestCase):

    def setUp(self):
        """
        Set up test fixtures for HogsScenarioPlugin
        """
        self.plugin = HogsScenarioPlugin()

    def test_get_scenario_types(self):
        """
        Test get_scenario_types returns correct scenario type
        """
        result = self.plugin.get_scenario_types()

        self.assertEqual(result, ["hog_scenarios"])
        self.assertEqual(len(result), 1)

    def _stub_config(self):
        config = MagicMock()
        config.type.value = "cpu"
        return config

    def test_run_scenario_drains_all_exceptions(self):
        """Multiple worker failures are aggregated into one exception."""
        q = queue.Queue()
        q.put(Exception("node-a failed"))
        q.put(Exception("node-b failed"))
        with self.assertRaises(Exception) as ctx:
            self.plugin.run_scenario(self._stub_config(), MagicMock(), [], q)
        msg = str(ctx.exception)
        self.assertIn("2 node(s)", msg)
        self.assertIn("node-a failed", msg)
        self.assertIn("node-b failed", msg)
        self.assertTrue(q.empty())

    def test_run_scenario_single_exception(self):
        """A single worker failure propagates in the combined exception."""
        q = queue.Queue()
        q.put(Exception("only failure"))
        with self.assertRaises(Exception) as ctx:
            self.plugin.run_scenario(self._stub_config(), MagicMock(), [], q)
        self.assertIn("only failure", str(ctx.exception))

    def test_run_scenario_no_exception(self):
        """Empty exception queue means run_scenario does not raise."""
        q = queue.Queue()
        # should not raise when queue is empty
        self.plugin.run_scenario(self._stub_config(), MagicMock(), [], q)


class FakeTime:
    """time module stand-in that advances on every call.

    The worker sampling loop has no sleep, so a frozen time mock would
    hang the loop forever.
    """

    def __init__(self, step=0.5):
        self.t = 1000.0
        self.step = step
        self.sleeps = []

    def time(self):
        self.t += self.step
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


class HogsPluginTestCase(unittest.TestCase):
    """Shared harness: plugin with mocked rollback handler and kube client."""

    def setUp(self):
        self.plugin = HogsScenarioPlugin()
        # the real RollbackHandler built by AbstractScenarioPlugin.__init__
        # serializes version files to disk on set_rollback_callable
        self.plugin.rollback_handler = MagicMock()
        self.kube = MagicMock(spec=KrknKubernetes)
        self.kube.list_nodes.return_value = ["node-a", "node-b", "node-c"]
        self.kube.get_node_cpu_count.return_value = 4
        self.kube.deploy_hog.return_value = None
        # must be a real bool: a bare MagicMock is truthy => "pod running"
        self.kube.is_pod_running.return_value = False
        self.kube.delete_pod.return_value = None
        self.kube.get_node_resources_info.side_effect = (
            lambda node: self.make_node_resources()
        )
        self.lib_telemetry = MagicMock()
        self.lib_telemetry.get_lib_kubernetes.return_value = self.kube

    @staticmethod
    def make_node_resources(cpu=0, memory=0, disk_space=0):
        resources = NodeResources()
        resources.cpu = cpu
        resources.memory = memory
        resources.disk_space = disk_space
        return resources

    def write_scenario(self, content):
        """Writes yaml content (dict, or raw string) to a temp file."""
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        path = os.path.join(directory, "scenario.yaml")
        with open(path, "w") as f:
            if isinstance(content, str):
                f.write(content)
            else:
                yaml.safe_dump(content, f)
        return path


class TestRollbackHogPod(HogsPluginTestCase):

    def test_rollback_hog_pod_deletes_pod_positionally(self):
        content = RollbackContent(namespace="ns", resource_identifier="pod-1")
        with self.assertLogs(level="INFO") as captured:
            result = HogsScenarioPlugin.rollback_hog_pod(content, self.lib_telemetry)
        self.assertIsNone(result)
        self.kube.delete_pod.assert_called_once_with("pod-1", "ns")
        self.assertTrue(any("completed successfully" in m for m in captured.output))

    def test_rollback_hog_pod_delete_failure_is_swallowed(self):
        self.kube.delete_pod.side_effect = Exception("delete failed")
        content = RollbackContent(namespace="ns", resource_identifier="pod-1")
        with self.assertLogs(level="ERROR") as captured:
            HogsScenarioPlugin.rollback_hog_pod(content, self.lib_telemetry)
        self.assertTrue(any("Failed to rollback hog pod" in m for m in captured.output))

    def test_rollback_hog_pod_default_namespace_none_still_attempts_delete(self):
        content = RollbackContent(resource_identifier="pod-1")
        HogsScenarioPlugin.rollback_hog_pod(content, self.lib_telemetry)
        self.kube.delete_pod.assert_called_once_with("pod-1", None)

    def test_rollback_hog_pod_none_lib_telemetry_logs_error(self):
        content = RollbackContent(resource_identifier="pod-1")
        with self.assertLogs(level="ERROR") as captured:
            HogsScenarioPlugin.rollback_hog_pod(content, None)
        self.assertFalse(self.kube.delete_pod.called)
        self.assertTrue(any("Failed to rollback hog pod" in m for m in captured.output))


class TestRunScenarioWorker(HogsPluginTestCase):

    def _run_worker(self, config, node="node-a", step=0.5):
        """Runs the worker synchronously under FakeTime; returns the queue."""
        q = queue.Queue()
        fake_time = FakeTime(step=step)
        with patch.object(hogs_mod, "time", fake_time), \
                patch.object(hogs_mod, "get_random_string", lambda n: "x" * n):
            self.plugin.run_scenario_worker(config, self.kube, node, q)
        return q

    # Sample-count arithmetic (tied to FakeTime(step=0.5) and the 3s pre-sleep):
    # start=t0+0.5; the pre-sleep adds 3s; each time() call advances 0.5s, so the
    # loop checks elapsed = 3.5, 4.0, 4.5, ... and samples while elapsed < duration-1.
    # duration=5 (window 4s) => 1 sample; duration=6 (window 5s) => 3 samples.
    def test_worker_cpu_happy_path(self):
        config = HogConfig()
        config.duration = 5  # 1 sample under FakeTime(step=0.5)
        self.kube.get_node_resources_info.side_effect = (
            lambda node: self.make_node_resources(cpu=10 ** 9)
        )
        q = self._run_worker(config)
        self.assertTrue(q.empty())
        self.kube.get_node_cpu_count.assert_called_once_with("node-a")
        self.kube.deploy_hog.assert_called_once()
        pod_name, deployed_config = self.kube.deploy_hog.call_args[0]
        self.assertEqual(pod_name, "cpu-hog-xxxxx")
        self.assertIs(deployed_config, config)
        self.kube.delete_pod.assert_called_once_with("cpu-hog-xxxxx", "default")

    def test_worker_registers_rollback_before_deploy(self):
        config = HogConfig()
        config.duration = 5
        events = []
        self.plugin.rollback_handler.set_rollback_callable.side_effect = (
            lambda *a: events.append("rollback-registered")
        )
        self.kube.deploy_hog.side_effect = lambda *a: events.append("deployed")
        q = self._run_worker(config)
        self.assertTrue(q.empty())
        self.assertEqual(events, ["rollback-registered", "deployed"])

    def test_worker_overwrites_selector_with_target_node(self):
        config = HogConfig()
        config.duration = 5
        config.node_selector = "original=selector"
        self._run_worker(config, node="node-b")
        deployed_config = self.kube.deploy_hog.call_args[0][1]
        self.assertEqual(deployed_config.node_selector, "kubernetes.io/hostname=node-b")

    def test_worker_preset_workers_skips_cpu_detection(self):
        config = HogConfig()
        config.duration = 5
        config.workers = 2
        self._run_worker(config)
        self.assertFalse(self.kube.get_node_cpu_count.called)

    def test_worker_deploy_failure_queues_exception(self):
        config = HogConfig()
        config.duration = 5
        self.kube.deploy_hog.side_effect = ValueError("deploy boom")
        q = self._run_worker(config)
        error = q.get_nowait()
        self.assertIsInstance(error, ValueError)
        self.assertEqual(str(error), "deploy boom")
        # registration happens before deploy, so it fired despite the failure
        self.plugin.rollback_handler.set_rollback_callable.assert_called_once()
        self.assertFalse(self.kube.delete_pod.called)


    def test_worker_duration_four_zero_samples_does_not_queue_zero_division(self):
        # sampling window is duration-1 = 3s, but the 3s pre-sleep already
        # exhausts it: zero samples => avg/len(samples) divides by zero
        config = HogConfig()
        config.duration = 4
        q = self._run_worker(config)
        # a zero-sample run must not surface as a bare ZeroDivisionError
        error = q.get_nowait() if not q.empty() else None
        self.assertFalse(isinstance(error, ZeroDivisionError), f"got {error!r}")
        # delete_pod (line 130) runs before the averaging, so cleanup still fired
        self.kube.delete_pod.assert_called_once()

    def test_worker_pod_never_finishes_times_out_after_30s(self):
        config = HogConfig()
        config.duration = 5
        self.kube.is_pod_running.return_value = True
        fake_time = FakeTime()
        q = queue.Queue()
        with patch.object(hogs_mod, "time", fake_time), \
                patch.object(hogs_mod, "get_random_string", lambda n: "x" * n):
            self.plugin.run_scenario_worker(config, self.kube, "node-a", q)
        error = q.get_nowait()
        self.assertIn("didn't finish after 30", str(error))
        self.assertEqual(fake_time.sleeps, [3] + [1] * 30)

    def test_worker_pod_timeout_should_delete_pod(self):
        config = HogConfig()
        config.duration = 5
        self.kube.is_pod_running.return_value = True
        self._run_worker(config)
        self.kube.delete_pod.assert_called_once_with("cpu-hog-xxxxx", "default")

    def test_worker_sampling_api_failure_mid_run_queues_exception(self):
        config = HogConfig()
        config.duration = 5
        # start sample, post-sleep sample, then the in-loop call fails
        self.kube.get_node_resources_info.side_effect = [
            self.make_node_resources(cpu=1),
            self.make_node_resources(cpu=1),
            Exception("metrics api down"),
        ]
        q = self._run_worker(config)
        self.assertEqual(str(q.get_nowait()), "metrics api down")

    def test_worker_sampling_failure_should_delete_pod(self):
        config = HogConfig()
        config.duration = 5
        self.kube.get_node_resources_info.side_effect = [
            self.make_node_resources(cpu=1),
            self.make_node_resources(cpu=1),
            Exception("metrics api down"),
        ]
        self._run_worker(config)
        self.kube.delete_pod.assert_called_once_with("cpu-hog-xxxxx", "default")

    def test_worker_memory_hog_logs_increase_from_start_baseline(self):
        config = HogConfig()
        config.type = HogType.memory
        config.duration = 5
        self.kube.get_node_resources_info.side_effect = [
            self.make_node_resources(memory=100),
            self.make_node_resources(memory=100),
            self.make_node_resources(memory=50),
        ]
        with self.assertLogs(level="INFO") as captured:
            q = self._run_worker(config)
        self.assertTrue(q.empty())
        self.assertTrue(any("detected memory increase: 50.0 %" in m for m in captured.output))
        self.kube.delete_pod.assert_called_once_with("memory-hog-xxxxx", "default")

    def test_worker_memory_hog_zero_start_memory_queues_zero_division(self):
        # Characterization: pins current behaviour; a zero baseline memory
        # divides by zero and is queued as ZeroDivisionError
        config = HogConfig()
        config.type = HogType.memory
        config.duration = 5
        self.kube.get_node_resources_info.side_effect = [
            self.make_node_resources(memory=0),
            self.make_node_resources(memory=0),
            self.make_node_resources(memory=50),
        ]
        q = self._run_worker(config)
        self.assertIsInstance(q.get_nowait(), ZeroDivisionError)


    def test_worker_io_hog_logs_allocated_disk_space(self):
        config = HogConfig()
        config.type = HogType.io
        config.duration = 5
        self.kube.get_node_resources_info.side_effect = [
            self.make_node_resources(disk_space=2 * 1024 * 1024),
            self.make_node_resources(disk_space=2 * 1024 * 1024),
            self.make_node_resources(disk_space=0),
        ]
        with self.assertLogs(level="INFO") as captured:
            q = self._run_worker(config)
        self.assertTrue(q.empty())
        self.assertTrue(any("detected disk space allocated: 2.0 MB" in m for m in captured.output))

    def test_worker_detected_zero_cpus_does_not_queue_zero_division(self):
        # get_node_cpu_count returns 0 on any API error => workers * 10**9 == 0
        self.kube.get_node_cpu_count.return_value = 0
        self.kube.get_node_resources_info.side_effect = (
            lambda node: self.make_node_resources(cpu=1)
        )
        config = HogConfig()
        config.duration = 5
        with self.assertLogs(level="INFO") as captured:
            q = self._run_worker(config)
        self.assertTrue(any("detected 0 cpus" in m for m in captured.output))
        # a zero-cpu node must not surface as a bare ZeroDivisionError; a
        # deliberate validation error with a message is acceptable
        error = q.get_nowait() if not q.empty() else None
        self.assertFalse(isinstance(error, ZeroDivisionError), f"got {error!r}")
        self.kube.delete_pod.assert_called_once()

    def test_worker_cpu_avg_over_samples(self):
        config = HogConfig()
        config.duration = 6  # 3 samples under FakeTime(step=0.5)
        config.workers = 4
        self.kube.get_node_resources_info.side_effect = (
            lambda node: self.make_node_resources(cpu=2)
        )
        expected = f"{(2.0 / (4 * 10 ** 9)) * 100} %"
        with self.assertLogs(level="INFO") as captured:
            q = self._run_worker(config)
        self.assertTrue(q.empty())
        self.assertTrue(any(f"detected cpu consumption: {expected}" in m for m in captured.output))

    def test_worker_rollback_registration_failure_queues_exception(self):
        config = HogConfig()
        config.duration = 5
        self.plugin.rollback_handler.set_rollback_callable.side_effect = Exception(
            "rollback registration failed"
        )
        q = self._run_worker(config)
        self.assertEqual(str(q.get_nowait()), "rollback registration failed")
        self.assertFalse(self.kube.deploy_hog.called)

    def test_worker_wait_loop_exits_on_pod_finish_boundary(self):
        config = HogConfig()
        config.duration = 5
        self.kube.is_pod_running.side_effect = [True] * 30 + [False]
        q = self._run_worker(config)
        self.assertTrue(q.empty())
        self.assertEqual(self.kube.is_pod_running.call_count, 31)
        self.kube.delete_pod.assert_called_once()


class TestRun(HogsPluginTestCase):

    def _run(self, scenario_content, run_uuid="u1"):
        scenario = self.write_scenario(scenario_content)
        telemetry = MagicMock()
        return self.plugin.run(run_uuid, scenario, self.lib_telemetry, telemetry)

    def _run_scenario_mock(self):
        run_scenario = MagicMock()
        self.plugin.run_scenario = run_scenario
        return run_scenario

    def test_run_cpu_happy_path(self):
        run_scenario = self._run_scenario_mock()
        rc = self._run({"hog-type": "cpu", "node-selector": "k=x"})
        self.assertEqual(rc, 0)
        run_scenario.assert_called_once()
        config, lib_k8s, nodes, q = run_scenario.call_args[0]
        self.assertEqual(config.type, HogType.cpu)
        self.assertEqual(config.node_selector, "k=x")
        self.assertIs(lib_k8s, self.kube)
        self.assertEqual(nodes, ["node-a", "node-b", "node-c"])
        self.assertIsInstance(q, queue.Queue)
        self.kube.list_nodes.assert_called_once_with("k=x")

    def test_run_node_name_targets_single_node(self):
        self.kube.list_nodes.return_value = ["node-a", "node-b"]
        run_scenario = self._run_scenario_mock()
        rc = self._run({"hog-type": "cpu", "node-selector": "k=x", "node-name": "node-b"})
        self.assertEqual(rc, 0)
        self.kube.list_nodes.assert_called_once_with("")
        config, _, nodes, _ = run_scenario.call_args[0]
        self.assertEqual(nodes, ["node-b"])

    def test_run_node_name_not_found_returns_error(self):
        run_scenario = self._run_scenario_mock()
        with self.assertLogs(level="ERROR") as captured:
            rc = self._run({"hog-type": "cpu", "node-selector": "k=x", "node-name": "ghost"})
        self.assertEqual(rc, 1)
        self.assertFalse(run_scenario.called)
        self.assertTrue(any("not found or not available" in m for m in captured.output))

    def test_run_no_available_nodes_returns_error(self):
        self.kube.list_nodes.return_value = []
        run_scenario = self._run_scenario_mock()
        with self.assertLogs(level="ERROR") as captured:
            rc = self._run({"hog-type": "cpu", "node-selector": "k=x"})
        self.assertEqual(rc, 1)
        self.assertFalse(run_scenario.called)
        self.assertTrue(any("no available nodes to schedule workload" in m for m in captured.output))

    def test_run_invalid_selector_falls_back_to_all_nodes(self):
        run_scenario = self._run_scenario_mock()
        with self.assertLogs(level="WARNING") as captured:
            rc = self._run({"hog-type": "cpu", "node-selector": "noequalsign"})
        self.assertEqual(rc, 0)
        self.assertTrue(any("not in right format" in m for m in captured.output))
        self.kube.list_nodes.assert_called_once_with("")

    # suspected bug: invalid selector lists all nodes; a single random node is intended (has_selector hard-coded True makes the random branch dead) (was raises=AssertionError)
    @unittest.expectedFailure
    def test_run_invalid_selector_selects_single_node(self):
        run_scenario = self._run_scenario_mock()
        rc = self._run({"hog-type": "cpu", "node-selector": "noequalsign"})
        self.assertEqual(rc, 0)
        self.assertEqual(len(run_scenario.call_args[0][2]), 1)

    def test_run_missing_node_selector_key_returns_error(self):
        # suspected bug: from_yaml_dict treats only hog-type as mandatory but
        # reads node-selector unconditionally; the KeyError surfaces as a
        # silent run() => 1 instead of a field validation error
        run_scenario = self._run_scenario_mock()
        with self.assertLogs(level="ERROR") as captured:
            rc = self._run({"hog-type": "cpu"})
        self.assertEqual(rc, 1)
        self.assertTrue(any("scenario exception: 'node-selector'" in m for m in captured.output))
        self.assertFalse(run_scenario.called)

    def test_run_empty_string_selector_queries_nodes_with_empty_selector(self):
        run_scenario = self._run_scenario_mock()
        rc = self._run({"hog-type": "cpu", "node-selector": ""})
        self.assertEqual(rc, 0)
        self.kube.list_nodes.assert_called_once_with("")



    def test_run_scenario_failure_returns_error(self):
        run_scenario = self._run_scenario_mock()
        run_scenario.side_effect = Exception("worker boom")
        with self.assertLogs(level="ERROR") as captured:
            rc = self._run({"hog-type": "cpu", "node-selector": "k=x"})
        self.assertEqual(rc, 1)
        self.assertTrue(any("scenario exception: worker boom" in m for m in captured.output))

    def test_run_number_of_nodes_samples_subset(self):
        run_scenario = self._run_scenario_mock()
        with patch.object(hogs_mod.random, "sample", return_value=["node-b", "node-c"]) as sample:
            rc = self._run({"hog-type": "cpu", "node-selector": "k=x", "number-of-nodes": 2})
        self.assertEqual(rc, 0)
        sample.assert_called_once_with(["node-a", "node-b", "node-c"], 2)
        self.assertEqual(run_scenario.call_args[0][2], ["node-b", "node-c"])

    def test_run_number_of_nodes_above_available_keeps_all(self):
        run_scenario = self._run_scenario_mock()
        with patch.object(hogs_mod.random, "sample") as sample:
            rc = self._run({"hog-type": "cpu", "node-selector": "k=x", "number-of-nodes": 5})
        self.assertEqual(rc, 0)
        self.assertFalse(sample.called)
        self.assertEqual(run_scenario.call_args[0][2], ["node-a", "node-b", "node-c"])

    def test_run_number_of_nodes_zero_keeps_all(self):
        # 0 is falsy: from_yaml_dict leaves number_of_nodes None => no sampling
        run_scenario = self._run_scenario_mock()
        with patch.object(hogs_mod.random, "sample") as sample:
            rc = self._run({"hog-type": "cpu", "node-selector": "k=x", "number-of-nodes": 0})
        self.assertEqual(rc, 0)
        self.assertFalse(sample.called)
        self.assertIsNone(run_scenario.call_args[0][0].number_of_nodes)

    def test_run_number_of_nodes_negative_returns_error(self):
        # real random.sample rejects a negative k
        with self.assertLogs(level="ERROR") as captured:
            rc = self._run({"hog-type": "cpu", "node-selector": "k=x", "number-of-nodes": -1})
        self.assertEqual(rc, 1)
        self.assertTrue(any("scenario exception: Sample larger than population or is negative" in m
                            for m in captured.output))

    def test_run_empty_scenario_file_returns_error(self):
        run_scenario = self._run_scenario_mock()
        with self.assertLogs(level="ERROR") as captured:
            rc = self._run("")
        self.assertEqual(rc, 1)
        self.assertFalse(run_scenario.called)
        self.assertTrue(any("scenario exception: 'NoneType' object has no attribute 'keys'" in m
                            for m in captured.output))

    def test_run_invalid_hog_type_returns_error(self):
        with self.assertLogs(level="ERROR") as captured:
            rc = self._run({"hog-type": "gpu", "node-selector": "k=x"})
        self.assertEqual(rc, 1)
        self.assertTrue(any("scenario exception: 'gpu'" in m for m in captured.output))

    def test_run_scenario_file_with_top_level_list_returns_error(self):
        with self.assertLogs(level="ERROR") as captured:
            rc = self._run("- a\n- b\n")
        self.assertEqual(rc, 1)
        self.assertTrue(any("scenario exception: 'list' object has no attribute 'keys'" in m
                            for m in captured.output))

    def test_run_nonexistent_scenario_path_returns_error(self):
        telemetry = MagicMock()
        with self.assertLogs(level="ERROR"):
            rc = self.plugin.run("u1", "/no/such/scenario.yaml", self.lib_telemetry, telemetry)
        self.assertEqual(rc, 1)

    def test_run_propagates_duration_and_namespace(self):
        run_scenario = self._run_scenario_mock()
        rc = self._run({"hog-type": "cpu", "node-selector": "k=x",
                        "duration": 5, "namespace": "hog-ns"})
        self.assertEqual(rc, 0)
        config = run_scenario.call_args[0][0]
        self.assertEqual(config.duration, 5)
        self.assertEqual(config.namespace, "hog-ns")

    def test_run_quoted_numeric_duration_is_cast_to_int(self):
        run_scenario = self._run_scenario_mock()
        rc = self._run({"hog-type": "cpu", "node-selector": "k=x", "duration": "5"})
        self.assertEqual(rc, 0)
        self.assertEqual(run_scenario.call_args[0][0].duration, 5)

    def test_run_quoted_numeric_workers_is_cast_to_int(self):
        run_scenario = self._run_scenario_mock()
        rc = self._run({"hog-type": "cpu", "node-selector": "k=x", "workers": "4"})
        self.assertEqual(rc, 0)
        self.assertEqual(run_scenario.call_args[0][0].workers, 4)

    def test_run_sets_and_clears_rollback_context(self):
        self._run_scenario_mock()
        rc = self._run({"hog-type": "cpu", "node-selector": "k=x"}, run_uuid="u1")
        self.assertEqual(rc, 0)
        self.plugin.rollback_handler.set_context.assert_called_once_with("u1")
        self.plugin.rollback_handler.clear_context.assert_called_once()

    def test_run_empty_run_uuid_skips_set_context_but_clears(self):
        self._run_scenario_mock()
        rc = self._run({"hog-type": "cpu", "node-selector": "k=x"}, run_uuid="")
        self.assertEqual(rc, 0)
        self.assertFalse(self.plugin.rollback_handler.set_context.called)
        self.plugin.rollback_handler.clear_context.assert_called_once()

    def test_run_empty_node_name_falls_back_to_selector(self):
        run_scenario = self._run_scenario_mock()
        rc = self._run({"hog-type": "cpu", "node-selector": "k=x", "node-name": ""})
        self.assertEqual(rc, 0)
        self.kube.list_nodes.assert_called_once_with("k=x")
        self.assertEqual(run_scenario.call_args[0][2], ["node-a", "node-b", "node-c"])

    def test_run_selector_with_empty_value_still_selects_nodes(self):
        # over-match hazard: regex ^.+=.*$ accepts "key="
        run_scenario = self._run_scenario_mock()
        rc = self._run({"hog-type": "cpu", "node-selector": "key="})
        self.assertEqual(rc, 0)
        self.kube.list_nodes.assert_called_once_with("key=")



class TestRunScenarioThreads(HogsPluginTestCase):
    """Full-stack run_scenario tests: real threads, FakeTime, unique pod names."""

    def _run_full(self, config, nodes):
        q = queue.Queue()
        counter = itertools.count()
        with patch.object(hogs_mod, "time", FakeTime()), \
                patch.object(hogs_mod, "get_random_string", lambda n: f"{next(counter)}" * n):
            self.plugin.run_scenario(config, self.kube, nodes, q)
        return q

    def _deployed(self):
        return [(call[0][0], call[0][1]) for call in self.kube.deploy_hog.call_args_list]

    def test_one_worker_per_node_with_distinct_selectors(self):
        config = HogConfig()
        config.duration = 5
        q = self._run_full(config, ["node-a", "node-b"])
        self.assertTrue(q.empty())
        deployed = self._deployed()
        self.assertEqual(len(deployed), 2)
        self.assertEqual({cfg.node_selector for _, cfg in deployed},
                         {"kubernetes.io/hostname=node-a", "kubernetes.io/hostname=node-b"})
        self.assertEqual(len({pod for pod, _ in deployed}), 2)
        self.assertEqual(self.kube.delete_pod.call_count, 2)

    def test_config_deep_copied_per_node(self):
        config = HogConfig()
        config.duration = 5
        self._run_full(config, ["node-a", "node-b"])
        # the caller-owned config is never mutated by the workers
        self.assertEqual(config.node_selector, "")
        self.assertIsNone(config.workers)
        deployed = [cfg for _, cfg in self._deployed()]
        self.assertIsNot(deployed[0], config)
        self.assertIsNot(deployed[0], deployed[1])

    def test_join_waits_for_slow_worker_before_returning(self):
        config = HogConfig()
        config.duration = 5
        self.kube.is_pod_running.side_effect = [True] * 5 + [False]
        q = self._run_full(config, ["node-a"])
        self.assertTrue(q.empty())
        self.assertEqual(self.kube.is_pod_running.call_count, 6)
        # observable only if join() really waited for the worker
        self.kube.delete_pod.assert_called_once_with("cpu-hog-00000", "default")

    def test_run_scenario_waits_for_late_worker_error(self):
        # a worker still running when run_scenario returns would enqueue its
        # error after the drain loop; join() must block until it has finished
        config = HogConfig()
        config.duration = 5
        release = threading.Event()
        worker_done = threading.Event()
        exception_queue = queue.Queue()
        self.addCleanup(release.set)

        def late_worker(cfg, lib_k8s, node, q):
            # bounded wait: the test cannot hang even if join() is missing
            release.wait(5)
            q.put(Exception("late worker boom"))
            worker_done.set()

        timer = threading.Timer(0.1, release.set)
        timer.daemon = True
        timer.start()
        self.addCleanup(timer.cancel)
        with patch.object(self.plugin, "run_scenario_worker", late_worker):
            with self.assertRaises(Exception) as ctx:
                self.plugin.run_scenario(config, self.kube, ["node-a"], exception_queue)
        self.assertTrue(worker_done.is_set())
        self.assertIn("failed on 1 node(s)", str(ctx.exception))
        self.assertIn("late worker boom", str(ctx.exception))

    def test_mixed_success_and_failure_reports_failed_node(self):
        config = HogConfig()
        config.duration = 5

        def deploy(pod_name, cfg):
            if "node-b" in cfg.node_selector:
                raise Exception("node-b deploy failed")

        self.kube.deploy_hog.side_effect = deploy
        with self.assertRaises(Exception) as ctx:
            self._run_full(config, ["node-a", "node-b"])
        msg = str(ctx.exception)
        self.assertIn("failed on 1 node(s)", msg)
        self.assertIn("node-b deploy failed", msg)
        # the healthy node's pod is still cleaned up; pod names come from a
        # shared counter across threads, so compare against the deployed names
        self.assertEqual(self.kube.delete_pod.call_count, 1)
        deleted_pod = self.kube.delete_pod.call_args[0][0]
        failed_pod = next(pod for pod, cfg in self._deployed()
                          if "node-b" in cfg.node_selector)
        self.assertNotEqual(deleted_pod, failed_pod)

    def test_duplicate_node_names_get_one_worker_each(self):
        # documents that run_scenario does not deduplicate node names
        config = HogConfig()
        config.duration = 5
        q = self._run_full(config, ["node-a", "node-a"])
        self.assertTrue(q.empty())
        deployed = self._deployed()
        self.assertEqual(len(deployed), 2)
        self.assertEqual({cfg.node_selector for _, cfg in deployed},
                         {"kubernetes.io/hostname=node-a"})

    def test_empty_node_list_starts_no_threads(self):
        config = HogConfig()
        q = queue.Queue()
        with patch.object(hogs_mod.threading, "Thread",
                          wraps=hogs_mod.threading.Thread) as thread_cls:
            self.plugin.run_scenario(config, self.kube, [], q)
        self.assertFalse(thread_cls.called)
        self.assertFalse(self.kube.deploy_hog.called)

    def test_none_hog_type_raises_before_threads_start(self):
        config = HogConfig()
        config.type = None
        with self.assertRaisesRegex(AttributeError, "'NoneType' object has no attribute 'value'"):
            self.plugin.run_scenario(config, self.kube, ["node-a"], queue.Queue())
        self.assertFalse(self.kube.deploy_hog.called)

    def test_empty_message_exception_yields_empty_detail(self):
        # errors are reduced to str(e): an exception without message
        # produces a combined error with no diagnostics
        config = HogConfig()
        config.duration = 5
        self.kube.deploy_hog.side_effect = Exception()
        with self.assertRaises(Exception) as ctx:
            self._run_full(config, ["node-a"])
        self.assertIn("Exception", str(ctx.exception))

    def test_workers_are_daemon_threads(self):
        config = HogConfig()
        config.duration = 5
        real_thread = hogs_mod.threading.Thread
        created = []

        def make_thread(*args, **kwargs):
            worker = real_thread(*args, **kwargs)
            created.append(worker)
            return worker

        with patch.object(hogs_mod.threading, "Thread", side_effect=make_thread):
            self._run_full(config, ["node-a"])
        self.assertEqual(len(created), 1)
        self.assertTrue(created[0].daemon)

    def test_all_workers_fail_messages_aggregated(self):
        config = HogConfig()
        config.duration = 5

        def deploy(pod_name, cfg):
            node = cfg.node_selector.split("=")[-1]
            raise Exception(f"{node} failed")

        self.kube.deploy_hog.side_effect = deploy
        with self.assertRaises(Exception) as ctx:
            self._run_full(config, ["node-a", "node-b"])
        msg = str(ctx.exception)
        # queue order across threads is racy: assert count and membership only
        self.assertIn("failed on 2 node(s)", msg)
        self.assertIn("node-a failed", msg)
        self.assertIn("node-b failed", msg)

    def test_registered_rollback_pairing_is_executable(self):
        config = HogConfig()
        config.duration = 5
        q = self._run_full(config, ["node-a"])
        self.assertTrue(q.empty())
        self.plugin.rollback_handler.set_rollback_callable.assert_called_once()
        rollback_fn, content = self.plugin.rollback_handler.set_rollback_callable.call_args[0]
        self.assertIs(rollback_fn, HogsScenarioPlugin.rollback_hog_pod)
        deployed_pod = self._deployed()[0][0]
        self.assertEqual(content, RollbackContent(namespace="default",
                                                   resource_identifier=deployed_pod))
        fresh_kube = MagicMock(spec=KrknKubernetes)
        fresh_telemetry = MagicMock()
        fresh_telemetry.get_lib_kubernetes.return_value = fresh_kube
        with self.assertLogs(level="INFO"):
            rollback_fn(content, fresh_telemetry)
        fresh_kube.delete_pod.assert_called_once_with(deployed_pod, "default")




if __name__ == "__main__":
    unittest.main()
