import importlib
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

# NOTE: krkn.chaos_recommender/__init__.py star-imports this module's
# functions, so the package attribute "analysis" is the *function*; resolve
# the module itself through importlib (sys.modules entry).
analysis_module = importlib.import_module("krkn.chaos_recommender.analysis")

TELEMETRY = (
    "namespace\tservice\tCPU\tCPU_LIMITS\tMEM\tMEM_LIMITS\tNETWORK\n"
    "ns1\tweb\t100\t200\t300\t400\t50\n"
    "ns1\tcart\t400\t500\t900\t1000\t10\n"
    "ns2\tdb\t50\t100\t80\t160\t5000\n"
)

CHAOS_TESTS = {"CPU": ["cpu.yml"], "MEM": ["mem.yml"], "NETWORK": ["net.yml"]}


def write_telemetry(tmp_path, text, name="utilisation.txt"):
    path = tmp_path / name
    path.write_text(text)
    return path


class LoadTelemetryDataTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = pathlib.Path(tmp.name)

    def test_load_telemetry_data_tab_separated_prod_file(self):
        path = write_telemetry(self.tmp_path, TELEMETRY)
        df = analysis_module.load_telemetry_data(path)
        assert list(df.columns) == [
            "namespace",
            "service",
            "CPU",
            "CPU_LIMITS",
            "MEM",
            "MEM_LIMITS",
            "NETWORK",
        ]
        assert len(df) == 3
        assert pd.api.types.is_numeric_dtype(df["CPU"])
        assert pd.api.types.is_numeric_dtype(df["MEM"])
        assert pd.api.types.is_numeric_dtype(df["NETWORK"])
        assert df["service"].tolist() == ["web", "cart", "db"]

    def test_load_telemetry_data_space_delimited_file_parses_identically(self):
        space_rows = TELEMETRY.replace("\t", " ")
        path = write_telemetry(self.tmp_path, space_rows)
        df = analysis_module.load_telemetry_data(path)
        tab_df = analysis_module.load_telemetry_data(
            write_telemetry(self.tmp_path, TELEMETRY, "tab.txt")
        )
        pd.testing.assert_frame_equal(df, tab_df)

    def test_load_telemetry_data_nonexistent_path_raises(self):
        with self.assertRaises(FileNotFoundError):
            analysis_module.load_telemetry_data(
                self.tmp_path / "does-not-exist.txt"
            )

    def test_load_telemetry_data_empty_file_raises_empty_data_error(self):
        path = write_telemetry(self.tmp_path, "")
        with self.assertRaises(pd.errors.EmptyDataError):
            analysis_module.load_telemetry_data(path)

    def test_load_telemetry_data_none_and_nan_tokens_keep_columns_aligned(self):
        # save_utilization_to_file writes missing values as the literal tokens
        # 'None' / 'nan' (astype(str)); they must parse as NaN without
        # shifting the following columns.
        path = write_telemetry(
            self.tmp_path,
            "namespace\tservice\tCPU\tCPU_LIMITS\tMEM\tMEM_LIMITS\tNETWORK\n"
            "ns1\tweb\tNone\t200\t300\t400\t50\n"
            "ns1\tcart\tnan\t500\t900\t1000\t10\n",
        )
        df = analysis_module.load_telemetry_data(path)
        assert df["CPU"].isna().all()
        assert df["CPU_LIMITS"].tolist() == [200, 500]
        assert df["MEM"].tolist() == [300, 900]
        assert df["MEM_LIMITS"].tolist() == [400, 1000]
        assert df["NETWORK"].tolist() == [50, 10]
        assert df["service"].tolist() == ["web", "cart"]

    def test_load_telemetry_data_header_only_file_keeps_columns(self):
        header = TELEMETRY.splitlines()[0] + "\n"
        path = write_telemetry(self.tmp_path, header)
        df = analysis_module.load_telemetry_data(path)
        assert len(df) == 0
        assert list(df.columns) == [
            "namespace",
            "service",
            "CPU",
            "CPU_LIMITS",
            "MEM",
            "MEM_LIMITS",
            "NETWORK",
        ]

    def test_load_telemetry_data_trailing_blank_lines_skipped(self):
        path = write_telemetry(
            self.tmp_path,
            "namespace\tservice\tCPU\tCPU_LIMITS\tMEM\tMEM_LIMITS\tNETWORK\n"
            "ns1\tweb\t100\t200\t300\t400\t50\n"
            "\n"
            "\n",
        )
        df = analysis_module.load_telemetry_data(path)
        assert len(df) == 1
        assert df["service"].tolist() == ["web"]


