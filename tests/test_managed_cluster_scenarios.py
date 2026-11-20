#!/usr/bin/env python3

"""
Test suite for the Scenarios class in
krkn/scenario_plugins/managed_cluster/scenarios.py

Covers the nine managedcluster scenario methods: ManifestWork body
construction (jinja/yaml are exercised for real), status waits,
per-iteration cleanup, error propagation and the delegation between the
composite stop/start scenarios. Only the krkn-lib boundary
(KrknKubernetes) and time.sleep are mocked.

Usage:
    python -m coverage run -a -m pytest tests/test_managed_cluster_scenarios.py -v
"""

import unittest
from unittest.mock import ANY, Mock, call, patch

from krkn_lib.k8s import KrknKubernetes

from krkn.scenario_plugins.managed_cluster.scenarios import Scenarios

CLUSTER = "cluster-xyz"
TIMEOUT = 120

# kubectl command each scenario renders into the ManifestWork Job. The start
# joiner is '&' (see test_start_joins_scale_commands_sequentially), so shared
# comparisons go through joiner_free() instead of pinning the joiner.
START_ARGS = (
    "kubectl scale deployment.apps/klusterlet --replicas 3 && "
    "kubectl scale deployment.apps/klusterlet-registration-agent "
    "--replicas 1 -n open-cluster-management-agent"
)
STOP_ARGS = (
    "kubectl scale deployment.apps/klusterlet --replicas 0 && "
    "kubectl scale deployment.apps/klusterlet-registration-agent "
    "--replicas 0 -n open-cluster-management-agent"
)
START_KLUSTERLET_ARGS = "kubectl scale deployment.apps/klusterlet --replicas 3"
STOP_KLUSTERLET_ARGS = "kubectl scale deployment.apps/klusterlet --replicas 0"


def job_args(body):
    """
    Extract the kubectl command from the kubectl Job manifest of a rendered
    ManifestWork body
    """
    manifests = body["spec"]["workload"]["manifests"]
    (job,) = [m for m in manifests if m["kind"] == "Job"]
    return job["spec"]["template"]["spec"]["containers"][0]["args"][0]


def joiner_free(args):
    """
    Collapse the '&&' joiner into '&' so shared contracts do not pin which
    joiner a scenario uses; each scenario asserts its own joiner
    """
    return args.replace("&&", "&")


