#!/usr/bin/env python3
"""
Test suite for the Object State Health Check Plugin.

Usage:
    python -m unittest tests/test_object_state_health_check_plugin.py -v
"""

import logging
import queue
import sys
import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from krkn.health_checks.object_state_health_check_plugin import ObjectStateHealthCheckPlugin


class TestObjectStateHealthCheckPlugin(unittest.TestCase):

    def setUp(self):
        self.mock_krkn_lib = MagicMock()
        self.plugin = ObjectStateHealthCheckPlugin(
            iterations=1,
            krkn_lib=self.mock_krkn_lib
        )

    def test_get_health_check_types(self):
        """Plugin returns expected health check types."""
        types = self.plugin.get_health_check_types()
        self.assertIn("object_state_health_check", types)
        self.assertIn("k8s_object_health_check", types)

    def test_get_config_key(self):
        """Plugin returns correct config key."""
        self.assertEqual(self.plugin.get_config_key(), "object_state_checks")

    def test_increment_iterations(self):
        """increment_iterations increases the counter."""
        initial = self.plugin.current_iterations
        self.plugin.increment_iterations()
        self.assertEqual(self.plugin.current_iterations, initial + 1)

    def test_run_once_no_config(self):
        """run_once with no config returns passed."""
        result = self.plugin.run_once({})
        self.assertTrue(result["passed"])
        self.assertEqual(len(result["failures"]), 0)

    def test_run_once_blank_run_during_skips(self):
        """Blank run_during values skip the object state check."""
        for run_during in ("", "   ", []):
            with self.subTest(run_during=run_during):
                result = self.plugin.run_once({"run_during": run_during, "config": [{"namespace": "default"}]})

                self.assertTrue(result["passed"])
                self.assertEqual(result["details"], {})
                self.mock_krkn_lib.list_pods.assert_not_called()

    def test_run_once_blank_namespace_skips(self):
        """Checks without a namespace are skipped without failing."""
        result = self.plugin.run_once({
            "config": [{
                "name": "missing-namespace",
                "kind": "Pod",
                "namespace": "",
                "condition": {"type": "Ready", "status": "True"}
            }]
        })

        self.assertTrue(result["passed"])
        self.assertEqual(result["details"], {})
        self.mock_krkn_lib.list_pods.assert_not_called()

    def test_check_object_condition_passed(self):
        """_check_object_condition returns True when condition matches."""
        obj = {
            "kind": "Pod",
            "metadata": {"name": "test-pod", "namespace": "default"},
            "status": {
                "conditions": [
                    {"type": "Ready", "status": "True", "reason": "AllGood"}
                ]
            }
        }

        passed, message = self.plugin._check_object_condition(obj, "Ready", "True")
        self.assertTrue(passed)
        self.assertIn("test-pod", message)

    def test_check_object_condition_failed(self):
        """_check_object_condition returns False when condition doesn't match."""
        obj = {
            "kind": "Pod",
            "metadata": {"name": "test-pod", "namespace": "default"},
            "status": {
                "conditions": [
                    {"type": "Ready", "status": "False", "reason": "NotReady", "message": "Pod not ready"}
                ]
            }
        }

        passed, message = self.plugin._check_object_condition(obj, "Ready", "True")
        self.assertFalse(passed)
        self.assertIn("False", message)
        self.assertIn("NotReady", message)

    def test_check_object_condition_missing_type(self):
        """_check_object_condition returns False when condition type not found."""
        obj = {
            "kind": "Deployment",
            "metadata": {"name": "test-deploy", "namespace": "default"},
            "status": {
                "conditions": [
                    {"type": "Progressing", "status": "True"}
                ]
            }
        }

        passed, message = self.plugin._check_object_condition(obj, "Available", "True")
        self.assertFalse(passed)
        self.assertIn("does not have condition type", message)
        self.assertIn("Available", message)

    def test_check_object_condition_no_conditions(self):
        """_check_object_condition handles objects with no conditions."""
        obj = {
            "kind": "Pod",
            "metadata": {"name": "test-pod", "namespace": "default"},
            "status": {}
        }

        passed, message = self.plugin._check_object_condition(obj, "Ready", "True")
        self.assertFalse(passed)
        self.assertIn("has no conditions", message)

    def test_get_objects_pods(self):
        """_get_objects retrieves pods correctly when API returns dicts."""
        self.mock_krkn_lib.list_pods.return_value = [
            {"metadata": {"name": "pod-1"}},
            {"metadata": {"name": "pod-2"}},
        ]

        objects = self.plugin._get_objects("Pod", "default")
        self.assertEqual(len(objects), 2)
        self.mock_krkn_lib.list_pods.assert_called_once_with("default")

    def test_get_objects_pods_as_strings(self):
        """_get_objects handles when API returns strings (names) instead of dicts."""
        # Some krkn_lib methods return strings
        self.mock_krkn_lib.list_pods.return_value = ["pod-1", "pod-2"]

        # Mock get_object_by_name to return full pod objects
        pod_1 = {
            "kind": "Pod",
            "metadata": {"name": "pod-1", "namespace": "default"},
            "status": {"conditions": [{"type": "Ready", "status": "True"}]}
        }
        pod_2 = {
            "kind": "Pod",
            "metadata": {"name": "pod-2", "namespace": "default"},
            "status": {"conditions": [{"type": "Ready", "status": "True"}]}
        }

        self.mock_krkn_lib.get_object_by_name.side_effect = [pod_1, pod_2]

        objects = self.plugin._get_objects("Pod", "default")
        self.assertEqual(len(objects), 2)
        self.assertEqual(objects[0]["metadata"]["name"], "pod-1")
        self.assertEqual(objects[1]["metadata"]["name"], "pod-2")

    def test_get_objects_with_label_selector(self):
        """_get_objects uses label selector for pods."""
        self.mock_krkn_lib.list_pods.return_value = [
            {"metadata": {"name": "pod-1"}},
        ]

        objects = self.plugin._get_objects(
            "Pod", "default", label_selector="app=test"
        )
        self.assertEqual(len(objects), 1)
        self.mock_krkn_lib.list_pods.assert_called_once_with("default", "app=test")

    def test_get_objects_with_name_pattern(self):
        """_get_objects filters by name pattern."""
        self.mock_krkn_lib.list_pods.return_value = [
            {"metadata": {"name": "etcd-1"}},
            {"metadata": {"name": "etcd-2"}},
            {"metadata": {"name": "nginx-1"}},
        ]

        objects = self.plugin._get_objects("Pod", "default", object_name="etcd-.*")
        self.assertEqual(len(objects), 2)
        self.assertEqual(objects[0]["metadata"]["name"], "etcd-1")
        self.assertEqual(objects[1]["metadata"]["name"], "etcd-2")

    def test_get_objects_deployments(self):
        """_get_objects retrieves deployments."""
        self.mock_krkn_lib.list_deployments.return_value = [
            {"metadata": {"name": "deploy-1"}},
        ]

        objects = self.plugin._get_objects("Deployment", "default")
        self.assertEqual(len(objects), 1)
        self.mock_krkn_lib.list_deployments.assert_called_once_with("default")

    def test_check_single_config_no_kind(self):
        """_check_single_config fails when no kind specified."""
        config = {"name": "test-check"}
        result = self.plugin._check_single_config(config)

        self.assertFalse(result["passed"])
        self.assertIn("No 'kind' specified", result["message"])

    def test_check_single_config_no_objects_found(self):
        """_check_single_config fails when no objects match."""
        self.mock_krkn_lib.list_pods.return_value = []

        config = {
            "name": "test-check",
            "kind": "Pod",
            "namespace": "default",
            "object_name": "nonexistent",
            "condition": {"type": "Ready", "status": "True"}
        }
        result = self.plugin._check_single_config(config)

        self.assertFalse(result["passed"])
        self.assertIn("No Pod objects found", result["message"])

    def test_check_single_config_success(self):
        """_check_single_config succeeds when all objects pass."""
        self.mock_krkn_lib.list_pods.return_value = [
            {
                "kind": "Pod",
                "metadata": {"name": "pod-1", "namespace": "default"},
                "status": {
                    "conditions": [{"type": "Ready", "status": "True"}]
                }
            }
        ]

        config = {
            "name": "pod-ready-check",
            "kind": "Pod",
            "namespace": "default",
            "object_name": "pod-.*",
            "condition": {"type": "Ready", "status": "True"}
        }
        result = self.plugin._check_single_config(config)

        self.assertTrue(result["passed"])
        self.assertEqual(result["objects_checked"], 1)

    def test_run_once_multiple_checks(self):
        """run_once handles multiple check configurations."""
        self.mock_krkn_lib.list_pods.return_value = [
            {
                "kind": "Pod",
                "metadata": {"name": "pod-1", "namespace": "default"},
                "status": {
                    "conditions": [{"type": "Ready", "status": "True"}]
                }
            }
        ]

        config = {
            "config": [
                {
                    "name": "check-1",
                    "kind": "Pod",
                    "namespace": "default",
                    "condition": {"type": "Ready", "status": "True"}
                },
                {
                    "name": "check-2",
                    "kind": "Pod",
                    "namespace": "default",
                    "condition": {"type": "Ready", "status": "True"}
                }
            ]
        }

        result = self.plugin.run_once(config)
        self.assertTrue(result["passed"])
        self.assertEqual(len(result["details"]), 2)
        self.assertIn("check-1", result["details"])
        self.assertIn("check-2", result["details"])

    def test_run_once_with_failures(self):
        """run_once reports failures correctly."""
        self.mock_krkn_lib.list_pods.return_value = [
            {
                "kind": "Pod",
                "metadata": {"name": "pod-1", "namespace": "default"},
                "status": {
                    "conditions": [{"type": "Ready", "status": "False"}]
                }
            }
        ]

        config = {
            "config": [
                {
                    "name": "failing-check",
                    "kind": "Pod",
                    "namespace": "default",
                    "condition": {"type": "Ready", "status": "True"}
                }
            ]
        }

        result = self.plugin.run_once(config)
        self.assertFalse(result["passed"])
        self.assertEqual(len(result["failures"]), 1)
        self.assertEqual(result["failures"][0]["check_name"], "failing-check")

    def test_multiple_objects_all_must_pass(self):
        """When multiple objects match, ALL must pass for check to pass."""
        # Setup: 3 pods matching pattern, one is not ready
        self.mock_krkn_lib.list_pods.return_value = [
            {
                "kind": "Pod",
                "metadata": {"name": "etcd-0", "namespace": "kube-system"},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]}
            },
            {
                "kind": "Pod",
                "metadata": {"name": "etcd-1", "namespace": "kube-system"},
                "status": {"conditions": [{"type": "Ready", "status": "False", "reason": "NotReady"}]}
            },
            {
                "kind": "Pod",
                "metadata": {"name": "etcd-2", "namespace": "kube-system"},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]}
            }
        ]

        config = {
            "config": [
                {
                    "name": "etcd-check",
                    "kind": "Pod",
                    "object_name": "etcd-.*",
                    "namespace": "kube-system",
                    "condition": {"type": "Ready", "status": "True"}
                }
            ]
        }

        result = self.plugin.run_once(config)

        # Check should FAIL because ONE pod (etcd-1) is not ready
        self.assertFalse(result["passed"])
        self.assertEqual(len(result["failures"]), 1)

        # Verify details show 3 objects were checked
        self.assertEqual(result["details"]["etcd-check"]["objects_checked"], 3)
        self.assertFalse(result["details"]["etcd-check"]["passed"])

        # Message should contain only failed pod (etcd-1), not the passing ones (etcd-0, etcd-2)
        message = result["details"]["etcd-check"]["message"]
        self.assertIn("etcd-1", message)
        self.assertNotIn("etcd-0", message)  # This pod passed, should not be in failed objects list
        self.assertNotIn("etcd-2", message)  # This pod passed, should not be in failed objects list

    def test_multiple_objects_all_healthy(self):
        """When multiple objects match and all are healthy, check passes."""
        self.mock_krkn_lib.list_pods.return_value = [
            {
                "kind": "Pod",
                "metadata": {"name": "etcd-0", "namespace": "kube-system"},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]}
            },
            {
                "kind": "Pod",
                "metadata": {"name": "etcd-1", "namespace": "kube-system"},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]}
            },
            {
                "kind": "Pod",
                "metadata": {"name": "etcd-2", "namespace": "kube-system"},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]}
            }
        ]

        config = {
            "config": [
                {
                    "name": "etcd-check",
                    "kind": "Pod",
                    "object_name": "etcd-.*",
                    "namespace": "kube-system",
                    "condition": {"type": "Ready", "status": "True"}
                }
            ]
        }

        result = self.plugin.run_once(config)

        # Check should PASS because ALL pods are ready
        self.assertTrue(result["passed"])
        self.assertEqual(len(result["failures"]), 0)
        self.assertEqual(result["details"]["etcd-check"]["objects_checked"], 3)
        self.assertTrue(result["details"]["etcd-check"]["passed"])


