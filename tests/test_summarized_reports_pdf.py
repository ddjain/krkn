#!/usr/bin/env python3

"""
Unit tests for build_chaos_report_pdf and its nested _build_alert_table in
krkn.summarized_reports.transform.

The reportlab boundary (SimpleDocTemplate.build) is mocked for flowable
inspection; a subset of tests leave the real build in place and assert real
%PDF output on disk.
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from reportlab.platypus import Paragraph, Table

from krkn.summarized_reports.transform import (
    _FAIL_RED,
    _PASS_GREEN,
    _WARN_YELLOW,
    build_chaos_report_pdf,
)


def _minimal_chaos_output(**overrides):
    base = {
        "telemetry": {
            "run_uuid": "test-uuid-1234",
            "cluster_version": "4.22.0",
            "cloud_infrastructure": "AWS",
            "cloud_type": "self-managed",
            "total_node_count": 6,
            "network_plugins": ["OVNKubernetes"],
            "scenarios": [],
            "overall_resiliency_report": {
                "total_slos": 0,
                "passed_slos": 0,
                "resiliency_score": 100,
                "scenarios": {},
            },
        },
    }
    base.update(overrides)
    return base


def _make_scenario(scenario_name="test.yml", scenario_type="pod_disruption_scenarios",
                   exit_status=0, parameters=None, affected_pods=None,
                   affected_vmis=None, affected_nodes=None,
                   additional_telemetry=None, cluster_events=None,
                   start_timestamp=1000000, end_timestamp=1000060):
    s = {
        "scenario": scenario_name,
        "scenario_type": scenario_type,
        "exit_status": exit_status,
        "parameters": parameters or {},
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
    }
    if affected_pods is not None:
        s["affected_pods"] = affected_pods
    if affected_vmis is not None:
        s["affected_vmis"] = affected_vmis
    if affected_nodes is not None:
        s["affected_nodes"] = affected_nodes
    if additional_telemetry is not None:
        s["additional_telemetry"] = additional_telemetry
    if cluster_events is not None:
        s["cluster_events"] = cluster_events
    return s


def _flowable_text(flowables):
    """Recursively flatten flowables (Table cells -> Paragraphs) to one string.

    Paragraph.text keeps the raw intra-paragraph markup, so <a href=...> and
    <font color=...> fragments are assertable.
    """
    parts = []

    def _walk(item):
        if isinstance(item, Table):
            for row in item._cellvalues:
                for cell in row:
                    _walk(cell)
        elif isinstance(item, Paragraph):
            parts.append(item.text)
        elif isinstance(item, (list, tuple)):
            for sub in item:
                _walk(sub)

    for flowable in flowables:
        _walk(flowable)
    return "\n".join(parts)


def _build_mocked(chaos_output, path):
    """Call build_chaos_report_pdf with doc.build mocked; return (text, mock)."""
    with patch("reportlab.platypus.SimpleDocTemplate.build") as mock_build:
        result = build_chaos_report_pdf(chaos_output, str(path))
    text = _flowable_text(mock_build.call_args.args[0])
    return result, text, mock_build


def _tables(mock_build):
    """Top-level Tables passed to the mocked doc.build."""
    return [f for f in mock_build.call_args.args[0] if isinstance(f, Table)]


def _table_with(mock_build, header_text, header_row):
    """First top-level Table whose given row contains header_text."""
    for table in _tables(mock_build):
        rows = table._cellvalues
        if len(rows) > header_row and any(
                header_text in getattr(cell, "text", "") for cell in rows[header_row]
        ):
            return table
    raise AssertionError(f"no table with {header_text!r} in row {header_row}")


def _tables_with(mock_build, header_text, header_row):
    """All top-level Tables whose given row contains header_text."""
    return [
        t for t in _tables(mock_build)
        if len(t._cellvalues) > header_row and any(
            header_text in getattr(cell, "text", "") for cell in t._cellvalues[header_row]
        )
    ]


class _PdfTestCase(unittest.TestCase):
    """Provides a per-test temporary directory as self.tmp_path."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)


class TestPdfRunMetadata(_PdfTestCase):

    def test_build_chaos_report_pdf_empty_output_writes_real_pdf(self):
        pdf_path = self.tmp_path / "out.pdf"

        result = build_chaos_report_pdf({}, str(pdf_path))

        assert result == str(pdf_path)
        assert pdf_path.exists()
        assert pdf_path.read_bytes()[:4] == b"%PDF"

    def test_build_chaos_report_pdf_empty_output_all_metadata_na(self):
        result, text, _ = _build_mocked({}, self.tmp_path / "out.pdf")

        assert result
        # Run UUID/Cluster Version/Infrastructure/Cloud Type/Time Window/Total Nodes
        assert text.count("N/A") >= 6
        assert "Run Metadata" in text

    def test_build_chaos_report_pdf_network_plugins_row(self):
        output = _minimal_chaos_output()
        output["telemetry"]["network_plugins"] = ["OVNKubernetes", "Kuryr"]

        _, text, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        assert "Network Plugins" in text
        assert "OVNKubernetes, Kuryr" in text

    def test_build_chaos_report_pdf_all_security_flags(self):
        output = _minimal_chaos_output()
        output["telemetry"]["fips_enabled"] = True
        output["telemetry"]["etcd_encryption_enabled"] = True
        output["telemetry"]["ipsec_enabled"] = True

        _, text, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        assert "Security" in text
        assert "FIPS, etcd encryption, IPSec" in text

    def test_build_chaos_report_pdf_no_security_flags_no_row(self):
        output = _minimal_chaos_output()

        _, text, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        assert "Security" not in text

    def test_build_chaos_report_pdf_node_summary_with_none_fields(self):
        output = _minimal_chaos_output()
        output["telemetry"]["node_summary_infos"] = [
            {"nodes_type": None, "count": None, "instance_type": None,
             "architecture": None, "kubelet_version": None, "os_version": None},
        ]

        _, text, mock_build = _build_mocked(output, self.tmp_path / "out.pdf")

        assert "Cluster Overview" in text
        # every empty cell should fall back to the intended "N/A" default
        row = [c.text for c in _table_with(mock_build, "Architecture", 0)._cellvalues[1]]
        assert "None" not in row
        assert row == ["N/A"] * 6

    def test_build_chaos_report_pdf_unicode_metadata_real_build(self):
        output = _minimal_chaos_output()
        output["telemetry"]["run_uuid"] = "étcd–✓ 中文"
        output["telemetry"]["cluster_version"] = "µs ≥ 100 ✓"
        pdf_path = self.tmp_path / "out.pdf"

        build_chaos_report_pdf(output, str(pdf_path))

        assert pdf_path.read_bytes()[:4] == b"%PDF"
        assert pdf_path.stat().st_size > 1000
        assert pdf_path.read_bytes().endswith(b"%%EOF\n")


