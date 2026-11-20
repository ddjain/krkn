#!/usr/bin/env python3

"""
Test suite for VMWare node scenarios

This test suite covers both the VMWare class and vmware_node_scenarios class
using mocks to avoid actual VMWare CLI calls.

Usage:
    python -m coverage run -a -m unittest tests/test_vmware_node_scenarios.py -v

Assisted By: Claude Code
"""


import unittest
from unittest.mock import MagicMock, patch, PropertyMock, call
from krkn.scenario_plugins.node_actions.vmware_node_scenarios import vmware_node_scenarios, vSphere
from krkn_lib.models.k8s import AffectedNodeStatus
from com.vmware.vcenter.vm_client import Power

class TestVmwareNodeScenarios(unittest.TestCase):

    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.vSphere')
    def setUp(self, mock_vsphere_class):
        # Mock the configuration and dependencies
        self.mock_kubecli = MagicMock()
        self.mock_affected_nodes_status = AffectedNodeStatus()
        self.mock_vsphere = MagicMock()
        mock_vsphere_class.return_value = self.mock_vsphere

        # Initialize the scenario class
        self.vmware_scenarios = vmware_node_scenarios(
            kubecli=self.mock_kubecli,
            node_action_kube_check=False,
            affected_nodes_status=self.mock_affected_nodes_status
        )

    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.vSphere')
    def test_reboot_node_success(self, mock_vsphere_class):
        """Test successful node reboot."""
        node_name = "test-node-01"
        mock_vsphere = MagicMock()
        mock_vsphere_class.return_value = mock_vsphere
        mock_vsphere.reboot_instances.return_value = True

        # Create a fresh instance with mocked vSphere
        scenarios = vmware_node_scenarios(
            kubecli=self.mock_kubecli,
            node_action_kube_check=False,
            affected_nodes_status=AffectedNodeStatus()
        )

        # Execute the reboot scenario
        scenarios.node_reboot_scenario(
            instance_kill_count=1,
            node=node_name,
            timeout=300
        )

        # Assertions
        mock_vsphere.reboot_instances.assert_called_with(node_name)

    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.vSphere')
    def test_node_not_found(self, mock_vsphere_class):
        """Test behavior when the VM does not exist in vCenter."""
        node_name = "non-existent-node"
        mock_vsphere = MagicMock()
        mock_vsphere_class.return_value = mock_vsphere
        mock_vsphere.get_vm.return_value = None
        mock_vsphere.reboot_instances.side_effect = Exception(f"VM {node_name} not found")

        scenarios = vmware_node_scenarios(
            kubecli=self.mock_kubecli,
            node_action_kube_check=False,
            affected_nodes_status=AffectedNodeStatus()
        )

        # This should handle the exception gracefully (just log it)
        scenarios.node_reboot_scenario(
            instance_kill_count=1,
            node=node_name,
            timeout=300
        )

    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.vSphere')
    def test_stop_start_node(self, mock_vsphere_class):
        """Test stopping and then starting a node."""
        node_name = "test-node-02"
        mock_vsphere = MagicMock()
        mock_vsphere_class.return_value = mock_vsphere
        mock_vsphere.stop_instances.return_value = True
        mock_vsphere.start_instances.return_value = True
        mock_vsphere.wait_until_stopped.return_value = True
        mock_vsphere.wait_until_running.return_value = True

        scenarios = vmware_node_scenarios(
            kubecli=self.mock_kubecli,
            node_action_kube_check=False,
            affected_nodes_status=AffectedNodeStatus()
        )

        # Test stop scenario
        scenarios.node_stop_scenario(
            instance_kill_count=1,
            node=node_name,
            timeout=300,
            poll_interval=5
        )
        mock_vsphere.stop_instances.assert_called_with(node_name)

        # Test start scenario
        scenarios.node_start_scenario(
            instance_kill_count=1,
            node=node_name,
            timeout=300,
            poll_interval=5
        )
        mock_vsphere.start_instances.assert_called_with(node_name)

    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.vSphere')
    def test_vcenter_connection_failure(self, mock_vsphere_class):
        """Test scenario where connection to vCenter fails."""
        # Force the vSphere init to raise an exception
        mock_vsphere_class.side_effect = Exception("Connection Refused")

        with self.assertRaises(Exception):
            vmware_node_scenarios(
                kubecli=self.mock_kubecli,
                node_action_kube_check=False,
                affected_nodes_status=AffectedNodeStatus()
            )

    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.vSphere')
    def test_node_terminate_scenario(self, mock_vsphere_class):
        """Test node termination scenario."""
        node_name = "test-node-terminate"
        mock_vsphere = MagicMock()
        mock_vsphere_class.return_value = mock_vsphere
        mock_vsphere.stop_instances.return_value = True
        mock_vsphere.wait_until_stopped.return_value = True
        mock_vsphere.wait_until_released.return_value = True

        scenarios = vmware_node_scenarios(
            kubecli=self.mock_kubecli,
            node_action_kube_check=False,
            affected_nodes_status=AffectedNodeStatus()
        )

        # Execute terminate scenario
        scenarios.node_terminate_scenario(
            instance_kill_count=1,
            node=node_name,
            timeout=300,
            poll_interval=5
        )

        # Verify the sequence of calls
        mock_vsphere.stop_instances.assert_called_with(node_name)
        mock_vsphere.wait_until_stopped.assert_called_once()
        mock_vsphere.release_instances.assert_called_with(node_name)
        mock_vsphere.wait_until_released.assert_called_once()

    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.vSphere')
    def test_node_already_stopped(self, mock_vsphere_class):
        """Test scenario when node is already in the stopped state."""
        node_name = "already-stopped-node"
        mock_vsphere = MagicMock()
        mock_vsphere_class.return_value = mock_vsphere
        # Return False indicating VM is already stopped
        mock_vsphere.stop_instances.return_value = False

        scenarios = vmware_node_scenarios(
            kubecli=self.mock_kubecli,
            node_action_kube_check=False,
            affected_nodes_status=AffectedNodeStatus()
        )

        scenarios.node_stop_scenario(
            instance_kill_count=1,
            node=node_name,
            timeout=300,
            poll_interval=5
        )

        # Should still call stop_instances but not wait_until_stopped
        mock_vsphere.stop_instances.assert_called_with(node_name)
        mock_vsphere.wait_until_stopped.assert_not_called()

    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.vSphere')
    def test_node_already_started(self, mock_vsphere_class):
        """Test scenario when node is already in the running state."""
        node_name = "already-running-node"
        mock_vsphere = MagicMock()
        mock_vsphere_class.return_value = mock_vsphere
        # Return False indicating VM is already running
        mock_vsphere.start_instances.return_value = False

        scenarios = vmware_node_scenarios(
            kubecli=self.mock_kubecli,
            node_action_kube_check=False,
            affected_nodes_status=AffectedNodeStatus()
        )

        scenarios.node_start_scenario(
            instance_kill_count=1,
            node=node_name,
            timeout=300,
            poll_interval=5
        )

        # Should still call start_instances but not wait_until_running
        mock_vsphere.start_instances.assert_called_with(node_name)
        mock_vsphere.wait_until_running.assert_not_called()

    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.nodeaction')
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.vSphere')
    def test_reboot_with_kube_check(self, mock_vsphere_class, mock_nodeaction):
        """Test reboot scenario with Kubernetes health checks enabled."""
        node_name = "test-node-kube-check"
        mock_vsphere = MagicMock()
        mock_vsphere_class.return_value = mock_vsphere
        mock_vsphere.reboot_instances.return_value = True

        scenarios = vmware_node_scenarios(
            kubecli=self.mock_kubecli,
            node_action_kube_check=True,  # Enable kube checks
            affected_nodes_status=AffectedNodeStatus()
        )

        scenarios.node_reboot_scenario(
            instance_kill_count=1,
            node=node_name,
            timeout=300
        )

        # Verify kube health check was called
        mock_nodeaction.wait_for_unknown_status.assert_called_once()

    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.nodeaction')
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.vSphere')
    def test_start_with_kube_check(self, mock_vsphere_class, mock_nodeaction):
        """Test start scenario with Kubernetes health checks enabled."""
        node_name = "test-node-start-kube"
        mock_vsphere = MagicMock()
        mock_vsphere_class.return_value = mock_vsphere
        mock_vsphere.start_instances.return_value = True
        mock_vsphere.wait_until_running.return_value = True

        scenarios = vmware_node_scenarios(
            kubecli=self.mock_kubecli,
            node_action_kube_check=True,
            affected_nodes_status=AffectedNodeStatus()
        )

        scenarios.node_start_scenario(
            instance_kill_count=1,
            node=node_name,
            timeout=300,
            poll_interval=5
        )

        # Verify both vSphere and kube checks were called
        mock_vsphere.wait_until_running.assert_called_once()
        mock_nodeaction.wait_for_ready_status.assert_called_once()

    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.vSphere')
    def test_multiple_instance_kill_count(self, mock_vsphere_class):
        """Test scenario with multiple instance kill count (loop)."""
        node_name = "test-node-multiple"
        mock_vsphere = MagicMock()
        mock_vsphere_class.return_value = mock_vsphere
        mock_vsphere.reboot_instances.return_value = True

        scenarios = vmware_node_scenarios(
            kubecli=self.mock_kubecli,
            node_action_kube_check=False,
            affected_nodes_status=AffectedNodeStatus()
        )

        # Test with kill count of 3
        scenarios.node_reboot_scenario(
            instance_kill_count=3,
            node=node_name,
            timeout=300
        )

        # Should be called 3 times
        assert mock_vsphere.reboot_instances.call_count == 3

    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.vSphere')
    def test_stop_failure_exception_handling(self, mock_vsphere_class):
        """Test exception handling during node stop."""
        node_name = "failing-node"
        mock_vsphere = MagicMock()
        mock_vsphere_class.return_value = mock_vsphere
        mock_vsphere.stop_instances.side_effect = Exception("vSphere API Error")

        scenarios = vmware_node_scenarios(
            kubecli=self.mock_kubecli,
            node_action_kube_check=False,
            affected_nodes_status=AffectedNodeStatus()
        )

        # Should not raise exception, just log it
        scenarios.node_stop_scenario(
            instance_kill_count=1,
            node=node_name,
            timeout=300,
            poll_interval=5
        )

        # Verify it attempted to stop
        mock_vsphere.stop_instances.assert_called_with(node_name)

    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.vSphere')
    def test_terminate_failure_exception_handling(self, mock_vsphere_class):
        """Test exception handling during node termination."""
        node_name = "terminate-failing-node"
        mock_vsphere = MagicMock()
        mock_vsphere_class.return_value = mock_vsphere
        mock_vsphere.stop_instances.return_value = True
        mock_vsphere.wait_until_stopped.return_value = True
        mock_vsphere.release_instances.side_effect = Exception("Cannot delete VM")

        scenarios = vmware_node_scenarios(
            kubecli=self.mock_kubecli,
            node_action_kube_check=False,
            affected_nodes_status=AffectedNodeStatus()
        )

        # Should not raise exception
        scenarios.node_terminate_scenario(
            instance_kill_count=1,
            node=node_name,
            timeout=300,
            poll_interval=5
        )

        # Verify termination was attempted
        mock_vsphere.release_instances.assert_called_with(node_name)

    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.vSphere')
    def test_affected_nodes_tracking(self, mock_vsphere_class):
        """Test that affected nodes are properly tracked."""
        node_name = "tracked-node"
        mock_vsphere = MagicMock()
        mock_vsphere_class.return_value = mock_vsphere
        mock_vsphere.reboot_instances.return_value = True

        affected_status = AffectedNodeStatus()
        scenarios = vmware_node_scenarios(
            kubecli=self.mock_kubecli,
            node_action_kube_check=False,
            affected_nodes_status=affected_status
        )

        # Verify no affected nodes initially
        assert len(affected_status.affected_nodes) == 0

        scenarios.node_reboot_scenario(
            instance_kill_count=1,
            node=node_name,
            timeout=300
        )

        # Verify affected node was tracked
        assert len(affected_status.affected_nodes) == 1
        assert affected_status.affected_nodes[0].node_name == node_name

    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.vSphere')
    def test_reboot_not_allowed_state(self, mock_vsphere_class):
        """Test reboot when VM is in a state that doesn't allow reboot."""
        node_name = "powered-off-node"
        mock_vsphere = MagicMock()
        mock_vsphere_class.return_value = mock_vsphere
        # Return False indicating reboot failed (VM not powered on)
        mock_vsphere.reboot_instances.return_value = False

        scenarios = vmware_node_scenarios(
            kubecli=self.mock_kubecli,
            node_action_kube_check=False,
            affected_nodes_status=AffectedNodeStatus()
        )

        scenarios.node_reboot_scenario(
            instance_kill_count=1,
            node=node_name,
            timeout=300
        )

        # Should attempt reboot
        mock_vsphere.reboot_instances.assert_called_with(node_name)


