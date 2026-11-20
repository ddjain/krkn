#!/usr/bin/env python3

"""
Test suite for pod network outage plugin

This test suite covers the get_test_pods function in pod_network_outage_plugin
using mocks to avoid needing actual Kubernetes infrastructure.

Test Coverage:
- get_test_pods with exclude_label filters
- get_test_pods with pod_name taking precedence over label filters
- get_test_pods with both pod_name and exclude_label

IMPORTANT: These tests use mocking and do NOT require any Kubernetes cluster.
All Kubernetes API calls are mocked via unittest.mock.

Usage:
    python -m unittest tests/test_pod_network_outage.py -v

    # Run with coverage
    python -m coverage run -a -m unittest tests/test_pod_network_outage.py -v

Assisted By: Claude Code
"""

import unittest
from unittest.mock import MagicMock

from krkn.scenario_plugins.native.pod_network_outage.pod_network_outage_plugin import (
    get_test_pods,
)


class TestPodNetworkOutage(unittest.TestCase):
    def test_list_pods_with_exclude_label(self):
        """Test that get_test_pods passes exclude_label to kubecli.list_pods"""
        mock_kubecli = MagicMock()
        mock_kubecli.list_pods.return_value = ["pod2", "pod3"]

        result = get_test_pods(None, "app=test", "test-namespace", mock_kubecli, "skip=true")

        mock_kubecli.list_pods.assert_called_once_with(
            label_selector="app=test",
            namespace="test-namespace",
            exclude_label="skip=true",
        )
        self.assertEqual(result, ["pod2", "pod3"])

    def test_list_pods_without_exclude_label(self):
        """Test that get_test_pods works without exclude_label"""
        mock_kubecli = MagicMock()
        mock_kubecli.list_pods.return_value = ["pod1", "pod2", "pod3"]

        result = get_test_pods(None, "app=test", "test-namespace", mock_kubecli)

        mock_kubecli.list_pods.assert_called_once_with(
            label_selector="app=test",
            namespace="test-namespace",
            exclude_label=None,
        )
        self.assertEqual(result, ["pod1", "pod2", "pod3"])

    def test_get_test_pods_with_exclude_label(self):
        """Test that get_test_pods passes exclude_label to list_pods correctly"""
        # Create mock kubecli
        mock_kubecli = MagicMock()
        mock_kubecli.list_pods.return_value = ["pod2", "pod3"]

        # Test get_test_pods with exclude_label
        result = get_test_pods(
            None, "app=test", "test-namespace", mock_kubecli, "skip=true"
        )

        # Verify list_pods was called with the correct parameters
        mock_kubecli.list_pods.assert_called_once_with(
            label_selector="app=test",
            namespace="test-namespace",
            exclude_label="skip=true",
        )

        # Verify the result
        self.assertEqual(result, ["pod2", "pod3"])

    def test_get_test_pods_with_pod_name_and_exclude_label(self):
        """Test that get_test_pods prioritizes pod_name over label filters"""
        # Create mock kubecli
        mock_kubecli = MagicMock()
        mock_kubecli.list_pods.return_value = ["pod1", "pod2", "pod3"]

        # Test get_test_pods with both pod_name and exclude_label
        # The pod_name should take precedence
        result = get_test_pods(
            "pod1", "app=test", "test-namespace", mock_kubecli, "skip=true"
        )

        # Verify list_pods was called with the correct parameters
        mock_kubecli.list_pods.assert_called_once_with(
            label_selector="app=test",
            namespace="test-namespace",
            exclude_label="skip=true",
        )

        # Verify the result contains only the specified pod
        self.assertEqual(result, ["pod1"])


if __name__ == "__main__":
    unittest.main()



# ---------------------------------------------------------------------------
# Extended coverage (appended): the tests below cover the remaining functions
# of pod_network_outage_plugin using the same mocking conventions as above
# (no real Kubernetes cluster required).
# ---------------------------------------------------------------------------
from types import SimpleNamespace
from unittest.mock import patch

from krkn.scenario_plugins.native.pod_network_outage import (
    pod_network_outage_plugin as pno,
)


def make_job_status(succeeded=None, failed=None, labels=None):
    """Build a fake job status API response."""
    return SimpleNamespace(
        status=SimpleNamespace(succeeded=succeeded, failed=failed),
        metadata=SimpleNamespace(
            labels=(
                labels
                if labels is not None
                else {"controller-uid": "controller-uid-1"}
            )
        ),
    )


def make_kubecli():
    """Build a mock KrknKubernetes configured for the happy paths."""
    kubecli = MagicMock()
    kubecli.api_client = object()
    kubecli.list_pods.return_value = []
    kubecli.read_pod.return_value = SimpleNamespace(
        status=SimpleNamespace(pod_ip="10.0.0.5", container_statuses=["ok"]),
        spec=SimpleNamespace(node_name="worker-0"),
        metadata=SimpleNamespace(labels={"app": "x"}),
    )
    kubecli.get_node.return_value = ["worker-0"]
    kubecli.create_pod.return_value = object()
    kubecli.exec_cmd_in_pod.return_value = "br0\n"
    kubecli.create_job.return_value = object()
    kubecli.get_job_status.return_value = make_job_status(succeeded=1)
    kubecli.delete_job.return_value = object()
    kubecli.get_pod_log.return_value = SimpleNamespace(data=b"job log")
    return kubecli


class TestGetTestPodsEdgeCases(unittest.TestCase):
    def test_get_test_pods_pod_name_not_in_listing_raises(self):
        kubecli = make_kubecli()
        kubecli.list_pods.return_value = ["pod1"]

        with self.assertRaisesRegex(
            Exception, "pod name not found in namespace"
        ):
            get_test_pods("podX", "app=test", "test-namespace", kubecli)

    def test_get_test_pods_empty_pod_name_returns_full_listing(self):
        kubecli = make_kubecli()
        kubecli.list_pods.return_value = ["pod1", "pod2"]

        result = get_test_pods("", "app=test", "test-namespace", kubecli)

        self.assertEqual(result, ["pod1", "pod2"])

    def test_get_test_pods_empty_listing_returns_empty_list(self):
        kubecli = make_kubecli()
        kubecli.list_pods.return_value = []

        result = get_test_pods(None, "app=test", "test-namespace", kubecli)

        self.assertEqual(result, [])


class TestGetJobPods(unittest.TestCase):
    def test_get_job_pods_returns_first_pod_matching_controller_uid(self):
        kubecli = make_kubecli()
        kubecli.list_pods.return_value = ["jpod1", "jpod2"]
        api_response = SimpleNamespace(
            metadata=SimpleNamespace(labels={"controller-uid": "uid-1"})
        )

        result = pno.get_job_pods(kubecli, api_response)

        self.assertEqual(result, "jpod1")
        kubecli.list_pods.assert_called_once_with(
            label_selector="controller-uid=uid-1", namespace="default"
        )

    def test_get_job_pods_no_matching_pods_raises_index_error(self):
        kubecli = make_kubecli()
        kubecli.list_pods.return_value = []
        api_response = SimpleNamespace(
            metadata=SimpleNamespace(labels={"controller-uid": "uid-1"})
        )

        with self.assertRaises(IndexError):
            pno.get_job_pods(kubecli, api_response)

    def test_get_job_pods_missing_controller_uid_label_raises_key_error(self):
        kubecli = make_kubecli()
        api_response = SimpleNamespace(
            metadata=SimpleNamespace(labels={"other": "label"})
        )

        with self.assertRaises(KeyError):
            pno.get_job_pods(kubecli, api_response)


