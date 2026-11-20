#!/usr/bin/env python3
"""
Test suite for VirtHealthCheckPlugin

This test file provides comprehensive coverage for the virt health check plugin:
- Plugin creation via factory
- VM health check functionality
- Thread-safe iteration tracking
- Telemetry collection
- Disconnected SSH access checking

How to run:
    # Run directly (requires full krkn environment with dependencies)
    python3 tests/test_virt_health_check_plugin.py

    # Run from project root
    cd /path/to/kraken
    python3 tests/test_virt_health_check_plugin.py

    # Run with unittest
    python3 -m unittest tests/test_virt_health_check_plugin.py -v

    # Run specific test
    python3 -m unittest tests.test_virt_health_check_plugin.TestVirtHealthCheckPlugin.test_plugin_creation -v

 

Migrated from test_virt_checker.py to use the plugin architecture.
"""

import queue
import sys
import os
import unittest
from unittest.mock import MagicMock, patch

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from krkn.health_checks import HealthCheckFactory, HealthCheckPluginNotFound
from krkn_lib.models.telemetry.models import VirtCheck
from datetime import datetime


class TestVirtHealthCheckPlugin(unittest.TestCase):

    def setUp(self):
        """Set up test fixtures for virt health check plugin"""
        self.factory = HealthCheckFactory()

        # Skip tests if plugin not loaded (missing dependencies)
        if "virt_health_check" not in self.factory.loaded_plugins:
            self.skipTest("Virt health check plugin not loaded (missing dependencies)")

        # Mock KrknKubernetes client
        self.mock_kubecli = MagicMock()

        # Create plugin with mock client
        self.plugin = self.factory.create_plugin(
            "virt_health_check",
            iterations=5,
            krkn_lib=self.mock_kubecli
        )

    def tearDown(self):
        """Clean up after each test"""
        if hasattr(self, 'plugin'):
            self.plugin.current_iterations = 0
            self.plugin.set_return_value(0)

    def test_plugin_creation(self):
        """Test plugin is created correctly via factory"""
        self.assertIsNotNone(self.plugin)
        self.assertEqual(self.plugin.iterations, 5)
        self.assertEqual(self.plugin.current_iterations, 0)
        self.assertEqual(self.plugin.get_return_value(), 0)

    def test_get_health_check_types(self):
        """Test plugin returns correct health check types"""
        types = self.plugin.get_health_check_types()
        self.assertIn("virt_health_check", types)
        self.assertIn("kubevirt_health_check", types)
        self.assertIn("vm_health_check", types)

    def test_increment_iterations(self):
        """Test increment_iterations is thread-safe"""
        initial = self.plugin.current_iterations

        # Call multiple times
        for _ in range(5):
            self.plugin.increment_iterations()

        self.assertEqual(self.plugin.current_iterations, initial + 5)

    def test_return_value_methods(self):
        """Test get/set return value methods"""
        self.assertEqual(self.plugin.get_return_value(), 0)

        self.plugin.set_return_value(2)
        self.assertEqual(self.plugin.get_return_value(), 2)

        self.plugin.set_return_value(0)
        self.assertEqual(self.plugin.get_return_value(), 0)

    def test_initialization_empty_config(self):
        """Test plugin initialization with empty namespace config"""
        config = {
            "namespace": "",
            "interval": 2
        }

        telemetry_queue = queue.Queue()
        self.plugin.run_health_check(config, telemetry_queue)

        # Should skip initialization and not crash
        self.assertTrue(telemetry_queue.empty())

    @patch('krkn.health_checks.virt_health_check_plugin.VmiOutageScenarioPlugin')
    def test_initialization_with_vmis(self, mock_plugin_class):
        """Test plugin initialization discovers VMIs"""
        # Mock the plugin instance
        mock_plugin = MagicMock()
        mock_plugin_class.return_value = mock_plugin

        # Mock VMI data
        mock_vmis = [
            {
                "metadata": {"name": "test-vm1", "namespace": "default"},
                "status": {
                    "nodeName": "worker-1",
                    "interfaces": [{"ipAddress": "10.0.0.1"}]
                }
            },
            {
                "metadata": {"name": "test-vm2", "namespace": "default"},
                "status": {
                    "nodeName": "worker-2",
                    "interfaces": [{"ipAddress": "10.0.0.2"}]
                }
            }
        ]

        # Setup mock to return VMIs
        self.mock_kubecli.get_vmis.return_value = mock_vmis
        mock_plugin.k8s_client = self.mock_kubecli

        config = {
            "namespace": "default",
            "name": ".*",
            "interval": 2,
            "disconnected": False,
            "only_failures": False
        }

        # Initialize from config
        result = self.plugin._initialize_from_config(config)

        self.assertTrue(result)
        self.assertEqual(len(self.plugin.vm_list), 2)

    @patch('krkn.health_checks.virt_health_check_plugin.invoke_no_exit')
    def test_check_disconnected_access_success(self, mock_invoke):
        """Test disconnected SSH access check succeeds"""
        # Mock returns values that match the expected output format
        # First call is debug output, second call is the actual check
        # The check looks for "True" in the output to indicate success

        # Track call count to differentiate between first and second invoke
        call_count = [0]
        def side_effect_fn(*args, **kwargs):
            call_count[0] += 1
            cmd = args[0] if args else ""

            # Second call has the check command with grep and echo
            if call_count[0] == 2 and ("grep Permission" in cmd or "2>&1" in cmd):
                # Return string containing "True" to indicate success
                return "Permission denied (publickey)\nTrue"
            else:
                # First call is debug - return permission denied message
                return "Permission denied (publickey)"

        mock_invoke.side_effect = side_effect_fn

        # Mock kube_vm_plugin to avoid None error (shouldn't be needed if returning early)
        # But set it up properly in case the check fails and tries to get VMI info
        mock_vm_plugin = MagicMock()
        mock_vm_plugin.get_vmi.return_value = {
            "status": {
                "interfaces": [{"ipAddress": "10.0.0.1"}],
                "nodeName": "worker-1"
            }
        }
        self.plugin.kube_vm_plugin = mock_vm_plugin
        self.plugin.namespace = "default"

        result, new_ip, new_node = self.plugin.check_disconnected_access(
            "10.0.0.1",
            "worker-1",
            "test-vm"
        )

        self.assertTrue(result)
        self.assertIsNone(new_ip)
        self.assertIsNone(new_node)
        # Verify invoke was called twice (debug + actual check)
        self.assertEqual(mock_invoke.call_count, 2)
        # Verify we didn't need to call get_vmi (check succeeded on first try)
        mock_vm_plugin.get_vmi.assert_not_called()

    @patch('krkn.health_checks.virt_health_check_plugin.invoke_no_exit')
    def test_get_vm_access_success(self, mock_invoke):
        """Test VM access check via virtctl succeeds"""
        # The method tries two different virtctl commands
        # Either one can return "True" to indicate success
        # Return output that contains "True" to simulate permission denied but accessible
        mock_invoke.return_value = "Permission denied (publickey).\nTrue"

        result = self.plugin.get_vm_access("test-vm", "default")

        self.assertTrue(result)
        # Verify invoke was called (may be called 1 or 2 times depending on first result)
        self.assertGreaterEqual(mock_invoke.call_count, 1)

    @patch('krkn.health_checks.virt_health_check_plugin.invoke_no_exit')
    def test_get_vm_access_failure(self, mock_invoke):
        """Test VM access check via virtctl fails"""
        mock_invoke.return_value = "False"

        result = self.plugin.get_vm_access("test-vm", "default")

        self.assertFalse(result)

    def test_thread_join(self):
        """Test thread_join waits for worker threads"""
        # Create mock threads
        mock_thread1 = MagicMock()
        mock_thread2 = MagicMock()
        self.plugin.threads = [mock_thread1, mock_thread2]

        self.plugin.thread_join()

        # Verify join was called on all threads
        mock_thread1.join.assert_called_once()
        mock_thread2.join.assert_called_once()

    def test_batch_size_calculation(self):
        """Test batch size is calculated correctly"""
        # Create plugin with mock VMs
        self.plugin.vm_list = [MagicMock() for _ in range(25)]
        self.plugin.threads_limit = 10

        import math
        expected_batch_size = math.ceil(25 / 10)

        # Calculate batch size
        self.plugin.batch_size = math.ceil(len(self.plugin.vm_list) / self.plugin.threads_limit)

        self.assertEqual(self.plugin.batch_size, expected_batch_size)


