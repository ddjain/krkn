#!/usr/bin/env python3

"""
Test suite for the network_chaos_ng utils and utils_network_filter modules.

Usage:
    python -m coverage run -a -m unittest tests/test_utils_network_chaos_ng.py -v
"""

import unittest
from unittest.mock import MagicMock, patch, call

from krkn_lib.k8s import KrknKubernetes

from krkn.scenario_plugins.network_chaos_ng.modules.utils import (
    deploy_network_chaos_ng_pod,
    get_pod_default_interface,
    find_virt_launcher_netns_pid,
    get_vmi_tap_interface,
    get_vmi_masquerade_interface,
    setup_network_chaos_ng_scenario,
    log_error,
    log_warning,
)
from krkn.scenario_plugins.network_chaos_ng.modules.utils_network_filter import (
    generate_rules,
    apply_network_rules,
    clean_network_rules,
    clean_network_rules_namespaced,
    generate_namespaced_rules,
    apply_tc_vmi_chaos,
    clean_tc_vmi_chaos,
)
from krkn.scenario_plugins.network_chaos_ng.models import NetworkFilterConfig

MODULE = "krkn.scenario_plugins.network_chaos_ng.modules.utils"
FILTER_MODULE = "krkn.scenario_plugins.network_chaos_ng.modules.utils_network_filter"


def make_cfg(**over):
    base = dict(
        id="x",
        image="quay.io/fedora:latest",
        wait_duration=1,
        test_duration=1,
        label_selector="",
        service_account="",
        taints=[],
        namespace="test-ns",
        instance_count=1,
        execution="serial",
        interfaces=[],
        target="pod-a",
        ingress=True,
        egress=True,
    )
    base.update(over)
    return NetworkFilterConfig(**base)


class TestDeployNetworkChaosNgPod(unittest.TestCase):
    def setUp(self):
        self.kubecli = MagicMock(spec=KrknKubernetes)

    def _pod_body(self):
        self.assertTrue(self.kubecli.create_pod.called)
        body, namespace, timeout = self.kubecli.create_pod.call_args[0]
        self.assertEqual("test-ns", namespace)
        self.assertEqual(300, timeout)
        return body

    def test_deploy_pod_taint_with_value_renders_equal_toleration(self):
        config = make_cfg(taints=["k=v:NoSchedule"])
        deploy_network_chaos_ng_pod(config, "node-1", "pod-x", self.kubecli)
        body = self._pod_body()
        toleration = body["spec"]["tolerations"][0]
        self.assertEqual(
            {
                "key": "k",
                "operator": "Equal",
                "value": "v",
                "effect": "NoSchedule",
            },
            toleration,
        )
        self.kubecli.create_pod.assert_called_once_with(body, "test-ns", 300)

    def test_deploy_pod_taint_without_value_renders_exists_toleration(self):
        config = make_cfg(taints=["k:NoSchedule"])
        deploy_network_chaos_ng_pod(config, "node-1", "pod-x", self.kubecli)
        toleration = self._pod_body()["spec"]["tolerations"][0]
        self.assertEqual(
            {"key": "k", "operator": "Exists", "effect": "NoSchedule"},
            toleration,
        )

    # CHARACTERIZATION: unvalidated tuple unpacking (`key_value_part, effect =
    # taint.split(":", 1)`) raises a bare ValueError from unpacking, not a validation error.
    def test_deploy_pod_taint_without_colon_raises_value_error(self):
        config = make_cfg(taints=["NoSchedule"])
        with self.assertRaisesRegex(ValueError, "not enough values"):
            deploy_network_chaos_ng_pod(config, "node-1", "pod-x", self.kubecli)
        self.assertFalse(self.kubecli.create_pod.called)

    def test_deploy_pod_taint_empty_key_not_emitted_as_equal_toleration(self):
        config = make_cfg(taints=["=v:NoSchedule"])
        try:
            deploy_network_chaos_ng_pod(config, "node-1", "pod-x", self.kubecli)
        except ValueError:
            return
        toleration = self._pod_body()["spec"]["tolerations"][0]
        self.assertTrue(
            toleration["key"] or toleration["operator"] == "Exists",
            f"invalid toleration emitted: {toleration}",
        )

    def test_deploy_pod_image_with_ampersand_not_escaped(self):
        config = make_cfg(image="quay.io/a&b")
        deploy_network_chaos_ng_pod(config, "node-1", "pod-x", self.kubecli)
        container = self._pod_body()["spec"]["containers"][0]
        self.assertEqual("quay.io/a&b", container["image"])

    def test_deploy_pod_empty_taints_omits_tolerations(self):
        config = make_cfg(taints=[])
        deploy_network_chaos_ng_pod(config, "node-1", "pod-x", self.kubecli)
        spec = self._pod_body()["spec"]
        self.assertNotIn("tolerations", spec)

    def test_deploy_pod_not_host_network_selects_node(self):
        config = make_cfg()
        deploy_network_chaos_ng_pod(
            config, "node-1", "pod-x", self.kubecli, host_network=False
        )
        spec = self._pod_body()["spec"]
        self.assertNotIn("hostNetwork", spec)
        self.assertEqual("node-1", spec["nodeSelector"]["kubernetes.io/hostname"])
        self.assertTrue(spec["hostPID"])