class TestPdfTargets(_PdfTestCase):

    def test_scenario_type_with_docs_url_renders_link(self):
        output = _minimal_chaos_output()
        output["telemetry"]["scenarios"] = [
            _make_scenario(scenario_type="hog_scenarios"),
        ]

        _, text, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        assert '<a href="https://krkn-chaos.dev/docs/scenarios/hog-scenarios/"' in text
        assert "<u>hog_scenarios</u>" in text

    def test_unknown_scenario_type_renders_plain_text(self):
        output = _minimal_chaos_output()
        output["telemetry"]["scenarios"] = [
            _make_scenario(scenario_type="weird_type"),
        ]

        _, text, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        assert "weird_type" in text
        assert "<a href" not in text

    def test_scenario_type_absent_renders_na_without_link(self):
        output = _minimal_chaos_output()
        scenario = _make_scenario()
        del scenario["scenario_type"]
        output["telemetry"]["scenarios"] = [scenario]

        _, text, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        assert "N/A" in text
        assert "<a href" not in text

    def test_vmi_pvc_target_full_rows(self):
        output = _minimal_chaos_output()
        output["telemetry"]["scenarios"] = [_make_scenario(
            scenario_name="vmi_pvc_fill.yaml",
            parameters={"vmi_pvc_fill_scenario": {
                "namespace": "default", "vmi_name": "vmi-1", "pvc_name": "pvc-1",
                "fill_percentage": 80, "duration": 60,
            }},
        )]

        _, text, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        for expected in ("VMI Name", "vmi-1", "PVC Name", "pvc-1",
                         "Fill Percentage", "80%", "Duration", "60s"):
            assert expected in text

    # Characterization: out-of-range fill_percentage values (150, -5) are passed
    # through verbatim into the PDF; pinned as-is, not endorsed as correct.
    def _assert_fill_percentage_rendered_verbatim(self, fill):
        output = _minimal_chaos_output()
        output["telemetry"]["scenarios"] = [_make_scenario(
            parameters={"vmi_pvc_fill_scenario": {
                "namespace": "ns", "vmi_name": "vmi-1", "pvc_name": "pvc-1",
                "fill_percentage": fill, "duration": 60,
            }},
        )]

        _, text, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        assert "Fill Percentage" in text
        assert f"{fill}%" in text

    def test_fill_percentage_out_of_range_rendered_verbatim_150(self):
        self._assert_fill_percentage_rendered_verbatim(150)

    def test_fill_percentage_out_of_range_rendered_verbatim_neg5(self):
        self._assert_fill_percentage_rendered_verbatim(-5)

    def test_pods_error_only_renders_pod_monitoring_row(self):
        output = _minimal_chaos_output()
        output["telemetry"]["scenarios"] = [_make_scenario(
            affected_pods={"recovered": [], "unrecovered": [], "error": "timeout reached"},
        )]

        _, text, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        assert "Pod Monitoring" in text
        assert f'<font color="{_FAIL_RED}">Error: timeout reached</font>' in text
        assert "Disrupted Pods" not in text

    def test_vmis_error_only_renders_vmi_monitoring_row(self):
        output = _minimal_chaos_output()
        output["telemetry"]["scenarios"] = [_make_scenario(
            affected_vmis={"recovered": [], "unrecovered": [], "error": "ssh failed"},
        )]

        _, text, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        assert "VMI Monitoring" in text
        assert f'<font color="{_FAIL_RED}">Error: ssh failed</font>' in text
        assert "Disrupted VMIs" not in text

    def test_pods_with_monitoring_error_renders_both(self):
        output = _minimal_chaos_output()
        output["telemetry"]["scenarios"] = [_make_scenario(
            affected_pods={
                "recovered": [{"namespace": "ns", "pod_name": "p1"}],
                "unrecovered": [{"namespace": "ns", "pod_name": "p2"}],
                "error": "partial scrape failure",
            },
        )]

        _, text, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        assert "Disrupted Pods" in text
        assert "ns/p1" in text
        assert "ns/p2" in text
        assert "Monitoring error: partial scrape failure" in text

    def test_nodes_mixed_node_id_uses_id_column(self):
        output = _minimal_chaos_output()
        output["telemetry"]["scenarios"] = [_make_scenario(
            affected_nodes=[
                {"node_name": "worker-1", "node_id": "i-123",
                 "stopped_time": 100.0, "running_time": 15.0},
                {"node_name": "worker-2", "node_id": ""},
            ],
        )]

        _, text, mock_build = _build_mocked(output, self.tmp_path / "out.pdf")

        assert "worker-1 (i-123)" in text
        assert "worker-2\n" in text  # target row: no "(...)" suffix
        # has_id branch of Node Recovery: 7-column table with Instance column
        node_table = _table_with(mock_build, "Instance", 1)
        body = node_table._cellvalues[2:]  # skip span header + column header rows
        assert [c.text for c in body[0][:2]] == ["worker-1", "i-123"]
        assert [c.text for c in body[1][:2]] == ["worker-2", ""]  # bare node: empty id

    def test_scenario_level_namespace_row(self):
        output = _minimal_chaos_output()
        output["telemetry"]["scenarios"] = [_make_scenario(
            parameters=[{"namespace": "top-level-ns"}],
        )]

        _, text, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        assert "Namespace" in text
        assert "top-level-ns" in text

    def test_exclude_labels_and_cloud_types_rows(self):
        output = _minimal_chaos_output()
        output["telemetry"]["scenarios"] = [_make_scenario(
            parameters=[{"id": "kill", "config": {
                "label_selector": "app=etcd",
                "exclude_label": "component=backup",
                "cloud_type": "aws",
            }}],
        )]

        _, text, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        assert "Label Selector" in text
        assert "app=etcd" in text
        assert "Exclude Label" in text
        assert "component=backup" in text
        assert "Cloud Type" in text
        assert "aws" in text