class ManagedClusterScenarioContractMixin:
    """
    Contract shared by managedcluster_start_scenario and
    managedcluster_stop_scenario: render manifestwork.j2, create the
    ManifestWork in the managedcluster namespace, wait for the expected
    status, and delete the ManifestWork on every iteration.
    """

    method_name = None      # Scenarios method under test
    expected_args = None    # kubectl command rendered into the Job
    expected_status = None  # status waited for after the injection

    def setUp(self):
        self.mock_kubecli = Mock(spec=KrknKubernetes)
        self.scenarios = Scenarios(self.mock_kubecli)
        self.method = getattr(self.scenarios, self.method_name)

    def run_scenario(self, instance_kill_count=1, managedcluster=CLUSTER, timeout=TIMEOUT):
        return self.method(instance_kill_count, managedcluster, timeout)

    def kube_call_names(self):
        return [c[0] for c in self.mock_kubecli.mock_calls]

    def test_creates_manifestwork_in_managedcluster_namespace(self):
        """
        Test the scenario creates the template ManifestWork in the
        managedcluster namespace with the expected scale command
        """
        self.run_scenario()

        self.assertEqual(self.mock_kubecli.create_manifestwork.call_count, 1)
        body, namespace = self.mock_kubecli.create_manifestwork.call_args.args
        self.assertEqual(namespace, CLUSTER)
        self.assertEqual(body["kind"], "ManifestWork")
        self.assertEqual(body["metadata"]["namespace"], CLUSTER)
        self.assertEqual(body["metadata"]["name"], "managedcluster-scenarios-template")
        self.assertEqual(joiner_free(job_args(body)), joiner_free(self.expected_args))
        self.assertEqual(self.mock_kubecli.delete_manifestwork.call_args.args, (CLUSTER,))

    def test_waits_for_expected_status_with_timeout(self):
        """
        Test the scenario waits for the expected managedcluster status with
        the timeout passed through unmodified
        """
        self.run_scenario(timeout=456)

        self.mock_kubecli.watch_managedcluster_status.assert_called_once_with(
            CLUSTER, self.expected_status, 456
        )

    def test_create_wait_delete_run_in_order_on_same_cluster(self):
        """
        Test create, status wait and delete all target the same
        managedcluster and run in that order (a wrong-target swap between
        create and delete namespaces would break this)
        """
        self.run_scenario()

        self.assertEqual(
            self.mock_kubecli.mock_calls,
            [
                call.create_manifestwork(ANY, CLUSTER),
                call.watch_managedcluster_status(CLUSTER, self.expected_status, TIMEOUT),
                call.delete_manifestwork(CLUSTER),
            ],
        )

    def test_two_iterations_interleave_create_and_delete(self):
        """
        Test the cleanup delete runs on every iteration, not once at the end
        """
        self.run_scenario(instance_kill_count=2)

        self.assertEqual(self.mock_kubecli.create_manifestwork.call_count, 2)
        self.assertEqual(self.mock_kubecli.delete_manifestwork.call_count, 2)
        self.assertEqual(
            self.kube_call_names(),
            [
                "create_manifestwork",
                "watch_managedcluster_status",
                "delete_manifestwork",
            ]
            * 2,
        )

    def test_zero_count_performs_no_kube_calls(self):
        """
        Test a zero instance_kill_count is a no-op returning None
        """
        self.assertIsNone(self.run_scenario(instance_kill_count=0))
        self.assertEqual(self.mock_kubecli.mock_calls, [])

    def test_negative_count_performs_no_kube_calls(self):
        """
        Test a negative instance_kill_count is a no-op (empty range)
        """
        self.assertIsNone(self.run_scenario(instance_kill_count=-1))
        self.assertEqual(self.mock_kubecli.mock_calls, [])

    def test_create_failure_still_deletes_and_reraises(self):
        """
        Test a create failure is re-raised after the finally cleanup
        """
        self.mock_kubecli.create_manifestwork.side_effect = RuntimeError("api error")

        with self.assertRaises(RuntimeError) as context:
            self.run_scenario()

        self.assertEqual(str(context.exception), "api error")
        self.mock_kubecli.delete_manifestwork.assert_called_once_with(CLUSTER)
        self.mock_kubecli.watch_managedcluster_status.assert_not_called()

    def test_wait_failure_still_deletes_and_reraises(self):
        """
        Test a status watch failure is re-raised after the finally cleanup
        """
        self.mock_kubecli.watch_managedcluster_status.side_effect = KeyError("conditions")

        with self.assertRaisesRegex(KeyError, "conditions"):
            self.run_scenario()

        self.assertEqual(self.mock_kubecli.create_manifestwork.call_count, 1)
        self.mock_kubecli.delete_manifestwork.assert_called_once_with(CLUSTER)

    def test_wait_timeout_raises(self):
        """
        Test a status watch timeout fails the scenario instead of being
        silently ignored
        """
        self.mock_kubecli.watch_managedcluster_status.return_value = False

        with self.assertRaises(Exception):
            self.run_scenario()

        self.mock_kubecli.delete_manifestwork.assert_called_once_with(CLUSTER)

    def test_create_returning_none_still_waits_and_deletes(self):
        """
        Characterization of suspected bug: a None create response is treated
        exactly like success. krkn-lib's create_manifestwork swallows
        ApiException and returns None, and the scenario never checks it
        (scenarios.py:55), so a failed create is indistinguishable from a
        successful one at this layer.
        """
        self.mock_kubecli.create_manifestwork.return_value = None

        self.run_scenario()

        self.mock_kubecli.watch_managedcluster_status.assert_called_once_with(
            CLUSTER, self.expected_status, TIMEOUT
        )
        self.mock_kubecli.delete_manifestwork.assert_called_once_with(CLUSTER)

    def test_delete_failure_propagates_after_successful_run(self):
        """
        Test a cleanup delete failure propagates to the caller uncaught
        """
        self.mock_kubecli.delete_manifestwork.side_effect = RuntimeError("delete failed")

        with self.assertRaises(RuntimeError) as context:
            self.run_scenario()

        self.assertEqual(str(context.exception), "delete failed")
        self.mock_kubecli.watch_managedcluster_status.assert_called_once()

    def test_delete_attempted_when_injection_fails(self):
        """
        Test the cleanup delete is still attempted when the status wait fails
        """
        self.mock_kubecli.watch_managedcluster_status.side_effect = ValueError("watch failed")

        with self.assertRaisesRegex(ValueError, "watch failed"):
            self.run_scenario()

        self.mock_kubecli.delete_manifestwork.assert_called_once_with(CLUSTER)

    def test_delete_failure_does_not_mask_injection_failure(self):
        """
        Test the finally delete failure does not replace the in-flight
        injection exception
        """
        self.mock_kubecli.watch_managedcluster_status.side_effect = ValueError("watch failed")
        self.mock_kubecli.delete_manifestwork.side_effect = RuntimeError("delete failed")

        try:
            self.run_scenario()
        except Exception as exc:
            self.assertIsInstance(exc, ValueError, "cleanup failure masked the injection failure")
        else:
            self.fail("expected the injection failure to propagate")

    @patch("krkn.scenario_plugins.managed_cluster.scenarios.yaml.safe_load")
    def test_failure_before_create_still_deletes(self, mock_safe_load):
        """
        Characterization of suspected bug: the finally cleanup issues a delete
        even when rendering fails before any ManifestWork is created, so a
        delete is sent for an object this call never created. Update this test
        if the cleanup becomes conditional on a successful create.
        """
        mock_safe_load.side_effect = ValueError("render failed")

        with self.assertRaisesRegex(ValueError, "render failed"):
            self.run_scenario()

        self.mock_kubecli.create_manifestwork.assert_not_called()
        self.mock_kubecli.delete_manifestwork.assert_called_once_with(CLUSTER)