class TestGenerateRules(unittest.TestCase):
    def test_generate_rules_ports_both_directions_full_matrix(self):
        config = make_cfg(interfaces=["eth0", "eth1"], ports=[80, 443])
        input_rules, output_rules = generate_rules(["eth0", "eth1"], config)
        self.assertEqual(8, len(input_rules))
        expected_input = sorted(
            f"iptables -I INPUT 1 -i {iface} -p {protocol} --dport {port} "
            f"-m state --state NEW,RELATED,ESTABLISHED -j DROP"
            for iface in ("eth0", "eth1")
            for protocol in ("tcp", "udp")
            for port in (80, 443)
        )
        self.assertEqual(expected_input, sorted(input_rules))
        for rule in output_rules:
            self.assertTrue(rule.startswith("iptables -I OUTPUT 1"))
            self.assertTrue(
                rule.endswith("-m state --state NEW,RELATED,ESTABLISHED -j DROP")
            )
        for protocol in ("tcp", "udp"):
            for port in (80, 443):
                self.assertTrue(
                    any(
                        f"-p {protocol} --dport {port} " in rule
                        for rule in output_rules
                    )
                )

    def test_generate_rules_output_rules_scoped_to_interface(self):
        config = make_cfg(interfaces=["eth0", "eth1"], ports=[80])
        _, output_rules = generate_rules(["eth0", "eth1"], config)
        for rule in output_rules:
            self.assertTrue(
                "-o eth0" in rule or "-o eth1" in rule,
                f"rule not scoped to a scenario interface: {rule}",
            )

    def test_generate_rules_output_rules_have_no_duplicates(self):
        config = make_cfg(interfaces=["eth0", "eth1"], ports=[80, 443])
        _, output_rules = generate_rules(["eth0", "eth1"], config)
        self.assertEqual(len(output_rules), len(set(output_rules)))

    def test_generate_rules_empty_ports_blocks_all_traffic(self):
        config = make_cfg(interfaces=["eth0", "eth1"], ports=[])
        input_rules, output_rules = generate_rules(["eth0", "eth1"], config)
        expected_input = sorted(
            f"iptables -I INPUT 1 -i {iface} -p {protocol} "
            f"-m state --state NEW,RELATED,ESTABLISHED -j DROP"
            for iface in ("eth0", "eth1")
            for protocol in ("tcp", "udp")
        )
        self.assertEqual(expected_input, sorted(input_rules))
        for rule in output_rules:
            self.assertTrue(rule.startswith("iptables -I OUTPUT 1"))
            self.assertNotIn("--dport", rule)

    def test_generate_rules_duplicate_protocols_deduplicated(self):
        config = make_cfg(
            interfaces=["eth0"], ports=[80], protocols=["tcp", "tcp", "udp"]
        )
        input_rules, output_rules = generate_rules(["eth0"], config)
        self.assertEqual(2, len(input_rules))
        self.assertEqual(2, len(output_rules))
        protocols = {rule.split(" -p ")[1].split(" ")[0] for rule in input_rules}
        self.assertEqual({"tcp", "udp"}, protocols)

    def test_generate_rules_ingress_only_emits_no_output_rules(self):
        config = make_cfg(interfaces=["eth0"], ports=[80], egress=False)
        input_rules, output_rules = generate_rules(["eth0"], config)
        self.assertEqual([], output_rules)
        self.assertEqual(2, len(input_rules))

    def test_generate_rules_egress_only_emits_no_input_rules(self):
        config = make_cfg(interfaces=["eth0"], ports=[80], ingress=False)
        input_rules, output_rules = generate_rules(["eth0"], config)
        self.assertEqual([], input_rules)
        self.assertEqual(2, len(output_rules))

    def test_generate_rules_port_zero_not_emitted(self):
        config = make_cfg(interfaces=["eth0"], ports=[0])
        input_rules, output_rules = generate_rules(["eth0"], config)
        for rule in input_rules + output_rules:
            self.assertNotIn("--dport 0", rule)

    def test_generate_rules_empty_ports_ingress_only(self):
        config = make_cfg(interfaces=["eth0"], ports=[], egress=False)
        input_rules, output_rules = generate_rules(["eth0"], config)
        self.assertEqual([], output_rules)
        self.assertEqual(2, len(input_rules))

    def test_generate_rules_empty_ports_egress_only(self):
        config = make_cfg(interfaces=["eth0"], ports=[], ingress=False)
        input_rules, output_rules = generate_rules(["eth0"], config)
        self.assertEqual([], input_rules)
        self.assertEqual(2, len(output_rules))

    # CHARACTERIZATION: no interfaces means no rules in either direction
    def test_generate_rules_empty_interfaces_returns_two_empty_lists(self):
        config = make_cfg(interfaces=[], ports=[80])
        self.assertEqual(([], []), generate_rules([], config))