class TestPdfKeyMetrics(_PdfTestCase):

    def _key_metrics_table(self, mock_build):
        return _table_with(mock_build, "Pods Recovered", 0)

    def test_recovered_pod_without_recovery_time_empty_cell(self):
        output = _minimal_chaos_output()
        output["telemetry"]["scenarios"] = [_make_scenario(
            affected_pods={"recovered": [{}], "unrecovered": []},
        )]

        _, text, mock_build = _build_mocked(output, self.tmp_path / "out.pdf")

        assert "Key Metrics" in text
        table = self._key_metrics_table(mock_build)
        row = table._cellvalues[1]  # header row + one data row
        assert [c.text for c in row] == ["test.yml", "1", "0", ""]

    def test_recovery_time_zero_is_not_dropped(self):
        output = _minimal_chaos_output()
        output["telemetry"]["scenarios"] = [_make_scenario(
            affected_pods={"recovered": [{"total_recovery_time": 0.0}],
                           "unrecovered": []},
        )]

        _, text, mock_build = _build_mocked(output, self.tmp_path / "out.pdf")

        assert "0.00s" in text
        row = self._key_metrics_table(mock_build)._cellvalues[1]
        assert row[3].text.startswith("0.00s")

    def test_none_rescheduling_readiness_coalesced_to_zero(self):
        output = _minimal_chaos_output()
        output["telemetry"]["scenarios"] = [_make_scenario(
            affected_pods={"recovered": [{"total_recovery_time": 5.0,
                                          "pod_rescheduling_time": None,
                                          "pod_readiness_time": None}],
                           "unrecovered": []},
        )]

        _, text, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        assert "Rescheduling: 0.00s" in text
        assert "Readiness: 0.00s" in text

    def test_vmi_guest_reachable_omitted_when_none(self):
        output = _minimal_chaos_output()
        output["telemetry"]["scenarios"] = [_make_scenario(
            affected_vmis={"recovered": [{"namespace": "kv", "vmi_name": "vm1",
                                          "total_recovery_time": 8.0,
                                          "vmi_rescheduling_time": 1.0,
                                          "vmi_readiness_time": 7.0}],
                           "unrecovered": []},
        )]

        _, text, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        assert "VMI Recovery" in text
        assert "Rescheduling: 1.00s" in text
        assert "Guest Reachable" not in text

    def test_vmi_guest_reachable_shown_when_set(self):
        output = _minimal_chaos_output()
        output["telemetry"]["scenarios"] = [_make_scenario(
            affected_vmis={"recovered": [{"namespace": "kv", "vmi_name": "vm1",
                                          "total_recovery_time": 8.0,
                                          "guest_reachable_time": 4.25}],
                           "unrecovered": []},
        )]

        _, text, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        assert "Guest Reachable: 4.25s" in text

    def test_node_timing_none_fields_render_empty_cells(self):
        output = _minimal_chaos_output()
        output["telemetry"]["scenarios"] = [_make_scenario(
            affected_nodes=[{"node_name": "worker-1", "node_id": "i-1",
                             "stopped_time": 100.0, "running_time": None,
                             "terminating_time": None, "not_ready_time": None,
                             "ready_time": None}],
        )]

        _, text, mock_build = _build_mocked(output, self.tmp_path / "out.pdf")

        node_table = _table_with(mock_build, "Stopped", 1)
        row = node_table._cellvalues[2]
        assert [c.text for c in row] == ["worker-1", "i-1", "100.00s", "", "", "", ""]

    def test_node_all_times_none_all_timing_cells_empty(self):
        output = _minimal_chaos_output()
        output["telemetry"]["scenarios"] = [_make_scenario(
            affected_nodes=[{"node_name": "worker-1"}],
        )]

        _, text, mock_build = _build_mocked(output, self.tmp_path / "out.pdf")

        # timing cells must be empty, never the string "None"
        node_table = _table_with(mock_build, "Stopped", 1)
        assert [c.text for c in node_table._cellvalues[2]] == ["worker-1", "", "", "", "", ""]

    def test_additional_telemetry_list_skipped_silently(self):
        output = _minimal_chaos_output()
        output["telemetry"]["scenarios"] = [_make_scenario(
            additional_telemetry=["x"],
        )]

        result, text, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        assert result
        assert "Load Test Metrics" not in text

    def test_additional_telemetry_unicode_values_escaped_into_table(self):
        output = _minimal_chaos_output()
        output["telemetry"]["scenarios"] = [_make_scenario(
            additional_telemetry={"latency": "µs≥100", "rps&rate": 150.5},
        )]

        _, text, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        assert "Load Test Metrics" in text
        assert "µs≥100" in text
        assert "rps&amp;rate" in text  # XML-escaped key


class TestPdfClusterEvents(_PdfTestCase):

    def test_events_truncated_beyond_10_with_more_line(self):
        events = [{"type": "Normal", "reason": f"Reason{i}", "message": f"msg{i}"}
                  for i in range(15)]
        output = _minimal_chaos_output()
        output["telemetry"]["scenarios"] = [_make_scenario(cluster_events=events)]

        _, text, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        assert "Cluster Events" in text
        assert "(15 events)" in text
        assert "Reason9" in text
        assert "Reason10" not in text
        assert "... and 5 more" in text

    def test_string_event_rendered_with_event_reason(self):
        output = _minimal_chaos_output()
        output["telemetry"]["scenarios"] = [
            _make_scenario(cluster_events=["raw event string"]),
        ]

        _, text, mock_build = _build_mocked(output, self.tmp_path / "out.pdf")

        events_table = _table_with(mock_build, "Reason", 1)
        row = [c.text for c in events_table._cellvalues[2]]
        assert row == ["", "Event", "", "raw event string", ""]

    def test_warning_event_rendered_red_bold_and_normal_plain(self):
        output = _minimal_chaos_output()
        output["telemetry"]["scenarios"] = [_make_scenario(cluster_events=[
            {"type": "Warning", "reason": "BackOff", "message": "m1"},
            {"type": "Normal", "reason": "Pulled", "message": "m2"},
        ])]

        _, text, mock_build = _build_mocked(output, self.tmp_path / "out.pdf")

        events_table = _table_with(mock_build, "Reason", 1)
        warning_cell = events_table._cellvalues[2][0]
        normal_cell = events_table._cellvalues[3][0]
        assert warning_cell.text == (
            f'<font color="{_WARN_YELLOW}"><b>Warning</b></font>'
        )
        assert normal_cell.text == "Normal"

    def test_event_without_involved_object_kind_empty_object_ref(self):
        output = _minimal_chaos_output()
        output["telemetry"]["scenarios"] = [_make_scenario(cluster_events=[
            {"reason": "R", "message": "m", "involved_object_name": "obj"},
        ])]

        _, text, mock_build = _build_mocked(output, self.tmp_path / "out.pdf")

        events_table = _table_with(mock_build, "Reason", 1)
        assert events_table._cellvalues[2][2].text == ""  # no "None/obj" leakage