class TestDeleteJobs(unittest.TestCase):
    def test_delete_jobs_failed_job_collects_logs_and_deletes(self):
        kubecli = make_kubecli()
        kubecli.get_job_status.return_value = make_job_status(failed=1)
        kubecli.list_pods.return_value = ["jpod"]

        with self.assertLogs("root", level="ERROR") as logs:
            pno.delete_jobs(kubecli, ["job1"])

        kubecli.read_pod.assert_called_once_with(name="jpod", namespace="default")
        kubecli.get_pod_log.assert_called_once_with(
            name="jpod", namespace="default"
        )
        kubecli.delete_job.assert_called_once_with(
            name="job1", namespace="default"
        )
        self.assertIn("job log", "\n".join(logs.output))

    def test_delete_jobs_succeeded_job_skips_log_collection(self):
        kubecli = make_kubecli()
        kubecli.get_job_status.return_value = make_job_status(succeeded=1)

        pno.delete_jobs(kubecli, ["job1"])

        kubecli.get_pod_log.assert_not_called()
        kubecli.read_pod.assert_not_called()
        kubecli.delete_job.assert_called_once_with(
            name="job1", namespace="default"
        )

    def test_delete_jobs_status_api_error_still_deletes_job(self):
        kubecli = make_kubecli()
        kubecli.get_job_status.side_effect = Exception("api down")

        with self.assertLogs("root", level="WARNING") as logs:
            pno.delete_jobs(kubecli, ["job1"])

        kubecli.delete_job.assert_called_once_with(
            name="job1", namespace="default"
        )
        self.assertIn("api down", "\n".join(logs.output))

    def test_delete_jobs_none_status_still_deletes_job(self):
        kubecli = make_kubecli()
        kubecli.get_job_status.return_value = SimpleNamespace(status=None)

        pno.delete_jobs(kubecli, ["job1"])

        kubecli.delete_job.assert_called_once_with(
            name="job1", namespace="default"
        )

    def test_delete_jobs_string_log_data_still_deletes_job(self):
        kubecli = make_kubecli()
        kubecli.get_job_status.return_value = make_job_status(failed=1)
        kubecli.list_pods.return_value = ["jpod"]
        kubecli.get_pod_log.return_value = SimpleNamespace(data="not bytes")

        pno.delete_jobs(kubecli, ["job1"])

        kubecli.delete_job.assert_called_once_with(
            name="job1", namespace="default"
        )

    def test_delete_jobs_delete_failure_propagates(self):
        kubecli = make_kubecli()
        kubecli.delete_job.side_effect = RuntimeError("delete failed")

        with self.assertRaisesRegex(RuntimeError, "delete failed"):
            pno.delete_jobs(kubecli, ["job1"])

    def test_delete_jobs_empty_list_makes_no_calls(self):
        kubecli = make_kubecli()

        pno.delete_jobs(kubecli, [])

        kubecli.get_job_status.assert_not_called()
        kubecli.delete_job.assert_not_called()

    def test_delete_jobs_multiple_jobs_deleted_in_order(self):
        kubecli = make_kubecli()
        kubecli.get_job_status.return_value = make_job_status(succeeded=1)

        pno.delete_jobs(kubecli, ["job1", "job2"])

        self.assertEqual(
            [call.kwargs["name"] for call in kubecli.delete_job.call_args_list],
            ["job1", "job2"],
        )


class TestWaitForJob(unittest.TestCase):
    @patch.object(pno, "time")
    def test_wait_for_job_all_succeed_first_pass(self, mock_time):
        mock_time.time.side_effect = [0, 10]
        kubecli = make_kubecli()
        kubecli.get_job_status.return_value = make_job_status(succeeded=1)

        pno.wait_for_job(["job1"], kubecli, timeout=300)

        kubecli.get_job_status.assert_called_once_with(
            "job1", namespace="default"
        )
        mock_time.sleep.assert_called_once_with(5)

    @patch.object(pno, "time")
    def test_wait_for_job_pending_then_succeeds(self, mock_time):
        mock_time.time.side_effect = [0, 1, 2]
        kubecli = make_kubecli()
        kubecli.get_job_status.side_effect = [
            make_job_status(),
            make_job_status(succeeded=1),
        ]

        pno.wait_for_job(["job1"], kubecli, timeout=300)

        self.assertEqual(kubecli.get_job_status.call_count, 2)
        self.assertEqual(mock_time.sleep.call_count, 2)

    @patch.object(pno, "time")
    def test_wait_for_job_failed_job_counts_as_done(self, mock_time):
        mock_time.time.side_effect = [0, 10]
        kubecli = make_kubecli()
        kubecli.get_job_status.return_value = make_job_status(failed=1)

        pno.wait_for_job(["job1"], kubecli, timeout=300)

        kubecli.get_job_status.assert_called_once_with(
            "job1", namespace="default"
        )

    @patch.object(pno, "time")
    def test_wait_for_job_timeout_raises(self, mock_time):
        mock_time.time.side_effect = [0, 1000]
        kubecli = make_kubecli()
        kubecli.get_job_status.side_effect = Exception("poll error")

        with self.assertLogs("root", level="WARNING") as logs:
            with self.assertRaisesRegex(
                Exception, "Jobs did not complete within"
            ):
                pno.wait_for_job(["job1"], kubecli, timeout=300)

        self.assertIn("Exception in getting job status", "\n".join(logs.output))

    @patch.object(pno, "time")
    def test_wait_for_job_empty_list_returns_without_polling(self, mock_time):
        kubecli = make_kubecli()

        pno.wait_for_job([], kubecli, timeout=300)

        kubecli.get_job_status.assert_not_called()
        mock_time.sleep.assert_not_called()

    @patch.object(pno, "time")
    def test_wait_for_job_empties_caller_owned_list(self, mock_time):
        mock_time.time.side_effect = [0, 10]
        kubecli = make_kubecli()
        kubecli.get_job_status.return_value = make_job_status(succeeded=1)
        job_list = ["job1"]

        pno.wait_for_job(job_list, kubecli, timeout=300)

        self.assertEqual(job_list, [])

    @patch.object(pno, "time")
    def test_wait_for_job_staggered_completion_terminates(self, mock_time):
        mock_time.time.side_effect = [0, 1, 2, 3, 4]
        kubecli = make_kubecli()
        done = make_job_status(succeeded=1)
        pending = make_job_status()
        kubecli.get_job_status.side_effect = [done, pending, done, done]

        pno.wait_for_job(
            ["job-a", "job-b", "job-c"], kubecli, timeout=300
        )

        self.assertEqual(kubecli.get_job_status.call_count, 4)
        self.assertEqual(mock_time.sleep.call_count, 4)


