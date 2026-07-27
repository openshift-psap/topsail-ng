"""
Notification formatting for Caliper postprocess status.

Provides formatting functions for converting typed postprocess status
into GitHub notification text. Now uses the typed dataclass models
from the public API with object-oriented step formatting.
"""

from __future__ import annotations

from projects.caliper.public import PostprocessStatus, StepStatus


def format_postprocess_status_notification(
    status: PostprocessStatus, get_file_link: callable | None = None
) -> str:
    """Format postprocess status into notification text with file links.

    Args:
        status: Typed PostprocessStatus object
        get_file_link: Optional callback function that takes a file path and returns a URL.
                      Signature: get_file_link(file_path: str) -> str

    Returns:
        Formatted notification text to include in GitHub notification
    """

    if not status or not status.steps:
        return []

    lines = []

    # Check overall status (keep unchanged regardless of abort status)
    status_emoji = "✅" if status.is_success() else "❌"
    base_directory = status.base_directory
    if base_directory.name == "status_files":
        base_directory = base_directory.parent

    lines.append(f"**Post-processing Status** {status_emoji} `{base_directory}`")

    # Convert steps from list[dict] format to sorted list of (step_name, step_data) tuples
    step_tuples = []
    for step_dict in status.steps:
        for step_name, step_data in step_dict.items():
            step_tuples.append((step_name, step_data))

    # Sort by completion timestamp, with fallback to step name for stable ordering
    sorted_steps = sorted(
        step_tuples,
        key=lambda item: (
            item[1].get("completed_at", 0) or 0,  # Use completed_at if available, else 0
            item[0],  # fallback to step name for stable ordering
        ),
    )

    for step_name, step_data in sorted_steps:
        step_emoji = _get_step_emoji(step_data.get("status", "unknown"))

        # Create step name as link to log file if available
        log_file = step_data.get("log_file")
        if log_file and get_file_link:
            try:
                log_url = get_file_link(log_file)
                step_name_display = f"[**{step_name}**]({log_url})"
            except Exception:
                # Fallback to plain text if link generation fails
                step_name_display = f"**{step_name}**"
        else:
            step_name_display = f"**{step_name}**"

        # Format step with message if available
        lines.append(f"- {step_emoji} {step_name_display}: `{step_data.get('status', 'unknown')}`")
        message = step_data.get("message")
        if message:
            lines.append(f"  * `{message}`")

        reason = step_data.get("reason")
        if reason:
            lines.append(f"  * `{reason}`")

        # Use object-oriented step formatter to handle step-specific details
        step_details = _format_step_details_with_formatters(step_name, step_data, get_file_link)
        lines.extend(step_details)

    return "\n".join(lines) if lines else ""


def _format_step_details_with_formatters(
    step_name: str, step_data: dict, get_file_link: callable | None
) -> list[str]:
    """Format step-specific details using object-oriented formatters."""
    if step_name == "artifacts_to_kpis":
        return _format_artifacts_to_kpis_step(step_data, get_file_link)
    elif step_name == "dashboard_csv":
        return _format_dashboard_csv_step(step_data, get_file_link)
    elif step_name == "artifacts_to_ai_data":
        return _format_artifacts_to_ai_data_step(step_data, get_file_link)
    elif step_name == "s3_export":
        return _format_s3_export_step(step_data, get_file_link)
    elif step_name == "s3_import":
        return _format_s3_import_step(step_data, get_file_link)
    elif step_name == "analyse_kpis":
        return _format_analyse_kpis_step(step_data, get_file_link)
    else:
        return _format_generic_file_step(step_data, get_file_link)


def _create_file_link(file_path: str, emoji: str, get_file_link: callable | None) -> str:
    """Create a file link line for notifications."""
    if not get_file_link:
        filename = file_path.split("/")[-1]
        return f"  - {emoji} {filename}"

    try:
        from pathlib import Path

        output_path = Path(file_path)
        filename = output_path.name
        file_url = get_file_link(file_path)
        return f"  - {emoji} [{filename}]({file_url})"
    except Exception:
        filename = file_path.split("/")[-1]
        return f"  - {emoji} {filename}"