class CalculateZscoresTest(unittest.TestCase):
    def setUp(self):
        self.data = pd.DataFrame(
            {
                "namespace": ["ns1", "ns1", "ns2"],
                "service": ["web", "cart", "db"],
                "CPU": [100.0, 400.0, 50.0],
                "MEM": [300.0, 900.0, 80.0],
                "NETWORK": [50.0, 10.0, 5000.0],
            }
        )

    def test_calculate_zscores_happy_path_columns_and_standardization(self):
        zscores = analysis_module.calculate_zscores(self.data)
        assert list(zscores.columns) == [
            "Namespace",
            "Service",
            "CPU",
            "Memory",
            "Network",
        ]
        assert zscores["Namespace"].tolist() == ["ns1", "ns1", "ns2"]
        assert zscores["Service"].tolist() == ["web", "cart", "db"]
        self.assertAlmostEqual(zscores["CPU"].mean(), 0.0, places=9)
        self.assertAlmostEqual(zscores["CPU"].std(), 1.0, places=9)
        self.assertAlmostEqual(zscores["Memory"].mean(), 0.0, places=9)
        self.assertAlmostEqual(zscores["Network"].std(), 1.0, places=9)

    def test_calculate_zscores_single_row_yields_all_nan(self):
        # CHARACTERIZATION: std() uses ddof=1: one sample -> NaN std -> every zscore NaN.
        data = self.data[self.data["namespace"] == "ns2"].reset_index(drop=True)
        zscores = analysis_module.calculate_zscores(data)
        assert zscores["CPU"].isna().all()
        assert zscores["Memory"].isna().all()
        assert zscores["Network"].isna().all()
        assert zscores["Service"].tolist() == ["db"]

    def test_calculate_zscores_all_equal_values_yield_nan_not_inf(self):
        data = pd.DataFrame(
            {
                "namespace": ["ns1"] * 3,
                "service": ["a", "b", "c"],
                "CPU": [5.0, 5.0, 5.0],
                "MEM": [1.0, 2.0, 3.0],
                "NETWORK": [1.0, 2.0, 9.0],
            }
        )
        zscores = analysis_module.calculate_zscores(data)
        assert zscores["CPU"].isna().all()
        assert not (zscores["CPU"] == float("inf")).any()

    def test_calculate_zscores_nan_metric_cell_standardizes_rest(self):
        # A missing Prometheus sample reads back as NaN; mean()/std() skip
        # it, so the NaN row gets a NaN zscore (never flagged as outlier)
        # while the remaining rows are standardized over the non-NaN subset.
        data = self.data.copy()
        data.loc[1, "CPU"] = float("nan")
        zscores = analysis_module.calculate_zscores(data)
        assert pd.isna(zscores.loc[1, "CPU"])
        self.assertAlmostEqual(zscores.loc[0, "CPU"], 0.70710678, places=6)
        self.assertAlmostEqual(zscores.loc[2, "CPU"], -0.70710678, places=6)
        assert not zscores["Memory"].isna().any()

    def test_calculate_zscores_single_non_nan_value_collapses_column(self):
        # CHARACTERIZATION: one NaN and one remaining sample -> std() (ddof=1)
        # is NaN, so every zscore in the column is NaN.
        data = self.data[self.data["service"].isin(["web", "cart"])].copy()
        data.loc[data.index[1], "CPU"] = float("nan")
        zscores = analysis_module.calculate_zscores(data)
        assert zscores["CPU"].isna().all()
        assert not zscores["Memory"].isna().any()

    def test_calculate_zscores_non_numeric_cell_raises_type_error(self):
        data = pd.DataFrame(
            {
                "namespace": ["ns1", "ns1", "ns2"],
                "service": ["web", "cart", "db"],
                "CPU": [100.0, "banana", 50.0],
                "MEM": [300.0, 900.0, 80.0],
                "NETWORK": [50.0, 10.0, 5000.0],
            }
        )
        with self.assertRaisesRegex(TypeError, "str|banana"):
            analysis_module.calculate_zscores(data)

    def test_calculate_zscores_lowercase_metric_header_raises_key_error(self):
        data = pd.DataFrame(
            {
                "namespace": ["ns1"],
                "service": ["web"],
                "cpu": [100.0],
                "MEM": [300.0],
                "NETWORK": [50.0],
            }
        )
        with self.assertRaisesRegex(KeyError, "CPU"):
            analysis_module.calculate_zscores(data)