class TestApplyNetworkRules(unittest.TestCase):
    def setUp(self):
        self.kubecli = MagicMock(spec=KrknKubernetes)

    def test_apply_network_rules_inputs_first_then_outputs(self):
        apply_network_rules(
            self.kubecli,
            ["in-1", "in-2"],
            ["out-1"],
            "pod-a",
            "test-ns",
            parallel=True,
            node_name="node-1",
        )
        self.assertEqual(
            [
                call(["in-1"], "pod-a", "test-ns"),
                call(["in-2"], "pod-a", "test-ns"),
                call(["out-1"], "pod-a", "test-ns"),
            ],
            self.kubecli.exec_cmd_in_pod.call_args_list,
        )

    def test_apply_network_rules_partial_failure_propagates(self):
        self.kubecli.exec_cmd_in_pod.side_effect = [
            None,
            RuntimeError("exec failed"),
        ]
        with self.assertRaisesRegex(RuntimeError, "exec failed"):
            apply_network_rules(
                self.kubecli, ["in-1", "in-2"], ["out-1"], "pod-a", "test-ns",
                parallel=False, node_name="",
            )
        # CHARACTERIZATION: rules are applied in order and the first failure stops
        # the loop, so the OUTPUT rule is never attempted
        self.assertEqual(
            [call(["in-1"], "pod-a", "test-ns"), call(["in-2"], "pod-a", "test-ns")],
            self.kubecli.exec_cmd_in_pod.call_args_list[:2],
        )
        self.assertNotIn(
            call(["out-1"], "pod-a", "test-ns"),
            self.kubecli.exec_cmd_in_pod.call_args_list,
        )

    def test_apply_network_rules_partial_failure_cleans_up_applied_rules(self):
        self.kubecli.exec_cmd_in_pod.side_effect = (
            [None, RuntimeError("exec failed")] + [None] * 4
        )
        with self.assertRaisesRegex(RuntimeError, "exec failed"):
            apply_network_rules(
                self.kubecli, ["in-1", "in-2"], ["out-1"], "pod-a", "test-ns",
                parallel=False, node_name="",
            )
        deleted = [c.args[0][0] for c in self.kubecli.exec_cmd_in_pod.call_args_list]
        self.assertIn("iptables -D INPUT 1", deleted)



