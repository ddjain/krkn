#!/usr/bin/env python3

"""
Test suite for Docker/Podman node scenarios

This test suite covers both the Docker container runtime wrapper class and
the docker_node_scenarios class using mocks to avoid actual Docker/Podman
API calls (no sockets, no containers, no cluster).

Usage:
    python -m pytest tests/test_docker_node_scenarios.py

Assisted By: Claude Code
"""

import unittest
from unittest.mock import ANY, MagicMock, call, patch

import docker

from krkn_lib.k8s import KrknKubernetes
from krkn_lib.models.k8s import AffectedNodeStatus
from krkn.scenario_plugins.node_actions.docker_node_scenarios import (
    Docker,
    docker_node_scenarios,
)

MODULE = 'krkn.scenario_plugins.node_actions.docker_node_scenarios'
NODE = 'kind-control-plane'
CONTAINER_ID = 'abc123'

# Sentinel so tests can explicitly pass version=None (non-dict payload)
_UNSET = object()


def make_mock_client(version=_UNSET, ping_error=None, containers=None,
                     containers_error=None):
    """Build a mock Docker/Podman API client with real dict/list returns.

    A plain MagicMock would make version() return a Mock, and
    version_info.get('Version', '') would silently pass 'podman' checks in
    unexpected ways, so version/containers must be real objects.
    """
    client = MagicMock()
    if ping_error is not None:
        client.ping.side_effect = ping_error
    else:
        client.ping.return_value = True
    if version is _UNSET:
        version = {'Version': '24.0.7'}
    client.version.return_value = version
    if containers_error is not None:
        client.containers.list.side_effect = containers_error
    else:
        client.containers.list.return_value = containers if containers is not None else []
    return client