def _format_artifacts_to_kpis_step(step_data: dict, get_file_link: callable | None) -> list[str]:
    """Format artifacts_to_kpis step details."""
    lines = []
    output_file = step_data.get("output_file")
    if output_file:
        lines.append(_create_file_link(output_file, "📄", get_file_link))

    # Include HTML file if available
    html_file = step_data.get("html_file")
    if html_file:
        lines.append(_create_file_link(html_file, "🌐", get_file_link))

    return lines


def _format_dashboard_csv_step(step_data: dict, get_file_link: callable | None) -> list[str]:
    """Format dashboard_csv step details."""
    lines = []
    output_file = step_data.get("output_file")
    if output_file:
        lines.append(_create_file_link(output_file, "📊", get_file_link))
    return lines


def _format_artifacts_to_ai_data_step(step_data: dict, get_file_link: callable | None) -> list[str]:
    """Format artifacts_to_ai_data step details."""
    lines = []

    if not get_file_link:
        return lines

    # AI data directory link
    ai_data_dir = step_data.get("ai_data_dir")
    if ai_data_dir:
        try:
            # Extract relative path from the full path
            ai_data_dir_relative = ai_data_dir.split("/")[-1]  # Get just "ai_eval"
            dir_url = get_file_link(ai_data_dir_relative)
            lines.append(f"  - 📁 [AI Eval Directory]({dir_url})")
        except Exception:
            lines.append(f"  - 📁 AI Eval Directory: {ai_data_dir}")

    # Output file link
    output_file = step_data.get("output_file")
    if output_file:
        try:
            import os

            output_file_relative = os.path.relpath(
                output_file,
                ai_data_dir or "",
            )
            if ai_data_dir and "ai_eval" in ai_data_dir:
                output_file_relative = f"ai_eval/{output_file_relative}"
            file_url = get_file_link(output_file_relative)
            filename = output_file.split("/")[-1]
            lines.append(f"  - 📄 [{filename}]({file_url})")
        except Exception:
            filename = output_file.split("/")[-1]
            lines.append(f"  - 📄 {filename}")

    return lines


def _format_s3_export_step(step_data: dict, get_file_link: callable | None) -> list[str]:
    """Format s3_export step details."""
    lines = []

    # Show exported path
    exported_path = step_data.get("exported_path")
    if exported_path:
        lines.append(f"  - 📤 Exported to: `{exported_path}`")

    # Show uploaded files count
    uploaded_files = step_data.get("uploaded_files")
    if uploaded_files is not None:
        lines.append(f"  - ✅ Uploaded files: {uploaded_files}")

    # Show failed files count if > 0
    failed_files = step_data.get("failed_files")
    if failed_files and failed_files > 0:
        lines.append(f"  - ❌ Failed files: {failed_files}")

    return lines


def _format_s3_import_step(step_data: dict, get_file_link: callable | None) -> list[str]:
    """Format s3_import step details."""
    lines = []

    # Show downloaded files count
    downloaded_files = step_data.get("downloaded_files")
    if downloaded_files is not None:
        lines.append(f"  - ⬇️ Downloaded files: {downloaded_files}")

    # Show failed files count if > 0
    failed_files = step_data.get("failed_files")
    if failed_files and failed_files > 0:
        lines.append(f"  - ❌ Failed files: {failed_files}")

    return lines


def _format_analyse_kpis_step(step_data: dict, get_file_link: callable | None) -> list[str]:
    """Format analyse_kpis step details."""
    lines = []

    # Show analysis output file
    output_file = step_data.get("output_file")
    if output_file:
        lines.append(_create_file_link(output_file, "📊", get_file_link))

    step_status = step_data.get("status")

    # Show error message if step failed
    if step_status == "failed":
        error_msg = step_data.get("error")
        if error_msg:
            lines.append(f"  - ❌ `{error_msg}`")

    # Show regression analysis results if the step was successful
    elif step_status in ("success", "warning", "regression_detected"):
        # Show regression analysis results
        if step_data.get("regressions_detected"):
            lines.append("  - ❌ Regression detected")
            lines.append(
                f"  - `{step_data.get('regression_count')}` regressions out of `{step_data.get('total_kpis')}` KPIs"
            )
        else:
            total_kpis = step_data.get("total_kpis")
            if total_kpis is not None:
                lines.append(f"  - No regression in `{total_kpis}` KPIs")

        # Show baseline files count if available
        baseline_files_count = step_data.get("baseline_files_count")
        if baseline_files_count is not None:
            lines.append(f"  - 📈 Baseline files analyzed: `{baseline_files_count}`")

    # Include HTML file if available
    html_file = step_data.get("html_file")
    if html_file:
        lines.append(_create_file_link(html_file, "🌐", get_file_link))

    return lines


