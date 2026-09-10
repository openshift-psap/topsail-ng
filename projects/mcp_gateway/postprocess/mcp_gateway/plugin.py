"""MCP Gateway Caliper PostProcessingPlugin."""

from __future__ import annotations

import logging
from pathlib import Path

from projects.caliper.engine.kpi import KpiCatalogEntry, KpiComputationStatus, KpiRecord
from projects.caliper.engine.kpi.analyze import AnalysisConfig
from projects.caliper.engine.model import (
    ParseResult,
    PostProcessingPlugin,
    TestBaseNode,
    UnifiedRunModel,
)

from .parsing import MCPGatewayKpiHandler, MCPGatewayParser

logger = logging.getLogger(__name__)

# Compare versions while matching on load shape / target / protocol.
analysis_config = AnalysisConfig(
    comparison_labels=["mcp_gateway_version"],
    ignored_labels=[],
    sorting_labels=["num_servers", "users", "target"],
    regression_config={
        "SCALAR_RELATIVE_CHANGE": {
            "max_relative_regression": 0.10,
            "min_baseline_points": 1,
        },
    },
)


class MCPGatewayPlugin(PostProcessingPlugin):
    """Parses Locust stats.csv artifacts from MCP Gateway performance tests."""

    def __init__(self):
        self.parser = MCPGatewayParser()
        self.kpi_handler = MCPGatewayKpiHandler()

    def parse(self, nodes: list[TestBaseNode]) -> ParseResult:
        return self.parser.parse(nodes)

    def compute_kpis(self, model: UnifiedRunModel) -> tuple[list[KpiRecord], KpiComputationStatus]:
        return self.kpi_handler.compute_kpis(model)

    def kpi_catalog(self) -> list[KpiCatalogEntry]:
        """Return catalog of available KPIs for hierarchical formatting."""
        return self.kpi_handler.get_catalog()

    def export_dashboard_csv(self, model: UnifiedRunModel, output_path: Path) -> str:
        """Generate dashboard CSV without header comments."""
        import csv

        # Get KPIs from the handler
        kpis, _ = self.kpi_handler.compute_kpis(model)

        # Write CSV without any header comments
        with open(output_path, "w", newline="", encoding="utf-8") as csvfile:
            writer = csv.writer(csvfile)

            if not kpis:
                # Write empty CSV with just headers
                writer.writerow(["kpi_id", "value", "timestamp", "labels"])
                return str(output_path)

            # Write column headers and data rows only (no comment headers)
            writer.writerow(["kpi_id", "value", "timestamp", "labels"])

            for kpi in kpis:
                writer.writerow([kpi.kpi_id, kpi.value, kpi.timestamp, str(kpi.labels)])

        return str(output_path)


def get_plugin() -> PostProcessingPlugin:
    """Return the MCP Gateway plugin instance."""
    return MCPGatewayPlugin()