class TestDocker(unittest.TestCase):
    """Test cases for the Docker container runtime wrapper class"""

    def setUp(self):
        """Set up test fixtures: mock the docker SDK boundary and platform"""
        self.docker_client_patcher = patch(f'{MODULE}.docker.DockerClient')
        self.from_env_patcher = patch(f'{MODULE}.docker.from_env')
        self.platform_system_patcher = patch(f'{MODULE}.platform.system', return_value='Linux')
        self.path_exists_patcher = patch(f'{MODULE}.os.path.exists', return_value=False)
        self.expanduser_patcher = patch(
            f'{MODULE}.os.path.expanduser', return_value='/fakehome'
        )
        self.getuid_patcher = patch(f'{MODULE}.os.getuid', return_value=1000)

        self.mock_docker_client = self.docker_client_patcher.start()
        self.mock_from_env = self.from_env_patcher.start()
        self.platform_system_patcher.start()
        self.path_exists_patcher.start()
        self.expanduser_patcher.start()
        self.getuid_patcher.start()

    def tearDown(self):
        """Clean up test fixtures"""
        self.docker_client_patcher.stop()
        self.from_env_patcher.stop()
        self.platform_system_patcher.stop()
        self.path_exists_patcher.stop()
        self.expanduser_patcher.stop()
        self.getuid_patcher.stop()

    def test_init_docker_socket_succeeds_first(self):
        """Test init connects via Docker Unix socket first and stops there"""
        good_client = make_mock_client()
        self.mock_docker_client.return_value = good_client

        runtime_wrapper = Docker()

        self.assertIs(runtime_wrapper.client, good_client)
        self.assertEqual(runtime_wrapper.runtime, 'docker')
        self.mock_docker_client.assert_called_once_with(
            base_url='unix:///var/run/docker.sock'
        )
        self.mock_from_env.assert_not_called()

    def test_init_all_methods_fail_raises_runtime_error(self):
        """Test init raises RuntimeError when every connection method fails"""
        self.mock_docker_client.side_effect = Exception('no socket')
        self.mock_from_env.side_effect = Exception('no env')

        with self.assertLogs(level='ERROR') as log_ctx:
            with self.assertRaises(RuntimeError) as ctx:
                Docker()

        self.assertIn(
            'Failed to initialize container runtime client (Docker/Podman) '
            'with any connection method',
            str(ctx.exception),
        )
        # The error path enumerates every attempted connection method
        logged = '\n'.join(log_ctx.output)
        self.assertIn('Podman Unix socket (rootless)', logged)
        self.assertIn('Environment variables', logged)
        # NOTE: the summary logs the unformatted '{uid}' placeholder because
        # the loop rebinds a local copy of the method string; the actual
        # connection attempt uses the formatted uid (see ordering test below)
        self.assertIn('unix:///run/user/{uid}/podman/podman.sock', logged)

    def test_init_ping_failure_falls_through_to_from_env(self):
        """Test init falls through to from_env when socket ping fails"""
        bad_client = make_mock_client(ping_error=Exception('ping refused'))
        good_client = make_mock_client(version={'Version': '24.0.7'})
        self.mock_docker_client.return_value = bad_client
        self.mock_from_env.return_value = good_client

        runtime_wrapper = Docker()

        self.assertIs(runtime_wrapper.client, good_client)
        self.assertEqual(runtime_wrapper.runtime, 'docker')
        self.mock_from_env.assert_called_once_with()

    def test_init_linux_tries_sockets_in_order_before_from_env(self):
        """Test init order: docker.sock, rootful, rootless (uid formatted), from_env"""
        bad_client = make_mock_client(ping_error=Exception('ping refused'))
        good_client = make_mock_client()
        self.mock_docker_client.return_value = bad_client
        self.mock_from_env.return_value = good_client

        runtime_wrapper = Docker()

        self.assertEqual(
            self.mock_docker_client.call_args_list,
            [
                call(base_url='unix:///var/run/docker.sock'),
                call(base_url='unix:///run/podman/podman.sock'),
                call(base_url='unix:///run/user/1000/podman/podman.sock'),
            ],
        )
        self.assertIs(runtime_wrapper.client, good_client)
        self.mock_from_env.assert_called_once_with()

    def test_init_macos_podman_machine_socket(self):
        """Test init uses the macOS podman machine socket when it exists"""
        self.platform_system_patcher.stop()
        with patch(f'{MODULE}.platform.system', return_value='Darwin'):
            self.path_exists_patcher.stop()
            with patch(f'{MODULE}.os.path.exists', return_value=True):
                self.expanduser_patcher.stop()
                with patch(
                    f'{MODULE}.os.path.expanduser',
                    return_value='/fakehome/.local/share/containers/podman/machine/podman.sock',
                ):
                    bad_client = make_mock_client(ping_error=Exception('ping refused'))
                    good_client = make_mock_client(version={'Version': '4.9.3'})
                    self.mock_docker_client.side_effect = [bad_client, good_client]

                    runtime_wrapper = Docker()

        self.assertIs(runtime_wrapper.client, good_client)
        self.assertEqual(
            self.mock_docker_client.call_args_list,
            [
                call(base_url='unix:///var/run/docker.sock'),
                call(
                    base_url='unix:///fakehome/.local/share/containers/podman/machine/podman.sock'
                ),
            ],
        )
        self.mock_from_env.assert_not_called()

    def test_init_macos_podman_socket_absent_not_attempted(self):
        """Test init skips the macOS podman socket when it does not exist"""
        self.platform_system_patcher.stop()
        with patch(f'{MODULE}.platform.system', return_value='Darwin'):
            bad_client = make_mock_client(ping_error=Exception('ping refused'))
            good_client = make_mock_client()
            self.mock_docker_client.return_value = bad_client
            self.mock_from_env.return_value = good_client

            runtime_wrapper = Docker()

        # Only the Docker socket was attempted via DockerClient, then from_env
        self.assertEqual(
            self.mock_docker_client.call_args_list,
            [call(base_url='unix:///var/run/docker.sock')],
        )
        self.assertIs(runtime_wrapper.client, good_client)

    def test_init_version_error_runtime_unknown(self):
        """Test init still succeeds with runtime 'unknown' when version() fails"""
        client = make_mock_client(version=None)
        client.version.side_effect = Exception('version endpoint unavailable')
        self.mock_docker_client.return_value = client

        runtime_wrapper = Docker()

        self.assertIs(runtime_wrapper.client, client)
        self.assertEqual(runtime_wrapper.runtime, 'unknown')

    def test_init_version_payload_missing_version_key(self):
        """Test init defaults to 'docker' when version payload lacks 'Version'"""
        client = make_mock_client(version={})
        self.mock_docker_client.return_value = client

        runtime_wrapper = Docker()

        self.assertEqual(runtime_wrapper.runtime, 'docker')

    def test_init_version_non_dict_runtime_unknown(self):
        """Test init swallows non-dict version payloads as 'unknown' runtime"""
        client = make_mock_client(version=None)
        self.mock_docker_client.return_value = client

        runtime_wrapper = Docker()

        self.assertEqual(runtime_wrapper.runtime, 'unknown')

    def test_init_version_string_with_podman_suffix(self):
        """Test init detects podman from a 'podman'-bearing version string"""
        client = make_mock_client(version={'Version': '4.9.3-podman'})
        self.mock_docker_client.return_value = client

        runtime_wrapper = Docker()

        self.assertEqual(runtime_wrapper.runtime, 'podman')

    def test_init_realistic_podman_version_payload_detected_as_podman(self):
        """Test init detects Podman from a realistic Podman version payload"""
        client = make_mock_client(
            version={
                'Version': '4.9.3',
                'ApiVersion': '4.9.3',
                'Components': [
                    {'Name': 'Podman Engine', 'Version': '4.9.3'},
                ],
            }
        )
        self.mock_docker_client.return_value = client

        runtime_wrapper = Docker()

        self.assertEqual(runtime_wrapper.runtime, 'podman')

    def test_init_containers_list_error_non_fatal(self):
        """Test init succeeds even when listing containers fails"""
        client = make_mock_client(containers_error=Exception('list refused'))
        self.mock_docker_client.return_value = client

        runtime_wrapper = Docker()

        self.assertIs(runtime_wrapper.client, client)
        self.assertEqual(runtime_wrapper.runtime, 'docker')

    def test_init_logs_only_first_five_containers(self):
        """Test init debug-logs at most 5 containers regardless of count"""
        containers = []
        for i in range(10):
            container = MagicMock()
            container.name = f'kind-{i}'
            container.status = 'running'
            containers.append(container)
        client = make_mock_client(containers=containers)
        self.mock_docker_client.return_value = client

        with self.assertLogs(level='DEBUG') as log_ctx:
            Docker()

        debug_container_lines = [
            line for line in log_ctx.output if '  Container:' in line
        ]
        self.assertEqual(len(debug_container_lines), 5)
        client.containers.list.assert_called_once_with(all=True)

    def test_get_container_id_returns_id_and_passes_name_verbatim(self):
        """Test get_container_id returns the container id for the exact name"""
        runtime_wrapper = Docker()
        container = MagicMock()
        container.id = CONTAINER_ID
        runtime_wrapper.client.containers.get.return_value = container

        self.assertEqual(runtime_wrapper.get_container_id(NODE), CONTAINER_ID)
        runtime_wrapper.client.containers.get.assert_called_once_with(NODE)

    def test_get_container_id_dotted_node_name_passed_verbatim(self):
        """Test get_container_id passes dotted node names through unchanged"""
        runtime_wrapper = Docker()
        runtime_wrapper.client.containers.get.side_effect = docker.errors.NotFound(
            'no such container'
        )

        with self.assertRaises(docker.errors.NotFound):
            runtime_wrapper.get_container_id('ip-10-0-1-100.ec2.internal')

        runtime_wrapper.client.containers.get.assert_called_once_with(
            'ip-10-0-1-100.ec2.internal'
        )

    def test_get_container_id_not_found_propagates_unwrapped(self):
        """Test get_container_id propagates NotFound without wrapping"""
        runtime_wrapper = Docker()
        runtime_wrapper.client.containers.get.side_effect = docker.errors.NotFound(
            'no such container'
        )

        with self.assertRaises(docker.errors.NotFound):
            runtime_wrapper.get_container_id(NODE)

    def test_instance_actions_invoke_container_method(self):
        """Test start/stop/reboot actions call the matching container method"""
        for action, container_method in [
            ('start_instances', 'start'),
            ('stop_instances', 'stop'),
            ('reboot_instances', 'restart'),
        ]:
            with self.subTest(action=action):
                # fresh client per iteration: DockerClient.return_value is
                # cached on the mock shared across subTests
                self.mock_docker_client.return_value = make_mock_client()
                runtime_wrapper = Docker()
                container = MagicMock()
                container.id = CONTAINER_ID
                runtime_wrapper.client.containers.get.return_value = container

                getattr(runtime_wrapper, action)(NODE)

                runtime_wrapper.client.containers.get.assert_called_once_with(NODE)
                getattr(container, container_method).assert_called_once_with()

    def test_instance_actions_api_error_propagates_unwrapped(self):
        """Test start/stop/reboot propagate APIError without retry or wrap"""
        for action, container_method in [
            ('start_instances', 'start'),
            ('stop_instances', 'stop'),
            ('reboot_instances', 'restart'),
        ]:
            with self.subTest(action=action):
                self.mock_docker_client.return_value = make_mock_client()
                runtime_wrapper = Docker()
                container = MagicMock()
                getattr(
                    container, container_method
                ).side_effect = docker.errors.APIError('daemon error')
                runtime_wrapper.client.containers.get.return_value = container

                with self.assertRaises(docker.errors.APIError):
                    getattr(runtime_wrapper, action)(NODE)

    def test_start_instances_gets_container_before_start(self):
        """Test the container is looked up before the action is invoked"""
        runtime_wrapper = Docker()
        parent = MagicMock()
        # use a child mock of `parent` as the client so call ordering is
        # tracked on a single mock tree
        runtime_wrapper.client = parent.client
        container = parent.container
        container.id = CONTAINER_ID
        runtime_wrapper.client.containers.get.return_value = container

        runtime_wrapper.start_instances(NODE)

        self.assertEqual(
            parent.mock_calls,
            [call.client.containers.get(NODE), call.container.start()],
        )

    def test_terminate_instances_stops_before_removes(self):
        """Test terminate stops the container before removing it"""
        runtime_wrapper = Docker()
        parent = MagicMock()
        runtime_wrapper.client = parent.client
        container = parent.container
        container.id = CONTAINER_ID
        runtime_wrapper.client.containers.get.return_value = container

        runtime_wrapper.terminate_instances(NODE)

        self.assertEqual(
            parent.mock_calls,
            [call.client.containers.get(NODE), call.container.stop(), call.container.remove()],
        )

    def test_terminate_instances_remove_called_without_force(self):
        """Test remove is called with no kwargs (stop-first is the only guard)"""
        runtime_wrapper = Docker()
        container = MagicMock()
        container.id = CONTAINER_ID
        runtime_wrapper.client.containers.get.return_value = container

        runtime_wrapper.terminate_instances(NODE)

        container.stop.assert_called_once_with()
        container.remove.assert_called_once_with()

    def test_terminate_instances_stop_failure_never_removes(self):
        """Test a failing stop never leads to container removal"""
        runtime_wrapper = Docker()
        container = MagicMock()
        container.stop.side_effect = docker.errors.APIError('cannot stop')
        runtime_wrapper.client.containers.get.return_value = container

        with self.assertRaises(docker.errors.APIError):
            runtime_wrapper.terminate_instances(NODE)

        container.remove.assert_not_called()

    def test_terminate_instances_remove_failure_leaves_container_stopped(self):
        """Test a failing remove propagates with no retry/rollback"""
        runtime_wrapper = Docker()
        container = MagicMock()
        container.remove.side_effect = docker.errors.APIError('cannot remove')
        runtime_wrapper.client.containers.get.return_value = container

        with self.assertRaises(docker.errors.APIError):
            runtime_wrapper.terminate_instances(NODE)

        container.stop.assert_called_once_with()
        container.remove.assert_called_once_with()