class TestCleanNetworkRules(unittest.TestCase):
    def setUp(self):
        self.kubecli = MagicMock(spec=KrknKubernetes)

    # CHARACTERIZATION: relies on `-I ... 1` insertion order
    def test_clean_network_rules_deletes_positionally(self):
        clean_network_rules(
            self.kubecli, ["in-1", "in-2"], ["out-1", "out-2", "out-3"],
            "pod-a", "test-ns",
        )
        self.assertEqual(
            [call(["iptables -D INPUT 1"], "pod-a", "test-ns")] * 2
            + [call(["iptables -D OUTPUT 1"], "pod-a", "test-ns")] * 3,
            self.kubecli.exec_cmd_in_pod.call_args_list,
        )


class TestCleanNetworkRulesNamespaced(unittest.TestCase):
    def setUp(self):
        self.kubecli = MagicMock(spec=KrknKubernetes)

    # CHARACTERIZATION: relies on `-I ... 1` insertion order
    def test_clean_network_rules_namespaced_single_pid(self):
        clean_network_rules_namespaced(
            self.kubecli, ["in-1"], ["out-1"], "pod-a", "test-ns", pids=["p1"]
        )
        self.assertEqual(
            [
                call(["nsenter --target p1 --net -- iptables -D INPUT 1"],
                     "pod-a", "test-ns"),
                call(["nsenter --target p1 --net -- iptables -D OUTPUT 1"],
                     "pod-a", "test-ns"),
            ],
            self.kubecli.exec_cmd_in_pod.call_args_list,
        )

    # CHARACTERIZATION: relies on `-I ... 1` insertion order
    def test_clean_network_rules_namespaced_cross_product_delete(self):
        clean_network_rules_namespaced(
            self.kubecli, ["in-1", "in-2"], ["out-1", "out-2"],
            "pod-a", "test-ns", pids=["p1", "p2"],
        )
        self.assertEqual(
            [call([f"nsenter --target {pid} --net -- iptables -D INPUT 1"],
                  "pod-a", "test-ns")
             for _ in ("in-1", "in-2") for pid in ("p1", "p2")]
            + [call([f"nsenter --target {pid} --net -- iptables -D OUTPUT 1"],
                    "pod-a", "test-ns")
               for _ in ("out-1", "out-2") for pid in ("p1", "p2")],
            self.kubecli.exec_cmd_in_pod.call_args_list,
        )
        self.assertEqual(8, self.kubecli.exec_cmd_in_pod.call_count)

    def test_clean_network_rules_namespaced_empty_pids_no_exec(self):
        clean_network_rules_namespaced(
            self.kubecli, ["in-1"], ["out-1"], "pod-a", "test-ns", pids=[]
        )
        self.assertFalse(self.kubecli.exec_cmd_in_pod.called)


class TestGenerateNamespacedRules(unittest.TestCase):
    def test_generate_namespaced_rules_prefixes_every_rule_per_pid(self):
        config = make_cfg(interfaces=["eth0"], ports=[80])
        input_rules, output_rules = generate_namespaced_rules(
            ["eth0"], config, pids=["p1", "p2"]
        )
        expected_input = sorted(
            f"nsenter --target {pid} --net -- iptables -I INPUT 1 -i eth0 "
            f"-p {protocol} --dport 80 -m state --state NEW,RELATED,ESTABLISHED -j DROP"
            for pid in ("p1", "p2")
            for protocol in ("tcp", "udp")
        )
        self.assertEqual(expected_input, sorted(input_rules))
        self.assertEqual(4, len(output_rules))
        for rule in output_rules:
            self.assertTrue(rule.startswith("nsenter --target "))
            self.assertIn(" --net -- iptables -I OUTPUT 1", rule)
            self.assertTrue(
                rule.endswith("-m state --state NEW,RELATED,ESTABLISHED -j DROP")
            )

    def test_generate_namespaced_rules_empty_pids(self):
        config = make_cfg(interfaces=["eth0"], ports=[80])
        input_rules, output_rules = generate_namespaced_rules(
            ["eth0"], config, pids=[]
        )
        self.assertEqual([], input_rules)
        self.assertEqual([], output_rules)


