"""
Orchestration-level censoring functionality for Caliper artifact export.

This module provides high-level censoring operations that integrate with the export
orchestration system, including reporting and notification generation.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from projects.caliper.engine.file_export.censoring import apply_censoring_to_artifacts
from projects.caliper.orchestration.export_config import CaliperOrchestrationExportConfig
from projects.core.library import ci as ci_lib
from projects.core.library import env
from projects.core.library import vault as vault_lib

logger = logging.getLogger(__name__)


def discover_vault_secrets(verbose: bool = False) -> tuple[set[str], dict[str, str]]:
    """
    Discover all vault secrets for censoring (all vault content is treated as sensitive).

    Args:
        verbose: Enable verbose logging

    Returns:
        Tuple of (secret_strings, secret_mapping) where:
        - secret_strings: Set of secret strings loaded from vaults
        - secret_mapping: Dict mapping secret string -> vault/content identifier
    """
    vault_secrets = set()
    secret_mapping = {}

    try:
        vault_manager = vault_lib.get_vault_manager()
        available_vaults = vault_manager.list_vaults()

        if verbose:
            logger.info(f"Discovering vault secrets from {len(available_vaults)} vaults")

        secrets_discovered = 0
        for vault_name in available_vaults:
            vault = vault_manager.get_vault(vault_name)
            if not vault:
                continue

            for content_name, content_def in vault.content.items():
                # Only process content marked as sensible
                if not content_def.is_sensible:
                    if verbose:
                        logger.info(
                            f"Skipping non-sensible vault content {vault_name}/{content_name}"
                        )
                    continue

                content_path = vault_manager.get_vault_content_path(vault_name, content_name)
                if not (content_path and content_path.exists()):
                    logger.warning(f"Invalid vault found: {vault_name} {content_name} (missing)")
                    continue

                try:
                    # Read vault content (assume it's text)
                    content_text = content_path.read_text(encoding="utf-8", errors="ignore").strip()
                    if not content_text:
                        logger.warning(f"Invalid vault found: {vault_name} {content_name} (empty)")
                    else:
                        vault_secrets.add(content_text)
                        secret_mapping[content_text] = f"{vault_name}/{content_name}"
                        secrets_discovered += 1
                        if verbose:
                            logger.info(f"Discovered secret from vault {vault_name}/{content_name}")
                except Exception as e:
                    logger.warning(f"Failed to read vault content {vault_name}/{content_name}: {e}")

        if verbose:
            logger.info(f"Discovered {secrets_discovered} vault secrets for censoring")

    except Exception as e:
        logger.exception(f"Failed to discover vault secrets: {e}")
        raise

    return vault_secrets, secret_mapping


def censor_text(text: str, verbose: bool = False) -> str:
    """
    Censor sensitive content in text using caliper's censoring rules.

    Args:
        text: The text to censor
        verbose: Enable verbose logging

    Returns:
        Censored text with sensitive content replaced
    """
    if not text:
        return text

    censored_text = text
    replacements_made = 0

    # Import keyword patterns for content censoring (always try this first)
    from projects.caliper.engine.file_export.censoring_rules import (
        COMPILED_KEYWORD_PATTERNS,
        KEYWORD_PATTERNS,
    )

    # Apply keyword pattern censoring first (preserve this even if vault discovery fails)
    for i, pattern in enumerate(COMPILED_KEYWORD_PATTERNS):
        matches = list(pattern.finditer(censored_text))
        if matches:
            # Replace all matched spans with redacted text, preserving other content
            # Process patterns in reverse order by position to maintain string indices
            for match in reversed(matches):
                censored_text = (
                    censored_text[: match.start()] + "[REDACTED]" + censored_text[match.end() :]
                )
                replacements_made += 1

            if verbose:
                logger.info(
                    f"Censored {len(matches)} instances of pattern '{KEYWORD_PATTERNS[i]}' in text"
                )

    # Now try vault secrets discovery and censoring
    try:
        # Discover vault secrets for censoring
        vault_secrets, secret_mapping = discover_vault_secrets(verbose=verbose)
        # /!\ secret_mapping contains the secret values. Process with extra care.

        # Replace vault secrets (more specific)
        for secret in vault_secrets:
            if secret and secret.strip() and secret.strip() in censored_text:
                censored_text = censored_text.replace(secret.strip(), "[REDACTED-VAULT]")
                replacements_made += 1
                if verbose:
                    vault_identifier = secret_mapping.get(secret.strip(), "unknown vault")
                    logger.info(f"Censored vault secret from {vault_identifier} in text")

        if replacements_made > 0:
            logger.info(f"Censored {replacements_made} sensitive items from text")
        elif verbose:
            logger.info("No sensitive content found in text")

    except Exception as e:
        logger.error(f"Failed to discover vault secrets during text censoring: {e}")
        # Propagate the vault discovery failure after keyword censoring is complete
        # This allows _censor_notification_text to handle the failure appropriately
        raise

    return censored_text


def orchestration_apply_censoring(
    from_path: Path,
    export_cfg: CaliperOrchestrationExportConfig,
    disable_censoring: bool = False,
) -> bool:
    """
    Apply censoring for single-run orchestration export.

    Args:
        from_path: Source directory containing artifacts
        export_cfg: Export configuration
        disable_censoring: Whether to skip censoring

    Returns:
        True if any files were censored (sanitized or excluded), False otherwise
    """
    if disable_censoring:
        if export_cfg.verbose:
            logger.info("Censoring disabled via --disable-censoring flag")
        return False

    # Discover vault secrets
    vault_secrets, secret_mapping = discover_vault_secrets(verbose=export_cfg.verbose)
    # /!\ secret_mapping contains the secret values. Process with extra care.

    # Collect all artifact files
    all_artifact_paths = [p for p in from_path.rglob("*") if p.is_file()]

    logger.info(
        f"Starting artifact censoring: {len(all_artifact_paths)} files to scan from {from_path}"
    )

    # Apply in-place censoring
    processed_paths, censoring_results = apply_censoring_to_artifacts(
        all_artifact_paths,
        censoring_enabled=True,
        verbose=export_cfg.verbose,
        vault_secrets=vault_secrets,
        secret_mapping=secret_mapping,
    )

    # Separate results by type
    clean_files = [r for r in censoring_results if not r.censored]
    sanitized_files = [r for r in censoring_results if r.sanitized]
    excluded_files = [r for r in censoring_results if r.censored and not r.sanitized]

    logger.info(
        f"Censoring complete: {len(clean_files)} clean, {len(sanitized_files)} sanitized, {len(excluded_files)} excluded"
    )

    _generate_censoring_report(censoring_results, from_path)

    # Abort export if any files were excluded (fail closed)
    if excluded_files:
        # Create notification about censoring activity blocking export
        _create_censoring_notification(censoring_results, from_path, export_blocked=True)

        excluded_file_names = [str(r.file_path.relative_to(from_path)) for r in excluded_files]
        logger.error(
            f"Export aborted: {len(excluded_files)} files contain sensitive content that could not be sanitized: {excluded_file_names[:5]}{'...' if len(excluded_file_names) > 5 else ''}"
        )
        raise RuntimeError(
            f"Export blocked due to {len(excluded_files)} unsanitizable sensitive files"
        )

    # Generate censoring report and notification if any files were processed
    if sanitized_files:
        # Create notification about censoring activity (export proceeds with sanitized files)
        _create_censoring_notification(censoring_results, from_path, export_blocked=False)

        if export_cfg.verbose:
            logger.info(
                f"Export proceeding with {len(sanitized_files)} files sanitized for sensitive content"
            )
        return True  # Censoring occurred

    elif export_cfg.verbose:
        logger.info("No files required censoring")

    return False  # No censoring occurred


def _generate_censoring_report(censoring_results, from_path: Path) -> None:
    """Generate a YAML report of censored files in ARTIFACT_DIR."""
    try:
        from datetime import datetime

        # Group censored files by reason
        censored_by_reason = {}
        for r in censoring_results:
            if r.censored:
                reason = r.reason
                if reason not in censored_by_reason:
                    censored_by_reason[reason] = []

                file_path = (
                    str(r.file_path.relative_to(from_path))
                    if r.file_path.is_relative_to(from_path)
                    else str(r.file_path)
                )
                censored_by_reason[reason].append(file_path)

        # Create report data
        report_data = {
            "timestamp": datetime.now().isoformat(),
            "source_directory": str(from_path),
            "total_files": len(censoring_results),
            "censored_files": len([r for r in censoring_results if r.censored]),
            "clean_files": len([r for r in censoring_results if not r.censored]),
            "censored_by_reason": censored_by_reason,
        }

        # Write report to ARTIFACT_DIR
        if env.ARTIFACT_DIR:
            report_path = env.ARTIFACT_DIR / "censoring_report.yaml"
            report_path.parent.mkdir(parents=True, exist_ok=True)

            with open(report_path, "w") as f:
                yaml.dump(report_data, f, indent=2, default_flow_style=False)

            logger.info(f"Censoring report written to {report_path}")
        else:
            logger.warning("ARTIFACT_DIR not set, cannot write censoring report")

    except Exception as e:
        logger.error(f"Failed to generate censoring report: {e}")


def _create_censoring_notification(
    censoring_results, from_path: Path, export_blocked: bool = False
) -> None:
    """Create a notification file about censoring activity."""
    try:
        # Separate results
        sanitized_files = [r.file_path for r in censoring_results if r.sanitized]
        excluded_files = [r.file_path for r in censoring_results if r.censored and not r.sanitized]

        if not sanitized_files and not excluded_files:
            return  # No censoring occurred

        # Prepare file lists
        def format_file_list(files, limit=10):
            file_list = "\n".join(
                [
                    f"  - {file.relative_to(from_path) if file.is_relative_to(from_path) else file}"
                    for file in files[:limit]
                ]
            )
            if len(files) > limit:
                file_list += f"\n  ... and {len(files) - limit} more files"
            return file_list

        if export_blocked and excluded_files:
            # Export blocked due to excluded files
            message = f"""Caliper Export Blocked: Sensitive Files Cannot Be Sanitized