class IdentifyOutliersTest(unittest.TestCase):
    def setUp(self):
        self.data = pd.DataFrame(
            {
                "Service": ["web", "cart", "db", "cache"],
                "CPU": [2.0, 1.0, float("nan"), float("inf")],
                "Memory": [2.0, 1.0, float("nan"), 0.5],
                "Network": [0.5, 1.0, float("nan"), 2.5],
            }
        )

    def test_identify_outliers_threshold_boundary_excluded(self):
        # Comparison is strictly greater-than: z == threshold is not an
        # outlier (web CPU/Memory are exactly 2.0).
        outliers_cpu, outliers_memory, outliers_network = (
            analysis_module.identify_outliers(self.data, 2.0)
        )
        assert outliers_cpu == ["cache"]
        assert outliers_memory == []
        assert outliers_network == ["cache"]

    def test_identify_outliers_string_threshold_coerced(self):
        # Thresholds arrive as strings from the CLI arguments.
        result_str = analysis_module.identify_outliers(self.data, "2.0")
        result_float = analysis_module.identify_outliers(self.data, 2.0)
        assert result_str == result_float

    def test_identify_outliers_malformed_threshold_raises_value_error(self):
        with self.assertRaisesRegex(ValueError, "abc"):
            analysis_module.identify_outliers(self.data, "abc")

    def test_identify_outliers_nan_rows_never_flagged(self):
        outliers_cpu, outliers_memory, outliers_network = (
            analysis_module.identify_outliers(self.data, -1.0)
        )
        for lst in (outliers_cpu, outliers_memory, outliers_network):
            assert "db" not in lst

    def test_identify_outliers_inf_zscore_flagged(self):
        # inf > any finite threshold: cache CPU is inf.
        outliers_cpu, _, _ = analysis_module.identify_outliers(self.data, 100.0)
        assert outliers_cpu == ["cache"]

    def test_identify_outliers_empty_frame_returns_empty_lists(self):
        empty = pd.DataFrame(columns=["Service", "CPU", "Memory", "Network"])
        assert analysis_module.identify_outliers(empty, 2.0) == ([], [], [])

    def test_identify_outliers_negative_threshold_flags_all_finite(self):
        outliers_cpu, outliers_memory, outliers_network = (
            analysis_module.identify_outliers(self.data, -1.0)
        )
        assert outliers_cpu == ["web", "cart", "cache"]
        assert outliers_memory == ["web", "cart", "cache"]
        assert outliers_network == ["web", "cart", "cache"]