class TestTcVmiChaosDelegation(unittest.TestCase):
    def setUp(self):
        self.kubecli = MagicMock(spec=KrknKubernetes)
        self.config = make_cfg(interfaces=["tap0"], ports=[80])

    def test_apply_tc_vmi_chaos_delegates_to_helpers(self):
        with patch(
            f"{FILTER_MODULE}.generate_namespaced_rules",
            return_value=(["ns-in"], ["ns-out"]),
        ) as generate, patch(f"{FILTER_MODULE}.apply_network_rules") as apply_rules:
            result = apply_tc_vmi_chaos(
                self.kubecli, "chaos-pod", "test-ns", "pid-9", "tap0",
                self.config, parallel=True, vmi_name="vmi-x",
            )
        generate.assert_called_once_with(["tap0"], self.config, ["pid-9"])
        apply_rules.assert_called_once_with(
            self.kubecli, ["ns-in"], ["ns-out"], "chaos-pod", "test-ns",
            True, "vmi-x",
        )
        self.assertEqual((["ns-in"], ["ns-out"]), result)

    def test_clean_tc_vmi_chaos_delegates_to_namespaced_clean(self):
        with patch(f"{FILTER_MODULE}.clean_network_rules_namespaced") as clean:
            clean_tc_vmi_chaos(
                self.kubecli, "chaos-pod", "test-ns", "pid-9", "tap0",
                ["ns-in"], ["ns-out"],
            )
        clean.assert_called_once_with(
            self.kubecli, ["ns-in"], ["ns-out"], "chaos-pod", "test-ns", ["pid-9"]
        )


class TestPodInterfaceProbes(unittest.TestCase):
    def setUp(self):
        self.kubecli = MagicMock(spec=KrknKubernetes)

    def test_get_pod_default_interface_strips_newline(self):
        self.kubecli.exec_cmd_in_pod.return_value = "eth0\n"
        iface = get_pod_default_interface("pod-a", "test-ns", self.kubecli)
        self.assertEqual("eth0", iface)
        self.kubecli.exec_cmd_in_pod.assert_called_once_with(
            ["ip r | grep default | awk '/default/ {print $5}'"],
            "pod-a",
            "test-ns",
        )

    def test_get_pod_default_interface_no_default_route_is_signalled(self):
        self.kubecli.exec_cmd_in_pod.return_value = ""
        try:
            result = get_pod_default_interface("pod-a", "test-ns", self.kubecli)
        except Exception:
            return
        self.assertNotEqual("", result, "missing default route returned ''")

    def test_get_pod_default_interface_multiple_default_routes_returns_first(self):
        self.kubecli.exec_cmd_in_pod.return_value = "eth0\neth1\n"
        self.assertEqual(
            "eth0", get_pod_default_interface("pod-a", "test-ns", self.kubecli)
        )

    def test_get_pod_default_interface_none_exec_result_does_not_crash(self):
        self.kubecli.exec_cmd_in_pod.return_value = None
        try:
            get_pod_default_interface("pod-a", "test-ns", self.kubecli)
        except AttributeError as exc:
            self.fail(f"crashed on None exec result: {exc}")
        except RuntimeError:
            pass



