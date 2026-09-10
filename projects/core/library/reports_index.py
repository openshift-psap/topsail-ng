"""
Generate HTML index pages for Caliper reports.

Inspired by topsail/testing/utils/generate_plot_index.py but adapted for
Caliper postprocessing workflow.
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from projects.caliper.orchestration.postprocess import POSTPROCESS_STATUS_FILENAME

logger = logging.getLogger(__name__)


def generate_caliper_reports_index(
    status: dict[str, Any], output_dir: Path, index_filename: str = "index.html"
) -> Path | None:
    """
    Generate an HTML index page with links to all Caliper reports.

    Args:
        status: Caliper postprocess status dict with steps information
        output_dir: Directory where reports are located and where index will be written
        index_filename: Name of the index file to generate (default: "index.html")

    Returns:
        Path to the generated index file, or None if no reports found
    """
    output_dir = Path(output_dir).resolve()
    index_path = output_dir / index_filename

    # Extract report information from status
    steps = status.get("steps", [])
    visualize_step = {}

    # Find visualize step in list format
    if isinstance(steps, list):
        for step in steps:
            if "visualize" in step:
                visualize_step = step["visualize"]
                break
    elif isinstance(steps, dict):
        # Legacy dict format fallback
        visualize_step = steps.get("visualize", {})

    if visualize_step.get("status") not in ("ok", "success"):
        logger.info("No successful visualize step found, skipping index generation")
        return None

    # Look for HTML and data files in the output directory
    html_files = []
    data_files = []

    ignored_files = {index_filename, POSTPROCESS_STATUS_FILENAME}

    for html_file in sorted(output_dir.glob("*.html")):
        if html_file.name not in ignored_files:
            html_files.append(html_file)

    # Include JSON and JSON files
    for data_file in sorted(output_dir.glob("*.json")):
        if data_file.name not in ignored_files:
            data_files.append(data_file)

    # Only list files in the root output directory (no subdirectories)

    if not html_files and not data_files:
        logger.info("No HTML or data reports found, skipping index generation")
        return None

    # Generate HTML content
    html_content = _generate_index_html_content(
        html_files=html_files, data_files=data_files, output_dir=output_dir, status=status
    )

    # Write index file
    try:
        index_path.write_text(html_content, encoding="utf-8")
        logger.info("Generated Caliper reports index at %s", index_path)
        return index_path
    except OSError as e:
        logger.warning("Failed to write reports index to %s: %s", index_path, e)
        return None


def _generate_index_html_content(
    html_files: list[Path], data_files: list[Path], output_dir: Path, status: dict[str, Any]
) -> str:
    """Generate the HTML content for the reports index."""

    # Get test outcome and timing information
    test_phase = status.get("test_phase", {})
    test_status = test_phase.get("phase", "UNKNOWN")
    final_status = status.get("final_status", "unknown")

    # Build HTML content
    html_parts = [
        "<!DOCTYPE html>",
        "<html>",
        "<head>",
        "    <meta charset='utf-8'>",
        "    <title>Caliper Reports Index</title>",
        "    <style>",
        "        body { font-family: Arial, sans-serif; margin: 40px; }",
        "        h1 { color: #333; border-bottom: 2px solid #333; }",
        "        h2 { color: #666; margin-top: 30px; }",
        "        ul { list-style-type: disc; padding-left: 20px; }",
        "        li { margin: 8px 0; }",
        "        a { text-decoration: none; color: #0066cc; }",
        "        .status { padding: 10px; border-radius: 5px; margin: 20px 0; }",
        "        .success { background-color: #d4edda; border: 1px solid #c3e6cb; }",
        "        .warning { background-color: #fff3cd; border: 1px solid #ffeaa7; }",
        "        .error { background-color: #f8d7da; border: 1px solid #f5c6cb; }",
        "    </style>",
        "</head>",
        "<body>",
        "    <h1>Caliper Reports Index</h1>",
    ]

    # Add status information
    status_class = "success" if "failed" not in final_status else "error"
    html_parts.extend(
        [
            f"    <div class='status {status_class}'>",
            f"        <strong>Test Status:</strong> {test_status}<br>",
            f"        <strong>Final Status:</strong> {final_status}",
            "    </div>",
        ]
    )

    # Add HTML reports section
    if html_files:
        html_parts.append("    <h2>📊 HTML Reports</h2>")
        html_parts.append("    <ul>")
        for html_file in html_files:
            rel_path = html_file.relative_to(output_dir)
            html_parts.append(f"        <li><a href='{rel_path}'>{html_file.name}</a></li>")
        html_parts.append("    </ul>")

    # Add data files section
    if data_files:
        html_parts.append("    <h2>📋 Data Files</h2>")
        html_parts.append("    <ul>")
        for data_file in data_files:
            rel_path = data_file.relative_to(output_dir)
            html_parts.append(f"        <li><a href='{rel_path}'>{data_file.name}</a></li>")
        html_parts.append("    </ul>")

    # Add footer with generation timestamp
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    html_parts.extend(
        [
            "    <hr>",
            f"    <p><small>Generated on {timestamp} by FORGE Caliper</small></p>",
            "</body>",
            "</html>",
        ]
    )

    return "\n".join(html_parts) + "\n"
