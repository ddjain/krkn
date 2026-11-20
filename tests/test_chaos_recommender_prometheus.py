import pathlib
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

import krkn.chaos_recommender.prometheus as prometheus_module


# Substring keys used to dispatch fake custom_query responses. They are
# mutually exclusive across the seven metric queries plus kube_pod_info.
CPU_USAGE_KEY = "container_cpu_usage"
CPU_LIMITS_KEY = 'kube_pod_container_resource_limits{resource="cpu"'
NODE_CPU_KEY = 'kube_node_status_capacity{resource="cpu", unit="core"}'
MEM_USAGE_KEY = "container_memory_usage"
MEM_LIMITS_KEY = 'kube_pod_container_resource_limits{resource="memory"'
NODE_MEM_KEY = 'kube_node_status_capacity{resource="memory", unit="byte"}'
NETWORK_KEY = "container_network"
POD_INFO_KEY = "kube_pod_info{pod="


def series(pod, value, **extra_labels):
    """One Prometheus instant-vector entry for a pod metric."""
    metric = {"pod": pod}
    metric.update(extra_labels)
    return {"metric": metric, "value": [1700000000, str(value)]}


def node_series(node, value):
    """One Prometheus instant-vector entry for a node metric."""
    return {"metric": {"node": node}, "value": [1700000000, str(value)]}


class FakePrometheus:
    """Stand-in for PrometheusConnect dispatching custom_query by substring."""

    def __init__(self, responses=None, default=None):
        self.responses = responses or {}
        self.default = [] if default is None else default
        self.query_log = []

    def custom_query(self, query):
        self.query_log.append(query)
        for key, result in self.responses.items():
            if key in query:
                return result
        return self.default

    def queries_matching(self, key):
        return [q for q in self.query_log if key in q]


def full_slots(pods):
    """Seven-slot utilization list in the order the module expects:
    cpu, cpu_limits, mem, mem_limits, network, node_cpu, node_mem."""
    return [
        [series(p, "cpu-" + p) for p in pods],
        [series(p, "cpulim-" + p) for p in pods],
        [series(p, "mem-" + p) for p in pods],
        [series(p, "memlim-" + p) for p in pods],
        [series(p, "net-" + p) for p in pods],
        [node_series("n1", "nodecpu")],
        [node_series("n1", "nodemem")],
    ]


def read_tsv(path):
    return pd.read_csv(path, sep="\t", dtype=str, keep_default_na=False)


def metric_responses(pods, limits=True):
    """Fake custom_query responses keyed by query substring."""
    return {
        CPU_USAGE_KEY: [series(p, "cpu-" + p) for p in pods],
        CPU_LIMITS_KEY: [series(p, "cpulim-" + p) for p in pods] if limits else [],
        NODE_CPU_KEY: [node_series("n1", "nodecpu")],
        MEM_USAGE_KEY: [series(p, "mem-" + p) for p in pods],
        MEM_LIMITS_KEY: [series(p, "memlim-" + p) for p in pods] if limits else [],
        NODE_MEM_KEY: [node_series("n1", "nodemem")],
        NETWORK_KEY: [series(p, "net-" + p) for p in pods],
        POD_INFO_KEY: [{"metric": {"node": "n1"}, "value": [1, "1"]}],
    }


class ChaosRecommenderPrometheusTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = pathlib.Path(tmp.name)

    def _patch(self, target, name, value):
        patcher = patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fetch_env(self, fake):
        """Patch the PrometheusConnect factory and saved_metrics_path."""
        holder = {}

        def factory(**kwargs):
            holder["kwargs"] = kwargs
            return fake

        self._patch(prometheus_module, "PrometheusConnect", factory)
        metrics_path = self.tmp_path / "utilisation.txt"
        self._patch(prometheus_module, "saved_metrics_path", str(metrics_path))
        return holder, metrics_path

    # --- convert_data_to_dataframe ---

    def test_convert_data_to_dataframe_preserves_values_as_strings(self):
        data = [series("a", "123.456789"), series("b", "0")]
        df = prometheus_module.convert_data_to_dataframe(data, "CPU")
        assert list(df.columns) == ["service", "CPU"]
        assert df["service"].tolist() == ["a", "b"]
        assert df["CPU"].tolist() == ["123.456789", "0"]

    def test_convert_data_to_dataframe_empty_data_yields_empty_frame(self):
        df = prometheus_module.convert_data_to_dataframe([], "CPU")
        assert list(df.columns) == ["service", "CPU"]
        assert len(df) == 0

    def test_convert_data_to_dataframe_keeps_duplicate_pods(self):
        data = [series("a", "1"), series("a", "2")]
        df = prometheus_module.convert_data_to_dataframe(data, "CPU")
        assert df["service"].tolist() == ["a", "a"]
        assert df["CPU"].tolist() == ["1", "2"]

    def test_convert_data_to_dataframe_preserves_prometheus_specials(self):
        data = [series("a", "NaN"), series("b", "+Inf"), series("c", "1e+09")]
        df = prometheus_module.convert_data_to_dataframe(data, "CPU")
        assert df["CPU"].tolist() == ["NaN", "+Inf", "1e+09"]

    def test_convert_data_to_dataframe_missing_pod_label_raises(self):
        data = [{"metric": {}, "value": [1700000000, "5"]}]
        with self.assertRaisesRegex(KeyError, "pod"):
            prometheus_module.convert_data_to_dataframe(data, "CPU")

    def test_convert_data_to_dataframe_missing_value_key_raises(self):
        data = [{"metric": {"pod": "a"}}]
        with self.assertRaisesRegex(KeyError, "value"):
            prometheus_module.convert_data_to_dataframe(data, "CPU")

    def test_convert_data_to_dataframe_short_value_raises(self):
        data = [{"metric": {"pod": "a"}, "value": [1700000000]}]
        with self.assertRaises(IndexError):
            prometheus_module.convert_data_to_dataframe(data, "CPU")

    def test_convert_data_to_dataframe_service_label_overwrites_pod_column(self):
        # Characterization: passing label "service" collides with the pod column
        # and the metric values overwrite the pod names.
        # characterization: label 'service' collides with the pod column;
        # production labels are never 'service'
        df = prometheus_module.convert_data_to_dataframe([series("a", "5")], "service")
        assert list(df.columns) == ["service"]
        assert df["service"].tolist() == ["5"]

    # --- convert_data ---

    def test_convert_data_returns_value_string_for_known_pod(self):
        assert prometheus_module.convert_data([series("a", "7")], "a") == "7"

    def test_convert_data_returns_none_for_unknown_pod(self):
        assert prometheus_module.convert_data([series("a", "7")], "b") is None

    def test_convert_data_empty_data_returns_none(self):
        assert prometheus_module.convert_data([], "a") is None

    def test_convert_data_duplicate_pod_last_wins(self):
        data = [series("a", "1"), series("a", "2")]
        assert prometheus_module.convert_data(data, "a") == "2"

    def test_convert_data_none_service_returns_none(self):
        assert prometheus_module.convert_data([series("a", "1")], None) is None

    def test_convert_data_missing_pod_label_raises(self):
        data = [{"metric": {}, "value": [1700000000, "5"]}]
        with self.assertRaisesRegex(KeyError, "pod"):
            prometheus_module.convert_data(data, "a")

    # --- convert_data_limits ---

    def test_convert_data_limits_returns_limit_when_pod_has_limit(self):
        prom = FakePrometheus({POD_INFO_KEY: [{"metric": {"node": "n1"}, "value": [1, "1"]}]})
        result = prometheus_module.convert_data_limits(
            [series("a", "4")], [node_series("n1", "16000")], "a", prom
        )
        assert result == "4"

    def test_convert_data_limits_does_not_query_pod_info_when_pod_has_limit(self):
        prom = FakePrometheus({POD_INFO_KEY: [{"metric": {"node": "n1"}, "value": [1, "1"]}]})
        prometheus_module.convert_data_limits(
            [series("a", "4")], [node_series("n1", "16000")], "a", prom
        )
        assert not prom.queries_matching(POD_INFO_KEY)

    def test_convert_data_limits_pod_missing_falls_back_to_node_capacity(self):
        prom = FakePrometheus({POD_INFO_KEY: [{"metric": {"node": "n1"}, "value": [1, "1"]}]})
        result = prometheus_module.convert_data_limits(
            [series("other", "4")], [node_series("n1", "16000")], "a", prom
        )
        assert result == "16000"

    def test_convert_data_limits_empty_limits_uses_node_capacity(self):
        prom = FakePrometheus({POD_INFO_KEY: [{"metric": {"node": "n1"}, "value": [1, "1"]}]})
        result = prometheus_module.convert_data_limits(
            [], [node_series("n1", "8000")], "a", prom
        )
        assert result == "8000"

    def test_convert_data_limits_pod_info_empty_returns_none(self):
        prom = FakePrometheus()
        result = prometheus_module.convert_data_limits(
            [series("other", "4")], [node_series("n1", "16000")], "a", prom
        )
        assert result is None

    def test_convert_data_limits_node_absent_returns_sentinel(self):
        prom = FakePrometheus({POD_INFO_KEY: [{"metric": {"node": "n1"}, "value": [1, "1"]}]})
        result = prometheus_module.convert_data_limits(
            [series("other", "4")], [node_series("n2", "16000")], "a", prom
        )
        assert result == "1000000000"
        assert isinstance(result, str)

    def test_convert_data_limits_query_failure_raises_despite_defined_limit(self):
        prom = MagicMock()
        prom.custom_query.side_effect = RuntimeError("boom")
        result = prometheus_module.convert_data_limits(
            [series("a", "4")], [node_series("n1", "16000")], "a", prom
        )
        assert result == "4"

    # --- get_node_capacity ---

    def test_get_node_capacity_queries_pod_info_with_exact_query(self):
        prom = FakePrometheus({POD_INFO_KEY: [{"metric": {"node": "n1"}, "value": [1, "1"]}]})
        prometheus_module.get_node_capacity([node_series("n1", "16000")], "mypod", prom)
        assert prom.query_log == ['kube_pod_info{pod="mypod"}']

    def test_get_node_capacity_returns_node_value(self):
        prom = FakePrometheus({POD_INFO_KEY: [{"metric": {"node": "n1"}, "value": [1, "1"]}]})
        result = prometheus_module.get_node_capacity(
            [node_series("n1", "16000"), node_series("n2", "32000")], "a", prom
        )
        assert result == "16000"

    def test_get_node_capacity_empty_pod_info_returns_none(self):
        prom = FakePrometheus()
        assert prometheus_module.get_node_capacity([node_series("n1", "16000")], "a", prom) is None

    def test_get_node_capacity_missing_node_label_raises(self):
        prom = FakePrometheus({POD_INFO_KEY: [{"metric": {"pod": "a"}, "value": [1, "1"]}]})
        with self.assertRaisesRegex(KeyError, "node"):
            prometheus_module.get_node_capacity([node_series("n1", "16000")], "a", prom)

    def test_get_node_capacity_node_absent_returns_cpu_sentinel(self):
        prom = FakePrometheus({POD_INFO_KEY: [{"metric": {"node": "n1"}, "value": [1, "1"]}]})
        result = prometheus_module.get_node_capacity([node_series("n2", "32000")], "a", prom)
        assert result == "1000000000"
        assert isinstance(result, str)

    def test_get_node_capacity_first_matching_node_entry_wins(self):
        prom = FakePrometheus({POD_INFO_KEY: [{"metric": {"node": "n1"}, "value": [1, "1"]}]})
        node_data = [node_series("n1", "16000"), node_series("n1", "32000")]
        assert prometheus_module.get_node_capacity(node_data, "a", prom) == "16000"

    def test_get_node_capacity_pod_name_injected_verbatim(self):
        # A quote in the pod name must not break out of the label matcher. Escaping
        # or rejecting the name are both acceptable; the raw injection is not.
        prom = FakePrometheus({POD_INFO_KEY: []})
        try:
            prometheus_module.get_node_capacity([], 'p"}', prom)
        except ValueError:
            return
        assert not any('p"}' in q for q in prom.query_log)

    # --- save_utilization_to_file ---

    def test_save_utilization_to_file_writes_full_tsv(self):
        path = self.tmp_path / "utilisation.txt"
        prometheus_module.save_utilization_to_file(
            {"ns1": full_slots(["a", "b"])}, str(path), FakePrometheus()
        )
        lines = path.read_text().splitlines()
        assert lines[0] == "namespace\tservice\tCPU\tCPU_LIMITS\tMEM\tMEM_LIMITS\tNETWORK"
        df = read_tsv(path)
        assert df.values.tolist() == [
            ["ns1", "a", "cpu-a", "cpulim-a", "mem-a", "memlim-a", "net-a"],
            ["ns1", "b", "cpu-b", "cpulim-b", "mem-b", "memlim-b", "net-b"],
        ]

    def test_save_utilization_to_file_rows_driven_by_cpu_pods_only(self):
        path = self.tmp_path / "utilisation.txt"
        slots = full_slots(["a"])
        for idx in (2, 3, 4):  # mem, mem limits, network also report pod "x"
            slots[idx].append(series("x", "value-x"))
        prometheus_module.save_utilization_to_file({"ns1": slots}, str(path), FakePrometheus())
        df = read_tsv(path)
        assert df["service"].tolist() == ["a"]

    def test_save_utilization_to_file_missing_metrics_written_as_none(self):
        path = self.tmp_path / "utilisation.txt"
        slots = full_slots(["a"])
        slots[2] = []  # no memory usage result
        slots[4] = []  # no network result
        prometheus_module.save_utilization_to_file({"ns1": slots}, str(path), FakePrometheus())
        row = read_tsv(path).values.tolist()[0]
        # characterization: the literal string 'None' comes from astype(str)
        assert row == ["ns1", "a", "cpu-a", "cpulim-a", "None", "memlim-a", "None"]

    def test_save_utilization_to_file_limits_fallback_uses_node_capacity(self):
        path = self.tmp_path / "utilisation.txt"
        slots = full_slots(["a"])
        slots[1] = []  # no cpu limits for the pod
        slots[3] = []  # no mem limits for the pod
        slots[5] = [node_series("n1", "16000")]
        slots[6] = [node_series("n1", "64000000")]
        prom = FakePrometheus(
            {POD_INFO_KEY: [{"metric": {"node": "n1"}, "value": [1, "1"]}]}
        )
        prometheus_module.save_utilization_to_file({"ns1": slots}, str(path), prom)
        row = read_tsv(path).values.tolist()[0]
        assert row == ["ns1", "a", "cpu-a", "16000", "mem-a", "64000000", "net-a"]

    def test_save_utilization_to_file_limits_fallback_none(self):
        path = self.tmp_path / "utilisation.txt"
        slots = full_slots(["a"])
        slots[1] = []
        slots[3] = []
        prometheus_module.save_utilization_to_file({"ns1": slots}, str(path), FakePrometheus())
        row = read_tsv(path).values.tolist()[0]
        # characterization: the literal string 'None' comes from astype(str)
        assert row == ["ns1", "a", "cpu-a", "None", "mem-a", "None", "net-a"]

    def test_save_utilization_to_file_duplicate_cpu_pod_single_row(self):
        path = self.tmp_path / "utilisation.txt"
        slots = full_slots(["a"])
        slots[0].append(series("a", "cpu-a-second"))
        prometheus_module.save_utilization_to_file({"ns1": slots}, str(path), FakePrometheus())
        df = read_tsv(path)
        assert df["service"].tolist() == ["a"]
        assert df["CPU"].tolist() == ["cpu-a-second"]

    def test_save_utilization_to_file_empty_utilization_writes_header_only(self):
        path = self.tmp_path / "utilisation.txt"
        prometheus_module.save_utilization_to_file({}, str(path), FakePrometheus())
        lines = path.read_text().splitlines()
        assert lines == ["namespace\tservice\tCPU\tCPU_LIMITS\tMEM\tMEM_LIMITS\tNETWORK"]

    def test_save_utilization_to_file_skips_namespace_with_empty_cpu(self):
        path = self.tmp_path / "utilisation.txt"
        utilization = {"empty-ns": [[]] * 7, "ns2": full_slots(["a"])}
        prometheus_module.save_utilization_to_file(utilization, str(path), FakePrometheus())
        df = read_tsv(path)
        assert df["namespace"].tolist() == ["ns2"]
        assert df["service"].tolist() == ["a"]

    def test_save_utilization_to_file_short_utilization_raises(self):
        # Five slots with every pod limited: node slots are only needed for the
        # fallback, so the file should still be written.
        path = self.tmp_path / "utilisation.txt"
        slots = full_slots(["a"])[:5]
        prometheus_module.save_utilization_to_file({"ns1": slots}, str(path), FakePrometheus())
        row = read_tsv(path).values.tolist()[0]
        assert row == ["ns1", "a", "cpu-a", "cpulim-a", "mem-a", "memlim-a", "net-a"]

    def test_save_utilization_to_file_preserves_special_values(self):
        path = self.tmp_path / "utilisation.txt"
        slots = full_slots(["a"])
        slots[0] = [series("a", "NaN")]
        slots[2] = [series("a", "+Inf")]
        prometheus_module.save_utilization_to_file({"ns1": slots}, str(path), FakePrometheus())
        row = read_tsv(path).values.tolist()[0]
        assert row[2] == "NaN"
        assert row[4] == "+Inf"

    def test_save_utilization_to_file_multi_namespace(self):
        path = self.tmp_path / "utilisation.txt"
        utilization = {"ns1": full_slots(["a"]), "ns2": full_slots(["b"])}
        prometheus_module.save_utilization_to_file(utilization, str(path), FakePrometheus())
        rows = read_tsv(path).values.tolist()
        assert rows == [
            ["ns1", "a", "cpu-a", "cpulim-a", "mem-a", "memlim-a", "net-a"],
            ["ns2", "b", "cpu-b", "cpulim-b", "mem-b", "memlim-b", "net-b"],
        ]

    # --- fetch_utilization_from_prometheus ---

    def test_fetch_utilization_from_prometheus_returns_path_and_writes_file(self):
        fake = FakePrometheus(metric_responses(["a"]))
        _, path = self._fetch_env(fake)
        result = prometheus_module.fetch_utilization_from_prometheus(
            "http://prom:9090", "secret", ["ns1"], "5m"
        )
        assert result[0] == str(path)
        assert set(result[1]) == {"ns1"}
        rows = read_tsv(path).values.tolist()
        assert rows == [["ns1", "a", "cpu-a", "cpulim-a", "mem-a", "memlim-a", "net-a"]]

    def test_fetch_utilization_from_prometheus_connects_with_bearer_token(self):
        fake = FakePrometheus(metric_responses(["a"]))
        holder, _ = self._fetch_env(fake)
        prometheus_module.fetch_utilization_from_prometheus(
            "http://prom:9090", "secret", ["ns1"], "5m"
        )
        assert holder["kwargs"] == {
            "url": "http://prom:9090",
            "headers": {"Authorization": "Bearer secret"},
            "disable_ssl": True,
        }

    def test_fetch_utilization_from_prometheus_exact_query_strings(self):
        fake = FakePrometheus(metric_responses(["a"]))
        self._fetch_env(fake)
        _, queries = prometheus_module.fetch_utilization_from_prometheus(
            "http://prom:9090", "secret", ["ns1"], "5m"
        )
        actual = dict(queries["ns1"])
        # Whitespace-normalised: the raw memory_limit and network strings carry
        # incidental runs of spaces from the source literals.
        actual["memory_limit_query"] = " ".join(actual["memory_limit_query"].split())
        actual["network_query"] = " ".join(actual["network_query"].split())
        assert actual == {
            "cpu_query": 'sum (rate (container_cpu_usage_seconds_total{image!="", '
            'namespace="ns1"}[5m])) by (pod) *1000',
            "cpu_limit_query": '(sum by (pod) (kube_pod_container_resource_limits'
            '{resource="cpu", namespace="ns1"}))*1000',
            "memory_query": 'sum by (pod) (avg_over_time(container_memory_usage_bytes'
            '{image!="", namespace="ns1"}[5m]))',
            "memory_limit_query": 'sum by (pod) (kube_pod_container_resource_limits'
            '{resource="memory", namespace="ns1"})',
            "network_query": 'sum by (pod) ((avg_over_time('
            'container_network_transmit_bytes_total{namespace="ns1"}[5m])) + '
            '(avg_over_time(container_network_receive_bytes_total{namespace="ns1"}[5m])))',
        }

    def test_fetch_utilization_from_prometheus_metric_query_order(self):
        fake = FakePrometheus(metric_responses(["a"]))
        self._fetch_env(fake)
        prometheus_module.fetch_utilization_from_prometheus(
            "http://prom:9090", "secret", ["ns1"], "5m"
        )
        metric_queries = [q for q in fake.query_log if POD_INFO_KEY not in q]
        assert len(metric_queries) == 7
        prefixes = (
            "sum (rate (container_cpu_usage",
            '(sum by (pod) (kube_pod_container_resource_limits{resource="cpu"',
            'kube_node_status_capacity{resource="cpu", unit="core"}',
            "sum by (pod) (avg_over_time(container_memory",
            'sum by (pod) (kube_pod_container_resource_limits{resource="memory"',
            'kube_node_status_capacity{resource="memory", unit="byte"}',
            "sum by (pod) ((avg_over_time(container_network",
        )
        for query, prefix in zip(metric_queries, prefixes):
            assert query.startswith(prefix)

    def test_fetch_utilization_from_prometheus_multiple_namespaces(self):
        fake = FakePrometheus(metric_responses(["p1"]))
        _, path = self._fetch_env(fake)
        _, queries = prometheus_module.fetch_utilization_from_prometheus(
            "http://prom:9090", "secret", ["ns-a", "ns-b"], "5m"
        )
        assert set(queries) == {"ns-a", "ns-b"}
        metric_queries = [q for q in fake.query_log if POD_INFO_KEY not in q]
        assert len(metric_queries) == 14
        assert 'namespace="ns-b"' in queries["ns-b"]["cpu_query"]
        assert [row[0] for row in read_tsv(path).values.tolist()] == ["ns-a", "ns-b"]

    def test_fetch_utilization_from_prometheus_slot_order_via_node_fallback(self):
        # Without pod limits the node-capacity slots land in CPU_LIMITS and
        # MEM_LIMITS, pinning the utilization slot order
        # [cpu, cpu_limits, mem, mem_limits, network, node_cpu, node_mem].
        fake = FakePrometheus(metric_responses(["a"], limits=False))
        _, path = self._fetch_env(fake)
        prometheus_module.fetch_utilization_from_prometheus(
            "http://prom:9090", "secret", ["ns1"], "5m"
        )
        row = read_tsv(path).values.tolist()[0]
        assert row == ["ns1", "a", "cpu-a", "nodecpu", "mem-a", "nodemem", "net-a"]

    def test_fetch_utilization_from_prometheus_empty_namespaces(self):
        fake = FakePrometheus(metric_responses(["a"]))
        _, path = self._fetch_env(fake)
        result = prometheus_module.fetch_utilization_from_prometheus(
            "http://prom:9090", "secret", [], "5m"
        )
        assert result == (str(path), {})
        assert fake.query_log == []
        assert path.read_text().splitlines() == [
            "namespace\tservice\tCPU\tCPU_LIMITS\tMEM\tMEM_LIMITS\tNETWORK"
        ]

    def test_fetch_utilization_from_prometheus_query_failure_propagates(self):
        # The caller in chaos_recommender.py catches prometheus.PrometheusConnectionError
        # and prometheus.PrometheusQueryError, which this module never defines, so a
        # failing query must surface as PrometheusQueryError.
        fake = MagicMock()
        fake.custom_query.side_effect = RuntimeError("prom down")
        self._fetch_env(fake)
        assert hasattr(prometheus_module, "PrometheusConnectionError")
        assert hasattr(prometheus_module, "PrometheusQueryError")
        with self.assertRaisesRegex(prometheus_module.PrometheusQueryError, "prom down"):
            prometheus_module.fetch_utilization_from_prometheus(
                "http://prom:9090", "secret", ["ns1"], "5m"
            )