class TestFindVirtLauncherNetnsPid(unittest.TestCase):
    def setUp(self):
        self.kubecli = MagicMock(spec=KrknKubernetes)

    def test_skips_pid_without_tap_and_returns_matching_pid(self):
        self.kubecli.exec_cmd_in_pod.side_effect = ["", "12: tap0: <...>"]
        pid = find_virt_launcher_netns_pid(
            "chaos-pod", "test-ns", ["101", "202"], self.kubecli
        )
        self.assertEqual("202", pid)
        self.assertEqual(
            [
                call(["nsenter --target 101 --net -- ip link show type tun"],
                     "chaos-pod", "test-ns"),
                call(["nsenter --target 202 --net -- ip link show type tun"],
                     "chaos-pod", "test-ns"),
            ],
            self.kubecli.exec_cmd_in_pod.call_args_list,
        )

    def test_no_tap_in_any_netns_returns_none(self):
        self.kubecli.exec_cmd_in_pod.return_value = ""
        self.assertIsNone(
            find_virt_launcher_netns_pid(
                "chaos-pod", "test-ns", ["101", "202"], self.kubecli
            )
        )

    def test_empty_pids_returns_none_without_probing(self):
        self.assertIsNone(
            find_virt_launcher_netns_pid("chaos-pod", "test-ns", [], self.kubecli)
        )
        self.assertFalse(self.kubecli.exec_cmd_in_pod.called)

    def test_exec_failure_on_first_pid_falls_through_to_next(self):
        self.kubecli.exec_cmd_in_pod.side_effect = [
            RuntimeError("nsenter failed"),
            "12: tap0: <...>",
        ]
        with patch(f"{MODULE}.logging") as mock_logging:
            pid = find_virt_launcher_netns_pid(
                "chaos-pod", "test-ns", ["101", "202"], self.kubecli
            )
        self.assertEqual("202", pid)
        mock_logging.warning.assert_called_once()
        self.assertIn("101", mock_logging.warning.call_args[0][0])

    def test_tun_device_not_tap_prefixed_does_not_match(self):
        self.kubecli.exec_cmd_in_pod.return_value = "5: vtap9: <...>"
        pid = find_virt_launcher_netns_pid(
            "chaos-pod", "test-ns", ["101"], self.kubecli
        )
        self.assertIsNone(pid)


class TestVmiTapInterface(unittest.TestCase):
    def setUp(self):
        self.kubecli = MagicMock(spec=KrknKubernetes)

    def test_returns_tap_member_of_k6t_bridge(self):
        self.kubecli.exec_cmd_in_pod.side_effect = ["k6t-ovn-udn1\n", " tap0\n"]
        iface = get_vmi_tap_interface("chaos-pod", "test-ns", "42", self.kubecli)
        self.assertEqual("tap0", iface)
        second_cmd = self.kubecli.exec_cmd_in_pod.call_args_list[1][0][0][0]
        self.assertIn("nsenter --target 42 --net --", second_cmd)
        self.assertIn("master k6t-ovn-udn1", second_cmd)
        self.assertIn("grep ': tap'", second_cmd)

    def test_no_k6t_bridge_returns_empty_without_tap_lookup(self):
        self.kubecli.exec_cmd_in_pod.return_value = ""
        self.assertEqual(
            "", get_vmi_tap_interface("chaos-pod", "test-ns", "42", self.kubecli)
        )
        self.assertEqual(1, self.kubecli.exec_cmd_in_pod.call_count)

    def test_bridge_lookup_returning_none_returns_empty(self):
        self.kubecli.exec_cmd_in_pod.return_value = None
        self.assertEqual(
            "", get_vmi_tap_interface("chaos-pod", "test-ns", "42", self.kubecli)
        )


class TestVmiMasqueradeInterface(unittest.TestCase):
    def setUp(self):
        self.kubecli = MagicMock(spec=KrknKubernetes)

    def test_returns_default_route_device(self):
        self.kubecli.exec_cmd_in_pod.return_value = (
            "default via 169.254.1.1 dev eth0"
        )
        iface = get_vmi_masquerade_interface("chaos-pod", "test-ns", "42", self.kubecli)
        self.assertEqual("eth0", iface)
        self.kubecli.exec_cmd_in_pod.assert_called_once_with(
            ["nsenter --target 42 --net -- ip route show default"],
            "chaos-pod",
            "test-ns",
        )

    def test_no_default_route_returns_empty(self):
        self.kubecli.exec_cmd_in_pod.return_value = ""
        self.assertEqual(
            "", get_vmi_masquerade_interface("chaos-pod", "test-ns", "42", self.kubecli)
        )

    # CHARACTERIZATION: '' is the not-found sentinel
    def test_none_result_returns_empty(self):
        self.kubecli.exec_cmd_in_pod.return_value = None
        self.assertEqual(
            "", get_vmi_masquerade_interface("chaos-pod", "test-ns", "42", self.kubecli)
        )

    # CHARACTERIZATION: '' is the not-found sentinel
    def test_dev_as_last_token_returns_empty(self):
        self.kubecli.exec_cmd_in_pod.return_value = "default via x dev"
        self.assertEqual(
            "", get_vmi_masquerade_interface("chaos-pod", "test-ns", "42", self.kubecli)
        )