class TestDockerNodeScenarios(unittest.TestCase):
    """Test cases for docker_node_scenarios class"""

    def setUp(self):
        """Set up test fixtures"""
        self.kubecli = MagicMock(spec=KrknKubernetes)
        self.affected_nodes_status = AffectedNodeStatus()

        # Mock the Docker class
        with patch(f'{MODULE}.Docker') as mock_docker_class:
            self.mock_docker = MagicMock()
            self.mock_docker.runtime = 'docker'
            mock_docker_class.return_value = self.mock_docker
            self.scenario = docker_node_scenarios(
                kubecli=self.kubecli,
                node_action_kube_check=True,
                affected_nodes_status=self.affected_nodes_status
            )

    def _make_scenario(self, node_action_kube_check):
        """Create a scenario with a specific node_action_kube_check setting"""
        with patch(f'{MODULE}.Docker') as mock_docker_class:
            mock_docker = MagicMock()
            mock_docker.runtime = 'docker'
            mock_docker_class.return_value = mock_docker
            return docker_node_scenarios(
                kubecli=self.kubecli,
                node_action_kube_check=node_action_kube_check,
                affected_nodes_status=AffectedNodeStatus()
            )

    def test_init_stores_attributes_and_docker_client(self):
        """Test the scenario constructor wires up collaborators"""
        self.assertIs(self.scenario.kubecli, self.kubecli)
        self.assertIs(
            self.scenario.affected_nodes_status, self.affected_nodes_status
        )
        self.assertTrue(self.scenario.node_action_kube_check)
        self.assertIs(self.scenario.docker, self.mock_docker)

    def test_init_docker_unavailable_propagates(self):
        """Test a failing Docker() construction propagates from the constructor"""
        with patch(f'{MODULE}.Docker', side_effect=RuntimeError('no runtime')):
            with self.assertRaises(RuntimeError):
                docker_node_scenarios(
                    kubecli=self.kubecli,
                    node_action_kube_check=True,
                    affected_nodes_status=AffectedNodeStatus(),
                )

    def test_init_logs_detected_runtime(self):
        """Test the constructor logs the detected container runtime"""
        with patch(f'{MODULE}.Docker') as mock_docker_class:
            mock_docker = MagicMock()
            mock_docker.runtime = 'podman'
            mock_docker_class.return_value = mock_docker

            with self.assertLogs(level='INFO') as log_ctx:
                docker_node_scenarios(
                    kubecli=self.kubecli,
                    node_action_kube_check=True,
                    affected_nodes_status=AffectedNodeStatus(),
                )

        self.assertTrue(any('podman runtime' in line for line in log_ctx.output))

    @patch(
        'krkn.scenario_plugins.node_actions.common_node_functions.wait_for_ready_status'
    )
    def test_node_start_scenario_success(self, mock_wait_ready):
        """Test node start scenario successfully"""
        self.mock_docker.get_container_id.return_value = CONTAINER_ID

        self.scenario.node_start_scenario(
            instance_kill_count=1,
            node=NODE,
            timeout=600,
            poll_interval=15
        )

        self.mock_docker.get_container_id.assert_called_once_with(NODE)
        self.mock_docker.start_instances.assert_called_once_with(NODE)
        mock_wait_ready.assert_called_once_with(
            NODE, 600, self.kubecli, self.affected_nodes_status.affected_nodes[0]
        )
        self.assertEqual(len(self.affected_nodes_status.affected_nodes), 1)
        self.assertEqual(
            self.affected_nodes_status.affected_nodes[0].node_name, NODE
        )
        self.assertEqual(
            self.affected_nodes_status.affected_nodes[0].node_id, CONTAINER_ID
        )

    @patch(
        'krkn.scenario_plugins.node_actions.common_node_functions.wait_for_ready_status'
    )
    def test_node_start_scenario_no_kube_check(self, mock_wait_ready):
        """Test node start scenario without kube check"""
        scenario = self._make_scenario(node_action_kube_check=False)
        scenario.docker.get_container_id.return_value = CONTAINER_ID

        scenario.node_start_scenario(
            instance_kill_count=1, node=NODE, timeout=600, poll_interval=15
        )

        mock_wait_ready.assert_not_called()
        self.assertEqual(len(scenario.affected_nodes_status.affected_nodes), 1)

    def test_node_start_scenario_failure_propagates_original_exception(self):
        """Test node start failure re-raises the original exception type"""
        self.mock_docker.get_container_id.side_effect = docker.errors.NotFound(
            'no such container'
        )

        with self.assertRaises(docker.errors.NotFound):
            self.scenario.node_start_scenario(
                instance_kill_count=1, node=NODE, timeout=600, poll_interval=15
            )

        self.mock_docker.start_instances.assert_not_called()
        self.assertEqual(len(self.affected_nodes_status.affected_nodes), 0)

    @patch(
        'krkn.scenario_plugins.node_actions.common_node_functions.wait_for_ready_status'
    )
    def test_node_start_scenario_wait_failure_no_append(self, mock_wait_ready):
        """Test a failing wait re-raises and records nothing despite the start"""
        self.mock_docker.get_container_id.return_value = CONTAINER_ID
        mock_wait_ready.side_effect = Exception('node never became ready')

        with self.assertRaisesRegex(Exception, 'node never became ready'):
            self.scenario.node_start_scenario(
                instance_kill_count=1, node=NODE, timeout=600, poll_interval=15
            )

        self.mock_docker.start_instances.assert_called_once_with(NODE)
        self.assertEqual(len(self.affected_nodes_status.affected_nodes), 0)

    def test_node_start_scenario_kill_count(self):
        """Test node start scenario repeats per instance_kill_count"""
        for count in (0, -1, 3):
            with self.subTest(instance_kill_count=count):
                scenario = self._make_scenario(node_action_kube_check=False)
                scenario.docker.get_container_id.return_value = CONTAINER_ID

                scenario.node_start_scenario(
                    instance_kill_count=count,
                    node=NODE,
                    timeout=600,
                    poll_interval=15
                )

                expected = max(count, 0)
                self.assertEqual(
                    scenario.docker.get_container_id.call_count, expected
                )
                self.assertEqual(
                    scenario.docker.start_instances.call_count, expected
                )
                self.assertEqual(
                    len(scenario.affected_nodes_status.affected_nodes), expected
                )

    def test_node_start_scenario_empty_node_name_propagates(self):
        """Test an empty node name is not silently ignored"""
        self.mock_docker.get_container_id.side_effect = docker.errors.NotFound(
            'no such container'
        )

        with self.assertRaises(docker.errors.NotFound):
            self.scenario.node_start_scenario(
                instance_kill_count=1, node='', timeout=600, poll_interval=15
            )

    def test_node_start_scenario_start_precedes_wait(self):
        """Test the container is started before waiting for readiness"""
        parent = MagicMock()
        mock_docker = parent.docker
        mock_docker.runtime = 'docker'
        mock_docker.get_container_id.return_value = CONTAINER_ID
        with patch(f'{MODULE}.Docker', return_value=mock_docker):
            scenario = docker_node_scenarios(
                kubecli=self.kubecli,
                node_action_kube_check=True,
                affected_nodes_status=AffectedNodeStatus(),
            )
        with patch(
            'krkn.scenario_plugins.node_actions.common_node_functions.wait_for_ready_status',
            new=parent.wait_ready,
        ):
            scenario.node_start_scenario(
                instance_kill_count=1, node=NODE, timeout=600, poll_interval=15
            )

        self.assertEqual(
            parent.mock_calls,
            [
                call.docker.get_container_id(NODE),
                call.docker.start_instances(NODE),
                call.wait_ready(NODE, 600, self.kubecli, ANY),
            ],
        )

    @patch(
        'krkn.scenario_plugins.node_actions.common_node_functions.wait_for_unknown_status'
    )
    def test_node_stop_scenario_success(self, mock_wait_unknown):
        """Test node stop scenario successfully"""
        self.mock_docker.get_container_id.return_value = CONTAINER_ID

        self.scenario.node_stop_scenario(
            instance_kill_count=1, node=NODE, timeout=600, poll_interval=15
        )

        self.mock_docker.get_container_id.assert_called_once_with(NODE)
        self.mock_docker.stop_instances.assert_called_once_with(NODE)
        mock_wait_unknown.assert_called_once_with(
            NODE, 600, self.kubecli, self.affected_nodes_status.affected_nodes[0]
        )
        self.assertEqual(len(self.affected_nodes_status.affected_nodes), 1)
        self.assertEqual(
            self.affected_nodes_status.affected_nodes[0].node_name, NODE
        )
        self.assertEqual(
            self.affected_nodes_status.affected_nodes[0].node_id, CONTAINER_ID
        )

    @patch(
        'krkn.scenario_plugins.node_actions.common_node_functions.wait_for_unknown_status'
    )
    def test_node_stop_scenario_no_kube_check(self, mock_wait_unknown):
        """Test node stop scenario without kube check"""
        scenario = self._make_scenario(node_action_kube_check=False)
        scenario.docker.get_container_id.return_value = CONTAINER_ID

        scenario.node_stop_scenario(
            instance_kill_count=1, node=NODE, timeout=600, poll_interval=15
        )

        mock_wait_unknown.assert_not_called()
        self.assertEqual(len(scenario.affected_nodes_status.affected_nodes), 1)

    def test_node_stop_scenario_failure_propagates_original_exception(self):
        """Test node stop failure re-raises the original exception type"""
        self.mock_docker.stop_instances.side_effect = docker.errors.APIError(
            'cannot stop container'
        )

        with self.assertRaises(docker.errors.APIError):
            self.scenario.node_stop_scenario(
                instance_kill_count=1, node=NODE, timeout=600, poll_interval=15
            )

        self.assertEqual(len(self.affected_nodes_status.affected_nodes), 0)

    @patch(
        'krkn.scenario_plugins.node_actions.common_node_functions.wait_for_unknown_status'
    )
    def test_node_stop_scenario_wait_failure_no_append(self, mock_wait_unknown):
        """Test a failing wait re-raises with no undo of the applied stop"""
        self.mock_docker.get_container_id.return_value = CONTAINER_ID
        mock_wait_unknown.side_effect = Exception('node never left the cluster')

        with self.assertRaisesRegex(Exception, 'node never left the cluster'):
            self.scenario.node_stop_scenario(
                instance_kill_count=1, node=NODE, timeout=600, poll_interval=15
            )

        self.mock_docker.stop_instances.assert_called_once_with(NODE)
        self.assertEqual(len(self.affected_nodes_status.affected_nodes), 0)

    def test_node_stop_scenario_kill_count(self):
        """Test node stop scenario repeats per instance_kill_count"""
        self.mock_docker.get_container_id.return_value = CONTAINER_ID

        self.scenario.node_stop_scenario(
            instance_kill_count=3, node=NODE, timeout=600, poll_interval=15
        )

        self.assertEqual(self.mock_docker.stop_instances.call_count, 3)
        self.assertEqual(len(self.affected_nodes_status.affected_nodes), 3)

    def test_node_stop_scenario_stop_precedes_wait(self):
        """Test the container is stopped before waiting for unknown status"""
        parent = MagicMock()
        mock_docker = parent.docker
        mock_docker.runtime = 'docker'
        mock_docker.get_container_id.return_value = CONTAINER_ID
        with patch(f'{MODULE}.Docker', return_value=mock_docker):
            scenario = docker_node_scenarios(
                kubecli=self.kubecli,
                node_action_kube_check=True,
                affected_nodes_status=AffectedNodeStatus(),
            )
        with patch(
            'krkn.scenario_plugins.node_actions.common_node_functions.wait_for_unknown_status',
            new=parent.wait_unknown,
        ):
            scenario.node_stop_scenario(
                instance_kill_count=1, node=NODE, timeout=600, poll_interval=15
            )

        self.assertEqual(
            parent.mock_calls,
            [
                call.docker.get_container_id(NODE),
                call.docker.stop_instances(NODE),
                call.wait_unknown(NODE, 600, self.kubecli, ANY),
            ],
        )

    def test_node_termination_scenario_success_records_affected_node(self):
        """Test node termination records the affected node for telemetry"""
        self.mock_docker.get_container_id.return_value = CONTAINER_ID

        self.scenario.node_termination_scenario(
            instance_kill_count=1, node=NODE, timeout=600, poll_interval=15
        )

        self.mock_docker.terminate_instances.assert_called_once_with(NODE)
        self.assertEqual(len(self.affected_nodes_status.affected_nodes), 1)
        self.assertEqual(
            self.affected_nodes_status.affected_nodes[0].node_name, NODE
        )
        self.assertEqual(
            self.affected_nodes_status.affected_nodes[0].node_id, CONTAINER_ID
        )

    def test_node_termination_scenario_failure_propagates_original_exception(self):
        """Test node termination failure re-raises the original exception type"""
        self.mock_docker.get_container_id.side_effect = docker.errors.NotFound(
            'no such container'
        )

        with self.assertRaises(docker.errors.NotFound):
            self.scenario.node_termination_scenario(
                instance_kill_count=1, node=NODE, timeout=600, poll_interval=15
            )

        self.mock_docker.terminate_instances.assert_not_called()

    def test_node_termination_scenario_partial_terminate_propagates(self):
        """Test a partially applied termination propagates with no record"""
        self.mock_docker.get_container_id.return_value = CONTAINER_ID
        self.mock_docker.terminate_instances.side_effect = (
            docker.errors.APIError('remove failed')
        )

        with self.assertRaises(docker.errors.APIError):
            self.scenario.node_termination_scenario(
                instance_kill_count=1, node=NODE, timeout=600, poll_interval=15
            )

        self.assertEqual(len(self.affected_nodes_status.affected_nodes), 0)

    def test_node_termination_scenario_kill_count_reruns_on_removed_container(self):
        """Test kill_count>1 re-terminates: the 2nd iteration hits a container
        removed by the 1st and raises NotFound (documented design quirk:
        unlike the cloud scenarios there is no wait between iterations)"""
        self.mock_docker.get_container_id.side_effect = [
            CONTAINER_ID,
            docker.errors.NotFound('container already removed'),
        ]

        with self.assertRaises(docker.errors.NotFound):
            self.scenario.node_termination_scenario(
                instance_kill_count=2, node=NODE, timeout=600, poll_interval=15
            )

        self.mock_docker.terminate_instances.assert_called_once_with(NODE)

    @patch(
        'krkn.scenario_plugins.node_actions.common_node_functions.wait_for_unknown_status'
    )
    @patch(
        'krkn.scenario_plugins.node_actions.common_node_functions.wait_for_ready_status'
    )
    def test_node_termination_scenario_ignores_kube_check(
        self, mock_wait_ready, mock_wait_unknown
    ):
        """Test termination performs no kube verification regardless of the
        node_action_kube_check setting (documented divergence from the cloud
        scenarios, which wait for the node to leave the cluster)"""
        self.mock_docker.get_container_id.return_value = CONTAINER_ID

        self.scenario.node_termination_scenario(
            instance_kill_count=1, node=NODE, timeout=600, poll_interval=15
        )

        self.mock_docker.terminate_instances.assert_called_once_with(NODE)
        mock_wait_ready.assert_not_called()
        mock_wait_unknown.assert_not_called()

    @patch(
        'krkn.scenario_plugins.node_actions.common_node_functions.wait_for_ready_status'
    )
    @patch(
        'krkn.scenario_plugins.node_actions.common_node_functions.wait_for_unknown_status'
    )
    def test_node_reboot_scenario_success(
        self, mock_wait_unknown, mock_wait_ready
    ):
        """Test node reboot scenario successfully"""
        self.mock_docker.get_container_id.return_value = CONTAINER_ID

        self.scenario.node_reboot_scenario(
            instance_kill_count=1, node=NODE, timeout=600
        )

        self.mock_docker.get_container_id.assert_called_once_with(NODE)
        self.mock_docker.reboot_instances.assert_called_once_with(NODE)
        mock_wait_unknown.assert_called_once_with(
            NODE, 600, self.kubecli, self.affected_nodes_status.affected_nodes[0]
        )
        mock_wait_ready.assert_called_once_with(
            NODE, 600, self.kubecli, self.affected_nodes_status.affected_nodes[0]
        )
        self.assertEqual(len(self.affected_nodes_status.affected_nodes), 1)
        self.assertEqual(
            self.affected_nodes_status.affected_nodes[0].node_name, NODE
        )

    def test_node_reboot_scenario_records_node_id(self):
        """Test node reboot records the container id on the affected node"""
        self.mock_docker.get_container_id.return_value = CONTAINER_ID

        self.scenario.node_reboot_scenario(
            instance_kill_count=1, node=NODE, timeout=600
        )

        self.assertEqual(
            self.affected_nodes_status.affected_nodes[0].node_id, CONTAINER_ID
        )

    @patch(
        'krkn.scenario_plugins.node_actions.common_node_functions.wait_for_ready_status'
    )
    @patch(
        'krkn.scenario_plugins.node_actions.common_node_functions.wait_for_unknown_status'
    )
    def test_node_reboot_scenario_no_kube_check(
        self, mock_wait_unknown, mock_wait_ready
    ):
        """Test node reboot scenario without kube check"""
        scenario = self._make_scenario(node_action_kube_check=False)
        scenario.docker.get_container_id.return_value = CONTAINER_ID

        scenario.node_reboot_scenario(
            instance_kill_count=1, node=NODE, timeout=600
        )

        scenario.docker.reboot_instances.assert_called_once_with(NODE)
        mock_wait_unknown.assert_not_called()
        mock_wait_ready.assert_not_called()
        self.assertEqual(len(scenario.affected_nodes_status.affected_nodes), 1)

    def test_node_reboot_scenario_failure_propagates_original_exception(self):
        """Test node reboot failure re-raises the original exception type"""
        self.mock_docker.reboot_instances.side_effect = docker.errors.APIError(
            'cannot restart container'
        )

        with self.assertRaises(docker.errors.APIError):
            self.scenario.node_reboot_scenario(
                instance_kill_count=1, node=NODE, timeout=600
            )

        self.assertEqual(len(self.affected_nodes_status.affected_nodes), 0)

    def test_node_reboot_scenario_kill_count(self):
        """Test node reboot scenario repeats per instance_kill_count"""
        self.mock_docker.get_container_id.return_value = CONTAINER_ID

        self.scenario.node_reboot_scenario(
            instance_kill_count=3, node=NODE, timeout=600, soft_reboot=True
        )

        # soft_reboot is accepted but ignored by the container runtime
        self.assertEqual(self.mock_docker.reboot_instances.call_count, 3)
        self.assertEqual(len(self.affected_nodes_status.affected_nodes), 3)

    def test_node_reboot_scenario_waits_unknown_then_ready(self):
        """Test reboot waits for unknown status before ready status"""
        parent = MagicMock()
        mock_docker = parent.docker
        mock_docker.runtime = 'docker'
        mock_docker.get_container_id.return_value = CONTAINER_ID
        with patch(f'{MODULE}.Docker', return_value=mock_docker):
            scenario = docker_node_scenarios(
                kubecli=self.kubecli,
                node_action_kube_check=True,
                affected_nodes_status=AffectedNodeStatus(),
            )
        with patch(
            'krkn.scenario_plugins.node_actions.common_node_functions.wait_for_unknown_status',
            new=parent.wait_unknown,
        ), patch(
            'krkn.scenario_plugins.node_actions.common_node_functions.wait_for_ready_status',
            new=parent.wait_ready,
        ):
            scenario.node_reboot_scenario(
                instance_kill_count=1, node=NODE, timeout=600
            )

        self.assertEqual(
            parent.mock_calls,
            [
                call.docker.get_container_id(NODE),
                call.docker.reboot_instances(NODE),
                call.wait_unknown(NODE, 600, self.kubecli, ANY),
                call.wait_ready(NODE, 600, self.kubecli, ANY),
            ],
        )

if __name__ == "__main__":
    unittest.main()