class TestManagedClusterStartScenario(
    ManagedClusterScenarioContractMixin, unittest.TestCase
):
    """
    managedcluster_start_scenario scales klusterlet (3) and its
    registration agent (1) back up and waits for the cluster to become
    available
    """

    method_name = "managedcluster_start_scenario"
    expected_args = START_ARGS
    expected_status = "True"

    def test_scales_klusterlet_up_with_sequential_joiner(self):
        """
        Test the start command scales both deployments up and joins them
        with a sequential '&&'

        """
        self.run_scenario()

        rendered_args = job_args(self.mock_kubecli.create_manifestwork.call_args.args[0])
        self.assertIn("--replicas 3 && ", rendered_args)
        self.assertIn("--replicas 1 -n open-cluster-management-agent", rendered_args)

    def test_start_joins_scale_commands_sequentially(self):
        """
        Test the start command joins both scale commands with a sequential
        '&&', like the stop scenario, so the Job cannot exit before the
        klusterlet scale takes effect
        """
        self.run_scenario()

        rendered_args = job_args(self.mock_kubecli.create_manifestwork.call_args.args[0])
        self.assertIn("--replicas 3 && ", rendered_args)
        self.assertIn("--replicas 1 -n open-cluster-management-agent", rendered_args)


class TestManagedClusterStopScenario(
    ManagedClusterScenarioContractMixin, unittest.TestCase
):
    """
    managedcluster_stop_scenario scales klusterlet (0) and its
    registration agent (0) down and waits for the cluster to become
    unavailable
    """

    method_name = "managedcluster_stop_scenario"
    expected_args = STOP_ARGS
    expected_status = "Unknown"

    def test_scales_both_deployments_to_zero_with_sequential_joiner(self):
        """
        Test the stop command scales both deployments to zero and joins
        them with a sequential '&&'
        """
        self.run_scenario()

        rendered_args = job_args(self.mock_kubecli.create_manifestwork.call_args.args[0])
        self.assertIn("--replicas 0 && ", rendered_args)
        self.assertIn("--replicas 0 -n open-cluster-management-agent", rendered_args)