class GetServicesAboveHeatmapThresholdTest(unittest.TestCase):
    def setUp(self):
        self.dataframe = pd.DataFrame(
            {
                "service": ["boundary", "over", "under"],
                "CPU": [80.0, 90.0, 10.0],
                "CPU_LIMITS": [100.0, 100.0, 100.0],
                "MEM": [80.0, 10.0, 90.0],
                "MEM_LIMITS": [100.0, 100.0, 100.0],
            }
        )

    def test_heatmap_ratio_boundary_excluded(self):
        # Ratio exactly == threshold is excluded (strict >): boundary is
        # 80/100 == 0.8 for both CPU and MEM.
        cpu_services, mem_services = (
            analysis_module.get_services_above_heatmap_threshold(
                self.dataframe, 0.8, 0.8
            )
        )
        assert cpu_services == ["over"]
        assert mem_services == ["under"]

    def test_heatmap_string_thresholds_coerced(self):
        # CLI passes thresholds as strings.
        result_str = analysis_module.get_services_above_heatmap_threshold(
            self.dataframe, "0.8", "0.8"
        )
        result_float = analysis_module.get_services_above_heatmap_threshold(
            self.dataframe, 0.8, 0.8
        )
        assert result_str == result_float

    def test_heatmap_nan_limit_service_silently_excluded(self):
        # CHARACTERIZATION: a missing limit reads back as NaN (the 'None'/'nan'
        # tokens the producer writes) -> ratio NaN -> the service is never flagged.
        dataframe = pd.DataFrame(
            {
                "service": ["nolimit"],
                "CPU": [50.0],
                "CPU_LIMITS": [float("nan")],
                "MEM": [50.0],
                "MEM_LIMITS": [float("nan")],
            }
        )
        cpu_services, mem_services = (
            analysis_module.get_services_above_heatmap_threshold(
                dataframe, 0.8, 0.8
            )
        )
        assert cpu_services == []
        assert mem_services == []

    def test_heatmap_huge_node_capacity_fallback_excluded(self):
        # CHARACTERIZATION: 1e9 node-capacity fallback -> ratio ~5e-8 -> not flagged.
        dataframe = pd.DataFrame(
            {
                "service": ["fallback"],
                "CPU": [50.0],
                "CPU_LIMITS": [1000000000.0],
                "MEM": [50.0],
                "MEM_LIMITS": [1000000000.0],
            }
        )
        cpu_services, mem_services = (
            analysis_module.get_services_above_heatmap_threshold(
                dataframe, 0.8, 0.8
            )
        )
        assert cpu_services == []
        assert mem_services == []

    def test_heatmap_missing_limits_column_raises_key_error(self):
        dataframe = pd.DataFrame(
            {
                "service": ["web"],
                "CPU": [50.0],
                "MEM": [50.0],
                "MEM_LIMITS": [100.0],
            }
        )
        with self.assertRaisesRegex(KeyError, "CPU_LIMITS"):
            analysis_module.get_services_above_heatmap_threshold(
                dataframe, 0.8, 0.8
            )

    def test_heatmap_empty_dataframe_returns_empty_lists(self):
        dataframe = pd.DataFrame(
            columns=["service", "CPU", "CPU_LIMITS", "MEM", "MEM_LIMITS"]
        )
        assert analysis_module.get_services_above_heatmap_threshold(
            dataframe, 0.8, 0.8
        ) == ([], [])

    def test_heatmap_zero_limit_yields_infinite_ratio_and_is_flagged(self):
        # CHARACTERIZATION: limit 0 -> ratio inf -> flagged (matches the log text)
        dataframe = pd.DataFrame(
            {
                "service": ["unlimited"],
                "CPU": [50.0],
                "CPU_LIMITS": [0.0],
                "MEM": [50.0],
                "MEM_LIMITS": [0.0],
            }
        )
        cpu_services, mem_services = (
            analysis_module.get_services_above_heatmap_threshold(
                dataframe, 0.8, 0.8
            )
        )
        assert ["unlimited"] == cpu_services
        assert ["unlimited"] == mem_services

    def test_heatmap_none_limit_token_from_file_never_flagged(self):
        # CHARACTERIZATION: 'None' limit token (what save_utilization_to_file
        # writes when no node capacity exists) -> NaN -> ratio NaN -> never flagged.
        with tempfile.TemporaryDirectory() as tmp:
            path = write_telemetry(
                pathlib.Path(tmp),
                "namespace\tservice\tCPU\tCPU_LIMITS\tMEM\tMEM_LIMITS\tNETWORK\n"
                "ns1\tweb\t50\tNone\t50\tNone\t10\n",
            )
            dataframe = analysis_module.load_telemetry_data(path)
        cpu_services, mem_services = (
            analysis_module.get_services_above_heatmap_threshold(
                dataframe, 0.8, 0.8
            )
        )
        assert cpu_services == []
        assert mem_services == []


class ReturnCriticalServicesTest(unittest.TestCase):
    def test_return_critical_services_returns_placeholder_list(self):
        # Placeholder helper pending topology-based implementation.
        assert analysis_module.return_critical_services() == ["web", "cart"]