class TestPdfHealthChecks(_PdfTestCase):

    def _health_table(self, mock_build):
        return _table_with(mock_build, "URL / Endpoint", 0)

    def test_health_check_phase_defaults_to_during(self):
        output = _minimal_chaos_output()
        output["telemetry"]["health_checks"] = [
            {"url": "https://api.cluster.local:6443", "status_code": 200,
             "status": True, "duration": 0.12},
        ]

        _, text, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        assert "Health Checks (HTTP) - During-Chaos" in text
        assert "api.cluster.local" in text
        assert "0.12s" in text

    def test_health_check_duration_none_renders_empty(self):
        output = _minimal_chaos_output()
        output["telemetry"]["health_checks"] = [
            {"url": "http://x", "status_code": 500, "status": False,
             "duration": None},
        ]

        _, text, mock_build = _build_mocked(output, self.tmp_path / "out.pdf")

        row = [c.text for c in self._health_table(mock_build)._cellvalues[1]]
        assert row == ["http://x", "500", "", f'<font color="{_FAIL_RED}"><b>FAIL</b></font>']

    def test_health_check_non_numeric_duration_does_not_crash(self):
        output = _minimal_chaos_output()
        output["telemetry"]["health_checks"] = [
            {"url": "http://x", "status_code": 200, "status": True,
             "duration": "abc"},
        ]

        result, _, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        assert result

    # suspected bug: passed = status or passed treats status=False + passed=True
    # as PASS; or-logic is not precedence-aware. Intended semantics are
    # UNCONFIRMED: the test assumes status=False should win over passed=True.
    # (was raises=AssertionError)
    @unittest.expectedFailure
    def test_health_check_status_false_passed_true_renders_fail(self):
        output = _minimal_chaos_output()
        output["telemetry"]["health_checks"] = [
            {"url": "http://x", "status_code": 200, "status": False,
             "passed": True, "duration": 0.5},
        ]

        _, text, mock_build = _build_mocked(output, self.tmp_path / "out.pdf")

        row = [c.text for c in self._health_table(mock_build)._cellvalues[1]]
        assert row[3] == f'<font color="{_FAIL_RED}"><b>FAIL</b></font>'

    def test_string_health_object_and_virt_checks_skipped(self):
        output = _minimal_chaos_output()
        output["telemetry"]["health_checks"] = ["raw check"]
        output["telemetry"]["object_state_checks"] = ["raw obj check"]
        output["telemetry"]["virt_checks"] = ["raw virt check"]

        result, text, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        assert result
        assert "Health Checks (HTTP)" not in text
        assert "Object State Checks" not in text
        assert "KubeVirt Health Checks" not in text


class TestPdfObjectStateChecks(_PdfTestCase):

    def _object_table(self, mock_build):
        return _table_with(mock_build, "Check Name", 0)

    def test_failed_check_with_message_populates_failed_objects(self):
        output = _minimal_chaos_output()
        output["telemetry"]["object_state_checks"] = [{
            "check_name": "deployment-check", "kind": "Deployment",
            "namespace": "ns1", "condition_type": "Available",
            "condition_status": "True", "objects_checked": 3,
            "objects_failed": 1, "duration": 1.5, "passed": False,
            "message": "deployment-2 does not have condition",
        }]

        _, text, mock_build = _build_mocked(output, self.tmp_path / "out.pdf")

        assert "Object State Checks (During-Chaos)" in text
        row = [c.text for c in self._object_table(mock_build)._cellvalues[1]]
        assert row == [
            "deployment-check", "Deployment", "ns1", "Available=True",
            "3 checked, 1 failed", "1.50s",
            f'<font color="{_FAIL_RED}"><b>FAIL</b></font>',
            "deployment-2 does not have condition",
        ]

    def test_all_pass_message_suppressed_from_failed_objects(self):
        output = _minimal_chaos_output()
        output["telemetry"]["object_state_checks"] = [{
            "check_name": "pod-check", "kind": "Pod", "namespace": "ns1",
            "condition_type": "Ready", "condition_status": "True",
            "objects_checked": 3, "objects_failed": 0, "passed": True,
            "message": "All objects passed",
        }]

        _, text, mock_build = _build_mocked(output, self.tmp_path / "out.pdf")

        row = [c.text for c in self._object_table(mock_build)._cellvalues[1]]
        assert row[4] == "3 checked"
        assert row[6] == f'<font color="{_PASS_GREEN}"><b>PASS</b></font>'
        assert row[7] == ""
        assert "All objects passed" not in text

    def test_objects_failed_none_treated_as_zero(self):
        output = _minimal_chaos_output()
        output["telemetry"]["object_state_checks"] = [{
            "check_name": "pod-check", "kind": "Pod", "namespace": "ns1",
            "objects_checked": 2, "objects_failed": None, "passed": True,
        }]

        result, _, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        assert result