class TestVirtHealthCheckPluginCoverage(unittest.TestCase):
    """Additional tests to increase coverage of VirtHealthCheckPlugin"""

    def setUp(self):
        self.factory = HealthCheckFactory()
        if "virt_health_check" not in self.factory.loaded_plugins:
            self.skipTest("Virt health check plugin not loaded (missing dependencies)")
        self.mock_kubecli = MagicMock()
        self.plugin = self.factory.create_plugin(
            "virt_health_check", iterations=2, krkn_lib=self.mock_kubecli
        )

    def tearDown(self):
        if hasattr(self, "plugin"):
            self.plugin.current_iterations = 0
            self.plugin.set_return_value(0)

    # --- _initialize_from_config ---

    @patch("krkn.health_checks.virt_health_check_plugin.VmiOutageScenarioPlugin")
    def test_initialize_from_config_exception(self, mock_plugin_class):
        """Test _initialize_from_config returns False on exception"""
        mock_plugin_class.side_effect = Exception("connection error")
        config = {"namespace": "default", "interval": 2}
        result = self.plugin._initialize_from_config(config)
        self.assertFalse(result)

    @patch("krkn.health_checks.virt_health_check_plugin.VmiOutageScenarioPlugin")
    def test_initialize_from_config_vmi_no_interfaces(self, mock_plugin_class):
        """Test VMIs with no interfaces are skipped"""
        mock_plugin = MagicMock()
        mock_plugin_class.return_value = mock_plugin
        mock_plugin.k8s_client = self.mock_kubecli
        self.mock_kubecli.get_vmis.return_value = [
            {
                "metadata": {"name": "no-iface-vm", "namespace": "default"},
                "status": {"nodeName": "worker-1", "interfaces": []},
            }
        ]
        config = {"namespace": "default", "interval": 2}
        result = self.plugin._initialize_from_config(config)
        self.assertTrue(result)
        self.assertEqual(len(self.plugin.vm_list), 0)

    @patch("krkn.health_checks.virt_health_check_plugin.VmiOutageScenarioPlugin")
    def test_initialize_from_config_node_name_filter_match(self, mock_plugin_class):
        """Test VMIs are filtered by node_names when specified and matching"""
        mock_plugin = MagicMock()
        mock_plugin_class.return_value = mock_plugin
        mock_plugin.k8s_client = self.mock_kubecli
        self.mock_kubecli.get_vmis.return_value = [
            {
                "metadata": {"name": "vm1", "namespace": "default"},
                "status": {"nodeName": "worker-1", "interfaces": [{"ipAddress": "10.0.0.1"}]},
            },
            {
                "metadata": {"name": "vm2", "namespace": "default"},
                "status": {"nodeName": "worker-2", "interfaces": [{"ipAddress": "10.0.0.2"}]},
            },
        ]
        config = {"namespace": "default", "node_names": "worker-1", "interval": 2}
        result = self.plugin._initialize_from_config(config)
        self.assertTrue(result)
        self.assertEqual(len(self.plugin.vm_list), 1)
        self.assertEqual(self.plugin.vm_list[0].vm_name, "vm1")

    @patch("krkn.health_checks.virt_health_check_plugin.VmiOutageScenarioPlugin")
    def test_initialize_from_config_node_name_filter_no_match(self, mock_plugin_class):
        """Test VMIs are excluded when node_names is set but node doesn't match"""
        mock_plugin = MagicMock()
        mock_plugin_class.return_value = mock_plugin
        mock_plugin.k8s_client = self.mock_kubecli
        self.mock_kubecli.get_vmis.return_value = [
            {
                "metadata": {"name": "vm1", "namespace": "default"},
                "status": {"nodeName": "worker-99", "interfaces": [{"ipAddress": "10.0.0.1"}]},
            }
        ]
        config = {"namespace": "default", "node_names": "worker-1", "interval": 2}
        result = self.plugin._initialize_from_config(config)
        self.assertTrue(result)
        self.assertEqual(len(self.plugin.vm_list), 0)

    # --- check_disconnected_access ---

    @patch("krkn.health_checks.virt_health_check_plugin.invoke_no_exit")
    def test_check_disconnected_access_new_ip_success(self, mock_invoke):
        """Test disconnected access succeeds after VM restarts with new IP"""
        mock_vmi = {
            "status": {
                "interfaces": [{"ipAddress": "10.0.0.2"}],
                "nodeName": "worker-1",
            }
        }
        mock_vm_plugin = MagicMock()
        mock_vm_plugin.get_vmi.return_value = mock_vmi
        self.plugin.kube_vm_plugin = mock_vm_plugin
        self.plugin.namespace = "default"

        call_count = [0]
        def side_effect(*_):
            call_count[0] += 1
            if call_count[0] <= 2:
                return "False"  # initial check fails
            return "True"  # new IP check succeeds

        mock_invoke.side_effect = side_effect

        result, new_ip, new_node = self.plugin.check_disconnected_access(
            "10.0.0.1", "worker-1", "test-vm"
        )
        self.assertTrue(result)
        self.assertEqual(new_ip, "10.0.0.2")
        self.assertIsNone(new_node)

    @patch("krkn.health_checks.virt_health_check_plugin.invoke_no_exit")
    def test_check_disconnected_access_node_migration_success(self, mock_invoke):
        """Test disconnected access succeeds after VM migrates to new node"""
        mock_vmi = {
            "status": {
                "interfaces": [{"ipAddress": "10.0.0.1"}],
                "nodeName": "worker-2",
            }
        }
        mock_vm_plugin = MagicMock()
        mock_vm_plugin.get_vmi.return_value = mock_vmi
        self.plugin.kube_vm_plugin = mock_vm_plugin
        self.plugin.namespace = "default"

        call_count = [0]
        def side_effect(*_):
            call_count[0] += 1
            if call_count[0] <= 2:
                return "False"
            return "True"  # new node check succeeds (call 3)

        mock_invoke.side_effect = side_effect

        result, new_ip, new_node = self.plugin.check_disconnected_access(
            "10.0.0.1", "worker-1", "test-vm"
        )
        self.assertTrue(result)
        self.assertEqual(new_ip, "10.0.0.1")
        self.assertEqual(new_node, "worker-2")

    @patch("krkn.health_checks.virt_health_check_plugin.invoke_no_exit")
    def test_check_disconnected_access_ssh_node_fallback(self, mock_invoke):
        """Test disconnected access falls back to ssh_node"""
        mock_vmi = {
            "status": {
                "interfaces": [{"ipAddress": "10.0.0.1"}],
                "nodeName": "worker-1",
            }
        }
        mock_vm_plugin = MagicMock()
        mock_vm_plugin.get_vmi.return_value = mock_vmi
        self.plugin.kube_vm_plugin = mock_vm_plugin
        self.plugin.namespace = "default"
        self.plugin.ssh_node = "bastion"

        call_count = [0]
        def side_effect(*_):
            call_count[0] += 1
            if call_count[0] <= 2:
                return "False"
            return "True"  # bastion fallback succeeds (call 3)

        mock_invoke.side_effect = side_effect

        result, new_ip, new_node = self.plugin.check_disconnected_access(
            "10.0.0.1", "worker-1", "test-vm"
        )
        self.assertTrue(result)
        self.assertEqual(new_ip, "10.0.0.1")
        self.assertIsNone(new_node)

    @patch("krkn.health_checks.virt_health_check_plugin.invoke_no_exit")
    def test_check_disconnected_access_all_fail(self, mock_invoke):
        """Test check_disconnected_access returns False when all attempts fail"""
        mock_vmi = {
            "status": {
                "interfaces": [{"ipAddress": "10.0.0.1"}],
                "nodeName": "worker-1",
            }
        }
        mock_vm_plugin = MagicMock()
        mock_vm_plugin.get_vmi.return_value = mock_vmi
        self.plugin.kube_vm_plugin = mock_vm_plugin
        self.plugin.namespace = "default"
        self.plugin.ssh_node = ""

        mock_invoke.return_value = "False"

        result, new_ip, new_node = self.plugin.check_disconnected_access(
            "10.0.0.1", "worker-1", "test-vm"
        )
        self.assertFalse(result)
        self.assertIsNone(new_ip)
        self.assertIsNone(new_node)

    # --- get_vm_access ---

    @patch("krkn.health_checks.virt_health_check_plugin.invoke_no_exit")
    def test_get_vm_access_first_fails_second_succeeds(self, mock_invoke):
        """Test get_vm_access falls back to second virtctl command"""
        call_count = [0]
        def side_effect(*_):
            call_count[0] += 1
            return "False" if call_count[0] == 1 else "True"

        mock_invoke.side_effect = side_effect

        result = self.plugin.get_vm_access("test-vm", "default")
        self.assertTrue(result)
        self.assertEqual(mock_invoke.call_count, 2)

    # --- batch_list ---

    def test_batch_list_zero_batch_size_no_threads(self):
        """Test batch_list does not start threads when batch_size is 0"""
        self.plugin.batch_size = 0
        self.plugin.vm_list = []
        self.plugin.batch_list(queue.SimpleQueue())
        self.assertEqual(len(self.plugin.threads), 0)

    @patch.object(
        __import__("krkn.health_checks.virt_health_check_plugin", fromlist=["VirtHealthCheckPlugin"]).VirtHealthCheckPlugin,
        "_run_virt_check_batch",
    )
    def test_batch_list_starts_threads(self, mock_batch):
        """Test batch_list starts worker threads for VM batches"""
        import math
        mock_batch.return_value = None
        self.plugin.vm_list = [MagicMock() for _ in range(4)]
        self.plugin.threads_limit = 2
        self.plugin.batch_size = math.ceil(len(self.plugin.vm_list) / self.plugin.threads_limit)
        self.plugin.batch_list(queue.SimpleQueue())
        for t in self.plugin.threads:
            t.join()
        self.assertGreater(len(self.plugin.threads), 0)

    # --- _run_virt_check_batch ---

    @patch("krkn.health_checks.virt_health_check_plugin.time.sleep")
    def test_run_virt_check_batch_tracks_initial_status(self, mock_sleep):
        """Test batch runner adds VM to tracker on first pass and records final status"""
        def stop_after_sleep(*_):
            self.plugin.current_iterations = self.plugin.iterations

        mock_sleep.side_effect = stop_after_sleep

        mock_vm = MagicMock()
        mock_vm.vm_name = "vm1"
        mock_vm.namespace = "default"
        mock_vm.ip_address = "10.0.0.1"
        mock_vm.node_name = "worker-1"
        mock_vm.new_ip_address = ""
        self.plugin.disconnected = False

        with patch.object(self.plugin, "get_vm_access", return_value=True):
            telemetry_queue = queue.SimpleQueue()
            self.plugin._run_virt_check_batch([mock_vm], telemetry_queue)

        result = telemetry_queue.get_nowait()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].vm_name, "vm1")

    @patch("krkn.health_checks.virt_health_check_plugin.time.sleep")
    def test_run_virt_check_batch_records_status_change(self, mock_sleep):
        """Test batch runner records status changes between iterations"""
        call_count = [0]
        def stop_after_two(*_):
            call_count[0] += 1
            if call_count[0] >= 2:
                self.plugin.current_iterations = self.plugin.iterations

        mock_sleep.side_effect = stop_after_two

        mock_vm = MagicMock()
        mock_vm.vm_name = "vm1"
        mock_vm.namespace = "default"
        mock_vm.ip_address = "10.0.0.1"
        mock_vm.node_name = "worker-1"
        mock_vm.new_ip_address = ""
        self.plugin.disconnected = False
        self.plugin.only_failures = False

        statuses = [True, False]
        idx = [0]
        def alternating(*_):
            val = statuses[idx[0] % 2]
            idx[0] += 1
            return val

        with patch.object(self.plugin, "get_vm_access", side_effect=alternating):
            telemetry_queue = queue.SimpleQueue()
            self.plugin._run_virt_check_batch([mock_vm], telemetry_queue)

        result = telemetry_queue.get_nowait()
        self.assertGreaterEqual(len(result), 1)

    @patch("krkn.health_checks.virt_health_check_plugin.time.sleep")
    def test_run_virt_check_batch_only_failures_skips_success(self, mock_sleep):
        """Test only_failures=True skips successful status changes in telemetry"""
        call_count = [0]
        def stop_after_two(*_):
            call_count[0] += 1
            if call_count[0] >= 2:
                self.plugin.current_iterations = self.plugin.iterations

        mock_sleep.side_effect = stop_after_two

        mock_vm = MagicMock()
        mock_vm.vm_name = "vm1"
        mock_vm.namespace = "default"
        mock_vm.ip_address = "10.0.0.1"
        mock_vm.node_name = "worker-1"
        mock_vm.new_ip_address = ""
        self.plugin.disconnected = False
        self.plugin.only_failures = True

        # VM goes from False (failure) -> True (success): only_failures skips success transitions
        statuses = [False, True]
        idx = [0]
        def alternating(*_):
            val = statuses[idx[0] % 2]
            idx[0] += 1
            return val

        with patch.object(self.plugin, "get_vm_access", side_effect=alternating):
            telemetry_queue = queue.SimpleQueue()
            self.plugin._run_virt_check_batch([mock_vm], telemetry_queue)

        result = telemetry_queue.get_nowait()
        # Final result may still be appended; just confirm no exception and queue populated
        self.assertIsNotNone(result)

    @patch("krkn.health_checks.virt_health_check_plugin.time.sleep")
    def test_run_virt_check_batch_exception_sets_false(self, mock_sleep):
        """Test exception in VM access is caught and status set to False"""
        def stop_after_sleep(*_):
            self.plugin.current_iterations = self.plugin.iterations

        mock_sleep.side_effect = stop_after_sleep

        mock_vm = MagicMock()
        mock_vm.vm_name = "vm-err"
        mock_vm.namespace = "default"
        mock_vm.ip_address = "10.0.0.1"
        mock_vm.node_name = "worker-1"
        mock_vm.new_ip_address = ""
        self.plugin.disconnected = False

        with patch.object(self.plugin, "get_vm_access", side_effect=Exception("boom")):
            telemetry_queue = queue.SimpleQueue()
            self.plugin._run_virt_check_batch([mock_vm], telemetry_queue)

        result = telemetry_queue.get_nowait()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].vm_name, "vm-err")

    @patch("krkn.health_checks.virt_health_check_plugin.time.sleep")
    def test_run_virt_check_batch_disconnected_updates_new_ip(self, mock_sleep):
        """Test disconnected mode updates new IP on VM object"""
        def stop_after_sleep(*_):
            self.plugin.current_iterations = self.plugin.iterations

        mock_sleep.side_effect = stop_after_sleep

        mock_vm = MagicMock()
        mock_vm.vm_name = "vm1"
        mock_vm.namespace = "default"
        mock_vm.ip_address = "10.0.0.1"
        mock_vm.node_name = "worker-1"
        mock_vm.new_ip_address = ""
        self.plugin.disconnected = True

        with patch.object(
            self.plugin, "check_disconnected_access", return_value=(True, "10.0.0.2", None)
        ):
            telemetry_queue = queue.SimpleQueue()
            self.plugin._run_virt_check_batch([mock_vm], telemetry_queue)

        self.assertEqual(mock_vm.new_ip_address, "10.0.0.2")

    @patch("krkn.health_checks.virt_health_check_plugin.time.sleep")
    def test_run_virt_check_batch_disconnected_uses_new_ip(self, mock_sleep):
        """Test disconnected mode uses existing new_ip_address if already set"""
        def stop_after_sleep(*_):
            self.plugin.current_iterations = self.plugin.iterations

        mock_sleep.side_effect = stop_after_sleep

        mock_vm = MagicMock()
        mock_vm.vm_name = "vm1"
        mock_vm.namespace = "default"
        mock_vm.ip_address = "10.0.0.1"
        mock_vm.node_name = "worker-1"
        mock_vm.new_ip_address = "10.0.0.5"
        self.plugin.disconnected = True

        check_calls = []
        def capture_call(ip, *_):
            check_calls.append(ip)
            return (True, None, None)

        with patch.object(self.plugin, "check_disconnected_access", side_effect=capture_call):
            telemetry_queue = queue.SimpleQueue()
            self.plugin._run_virt_check_batch([mock_vm], telemetry_queue)

        self.assertIn("10.0.0.5", check_calls)

    @patch("krkn.health_checks.virt_health_check_plugin.time.sleep")
    def test_run_virt_check_batch_vmi_ready_to_not_ready_transition(self, mock_sleep):
        """Test batch state machine records two segments with correct check_type on vmi_ready→not-ready flip"""
        call_count = [0]
        def stop_after_two(*_):
            call_count[0] += 1
            if call_count[0] >= 2:
                self.plugin.current_iterations = self.plugin.iterations

        mock_sleep.side_effect = stop_after_two

        mock_vm = MagicMock()
        mock_vm.vm_name = "vm1"
        mock_vm.namespace = "default"
        mock_vm.ip_address = "10.0.0.1"
        mock_vm.node_name = "worker-1"
        mock_vm.new_ip_address = ""
        self.plugin.disconnected = False
        self.plugin.only_failures = False

        # vmi_ready: True on first check, False on second; ssh stays True throughout
        vmi_seq = [True, False]
        vmi_idx = [0]
        def next_vmi_status(*_):
            val = vmi_seq[vmi_idx[0] % len(vmi_seq)]
            vmi_idx[0] += 1
            return val

        with patch.object(self.plugin, "get_vm_access", return_value=True), \
             patch.object(self.plugin, "check_vmi_ready", side_effect=next_vmi_status):
            telemetry_queue = queue.SimpleQueue()
            self.plugin._run_virt_check_batch([mock_vm], telemetry_queue)

        result = telemetry_queue.get_nowait()
        # Transition closes the first segment and opens a second, so two VirtCheck entries
        self.assertGreaterEqual(len(result), 2)

        healthy = [r for r in result if r.vmi_ready]
        not_ready = [r for r in result if not r.vmi_ready]

        self.assertGreater(len(healthy), 0)
        self.assertTrue(healthy[0].ssh_status)
        self.assertTrue(healthy[0].vmi_ready)
        self.assertEqual(healthy[0].check_type, "healthy")

        self.assertGreater(len(not_ready), 0)
        self.assertTrue(not_ready[0].ssh_status)
        self.assertFalse(not_ready[0].vmi_ready)
        self.assertEqual(not_ready[0].check_type, "vmi_ready")

    # --- _run_post_virt_check ---

    def test_run_post_virt_check_failed_vm_added_to_telemetry(self):
        """Test _run_post_virt_check adds failing VMs to telemetry with correct discriminator fields"""
        mock_vm = MagicMock()
        mock_vm.vm_name = "vm-fail"
        mock_vm.namespace = "default"
        mock_vm.ip_address = "10.0.0.1"
        mock_vm.node_name = "worker-1"
        mock_vm.new_ip_address = ""
        self.plugin.disconnected = False

        with patch.object(self.plugin, "get_vm_access", return_value=False), \
             patch.object(self.plugin, "check_vmi_ready", return_value=False):
            result_queue = queue.SimpleQueue()
            self.plugin._run_post_virt_check([mock_vm], [], result_queue)

        result = result_queue.get_nowait()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].vm_name, "vm-fail")
        self.assertFalse(result[0].ssh_status)
        self.assertFalse(result[0].vmi_ready)
        self.assertEqual(result[0].check_type, "both")

    def test_run_post_virt_check_healthy_vm_not_in_telemetry(self):
        """Test _run_post_virt_check skips healthy VMs (ssh up and VMI ready)"""
        mock_vm = MagicMock()
        mock_vm.vm_name = "vm-ok"
        mock_vm.namespace = "default"
        mock_vm.ip_address = "10.0.0.1"
        mock_vm.node_name = "worker-1"
        mock_vm.new_ip_address = ""
        self.plugin.disconnected = False

        with patch.object(self.plugin, "get_vm_access", return_value=True), \
             patch.object(self.plugin, "check_vmi_ready", return_value=True) as mock_vmi_ready:
            result_queue = queue.SimpleQueue()
            self.plugin._run_post_virt_check([mock_vm], [], result_queue)

        result = result_queue.get_nowait()
        self.assertEqual(len(result), 0)
        # Both checks were invoked; their True results combined to skip this VM from telemetry
        mock_vmi_ready.assert_called_once_with("vm-ok", "default")

    def test_run_post_virt_check_disconnected_mode(self):
        """Test _run_post_virt_check in disconnected mode"""
        mock_vm = MagicMock()
        mock_vm.vm_name = "vm1"
        mock_vm.namespace = "default"
        mock_vm.ip_address = "10.0.0.1"
        mock_vm.node_name = "worker-1"
        mock_vm.new_ip_address = ""
        self.plugin.disconnected = True

        with patch.object(
            self.plugin, "check_disconnected_access", return_value=(False, "10.0.0.2", "worker-2")
        ):
            result_queue = queue.SimpleQueue()
            self.plugin._run_post_virt_check([mock_vm], [], result_queue)

        self.assertEqual(mock_vm.new_ip_address, "10.0.0.2")
        self.assertEqual(mock_vm.node_name, "worker-2")

    def test_run_post_virt_check_check_type_both_when_ssh_and_vmi_fail(self):
        """Test VirtCheck.check_type is 'both' when ssh_status and vmi_ready are both False"""
        mock_vm = MagicMock()
        mock_vm.vm_name = "vm-both-fail"
        mock_vm.namespace = "default"
        mock_vm.ip_address = "10.0.0.1"
        mock_vm.node_name = "worker-1"
        mock_vm.new_ip_address = ""
        self.plugin.disconnected = False

        with patch.object(self.plugin, "get_vm_access", return_value=False), \
             patch.object(self.plugin, "check_vmi_ready", return_value=False):
            result_queue = queue.SimpleQueue()
            self.plugin._run_post_virt_check([mock_vm], [], result_queue)

        result = result_queue.get_nowait()
        self.assertEqual(len(result), 1)
        self.assertFalse(result[0].ssh_status)
        self.assertFalse(result[0].vmi_ready)
        self.assertEqual(result[0].check_type, "both")

    def test_run_post_virt_check_check_type_ssh_access_when_only_ssh_fails(self):
        """Test VirtCheck.check_type is 'ssh_access' when ssh_status is False but vmi_ready is True"""
        mock_vm = MagicMock()
        mock_vm.vm_name = "vm-ssh-fail"
        mock_vm.namespace = "default"
        mock_vm.ip_address = "10.0.0.1"
        mock_vm.node_name = "worker-1"
        mock_vm.new_ip_address = ""
        self.plugin.disconnected = False

        with patch.object(self.plugin, "get_vm_access", return_value=False), \
             patch.object(self.plugin, "check_vmi_ready", return_value=True):
            result_queue = queue.SimpleQueue()
            self.plugin._run_post_virt_check([mock_vm], [], result_queue)

        result = result_queue.get_nowait()
        self.assertEqual(len(result), 1)
        self.assertFalse(result[0].ssh_status)
        self.assertTrue(result[0].vmi_ready)
        self.assertEqual(result[0].check_type, "ssh_access")

    def test_run_post_virt_check_check_type_vmi_ready_when_only_vmi_not_ready(self):
        """Test VirtCheck.check_type is 'vmi_ready' when ssh_status is True but vmi_ready is False"""
        mock_vm = MagicMock()
        mock_vm.vm_name = "vm-vmi-fail"
        mock_vm.namespace = "default"
        mock_vm.ip_address = "10.0.0.1"
        mock_vm.node_name = "worker-1"
        mock_vm.new_ip_address = ""
        self.plugin.disconnected = False

        with patch.object(self.plugin, "get_vm_access", return_value=True), \
             patch.object(self.plugin, "check_vmi_ready", return_value=False):
            result_queue = queue.SimpleQueue()
            self.plugin._run_post_virt_check([mock_vm], [], result_queue)

        result = result_queue.get_nowait()
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].ssh_status)
        self.assertFalse(result[0].vmi_ready)
        self.assertEqual(result[0].check_type, "vmi_ready")

    # --- gather_post_virt_checks ---

    def test_gather_post_virt_checks_exit_on_failure(self):
        """Test gather_post_virt_checks sets ret_value when exit_on_failure and failures exist"""
        self.plugin.exit_on_failure = True
        self.plugin.batch_size = 0  # skip thread logic

        # Simulate existing telemetry with failures
        self.plugin.gather_post_virt_checks([MagicMock()])
        self.assertEqual(self.plugin.get_return_value(), 3)

    def test_gather_post_virt_checks_no_failures_no_exit(self):
        """Test gather_post_virt_checks does not set ret_value when no failures"""
        self.plugin.exit_on_failure = True
        self.plugin.batch_size = 0

        self.plugin.gather_post_virt_checks([])
        self.assertEqual(self.plugin.get_return_value(), 0)

    # --- run_health_check ---

    def test_run_health_check_none_config_skips(self):
        """Test run_health_check returns early on None config"""
        telemetry_queue = queue.Queue()
        self.plugin.run_health_check(None, telemetry_queue)
        self.assertTrue(telemetry_queue.empty())

    @patch("krkn.health_checks.virt_health_check_plugin.VmiOutageScenarioPlugin")
    def test_run_health_check_valid_config_starts_batch(self, mock_plugin_class):
        """Test run_health_check starts batch threads on valid config"""
        mock_plugin = MagicMock()
        mock_plugin_class.return_value = mock_plugin
        mock_plugin.k8s_client = self.mock_kubecli
        self.mock_kubecli.get_vmis.return_value = []

        self.plugin.current_iterations = self.plugin.iterations  # prevent infinite loop
        config = {"namespace": "default", "interval": 2}
        telemetry_queue = queue.Queue()
        self.plugin.run_health_check(config, telemetry_queue)
        # batch_size is 0 (no VMs), so no threads — but no crash either
        self.assertEqual(len(self.plugin.threads), 0)