class TestVSphereClass(unittest.TestCase):
    """Test suite for the vSphere class."""

    @patch.dict('os.environ', {
        'VSPHERE_IP': '192.168.1.100',
        'VSPHERE_USERNAME': 'admin',
        'VSPHERE_PASSWORD': 'password123'
    })
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.create_vsphere_client')
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.requests.session')
    def test_vsphere_initialization_success(self, mock_session, mock_create_client):
        """Test successful vSphere client initialization."""
        mock_client = MagicMock()
        mock_create_client.return_value = mock_client

        vsphere = vSphere()

        self.assertEqual(vsphere.server, '192.168.1.100')
        self.assertEqual(vsphere.username, 'admin')
        self.assertEqual(vsphere.password, 'password123')
        self.assertTrue(vsphere.credentials_present)
        mock_create_client.assert_called_once()

    @patch.dict('os.environ', {}, clear=True)
    def test_vsphere_initialization_missing_credentials(self):
        """Test vSphere initialization fails when credentials are missing."""
        with self.assertRaises(Exception) as context:
            vSphere()

        self.assertIn("Environmental variables", str(context.exception))

    @patch.dict('os.environ', {
        'VSPHERE_IP': '192.168.1.100',
        'VSPHERE_USERNAME': 'admin',
        'VSPHERE_PASSWORD': 'password123'
    })
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.create_vsphere_client')
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.requests.session')
    def test_get_vm_success(self, mock_session, mock_create_client):
        """Test getting a VM by name."""
        mock_client = MagicMock()
        mock_vm_obj = MagicMock()
        mock_vm_obj.vm = 'vm-123'
        mock_client.vcenter.VM.list.return_value = [mock_vm_obj]
        mock_create_client.return_value = mock_client

        vsphere = vSphere()
        vm_id = vsphere.get_vm('test-vm')

        self.assertEqual(vm_id, 'vm-123')
        mock_client.vcenter.VM.list.assert_called_once()

    @patch.dict('os.environ', {
        'VSPHERE_IP': '192.168.1.100',
        'VSPHERE_USERNAME': 'admin',
        'VSPHERE_PASSWORD': 'password123'
    })
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.create_vsphere_client')
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.requests.session')
    def test_get_vm_not_found(self, mock_session, mock_create_client):
        """Test getting a VM that doesn't exist."""
        mock_client = MagicMock()
        mock_client.vcenter.VM.list.return_value = []
        mock_create_client.return_value = mock_client

        vsphere = vSphere()
        vm_id = vsphere.get_vm('non-existent-vm')

        self.assertIsNone(vm_id)

    @patch.dict('os.environ', {
        'VSPHERE_IP': '192.168.1.100',
        'VSPHERE_USERNAME': 'admin',
        'VSPHERE_PASSWORD': 'password123'
    })
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.create_vsphere_client')
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.requests.session')
    def test_reboot_instances_success(self, mock_session, mock_create_client):
        """Test successful VM reboot."""
        mock_client = MagicMock()
        mock_vm_obj = MagicMock()
        mock_vm_obj.vm = 'vm-123'
        mock_client.vcenter.VM.list.return_value = [mock_vm_obj]
        mock_create_client.return_value = mock_client

        vsphere = vSphere()
        result = vsphere.reboot_instances('test-vm')

        self.assertTrue(result)
        mock_client.vcenter.vm.Power.reset.assert_called_with('vm-123')

    @patch.dict('os.environ', {
        'VSPHERE_IP': '192.168.1.100',
        'VSPHERE_USERNAME': 'admin',
        'VSPHERE_PASSWORD': 'password123'
    })
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.create_vsphere_client')
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.requests.session')
    def test_reboot_instances_not_powered_on(self, mock_session, mock_create_client):
        """Test reboot fails when VM is not powered on."""
        from com.vmware.vapi.std.errors_client import NotAllowedInCurrentState

        mock_client = MagicMock()
        mock_vm_obj = MagicMock()
        mock_vm_obj.vm = 'vm-123'
        mock_client.vcenter.VM.list.return_value = [mock_vm_obj]
        mock_client.vcenter.vm.Power.reset.side_effect = NotAllowedInCurrentState()
        mock_create_client.return_value = mock_client

        vsphere = vSphere()
        result = vsphere.reboot_instances('test-vm')

        self.assertFalse(result)

    @patch.dict('os.environ', {
        'VSPHERE_IP': '192.168.1.100',
        'VSPHERE_USERNAME': 'admin',
        'VSPHERE_PASSWORD': 'password123'
    })
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.create_vsphere_client')
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.requests.session')
    def test_stop_instances_success(self, mock_session, mock_create_client):
        """Test successful VM stop."""
        mock_client = MagicMock()
        mock_vm_obj = MagicMock()
        mock_vm_obj.vm = 'vm-123'
        mock_client.vcenter.VM.list.return_value = [mock_vm_obj]
        mock_create_client.return_value = mock_client

        vsphere = vSphere()
        result = vsphere.stop_instances('test-vm')

        self.assertTrue(result)
        mock_client.vcenter.vm.Power.stop.assert_called_with('vm-123')

    @patch.dict('os.environ', {
        'VSPHERE_IP': '192.168.1.100',
        'VSPHERE_USERNAME': 'admin',
        'VSPHERE_PASSWORD': 'password123'
    })
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.create_vsphere_client')
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.requests.session')
    def test_stop_instances_already_stopped(self, mock_session, mock_create_client):
        """Test stop when VM is already stopped."""
        from com.vmware.vapi.std.errors_client import AlreadyInDesiredState

        mock_client = MagicMock()
        mock_vm_obj = MagicMock()
        mock_vm_obj.vm = 'vm-123'
        mock_client.vcenter.VM.list.return_value = [mock_vm_obj]
        mock_client.vcenter.vm.Power.stop.side_effect = AlreadyInDesiredState()
        mock_create_client.return_value = mock_client

        vsphere = vSphere()
        result = vsphere.stop_instances('test-vm')

        self.assertFalse(result)

    @patch.dict('os.environ', {
        'VSPHERE_IP': '192.168.1.100',
        'VSPHERE_USERNAME': 'admin',
        'VSPHERE_PASSWORD': 'password123'
    })
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.create_vsphere_client')
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.requests.session')
    def test_start_instances_success(self, mock_session, mock_create_client):
        """Test successful VM start."""
        mock_client = MagicMock()
        mock_vm_obj = MagicMock()
        mock_vm_obj.vm = 'vm-123'
        mock_client.vcenter.VM.list.return_value = [mock_vm_obj]
        mock_create_client.return_value = mock_client

        vsphere = vSphere()
        result = vsphere.start_instances('test-vm')

        self.assertTrue(result)
        mock_client.vcenter.vm.Power.start.assert_called_with('vm-123')

    @patch.dict('os.environ', {
        'VSPHERE_IP': '192.168.1.100',
        'VSPHERE_USERNAME': 'admin',
        'VSPHERE_PASSWORD': 'password123'
    })
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.create_vsphere_client')
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.requests.session')
    def test_start_instances_already_started(self, mock_session, mock_create_client):
        """Test start when VM is already running."""
        from com.vmware.vapi.std.errors_client import AlreadyInDesiredState

        mock_client = MagicMock()
        mock_vm_obj = MagicMock()
        mock_vm_obj.vm = 'vm-123'
        mock_client.vcenter.VM.list.return_value = [mock_vm_obj]
        mock_client.vcenter.vm.Power.start.side_effect = AlreadyInDesiredState()
        mock_create_client.return_value = mock_client

        vsphere = vSphere()
        result = vsphere.start_instances('test-vm')

        self.assertFalse(result)

    @patch.dict('os.environ', {
        'VSPHERE_IP': '192.168.1.100',
        'VSPHERE_USERNAME': 'admin',
        'VSPHERE_PASSWORD': 'password123'
    })
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.create_vsphere_client')
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.requests.session')
    def test_get_vm_status(self, mock_session, mock_create_client):
        """Test getting VM status."""
        mock_client = MagicMock()
        mock_vm_obj = MagicMock()
        mock_vm_obj.vm = 'vm-123'
        mock_client.vcenter.VM.list.return_value = [mock_vm_obj]
        mock_power_state = MagicMock()
        mock_power_state.state = Power.State.POWERED_ON
        mock_client.vcenter.vm.Power.get.return_value = mock_power_state
        mock_create_client.return_value = mock_client

        vsphere = vSphere()
        status = vsphere.get_vm_status('test-vm')

        self.assertEqual(status, Power.State.POWERED_ON)

    @patch.dict('os.environ', {
        'VSPHERE_IP': '192.168.1.100',
        'VSPHERE_USERNAME': 'admin',
        'VSPHERE_PASSWORD': 'password123'
    })
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.create_vsphere_client')
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.requests.session')
    def test_get_vm_status_exception(self, mock_session, mock_create_client):
        """Test get_vm_status handles exceptions gracefully."""
        mock_client = MagicMock()
        mock_vm_obj = MagicMock()
        mock_vm_obj.vm = 'vm-123'
        mock_client.vcenter.VM.list.return_value = [mock_vm_obj]
        mock_client.vcenter.vm.Power.get.side_effect = Exception("API Error")
        mock_create_client.return_value = mock_client

        vsphere = vSphere()
        status = vsphere.get_vm_status('test-vm')

        self.assertIsNone(status)

    @patch.dict('os.environ', {
        'VSPHERE_IP': '192.168.1.100',
        'VSPHERE_USERNAME': 'admin',
        'VSPHERE_PASSWORD': 'password123'
    })
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.create_vsphere_client')
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.requests.session')
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.time.sleep')
    def test_wait_until_running(self, mock_sleep, mock_session, mock_create_client):
        """Test waiting for VM to reach POWERED_ON state."""
        mock_client = MagicMock()
        mock_vm_obj = MagicMock()
        mock_vm_obj.vm = 'vm-123'
        mock_client.vcenter.VM.list.return_value = [mock_vm_obj]

        # Simulate VM transitioning to POWERED_ON after 2 checks
        mock_power_states = [
            MagicMock(state=Power.State.POWERED_OFF),
            MagicMock(state=Power.State.POWERED_ON)
        ]
        mock_client.vcenter.vm.Power.get.side_effect = mock_power_states
        mock_create_client.return_value = mock_client

        vsphere = vSphere()
        mock_affected_node = MagicMock()
        result = vsphere.wait_until_running('test-vm', timeout=60, affected_node=mock_affected_node)

        self.assertTrue(result)
        mock_affected_node.set_affected_node_status.assert_called_once()

    @patch.dict('os.environ', {
        'VSPHERE_IP': '192.168.1.100',
        'VSPHERE_USERNAME': 'admin',
        'VSPHERE_PASSWORD': 'password123'
    })
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.create_vsphere_client')
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.requests.session')
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.time.sleep')
    def test_wait_until_stopped(self, mock_sleep, mock_session, mock_create_client):
        """Test waiting for VM to reach POWERED_OFF state."""
        mock_client = MagicMock()
        mock_vm_obj = MagicMock()
        mock_vm_obj.vm = 'vm-123'
        mock_client.vcenter.VM.list.return_value = [mock_vm_obj]

        # Simulate VM transitioning to POWERED_OFF
        mock_power_states = [
            MagicMock(state=Power.State.POWERED_ON),
            MagicMock(state=Power.State.POWERED_OFF)
        ]
        mock_client.vcenter.vm.Power.get.side_effect = mock_power_states
        mock_create_client.return_value = mock_client

        vsphere = vSphere()
        mock_affected_node = MagicMock()
        result = vsphere.wait_until_stopped('test-vm', timeout=60, affected_node=mock_affected_node)

        self.assertTrue(result)

    @patch.dict('os.environ', {
        'VSPHERE_IP': '192.168.1.100',
        'VSPHERE_USERNAME': 'admin',
        'VSPHERE_PASSWORD': 'password123'
    })
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.create_vsphere_client')
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.requests.session')
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.time.sleep')
    def test_wait_until_running_timeout(self, mock_sleep, mock_session, mock_create_client):
        """Test wait_until_running returns False after the timeout."""
        # CHARACTERIZATION: terminates only because the mocked Power.get returns
        # POWERED_ON on the third call. Production does not break after the
        # timeout; see test_wait_until_running_timeout_expected_failure.
        mock_client = MagicMock()
        mock_vm_obj = MagicMock()
        mock_vm_obj.vm = 'vm-123'
        mock_client.vcenter.VM.list.return_value = [mock_vm_obj]

        # VM is POWERED_OFF initially, then transitions to POWERED_ON after timeout to exit loop
        call_count = [0]
        def get_status_side_effect(vm):
            call_count[0] += 1
            # Return POWERED_OFF for first 2 calls (to exceed timeout=2 with 5 second increments)
            # Then return POWERED_ON to exit the loop
            if call_count[0] <= 2:
                return MagicMock(state=Power.State.POWERED_OFF)
            return MagicMock(state=Power.State.POWERED_ON)

        mock_client.vcenter.vm.Power.get.side_effect = get_status_side_effect
        mock_create_client.return_value = mock_client

        vsphere = vSphere()
        mock_affected_node = MagicMock()
        result = vsphere.wait_until_running('test-vm', timeout=2, affected_node=mock_affected_node)

        self.assertFalse(result)

    @patch.dict('os.environ', {
        'VSPHERE_IP': '192.168.1.100',
        'VSPHERE_USERNAME': 'admin',
        'VSPHERE_PASSWORD': 'password123'
    })
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.create_vsphere_client')
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.requests.session')
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.time.sleep')
    def test_wait_until_released(self, mock_sleep, mock_session, mock_create_client):
        """Test waiting for VM to be deleted."""
        mock_client = MagicMock()
        mock_vm_obj = MagicMock()
        mock_vm_obj.vm = 'vm-123'

        # VM exists first, then is deleted
        mock_client.vcenter.VM.list.side_effect = [
            [mock_vm_obj],  # VM exists
            []  # VM deleted
        ]
        mock_create_client.return_value = mock_client

        vsphere = vSphere()
        mock_affected_node = MagicMock()
        result = vsphere.wait_until_released('test-vm', timeout=60, affected_node=mock_affected_node)

        self.assertTrue(result)
        mock_affected_node.set_affected_node_status.assert_called_once()

    @patch.dict('os.environ', {
        'VSPHERE_IP': '192.168.1.100',
        'VSPHERE_USERNAME': 'admin',
        'VSPHERE_PASSWORD': 'password123'
    })
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.create_vsphere_client')
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.requests.session')
    def test_get_datacenter_list(self, mock_session, mock_create_client):
        """Test getting list of datacenters."""
        mock_client = MagicMock()
        mock_dc1 = MagicMock()
        mock_dc1.datacenter = 'dc-1'
        mock_dc1.name = 'Datacenter1'
        mock_dc2 = MagicMock()
        mock_dc2.datacenter = 'dc-2'
        mock_dc2.name = 'Datacenter2'
        mock_client.vcenter.Datacenter.list.return_value = [mock_dc1, mock_dc2]
        mock_create_client.return_value = mock_client

        vsphere = vSphere()
        datacenters = vsphere.get_datacenter_list()

        self.assertEqual(len(datacenters), 2)
        self.assertEqual(datacenters[0]['datacenter_name'], 'Datacenter1')
        self.assertEqual(datacenters[1]['datacenter_name'], 'Datacenter2')

    @patch.dict('os.environ', {
        'VSPHERE_IP': '192.168.1.100',
        'VSPHERE_USERNAME': 'admin',
        'VSPHERE_PASSWORD': 'password123'
    })
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.create_vsphere_client')
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.requests.session')
    def test_release_instances_vm_not_found(self, mock_session, mock_create_client):
        """Test release_instances raises exception when VM not found."""
        mock_client = MagicMock()
        mock_client.vcenter.VM.list.return_value = []
        mock_create_client.return_value = mock_client

        vsphere = vSphere()

        with self.assertRaises(Exception) as context:
            vsphere.release_instances('non-existent-vm')

        self.assertIn("does not exist", str(context.exception))

    @patch.dict('os.environ', {
        'VSPHERE_IP': '192.168.1.100',
        'VSPHERE_USERNAME': 'admin',
        'VSPHERE_PASSWORD': 'password123'
    })
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.create_vsphere_client')
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.requests.session')
    def test_get_unverified_session(self, mock_session_class, mock_create_client):
        """Test creating an unverified session."""
        mock_session_instance = MagicMock()
        mock_session_class.return_value = mock_session_instance
        mock_create_client.return_value = MagicMock()

        vsphere = vSphere()
        session = vsphere.get_unverified_session()

        self.assertFalse(session.verify)
        mock_session_class.assert_called()