class TestPdfVirtChecks(_PdfTestCase):

    def _virt_table(self, mock_build):
        return _table_with(mock_build, "VM Name", 0)

    def test_new_ip_equal_to_old_renders_empty_cell_with_column(self):
        output = _minimal_chaos_output()
        output["telemetry"]["virt_checks"] = [{
            "vm_name": "vm-1", "namespace": "kubevirt", "node_name": "worker-1",
            "ip_address": "10.0.0.5", "new_ip_address": "10.0.0.5",
            "status": True, "duration": 1.5,
        }]

        _, text, mock_build = _build_mocked(output, self.tmp_path / "out.pdf")

        table = self._virt_table(mock_build)
        headers = [c.text for c in table._cellvalues[0]]
        assert "New IP" in headers
        row = [c.text for c in table._cellvalues[1]]
        assert row == ["vm-1", "kubevirt", "worker-1", "10.0.0.5", "",
                       "1.50s", f'<font color="{_PASS_GREEN}"><b>PASS</b></font>']

    def test_status_none_means_pass(self):
        output = _minimal_chaos_output()
        output["telemetry"]["virt_checks"] = [{
            "vm_name": "vm-1", "namespace": "kubevirt", "node_name": "worker-1",
            "ip_address": "10.0.0.5", "status": None, "duration": 2.0,
        }]

        _, text, mock_build = _build_mocked(output, self.tmp_path / "out.pdf")

        row = [c.text for c in self._virt_table(mock_build)._cellvalues[1]]
        assert "New IP" not in [c.text for c in self._virt_table(mock_build)._cellvalues[0]]
        assert row[-1] == f'<font color="{_PASS_GREEN}"><b>PASS</b></font>'

    def test_status_false_renders_fail(self):
        output = _minimal_chaos_output()
        output["telemetry"]["virt_checks"] = [{
            "vm_name": "vm-1", "namespace": "kubevirt", "node_name": "worker-1",
            "ip_address": "10.0.0.5", "status": False, "duration": 2.0,
        }]

        _, text, mock_build = _build_mocked(output, self.tmp_path / "out.pdf")

        row = [c.text for c in self._virt_table(mock_build)._cellvalues[1]]
        assert row[-1] == f'<font color="{_FAIL_RED}"><b>FAIL</b></font>'

    def test_post_phase_grouped_under_post_chaos_header(self):
        output = _minimal_chaos_output()
        output["telemetry"]["virt_checks"] = [{
            "vmi_name": "vmi-1", "namespace": "kubevirt", "node_name": "worker-2",
            "ip_address": "10.0.0.6", "new_ip_address": "10.0.0.7",
            "status": True, "duration": 2.5, "phase": "post",
        }]

        _, text, mock_build = _build_mocked(output, self.tmp_path / "out.pdf")

        assert "KubeVirt Health Checks - Post-Chaos" in text
        row = [c.text for c in self._virt_table(mock_build)._cellvalues[1]]
        assert row[4] == "10.0.0.7"


class TestPdfAlerts(_PdfTestCase):

    def test_legacy_mode_renders_two_tables_with_legacy_headers(self):
        output = _minimal_chaos_output()
        output["critical_alerts"] = {
            "chaos_alerts": [{"alertname": "A1", "severity": "critical",
                              "namespace": "ns1", "alertstate": "firing"}],
            "post_chaos_alerts": [{"alertname": "A2", "severity": "warning",
                                   "namespace": "ns2", "alertstate": "pending"}],
        }

        _, text, mock_build = _build_mocked(output, self.tmp_path / "out.pdf")

        assert "Critical Alerts (During Chaos)" in text
        assert "Critical Alerts (Post Chaos)" in text
        alert_tables = _tables_with(mock_build, "Alert Name", 0)
        assert len(alert_tables) == 2
        for table, expected_row in zip(alert_tables, (
                ["A1", "critical", "ns1", "firing"],
                ["A2", "warning", "ns2", "pending"])):
            headers = [c.text for c in table._cellvalues[0]]
            assert headers == ["Alert Name", "Severity", "Namespace", "State"]
            assert [c.text for c in table._cellvalues[1]] == expected_row

    def test_legacy_non_dict_alert_row(self):
        output = _minimal_chaos_output()
        output["critical_alerts"] = {"chaos_alerts": ["raw"], "post_chaos_alerts": []}

        _, text, mock_build = _build_mocked(output, self.tmp_path / "out.pdf")

        table = _table_with(mock_build, "Alert Name", 0)
        assert [c.text for c in table._cellvalues[1]] == ["raw", "", "", ""]

    def test_legacy_summary_line_counts_all_alerts(self):
        output = _minimal_chaos_output()
        output["critical_alerts"] = {
            "chaos_alerts": [{"alertname": f"A{i}"} for i in range(2)],
            "post_chaos_alerts": [{"alertname": "A2"}],
        }

        _, text, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        assert "Critical Alerts" in text
        assert f'<font color="{_FAIL_RED}"><b>3</b></font>' in text

    def test_non_legacy_no_failures_renders_none_and_prometheus_table(self):
        output = _minimal_chaos_output()
        output["telemetry"]["alerts"] = [{"name": "SLO1", "status": True,
                                          "severity": "info", "phase": "during"}]
        output["critical_alerts"] = {
            "chaos_alerts": [{"alertname": "A1", "severity": "critical",
                              "alertstate": "firing"}],
        }

        _, text, mock_build = _build_mocked(output, self.tmp_path / "out.pdf")

        assert "SLO Validation Failures" in text
        assert "\nNone\n" in text
        assert "Critical Alerts" not in text  # no legacy summary kv row
        prometheus_tables = _tables_with(mock_build, "Status", 0)
        assert len(prometheus_tables) == 1
        headers = [c.text for c in prometheus_tables[0]._cellvalues[0]]
        assert headers == ["Alert Name", "Severity", "Status", "Phase"]
        row = [c.text for c in prometheus_tables[0]._cellvalues[1]]
        assert row == ["A1", "critical", "firing", "during"]

    def test_non_legacy_status_false_is_failure_row(self):
        output = _minimal_chaos_output()
        output["telemetry"]["alerts"] = [{"name": "A", "status": False,
                                          "severity": "critical", "phase": "during"}]

        _, text, mock_build = _build_mocked(output, self.tmp_path / "out.pdf")

        tables = _tables_with(mock_build, "Alert Name", 0)
        assert len(tables) == 1  # only the SLO Validation Failures table
        row = [c.text for c in tables[0]._cellvalues[1]]
        assert row == ["A", "critical", "FAIL", "during"]

    def test_non_legacy_failure_deduped_in_prometheus_table(self):
        output = _minimal_chaos_output()
        output["telemetry"]["alerts"] = [{"name": "A", "status": False}]
        output["critical_alerts"] = {
            "chaos_alerts": [
                {"alertname": "A", "severity": "critical", "alertstate": "firing"},
                {"alertname": "A", "severity": "critical", "alertstate": "firing"},
            ],
            "post_chaos_alerts": [],
        }

        _, text, mock_build = _build_mocked(output, self.tmp_path / "out.pdf")

        # same (name, phase) collapses to a single row
        prometheus = _table_with(mock_build, "Status", 0)
        assert len(prometheus._cellvalues) == 2  # header + 1 row
        # failure table + deduped prometheus table both render
        assert len(_tables_with(mock_build, "Alert Name", 0)) == 2

    def test_non_legacy_truthy_string_status_not_a_failure(self):
        output = _minimal_chaos_output()
        output["telemetry"]["alerts"] = [{"name": "B", "status": "firing",
                                          "alertstate": "firing", "phase": "during"}]

        _, text, mock_build = _build_mocked(output, self.tmp_path / "out.pdf")

        assert "SLO Validation Failures" in text
        assert "\nNone\n" in text  # no failures listed
        assert _tables_with(mock_build, "Alert Name", 0) == []

    def test_non_dict_telemetry_alert_with_false_status_is_failure(self):
        output = _minimal_chaos_output()
        output["telemetry"]["alerts"] = [
            SimpleNamespace(name="obj-alert", severity="critical",
                            status=False, phase="during"),
        ]

        _, text, mock_build = _build_mocked(output, self.tmp_path / "out.pdf")

        table = _table_with(mock_build, "Alert Name", 0)
        row = [c.text for c in table._cellvalues[1]]
        assert row == ["obj-alert", "critical", "FAIL", "during"]

    def test_same_alertname_across_phases_not_deduped(self):
        output = _minimal_chaos_output()
        output["telemetry"]["alerts"] = [{"name": "S", "status": True}]
        output["critical_alerts"] = {
            "chaos_alerts": [{"alertname": "X", "severity": "critical"}],
            "post_chaos_alerts": [{"alertname": "X", "severity": "critical"}],
        }

        _, text, mock_build = _build_mocked(output, self.tmp_path / "out.pdf")

        prometheus = _table_with(mock_build, "Status", 0)
        rows = [[c.text for c in r] for r in prometheus._cellvalues[1:]]
        assert rows == [["X", "critical", "N/A", "during"],
                        ["X", "critical", "N/A", "post"]]