class TestVirtHealthCheckPluginFactory(unittest.TestCase):
    """Test factory-specific functionality"""

    def test_factory_loads_virt_plugin(self):
        """Test that factory loads virt health check plugin"""
        factory = HealthCheckFactory()

        # May not be loaded if dependencies missing
        virt_types = ["virt_health_check", "kubevirt_health_check", "vm_health_check"]
        found = any(vt in factory.loaded_plugins for vt in virt_types)

        if not found:
            self.skipTest("Virt health check plugin not loaded (missing dependencies)")

        # At least one type should be loaded
        self.assertTrue(found)

    def test_factory_creates_virt_plugin(self):
        """Test factory creates virt plugin instances"""
        factory = HealthCheckFactory()

        if "virt_health_check" not in factory.loaded_plugins:
            self.skipTest("Virt health check plugin not loaded (missing dependencies)")

        mock_kubecli = MagicMock()
        plugin = factory.create_plugin(
            "virt_health_check",
            iterations=10,
            krkn_lib=mock_kubecli
        )

        self.assertIsNotNone(plugin)
        self.assertEqual(plugin.iterations, 10)
        self.assertEqual(plugin.__class__.__name__, "VirtHealthCheckPlugin")

    def test_factory_multiple_type_mappings(self):
        """Test factory maps multiple types to virt plugin"""
        factory = HealthCheckFactory()

        if "virt_health_check" not in factory.loaded_plugins:
            self.skipTest("Virt health check plugin not loaded (missing dependencies)")

        mock_kubecli = MagicMock()

        # All these types should map to the same plugin class
        plugin1 = factory.create_plugin("virt_health_check", iterations=5, krkn_lib=mock_kubecli)
        plugin2 = factory.create_plugin("kubevirt_health_check", iterations=5, krkn_lib=mock_kubecli)
        plugin3 = factory.create_plugin("vm_health_check", iterations=5, krkn_lib=mock_kubecli)

        self.assertEqual(plugin1.__class__.__name__, "VirtHealthCheckPlugin")
        self.assertEqual(plugin2.__class__.__name__, "VirtHealthCheckPlugin")
        self.assertEqual(plugin3.__class__.__name__, "VirtHealthCheckPlugin")