class KlusterletScenarioContractMixin:
    """
    Contract shared by start_klusterlet_scenario and
    stop_klusterlet_scenario: render manifestwork.j2, create the
    ManifestWork, sleep a hardcoded 30s (no status watch) and delete the
    ManifestWork on every iteration.
    """

    method_name = None      # Scenarios method under test
    expected_args = None    # kubectl command rendered into the Job

    def setUp(self):
        self.mock_kubecli = Mock(spec=KrknKubernetes)
        self.scenarios = Scenarios(self.mock_kubecli)
        self.method = getattr(self.scenarios, self.method_name)

    def run_scenario(self, instance_kill_count=1, managedcluster=CLUSTER, timeout=TIMEOUT):
        return self.method(instance_kill_count, managedcluster, timeout)

    @patch("time.sleep")
    def test_creates_manifestwork_with_single_scale_command(self, mock_sleep):
        """
        Test the scenario creates a ManifestWork in the managedcluster
        namespace containing only the klusterlet scale command
        """
        self.run_scenario()

        self.assertEqual(self.mock_kubecli.create_manifestwork.call_count, 1)
        body, namespace = self.mock_kubecli.create_manifestwork.call_args.args
        self.assertEqual(namespace, CLUSTER)
        self.assertEqual(body["kind"], "ManifestWork")
        self.assertEqual(body["metadata"]["namespace"], CLUSTER)
        self.assertEqual(job_args(body), self.expected_args)
        self.assertEqual(self.mock_kubecli.delete_manifestwork.call_args.args, (CLUSTER,))

    @patch("time.sleep")
    def test_sleeps_hardcoded_30s_before_status_watch(self, mock_sleep):
        """
        Test the wait is a hardcoded 30s sleep with no managedcluster
        status watch

        Characterization of suspected bug scenarios.py:135/165 vs 154/185:
        the timeout parameter is never used; the scenario sleeps a fixed
        30s instead of waiting for the klusterlet status (OCM issue 118).
        """
        # Characterization: the timeout is ignored; only the fixed 30s sleep is pinned
        self.run_scenario(timeout=999)

        mock_sleep.assert_called_once_with(30)
        self.mock_kubecli.watch_managedcluster_status.assert_called_once()

    @patch("time.sleep")
    def test_status_wait_honours_timeout(self, mock_sleep):
        """
        Test the scenario waits for managedcluster status with the timeout
        passed through unmodified
        """
        self.run_scenario(timeout=999)

        self.mock_kubecli.watch_managedcluster_status.assert_called_once()
        self.assertEqual(self.mock_kubecli.watch_managedcluster_status.call_args.args[2], 999)

    @patch("time.sleep")
    def test_two_iterations_interleave_create_and_delete(self, mock_sleep):
        """
        Test the cleanup delete runs on every iteration and each iteration
        sleeps 30s
        """
        self.run_scenario(instance_kill_count=2)

        self.assertEqual(self.mock_kubecli.create_manifestwork.call_count, 2)
        self.assertEqual(self.mock_kubecli.delete_manifestwork.call_count, 2)
        self.assertEqual(
            [c[0] for c in self.mock_kubecli.mock_calls],
            ["create_manifestwork", "watch_managedcluster_status", "delete_manifestwork"] * 2,
        )
        mock_sleep.assert_has_calls([call(30), call(30)])

    @patch("time.sleep")
    def test_zero_count_performs_no_kube_calls(self, mock_sleep):
        """
        Test a zero instance_kill_count is a no-op with no sleep
        """
        self.assertIsNone(self.run_scenario(instance_kill_count=0))
        self.assertEqual(self.mock_kubecli.mock_calls, [])
        mock_sleep.assert_not_called()

    @patch("time.sleep")
    def test_negative_count_performs_no_kube_calls(self, mock_sleep):
        """
        Test a negative instance_kill_count is a no-op with no sleep
        """
        self.assertIsNone(self.run_scenario(instance_kill_count=-1))
        self.assertEqual(self.mock_kubecli.mock_calls, [])
        mock_sleep.assert_not_called()

    @patch("time.sleep")
    def test_create_failure_deletes_reraises_and_skips_sleep(self, mock_sleep):
        """
        Test a create failure is re-raised after the finally cleanup and
        the hardcoded sleep is skipped (it runs after create inside the try)
        """
        self.mock_kubecli.create_manifestwork.side_effect = RuntimeError("api error")

        with self.assertRaises(RuntimeError) as context:
            self.run_scenario()

        self.assertEqual(str(context.exception), "api error")
        self.mock_kubecli.delete_manifestwork.assert_called_once_with(CLUSTER)
        mock_sleep.assert_not_called()

    @patch("time.sleep")
    def test_delete_failure_propagates_after_successful_run(self, mock_sleep):
        """
        Test a cleanup delete failure propagates to the caller uncaught
        """
        self.mock_kubecli.delete_manifestwork.side_effect = RuntimeError("delete failed")

        with self.assertRaises(RuntimeError) as context:
            self.run_scenario()

        self.assertEqual(str(context.exception), "delete failed")
        mock_sleep.assert_called_once_with(30)

    @patch("time.sleep")
    def test_delete_attempted_when_injection_fails(self, mock_sleep):
        """
        Test the cleanup delete is still attempted when create fails
        """
        self.mock_kubecli.create_manifestwork.side_effect = ValueError("create failed")

        with self.assertRaisesRegex(ValueError, "create failed"):
            self.run_scenario()

        self.mock_kubecli.delete_manifestwork.assert_called_once_with(CLUSTER)

    @patch("time.sleep")
    def test_delete_failure_does_not_mask_injection_failure(self, mock_sleep):
        """
        Test the finally delete failure does not replace the in-flight
        create exception
        """
        self.mock_kubecli.create_manifestwork.side_effect = ValueError("create failed")
        self.mock_kubecli.delete_manifestwork.side_effect = RuntimeError("delete failed")

        try:
            self.run_scenario()
        except Exception as exc:
            self.assertIsInstance(exc, ValueError, "cleanup failure masked the injection failure")
        else:
            self.fail("expected the injection failure to propagate")

    @patch("krkn.scenario_plugins.managed_cluster.scenarios.yaml.safe_load")
    def test_failure_before_create_still_deletes(self, mock_safe_load):
        """
        Characterization of suspected bug: the finally cleanup issues a delete
        even when rendering fails before any ManifestWork is created, so a
        delete is sent for an object this call never created. Update this test
        if the cleanup becomes conditional on a successful create.
        """
        mock_safe_load.side_effect = ValueError("render failed")

        with self.assertRaisesRegex(ValueError, "render failed"):
            self.run_scenario()

        self.mock_kubecli.create_manifestwork.assert_not_called()
        self.mock_kubecli.delete_manifestwork.assert_called_once_with(CLUSTER)