class TestPdfErrorLogs(_PdfTestCase):

    def _logs_table(self, mock_build):
        return _table_with(mock_build, "Timestamp", 0)

    def test_logs_truncated_beyond_20_with_more_line(self):
        output = _minimal_chaos_output()
        output["telemetry"]["error_logs"] = [
            {"timestamp": f"t{i}", "message": f"log{i}"} for i in range(25)
        ]

        _, text, mock_build = _build_mocked(output, self.tmp_path / "out.pdf")

        assert "Error Logs (25)" in text
        assert "log19" in text
        assert "log20" not in text
        assert "... and 5 more" in text
        assert len(self._logs_table(mock_build)._cellvalues) == 21  # header + 20

    def test_long_message_truncated_to_300_chars(self):
        output = _minimal_chaos_output()
        output["telemetry"]["error_logs"] = [
            {"timestamp": "t", "message": "x" * 400},
        ]

        _, text, mock_build = _build_mocked(output, self.tmp_path / "out.pdf")

        assert "x" * 300 in text
        assert "x" * 301 not in text

    # Characterization: a None message falls back to str(entry); the dict-repr
    # output is pinned as-is, not endorsed as the intended rendering.
    def test_none_message_falls_back_to_entry_str(self):
        output = _minimal_chaos_output()
        output["telemetry"]["error_logs"] = [
            {"timestamp": "t0", "message": None},
        ]

        _, text, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        assert "'timestamp'" in text  # str(dict) fallback
        assert "'message'" in text

    def test_string_log_entry_rendered(self):
        output = _minimal_chaos_output()
        output["telemetry"]["error_logs"] = ["raw error string"]

        _, text, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        assert "raw error string" in text


class TestPdfResiliencyScore(_PdfTestCase):

    def test_failed_slo_details_render_failure_section(self):
        output = _minimal_chaos_output()
        output["scenario_slo_details"] = [{
            "scenario": "test.yml",
            "slo_details": [{"name": "slo1", "severity": "warning", "passed": False}],
        }]

        _, text, mock_build = _build_mocked(output, self.tmp_path / "out.pdf")

        assert "Resiliency Score SLO Failures" in text
        assert "Resiliency Alert File: N/A" in text
        slo_table = _table_with(mock_build, "Severity", 0)
        row = [c.text for c in slo_table._cellvalues[1]]
        assert row == ["slo1", "warning", f'<font color="{_FAIL_RED}"><b>FAIL</b></font>']

    def test_resiliency_alert_file_value_rendered(self):
        output = _minimal_chaos_output()
        output["resiliency_alert_file"] = "/tmp/alerts.txt"
        output["scenario_slo_details"] = [{
            "scenario": "test.yml",
            "slo_details": [{"name": "slo1", "severity": "warning", "passed": False}],
        }]

        _, text, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        assert "Resiliency Alert File: /tmp/alerts.txt" in text

    def test_slo_detail_missing_passed_key_treated_as_not_passed(self):
        output = _minimal_chaos_output()
        output["scenario_slo_details"] = [{
            "scenario": "test.yml",
            "slo_details": [{"name": "slo1", "severity": "warning"}],
        }]

        result, text, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        assert result
        assert "Resiliency Score SLO Failures" in text

    def test_overall_score_na_renders_default_color(self):
        output = _minimal_chaos_output()
        del output["telemetry"]["overall_resiliency_report"]["resiliency_score"]

        _, text, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        assert "Overall: N/A / 100" in text

    def test_score_colors_at_90_and_89_boundary(self):
        output = _minimal_chaos_output()
        output["telemetry"]["overall_resiliency_report"]["scenarios"] = {
            "green-scenario": 90, "yellow-scenario": 89,
        }

        _, text, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        assert f'<font color="{_PASS_GREEN}"><b>90 / 100</b></font>' in text
        assert f'<font color="{_WARN_YELLOW}"><b>89 / 100</b></font>' in text


class TestPdfResiliencyWeights(_PdfTestCase):

    def test_weight_map_without_scores_skips_calc_detail(self):
        output = _minimal_chaos_output()
        output["resiliency_report"] = {"scenarios": [{"name": "a", "weight": 2}]}

        _, text, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        assert "Resiliency Score Calculation" not in text
        assert "Weight" not in text

    def test_weighted_calc_detail_rendered_when_scores_present(self):
        output = _minimal_chaos_output()
        output["telemetry"]["overall_resiliency_report"]["scenarios"] = {"a": 80}
        output["resiliency_report"] = {"scenarios": [{"name": "a", "weight": 2}]}

        _, text, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        assert "Weight: 2x" in text
        assert "Total weight: 2x" in text
        assert "Resiliency Score Calculation" in text

    def test_null_weight_defaults_to_one(self):
        output = _minimal_chaos_output()
        output["telemetry"]["overall_resiliency_report"]["scenarios"] = {"a": 80}
        output["resiliency_report"] = {"scenarios": [{"name": "a", "weight": None}]}

        result, text, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        assert result
        assert "Weight: Nonex" not in text
        assert "Weight: 1x" in text