class TestGetBridgeName(unittest.TestCase):
    @staticmethod
    def _make_cli(network_crd_name="Networks.Config.OpenShift.io"):
        cli = MagicMock()
        cli.list_custom_resource_definition.return_value.to_dict.return_value = {
            "items": [{"metadata": {"name": network_crd_name}}]
        }
        return cli

    @staticmethod
    def _make_custom_obj(network_type="OpenShiftSDN"):
        custom_obj = MagicMock()
        custom_obj.get_cluster_custom_object.return_value = {
            "spec": {"networkType": network_type}
        }
        return custom_obj

    def test_get_bridge_name_crd_absent_raises(self):
        cli = self._make_cli(network_crd_name="unrelated.crd.io")

        with self.assertRaisesRegex(
            Exception, "OpenShiftSDN or OVNKubernetes not found in cluster"
        ):
            pno.get_bridge_name(cli, self._make_custom_obj())

    def test_get_bridge_name_crd_match_is_case_insensitive(self):
        cli = self._make_cli(network_crd_name="Networks.Config.OpenShift.IO")

        result = pno.get_bridge_name(cli, self._make_custom_obj("OpenShiftSDN"))

        self.assertEqual(result, "br0")

    def test_get_bridge_name_openshift_sdn_returns_br0(self):
        custom_obj = self._make_custom_obj("OpenShiftSDN")

        result = pno.get_bridge_name(self._make_cli(), custom_obj)

        self.assertEqual(result, "br0")
        custom_obj.get_cluster_custom_object.assert_called_once_with(
            group="config.openshift.io",
            version="v1",
            name="cluster",
            plural="networks",
        )

    def test_get_bridge_name_ovn_kubernetes_returns_br_int(self):
        result = pno.get_bridge_name(
            self._make_cli(), self._make_custom_obj("OVNKubernetes")
        )

        self.assertEqual(result, "br-int")

    def test_get_bridge_name_unknown_network_type_raises(self):
        with self.assertRaisesRegex(Exception, "Calico"):
            pno.get_bridge_name(self._make_cli(), self._make_custom_obj("Calico"))

    def test_get_bridge_name_missing_network_type_raises_key_error(self):
        custom_obj = MagicMock()
        custom_obj.get_cluster_custom_object.return_value = {"spec": {}}

        with self.assertRaises(KeyError):
            pno.get_bridge_name(self._make_cli(), custom_obj)


def make_job_template(name="chaos-testjob"):
    """Build a mock jinja2 job template rendering a job with a name."""
    template = MagicMock()
    template.render.return_value = "metadata:\n  name: %s" % name
    return template


class TestApplyOutagePolicy(unittest.TestCase):
    @staticmethod
    def _policy_args(kubecli, node_dict=None, ports=None, direction="egress",
                     bridge_name="br0", duration="60"):
        return dict(
            node_dict=node_dict if node_dict is not None else {
                "worker-0": ["10.0.0.5"]
            },
            ports=ports if ports is not None else [80],
            job_template=make_job_template(),
            pod_template=MagicMock(),
            direction=direction,
            duration=duration,
            bridge_name=bridge_name,
            kubecli=kubecli,
            image="test-image",
        )

    @patch.object(pno, "random")
    @patch.object(pno, "check_cookie")
    def test_br0_default_uses_br0_and_table_0(self, mock_check, mock_random):
        mock_check.return_value = ["f1", "f2"]
        mock_random.randint.return_value = 500
        kubecli = make_kubecli()
        args = self._policy_args(kubecli)

        job_list = pno.apply_outage_policy(**args)

        cmd = args["job_template"].render.call_args.kwargs["cmd"]
        self.assertIn("add-flow br0", cmd)
        self.assertIn("table=0", cmd)
        self.assertEqual(job_list, ["chaos-testjob"])

    @patch.object(pno, "random")
    @patch.object(pno, "check_cookie")
    def test_br_int_uses_br_int_and_table_8(self, mock_check, mock_random):
        mock_check.return_value = ["f1", "f2"]
        mock_random.randint.return_value = 500
        args = self._policy_args(make_kubecli(), bridge_name="br-int")

        pno.apply_outage_policy(**args)

        cmd = args["job_template"].render.call_args.kwargs["cmd"]
        self.assertIn("add-flow br-int", cmd)
        self.assertIn("table=8", cmd)

    def test_direction_maps_to_nw_src_or_nw_dst(self):
        for direction, key, other in (
            ("egress", "nw_src", "nw_dst"),
            ("ingress", "nw_dst", "nw_src"),
        ):
            with self.subTest(direction=direction):
                with patch.object(
                    pno, "check_cookie", return_value=["f1", "f2"]
                ), patch.object(
                    pno, "random", **{"randint.return_value": 500}
                ):
                    args = self._policy_args(make_kubecli(), direction=direction)
                    pno.apply_outage_policy(**args)
                    cmd = args["job_template"].render.call_args.kwargs["cmd"]
                self.assertIn("%s=10.0.0.5" % key, cmd)
                self.assertNotIn(other, cmd)

    @patch.object(pno, "random")
    @patch.object(pno, "check_cookie")
    def test_ports_generate_tcp_and_udp_rules_per_port(self, mock_check, mock_random):
        mock_check.return_value = ["f1", "f2"]
        mock_random.randint.return_value = 500
        args = self._policy_args(make_kubecli(), ports=[80, 443])

        pno.apply_outage_policy(**args)

        cmd = args["job_template"].render.call_args.kwargs["cmd"]
        self.assertEqual(cmd.count("tp_dst=80"), 2)
        self.assertEqual(cmd.count("tp_dst=443"), 2)
        self.assertEqual(cmd.count("tcp,"), 2)
        self.assertEqual(cmd.count("udp,"), 2)

    @patch.object(pno, "random")
    @patch.object(pno, "check_cookie")
    def test_empty_ports_generate_single_ip_drop_rule(self, mock_check, mock_random):
        mock_check.return_value = ["f1", "f2"]
        mock_random.randint.return_value = 500
        args = self._policy_args(make_kubecli(), ports=[])

        pno.apply_outage_policy(**args)

        cmd = args["job_template"].render.call_args.kwargs["cmd"]
        self.assertIn("ip,nw_src=10.0.0.5,actions=drop", cmd)
        self.assertNotIn("tcp,", cmd)
        self.assertNotIn("udp,", cmd)

    @patch.object(pno, "random")
    @patch.object(pno, "check_cookie")
    def test_multiple_ips_in_single_command(self, mock_check, mock_random):
        mock_check.return_value = ["f1", "f2"]
        mock_random.randint.return_value = 500
        args = self._policy_args(
            make_kubecli(), node_dict={"worker-0": ["10.0.0.5", "10.0.0.6"]}
        )

        pno.apply_outage_policy(**args)

        cmd = args["job_template"].render.call_args.kwargs["cmd"]
        self.assertIn("nw_src=10.0.0.5", cmd)
        self.assertIn("nw_src=10.0.0.6", cmd)

    @patch.object(pno, "random")
    @patch.object(pno, "check_cookie")
    def test_command_envelope_sleep_and_cleanup(self, mock_check, mock_random):
        mock_check.return_value = ["f1", "f2"]
        mock_random.randint.return_value = 500
        args = self._policy_args(make_kubecli(), duration="60")

        pno.apply_outage_policy(**args)

        cmd = args["job_template"].render.call_args.kwargs["cmd"]
        self.assertTrue(cmd.startswith("sleep 30;"))
        self.assertIn("sleep 60;", cmd)
        self.assertTrue(cmd.endswith("del-flows br0 cookie=500/-1"))

    @patch.object(pno, "random")
    @patch.object(pno, "check_cookie")
    def test_cookie_collision_with_existing_flows_redraws(self, mock_check, mock_random):
        mock_check.side_effect = [
            ["f1", "f2", "f3"],
            ["f1"],
        ]
        mock_random.randint.side_effect = [500, 700, 900]
        args = self._policy_args(make_kubecli())

        pno.apply_outage_policy(**args)

        cmd = args["job_template"].render.call_args.kwargs["cmd"]
        self.assertIn("cookie=700", cmd)
        self.assertNotIn("cookie=500", cmd)
        self.assertEqual(mock_check.call_count, 2)

    @patch.object(pno, "random")
    @patch.object(pno, "check_cookie")
    def test_cookie_reused_across_nodes_redraws(self, mock_check, mock_random):
        mock_check.return_value = ["f1", "f2"]
        mock_random.randint.side_effect = [500, 111, 500, 900, 222]
        args = self._policy_args(
            make_kubecli(),
            node_dict={"worker-0": ["10.0.0.5"], "worker-1": ["10.0.0.6"]},
        )
        args["job_template"].render.side_effect = [
            "metadata:\n  name: chaos-a",
            "metadata:\n  name: chaos-b",
        ]

        job_list = pno.apply_outage_policy(**args)

        first_cmd = args["job_template"].render.call_args_list[0].kwargs["cmd"]
        second_cmd = args["job_template"].render.call_args_list[1].kwargs["cmd"]
        self.assertIn("cookie=500", first_cmd)
        self.assertIn("cookie=900", second_cmd)
        self.assertNotIn("cookie=500", second_cmd)
        self.assertEqual(job_list, ["chaos-a", "chaos-b"])

    @patch.object(pno, "random")
    @patch.object(pno, "check_cookie")
    def test_two_nodes_render_distinct_nodename_and_cmd(self, mock_check, mock_random):
        mock_check.return_value = ["f1", "f2"]
        # 500: initial cookie; 1: node-0 jobname; 111: node-1 cookie redraw; 2: node-1 jobname
        mock_random.randint.side_effect = [500, 1, 111, 2]
        args = self._policy_args(
            make_kubecli(),
            node_dict={"worker-0": ["10.0.0.5"], "worker-1": ["10.0.0.6"]},
        )

        pno.apply_outage_policy(**args)

        calls = args["job_template"].render.call_args_list
        self.assertEqual(
            [call.kwargs["nodename"] for call in calls], ["worker-0", "worker-1"]
        )
        self.assertEqual(args["kubecli"].create_job.call_count, 2)
        first_cmd = calls[0].kwargs["cmd"]
        second_cmd = calls[1].kwargs["cmd"]
        self.assertIn("cookie=500", first_cmd)
        self.assertIn("nw_src=10.0.0.5", first_cmd)
        self.assertNotIn("nw_src=10.0.0.6", first_cmd)
        self.assertIn("cookie=111", second_cmd)
        self.assertNotIn("cookie=500", second_cmd)
        self.assertIn("nw_src=10.0.0.6", second_cmd)
        self.assertNotIn("nw_src=10.0.0.5", second_cmd)

    def test_empty_node_dict_returns_empty_list(self):
        with patch.object(pno, "check_cookie") as mock_check, patch.object(
            pno, "random"
        ) as mock_random:
            kubecli = make_kubecli()
            args = self._policy_args(kubecli, node_dict={})

            job_list = pno.apply_outage_policy(**args)

            self.assertEqual(job_list, [])
            kubecli.create_job.assert_not_called()
            mock_check.assert_not_called()
            mock_random.randint.assert_called_once()

    def test_create_job_returning_none_raises_after_prior_creates(self):
        with patch.object(pno, "check_cookie", return_value=["f1", "f2"]), patch.object(
            pno, "random", **{"randint.side_effect": [500, 111, 500, 900, 222]}
        ):
            kubecli = make_kubecli()
            kubecli.create_job.side_effect = [object(), None]
            args = self._policy_args(
                kubecli,
                node_dict={"worker-0": ["10.0.0.5"], "worker-1": ["10.0.0.6"]},
            )
            args["job_template"].render.side_effect = [
                "metadata:\n  name: chaos-a",
                "metadata:\n  name: chaos-b",
            ]

            with self.assertRaisesRegex(Exception, "Error creating job"):
                pno.apply_outage_policy(**args)

            self.assertEqual(kubecli.create_job.call_count, 2)