class TestStartKlusterletScenario(KlusterletScenarioContractMixin, unittest.TestCase):
    """
    start_klusterlet_scenario scales only the klusterlet deployment back up
    """

    method_name = "start_klusterlet_scenario"
    expected_args = START_KLUSTERLET_ARGS


class TestStopKlusterletScenario(KlusterletScenarioContractMixin, unittest.TestCase):
    """
    stop_klusterlet_scenario scales only the klusterlet deployment down
    """

    method_name = "stop_klusterlet_scenario"
    expected_args = STOP_KLUSTERLET_ARGS


class TestManagedClusterStopStartScenario(unittest.TestCase):
    """
    managedcluster_stop_start_scenario delegates to the stop scenario,
    sleeps 10s, then delegates to the start scenario
    """

    def setUp(self):
        self.mock_kubecli = Mock(spec=KrknKubernetes)
        self.scenarios = Scenarios(self.mock_kubecli)

    @patch("time.sleep")
    def test_runs_stop_then_start_with_pause_between(self, mock_sleep):
        """
        Test the stop half runs to completion, then a 10s pause, then the
        start half, all on the same managedcluster
        """
        manager = Mock()
        manager.attach_mock(self.mock_kubecli.create_manifestwork, "create")
        manager.attach_mock(self.mock_kubecli.delete_manifestwork, "delete")
        manager.attach_mock(self.mock_kubecli.watch_managedcluster_status, "watch")
        manager.attach_mock(mock_sleep, "sleep")

        self.scenarios.managedcluster_stop_start_scenario(1, CLUSTER, TIMEOUT)

        self.assertEqual(
            manager.mock_calls,
            [
                call.create(ANY, CLUSTER),
                call.watch(CLUSTER, "Unknown", TIMEOUT),
                call.delete(CLUSTER),
                call.sleep(10),
                call.create(ANY, CLUSTER),
                call.watch(CLUSTER, "True", TIMEOUT),
                call.delete(CLUSTER),
            ],
        )

    @patch("time.sleep")
    def test_forwards_count_and_timeout_to_both_halves(self, mock_sleep):
        """
        Test both halves receive the same instance_kill_count and timeout
        """
        self.scenarios.managedcluster_stop_start_scenario(3, CLUSTER, 77)

        self.assertEqual(self.mock_kubecli.create_manifestwork.call_count, 6)
        self.assertEqual(self.mock_kubecli.delete_manifestwork.call_count, 6)
        watch_calls = self.mock_kubecli.watch_managedcluster_status.call_args_list
        self.assertEqual(len(watch_calls), 6)
        self.assertEqual([c.args[1] for c in watch_calls], ["Unknown"] * 3 + ["True"] * 3)
        for watch_call in watch_calls:
            self.assertEqual(watch_call.args[0], CLUSTER)
            self.assertEqual(watch_call.args[2], 77)
        mock_sleep.assert_called_once_with(10)

    @patch("time.sleep")
    def test_stop_failure_skips_pause_and_start(self, mock_sleep):
        """
        Test a stop-half failure propagates without running the pause or
        the start half; the cluster is left stopped (no rollback)
        """

        def fail_on_unavailable(managedcluster, status, timeout):
            if status == "Unknown":
                raise RuntimeError("watch failed")
            return True

        self.mock_kubecli.watch_managedcluster_status.side_effect = fail_on_unavailable

        with self.assertRaises(RuntimeError) as context:
            self.scenarios.managedcluster_stop_start_scenario(1, CLUSTER, TIMEOUT)

        self.assertEqual(str(context.exception), "watch failed")
        self.assertEqual(self.mock_kubecli.create_manifestwork.call_count, 1)
        self.assertEqual(self.mock_kubecli.delete_manifestwork.call_count, 1)
        mock_sleep.assert_not_called()

    @patch("time.sleep")
    def test_start_failure_propagates_after_stop_completed(self, mock_sleep):
        """
        Test a start-half failure propagates after the stop half completed
        and was cleaned up
        """
        self.mock_kubecli.create_manifestwork.side_effect = [
            Mock(name="stop manifestwork"),
            RuntimeError("start create failed"),
        ]

        with self.assertRaises(RuntimeError) as context:
            self.scenarios.managedcluster_stop_start_scenario(1, CLUSTER, TIMEOUT)

        self.assertEqual(str(context.exception), "start create failed")
        self.assertEqual(self.mock_kubecli.create_manifestwork.call_count, 2)
        self.assertEqual(self.mock_kubecli.delete_manifestwork.call_count, 2)
        mock_sleep.assert_called_once_with(10)