class TestPdfRealBuild(_PdfTestCase):
    """Tests exercising the real reportlab build (doc.build NOT mocked)."""

    @staticmethod
    def _page_count(pdf_path):
        data = pdf_path.read_bytes()
        return data.count(b"/Type /Page") - data.count(b"/Type /Pages")

    def test_large_report_builds_multi_page_pdf(self):
        output = _minimal_chaos_output()
        output["telemetry"]["scenarios"] = [
            _make_scenario(
                scenario_name=f"scenario-{i}.yml",
                affected_nodes=[{"node_name": f"worker-{i}", "node_id": f"i-{i}",
                                 "stopped_time": 100.0 + i, "running_time": 15.0}],
            )
            for i in range(40)
        ]
        output["telemetry"]["error_logs"] = [
            {"timestamp": f"2026-11-20 10:00:{i:02d}",
             "message": ("wide message token " * 40) + f"#{i}"}
            for i in range(10)
        ]
        pdf_path = self.tmp_path / "multi.pdf"

        result = build_chaos_report_pdf(output, str(pdf_path))

        assert result == str(pdf_path)
        assert pdf_path.read_bytes()[:4] == b"%PDF"
        assert self._page_count(pdf_path) > 1  # no LayoutError, multiple pages

    def test_unbroken_300_char_token_wraps_without_layout_error(self):
        # Characterization: reportlab's splitLongWords wraps the oversized token
        # inside the fixed-width table cell instead of raising a LayoutError.
        output = _minimal_chaos_output()
        output["telemetry"]["error_logs"] = [
            {"timestamp": "t", "message": "a" * 300},
        ]
        pdf_path = self.tmp_path / "token.pdf"

        build_chaos_report_pdf(output, str(pdf_path))

        assert pdf_path.read_bytes()[:4] == b"%PDF"
        assert pdf_path.stat().st_size > 1000
        assert pdf_path.read_bytes().endswith(b"%%EOF\n")

    def test_unicode_everywhere_renders_real_pdf(self):
        output = _minimal_chaos_output()
        output["telemetry"]["scenarios"] = [_make_scenario(
            affected_pods={"recovered": [{"namespace": "中文", "pod_name": "pod-😀"}],
                           "unrecovered": []},
            cluster_events=[{"type": "Normal", "reason": "原因",
                             "message": "消息 ✅"}],
        )]
        output["telemetry"]["error_logs"] = [{"timestamp": "t", "message": "错误日志"}]
        output["critical_alerts"] = {
            "chaos_alerts": [{"alertname": "警报😀", "severity": "critical",
                              "namespace": "ns", "alertstate": "firing"}],
        }
        pdf_path = self.tmp_path / "unicode.pdf"

        build_chaos_report_pdf(output, str(pdf_path))

        assert pdf_path.read_bytes()[:4] == b"%PDF"
        assert pdf_path.stat().st_size > 1000
        assert pdf_path.read_bytes().endswith(b"%%EOF\n")

    def test_repeated_call_same_path_overwrites_and_returns_path(self):
        output = _minimal_chaos_output()
        pdf_path = self.tmp_path / "out.pdf"

        first = build_chaos_report_pdf(output, str(pdf_path))
        second = build_chaos_report_pdf(output, str(pdf_path))

        assert first == second == str(pdf_path)
        assert pdf_path.read_bytes()[:4] == b"%PDF"


class TestPdfMixedScenarios(_PdfTestCase):
    """Scenario pairs where one has data and one does not: the empty ones are
    skipped from per-section tables via their continue branches."""

    def test_vmis_with_monitoring_error_renders_both(self):
        output = _minimal_chaos_output()
        output["telemetry"]["scenarios"] = [_make_scenario(
            affected_vmis={
                "recovered": [{"namespace": "kv", "vmi_name": "vm1"}],
                "unrecovered": [],
                "error": "ssh failed",
            },
        )]

        _, text, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        assert "Disrupted VMIs" in text
        assert "kv/vm1" in text
        assert "Monitoring error: ssh failed" in text

    def test_empty_scenario_skipped_from_data_sections(self):
        full = _make_scenario(
            scenario_name="full.yml",
            affected_pods={"recovered": [{"namespace": "ns", "pod_name": "p1",
                                          "total_recovery_time": 3.0}],
                           "unrecovered": []},
            affected_vmis={"recovered": [{"namespace": "kv", "vmi_name": "vm1",
                                          "total_recovery_time": 8.0}],
                           "unrecovered": []},
            affected_nodes=[{"node_name": "worker-1", "node_id": "i-1",
                             "stopped_time": 10.0}],
            additional_telemetry={"rps": 100},
            cluster_events=[{"type": "Normal", "reason": "R", "message": "m"}],
        )
        empty = _make_scenario(scenario_name="empty.yml")
        output = _minimal_chaos_output()
        output["telemetry"]["scenarios"] = [full, empty]

        _, text, mock_build = _build_mocked(output, self.tmp_path / "out.pdf")

        # the empty scenario gets only a Targets table, no per-section rows
        assert text.count("empty.yml") == 1
        for header in ("Pods Recovered", "VMIs Recovered"):
            table = _table_with(mock_build, header, 0)
            assert [r[0].text for r in table._cellvalues[1:]] == ["full.yml"]
        assert _table_with(mock_build, "Stopped", 1)._cellvalues[0][0].text == "full.yml"
        assert _table_with(mock_build, "Reason", 1)._cellvalues[0][0].text == "full.yml (1 events)"
        assert "Load Test Metrics" in text

    def test_event_with_involved_object_kind_renders_object_ref(self):
        output = _minimal_chaos_output()
        output["telemetry"]["scenarios"] = [_make_scenario(cluster_events=[
            {"type": "Normal", "reason": "R", "message": "m",
             "involved_object_kind": "Pod", "involved_object_name": "mypod"},
        ])]

        _, text, mock_build = _build_mocked(output, self.tmp_path / "out.pdf")

        events_table = _table_with(mock_build, "Reason", 1)
        assert events_table._cellvalues[2][2].text == "Pod/mypod"