class TestApplyIngressPolicy(unittest.TestCase):
    @staticmethod
    def _ingress_args(kubecli, ips=None, node="worker-0", mod="latency"):
        return dict(
            mod=mod,
            node=node,
            ips=ips if ips is not None else ["10.0.0.5"],
            job_template=make_job_template(),
            pod_template=MagicMock(),
            network_params={"latency": "100ms"},
            duration="60",
            bridge_name="br0",
            kubecli=kubecli,
            test_execution="serial",
            image="test-image",
        )

    def _run(self, args):
        with patch.object(pno, "create_virtual_interfaces") as mock_create_vif, \
                patch.object(pno, "get_pod_interface", return_value="eth0"), \
                patch.object(pno, "get_ingress_cmd", return_value="CMD"):
            job_list = pno.apply_ingress_policy(**args)
        return job_list, mock_create_vif

    def test_ifb_count_equals_number_of_ips(self):
        args = self._ingress_args(make_kubecli(), ips=["ip1", "ip2"])

        _, mock_create_vif = self._run(args)

        mock_create_vif.assert_called_once_with(
            args["kubecli"], 2, "worker-0", args["pod_template"], "test-image"
        )

    def test_one_job_per_unique_ip(self):
        args = self._ingress_args(make_kubecli(), ips=["ip1", "ip1"])

        with patch.object(pno, "create_virtual_interfaces"), patch.object(
            pno, "get_pod_interface", return_value="eth0"
        ) as mock_get_inf, patch.object(
            pno, "get_ingress_cmd", return_value="CMD"
        ):
            job_list = pno.apply_ingress_policy(**args)

        self.assertEqual(job_list, ["chaos-testjob"])
        mock_get_inf.assert_called_once()
        args["kubecli"].create_job.assert_called_once()

    def test_command_and_jobname_passed_to_template(self):
        args = self._ingress_args(make_kubecli(), mod="latency")

        self._run(args)

        render_kwargs = args["job_template"].render.call_args.kwargs
        self.assertEqual(render_kwargs["cmd"], "CMD")
        self.assertEqual(render_kwargs["jobname"], "latency10.0.0.5")
        self.assertEqual(render_kwargs["nodename"], "worker-0")
        self.assertEqual(render_kwargs["image"], "test-image")

    def test_pod_ip_equal_to_node_exits_loop_after_single_job(self):
        args = self._ingress_args(
            make_kubecli(),
            ips=["worker-0", "10.0.0.5", "10.0.0.6"],
            node="worker-0",
        )

        # set() ordering is nondeterministic; keep input order so the node
        # name is hit first while other IPs remain unprocessed.
        with patch.object(pno, "set", lambda x: list(x), create=True):
            job_list, _ = self._run(args)

        self.assertEqual(job_list, ["chaos-testjob"])
        args["job_template"].render.assert_called_once()
        self.assertEqual(
            args["job_template"].render.call_args.kwargs["jobname"],
            "latencyworker-0",
        )

    def test_create_job_returning_none_raises(self):
        kubecli = make_kubecli()
        kubecli.create_job.return_value = None
        args = self._ingress_args(kubecli)

        with self.assertRaisesRegex(Exception, "Error creating job"):
            self._run(args)

    def test_image_propagated_to_interface_query(self):
        args = self._ingress_args(make_kubecli())

        with patch.object(
            pno, "get_pod_interface", return_value="eth0"
        ) as mock_get_inf, patch.object(
            pno, "create_virtual_interfaces"
        ), patch.object(pno, "get_ingress_cmd", return_value="CMD"):
            pno.apply_ingress_policy(**args)

        mock_get_inf.assert_called_once_with(
            "worker-0",
            "10.0.0.5",
            args["pod_template"],
            "br0",
            args["kubecli"],
            "test-image",
        )