class TestStopStartKlusterletScenario(unittest.TestCase):
    """
    stop_start_klusterlet_scenario delegates to stop_klusterlet_scenario,
    sleeps 10s, then delegates to start_klusterlet_scenario
    """

    def setUp(self):
        self.mock_kubecli = Mock(spec=KrknKubernetes)
        self.scenarios = Scenarios(self.mock_kubecli)

    @patch("time.sleep")
    def test_runs_stop_then_start_with_pause_between(self, mock_sleep):
        """
        Test the stop half runs to completion (30s wait + cleanup), then a
        10s pause, then the start half, with no status watch at all
        """
        manager = Mock()
        manager.attach_mock(self.mock_kubecli.create_manifestwork, "create")
        manager.attach_mock(self.mock_kubecli.delete_manifestwork, "delete")
        manager.attach_mock(mock_sleep, "sleep")

        self.scenarios.stop_start_klusterlet_scenario(1, CLUSTER, TIMEOUT)

        self.assertEqual(
            manager.mock_calls,
            [
                call.create(ANY, CLUSTER),
                call.sleep(30),
                call.delete(CLUSTER),
                call.sleep(10),
                call.create(ANY, CLUSTER),
                call.sleep(30),
                call.delete(CLUSTER),
            ],
        )
        self.assertEqual(self.mock_kubecli.watch_managedcluster_status.call_count, 2)

    @patch("time.sleep")
    def test_forwards_count_and_timeout_to_both_halves(self, mock_sleep):
        """
        Test both halves receive the same instance_kill_count (the timeout
        is unused by the klusterlet halves)
        """
        # Characterization: the timeout (88) is ignored by the klusterlet halves; see the xfail below
        self.scenarios.stop_start_klusterlet_scenario(2, CLUSTER, 88)

        self.assertEqual(self.mock_kubecli.create_manifestwork.call_count, 4)
        self.assertEqual(self.mock_kubecli.delete_manifestwork.call_count, 4)
        self.assertEqual(
            [c.args for c in mock_sleep.call_args_list],
            [(30,), (30,), (10,), (30,), (30,)],
        )

    @patch("time.sleep")
    def test_stop_start_klusterlet_honours_timeout(self, mock_sleep):
        """
        Test both klusterlet halves wait on managedcluster status with the
        timeout passed through unmodified
        """
        self.scenarios.stop_start_klusterlet_scenario(1, CLUSTER, 88)

        self.mock_kubecli.watch_managedcluster_status.assert_called_with(CLUSTER, ANY, 88)

    @patch("time.sleep")
    def test_stop_failure_skips_pause_and_start(self, mock_sleep):
        """
        Test a stop-half failure propagates without running the pause or
        the start half
        """
        self.mock_kubecli.create_manifestwork.side_effect = RuntimeError("api error")

        with self.assertRaises(RuntimeError) as context:
            self.scenarios.stop_start_klusterlet_scenario(1, CLUSTER, TIMEOUT)

        self.assertEqual(str(context.exception), "api error")
        self.assertEqual(self.mock_kubecli.create_manifestwork.call_count, 1)
        self.assertEqual(self.mock_kubecli.delete_manifestwork.call_count, 1)
        mock_sleep.assert_not_called()