class TestVSphereExtended(unittest.TestCase):
    """Extended branch coverage for the vSphere class."""

    ENV = {
        'VSPHERE_IP': '192.168.1.100',
        'VSPHERE_USERNAME': 'admin',
        'VSPHERE_PASSWORD': 'password123',
    }

    def setUp(self):
        env = patch.dict('os.environ', self.ENV)
        env.start()
        self.addCleanup(env.stop)
        session_patcher = patch(
            'krkn.scenario_plugins.node_actions.vmware_node_scenarios.requests.session'
        )
        session_patcher.start()
        self.addCleanup(session_patcher.stop)
        factory_patcher = patch(
            'krkn.scenario_plugins.node_actions.vmware_node_scenarios.create_vsphere_client'
        )
        factory = factory_patcher.start()
        self.addCleanup(factory_patcher.stop)
        self.mock_client = MagicMock()
        factory.return_value = self.mock_client
        self.vsphere = vSphere()

    def _vm(self, vm_id, name=None):
        obj = MagicMock()
        obj.vm = vm_id
        if name is not None:
            obj.name = name
        return obj

    def test_release_instances_powered_on_vm_stops_then_deletes(self):
        """A powered-on VM is stopped before deletion."""
        self.mock_client.vcenter.VM.list.return_value = [self._vm('vm-1')]
        self.mock_client.vcenter.vm.Power.get.return_value = Power.Info(
            state=Power.State.POWERED_ON
        )

        self.vsphere.release_instances('vm-1')

        self.mock_client.vcenter.vm.Power.stop.assert_called_once_with('vm-1')
        self.mock_client.vcenter.VM.delete.assert_called_once_with('vm-1')
        self.mock_client.vcenter.vm.Power.start.assert_not_called()
        call_names = [c[0] for c in self.mock_client.mock_calls]
        self.assertLess(
            call_names.index('vcenter.vm.Power.stop'),
            call_names.index('vcenter.VM.delete'),
        )

    def test_release_instances_suspended_vm_starts_stops_then_deletes(self):
        """A suspended VM is started, stopped and then deleted."""
        self.mock_client.vcenter.VM.list.return_value = [self._vm('vm-1')]
        self.mock_client.vcenter.vm.Power.get.return_value = Power.Info(
            state=Power.State.SUSPENDED
        )

        self.vsphere.release_instances('vm-1')

        self.mock_client.vcenter.vm.Power.start.assert_called_once_with('vm-1')
        self.mock_client.vcenter.vm.Power.stop.assert_called_once_with('vm-1')
        self.mock_client.vcenter.VM.delete.assert_called_once_with('vm-1')
        call_names = [c[0] for c in self.mock_client.mock_calls]
        self.assertLess(
            call_names.index('vcenter.vm.Power.start'),
            call_names.index('vcenter.vm.Power.stop'),
        )
        self.assertLess(
            call_names.index('vcenter.vm.Power.stop'),
            call_names.index('vcenter.VM.delete'),
        )

    def test_release_instances_powered_off_vm_deletes_without_power_calls(self):
        """A powered-off VM is deleted without any power state change."""
        self.mock_client.vcenter.VM.list.return_value = [self._vm('vm-1')]
        self.mock_client.vcenter.vm.Power.get.return_value = Power.Info(
            state=Power.State.POWERED_OFF
        )

        self.vsphere.release_instances('vm-1')

        self.assertEqual(self.mock_client.vcenter.vm.Power.mock_calls, [call.get('vm-1')])
        self.mock_client.vcenter.VM.delete.assert_called_once_with('vm-1')

    def test_release_instances_duplicate_vm_names_deletes_first_match_only(self):
        """Only the first match is released when several VMs share a name."""
        # CHARACTERIZATION: documented (get_vm) first-match-wins; destructive when names are ambiguous.
        self.mock_client.vcenter.VM.list.return_value = [
            self._vm('vm-a'),
            self._vm('vm-b'),
        ]
        self.mock_client.vcenter.vm.Power.get.return_value = Power.Info(
            state=Power.State.POWERED_OFF
        )

        self.vsphere.release_instances('dup-vm')

        self.mock_client.vcenter.VM.delete.assert_called_once_with('vm-a')
        self.assertNotIn(call('vm-b'), self.mock_client.vcenter.VM.delete.call_args_list)

    def test_list_instances_returns_vms_of_datacenter(self):
        """list_instances returns name/id pairs for VMs of the datacenter."""
        dc = MagicMock()
        dc.datacenter = 'dc-1'
        dc.name = 'mydc'
        self.mock_client.vcenter.Datacenter.list.return_value = [dc]
        self.mock_client.vcenter.VM.list.return_value = [
            self._vm('vm-1', name='n1')
        ]

        result = self.vsphere.list_instances('mydc')

        self.assertEqual(result, [{"vm_name": "n1", "vm_id": "vm-1"}])
        self.mock_client.vcenter.Datacenter.FilterSpec.assert_called_once_with(
            names={'mydc'}
        )
        self.mock_client.vcenter.VM.FilterSpec.assert_called_once_with(
            datacenters={'dc-1'}
        )

    def test_list_instances_unknown_datacenter_exits(self):
        """list_instances exits with status 1 for an unknown datacenter."""
        self.mock_client.vcenter.Datacenter.list.return_value = []

        with self.assertRaises(SystemExit) as context:
            self.vsphere.list_instances('no-such-dc')

        self.assertEqual(context.exception.code, 1)
        self.mock_client.vcenter.VM.list.assert_not_called()

    def test_list_instances_datacenter_without_vms_returns_empty(self):
        """list_instances returns an empty list when the datacenter has no VMs."""
        dc = MagicMock()
        dc.datacenter = 'dc-1'
        self.mock_client.vcenter.Datacenter.list.return_value = [dc]
        self.mock_client.vcenter.VM.list.return_value = []

        self.assertEqual(self.vsphere.list_instances('mydc'), [])

    def test_get_datastore_list_returns_datastores_of_datacenter(self):
        """get_datastore_list returns name/id pairs for the datacenter."""
        ds = MagicMock()
        ds.datastore = 'ds-1'
        ds.name = 'store1'
        self.mock_client.vcenter.Datastore.list.return_value = [ds]

        result = self.vsphere.get_datastore_list('dc-1')

        self.assertEqual(result, [{"datastore_name": "store1", "datastore_id": "ds-1"}])
        self.mock_client.vcenter.Datastore.FilterSpec.assert_called_once_with(
            datacenters={'dc-1'}
        )

    def test_get_datastore_list_empty_returns_empty(self):
        """get_datastore_list returns an empty list when nothing is found."""
        self.mock_client.vcenter.Datastore.list.return_value = []

        self.assertEqual(self.vsphere.get_datastore_list('dc-1'), [])

    def test_get_datastore_list_default_datacenter_none(self):
        # CHARACTERIZATION: without a datacenter argument the filter is built
        # with datacenters={None}, which matches no real datacenter.
        self.mock_client.vcenter.Datastore.list.return_value = []

        self.vsphere.get_datastore_list()

        self.mock_client.vcenter.Datastore.FilterSpec.assert_called_once_with(
            datacenters={None}
        )

    def test_get_folder_list_returns_folders_of_datacenter(self):
        """get_folder_list returns name/id pairs for the datacenter."""
        folder = MagicMock()
        folder.folder = 'folder-1'
        folder.name = 'f1'
        self.mock_client.vcenter.Folder.list.return_value = [folder]

        result = self.vsphere.get_folder_list('dc-1')

        self.assertEqual(result, [{"folder_name": "f1", "folder_id": "folder-1"}])
        self.mock_client.vcenter.Folder.FilterSpec.assert_called_once_with(
            datacenters={'dc-1'}
        )

    def test_get_folder_list_empty_returns_empty(self):
        """get_folder_list returns an empty list when nothing is found."""
        self.mock_client.vcenter.Folder.list.return_value = []

        self.assertEqual(self.vsphere.get_folder_list('dc-1'), [])

    def test_get_folder_list_default_datacenter_none(self):
        # CHARACTERIZATION: without a datacenter argument the filter is built
        # with datacenters={None}, which matches no real folder.
        self.mock_client.vcenter.Folder.list.return_value = []

        self.vsphere.get_folder_list()

        self.mock_client.vcenter.Folder.FilterSpec.assert_called_once_with(
            datacenters={None}
        )

    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.ResourcePool')
    def test_get_resource_pool_by_name_returns_pool_id(self, mock_rp_class):
        """get_resource_pool filters by name and returns the pool id."""
        rp = MagicMock()
        rp.resource_pool = 'rp-9'
        self.mock_client.vcenter.ResourcePool.list.return_value = [rp]

        result = self.vsphere.get_resource_pool('dc-1', 'rp-9')

        self.assertEqual(result, 'rp-9')
        mock_rp_class.FilterSpec.assert_called_once_with(
            datacenters={'dc-1'}, names={'rp-9'}
        )
        self.mock_client.vcenter.ResourcePool.list.assert_called_once_with(
            mock_rp_class.FilterSpec.return_value
        )

    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.ResourcePool')
    def test_get_resource_pool_without_name_returns_first_pool(self, mock_rp_class):
        """Without a name the filter has names=None and the first pool is returned."""
        rp = MagicMock()
        rp.resource_pool = 'rp-1'
        self.mock_client.vcenter.ResourcePool.list.return_value = [rp]

        result = self.vsphere.get_resource_pool('dc-1')

        self.assertEqual(result, 'rp-1')
        mock_rp_class.FilterSpec.assert_called_once_with(
            datacenters={'dc-1'}, names=None
        )

    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.ResourcePool')
    def test_get_resource_pool_not_found_returns_none(self, mock_rp_class):
        # CHARACTERIZATION: the malformed '{}' error log in get_resource_pool
        # fails inside the logging module, which prints a '--- Logging error ---'
        # traceback to stderr; nothing is raised to the caller. raiseExceptions is
        # disabled here to keep that output out of the run.
        raise_patcher = patch('logging.raiseExceptions', False)
        raise_patcher.start()
        self.addCleanup(raise_patcher.stop)
        self.mock_client.vcenter.ResourcePool.list.return_value = []

        self.assertIsNone(self.vsphere.get_resource_pool('dc-1', 'rp-x'))

    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.ResourcePool')
    def test_get_resource_pool_not_found_logs_datacenter(self, mock_rp_class):
        self.mock_client.vcenter.ResourcePool.list.return_value = []

        with self.assertLogs(level='ERROR') as cm:
            result = self.vsphere.get_resource_pool('dc-1', 'rp-x')

        self.assertIsNone(result)
        self.assertIn('dc-1', cm.records[0].getMessage())

    def _setup_create_default_vm_fixtures(self):
        dc = MagicMock()
        dc.datacenter = 'dc-1'
        dc.name = 'Datacenter1'
        self.mock_client.vcenter.Datacenter.list.return_value = [dc]
        folder = MagicMock()
        folder.folder = 'folder-1'
        folder.name = 'vm'
        self.mock_client.vcenter.Folder.list.return_value = [folder]
        datastore = MagicMock()
        datastore.datastore = 'ds-1'
        datastore.name = 'store1'
        self.mock_client.vcenter.Datastore.list.return_value = [datastore]

    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.ResourcePool')
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.VM')
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.random')
    def test_create_default_vm_first_attempt_success(
        self, mock_random, mock_vm_class, mock_rp_class
    ):
        """create_default_vm pins placement choices and builds the specs."""
        mock_random.choice.side_effect = lambda seq: seq[0]
        self._setup_create_default_vm_fixtures()
        rp = MagicMock()
        rp.resource_pool = 'rp-1'
        self.mock_client.vcenter.ResourcePool.list.return_value = [rp]
        self.mock_client.vcenter.VM.create.return_value = 'vm-99'

        vm_id, vm_name = self.vsphere.create_default_vm()

        self.assertEqual(vm_id, 'vm-99')
        self.assertTrue(vm_name.startswith('Test-'))
        mock_vm_class.PlacementSpec.assert_called_once_with(
            folder='folder-1', resource_pool='rp-1', datastore='ds-1'
        )
        create_kwargs = mock_vm_class.CreateSpec.call_args.kwargs
        self.assertEqual(create_kwargs['name'], vm_name)
        self.assertEqual(create_kwargs['guest_os'], 'RHEL_7_64')
        self.assertEqual(create_kwargs['placement'], mock_vm_class.PlacementSpec.return_value)
        self.mock_client.vcenter.VM.create.assert_called_once_with(
            mock_vm_class.CreateSpec.return_value
        )

    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.ResourcePool')
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.VM')
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.random')
    def test_create_default_vm_transient_failure_is_retried(
        self, mock_random, mock_vm_class, mock_rp_class
    ):
        """A transient failure on the first attempt is retried and succeeds."""
        mock_random.choice.side_effect = lambda seq: seq[0]
        self._setup_create_default_vm_fixtures()
        rp = MagicMock()
        rp.resource_pool = 'rp-1'
        self.mock_client.vcenter.ResourcePool.list.side_effect = [
            Exception("transient"),
            [rp],
        ]
        self.mock_client.vcenter.VM.create.return_value = 'vm-99'

        vm_id, vm_name = self.vsphere.create_default_vm()

        self.assertEqual(vm_id, 'vm-99')
        self.assertTrue(vm_name.startswith('Test-'))
        self.assertEqual(self.mock_client.vcenter.Datacenter.list.call_count, 2)
        self.assertEqual(self.mock_client.vcenter.VM.create.call_count, 1)

    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.ResourcePool')
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.VM')
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.random')
    def test_create_default_vm_attempts_exhausted_returns_none(
        self, mock_random, mock_vm_class, mock_rp_class
    ):
        """When every attempt fails, (None, None) is returned after max_attempts."""
        mock_random.choice.side_effect = lambda seq: seq[0]
        self._setup_create_default_vm_fixtures()
        rp = MagicMock()
        rp.resource_pool = 'rp-1'
        self.mock_client.vcenter.ResourcePool.list.return_value = [rp]
        self.mock_client.vcenter.VM.create.side_effect = Exception("no resources")

        vm_id, vm_name = self.vsphere.create_default_vm(max_attempts=3)

        self.assertIsNone(vm_id)
        self.assertIsNone(vm_name)
        self.assertEqual(self.mock_client.vcenter.VM.create.call_count, 3)

    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.time.sleep')
    def test_wait_until_released_timeout_returns_false(self, mock_sleep):
        """wait_until_released returns False once the timeout has elapsed."""
        # CHARACTERIZATION: terminates only because the third VM.list call returns
        # []. Production does not break after the timeout; see
        # test_wait_until_released_timeout_expected_failure.
        vm_obj = self._vm('vm-123')
        self.mock_client.vcenter.VM.list.side_effect = [
            [vm_obj],
            [vm_obj],
            [],
        ]
        mock_affected_node = MagicMock()

        result = self.vsphere.wait_until_released(
            'test-vm', timeout=2, affected_node=mock_affected_node
        )

        self.assertFalse(result)
        self.assertEqual(
            mock_affected_node.set_affected_node_status.call_args.args[0], 'terminated'
        )

    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.time.sleep')
    def test_wait_until_stopped_timeout_returns_false(self, mock_sleep):
        """wait_until_stopped returns False once the timeout has elapsed."""
        # CHARACTERIZATION: terminates only because the mocked Power.get changes
        # to POWERED_OFF on the third call. Production does not break after the
        # timeout; see test_wait_until_stopped_timeout_expected_failure.
        self.mock_client.vcenter.VM.list.return_value = [self._vm('vm-123')]
        self.mock_client.vcenter.vm.Power.get.side_effect = [
            MagicMock(state=Power.State.POWERED_ON),
            MagicMock(state=Power.State.POWERED_ON),
            MagicMock(state=Power.State.POWERED_OFF),
        ]
        mock_affected_node = MagicMock()

        result = self.vsphere.wait_until_stopped(
            'test-vm', timeout=2, affected_node=mock_affected_node
        )

        self.assertFalse(result)
        self.assertEqual(
            mock_affected_node.set_affected_node_status.call_args.args[0], 'stopped'
        )

    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.time.sleep')
    def test_wait_until_released_timeout_expected_failure(self, mock_sleep):
        self.mock_client.vcenter.VM.list.return_value = [self._vm('vm-123')]
        mock_sleep.side_effect = [None] * 10 + [RuntimeError('sleep bound reached')]

        result = self.vsphere.wait_until_released(
            'test-vm', timeout=2, affected_node=MagicMock()
        )

        self.assertFalse(result)
        self.assertLessEqual(mock_sleep.call_count, 2)

    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.time.sleep')
    def test_wait_until_stopped_timeout_expected_failure(self, mock_sleep):
        self.mock_client.vcenter.VM.list.return_value = [self._vm('vm-123')]
        self.mock_client.vcenter.vm.Power.get.return_value = MagicMock(
            state=Power.State.POWERED_ON
        )
        mock_sleep.side_effect = [None] * 10 + [RuntimeError('sleep bound reached')]

        result = self.vsphere.wait_until_stopped(
            'test-vm', timeout=2, affected_node=MagicMock()
        )

        self.assertFalse(result)
        self.assertLessEqual(mock_sleep.call_count, 2)

    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.time.sleep')
    def test_wait_until_running_timeout_expected_failure(self, mock_sleep):
        self.mock_client.vcenter.VM.list.return_value = [self._vm('vm-123')]
        self.mock_client.vcenter.vm.Power.get.return_value = MagicMock(
            state=Power.State.POWERED_OFF
        )
        mock_sleep.side_effect = [None] * 10 + [RuntimeError('sleep bound reached')]

        result = self.vsphere.wait_until_running(
            'test-vm', timeout=2, affected_node=MagicMock()
        )

        self.assertFalse(result)
        self.assertLessEqual(mock_sleep.call_count, 2)