class TestApplyNetPolicy(unittest.TestCase):
    @staticmethod
    def _net_args(kubecli, ips=None, node="worker-0", mod="loss"):
        return dict(
            mod=mod,
            node=node,
            ips=ips if ips is not None else ["10.0.0.5"],
            job_template=make_job_template(),
            pod_template=MagicMock(),
            network_params={"loss": "5"},
            duration="60",
            bridge_name="br0",
            kubecli=kubecli,
            test_execution="serial",
            image="test-image",
        )

    def test_no_virtual_interfaces_created_on_egress_path(self):
        args = self._net_args(make_kubecli())

        with patch.object(pno, "create_virtual_interfaces") as mock_create_vif, \
                patch.object(pno, "get_pod_interface", return_value="eth0"), \
                patch.object(pno, "get_egress_cmd", return_value="CMD"):
            pno.apply_net_policy(**args)

        mock_create_vif.assert_not_called()

    def test_one_job_per_unique_ip(self):
        args = self._net_args(make_kubecli(), ips=["ip1", "ip1", "ip2"])
        args["job_template"].render.side_effect = [
            "metadata:\n  name: chaos-a",
            "metadata:\n  name: chaos-b",
        ]

        with patch.object(pno, "get_pod_interface", return_value="eth0"), \
                patch.object(pno, "get_egress_cmd", return_value="CMD"):
            job_list = pno.apply_net_policy(**args)

        self.assertEqual(len(job_list), 2)
        jobnames = {
            call.kwargs["jobname"]
            for call in args["job_template"].render.call_args_list
        }
        self.assertEqual(jobnames, {"lossip1", "lossip2"})

    def test_create_job_returning_none_raises(self):
        kubecli = make_kubecli()
        kubecli.create_job.return_value = None
        args = self._net_args(kubecli)

        with patch.object(pno, "get_pod_interface", return_value="eth0"), \
                patch.object(pno, "get_egress_cmd", return_value="CMD"):
            with self.assertRaisesRegex(Exception, "Error creating job"):
                pno.apply_net_policy(**args)

    def test_image_propagated_to_interface_query(self):
        args = self._net_args(make_kubecli())

        with patch.object(
            pno, "get_pod_interface", return_value="eth0"
        ) as mock_get_inf, patch.object(
            pno, "get_egress_cmd", return_value="CMD"
        ):
            pno.apply_net_policy(**args)

        mock_get_inf.assert_called_once_with(
            "worker-0",
            "10.0.0.5",
            args["pod_template"],
            "br0",
            args["kubecli"],
            "test-image",
        )


class TestGetIngressCmd(unittest.TestCase):
    def test_serial_latency_full_command(self):
        cmd = pno.get_ingress_cmd(
            "serial", "eth0", "latency", 0, {"latency": "100ms"}, "60"
        )

        self.assertTrue(cmd.startswith("sleep 30;"))
        self.assertIn("tc qdisc add dev eth0 ingress ;", cmd)
        self.assertIn(
            "tc filter add dev eth0 ingress matchall action "
            "mirred egress redirect dev ifb0 ;",
            cmd,
        )
        self.assertIn("tc qdisc replace dev ifb0 root netem delay 100ms ;", cmd)
        self.assertIn("tc qdisc ls dev ifb0 ;", cmd)
        self.assertIn("sleep 60;", cmd)
        self.assertIn(" tc qdisc del dev ifb0 root ;", cmd)
        self.assertTrue(cmd.endswith("tc qdisc del dev eth0 ingress"))

    def test_serial_mod_mapping(self):
        for mod, value, expected in (
            ("loss", "10", "netem loss 10 ;"),
            ("bandwidth", "1gbit", "netem rate 1gbit ;"),
        ):
            with self.subTest(mod=mod):
                cmd = pno.get_ingress_cmd(
                    "serial", "eth0", mod, 0, {mod: value}, "60"
                )
                self.assertIn(expected, cmd)

    def test_parallel_appends_all_params_in_order(self):
        cmd = pno.get_ingress_cmd(
            "parallel",
            "eth0",
            "latency",
            1,
            {"latency": "1ms", "loss": "5", "bandwidth": "1gbit"},
            "60",
        )

        self.assertIn(
            "tc qdisc replace dev ifb1 root netem delay 1ms  loss 5  "
            "rate 1gbit ;",
            cmd,
        )

    # Documents current behaviour, not the desired contract.
    def test_parallel_with_empty_params_produces_bare_netem_pinned_quirk(self):
        cmd = pno.get_ingress_cmd("parallel", "eth0", "latency", 0, {}, "60")

        # Documents current behavior: an empty parameter dictionary renders
        # an invalid bare "netem ;" qdisc command.
        self.assertIn("root netem;", cmd)

    def test_unknown_mod_raises_key_error(self):
        with self.assertRaises(KeyError):
            pno.get_ingress_cmd(
                "serial", "eth0", "jitter", 0, {"jitter": "1"}, "60"
            )

    def test_unknown_parallel_param_raises_key_error(self):
        with self.assertRaises(KeyError):
            pno.get_ingress_cmd(
                "parallel", "eth0", "latency", 0, {"jitter": "1"}, "60"
            )

    def test_non_parallel_execution_strings_use_serial_branch(self):
        for execution in ("serial", "weird", ""):
            with self.subTest(execution=execution):
                cmd = pno.get_ingress_cmd(
                    execution, "eth0", "loss", 0, {"loss": "5"}, "60"
                )
                self.assertIn("netem loss 5 ;", cmd)

    def test_count_selects_ifb_device_consistently(self):
        cmd = pno.get_ingress_cmd(
            "serial", "eth0", "loss", 3, {"loss": "5"}, "60"
        )

        self.assertIn("redirect dev ifb3", cmd)
        self.assertIn("replace dev ifb3 root", cmd)
        self.assertIn("ls dev ifb3", cmd)
        self.assertIn("del dev ifb3 root", cmd)
        self.assertNotIn("ifb0", cmd)


class TestGetEgressCmd(unittest.TestCase):
    def test_serial_loss_full_command(self):
        cmd = pno.get_egress_cmd(
            "serial", "eth0", "loss", {"loss": "5"}, "60"
        )

        self.assertTrue(cmd.startswith("sleep 30;"))
        self.assertIn("tc qdisc replace dev eth0 root netem loss 5 ;", cmd)
        self.assertIn("tc qdisc ls dev eth0 ;", cmd)
        self.assertIn("sleep 60;", cmd)
        self.assertTrue(cmd.endswith("tc qdisc del dev eth0 root ;"))

    def test_serial_mod_mapping(self):
        for mod, value, expected in (
            ("latency", "100ms", "netem delay 100ms ;"),
            ("bandwidth", "1gbit", "netem rate 1gbit ;"),
        ):
            with self.subTest(mod=mod):
                cmd = pno.get_egress_cmd(
                    "serial", "eth0", mod, {mod: value}, "60"
                )
                self.assertIn(expected, cmd)

    def test_parallel_appends_all_params_in_order(self):
        cmd = pno.get_egress_cmd(
            "parallel",
            "eth0",
            "loss",
            {"latency": "1ms", "loss": "5", "bandwidth": "1gbit"},
            "60",
        )

        self.assertIn(
            "netem delay 1ms  loss 5  rate 1gbit ;",
            cmd,
        )

    def test_unknown_mod_raises_key_error(self):
        with self.assertRaises(KeyError):
            pno.get_egress_cmd("serial", "eth0", "jitter", {"jitter": "1"}, "60")

    def test_unknown_parallel_param_raises_key_error(self):
        with self.assertRaises(KeyError):
            pno.get_egress_cmd(
                "parallel", "eth0", "loss", {"jitter": "1"}, "60"
            )


POD_YAML = "metadata:\n  name: modtools-%s"


