"""Tests for the MCP Gateway Caliper PostProcessingPlugin."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from projects.caliper.engine.constants import METADATA_FILE
from projects.caliper.engine.model import TestBaseNode, UnifiedRunModel
from projects.caliper.prometheus_metrics.queries import load_queries
from projects.mcp_gateway.postprocess.mcp_gateway.parsing.kpis import MCPGatewayKpiHandler
from projects.mcp_gateway.postprocess.mcp_gateway.parsing.parsers import MCPGatewayParser
from projects.mcp_gateway.postprocess.mcp_gateway.plugin import MCPGatewayPlugin, get_plugin

SAMPLE_STATS_CSV = (
    "Type,Name,Request Count,Failure Count,Average Response Time,"
    "50%,90%,95%,99%,Max Response Time,Requests/s\n"
    "POST,/mcp/tools,800,2,42.5,35,70,90,150,300,25.0\n"
    "GET,/health,200,0,5.0,4,8,10,15,20,6.5\n"
    ",Aggregated,1000,2,33.5,30,60,80,120,300,31.5\n"
)

MCP_STATS_CSV = (
    "Type,Name,Request Count,Failure Count,Average Response Time,"
    "50%,90%,95%,99%,Max Response Time,Requests/s\n"
    "MCP,initialize,16,0,40.0,38,50,55,70,90,0.5\n"
    "MCP,tools/list,16,0,20.0,18,28,30,40,50,0.5\n"
    "MCP,call:alpha,400,0,12.0,10,18,20,28,40,12.5\n"
    "MCP,call:bravo,400,0,14.0,12,20,24,32,45,12.5\n"
    "MCP,FAIL:call:alpha,10,10,80.0,70,90,100,120,150,0.3\n"
    ",Aggregated,842,10,14.5,12,22,26,40,150,26.3\n"
)

STATELESS_STATS_CSV = (
    "Type,Name,Request Count,Failure Count,Average Response Time,"
    "50%,90%,95%,99%,Max Response Time,Requests/s\n"
    "MCP,ttftr,16,0,11.0,9,14,16,20,28,0.5\n"
    "MCP,call:alpha,800,0,9.0,8,12,14,18,25,25.0\n"
    ",Aggregated,816,0,9.1,8,13,15,20,40,25.5\n"
)

TEST_LABELS = {
    "preset": "smoke",
    "target": "gateway",
    "users": "16",
    "num_servers": "1",
    "protocol_mode": "stateful",
    "mcp_gateway_version": "0.7.0",
    "version_kind": "release",
}


def _prom_capture(query_key: str, series: list[dict]) -> dict:
    return {
        "query_key": query_key,
        "response": {
            "status": "success",
            "data": {"resultType": "matrix", "result": series},
        },
    }


def _series(
    namespace: str, pod: str, values: list[float], extra_metric: dict | None = None
) -> dict:
    metric = {"namespace": namespace, "pod": pod}
    if extra_metric:
        metric.update(extra_metric)
    return {
        "metric": metric,
        "values": [[1000.0 + i, str(v)] for i, v in enumerate(values)],
    }


def _make_test_node(
    base_dir: Path,
    name: str,
    stats_csv: str,
    labels: dict,
    *,
    prom_files: dict[str, dict] | None = None,
) -> TestBaseNode:
    """Create a test base directory with stats.csv and __caliper_test_metadata__.yaml."""
    node_dir = base_dir / name
    node_dir.mkdir(parents=True, exist_ok=True)

    (node_dir / "stats.csv").write_text(stats_csv, encoding="utf-8")
    (node_dir / "master.log").write_text("log output", encoding="utf-8")
    (node_dir / METADATA_FILE).write_text(
        yaml.safe_dump({"version": "1", "labels": labels}, sort_keys=False),
        encoding="utf-8",
    )

    if prom_files:
        raw_dir = node_dir / "metrics" / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        for filename, payload in prom_files.items():
            (raw_dir / filename).write_text(json.dumps(payload), encoding="utf-8")

    artifact_paths = sorted(
        p for p in node_dir.rglob("*") if p.is_file() and p.name != METADATA_FILE
    )
    return TestBaseNode(
        directory=node_dir,
        test_path=Path(name),
        test_labels={"version": "1", "labels": labels},
        artifact_paths=artifact_paths,
    )


def _prom_files() -> dict[str, dict]:
    return {
        "cpu_usage.json": _prom_capture(
            "cpu_usage",
            [
                _series("mcp-system", "mcp-gateway-abc", [0.2, 0.4, 0.6]),
                _series("gateway-system", "mcp-gateway-istio-xyz", [0.1, 0.1, 0.4]),
                _series("mcp-gw-bench", "locust-master", [0.9, 0.9, 0.9]),
            ],
        ),
        "memory_usage.json": _prom_capture(
            "memory_usage",
            [
                _series("mcp-system", "mcp-gateway-abc", [100.0, 200.0, 300.0]),
                _series("gateway-system", "mcp-gateway-istio-xyz", [50.0, 50.0, 80.0]),
            ],
        ),
        "http_4xx_rate.json": _prom_capture(
            "http_4xx_rate",
            [
                {
                    "metric": {"destination_workload": "mcp-gateway-istio"},
                    "values": [[1000.0, "0.2"], [1001.0, "0.4"]],
                }
            ],
        ),
    }


# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------


class TestMCPGatewayParser:
    def test_parse_creates_records(self, tmp_path: Path):
        node = _make_test_node(tmp_path, "run-a", SAMPLE_STATS_CSV, TEST_LABELS)
        parser = MCPGatewayParser()

        result = parser.parse([node])

        assert len(result.records) == 1
        assert result.warnings == []
        record = result.records[0]
        assert record.run_identity == {"mcp_gateway": True}
        assert record.metrics["total_requests"] == 1000
        assert record.metrics["requests_per_second"] == 31.5
        assert record.metrics["p95_ms"] == 80.0

    def test_parse_does_not_write_metrics_json(self, tmp_path: Path):
        """Parser no longer writes metrics.json — caliper handles it generically."""
        node = _make_test_node(tmp_path, "run-a", SAMPLE_STATS_CSV, TEST_LABELS)
        parser = MCPGatewayParser()

        parser.parse([node])

        assert not (tmp_path / "run-a" / "metrics.json").exists()

    def test_parse_no_stats_csv(self, tmp_path: Path):
        node_dir = tmp_path / "run-empty"
        node_dir.mkdir(parents=True)
        (node_dir / "master.log").write_text("log")
        node = TestBaseNode(
            directory=node_dir,
            test_path=Path("run-empty"),
            test_labels={"version": "1", "labels": TEST_LABELS},
            artifact_paths=[node_dir / "master.log"],
        )
        parser = MCPGatewayParser()

        result = parser.parse([node])

        assert len(result.records) == 1
        assert result.records[0].metrics.get("no_stats_csv_found") is True
        assert result.records[0].parse_notes == ["No stats.csv file found"]

    def test_parse_multiple_nodes(self, tmp_path: Path):
        labels_a = {**TEST_LABELS, "users": "16"}
        labels_b = {**TEST_LABELS, "users": "64", "num_servers": "2"}
        node_a = _make_test_node(tmp_path, "run-a", SAMPLE_STATS_CSV, labels_a)
        node_b = _make_test_node(tmp_path, "run-b", SAMPLE_STATS_CSV, labels_b)
        parser = MCPGatewayParser()

        result = parser.parse([node_a, node_b])

        assert len(result.records) == 2
        paths = {r.test_base_path for r in result.records}
        assert "run-a" in paths
        assert "run-b" in paths

        for node_name in ["run-a", "run-b"]:
            assert not (tmp_path / node_name / "metrics.json").exists()
            assert not (tmp_path / node_name / "parameters.json").exists()

    def test_parse_promotes_locust_operations(self, tmp_path: Path):
        node = _make_test_node(tmp_path, "run-mcp", MCP_STATS_CSV, TEST_LABELS)
        record = MCPGatewayParser().parse([node]).records[0]

        assert record.metrics["handshake_p95_ms"] == 55.0
        assert record.metrics["tools_list_p95_ms"] == 30.0
        assert record.metrics["tools_list_rps"] == 0.5
        assert record.metrics["tool_call_rps"] == pytest.approx(25.3)
        assert record.metrics["tool_call_p95_ms"] == pytest.approx(22.0)
        assert record.metrics["tool_call_failure_rate"] == pytest.approx(10 / 810, abs=1e-6)

    def test_parse_stateless_ttftr(self, tmp_path: Path):
        labels = {**TEST_LABELS, "protocol_mode": "stateless"}
        node = _make_test_node(tmp_path, "run-sl", STATELESS_STATS_CSV, labels)
        record = MCPGatewayParser().parse([node]).records[0]

        assert "handshake_p95_ms" not in record.metrics
        assert "tools_list_p95_ms" not in record.metrics
        assert record.metrics["ttftr_p95_ms"] == 16.0
        assert record.distinguishing_labels["protocol_mode"] == "stateless"

    def test_parse_prometheus_resource_kpis(self, tmp_path: Path):
        node = _make_test_node(
            tmp_path, "run-prom", MCP_STATS_CSV, TEST_LABELS, prom_files=_prom_files()
        )
        record = MCPGatewayParser().parse([node]).records[0]

        assert record.metrics["broker_cpu_avg_cores"] == pytest.approx(0.4)
        assert record.metrics["broker_cpu_max_cores"] == pytest.approx(0.6)
        assert record.metrics["envoy_cpu_avg_cores"] == pytest.approx(0.2)
        assert record.metrics["envoy_cpu_max_cores"] == pytest.approx(0.4)
        assert record.metrics["broker_memory_avg_bytes"] == pytest.approx(200.0)
        assert record.metrics["envoy_memory_max_bytes"] == pytest.approx(80.0)
        assert record.metrics["http_4xx_rate"] == pytest.approx(0.3)

    def test_parse_http_4xx_zero_when_empty(self, tmp_path: Path):
        prom = {
            "http_4xx_rate.json": _prom_capture("http_4xx_rate", []),
        }
        node = _make_test_node(tmp_path, "run-4xx", MCP_STATS_CSV, TEST_LABELS, prom_files=prom)
        record = MCPGatewayParser().parse([node]).records[0]
        assert record.metrics["http_4xx_rate"] == 0.0


# ---------------------------------------------------------------------------
# KPI tests
# ---------------------------------------------------------------------------


class TestMCPGatewayKpis:
    def test_compute_kpis(self, tmp_path: Path):
        node = _make_test_node(tmp_path, "run-a", SAMPLE_STATS_CSV, TEST_LABELS)
        parser = MCPGatewayParser()
        parse_result = parser.parse([node])

        model = UnifiedRunModel(
            plugin_module="projects.mcp_gateway.postprocess.mcp_gateway.plugin",
            base_directory=str(tmp_path),
            test_nodes=[node],
            unified_result_records=parse_result.records,
        )

        handler = MCPGatewayKpiHandler()
        kpis = handler.compute_kpis(model)

        assert len(kpis) > 0
        kpi_dict = {k["kpi_id"]: k for k in kpis}
        assert kpi_dict["mcp_gw_requests_per_second"]["value"] == 31.5
        assert kpi_dict["mcp_gw_p95_ms"]["value"] == 80.0
        assert kpi_dict["mcp_gw_failure_rate"]["value"] == pytest.approx(0.002, abs=1e-4)
        assert "mcp_gw_tool_call_rps" not in kpi_dict

    def test_compute_operation_and_prom_kpis(self, tmp_path: Path):
        node = _make_test_node(
            tmp_path, "run-full", MCP_STATS_CSV, TEST_LABELS, prom_files=_prom_files()
        )
        parse_result = MCPGatewayParser().parse([node])
        model = UnifiedRunModel(
            plugin_module="projects.mcp_gateway.postprocess.mcp_gateway.plugin",
            base_directory=str(tmp_path),
            test_nodes=[node],
            unified_result_records=parse_result.records,
        )

        kpis = MCPGatewayKpiHandler.compute_kpis(model)
        kpi_dict = {k["kpi_id"]: k for k in kpis}

        assert kpi_dict["mcp_gw_tool_call_rps"]["value"] == pytest.approx(25.3)
        assert kpi_dict["mcp_gw_tool_call_p95_ms"]["value"] == pytest.approx(22.0)
        assert kpi_dict["mcp_gw_handshake_p95_ms"]["value"] == 55.0
        assert kpi_dict["mcp_gw_tools_list_p95_ms"]["value"] == 30.0
        assert kpi_dict["mcp_gw_broker_cpu_avg_cores"]["value"] == pytest.approx(0.4)
        assert kpi_dict["mcp_gw_envoy_memory_max_bytes"]["value"] == pytest.approx(80.0)
        assert kpi_dict["mcp_gw_http_4xx_rate"]["value"] == pytest.approx(0.3)
        assert kpi_dict["mcp_gw_tool_call_p95_ms"]["labels"]["protocol_mode"] == "stateful"
        assert kpi_dict["mcp_gw_tool_call_p95_ms"]["labels"]["target"] == "gateway"
        assert kpi_dict["mcp_gw_tool_call_p95_ms"]["labels"]["mcp_gateway_version"] == "0.7.0"
        assert kpi_dict["mcp_gw_tool_call_p95_ms"]["labels"]["version_kind"] == "release"

    def test_compute_kpis_skips_missing_records(self):
        from projects.caliper.engine.model import UnifiedResultRecord

        record = UnifiedResultRecord(
            test_base_path="run-empty",
            distinguishing_labels={},
            metrics={"no_stats_csv_found": True},
            run_identity={"mcp_gateway": True},
        )
        model = UnifiedRunModel(
            plugin_module="test",
            base_directory="/tmp",
            test_nodes=[],
            unified_result_records=[record],
        )

        kpis = MCPGatewayKpiHandler.compute_kpis(model)
        assert kpis == []


# ---------------------------------------------------------------------------
# Plugin integration tests
# ---------------------------------------------------------------------------


class TestMCPGatewayPlugin:
    def test_get_plugin_returns_instance(self):
        plugin = get_plugin()
        assert isinstance(plugin, MCPGatewayPlugin)

    def test_analysis_config_present(self):
        from projects.mcp_gateway.postprocess.mcp_gateway import plugin as plugin_mod

        assert plugin_mod.analysis_config.comparison_labels == ["mcp_gateway_version"]
        assert (
            plugin_mod.analysis_config.regression_config["SCALAR_RELATIVE_CHANGE"][
                "max_relative_regression"
            ]
            == 0.10
        )

    def test_plugin_parse_and_kpis(self, tmp_path: Path):
        node = _make_test_node(tmp_path, "run-a", SAMPLE_STATS_CSV, TEST_LABELS)
        plugin = get_plugin()

        parse_result = plugin.parse([node])
        assert len(parse_result.records) == 1

        model = UnifiedRunModel(
            plugin_module="projects.mcp_gateway.postprocess.mcp_gateway.plugin",
            base_directory=str(tmp_path),
            test_nodes=[node],
            unified_result_records=parse_result.records,
        )

        kpis = plugin.compute_kpis(model)
        assert len(kpis) > 0

    def test_visualize_returns_empty(self, tmp_path: Path):
        plugin = get_plugin()
        model = UnifiedRunModel(
            plugin_module="test",
            base_directory=str(tmp_path),
            test_nodes=[],
            unified_result_records=[],
        )
        result = plugin.visualize(model, tmp_path, None, None, None)
        assert result == []


class TestQueryCatalog:
    def test_http_4xx_rate_is_loadable(self):
        specs = load_queries(namespaces=["mcp-system", "gateway-system"], keys=["http_4xx_rate"])
        assert len(specs) == 1
        assert specs[0].key == "http_4xx_rate"
        assert "4.." in specs[0].promql
        assert "mcp-system|gateway-system" in specs[0].promql