def _format_generic_file_step(step_data: dict, get_file_link: callable | None) -> list[str]:
    """Format generic file step details (like visualize)."""
    lines = []

    if not get_file_link:
        return lines

    # Add general file links if available (for visualize step, etc.)
    file_paths = step_data.get("output_files") or step_data.get("paths")
    if file_paths:
        lines.extend(_format_step_file_links(file_paths, step_data, get_file_link))

    return lines


def _format_step_file_links(
    file_paths: list[str], step_data: dict, get_file_link: callable
) -> list[str]:
    """Format file paths as clickable links using the provided callback."""
    if not file_paths:
        return []

    lines = []

    # Check if we need to combine output_dir with file paths (e.g., for visualize step)
    output_dir = step_data.get("output_dir")

    # Group files by type for better organization
    file_groups = _group_files_by_type(file_paths)

    # Flatten the structure - just list all files without grouping by type
    for file_type, files in file_groups.items():
        for file_path in files:
            try:
                # Combine output_dir with file_path if available
                if output_dir:
                    # Use pathlib to properly join paths and avoid double slashes
                    from pathlib import Path

                    full_path = str(Path(output_dir) / file_path)
                else:
                    full_path = file_path

                file_url = get_file_link(full_path)
                file_name = _get_display_name(file_path)
                emoji = "📊" if file_type == "visualization" else "📄"
                lines.append(f"  - {emoji} [{file_name}]({file_url})")
            except Exception:
                # Fallback to plain text if link generation fails
                file_name = _get_display_name(file_path)
                emoji = "📊" if file_type == "visualization" else "📄"
                lines.append(f"  - {emoji} {file_name}")

    return lines


def _group_files_by_type(file_paths: list[str]) -> dict[str, list[str]]:
    """Group file paths by their type based on extension."""
    groups = {}

    for file_path in file_paths:
        file_type = _get_file_type(file_path)
        if file_type not in groups:
            groups[file_type] = []
        groups[file_type].append(file_path)

    return groups


def _get_file_type(file_path: str) -> str:
    """Determine file type from path."""
    from pathlib import Path

    ext = Path(file_path).suffix.lower()

    if ext in (".html", ".htm"):
        return "report"
    elif ext in (".png", ".jpg", ".jpeg", ".svg", ".pdf"):
        return "visualization"
    elif ext in (".json", ".yaml", ".yml"):
        return "data"
    elif ext in (".csv", ".tsv"):
        return "table"
    elif ext in (".txt", ".log"):
        return "log"
    else:
        return "file"


def _get_display_name(file_path: str) -> str:
    """Get display name for a file path."""
    from pathlib import Path

    path = Path(file_path)

    # For files in subdirectories, show parent/filename for context
    if len(path.parts) > 1:
        parent = path.parent.name
        return f"{parent}/{path.name}"

    return path.name


def _get_step_emoji(status: str) -> str:
    """Get emoji for step status."""
    if status == StepStatus.SUCCESS:
        return "✅"
    elif status in (StepStatus.FAILED, "failure"):  # Keep "failure" for backward compatibility
        return "❌"
    elif status in ("skipped", StepStatus.DISABLED):  # Keep "skipped" for backward compatibility
        return "⏭️"
    elif status == StepStatus.WARNING:
        return "⚠️"
    elif status == StepStatus.REGRESSION_DETECTED:
        return "🚨"
    else:
        return "⚠️"


def parse_postprocess_status(status_data: dict) -> PostprocessStatus | None:
    """Parse postprocess status data into typed PostprocessStatus object.

    Args:
        status_data: Raw postprocess status dictionary

    Returns:
        PostprocessStatus object or None if data is invalid
    """
    if not status_data or not isinstance(status_data, dict):
        return None

    try:
        return PostprocessStatus.from_orchestration_result(status_data)
    except Exception:
        return None