class TestIfbHelpers(unittest.TestCase):
    def test_create_ifb_runs_modprobe_and_link_set_per_interface(self):
        kubecli = make_kubecli()

        pno.create_ifb(kubecli, 2, "test-pod")

        self.assertEqual(kubecli.exec_cmd_in_pod.call_count, 3)
        first_call = kubecli.exec_cmd_in_pod.call_args_list[0]
        self.assertEqual(
            first_call.args[0],
            ["/host", "modprobe", "ifb", "numifbs=2"],
        )
        self.assertEqual(first_call.args[1], "test-pod")
        self.assertEqual(first_call.kwargs, {"base_command": "chroot"})
        self.assertEqual(
            kubecli.exec_cmd_in_pod.call_args_list[1].args[0],
            ["/host", "ip", "link", "set", "dev", "ifb0", "up"],
        )
        self.assertEqual(
            kubecli.exec_cmd_in_pod.call_args_list[2].args[0],
            ["/host", "ip", "link", "set", "dev", "ifb1", "up"],
        )

    def test_create_ifb_zero_interfaces_only_modprobes(self):
        kubecli = make_kubecli()

        pno.create_ifb(kubecli, 0, "test-pod")

        kubecli.exec_cmd_in_pod.assert_called_once()

    def test_delete_ifb_runs_modprobe_remove(self):
        kubecli = make_kubecli()

        pno.delete_ifb(kubecli, "test-pod")

        kubecli.exec_cmd_in_pod.assert_called_once_with(
            ["/host", "modprobe", "-r", "ifb"],
            "test-pod",
            "default",
            base_command="chroot",
        )


class TestVirtualInterfaces(unittest.TestCase):
    @patch.object(pno, "random")
    def test_create_virtual_interfaces_pod_lifecycle(self, mock_random):
        mock_random.randint.return_value = 7
        kubecli = make_kubecli()
        pod_template = MagicMock()
        pod_template.render.return_value = POD_YAML % 7

        pno.create_virtual_interfaces(kubecli, 2, "worker-0", pod_template, "img")

        pod_template.render.assert_called_once_with(
            regex_name="7", nodename="worker-0", image="img"
        )
        kubecli.create_pod.assert_called_once_with(
            {"metadata": {"name": "modtools-7"}}, "default", 300
        )
        # 1 modprobe + 2 ip link set commands issued through the helper pod
        self.assertEqual(kubecli.exec_cmd_in_pod.call_count, 3)
        kubecli.delete_pod.assert_called_once_with("modtools-7", "default")

    @patch.object(pno, "random")
    def test_delete_virtual_interfaces_per_node(self, mock_random):
        mock_random.randint.side_effect = [1, 2]
        kubecli = make_kubecli()
        pod_template = MagicMock()
        pod_template.render.side_effect = [
            POD_YAML % 1,
            POD_YAML % 2,
        ]

        pno.delete_virtual_interfaces(
            kubecli, ["node-1", "node-2"], pod_template, "img"
        )

        self.assertEqual(kubecli.create_pod.call_count, 2)
        self.assertEqual(kubecli.delete_pod.call_count, 2)
        self.assertEqual(
            [call.args[0] for call in kubecli.delete_pod.call_args_list],
            ["modtools-1", "modtools-2"],
        )
        modprobe_calls = [
            call
            for call in kubecli.exec_cmd_in_pod.call_args_list
            if call.args[0] == ["/host", "modprobe", "-r", "ifb"]
        ]
        self.assertEqual(len(modprobe_calls), 2)


class TestListBridges(unittest.TestCase):
    @patch.object(pno, "random")
    def test_list_bridges_splits_ovs_output(self, mock_random):
        mock_random.randint.return_value = 7
        kubecli = make_kubecli()
        kubecli.exec_cmd_in_pod.return_value = "br0\nbr-int\n"
        pod_template = MagicMock()
        pod_template.render.return_value = POD_YAML % 7

        bridges = pno.list_bridges("worker-0", pod_template, kubecli, "img")

        self.assertEqual(bridges, ["br0", "br-int", ""])
        kubecli.exec_cmd_in_pod.assert_called_once_with(
            ["/host", "ovs-vsctl", "list-br"],
            "modtools-7",
            "default",
            base_command="chroot",
        )
        kubecli.delete_pod.assert_called_once_with("modtools-7", "default")

    @patch.object(pno, "random")
    def test_list_bridges_empty_output_exits_and_cleans_up(self, mock_random):
        mock_random.randint.return_value = 7
        kubecli = make_kubecli()
        kubecli.exec_cmd_in_pod.return_value = ""
        pod_template = MagicMock()
        pod_template.render.return_value = POD_YAML % 7

        with self.assertRaises(SystemExit):
            pno.list_bridges("worker-0", pod_template, kubecli, "img")

        kubecli.delete_pod.assert_called_once_with("modtools-7", "default")


class TestCheckCookie(unittest.TestCase):
    @patch.object(pno, "random")
    def test_check_cookie_dumps_flows_matching_cookie(self, mock_random):
        mock_random.randint.return_value = 7
        kubecli = make_kubecli()
        kubecli.exec_cmd_in_pod.return_value = "flow1\nflow2\n"
        pod_template = MagicMock()
        pod_template.render.return_value = POD_YAML % 7

        flows = pno.check_cookie(
            "worker-0", pod_template, "br0", 500, kubecli, "img"
        )

        self.assertEqual(flows, ["flow1", "flow2", ""])
        kubecli.exec_cmd_in_pod.assert_called_once_with(
            [
                "chroot",
                "/host",
                "ovs-ofctl",
                "-O",
                "OpenFlow13",
                "dump-flows",
                "br0",
                "cookie=500/-1",
            ],
            "modtools-7",
            "default",
            base_command="chroot",
        )
        kubecli.delete_pod.assert_called_once_with("modtools-7", "default")

    @patch.object(pno, "random")
    def test_check_cookie_empty_output_exits_and_cleans_up(self, mock_random):
        mock_random.randint.return_value = 7
        kubecli = make_kubecli()
        kubecli.exec_cmd_in_pod.return_value = ""
        pod_template = MagicMock()
        pod_template.render.return_value = POD_YAML % 7

        with self.assertRaises(SystemExit):
            pno.check_cookie(
                "worker-0", pod_template, "br0", 500, kubecli, "img"
            )

        kubecli.delete_pod.assert_called_once_with("modtools-7", "default")