class AnalysisJsonTest(unittest.TestCase):
    def test_analysis_json_all_inputs_empty_skips_recommendations(self):
        result = analysis_module.analysis_json([], [], [], [], [], CHAOS_TESTS)
        profiling, heatmap, recommendations = result
        assert recommendations == {}
        assert profiling == {
            "cpu_outliers": [],
            "memory_outliers": [],
            "network_outliers": [],
        }
        assert heatmap == {
            "services_with_cpu_heatmap_above_threshold": [],
            "services_with_mem_heatmap_above_threshold": [],
        }

    def test_analysis_json_only_cpu_services(self):
        _, _, recommendations = analysis_module.analysis_json(
            [], [], [], ["web"], [], CHAOS_TESTS
        )
        assert recommendations == {
            "cpu_services_recommendations": {
                "services": ["web"],
                "tests": ["cpu.yml"],
            }
        }

    def test_analysis_json_only_mem_services(self):
        _, _, recommendations = analysis_module.analysis_json(
            [], [], [], [], ["cart"], CHAOS_TESTS
        )
        assert recommendations == {
            "mem_services_recommendations": {
                "services": ["cart"],
                "tests": ["mem.yml"],
            }
        }

    def test_analysis_json_only_network_outliers_uses_networks_key(self):
        profiling, _, recommendations = analysis_module.analysis_json(
            [], [], ["db"], [], [], CHAOS_TESTS
        )
        assert recommendations == {
            "outliers_network_recommendations": {
                "outliers_networks": ["db"],
                "tests": ["net.yml"],
            }
        }
        assert profiling["network_outliers"] == ["db"]

    def test_analysis_json_all_populated_insertion_order(self):
        _, _, recommendations = analysis_module.analysis_json(
            ["a"],
            ["b"],
            ["c"],
            ["web"],
            ["cart"],
            CHAOS_TESTS,
        )
        assert list(recommendations) == [
            "cpu_services_recommendations",
            "mem_services_recommendations",
            "outliers_network_recommendations",
        ]

    def test_analysis_json_cpu_memory_outliers_do_not_recommend(self):
        # CHARACTERIZATION: cpu/mem zscore outliers only surface in profiling,
        # never in recommendations (unlike network outliers).
        profiling, _, recommendations = analysis_module.analysis_json(
            ["web"], ["cart"], [], [], [], CHAOS_TESTS
        )
        assert profiling["cpu_outliers"] == ["web"]
        assert profiling["memory_outliers"] == ["cart"]
        assert recommendations == {}

    def test_analysis_json_missing_network_config_does_not_raise(self):
        profiling, _, recommendations = analysis_module.analysis_json(
            [], [], ["db"], [], [], {"CPU": ["cpu.yml"]}
        )
        assert profiling["network_outliers"] == ["db"]
        network = recommendations.get("outliers_network_recommendations")
        assert network is None or network["tests"] == []

    def test_analysis_json_missing_cpu_config_does_not_raise(self):
        _, _, recommendations = analysis_module.analysis_json(
            [], [], [], ["web"], [], {"MEM": ["mem.yml"]}
        )
        cpu = recommendations.get("cpu_services_recommendations")
        assert cpu is None or cpu["tests"] == []


class AnalysisTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = pathlib.Path(tmp.name)
        self.path = write_telemetry(self.tmp_path, TELEMETRY)
        # analysis() sleeps 2s per namespace: patch the module's time.
        patcher = patch.object(analysis_module, "time")
        self.time_mock = patcher.start()
        self.addCleanup(patcher.stop)

    def test_analysis_happy_path_two_namespaces(self):
        analysis_data = analysis_module.analysis(
            self.path,
            ["ns1", "ns2"],
            CHAOS_TESTS,
            1.0,
            0.6,
            0.95,
        )
        assert set(analysis_data) == {"ns1", "ns2"}
        # Each value is a [profiling, heatmap, recommendations] list.
        ns1_profiling, ns1_heatmap, ns1_recommendations = analysis_data["ns1"]
        assert ns1_profiling["cpu_outliers"] == ["cart"]
        assert ns1_profiling["memory_outliers"] == ["cart"]
        assert ns1_profiling["network_outliers"] == []
        assert ns1_heatmap == {
            "services_with_cpu_heatmap_above_threshold": ["cart"],
            "services_with_mem_heatmap_above_threshold": [],
        }
        assert ns1_recommendations == {
            "cpu_services_recommendations": {
                "services": ["cart"],
                "tests": ["cpu.yml"],
            }
        }
        ns2_profiling, _, ns2_recommendations = analysis_data["ns2"]
        assert ns2_profiling["network_outliers"] == ["db"]
        assert ns2_recommendations == {
            "outliers_network_recommendations": {
                "outliers_networks": ["db"],
                "tests": ["net.yml"],
            }
        }

    def test_analysis_sleeps_two_seconds_per_namespace(self):
        analysis_module.analysis(
            self.path, ["ns1", "ns2"], CHAOS_TESTS, 1.0, 0.6, 0.95
        )
        assert self.time_mock.sleep.call_args_list == [
            ((2,), {}),
            ((2,), {}),
        ]

    def test_analysis_absent_namespace_gets_empty_entry(self):
        analysis_data = analysis_module.analysis(
            self.path, ["ns1", "ghost"], CHAOS_TESTS, 1.0, 0.6, 0.95
        )
        profiling, heatmap, recommendations = analysis_data["ghost"]
        assert profiling == {
            "cpu_outliers": [],
            "memory_outliers": [],
            "network_outliers": [],
        }
        assert heatmap == {
            "services_with_cpu_heatmap_above_threshold": [],
            "services_with_mem_heatmap_above_threshold": [],
        }
        assert recommendations == {}

    def test_analysis_missing_file_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            analysis_module.analysis(
                self.tmp_path / "nope.txt",
                ["ns1"],
                CHAOS_TESTS,
                1.0,
                0.6,
                0.95,
            )

    def test_analysis_missing_cpu_config_key_does_not_raise(self):
        # read_configuration defaults chaos_tests to {} when unset.
        analysis_data = analysis_module.analysis(
            self.path, ["ns1"], {}, 1.0, 0.6, 0.95
        )
        assert set(analysis_data) == {"ns1"}
        _, heatmap, _ = analysis_data["ns1"]
        assert heatmap["services_with_cpu_heatmap_above_threshold"] == ["cart"]

    def test_analysis_logs_cpu_and_mem_branches(self):
        with self.assertLogs(level="INFO") as logs:
            # ns1: cart over cpu heatmap (0.8 > 0.6); no service over the
            # 0.95 mem threshold; ns2: neither over.
            analysis_module.analysis(
                self.path, ["ns1", "ns2"], CHAOS_TESTS, 1.0, 0.6, 0.95
            )
        messages = "\n".join(logs.output)
        assert "significant CPU compared to their assigned limits: ['cart']" in messages
        assert "no services that are using significant CPU" in messages
        assert "no services that are using significant MEMORY" in messages

    def test_analysis_logs_mem_services_branch(self):
        with self.assertLogs(level="INFO") as logs:
            # mem threshold 0.7: web (0.75) and cart (0.9) exceed it.
            analysis_module.analysis(
                self.path, ["ns1"], CHAOS_TESTS, 1.0, 0.6, 0.7
            )
        messages = "\n".join(logs.output)
        assert (
            "significant MEMORY compared to their assigned limits: "
            "['web', 'cart']" in messages
        )

    def test_analysis_empty_namespaces_returns_empty_dict(self):
        real_load = analysis_module.load_telemetry_data
        with patch.object(
            analysis_module, "load_telemetry_data", side_effect=real_load
        ) as load_mock:
            analysis_data = analysis_module.analysis(
                self.path, [], CHAOS_TESTS, 1.0, 0.6, 0.95
            )
        # Telemetry is loaded before the loop even with no namespaces.
        load_mock.assert_called_once_with(self.path)
        assert analysis_data == {}
        self.time_mock.sleep.assert_not_called()

    def test_analysis_telemetry_load_error_propagates(self):
        with patch.object(
            analysis_module,
            "load_telemetry_data",
            side_effect=OSError("disk error"),
        ):
            with self.assertRaisesRegex(OSError, "disk error"):
                analysis_module.analysis(
                    self.path, ["ns1"], CHAOS_TESTS, 1.0, 0.6, 0.95
                )

    def test_analysis_zscores_computed_across_namespaces(self):
        # CHARACTERIZATION: zscores are computed once over ALL namespaces,
        # not per namespace. "c" is the only service of ns2 (its own
        # per-namespace zscore would be NaN), yet its global CPU zscore
        # (1.1547 > 1.0) flags it as an ns2 CPU outlier.
        path = write_telemetry(
            self.tmp_path,
            "namespace\tservice\tCPU\tCPU_LIMITS\tMEM\tMEM_LIMITS\tNETWORK\n"
            "ns1\ta\t100\t1000\t100\t1000\t10\n"
            "ns1\tb\t110\t1000\t100\t1000\t10\n"
            "ns2\tc\t1000\t1000\t100\t1000\t10\n",
            "leakage.txt",
        )
        analysis_data = analysis_module.analysis(
            path, ["ns1", "ns2"], CHAOS_TESTS, 1.0, 10.0, 10.0
        )
        profiling, _, recommendations = analysis_data["ns2"]
        assert profiling["cpu_outliers"] == ["c"]
        assert recommendations == {}