class TestSetupNetworkChaosNgScenario(unittest.TestCase):
    def setUp(self):
        self.kubecli = MagicMock(spec=KrknKubernetes)
        self.kubecli.exec_cmd_in_pod.return_value = ""
        self.kubecli.get_container_ids.return_value = ["cid0"]

    def test_autodetects_default_interface_for_node_target(self):
        config = make_cfg(interfaces=[])
        with patch(f"{MODULE}.deploy_network_chaos_ng_pod") as deploy, patch(
            f"{MODULE}.get_pod_default_interface", return_value="eth0"
        ) as default_iface:
            result = setup_network_chaos_ng_scenario(
                config, "node-1", "pod-x", "fedora", self.kubecli,
                target="pod-a", parallel=False, host_network=True,
            )
        deploy.assert_called_once_with(
            config, "node-1", "pod-x", self.kubecli, "fedora", host_network=True
        )
        default_iface.assert_called_once_with("pod-x", "test-ns", self.kubecli)
        self.assertEqual(([], ["eth0"]), result)

    def test_explicit_interfaces_skip_autodetection(self):
        config = make_cfg(interfaces=["eth1", "eth2"])
        with patch(f"{MODULE}.deploy_network_chaos_ng_pod"), patch(
            f"{MODULE}.get_pod_default_interface"
        ) as default_iface:
            result = setup_network_chaos_ng_scenario(
                config, "node-1", "pod-x", "fedora", self.kubecli,
                target="pod-a", parallel=False, host_network=True,
            )
        default_iface.assert_not_called()
        self.assertEqual(([], ["eth1", "eth2"]), result)

    def test_pod_target_resolves_container_ids(self):
        config = make_cfg(interfaces=[])
        with patch(f"{MODULE}.deploy_network_chaos_ng_pod"), patch(
            f"{MODULE}.get_pod_default_interface", return_value="eth0"
        ):
            result = setup_network_chaos_ng_scenario(
                config, "node-1", "pod-x", "fedora", self.kubecli,
                target="pod-a", parallel=False, host_network=False,
            )
        self.kubecli.get_container_ids.assert_called_once_with("pod-a", "test-ns")
        self.assertEqual((["cid0"], ["eth0"]), result)

    def test_no_default_route_never_yields_empty_interface_name(self):
        config = make_cfg(interfaces=[])
        with patch(f"{MODULE}.deploy_network_chaos_ng_pod"):
            try:
                _, interfaces = setup_network_chaos_ng_scenario(
                    config, "node-1", "pod-x", "fedora", self.kubecli,
                    target="pod-a", parallel=False, host_network=True,
                )
            except Exception:
                return
        self.assertNotIn("", interfaces)


class TestLogHelpers(unittest.TestCase):
    def test_log_error_parallel_prefixes_node_name(self):
        with patch(f"{MODULE}.logging") as mock_logging:
            log_error("boom", parallel=True, node_name="node-1")
        mock_logging.error.assert_called_once_with("[node-1]: boom")

    def test_log_error_non_parallel_logs_plain_message(self):
        with patch(f"{MODULE}.logging") as mock_logging:
            log_error("boom")
        mock_logging.error.assert_called_once_with("boom")

    def test_log_warning_parallel_prefixes_node_name(self):
        with patch(f"{MODULE}.logging") as mock_logging:
            log_warning("careful", parallel=True, node_name="node-1")
        mock_logging.warning.assert_called_once_with("[node-1]: careful")

    def test_log_warning_non_parallel_logs_plain_message(self):
        with patch(f"{MODULE}.logging") as mock_logging:
            log_warning("careful")
        mock_logging.warning.assert_called_once_with("careful")