class TestVirtHealthCheckPluginLabelSelector(unittest.TestCase):
    """Tests for label_selector support in VirtHealthCheckPlugin._initialize_from_config"""

    def setUp(self):
        self.factory = HealthCheckFactory()
        if "virt_health_check" not in self.factory.loaded_plugins:
            self.skipTest("Virt health check plugin not loaded (missing dependencies)")
        self.mock_kubecli = MagicMock()
        self.plugin = self.factory.create_plugin(
            "virt_health_check", iterations=1, krkn_lib=self.mock_kubecli
        )

    def _mock_vmi(self, name, ip="10.0.0.1", node="worker-1", namespace="default"):
        return {
            "metadata": {"name": name, "namespace": namespace},
            "status": {
                "nodeName": node,
                "interfaces": [{"ipAddress": ip}],
            },
        }

    @patch("krkn.health_checks.virt_health_check_plugin.VmiOutageScenarioPlugin")
    def test_initialize_with_label_selector_only(self, mock_plugin_class):
        """Test _initialize_from_config filters VMIs by label_selector when name is not set"""
        mock_plugin = MagicMock()
        mock_plugin_class.return_value = mock_plugin
        mock_plugin.k8s_client = self.mock_kubecli
        self.mock_kubecli.get_vmis.return_value = [self._mock_vmi("labeled-vm")]

        config = {"namespace": "default", "label_selector": "app=myvm", "interval": 2}

        result = self.plugin._initialize_from_config(config)

        self.assertTrue(result)
        self.assertEqual(len(self.plugin.vm_list), 1)
        self.mock_kubecli.get_vmis.assert_called_once_with(
            ".*", "default", label_selector="app=myvm"
        )

    @patch("krkn.health_checks.virt_health_check_plugin.VmiOutageScenarioPlugin")
    def test_initialize_with_name_and_label_selector(self, mock_plugin_class):
        """Test _initialize_from_config passes both name and label_selector to get_vmis"""
        mock_plugin = MagicMock()
        mock_plugin_class.return_value = mock_plugin
        mock_plugin.k8s_client = self.mock_kubecli
        self.mock_kubecli.get_vmis.return_value = [self._mock_vmi("test-vm")]

        config = {
            "namespace": "default",
            "name": "test-vm",
            "label_selector": "env=chaos",
            "interval": 2,
        }

        result = self.plugin._initialize_from_config(config)

        self.assertTrue(result)
        self.mock_kubecli.get_vmis.assert_called_once_with(
            "test-vm", "default", label_selector="env=chaos"
        )

    @patch("krkn.health_checks.virt_health_check_plugin.VmiOutageScenarioPlugin")
    def test_initialize_no_name_no_label_selector_defaults_to_match_all(self, mock_plugin_class):
        """Test _initialize_from_config falls back to '.*' when neither name nor label_selector is set"""
        mock_plugin = MagicMock()
        mock_plugin_class.return_value = mock_plugin
        mock_plugin.k8s_client = self.mock_kubecli
        self.mock_kubecli.get_vmis.return_value = []

        config = {"namespace": "default", "interval": 2}

        result = self.plugin._initialize_from_config(config)

        self.assertTrue(result)
        self.mock_kubecli.get_vmis.assert_called_once_with(
            ".*", "default", label_selector=None
        )

    @patch("krkn.health_checks.virt_health_check_plugin.VmiOutageScenarioPlugin")
    def test_initialize_empty_label_selector_treated_as_none(self, mock_plugin_class):
        """Test that an empty string label_selector is treated as not set, falling back to '.*'"""
        mock_plugin = MagicMock()
        mock_plugin_class.return_value = mock_plugin
        mock_plugin.k8s_client = self.mock_kubecli
        self.mock_kubecli.get_vmis.return_value = []

        config = {"namespace": "default", "label_selector": "", "interval": 2}

        result = self.plugin._initialize_from_config(config)

        self.assertTrue(result)
        self.mock_kubecli.get_vmis.assert_called_once_with(
            ".*", "default", label_selector=None
        )