class TestGetPodInterface(unittest.TestCase):
    @patch.object(pno, "random")
    def test_get_pod_interface_br0_queries_external_ids_ip(self, mock_random):
        mock_random.randint.return_value = 7
        kubecli = make_kubecli()
        kubecli.exec_cmd_in_pod.return_value = "eth0"
        pod_template = MagicMock()
        pod_template.render.return_value = POD_YAML % 7

        inf = pno.get_pod_interface(
            "worker-0", "10.0.0.5", pod_template, "br0", kubecli, "img"
        )

        self.assertEqual(inf, "eth0")
        kubecli.exec_cmd_in_pod.assert_called_once_with(
            [
                "/host",
                "ovs-vsctl",
                "--bare",
                "--columns=name",
                "find",
                "interface",
                "external-ids:ip=10.0.0.5",
            ],
            "modtools-7",
            "default",
            base_command="chroot",
        )
        kubecli.delete_pod.assert_called_once_with("modtools-7", "default")

    # Documents current behaviour, not the desired contract.
    @patch.object(pno, "random")
    def test_get_pod_interface_br_int_queries_ip_addresses_pinned_quirk(self, mock_random):
        mock_random.randint.return_value = 7
        kubecli = make_kubecli()
        kubecli.exec_cmd_in_pod.return_value = "eth0"
        pod_template = MagicMock()
        pod_template.render.return_value = POD_YAML % 7

        pno.get_pod_interface(
            "worker-0", "10.0.0.5", pod_template, "br-int", kubecli, "img"
        )

        find_cmd = kubecli.exec_cmd_in_pod.call_args.args[0]
        self.assertIn("external-ids:ip_addresses=10.0.0.5/23", find_cmd)

    @patch.object(pno, "random")
    def test_get_pod_interface_falls_back_to_ip_addr_show(self, mock_random):
        mock_random.randint.return_value = 7
        kubecli = make_kubecli()
        kubecli.exec_cmd_in_pod.side_effect = [
            "",
            "2: eth0    inet 10.0.0.5/24 scope global eth0",
        ]
        pod_template = MagicMock()
        pod_template.render.return_value = POD_YAML % 7

        inf = pno.get_pod_interface(
            "worker-0", "10.0.0.5", pod_template, "br0", kubecli, "img"
        )

        self.assertEqual(inf, "eth0")
        fallback_cmd = kubecli.exec_cmd_in_pod.call_args_list[1].args[0]
        self.assertEqual(fallback_cmd, ["/host", "ip", "addr", "show"])

    @patch.object(pno, "random")
    def test_get_pod_interface_fallback_no_match_returns_empty(self, mock_random):
        mock_random.randint.return_value = 7
        kubecli = make_kubecli()
        kubecli.exec_cmd_in_pod.side_effect = ["", "1: lo    inet 127.0.0.1/8"]
        pod_template = MagicMock()
        pod_template.render.return_value = POD_YAML % 7

        inf = pno.get_pod_interface(
            "worker-0", "10.0.0.5", pod_template, "br0", kubecli, "img"
        )

        self.assertEqual(inf, "")


class TestCheckBridgeInterface(unittest.TestCase):
    @patch.object(pno, "random")
    def test_check_bridge_interface_found_returns_true(self, mock_random):
        mock_random.randint.return_value = 7
        kubecli = make_kubecli()
        kubecli.exec_cmd_in_pod.return_value = "br0\n"
        pod_template = MagicMock()
        pod_template.render.return_value = POD_YAML % 7

        result = pno.check_bridge_interface(
            "worker-0", pod_template, "br0", kubecli, "img"
        )

        self.assertTrue(result)
        kubecli.get_node.assert_called_once_with("worker-0", None, 1)

    @patch.object(pno, "random")
    def test_check_bridge_interface_not_found_raises(self, mock_random):
        mock_random.randint.return_value = 7
        kubecli = make_kubecli()
        kubecli.exec_cmd_in_pod.return_value = "br1\n"
        pod_template = MagicMock()
        pod_template.render.return_value = POD_YAML % 7

        with self.assertRaisesRegex(
            Exception, "OVS bridge br0 not found on the node"
        ):
            pno.check_bridge_interface(
                "worker-0", pod_template, "br0", kubecli, "img"
            )


class TestPodOutageStep(unittest.TestCase):
    def _make_params(self, **overrides):
        params = pno.InputParams(
            namespace="test-ns", label_selector="app=x", **overrides
        )
        params.kubecli = make_kubecli()
        params.kubecli.list_pods.return_value = ["pod1"]
        return params

    @patch.object(pno, "delete_jobs")
    @patch.object(pno, "wait_for_job")
    @patch.object(pno, "apply_outage_policy")
    @patch.object(pno, "check_bridge_interface", return_value=True)
    @patch.object(pno, "get_bridge_name", return_value="br0")
    @patch.object(pno, "client")
    def test_pod_outage_success(
        self,
        mock_client,
        mock_get_bridge,
        mock_check_bridge,
        mock_apply,
        mock_wait,
        mock_delete,
    ):
        params = self._make_params()
        params.ingress_ports = [80]
        params.egress_ports = [443]
        mock_apply.side_effect = [["job1"], ["job2"]]

        output_id, output = pno.pod_outage("run-1", params)

        self.assertEqual(output_id, "success")
        self.assertEqual(output.test_pods, ["pod1"])
        self.assertEqual(output.direction, ["ingress", "egress"])
        self.assertEqual(output.ingress_ports, [80])
        self.assertEqual(output.egress_ports, [443])
        mock_client.ApiextensionsV1Api.assert_called_once_with(
            params.kubecli.api_client
        )
        mock_client.CustomObjectsApi.assert_called_once_with(
            params.kubecli.api_client
        )
        params.kubecli.read_pod.assert_called_once_with("pod1", "test-ns")
        self.assertEqual(mock_apply.call_count, 2)
        directions = [call.args[4] for call in mock_apply.call_args_list]
        self.assertEqual(directions, ["ingress", "egress"])
        ports = [call.args[1] for call in mock_apply.call_args_list]
        self.assertEqual(ports, [[80], [443]])
        mock_wait.assert_called_once_with(
            ["job1", "job2"], params.kubecli, params.test_duration + 300
        )
        mock_delete.assert_called_once_with(params.kubecli, ["job1", "job2"])

    @patch.object(pno, "delete_jobs")
    @patch.object(pno, "get_bridge_name")
    @patch.object(pno, "client")
    def test_pod_outage_error_returns_error_output(
        self, mock_client, mock_get_bridge, mock_delete
    ):
        params = self._make_params()
        mock_get_bridge.side_effect = Exception("no CRD found")

        output_id, output = pno.pod_outage("run-1", params)

        self.assertEqual(output_id, "error")
        self.assertIn("no CRD found", output.error)
        mock_delete.assert_called_once_with(params.kubecli, [])

    @patch.object(pno, "delete_jobs")
    @patch.object(pno, "wait_for_job")
    @patch.object(pno, "apply_outage_policy")
    @patch.object(pno, "check_bridge_interface", return_value=True)
    @patch.object(pno, "get_bridge_name", return_value="br0")
    @patch.object(pno, "client")
    @patch.object(pno, "random")
    def test_pod_outage_trims_pods_to_instance_count(
        self,
        mock_random,
        mock_client,
        mock_get_bridge,
        mock_check_bridge,
        mock_apply,
        mock_wait,
        mock_delete,
    ):
        params = self._make_params(instance_count=1)
        params.kubecli.list_pods.return_value = ["pod1", "pod2", "pod3"]
        mock_random.randint.return_value = 0
        mock_apply.side_effect = [[], []]

        output_id, output = pno.pod_outage("run-1", params)

        self.assertEqual(output_id, "success")
        self.assertEqual(output.test_pods, ["pod3"])
        self.assertEqual(mock_random.randint.call_count, 2)


