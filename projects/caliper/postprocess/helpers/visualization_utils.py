"""Core visualization utilities for Caliper reports and analysis."""

from __future__ import annotations

import base64
import hashlib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _truncate_filename(filename: str, max_length: int = 200) -> str:
    """
    Truncate a filename if it's too long, preserving the end and adding a hash.

    Args:
        filename: Original filename (without extension)
        max_length: Maximum allowed filename length

    Returns:
        Truncated filename with hash if needed
    """
    if len(filename) <= max_length:
        return filename

    # Create a hash of the original filename
    hash_object = hashlib.md5(filename.encode())
    filename_hash = hash_object.hexdigest()[:8]

    # Truncate and add hash
    truncated = filename[: (max_length - 9)] + "_" + filename_hash
    logger.warning(f"Filename too long, truncated to: {truncated}")

    return truncated


def save_figure(
    fig,
    output_dir: Path,
    filename: str,
    as_image: bool = True,
    report_number: int | None = None,
    width: int = 1200,
    height: int = 650,
) -> str | None:
    """
    Save a plotly figure as either an image or HTML file with optional report numbering.

    Args:
        fig: Plotly figure object
        output_dir: Directory to save the file
        filename: Base filename (without extension)
        as_image: If True, save as PNG; if False, save as HTML
        report_number: Optional report number for file naming (e.g., 0 for "report_00_")
        width: Image width in pixels (for PNG output)
        height: Image height in pixels (for PNG output)

    Returns:
        Path to the saved file, or None if failed
    """
    try:
        # Add report number prefix if provided
        if report_number is not None:
            final_filename = f"report_{report_number:02d}_{filename}"
        else:
            final_filename = filename

        # Truncate filename if too long to avoid filesystem limits
        final_filename = _truncate_filename(final_filename)

        if as_image:
            logger.info(f"Saving {final_filename} as PNG image...")
            output_file = output_dir / f"{final_filename}.png"
            try:
                fig.write_image(output_file, width=width, height=height)
                logger.debug(f"{final_filename} PNG saved successfully")
                return str(output_file)
            except Exception as png_error:
                # PNG generation failed - check if it's a Kaleido/Chrome issue
                error_msg = str(png_error)
                if "Kaleido" in error_msg or "Chrome" in error_msg:
                    logger.warning(
                        f"PNG generation failed for {final_filename} due to missing Chrome/Kaleido: {png_error}"
                    )
                    logger.warning(
                        "Hint: Install Chrome with 'plotly_get_chrome' or use HTML-only reports"
                    )
                else:
                    logger.error(f"PNG generation failed for {final_filename}: {png_error}")
                return None
        else:
            logger.debug(f"Saving {final_filename} as full-page interactive HTML...")
            output_file = output_dir / f"{final_filename}.html"

            # Configure figure for full-screen responsive behavior
            fig.update_layout(
                autosize=True,
                width=None,  # Remove any fixed width
                height=None,  # Remove any fixed height
            )
            fig.write_html(output_file)
            logger.debug(f"{final_filename} HTML saved successfully")
            return str(output_file)

    except Exception as e:
        final_filename = (
            f"report_{report_number:02d}_{filename}" if report_number is not None else filename
        )
        logger.error(f"Failed to save figure {final_filename}: {e}")
        return None


def figure_to_base64(
    fig, width: int = 1200, height: int = 650, plot_name: str = "plot"
) -> str | None:
    """
    Convert a plotly figure to base64-encoded PNG for embedding in HTML.

    Args:
        fig: Plotly figure object
        width: Image width in pixels
        height: Image height in pixels
        plot_name: Name of the plot for logging

    Returns:
        Base64-encoded image string with data URI prefix, or None if failed
    """
    try:
        logger.info(f"Converting {plot_name} to high-quality PNG ({width}x{height})...")

        # Convert figure to PNG bytes
        img_bytes = fig.to_image(format="png", width=width, height=height)

        logger.info(f"Encoding {plot_name} as base64 for HTML embedding...")
        # Encode as base64
        img_base64 = base64.b64encode(img_bytes).decode()

        logger.info(f"{plot_name} image ready ({len(img_base64) // 1024}KB)")
        return f"data:image/png;base64,{img_base64}"

    except Exception as e:
        logger.error(f"Failed to convert {plot_name} to base64: {e}")
        return None


def create_report_filename(
    base_name: str,
    report_number: int | None = None,
    report_title: str | None = None,
    extension: str = "html",
) -> str:
    """
    Create a standardized filename for Caliper reports.

    Args:
        base_name: Base filename (e.g., "performance_analysis")
        report_number: Optional report number (e.g., 0 for "Report 00:")
        report_title: Optional human-readable title for the report
        extension: File extension (without dot)

    Returns:
        Formatted filename following Caliper conventions

    Examples:
        >>> create_report_filename("performance_analysis", 0, "GuideLLM Performance Analysis")
        "report_00_guidellm_performance_analysis.html"

        >>> create_report_filename("summary", 5, "Baseline Comparisons")
        "report_05_baseline_comparisons.html"

        >>> create_report_filename("analysis")  # No numbering
        "analysis.html"
    """
    if report_number is not None:
        # Use report title if provided, otherwise base name
        if report_title:
            # Convert title to filename-safe format
            safe_title = report_title.lower().replace(" ", "_").replace(":", "").replace("-", "_")
            filename = f"report_{report_number:02d}_{safe_title}"
        else:
            filename = f"report_{report_number:02d}_{base_name}"
    else:
        filename = base_name

    return f"{filename}.{extension}"


def create_report_title_display(base_title: str, report_number: int | None = None) -> str:
    """
    Create a standardized display title for Caliper reports.

    Args:
        base_title: Base title (e.g., "GuideLLM Performance Analysis")
        report_number: Optional report number

    Returns:
        Formatted display title

    Examples:
        >>> create_report_title_display("GuideLLM Performance Analysis", 0)
        "Report 00: GuideLLM Performance Analysis"

        >>> create_report_title_display("Baseline Comparisons", 1)
        "Report 01: Baseline Comparisons"

        >>> create_report_title_display("Summary")  # No numbering
        "Summary"
    """
    if report_number is not None:
        return f"Report {report_number:02d}: {base_title}"
    else:
        return base_title


def write_full_page_html(fig, output_path: str, title: str) -> bool:
    """
    Write a plotly figure as a full-page HTML file.

    Args:
        fig: Plotly figure object
        output_path: Full path to the output HTML file
        title: Title for the HTML document (currently unused but kept for compatibility)

    Returns:
        True if successful, False otherwise
    """
    try:
        output_file = Path(output_path)
        output_dir = output_file.parent
        filename = output_file.stem  # Get filename without extension

        # Use save_figure to write HTML
        result = save_figure(fig, output_dir, filename, as_image=False)
        return result is not None
    except Exception as e:
        logger.error(f"Failed to write full-page HTML to {output_path}: {e}")
        return False