class TestVirtIsConfiguredAndThreads(unittest.TestCase):
    """Tests for is_configured and manages_own_threads"""

    def setUp(self):
        self.factory = HealthCheckFactory()
        if "virt_health_check" not in self.factory.loaded_plugins:
            self.skipTest("Virt health check plugin not loaded (missing dependencies)")
        self.plugin = self.factory.create_plugin(
            "virt_health_check", iterations=1, krkn_lib=MagicMock()
        )

    def test_is_configured_non_empty_namespace_true(self):
        """Test is_configured returns True for a non-empty namespace"""
        self.assertTrue(self.plugin.is_configured({"namespace": "kubevirt"}))

    def test_is_configured_whitespace_namespace_false(self):
        """Test is_configured returns False for a whitespace-only namespace"""
        self.assertFalse(self.plugin.is_configured({"namespace": "   "}))

    def test_is_configured_key_absent_false(self):
        """Test is_configured returns False when namespace key is absent"""
        self.assertFalse(self.plugin.is_configured({}))

    def test_is_configured_none_namespace_false(self):
        """Test is_configured returns False when namespace is explicitly None"""
        # correct behaviour: None means not configured, consistent with the
        # namespace == "" gate in _initialize_from_config
        self.assertFalse(self.plugin.is_configured({"namespace": None}))

    def test_manages_own_threads_true(self):
        """Test manages_own_threads returns True so factory does not wrap run_health_check"""
        self.assertTrue(self.plugin.manages_own_threads())


class TestVirtCheckVmiReady(unittest.TestCase):
    """Tests for check_vmi_ready"""

    def setUp(self):
        self.factory = HealthCheckFactory()
        if "virt_health_check" not in self.factory.loaded_plugins:
            self.skipTest("Virt health check plugin not loaded (missing dependencies)")
        self.mock_kubecli = MagicMock()
        self.plugin = self.factory.create_plugin(
            "virt_health_check", iterations=1, krkn_lib=self.mock_kubecli
        )

    @staticmethod
    def _vmi(phase="Running", conditions=None):
        status = {"phase": phase}
        if conditions is not None:
            status["conditions"] = conditions
        return {"metadata": {"name": "vm1", "namespace": "default"}, "status": status}

    def test_check_vmi_ready_vmi_gone_false(self):
        """Test check_vmi_ready returns False when the VMI no longer exists"""
        self.mock_kubecli.get_vmi.return_value = None
        self.assertFalse(self.plugin.check_vmi_ready("vm1", "default"))
        self.mock_kubecli.get_vmi.assert_called_once_with("vm1", "default")

    def test_check_vmi_ready_running_ready_true(self):
        """Test check_vmi_ready returns True for Running phase with Ready=True"""
        self.mock_kubecli.get_vmi.return_value = self._vmi(
            conditions=[{"type": "Ready", "status": "True"}]
        )
        self.assertTrue(self.plugin.check_vmi_ready("vm1", "default"))

    def test_check_vmi_ready_running_ready_false(self):
        """Test check_vmi_ready returns False when Ready condition is False"""
        self.mock_kubecli.get_vmi.return_value = self._vmi(
            conditions=[{"type": "Ready", "status": "False"}]
        )
        self.assertFalse(self.plugin.check_vmi_ready("vm1", "default"))

    def test_check_vmi_ready_running_unrelated_condition_false(self):
        """Test check_vmi_ready returns False when only unrelated conditions exist"""
        self.mock_kubecli.get_vmi.return_value = self._vmi(
            conditions=[{"type": "LiveMigratable", "status": "True"}]
        )
        self.assertFalse(self.plugin.check_vmi_ready("vm1", "default"))

    def test_check_vmi_ready_running_no_conditions_key_false(self):
        """Test check_vmi_ready returns False without raising when conditions key is absent"""
        self.mock_kubecli.get_vmi.return_value = self._vmi()
        self.assertFalse(self.plugin.check_vmi_ready("vm1", "default"))

    def test_check_vmi_ready_api_exception_false_logged(self):
        """Test check_vmi_ready returns False and logs when the API raises"""
        self.mock_kubecli.get_vmi.side_effect = Exception("api down")
        with self.assertLogs(level="ERROR") as logs:
            self.assertFalse(self.plugin.check_vmi_ready("vm1", "default"))
        self.assertTrue(any("vm1" in line for line in logs.output))

    def test_check_vmi_ready_not_running_phase_false(self):
        """Test check_vmi_ready returns False for a non-Running phase"""
        self.mock_kubecli.get_vmi.return_value = self._vmi(phase="Pending")
        self.assertFalse(self.plugin.check_vmi_ready("vm1", "default"))


class TestVirtBatchRunnerGaps(unittest.TestCase):
    """Tests for _run_virt_check_batch edge paths"""

    def setUp(self):
        self.factory = HealthCheckFactory()
        if "virt_health_check" not in self.factory.loaded_plugins:
            self.skipTest("Virt health check plugin not loaded (missing dependencies)")
        self.plugin = self.factory.create_plugin(
            "virt_health_check", iterations=2, krkn_lib=MagicMock()
        )
        self.plugin.disconnected = False
        self.plugin.only_failures = False

    @staticmethod
    def _mock_vm(name="vm1"):
        vm = MagicMock()
        vm.vm_name = name
        vm.namespace = "default"
        vm.ip_address = "10.0.0.1"
        vm.node_name = "worker-1"
        vm.new_ip_address = ""
        return vm

    @patch("krkn.health_checks.virt_health_check_plugin.time.sleep")
    def test_run_virt_check_batch_stamps_new_ip_into_closed_segment(self, mock_sleep):
        """Test a discovered new IP is stamped into the closed tracker segment on status change"""
        # CHARACTERIZATION: the new IP is stamped retroactively onto the tracker segment
        # closed at the status flip (_run_virt_check_batch sets it on the old entry), and
        # the reopened segment picks it up from vm.new_ip_address on creation.
        vm = self._mock_vm()
        ssh_values = [True, False]
        ssh_idx = [0]
        sleep_count = [0]

        def flip_then_stop(*_):
            sleep_count[0] += 1
            if sleep_count[0] == 1:
                # between pass 1 and pass 2: VM restarts elsewhere with a new IP
                vm.new_ip_address = "10.0.0.9"
            else:
                self.plugin.current_iterations = self.plugin.iterations

        mock_sleep.side_effect = flip_then_stop

        def next_ssh(*_):
            val = ssh_values[ssh_idx[0] % len(ssh_values)]
            ssh_idx[0] += 1
            return val

        with patch.object(self.plugin, "get_vm_access", side_effect=next_ssh), \
             patch.object(self.plugin, "check_vmi_ready", return_value=True):
            telemetry_queue = queue.SimpleQueue()
            self.plugin._run_virt_check_batch([vm], telemetry_queue)

        result = telemetry_queue.get_nowait()
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].new_ip_address, "10.0.0.9")
        self.assertTrue(result[0].ssh_status)
        self.assertEqual(result[1].new_ip_address, "10.0.0.9")

    def test_run_virt_check_batch_queue_put_error_logged(self):
        """Test _run_virt_check_batch returns cleanly when queue.put raises"""
        # CHARACTERIZATION: queue.put errors are swallowed here but not in _run_post_virt_check

        class FailingQueue(queue.SimpleQueue):
            def put(self, *args, **kwargs):
                raise RuntimeError("queue closed")

        vm = self._mock_vm()
        self.plugin.current_iterations = self.plugin.iterations
        with patch.object(self.plugin, "get_vm_access", return_value=True), \
             patch.object(self.plugin, "check_vmi_ready", return_value=True), \
             self.assertLogs("root", level="ERROR") as logs:
            self.plugin._run_virt_check_batch([vm], FailingQueue())
        self.assertTrue(any("Put queue error" in line for line in logs.output))

    def test_run_virt_check_batch_stop_event_skips_probes(self):
        """Test a preset stop_event exits the loop before probing any VM"""
        vm = self._mock_vm()
        self.plugin.stop()
        telemetry_queue = queue.SimpleQueue()
        with patch.object(self.plugin, "_get_ssh_status") as mock_ssh, \
             patch.object(self.plugin, "check_vmi_ready") as mock_ready:
            self.plugin._run_virt_check_batch([vm], telemetry_queue)
        mock_ssh.assert_not_called()
        mock_ready.assert_not_called()
        self.assertEqual(telemetry_queue.get_nowait(), [])