class TestNotImplementedScenarios(unittest.TestCase):
    """
    termination, reboot and crash are documented no-op scenarios
    """

    NOT_IMPLEMENTED_METHODS = (
        "managedcluster_termination_scenario",
        "managedcluster_reboot_scenario",
        "managedcluster_crash_scenario",
    )

    def setUp(self):
        self.mock_kubecli = Mock(spec=KrknKubernetes)
        self.scenarios = Scenarios(self.mock_kubecli)

    def test_not_implemented_scenarios_are_guaranteed_noops(self):
        """
        Test the scenarios perform no kube calls whatsoever and return
        None regardless of count and timeout (guards the not-implemented
        stubs against regressing into a destructive path)
        """
        for method_name in self.NOT_IMPLEMENTED_METHODS:
            with self.subTest(scenario=method_name):
                self.mock_kubecli.reset_mock()

                result = getattr(self.scenarios, method_name)(99, CLUSTER, 1)

                self.assertIsNone(result)
                self.assertEqual(self.mock_kubecli.mock_calls, [])

    def test_not_implemented_scenarios_log_that_no_action_is_taken(self):
        """
        Test each not-implemented scenario logs why nothing happens
        """
        for method_name in self.NOT_IMPLEMENTED_METHODS:
            with self.subTest(scenario=method_name):
                with self.assertLogs(level="INFO") as captured:
                    getattr(self.scenarios, method_name)(1, CLUSTER, TIMEOUT)

                self.assertIn("not implemented", "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()