class TestPdfBranches(_PdfTestCase):
    """Remaining branch coverage inside build_chaos_report_pdf."""

    def test_non_dict_node_skipped_from_node_recovery(self):
        output = _minimal_chaos_output()
        output["telemetry"]["scenarios"] = [
            _make_scenario(affected_nodes=["node-as-string"]),
        ]

        _, text, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        assert "Node Recovery" not in text

    def _assert_vmi_pvc_target_partial_fields(self, target, present, absent):
        output = _minimal_chaos_output()
        output["telemetry"]["scenarios"] = [_make_scenario(
            parameters={"vmi_pvc_fill_scenario": dict(target)},
        )]

        _, text, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        for expected in present:
            assert expected in text
        for unexpected in absent:
            assert unexpected not in text

    def test_vmi_pvc_target_partial_fields_vmi_only(self):
        # no namespace/pvc/fill/duration
        self._assert_vmi_pvc_target_partial_fields(
            {"vmi_name": "vmi-only"},
            ["VMI Name", "vmi-only"],
            ["PVC Name", "Namespace", "Fill Percentage", "Duration"],
        )

    def test_vmi_pvc_target_partial_fields_pvc_only(self):
        # no vmi_name
        self._assert_vmi_pvc_target_partial_fields(
            {"pvc_name": "pvc-only"},
            ["PVC Name", "pvc-only"],
            ["VMI Name", "Namespace", "Fill Percentage", "Duration"],
        )

    def test_two_health_checks_same_phase_share_group(self):
        output = _minimal_chaos_output()
        output["telemetry"]["health_checks"] = [
            {"url": "http://a", "status": True, "duration": 0.1},
            {"url": "http://b", "status": False, "duration": 0.2},
        ]

        _, text, mock_build = _build_mocked(output, self.tmp_path / "out.pdf")

        table = _table_with(mock_build, "URL / Endpoint", 0)
        assert len(table._cellvalues) == 3  # header + 2 rows in one group

    def test_two_object_state_checks_same_phase_share_group(self):
        output = _minimal_chaos_output()
        output["telemetry"]["object_state_checks"] = [
            {"check_name": "c1", "kind": "Pod", "passed": True},
            {"check_name": "c2", "kind": "Pod", "passed": False, "message": "m"},
        ]

        _, text, mock_build = _build_mocked(output, self.tmp_path / "out.pdf")

        table = _table_with(mock_build, "Check Name", 0)
        assert len(table._cellvalues) == 3

    def test_two_virt_checks_same_phase_share_group(self):
        output = _minimal_chaos_output()
        output["telemetry"]["virt_checks"] = [
            {"vm_name": "vm-1", "status": True, "duration": 1.0},
            {"vm_name": "vm-2", "status": True, "duration": 2.0},
        ]

        _, text, mock_build = _build_mocked(output, self.tmp_path / "out.pdf")

        table = _table_with(mock_build, "VM Name", 0)
        assert len(table._cellvalues) == 3

    def test_slo_failure_table_omits_passing_rows(self):
        output = _minimal_chaos_output()
        output["scenario_slo_details"] = [{
            "scenario": "test.yml",
            "slo_details": [
                {"name": "slo-pass", "severity": "info", "passed": True},
                {"name": "slo-fail", "severity": "warning", "passed": False},
            ],
        }]

        _, text, mock_build = _build_mocked(output, self.tmp_path / "out.pdf")

        slo_table = _table_with(mock_build, "Severity", 0)
        rows = [[c.text for c in r] for r in slo_table._cellvalues[1:]]
        assert rows == [["slo-fail", "warning",
                         f'<font color="{_FAIL_RED}"><b>FAIL</b></font>']]

    def test_slo_entry_all_passed_no_failure_section(self):
        output = _minimal_chaos_output()
        output["scenario_slo_details"] = [{
            "scenario": "test.yml",
            "slo_details": [{"name": "slo-pass", "severity": "info", "passed": True}],
        }]

        _, text, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        assert "Resiliency Score SLO Failures" not in text

    def test_duplicate_vmi_pvc_targets_deduped(self):
        output = _minimal_chaos_output()
        output["telemetry"]["scenarios"] = [_make_scenario(
            parameters={"a": {"vmi_name": "dup-vmi", "namespace": "ns"},
                        "b": {"vmi_name": "dup-vmi", "namespace": "ns"}},
        )]

        _, text, mock_build = _build_mocked(output, self.tmp_path / "out.pdf")

        assert text.count("dup-vmi") == 1  # identical nested target appended once

    def test_parameter_tree_with_scalar_list_items(self):
        output = _minimal_chaos_output()
        output["telemetry"]["scenarios"] = [_make_scenario(
            parameters=["scalar-item", {"label_selector": "app=etcd"}],
        )]

        _, text, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        assert "Label Selector" in text
        assert "app=etcd" in text

    def test_scalar_parameters_handled_without_targets(self):
        output = _minimal_chaos_output()
        output["telemetry"]["scenarios"] = [_make_scenario(parameters="raw")]

        result, text, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        assert result
        assert "Label Selector" not in text

    def test_affected_pods_null_scenario_still_renders(self):
        scenario = _make_scenario(scenario_name="null-pods.yml")
        scenario["affected_pods"] = None
        output = _minimal_chaos_output()
        output["telemetry"]["scenarios"] = [scenario]

        result, text, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        assert result
        assert "null-pods.yml" in text

    def test_affected_vmis_null_scenario_still_renders(self):
        scenario = _make_scenario(scenario_name="null-vmis.yml")
        scenario["affected_vmis"] = None
        output = _minimal_chaos_output()
        output["telemetry"]["scenarios"] = [scenario]

        result, text, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        assert result
        assert "null-vmis.yml" in text

    def test_affected_nodes_null_scenario_still_renders(self):
        scenario = _make_scenario(scenario_name="null-nodes.yml")
        scenario["affected_nodes"] = None
        output = _minimal_chaos_output()
        output["telemetry"]["scenarios"] = [scenario]

        result, text, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        assert result
        assert "null-nodes.yml" in text

    def test_non_dict_scenario_entry_does_not_block_build(self):
        output = _minimal_chaos_output()
        output["telemetry"]["scenarios"] = [
            "raw",
            _make_scenario(scenario_name="ok.yml"),
        ]

        result, text, _ = _build_mocked(output, self.tmp_path / "out.pdf")

        assert result
        assert "ok.yml" in text