{len(excluded_files)} file(s) with sensitive filenames were excluded from export:

{format_file_list(excluded_files)}

These files have filenames that indicate sensitive content and cannot be safely sanitized:
- Certificate files (.pem, .key, .p12, .pfx)
- Files with 'secret', 'credential', or 'password' in their names
- SSH keys and configuration files

Please review these artifacts and rename or relocate sensitive files before re-running the export.

For details, see: $ARTIFACT_DIR/censoring_report.yaml"""

            notification_name = "censoring_blocked"

        else:
            # Export proceeded with sanitization
            total_censored = len(sanitized_files) + len(excluded_files)
            message_parts = [
                f"Caliper Export: {total_censored} file(s) contained sensitive content"
            ]

            if sanitized_files:
                message_parts.append(
                    f"""
{len(sanitized_files)} file(s) had sensitive content sanitized and included in export:

{format_file_list(sanitized_files)}

Sensitive content (passwords, API keys, tokens) was replaced with placeholder text."""
                )

            if excluded_files:
                message_parts.append(
                    f"""
{len(excluded_files)} file(s) with sensitive filenames were excluded:

{format_file_list(excluded_files)}

These files cannot be safely sanitized due to their filenames."""
                )

            message_parts.append("\nFor details, see: $ARTIFACT_DIR/censoring_report.yaml")
            message = "\n".join(message_parts) + "\n"
            notification_name = "censoring_applied"

        # Create notification file using CI library
        notification_file = ci_lib.add_notification_file(name=notification_name, message=message)

        if notification_file:
            logger.info(f"Censoring notification created: {notification_file}")
        else:
            logger.error("Failed to create censoring notification file")

    except Exception as e:
        logger.error(f"Failed to create censoring notification: {e}")