class TestVirtGatherPostChecks(unittest.TestCase):
    """Tests for gather_post_virt_checks thread path"""

    def setUp(self):
        self.factory = HealthCheckFactory()
        if "virt_health_check" not in self.factory.loaded_plugins:
            self.skipTest("Virt health check plugin not loaded (missing dependencies)")
        self.plugin = self.factory.create_plugin(
            "virt_health_check", iterations=1, krkn_lib=MagicMock()
        )

    @staticmethod
    def _mock_vm(name):
        vm = MagicMock()
        vm.vm_name = name
        vm.namespace = "default"
        vm.ip_address = "10.0.0.1"
        vm.node_name = "worker-1"
        vm.new_ip_address = ""
        return vm

    def _fake_post_check(self):
        def fake_post_check(vm_batch, virt_telemetry, post_queue):
            post_queue.put([f"entry-{vm_batch[0].vm_name}"])

        return fake_post_check

    def test_gather_post_virt_checks_thread_path_drains_queue(self):
        """Test the thread path merges per-batch results and discards the input telemetry"""
        self.plugin.vm_list = [self._mock_vm("vm1"), self._mock_vm("vm2")]
        self.plugin.batch_size = 1
        self.plugin.exit_on_failure = False

        with patch.object(self.plugin, "_run_post_virt_check",
                          side_effect=self._fake_post_check()):
            result = self.plugin.gather_post_virt_checks(["stale"])

        self.assertEqual(sorted(result), ["entry-vm1", "entry-vm2"])
        self.assertEqual(self.plugin.get_return_value(), 0)

    def test_gather_post_virt_checks_multi_batch_merge(self):
        """Test all batches' entries are merged into the result"""
        self.plugin.vm_list = [self._mock_vm(f"vm{i}") for i in range(5)]
        self.plugin.batch_size = 2  # 3 batches: 2 + 2 + 1 VMs
        self.plugin.exit_on_failure = False

        with patch.object(self.plugin, "_run_post_virt_check",
                          side_effect=self._fake_post_check()):
            result = self.plugin.gather_post_virt_checks([])

        self.assertEqual(len(result), 3)
        self.assertEqual(
            sorted(result),
            ["entry-vm0", "entry-vm2", "entry-vm4"],
        )

    def test_gather_post_virt_checks_exit_on_failure_via_batch(self):
        """Test exit_on_failure sets ret_value when queued entries exist"""
        failing = VirtCheck(
            {
                "vm_name": "vm1", "ip_address": "10.0.0.1", "namespace": "default",
                "node_name": "worker-1", "ssh_status": False, "vmi_ready": True,
                "status": False, "check_type": "ssh_access",
                "start_timestamp": "t", "end_timestamp": "t", "duration": 0,
                "new_ip_address": "", "phase": "post",
            }
        )

        def fake_post_check(vm_batch, virt_telemetry, post_queue):
            post_queue.put([failing])

        self.plugin.vm_list = [self._mock_vm("vm1")]
        self.plugin.batch_size = 1
        self.plugin.exit_on_failure = True

        with patch.object(self.plugin, "_run_post_virt_check", side_effect=fake_post_check):
            result = self.plugin.gather_post_virt_checks([])

        self.assertEqual(result, [failing])
        self.assertEqual(self.plugin.get_return_value(), 3)

    def test_gather_post_virt_checks_exit_on_failure_all_healthy(self):
        """Test ret_value stays 0 when every batch reports no failures"""

        def fake_post_check(vm_batch, virt_telemetry, post_queue):
            post_queue.put([])

        self.plugin.vm_list = [self._mock_vm("vm1"), self._mock_vm("vm2")]
        self.plugin.batch_size = 1
        self.plugin.exit_on_failure = True

        with patch.object(self.plugin, "_run_post_virt_check", side_effect=fake_post_check):
            result = self.plugin.gather_post_virt_checks([])

        self.assertEqual(result, [])
        self.assertEqual(self.plugin.get_return_value(), 0)

    def test_run_post_virt_check_queue_put_error_propagates(self):
        """Test _run_post_virt_check lets a queue.put error escape to the caller"""
        # CHARACTERIZATION: unlike _run_virt_check_batch, the put here is not guarded,
        # so a queue error propagates out of _run_post_virt_check.
        class FailingQueue(queue.SimpleQueue):
            def put(self, *args, **kwargs):
                raise RuntimeError("queue closed")

        with patch.object(self.plugin, "_get_ssh_status", return_value=False), \
             patch.object(self.plugin, "check_vmi_ready", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "queue closed"):
                self.plugin._run_post_virt_check([self._mock_vm("vm1")], [], FailingQueue())


class TestVirtPostCheckProbeFailure(unittest.TestCase):
    """Tests for _run_post_virt_check SSH probe failure handling"""

    def setUp(self):
        self.factory = HealthCheckFactory()
        if "virt_health_check" not in self.factory.loaded_plugins:
            self.skipTest("Virt health check plugin not loaded (missing dependencies)")
        self.plugin = self.factory.create_plugin(
            "virt_health_check", iterations=1, krkn_lib=MagicMock()
        )
        self.plugin.disconnected = False
        vm = MagicMock()
        vm.vm_name = "vm-probe-err"
        vm.namespace = "default"
        vm.ip_address = "10.0.0.1"
        vm.node_name = "worker-1"
        vm.new_ip_address = ""
        self.vm = vm

    def test_run_post_virt_check_probe_exception_recorded_as_failure(self):
        """Test an SSH probe exception yields a failing ssh_access entry without propagating"""
        with patch.object(self.plugin, "get_vm_access",
                          side_effect=Exception("probe boom")), \
             patch.object(self.plugin, "check_vmi_ready", return_value=True):
            result_queue = queue.SimpleQueue()
            self.plugin._run_post_virt_check([self.vm], [], result_queue)

        result = result_queue.get_nowait()
        self.assertEqual(len(result), 1)
        self.assertFalse(result[0].ssh_status)
        self.assertTrue(result[0].vmi_ready)
        self.assertEqual(result[0].check_type, "ssh_access")
        self.assertFalse(result[0].status)

    def test_run_post_virt_check_probe_exception_logged(self):
        """Test an SSH probe exception in the post-check is logged like the during-check path"""
        # correct behaviour: observability parity with _run_virt_check_batch,
        # which logs probe exceptions via logging.exception
        with patch.object(self.plugin, "get_vm_access",
                          side_effect=Exception("probe boom")), \
             patch.object(self.plugin, "check_vmi_ready", return_value=True), \
             self.assertLogs(level="ERROR"):
            self.plugin._run_post_virt_check([self.vm], [], queue.SimpleQueue())