class TestVmwareScenarioExtended(unittest.TestCase):
    """Extended branch coverage for vmware_node_scenarios scenario wrappers."""

    def setUp(self):
        self.mock_kubecli = MagicMock()

    def _scenarios(self, mock_vsphere_class, node_action_kube_check,
                   affected_nodes_status):
        scenarios = vmware_node_scenarios(
            kubecli=self.mock_kubecli,
            node_action_kube_check=node_action_kube_check,
            affected_nodes_status=affected_nodes_status,
        )
        return scenarios, mock_vsphere_class.return_value

    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.vSphere')
    def test_node_start_scenario_exception_is_handled(self, mock_vsphere_class):
        """A failing start does not raise and tracks no affected node."""
        affected_status = AffectedNodeStatus()
        scenarios, mock_vsphere = self._scenarios(mock_vsphere_class, False,
                                                  affected_status)
        mock_vsphere.start_instances.side_effect = Exception("vSphere API Error")

        scenarios.node_start_scenario(
            instance_kill_count=1, node="failing-node", timeout=300, poll_interval=5
        )

        mock_vsphere.start_instances.assert_called_with("failing-node")
        self.assertEqual(len(affected_status.affected_nodes), 0)

    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.nodeaction')
    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.vSphere')
    def test_node_stop_scenario_with_kube_check_waits_for_ready(
        self, mock_vsphere_class, mock_nodeaction
    ):
        """The stop scenario runs the kube ready check when enabled."""
        scenarios, mock_vsphere = self._scenarios(mock_vsphere_class, True,
                                                  AffectedNodeStatus())
        mock_vsphere.stop_instances.return_value = True
        mock_vsphere.wait_until_stopped.return_value = True

        scenarios.node_stop_scenario(
            instance_kill_count=1, node="stop-kube-node", timeout=300, poll_interval=5
        )

        mock_vsphere.stop_instances.assert_called_with("stop-kube-node")
        mock_vsphere.wait_until_stopped.assert_called_once()
        mock_nodeaction.wait_for_ready_status.assert_called_once()