class TestPodEgressShapingStep(unittest.TestCase):
    def _make_params(self, **overrides):
        overrides.setdefault("network_params", {"latency": "50ms"})
        params = pno.EgressParams(
            namespace="test-ns",
            label_selector="app=x",
            **overrides,
        )
        params.kubecli = make_kubecli()
        params.kubecli.list_pods.return_value = ["pod1"]
        return params

    @patch.object(pno, "delete_jobs")
    @patch.object(pno, "time")
    @patch.object(pno, "wait_for_job")
    @patch.object(pno, "apply_net_policy")
    @patch.object(pno, "check_bridge_interface", return_value=True)
    @patch.object(pno, "get_bridge_name", return_value="br0")
    @patch.object(pno, "client")
    @patch.object(pno, "random")
    def test_parallel_success(
        self,
        mock_random,
        mock_client,
        mock_get_bridge,
        mock_check_bridge,
        mock_apply,
        mock_wait,
        mock_time,
        mock_delete,
    ):
        mock_time.time.return_value = 123456
        mock_random.randint.return_value = 0
        params = self._make_params(
            execution_type="parallel",
            kraken_config={"cerberus": "http://cerberus"},
            instance_count=1,
            image="custom-test-image",
        )
        params.kubecli.list_pods.return_value = ["pod1", "pod2", "pod3"]
        mock_apply.return_value = ["job1"]

        output_id, output = pno.pod_egress_shaping("run-1", params)

        self.assertEqual(output_id, "success")
        self.assertEqual(output.test_pods, ["pod3"])
        self.assertEqual(output.network_parameters, {"latency": "50ms"})
        self.assertEqual(output.execution_type, "parallel")
        mock_apply.assert_called_once()
        self.assertEqual(mock_apply.call_args.args[0], "latency")
        self.assertEqual(mock_apply.call_args.args[1], "worker-0")
        self.assertEqual(mock_apply.call_args.args[2], ["10.0.0.5"])
        self.assertEqual(mock_apply.call_args.args[9], "parallel")
        self.assertEqual(mock_apply.call_args.args[10], "custom-test-image")
        mock_wait.assert_called_once_with(
            ["job1"], params.kubecli, params.test_duration + 300
        )
        mock_time.sleep.assert_called_once_with(params.test_duration)
        mock_delete.assert_called_once_with(params.kubecli, ["job1"])

    @patch.object(pno, "delete_jobs")
    @patch.object(pno, "time")
    @patch.object(pno, "wait_for_job")
    @patch.object(pno, "apply_net_policy")
    @patch.object(pno, "check_bridge_interface", return_value=True)
    @patch.object(pno, "get_bridge_name", return_value="br0")
    @patch.object(pno, "client")
    def test_serial_waits_between_mods(
        self,
        mock_client,
        mock_get_bridge,
        mock_check_bridge,
        mock_apply,
        mock_wait,
        mock_time,
        mock_delete,
    ):
        mock_time.time.return_value = 123456
        params = self._make_params(
            execution_type="serial",
            network_params={"latency": "50ms", "loss": "5"},
        )
        mock_apply.side_effect = [["job1"], ["job2"]]

        output_id, output = pno.pod_egress_shaping("run-1", params)

        self.assertEqual(output_id, "success")
        self.assertEqual(output.execution_type, "serial")
        self.assertEqual(mock_apply.call_count, 2)
        mods_applied = [call.args[0] for call in mock_apply.call_args_list]
        self.assertEqual(mods_applied, ["latency", "loss"])
        self.assertEqual(mock_wait.call_count, 2)
        self.assertEqual(mock_time.sleep.call_count, 2)
        mock_delete.assert_called_once_with(params.kubecli, ["job1", "job2"])

    @patch.object(pno, "delete_jobs")
    @patch.object(pno, "get_bridge_name")
    @patch.object(pno, "client")
    def test_error_returns_error_output(
        self, mock_client, mock_get_bridge, mock_delete
    ):
        params = self._make_params()
        mock_get_bridge.side_effect = Exception("bridge lookup failed")

        output_id, output = pno.pod_egress_shaping("run-1", params)

        self.assertEqual(output_id, "error")
        self.assertIn("bridge lookup failed", output.error)
        mock_delete.assert_called_once_with(params.kubecli, [])


class TestPodIngressShapingStep(unittest.TestCase):
    def _make_params(self, **overrides):
        overrides.setdefault("network_params", {"latency": "50ms"})
        params = pno.IngressParams(
            namespace="test-ns",
            label_selector="app=x",
            **overrides,
        )
        params.kubecli = make_kubecli()
        params.kubecli.list_pods.return_value = ["pod1"]
        return params

    @patch.object(pno, "delete_jobs")
    @patch.object(pno, "delete_virtual_interfaces")
    @patch.object(pno, "time")
    @patch.object(pno, "wait_for_job")
    @patch.object(pno, "apply_ingress_policy")
    @patch.object(pno, "check_bridge_interface", return_value=True)
    @patch.object(pno, "get_bridge_name", return_value="br0")
    @patch.object(pno, "client")
    @patch.object(pno, "random")
    def test_parallel_success(
        self,
        mock_random,
        mock_client,
        mock_get_bridge,
        mock_check_bridge,
        mock_apply,
        mock_wait,
        mock_time,
        mock_delete_vif,
        mock_delete,
    ):
        mock_time.time.return_value = 123456
        mock_random.randint.return_value = 0
        params = self._make_params(
            execution_type="parallel",
            kraken_config={"cerberus": "http://cerberus"},
            instance_count=1,
        )
        params.kubecli.list_pods.return_value = ["pod1", "pod2", "pod3"]
        mock_apply.return_value = ["job1"]

        output_id, output = pno.pod_ingress_shaping("run-1", params)

        self.assertEqual(output_id, "success")
        self.assertEqual(output.test_pods, ["pod3"])
        self.assertEqual(output.network_parameters, {"latency": "50ms"})
        self.assertEqual(output.execution_type, "parallel")
        mock_apply.assert_called_once()
        self.assertEqual(mock_apply.call_args.args[0], "latency")
        self.assertEqual(mock_apply.call_args.kwargs.get("image"), params.image)
        mock_wait.assert_called_once_with(
            ["job1"], params.kubecli, params.test_duration + 300
        )
        mock_time.sleep.assert_called_once_with(params.test_duration)
        mock_delete_vif.assert_called_once()
        self.assertIs(mock_delete_vif.call_args.args[0], params.kubecli)
        self.assertEqual(set(mock_delete_vif.call_args.args[1]), {"worker-0"})
        self.assertEqual(mock_delete_vif.call_args.args[3], params.image)
        mock_delete.assert_called_once_with(params.kubecli, ["job1"])

    @patch.object(pno, "delete_jobs")
    @patch.object(pno, "delete_virtual_interfaces")
    @patch.object(pno, "time")
    @patch.object(pno, "wait_for_job")
    @patch.object(pno, "apply_ingress_policy")
    @patch.object(pno, "check_bridge_interface", return_value=True)
    @patch.object(pno, "get_bridge_name", return_value="br0")
    @patch.object(pno, "client")
    def test_serial_waits_between_mods(
        self,
        mock_client,
        mock_get_bridge,
        mock_check_bridge,
        mock_apply,
        mock_wait,
        mock_time,
        mock_delete_vif,
        mock_delete,
    ):
        mock_time.time.return_value = 123456
        params = self._make_params(
            execution_type="serial",
            network_params={"latency": "50ms", "loss": "5"},
        )
        mock_apply.side_effect = [["job1"], ["job2"]]

        output_id, output = pno.pod_ingress_shaping("run-1", params)

        self.assertEqual(output_id, "success")
        self.assertEqual(output.execution_type, "serial")
        self.assertEqual(mock_apply.call_count, 2)
        self.assertEqual(mock_wait.call_count, 2)
        self.assertEqual(mock_time.sleep.call_count, 2)
        mock_delete_vif.assert_called_once()
        mock_delete.assert_called_once_with(params.kubecli, ["job1", "job2"])

    @patch.object(pno, "delete_jobs")
    @patch.object(pno, "delete_virtual_interfaces")
    @patch.object(pno, "get_bridge_name")
    @patch.object(pno, "client")
    def test_error_returns_error_output_and_cleans_up_ifbs(
        self, mock_client, mock_get_bridge, mock_delete_vif, mock_delete
    ):
        params = self._make_params()
        mock_get_bridge.side_effect = Exception("bridge lookup failed")

        output_id, output = pno.pod_ingress_shaping("run-1", params)

        self.assertEqual(output_id, "error")
        self.assertIn("bridge lookup failed", output.error)
        mock_delete_vif.assert_called_once()
        mock_delete.assert_called_once_with(params.kubecli, [])