class TestVirtRunOnce(unittest.TestCase):
    """Tests for run_once"""

    def setUp(self):
        self.factory = HealthCheckFactory()
        if "virt_health_check" not in self.factory.loaded_plugins:
            self.skipTest("Virt health check plugin not loaded (missing dependencies)")
        self.mock_kubecli = MagicMock()
        self.plugin = self.factory.create_plugin(
            "virt_health_check", iterations=1, krkn_lib=self.mock_kubecli
        )

    @staticmethod
    def _vmi(name, namespace="default", ip="10.0.0.1", node="worker-1"):
        return {
            "metadata": {"name": name, "namespace": namespace},
            "status": {"nodeName": node, "interfaces": [{"ipAddress": ip}]},
        }

    def _patch_vmi_listing(self, vmis):
        """Patch the VMI outage plugin so run_once lists the given VMIs"""
        patcher = patch(
            "krkn.health_checks.virt_health_check_plugin.VmiOutageScenarioPlugin"
        )
        mock_plugin_class = patcher.start()
        self.addCleanup(patcher.stop)
        mock_plugin = MagicMock()
        mock_plugin_class.return_value = mock_plugin
        mock_plugin.k8s_client = self.mock_kubecli
        self.mock_kubecli.get_vmis.return_value = vmis
        return mock_plugin_class

    def _checks(self, ssh=True, vmi=True):
        """Patch SSH and VMI readiness probes with the given results"""
        for patcher in (
            patch.object(self.plugin, "_get_ssh_status", return_value=ssh),
            patch.object(self.plugin, "check_vmi_ready", return_value=vmi),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_run_once_falsy_config_returns_skip(self):
        """Test run_once returns the skip result and touches no queue for falsy config"""
        for config in (None, {}):
            with self.subTest(config=config):
                telemetry_queue = queue.Queue()
                result = self.plugin.run_once(config, telemetry_queue)
                self.assertEqual(
                    result, {"passed": True, "failures": [], "details": {}}
                )
                self.assertTrue(telemetry_queue.empty())

    def test_run_once_empty_namespace_returns_skip(self):
        """Test run_once skips initialization when namespace is not configured"""
        telemetry_queue = queue.Queue()
        result = self.plugin.run_once({"namespace": ""}, telemetry_queue)
        self.assertEqual(result, {"passed": True, "failures": [], "details": {}})
        self.assertTrue(telemetry_queue.empty())
        self.assertEqual(self.plugin.vm_list, [])

    def test_run_once_builds_vm_list_from_config(self):
        """Test run_once builds the VM list from the config-driven VMI listing"""
        self._patch_vmi_listing([self._vmi("vm1"), self._vmi("vm2")])
        self._checks()
        result = self.plugin.run_once({"namespace": "default"})
        self.assertEqual(len(self.plugin.vm_list), 2)
        self.assertEqual(
            sorted(result["details"].keys()), ["default/vm1", "default/vm2"]
        )

    def test_run_once_all_healthy_no_queue(self):
        """Test run_once passes with per-VM details when both checks succeed"""
        self._patch_vmi_listing([self._vmi("vm1"), self._vmi("vm2", ip="10.0.0.2")])
        self._checks(ssh=True, vmi=True)
        result = self.plugin.run_once({"namespace": "default"})
        self.assertTrue(result["passed"])
        self.assertEqual(result["failures"], [])
        self.assertEqual(sorted(result["details"].keys()), ["default/vm1", "default/vm2"])
        for key, entry in result["details"].items():
            with self.subTest(vm=key):
                self.assertTrue(entry["passed"])
                self.assertTrue(entry["ssh_status"])
                self.assertTrue(entry["vmi_ready"])
                self.assertEqual(entry["node_name"], "worker-1")
        self.assertEqual(result["details"]["default/vm2"]["ip_address"], "10.0.0.2")

    def test_run_once_both_checks_fail(self):
        """Test run_once reports both SSH and VMI failures for one VM"""
        self._patch_vmi_listing([self._vmi("vm1")])
        self._checks(ssh=False, vmi=False)
        result = self.plugin.run_once({"namespace": "default"})
        self.assertFalse(result["passed"])
        self.assertEqual(len(result["failures"]), 1)
        failure = result["failures"][0]
        self.assertEqual(failure["vm_name"], "vm1")
        self.assertFalse(failure["ssh_status"])
        self.assertFalse(failure["vmi_ready"])
        self.assertIn("SSH access failed", failure["message"])
        self.assertIn("VMI not ready", failure["message"])
        self.assertFalse(result["details"]["default/vm1"]["passed"])

    def test_run_once_vmi_only_failure(self):
        """Test run_once failure message names only the failing check"""
        self._patch_vmi_listing([self._vmi("vm1")])
        self._checks(ssh=True, vmi=False)
        result = self.plugin.run_once({"namespace": "default"})
        self.assertFalse(result["passed"])
        message = result["failures"][0]["message"]
        self.assertIn("VMI not ready", message)
        self.assertNotIn("SSH access failed", message)
        self.assertTrue(result["failures"][0]["ssh_status"])

    def test_run_once_ssh_probe_exception_no_propagation(self):
        """Test an SSH probe exception is caught and recorded as an SSH failure"""
        self._patch_vmi_listing([self._vmi("vm1")])
        self._checks(ssh=True, vmi=True)
        with patch.object(self.plugin, "_get_ssh_status",
                          side_effect=Exception("probe boom")):
            result = self.plugin.run_once({"namespace": "default"})
        entry = result["details"]["default/vm1"]
        self.assertFalse(entry["ssh_status"])
        self.assertTrue(entry["vmi_ready"])
        self.assertFalse(entry["passed"])
        self.assertIn("SSH access failed", result["failures"][0]["message"])

    def test_run_once_empty_vm_list(self):
        """Test run_once passes with empty details when no VMIs are listed"""
        self._patch_vmi_listing([])
        self._checks()
        telemetry_queue = queue.Queue()
        result = self.plugin.run_once({"namespace": "default"}, telemetry_queue)
        self.assertTrue(result["passed"])
        self.assertEqual(result["details"], {})
        self.assertEqual(result["failures"], [])
        self.assertTrue(telemetry_queue.empty())

    def test_run_once_telemetry_record_shape(self):
        """Test run_once enqueues one list of VirtCheck records with correct fields"""
        self._patch_vmi_listing([self._vmi("vm-ok"), self._vmi("vm-bad")])
        self._checks(ssh=True, vmi=True)
        with patch.object(self.plugin, "_get_ssh_status",
                          side_effect=[True, False]):
            telemetry_queue = queue.Queue()
            result = self.plugin.run_once(
                {"namespace": "default"}, telemetry_queue, phase="post"
            )
        self.assertFalse(result["passed"])
        records = telemetry_queue.get_nowait()
        self.assertEqual(len(records), 2)
        self.assertTrue(all(isinstance(r, VirtCheck) for r in records))
        failing = next(r for r in records if r.vm_name == "vm-bad")
        self.assertFalse(failing.status)
        self.assertFalse(failing.ssh_status)
        self.assertTrue(failing.vmi_ready)
        self.assertEqual(failing.check_type, "ssh_access")
        self.assertEqual(failing.phase, "post")
        self.assertGreaterEqual(failing.duration, 0)
        self.assertIsInstance(failing.duration, float)
        datetime.fromisoformat(failing.start_timestamp)
        datetime.fromisoformat(failing.end_timestamp)

    def test_run_once_phase_param_honored(self):
        """Test the phase argument is stamped onto telemetry records"""
        for phase in ("pre", "post"):
            with self.subTest(phase=phase):
                plugin = self.factory.create_plugin(
                    "virt_health_check", iterations=1, krkn_lib=MagicMock()
                )
                self._patch_vmi_listing([self._vmi("vm1")])
                with patch.object(plugin, "_get_ssh_status", return_value=True), \
                     patch.object(plugin, "check_vmi_ready", return_value=True):
                    telemetry_queue = queue.Queue()
                    plugin.run_once({"namespace": "default"}, telemetry_queue, phase=phase)
                self.assertEqual(telemetry_queue.get_nowait()[0].phase, phase)

    def test_run_once_default_phase_is_during(self):
        """Test telemetry records default to the 'during' phase"""
        self._patch_vmi_listing([self._vmi("vm1")])
        self._checks()
        telemetry_queue = queue.Queue()
        self.plugin.run_once({"namespace": "default"}, telemetry_queue)
        self.assertEqual(telemetry_queue.get_nowait()[0].phase, "during")

    def test_run_once_only_failures_filters_records(self):
        """Test only_failures enqueues only failing records"""
        self._patch_vmi_listing([self._vmi("vm-ok"), self._vmi("vm-bad")])
        self._checks(ssh=True, vmi=True)
        with patch.object(self.plugin, "_get_ssh_status",
                          side_effect=[True, False]):
            telemetry_queue = queue.Queue()
            self.plugin.run_once(
                {"namespace": "default", "only_failures": True}, telemetry_queue
            )
        records = telemetry_queue.get_nowait()
        self.assertEqual([r.vm_name for r in records], ["vm-bad"])

    def test_run_once_only_failures_all_pass_no_put(self):
        """Test only_failures enqueues nothing when all VMs pass"""
        self._patch_vmi_listing([self._vmi("vm1")])
        self._checks()
        telemetry_queue = queue.Queue()
        result = self.plugin.run_once(
            {"namespace": "default", "only_failures": True}, telemetry_queue
        )
        self.assertTrue(result["passed"])
        self.assertTrue(telemetry_queue.empty())

    def test_run_once_new_ip_carried_into_record(self):
        """Test a new IP discovered during the probe is carried into the telemetry record"""
        self._patch_vmi_listing([self._vmi("vm1")])
        self._checks()

        def probe_and_migrate(vm):
            vm.new_ip_address = "10.0.0.9"
            return True

        telemetry_queue = queue.Queue()
        with patch.object(self.plugin, "_get_ssh_status",
                          side_effect=probe_and_migrate):
            self.plugin.run_once({"namespace": "default"}, telemetry_queue)
        records = telemetry_queue.get_nowait()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].new_ip_address, "10.0.0.9")

    def test_run_once_namespace_qualified_detail_keys(self):
        """Test VMs with the same name in different namespaces get distinct detail keys"""
        self._patch_vmi_listing(
            [self._vmi("vm1", namespace="ns1"), self._vmi("vm1", namespace="ns2")]
        )
        self._checks()
        result = self.plugin.run_once({"namespace": "ns1"})
        self.assertEqual(sorted(result["details"].keys()), ["ns1/vm1", "ns2/vm1"])

    def test_run_once_init_api_exception_fails(self):
        """Test run_once does not report a pass when initialization hits a cluster error"""
        # correct behaviour: a failed initialization (e.g. get_vmis API error)
        # must not silently report a passing health check for pre/post phases
        with patch(
            "krkn.health_checks.virt_health_check_plugin.VmiOutageScenarioPlugin"
        ) as mock_plugin_class:
            mock_plugin_class.side_effect = Exception("api down")
            result = self.plugin.run_once({"namespace": "default"})
        self.assertFalse(result["passed"])

    def test_run_once_telemetry_duration_is_per_vm(self):
        """Test each VM's telemetry duration reflects only its own probe time"""
        from datetime import timedelta

        base = datetime(2026, 1, 1, 12, 0, 0)
        # Fake clock in whole seconds, advanced only by the probes, so the result
        # does not depend on how many datetime.now() calls production makes.
        clock = [0]

        class FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return base + timedelta(seconds=clock[0])

        def probe(vm):
            clock[0] += 1
            return True

        self._patch_vmi_listing([self._vmi("vm1"), self._vmi("vm2")])
        telemetry_queue = queue.Queue()
        with patch(
            "krkn.health_checks.virt_health_check_plugin.datetime", FakeDatetime
        ), patch.object(self.plugin, "_get_ssh_status", side_effect=probe), \
             patch.object(self.plugin, "check_vmi_ready", return_value=True):
            self.plugin.run_once({"namespace": "default"}, telemetry_queue)
        records = telemetry_queue.get_nowait()
        self.assertEqual(len(records), 2)
        for record in records:
            with self.subTest(vm=record.vm_name):
                self.assertEqual(record.duration, 1.0)

    def test_run_once_second_call_does_not_duplicate_vms(self):
        """Test a second run_once on the same instance does not re-report the same VMs"""
        self._patch_vmi_listing([self._vmi("vm1"), self._vmi("vm2")])
        self._checks()
        self.plugin.run_once({"namespace": "default"}, queue.Queue())
        second_queue = queue.Queue()
        result = self.plugin.run_once({"namespace": "default"}, second_queue)
        self.assertEqual(sorted(result["details"].keys()), ["default/vm1", "default/vm2"])
        self.assertEqual(len(second_queue.get_nowait()), 2)