class TestVSphereWaitWithoutNode(unittest.TestCase):
    """Wait functions must tolerate affected_node being None."""

    ENV = {
        'VSPHERE_IP': '192.168.1.100',
        'VSPHERE_USERNAME': 'admin',
        'VSPHERE_PASSWORD': 'password123',
    }

    def setUp(self):
        env = patch.dict('os.environ', self.ENV)
        env.start()
        self.addCleanup(env.stop)
        session_patcher = patch(
            'krkn.scenario_plugins.node_actions.vmware_node_scenarios.requests.session'
        )
        session_patcher.start()
        self.addCleanup(session_patcher.stop)
        factory_patcher = patch(
            'krkn.scenario_plugins.node_actions.vmware_node_scenarios.create_vsphere_client'
        )
        factory = factory_patcher.start()
        self.addCleanup(factory_patcher.stop)
        self.mock_client = MagicMock()
        factory.return_value = self.mock_client
        self.vsphere = vSphere()

    @patch('krkn.scenario_plugins.node_actions.vmware_node_scenarios.time.sleep')
    def test_wait_functions_without_affected_node_skip_status_update(self, mock_sleep):
        """All wait functions succeed and skip status updates without a node."""
        vm_obj = MagicMock()
        vm_obj.vm = 'vm-123'

        self.mock_client.vcenter.VM.list.side_effect = [[vm_obj], []]
        self.assertTrue(
            self.vsphere.wait_until_released('test-vm', timeout=60, affected_node=None)
        )

        self.mock_client.vcenter.VM.list.side_effect = None
        self.mock_client.vcenter.VM.list.return_value = [vm_obj]
        self.mock_client.vcenter.vm.Power.get.side_effect = [
            MagicMock(state=Power.State.POWERED_OFF),
            MagicMock(state=Power.State.POWERED_ON),
        ]
        self.assertTrue(
            self.vsphere.wait_until_running('test-vm', timeout=60, affected_node=None)
        )

        self.mock_client.vcenter.vm.Power.get.side_effect = [
            MagicMock(state=Power.State.POWERED_ON),
            MagicMock(state=Power.State.POWERED_OFF),
        ]
        self.assertTrue(
            self.vsphere.wait_until_stopped('test-vm', timeout=60, affected_node=None)
        )