def make_pod(name, namespace="default", ready=True):
    """Build a pod dict with a Ready condition."""
    status = "True" if ready else "False"
    return {
        "kind": "Pod",
        "metadata": {"name": name, "namespace": namespace},
        "status": {"conditions": [{"type": "Ready", "status": status}]},
    }


class TestObjectStateHealthCheckExtended(unittest.TestCase):
    """Additional coverage: is_configured, _get_objects, run_once telemetry, run_health_check."""

    def setUp(self):
        self.mock_krkn_lib = MagicMock()
        self.plugin = ObjectStateHealthCheckPlugin(
            iterations=1,
            krkn_lib=self.mock_krkn_lib
        )

    def _run_health_check_guarded(
        self, config, telemetry_queue, on_sleep=None, plugin=None
    ):
        """Run run_health_check in a guarded thread with module sleep patched.

        The patched sleep drives the loop: after each interval sleep the
        iteration counter is incremented (mirroring HealthCheckFactory), or
        on_sleep is invoked when custom behavior is needed (e.g. stop()).
        """
        plugin = plugin or self.plugin
        sleeps = []

        def drive(sec):
            sleeps.append(sec)
            if on_sleep is not None:
                on_sleep()
            else:
                plugin.increment_iterations()

        with patch(
            "krkn.health_checks.object_state_health_check_plugin.time.sleep",
            side_effect=drive,
        ):
            thread = threading.Thread(
                target=plugin.run_health_check,
                args=(config, telemetry_queue),
                daemon=True,
            )
            thread.start()
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive(), "run_health_check did not terminate")
        return sleeps

    # ------------------------------------------------------------------
    # is_configured
    # ------------------------------------------------------------------

    def test_is_configured_no_config_key_returns_false(self):
        self.assertFalse(self.plugin.is_configured({}))

    def test_is_configured_empty_checks_returns_false(self):
        self.assertFalse(self.plugin.is_configured({"config": []}))

    def test_is_configured_check_with_namespace_returns_true(self):
        self.assertTrue(
            self.plugin.is_configured({"config": [{"namespace": "default"}]})
        )

    def test_is_configured_whitespace_namespace_returns_false(self):
        self.assertFalse(
            self.plugin.is_configured({"config": [{"namespace": "   "}]})
        )

    def test_is_configured_non_dict_entries_return_false_without_raise(self):
        self.assertFalse(self.plugin.is_configured({"config": ["oops", None]}))

    def test_is_configured_namespace_none_is_not_configured(self):
        self.assertFalse(
            self.plugin.is_configured({"config": [{"namespace": None}]})
        )

    def test_is_configured_namespace_non_string_is_not_configured(self):
        self.assertFalse(self.plugin.is_configured({"config": [{"namespace": 123}]}))

    # ------------------------------------------------------------------
    # _get_object_by_name
    # ------------------------------------------------------------------

    def test_get_object_by_name_helper_raises_returns_none(self):
        self.mock_krkn_lib.get_object_by_name.side_effect = Exception("boom")
        result = self.plugin._get_object_by_name("Pod", "default", "pod-1")
        self.assertIsNone(result)
        self.mock_krkn_lib.get_object_by_name.assert_called_once_with(
            "Pod", "pod-1", "default"
        )

    # ------------------------------------------------------------------
    # _get_objects: kind dispatch
    # ------------------------------------------------------------------

    def test_get_objects_statefulset_daemonset_replicaset_dispatch(self):
        """Each workload kind dispatches to its krkn_lib list method with (namespace) only."""
        # NOTE: kube is an un-spec'd MagicMock; KrknKubernetes in the installed krkn-lib 7.0.3 has no list_deployments/list_statefulsets/list_daemonsets/list_replicasets/list_persistent_volume (requirements pin 7.0.4, unverified). Dispatch contract only.
        for kind, list_method in (
            ("StatefulSet", "list_statefulsets"),
            ("DaemonSet", "list_daemonsets"),
            ("ReplicaSet", "list_replicasets"),
        ):
            with self.subTest(kind=kind):
                getattr(self.mock_krkn_lib, list_method).return_value = [
                    {"metadata": {"name": "obj-1"}}
                ]
                objects = self.plugin._get_objects(kind, "ns-1")
                self.assertEqual(len(objects), 1)
                getattr(self.mock_krkn_lib, list_method).assert_called_once_with("ns-1")

    # suspected bug: _get_objects does not forward label_selector to list_deployments, so
    # Deployments are matched without the selector (was raises=AssertionError)
    @unittest.expectedFailure
    def test_get_objects_deployment_forwards_label_selector(self):
        """Deployment listing should receive the label selector, like Pod listing does."""
        self.mock_krkn_lib.list_deployments.return_value = [
            {"metadata": {"name": "deploy-1"}},
            {"metadata": {"name": "deploy-2"}},
        ]
        self.plugin._get_objects("Deployment", "ns-1", label_selector="app=x")
        self.mock_krkn_lib.list_deployments.assert_called_once_with("ns-1", "app=x")

    def test_get_objects_service_via_direct_api(self):
        """Service uses cli.list_namespaced_service with the label selector."""
        result = MagicMock()
        result.items = [{"metadata": {"name": "svc-1"}}]
        self.mock_krkn_lib.cli.list_namespaced_service.return_value = result

        objects = self.plugin._get_objects("Service", "ns-1", label_selector="app=x")
        self.assertEqual(objects, [{"metadata": {"name": "svc-1"}}])
        self.mock_krkn_lib.cli.list_namespaced_service.assert_called_once_with(
            "ns-1", label_selector="app=x"
        )

    def test_get_objects_service_result_without_items_returns_empty(self):
        result = object()
        self.mock_krkn_lib.cli.list_namespaced_service.return_value = result
        self.assertEqual(self.plugin._get_objects("Service", "ns-1"), [])

    def test_get_objects_pvc_job_cronjob_dispatch(self):
        """PVC/Job/CronJob forward label_selector and extract .items."""
        cases = (
            ("PersistentVolumeClaim", "cli", "list_namespaced_persistent_volume_claim"),
            ("Job", "batch_cli", "list_namespaced_job"),
            ("CronJob", "batch_cli", "list_namespaced_cron_job"),
        )
        for kind, client_attr, method in cases:
            with self.subTest(kind=kind):
                client = getattr(self.mock_krkn_lib, client_attr)
                result = MagicMock()
                result.items = [{"metadata": {"name": "obj-1"}}]
                getattr(client, method).return_value = result
                objects = self.plugin._get_objects(kind, "ns-1", label_selector="app=x")
                self.assertEqual(objects, [{"metadata": {"name": "obj-1"}}])
                getattr(client, method).assert_called_once_with("ns-1", label_selector="app=x")

    def test_get_objects_node_cluster_scoped(self):
        """Node is listed cluster-scoped; None selector passed positionally when unset."""
        self.mock_krkn_lib.list_nodes.return_value = [{"metadata": {"name": "node-1"}}]
        objects = self.plugin._get_objects("Node", "default")
        self.assertEqual(len(objects), 1)
        self.mock_krkn_lib.list_nodes.assert_called_once_with(None)

    def test_get_objects_node_with_label_selector(self):
        self.mock_krkn_lib.list_nodes.return_value = []
        self.plugin._get_objects("Node", "default", label_selector="role=master")
        self.mock_krkn_lib.list_nodes.assert_called_once_with("role=master")

    def test_get_objects_persistent_volume(self):
        result = MagicMock()
        result.items = [{"metadata": {"name": "pv-1"}}]
        self.mock_krkn_lib.cli.list_persistent_volume.return_value = result
        objects = self.plugin._get_objects(
            "PersistentVolume", "default", label_selector="app=x"
        )
        self.assertEqual(objects, [{"metadata": {"name": "pv-1"}}])
        self.mock_krkn_lib.cli.list_persistent_volume.assert_called_once_with(
            label_selector="app=x"
        )

    def test_get_objects_unknown_kind_returns_empty(self):
        self.assertEqual(self.plugin._get_objects("Foo", "ns-1"), [])

    def test_get_objects_kind_plural_goes_to_unknown_branch(self):
        """Only exact lowercase kind names match; 'pods' hits the unknown branch."""
        self.assertEqual(self.plugin._get_objects("pods", "ns-1"), [])
        self.mock_krkn_lib.list_pods.assert_not_called()

    def test_get_objects_kind_uppercase_matches_pod_branch(self):
        self.mock_krkn_lib.list_pods.return_value = [{"metadata": {"name": "p1"}}]
        objects = self.plugin._get_objects("POD", "ns-1")
        self.assertEqual(len(objects), 1)
        self.mock_krkn_lib.list_pods.assert_called_once_with("ns-1")

    # ------------------------------------------------------------------
    # _get_objects: error handling and object conversion
    # ------------------------------------------------------------------

    def test_get_objects_kind_none_returns_empty(self):
        """None kind raises AttributeError inside, swallowed to [] with an error log."""
        self.assertEqual(self.plugin._get_objects(None, "ns-1"), [])

    def test_get_objects_list_api_raises_returns_empty(self):
        self.mock_krkn_lib.list_pods.side_effect = Exception("api down")
        self.assertEqual(self.plugin._get_objects("Pod", "ns-1"), [])

    def test_get_objects_raw_none_returns_empty(self):
        self.mock_krkn_lib.list_pods.return_value = None
        self.assertEqual(self.plugin._get_objects("Pod", "ns-1"), [])

    def test_get_objects_k8s_api_objects_sanitized(self):
        """Kubernetes API objects are converted via sanitize_for_serialization."""
        self.mock_krkn_lib.list_pods.return_value = [
            SimpleNamespace(metadata=SimpleNamespace(name="p1")),
            SimpleNamespace(metadata=SimpleNamespace(name="p2")),
        ]
        self.mock_krkn_lib.api_client.sanitize_for_serialization.side_effect = (
            lambda o: {"kind": "Pod", "metadata": {"name": o.metadata.name}}
        )

        objects = self.plugin._get_objects("Pod", "ns-1")
        self.assertEqual(
            objects,
            [
                {"kind": "Pod", "metadata": {"name": "p1"}},
                {"kind": "Pod", "metadata": {"name": "p2"}},
            ],
        )
        self.assertEqual(
            self.mock_krkn_lib.api_client.sanitize_for_serialization.call_count, 2
        )

    def test_get_objects_sanitize_fails_vars_fallback(self):
        """When sanitize raises AttributeError, vars() is used as fallback."""
        self.mock_krkn_lib.list_pods.return_value = [
            SimpleNamespace(metadata=SimpleNamespace(name="p1"))
        ]
        self.mock_krkn_lib.api_client.sanitize_for_serialization.side_effect = (
            AttributeError("no api_client")
        )

        objects = self.plugin._get_objects("Pod", "ns-1")
        self.assertEqual(len(objects), 1)
        self.assertIsInstance(objects[0], dict)
        self.assertIn("metadata", objects[0])

    def test_get_objects_conversion_fully_fails_drops_object(self):
        """An object with no __dict__ and failing sanitize is dropped with a warning."""

        class Slotted:
            __slots__ = ("metadata",)

            def __init__(self):
                self.metadata = SimpleNamespace(name="p1")

        self.mock_krkn_lib.list_pods.return_value = [Slotted()]
        self.mock_krkn_lib.api_client.sanitize_for_serialization.side_effect = (
            AttributeError("no api_client")
        )

        self.assertEqual(self.plugin._get_objects("Pod", "ns-1"), [])

    def test_get_objects_mixed_string_and_dict_items(self):
        self.mock_krkn_lib.list_pods.return_value = [
            "pod-1",
            {"metadata": {"name": "pod-2"}},
        ]
        self.mock_krkn_lib.get_object_by_name.return_value = {
            "metadata": {"name": "pod-1"}
        }

        objects = self.plugin._get_objects("Pod", "ns-1")
        self.assertEqual(len(objects), 2)
        self.assertEqual(objects[0]["metadata"]["name"], "pod-1")
        self.assertEqual(objects[1]["metadata"]["name"], "pod-2")

    def test_get_objects_string_item_helper_returns_none_dropped(self):
        self.mock_krkn_lib.list_pods.return_value = ["pod-1"]
        self.mock_krkn_lib.get_object_by_name.return_value = None
        self.assertEqual(self.plugin._get_objects("Pod", "ns-1"), [])

    # ------------------------------------------------------------------
    # _get_objects: name filtering
    # ------------------------------------------------------------------

    def test_get_objects_non_dict_surviving_to_filter_skipped(self):
        """A non-dict conversion result is skipped (with warning) during filtering."""
        self.mock_krkn_lib.list_pods.return_value = [
            SimpleNamespace(metadata=SimpleNamespace(name="p1"))
        ]
        # sanitize returns a non-dict: it is appended, then skipped by the filter.
        self.mock_krkn_lib.api_client.sanitize_for_serialization.side_effect = (
            lambda o: "not-a-dict"
        )

        objects = self.plugin._get_objects("Pod", "ns-1", object_name="p.*")
        self.assertEqual(objects, [])

    def test_get_objects_invalid_regex_returns_empty(self):
        """An invalid object_name regex is swallowed and surfaces as no objects."""
        self.mock_krkn_lib.list_pods.return_value = [{"metadata": {"name": "p1"}}]
        self.assertEqual(
            self.plugin._get_objects("Pod", "ns-1", object_name="("), []
        )

    def test_get_objects_regex_is_prefix_match(self):
        """re.match semantics: pattern 'etcd' also matches 'etcdbad' (over-match pin)."""
        self.mock_krkn_lib.list_pods.return_value = [
            {"metadata": {"name": "etcd-1"}},
            {"metadata": {"name": "etcdbad"}},
        ]
        objects = self.plugin._get_objects("Pod", "ns-1", object_name="etcd")
        self.assertEqual(
            [o["metadata"]["name"] for o in objects], ["etcd-1", "etcdbad"]
        )

    def test_get_objects_anchored_regex_matches_exact(self):
        self.mock_krkn_lib.list_pods.return_value = [
            {"metadata": {"name": "etcd-1"}},
            {"metadata": {"name": "etcdbad"}},
        ]
        objects = self.plugin._get_objects("Pod", "ns-1", object_name="^etcd-1$")
        self.assertEqual([o["metadata"]["name"] for o in objects], ["etcd-1"])

    def test_get_objects_empty_object_name_no_filtering(self):
        """Empty object_name is falsy: all returned objects are kept."""
        self.mock_krkn_lib.list_pods.return_value = [
            {"metadata": {"name": "a"}},
            {"metadata": {"name": "b"}},
            {"metadata": {"name": "c"}},
        ]
        objects = self.plugin._get_objects("Pod", "ns-1", object_name="")
        self.assertEqual(len(objects), 3)

    def test_get_objects_none_item_appended(self):
        """A raw None item converted to None by sanitize must be dropped."""
        self.mock_krkn_lib.list_pods.return_value = [None]
        self.mock_krkn_lib.api_client.sanitize_for_serialization.side_effect = (
            lambda o: None
        )
        self.assertEqual(self.plugin._get_objects("Pod", "ns-1"), [])

    # ------------------------------------------------------------------
    # _check_object_condition: non-dict conversion
    # ------------------------------------------------------------------

    def test_check_object_condition_k8s_object_sanitized(self):
        obj = SimpleNamespace(
            metadata=SimpleNamespace(name="p1", namespace="ns-1"),
            status={"conditions": [{"type": "Ready", "status": "True"}]},
        )
        self.mock_krkn_lib.api_client.sanitize_for_serialization.side_effect = (
            lambda o: {
                "kind": "Pod",
                "metadata": {"name": "p1", "namespace": "ns-1"},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
            }
        )
        passed, message = self.plugin._check_object_condition(obj, "Ready", "True")
        self.assertTrue(passed)
        self.assertIn("p1", message)

    def test_check_object_condition_sanitize_fails_vars_fallback(self):
        class FakeObj:
            def __init__(self):
                self.kind = "Pod"
                self.metadata = {"name": "p1", "namespace": "default"}
                self.status = {"conditions": [{"type": "Ready", "status": "True"}]}

        self.mock_krkn_lib.api_client.sanitize_for_serialization.side_effect = (
            AttributeError("no api_client")
        )
        passed, message = self.plugin._check_object_condition(
            FakeObj(), "Ready", "True"
        )
        self.assertTrue(passed)
        self.assertIn("p1", message)

    def test_check_object_condition_both_conversions_fail(self):
        class Slotted:
            __slots__ = ()

        self.mock_krkn_lib.api_client.sanitize_for_serialization.side_effect = (
            AttributeError("no api_client")
        )
        passed, message = self.plugin._check_object_condition(
            Slotted(), "Ready", "True"
        )
        self.assertFalse(passed)
        self.assertIn("Failed to convert object to dict", message)

    def test_check_object_condition_sanitize_non_dict_should_fail_gracefully(self):
        self.mock_krkn_lib.api_client.sanitize_for_serialization.side_effect = (
            lambda o: "not-a-dict"
        )
        passed, message = self.plugin._check_object_condition(
            SimpleNamespace(), "Ready", "True"
        )
        self.assertFalse(passed)
        self.assertIn("Failed to convert object to dict", message)

    def test_check_object_condition_duplicate_types_first_match_wins(self):
        """Two conditions of the same type: the first one decides (order-dependent)."""
        # Characterization: first matching condition wins
        obj = {
            "kind": "Pod",
            "metadata": {"name": "p1", "namespace": "default"},
            "status": {
                "conditions": [
                    {"type": "Ready", "status": "False"},
                    {"type": "Ready", "status": "True"},
                ]
            },
        }
        passed, message = self.plugin._check_object_condition(obj, "Ready", "True")
        self.assertFalse(passed)
        self.assertIn("Ready=False", message)

    # ------------------------------------------------------------------
    # _check_single_config: edge cases
    # ------------------------------------------------------------------

    def test_check_single_config_blank_namespace_direct_call_skips(self):
        result = self.plugin._check_single_config({"name": "c", "kind": "Pod", "namespace": ""})
        self.assertTrue(result["passed"])
        self.assertTrue(result["skipped"])
        self.assertIn("Namespace is not specified", result["message"])

    def test_check_single_config_failed_object_without_namespace_bare_name(self):
        self.mock_krkn_lib.list_pods.return_value = [
            {
                "kind": "Pod",
                "metadata": {"name": "pod-1"},
                "status": {"conditions": [{"type": "Ready", "status": "False"}]},
            }
        ]
        result = self.plugin._check_single_config(
            {"name": "c", "kind": "Pod", "namespace": "default",
             "condition": {"type": "Ready", "status": "True"}}
        )
        self.assertFalse(result["passed"])
        self.assertIn("pod-1", result["message"])
        self.assertNotIn("/pod-1", result["message"])

    def test_check_single_config_condition_missing_defaults(self):
        self.mock_krkn_lib.list_pods.return_value = [
            {
                "kind": "Pod",
                "metadata": {"name": "pod-1", "namespace": "default"},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
            }
        ]
        result = self.plugin._check_single_config(
            {"name": "c", "kind": "Pod", "namespace": "default"}
        )
        self.assertTrue(result["passed"])

    def test_check_single_config_condition_status_lowercase_fails(self):
        """Condition status comparison is exact: 'true' != 'True'."""
        self.mock_krkn_lib.list_pods.return_value = [
            {
                "kind": "Pod",
                "metadata": {"name": "pod-1", "namespace": "default"},
                "status": {"conditions": [{"type": "Ready", "status": "true"}]},
            }
        ]
        result = self.plugin._check_single_config(
            {"name": "c", "kind": "Pod", "namespace": "default",
             "condition": {"type": "Ready", "status": "True"}}
        )
        self.assertFalse(result["passed"])

    def test_check_single_config_counts_checked_and_failed(self):
        self.mock_krkn_lib.list_pods.return_value = [
            make_pod("p1", ready=True),
            make_pod("p2", ready=False),
            make_pod("p3", ready=False),
        ]
        result = self.plugin._check_single_config(
            {"name": "c", "kind": "Pod", "namespace": "default",
             "condition": {"type": "Ready", "status": "True"}}
        )
        self.assertEqual(result["objects_checked"], 3)
        self.assertEqual(result["objects_failed"], 2)
        self.assertIn("p2", result["message"])
        self.assertIn("p3", result["message"])

    def test_check_single_config_non_dict_failed_object_not_named(self):
        """A failing non-dict object is counted but cannot be named in the message;
        the message must not claim 'All objects passed' when the check failed."""
        calls = []

        def stateful_sanitize(o):
            calls.append(o)
            if len(calls) == 1:
                # _get_objects stores the raw non-dict conversion result.
                return "not-a-dict"
            # _check_object_condition later converts it to a failing dict.
            return {
                "kind": "Pod",
                "metadata": {"name": "p1", "namespace": "default"},
                "status": {"conditions": [{"type": "Ready", "status": "False"}]},
            }

        self.mock_krkn_lib.api_client.sanitize_for_serialization.side_effect = (
            stateful_sanitize
        )
        self.mock_krkn_lib.list_pods.return_value = [
            SimpleNamespace(metadata=SimpleNamespace(name="p1"))
        ]

        result = self.plugin._check_single_config(
            {"name": "c", "kind": "Pod", "namespace": "default",
             "condition": {"type": "Ready", "status": "True"}}
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["objects_checked"], 1)
        self.assertEqual(result["objects_failed"], 1)
        self.assertNotEqual(result["message"], "All objects passed")

    def test_check_single_config_condition_none_should_default(self):
        self.mock_krkn_lib.list_pods.return_value = [make_pod("p1")]
        result = self.plugin._check_single_config(
            {"name": "c", "kind": "Pod", "namespace": "default", "condition": None}
        )
        self.assertTrue(result["passed"])

    # ------------------------------------------------------------------
    # run_once: telemetry
    # ------------------------------------------------------------------

    def _passing_config(self, **overrides):
        check = {
            "name": "pod-check",
            "kind": "Pod",
            "namespace": "default",
            "object_name": "etcd-.*",
            "condition": {"type": "Ready", "status": "True"},
        }
        check.update(overrides)
        return {"config": [check]}

    def test_run_once_telemetry_record_fields(self):
        self.mock_krkn_lib.list_pods.return_value = [make_pod("etcd-1")]
        tq = queue.Queue()
        result = self.plugin.run_once(self._passing_config(), tq)

        self.assertTrue(result["passed"])
        self.assertEqual(tq.qsize(), 1)
        rec = tq.get_nowait()
        self.assertEqual(rec.check_name, "pod-check")
        self.assertEqual(rec.kind, "Pod")
        self.assertEqual(rec.namespace, "default")
        self.assertEqual(rec.object_name, "etcd-.*")
        self.assertEqual(rec.condition_type, "Ready")
        self.assertEqual(rec.condition_status, "True")
        self.assertTrue(rec.passed)
        self.assertEqual(rec.objects_checked, 1)
        self.assertEqual(rec.objects_failed, 0)
        self.assertEqual(rec.duration, 0.0)
        self.assertEqual(rec.start_timestamp, rec.end_timestamp)

    def test_run_once_telemetry_phase_defaults_to_during(self):
        self.mock_krkn_lib.list_pods.return_value = [make_pod("etcd-1")]
        tq = queue.Queue()
        self.plugin.run_once(self._passing_config(), tq)
        self.assertEqual(tq.get_nowait().phase, "during")

    def test_run_once_telemetry_phase_passthrough(self):
        self.mock_krkn_lib.list_pods.return_value = [make_pod("etcd-1")]
        tq = queue.Queue()
        self.plugin.run_once(self._passing_config(), tq, phase="pre")
        self.assertEqual(tq.get_nowait().phase, "pre")

    def test_run_once_only_failures_passing_puts_nothing(self):
        self.mock_krkn_lib.list_pods.return_value = [make_pod("etcd-1")]
        tq = queue.Queue()
        result = self.plugin.run_once(
            dict(self._passing_config(), only_failures=True), tq
        )
        self.assertTrue(result["passed"])
        self.assertTrue(tq.empty())

    def test_run_once_only_failures_failing_puts_one_record(self):
        self.mock_krkn_lib.list_pods.return_value = [make_pod("etcd-1", ready=False)]
        tq = queue.Queue()
        result = self.plugin.run_once(
            dict(self._passing_config(), only_failures=True), tq
        )
        self.assertFalse(result["passed"])
        self.assertEqual(tq.qsize(), 1)
        rec = tq.get_nowait()
        self.assertFalse(rec.passed)
        self.assertEqual(rec.objects_failed, 1)

    def test_run_once_skipped_check_records_nothing(self):
        tq = queue.Queue()
        result = self.plugin.run_once(
            {"config": [{"name": "c", "kind": "Pod", "namespace": ""}]}, tq
        )
        self.assertTrue(result["passed"])
        self.assertTrue(tq.empty())

    def test_run_once_telemetry_multiple_checks_in_config_order(self):
        self.mock_krkn_lib.list_pods.return_value = [make_pod("p1")]
        tq = queue.Queue()
        config = {
            "config": [
                {"name": "check-a", "kind": "Pod", "namespace": "default"},
                {"name": "check-b", "kind": "Pod", "namespace": "default"},
            ]
        }
        self.plugin.run_once(config, tq)
        self.assertEqual(tq.qsize(), 2)
        self.assertEqual(tq.get_nowait().check_name, "check-a")
        self.assertEqual(tq.get_nowait().check_name, "check-b")

    def test_run_once_non_dict_config_entry_should_be_skipped(self):
        result = self.plugin.run_once({"config": ["oops"]})
        self.assertTrue(result["passed"])
        self.assertEqual(result["details"], {})

    # ------------------------------------------------------------------
    # run_health_check
    # ------------------------------------------------------------------

    def _monitoring_config(self, **config_overrides):
        check = {
            "name": "pod-check",
            "kind": "Pod",
            "namespace": "default",
            "condition": {"type": "Ready", "status": "True"},
        }
        cfg = {"config": [check]}
        cfg.update(config_overrides)
        return cfg

    def test_run_health_check_no_config_puts_nothing(self):
        tq = queue.Queue()
        for config in ({}, {"config": []}, None):
            with self.subTest(config=config):
                self._run_health_check_guarded(config, tq)
                self.assertTrue(tq.empty())

    def test_run_health_check_blank_run_during_puts_nothing(self):
        tq = queue.Queue()
        for run_during in ("  ", []):
            with self.subTest(run_during=run_during):
                self._run_health_check_guarded(
                    {"run_during": run_during,
                     "config": [{"namespace": "default"}]}, tq
                )
                self.assertTrue(tq.empty())
                self.mock_krkn_lib.list_pods.assert_not_called()

    def test_run_health_check_all_checks_filtered_puts_nothing(self):
        tq = queue.Queue()
        self._run_health_check_guarded(
            {"config": [{"name": "c", "kind": "Pod", "namespace": ""}]}, tq
        )
        self.assertTrue(tq.empty())
        self.mock_krkn_lib.list_pods.assert_not_called()

    def test_run_health_check_single_pass_puts_record_list(self):
        self.mock_krkn_lib.list_pods.return_value = [make_pod("p1")]
        tq = queue.Queue()
        self._run_health_check_guarded(self._monitoring_config(), tq)

        self.assertEqual(tq.qsize(), 1)
        records = tq.get_nowait()
        self.assertIsInstance(records, list)
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec.check_name, "pod-check")
        self.assertTrue(rec.passed)
        self.assertEqual(rec.phase, "during")
        self.assertIsInstance(rec.duration, float)
        self.assertGreaterEqual(rec.duration, 0.0)
        # exactly one loop pass for iterations=1
        self.assertEqual(self.mock_krkn_lib.list_pods.call_count, 1)

    def test_run_health_check_interval_custom_and_default(self):
        self.mock_krkn_lib.list_pods.return_value = [make_pod("p1")]
        tq = queue.Queue()
        sleeps = self._run_health_check_guarded(
            self._monitoring_config(interval=7), tq
        )
        self.assertEqual(sleeps, [7])

        self.mock_krkn_lib.list_pods.reset_mock()
        fresh_plugin = ObjectStateHealthCheckPlugin(
            iterations=1, krkn_lib=self.mock_krkn_lib
        )
        tq2 = queue.Queue()
        sleeps = self._run_health_check_guarded(
            self._monitoring_config(), tq2, plugin=fresh_plugin
        )
        self.assertEqual(sleeps, [5])

    def test_run_health_check_failing_pass_no_exit_keeps_ret_zero(self):
        self.mock_krkn_lib.list_pods.return_value = [make_pod("p1", ready=False)]
        tq = queue.Queue()
        self._run_health_check_guarded(self._monitoring_config(), tq)

        records = tq.get_nowait()
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0].passed)
        self.assertEqual(self.plugin.get_return_value(), 0)

    def test_run_health_check_status_unchanged_across_passes(self):
        """No status change between passes: no change record, only the final one."""
        plugin = ObjectStateHealthCheckPlugin(
            iterations=2, krkn_lib=self.mock_krkn_lib
        )
        self.mock_krkn_lib.list_pods.side_effect = [
            [make_pod("p1", ready=True)],
            [make_pod("p1", ready=True)],
        ]
        tq = queue.Queue()
        self._run_health_check_guarded(
            self._monitoring_config(), tq, plugin=plugin
        )
        records = tq.get_nowait()
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0].passed)
        self.assertEqual(self.mock_krkn_lib.list_pods.call_count, 2)

    def test_run_health_check_exit_on_failure_sets_ret_three(self):
        self.mock_krkn_lib.list_pods.return_value = [make_pod("p1", ready=False)]
        tq = queue.Queue()
        self._run_health_check_guarded(
            self._monitoring_config(exit_on_failure=True), tq
        )

        # Loop still completes normally and emits the final record.
        records = tq.get_nowait()
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0].passed)
        self.assertEqual(self.plugin.get_return_value(), 3)

    def test_run_health_check_exit_on_failure_preserves_prior_ret(self):
        self.mock_krkn_lib.list_pods.return_value = [make_pod("p1", ready=False)]
        self.plugin.set_return_value(5)
        tq = queue.Queue()
        self._run_health_check_guarded(
            self._monitoring_config(exit_on_failure=True), tq
        )
        self.assertEqual(self.plugin.get_return_value(), 5)

    def test_run_health_check_status_flip_pass_to_fail(self):
        plugin = ObjectStateHealthCheckPlugin(
            iterations=2, krkn_lib=self.mock_krkn_lib
        )
        self.mock_krkn_lib.list_pods.side_effect = [
            [make_pod("p1", ready=True)],
            [make_pod("p1", ready=False)],
        ]
        tq = queue.Queue()
        self._run_health_check_guarded(
            self._monitoring_config(), tq, plugin=plugin
        )

        records = tq.get_nowait()
        self.assertEqual(len(records), 2)
        change_record, final_record = records
        # Change record captures the previous (passing) state.
        self.assertTrue(change_record.passed)
        self.assertEqual(change_record.phase, "during")
        # Final record captures the failing state.
        self.assertFalse(final_record.passed)
        self.assertEqual(self.mock_krkn_lib.list_pods.call_count, 2)

    def test_run_health_check_status_flip_fail_to_pass(self):
        plugin = ObjectStateHealthCheckPlugin(
            iterations=2, krkn_lib=self.mock_krkn_lib
        )
        self.mock_krkn_lib.list_pods.side_effect = [
            [make_pod("p1", ready=False)],
            [make_pod("p1", ready=True)],
        ]
        tq = queue.Queue()
        self._run_health_check_guarded(
            self._monitoring_config(), tq, plugin=plugin
        )

        records = tq.get_nowait()
        self.assertEqual(len(records), 2)
        change_record, final_record = records
        self.assertFalse(change_record.passed)
        self.assertTrue(final_record.passed)

    def test_run_health_check_ret_set_only_once_across_flips(self):
        plugin = ObjectStateHealthCheckPlugin(
            iterations=3, krkn_lib=self.mock_krkn_lib
        )
        self.mock_krkn_lib.list_pods.side_effect = [
            [make_pod("p1", ready=False)],
            [make_pod("p1", ready=True)],
            [make_pod("p1", ready=False)],
        ]
        tq = queue.Queue()
        self._run_health_check_guarded(
            self._monitoring_config(exit_on_failure=True), tq, plugin=plugin
        )

        records = tq.get_nowait()
        # change(False) + change(True) + final(False)
        self.assertEqual(len(records), 3)
        self.assertEqual(plugin.get_return_value(), 3)

    def test_run_health_check_exit_on_failure_set_on_flip_to_failed(self):
        """ret_value is set when a check transitions to failed mid-run."""
        plugin = ObjectStateHealthCheckPlugin(
            iterations=2, krkn_lib=self.mock_krkn_lib
        )
        self.mock_krkn_lib.list_pods.side_effect = [
            [make_pod("p1", ready=True)],
            [make_pod("p1", ready=False)],
        ]
        tq = queue.Queue()
        self._run_health_check_guarded(
            self._monitoring_config(exit_on_failure=True), tq, plugin=plugin
        )
        records = tq.get_nowait()
        self.assertEqual(len(records), 2)
        self.assertTrue(records[0].passed)
        self.assertFalse(records[1].passed)
        self.assertEqual(plugin.get_return_value(), 3)

    def test_run_health_check_only_failures_all_passing_puts_empty_list(self):
        self.mock_krkn_lib.list_pods.return_value = [make_pod("p1")]
        tq = queue.Queue()
        self._run_health_check_guarded(
            self._monitoring_config(only_failures=True), tq
        )
        self.assertEqual(tq.qsize(), 1)
        self.assertEqual(tq.get_nowait(), [])

    def test_run_health_check_only_failures_flip_filters_change_record(self):
        plugin = ObjectStateHealthCheckPlugin(
            iterations=2, krkn_lib=self.mock_krkn_lib
        )
        self.mock_krkn_lib.list_pods.side_effect = [
            [make_pod("p1", ready=True)],
            [make_pod("p1", ready=False)],
        ]
        tq = queue.Queue()
        self._run_health_check_guarded(
            self._monitoring_config(only_failures=True), tq, plugin=plugin
        )

        # The passing change record is filtered out; only the failed final record.
        records = tq.get_nowait()
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0].passed)

    def test_run_health_check_stop_before_loop_puts_empty_list(self):
        self.plugin.stop()
        tq = queue.Queue()
        sleeps = self._run_health_check_guarded(
            self._monitoring_config(), tq
        )
        self.assertEqual(sleeps, [])
        self.assertEqual(tq.qsize(), 1)
        self.assertEqual(tq.get_nowait(), [])
        self.mock_krkn_lib.list_pods.assert_not_called()

    def test_run_health_check_stop_mid_run_still_emits_final_record(self):
        self.mock_krkn_lib.list_pods.return_value = [make_pod("p1")]
        tq = queue.Queue()
        self._run_health_check_guarded(
            self._monitoring_config(), tq, on_sleep=self.plugin.stop
        )
        # The first pass completed before the loop noticed the stop event.
        self.assertEqual(self.mock_krkn_lib.list_pods.call_count, 1)
        records = tq.get_nowait()
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0].passed)

    def test_run_health_check_unnamed_check_passing_uses_default_name(self):
        self.mock_krkn_lib.list_pods.return_value = [make_pod("p1")]
        config = {
            "config": [
                {"kind": "Pod", "namespace": "default",
                 "condition": {"type": "Ready", "status": "True"}}
            ]
        }
        tq = queue.Queue()
        self._run_health_check_guarded(config, tq)
        records = tq.get_nowait()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].check_name, "unnamed-check")
        self.assertTrue(records[0].passed)

    def test_run_health_check_unnamed_check_failing_should_emit_record(self):
        self.mock_krkn_lib.list_pods.return_value = [make_pod("p1", ready=False)]
        config = {
            "config": [
                {"kind": "Pod", "namespace": "default",
                 "condition": {"type": "Ready", "status": "True"}}
            ]
        }
        tq = queue.Queue()
        # The KeyError under test is raised inside the worker thread; silence
        # thread excepthook reporting for this expected crash.
        with patch.object(threading, "excepthook", lambda args: None):
            self._run_health_check_guarded(config, tq)
        self.assertEqual(tq.qsize(), 1)
        records = tq.get_nowait()
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0].passed)


if __name__ == "__main__":
    unittest.main()