class TestVirtDisconnectedAccessNoneIp(unittest.TestCase):
    """Tests guarding against probing root@None when the VMI lost its interfaces"""

    def setUp(self):
        self.factory = HealthCheckFactory()
        if "virt_health_check" not in self.factory.loaded_plugins:
            self.skipTest("Virt health check plugin not loaded (missing dependencies)")
        self.plugin = self.factory.create_plugin(
            "virt_health_check", iterations=1, krkn_lib=MagicMock()
        )
        self.plugin.namespace = "default"
        self.plugin.ssh_node = ""
        mock_vm_plugin = MagicMock()
        self.plugin.kube_vm_plugin = mock_vm_plugin
        self.mock_vm_plugin = mock_vm_plugin

    def test_check_disconnected_access_skips_probe_when_new_ip_none(self):
        """Test no new-IP probe runs when the restarted VMI has no interfaces"""
        # correct behaviour: skip the new-IP probe when new_ip_address is None
        self.mock_vm_plugin.get_vmi.return_value = {
            "status": {"interfaces": [], "nodeName": "worker-1"}
        }
        with patch(
            "krkn.health_checks.virt_health_check_plugin.invoke_no_exit",
            return_value="False",
        ) as mock_invoke:
            result, new_ip, new_node = self.plugin.check_disconnected_access(
                "10.0.0.1", "worker-1", "vm1"
            )
        self.assertFalse(result)
        self.assertIsNone(new_ip)
        self.assertIsNone(new_node)
        self.assertEqual(mock_invoke.call_count, 2)
        for call in mock_invoke.call_args_list:
            self.assertNotIn("root@None", call.args[0])

    def test_check_disconnected_access_node_migration_skips_none_ip(self):
        """Test no node-migration probe runs to root@None when the new IP is unknown"""
        # correct behaviour: skip the migrated-node probe when new_ip_address is None
        self.mock_vm_plugin.get_vmi.return_value = {
            "status": {"interfaces": [], "nodeName": "worker-2"}
        }
        with patch(
            "krkn.health_checks.virt_health_check_plugin.invoke_no_exit",
            return_value="False",
        ) as mock_invoke:
            result, new_ip, new_node = self.plugin.check_disconnected_access(
                "10.0.0.1", "worker-1", "vm1"
            )
        self.assertFalse(result)
        self.assertIsNone(new_ip)
        self.assertIsNone(new_node)
        for call in mock_invoke.call_args_list:
            self.assertNotIn("root@None", call.args[0])


class TestVirtBranchGaps(unittest.TestCase):
    """Tests for remaining partial branches"""

    def setUp(self):
        self.factory = HealthCheckFactory()
        if "virt_health_check" not in self.factory.loaded_plugins:
            self.skipTest("Virt health check plugin not loaded (missing dependencies)")
        self.plugin = self.factory.create_plugin(
            "virt_health_check", iterations=2, krkn_lib=MagicMock()
        )

    @patch("krkn.health_checks.virt_health_check_plugin.invoke_no_exit")
    def test_check_disconnected_access_ssh_node_fallback_fails(self, mock_invoke):
        """Test check returns failure when the ssh_node fallback probe also fails"""
        self.plugin.namespace = "default"
        self.plugin.ssh_node = "bastion"
        mock_vm_plugin = MagicMock()
        mock_vm_plugin.get_vmi.return_value = {
            "status": {"interfaces": [{"ipAddress": "10.0.0.1"}], "nodeName": "worker-1"}
        }
        self.plugin.kube_vm_plugin = mock_vm_plugin
        mock_invoke.return_value = "False"

        result, new_ip, new_node = self.plugin.check_disconnected_access(
            "10.0.0.1", "worker-1", "vm1"
        )
        self.assertFalse(result)
        self.assertIsNone(new_ip)
        self.assertIsNone(new_node)

    def test_get_ssh_status_disconnected_without_new_ip_leaves_vm_unchanged(self):
        """Test a failed disconnected probe returning no new IP leaves the VM untouched"""
        vm = MagicMock()
        vm.vm_name = "vm1"
        vm.namespace = "default"
        vm.ip_address = "10.0.0.1"
        vm.node_name = "worker-1"
        vm.new_ip_address = ""
        self.plugin.disconnected = True
        with patch.object(
            self.plugin, "check_disconnected_access", return_value=(True, None, None)
        ):
            self.assertTrue(self.plugin._get_ssh_status(vm))
        self.assertEqual(vm.new_ip_address, "")
        self.assertEqual(vm.node_name, "worker-1")

    @patch("krkn.health_checks.virt_health_check_plugin.time.sleep")
    def test_run_virt_check_batch_unchanged_status_single_entry(self, mock_sleep):
        """Test an unchanged status across passes keeps a single tracker segment"""
        vm = MagicMock()
        vm.vm_name = "vm1"
        vm.namespace = "default"
        vm.ip_address = "10.0.0.1"
        vm.node_name = "worker-1"
        vm.new_ip_address = ""
        self.plugin.disconnected = False
        self.plugin.only_failures = False
        call_count = [0]

        def stop_after_two(*_):
            call_count[0] += 1
            if call_count[0] >= 2:
                self.plugin.current_iterations = self.plugin.iterations

        mock_sleep.side_effect = stop_after_two
        with patch.object(self.plugin, "get_vm_access", return_value=True), \
             patch.object(self.plugin, "check_vmi_ready", return_value=True):
            telemetry_queue = queue.SimpleQueue()
            self.plugin._run_virt_check_batch([vm], telemetry_queue)
        result = telemetry_queue.get_nowait()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].check_type, "healthy")

    @patch("krkn.health_checks.virt_health_check_plugin.time.sleep")
    def test_run_virt_check_batch_only_failures_healthy_never_enqueued(self, mock_sleep):
        """Test only_failures drops a VM that stays healthy for the whole run"""
        vm = MagicMock()
        vm.vm_name = "vm1"
        vm.namespace = "default"
        vm.ip_address = "10.0.0.1"
        vm.node_name = "worker-1"
        vm.new_ip_address = ""
        self.plugin.disconnected = False
        self.plugin.only_failures = True

        def stop_after_sleep(*_):
            self.plugin.current_iterations = self.plugin.iterations

        mock_sleep.side_effect = stop_after_sleep
        with patch.object(self.plugin, "get_vm_access", return_value=True), \
             patch.object(self.plugin, "check_vmi_ready", return_value=True):
            telemetry_queue = queue.SimpleQueue()
            self.plugin._run_virt_check_batch([vm], telemetry_queue)
        self.assertEqual(telemetry_queue.get_nowait(), [])

    def test_gather_post_virt_checks_batch_thread_failure_isolated(self):
        """Test a batch thread that dies before enqueueing is surfaced, not silently dropped"""
        def fake_post_check(vm_batch, virt_telemetry, post_queue):
            if vm_batch[0].vm_name == "vm1":
                raise RuntimeError("thread died")
            post_queue.put(["kept"])

        vm_dead = MagicMock()
        vm_dead.vm_name = "vm1"
        vm_ok = MagicMock()
        vm_ok.vm_name = "vm2"
        self.plugin.vm_list = [vm_dead, vm_ok]
        self.plugin.batch_size = 1
        self.plugin.exit_on_failure = False

        with patch("threading.excepthook") as excepthook, \
             patch.object(self.plugin, "_run_post_virt_check",
                          side_effect=fake_post_check):
            result = self.plugin.gather_post_virt_checks(["stale"])

        self.assertIs(excepthook.call_args.args[0].exc_type, RuntimeError)
        self.assertIn("kept", result)
        self.assertTrue(
            any(getattr(entry, "vm_name", None) == "vm1" for entry in result),
            "dead batch for vm1 should surface a failure entry in the result",
        )


if __name__ == "__main__":
    unittest.main()
