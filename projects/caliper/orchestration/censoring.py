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


def discover_vault_secrets(
    verbose: bool = False,
) -> tuple[set[str], dict[str, str], dict[str, str]]:
    """
    Discover all vault secrets for censoring (all vault content is treated as sensitive).

    Args:
        verbose: Enable verbose logging

    Returns:
        Tuple of (secret_strings, secret_mapping, censor_text_mapping) where:
        - secret_strings: Set of secret strings loaded from vaults
        - secret_mapping: Dict mapping secret string -> vault/content identifier
        - censor_text_mapping: Dict mapping secret string -> censor_text replacement (if available)
    """
    vault_secrets = set()
    secret_mapping = {}
    censor_text_mapping = {}

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

                # If censor_text is available, use it directly instead of reading file
                if content_def.censor_text:
                    content_path = vault_manager.get_vault_content_path(vault_name, content_name)
                    if not (content_path and content_path.exists()):
                        logger.warning(
                            f"Invalid vault found: {vault_name} {content_name} (missing)"
                        )
                        continue

                    try:
                        # Read actual vault content
                        content_text = content_path.read_text(
                            encoding="utf-8", errors="ignore"
                        ).strip()
                        if not content_text:
                            logger.warning(
                                f"Invalid vault found: {vault_name} {content_name} (empty)"
                            )
                        else:
                            vault_secrets.add(content_text)
                            secret_mapping[content_text] = f"{vault_name}/{content_name}"
                            censor_text_mapping[content_text] = content_def.censor_text
                            secrets_discovered += 1
                            if verbose:
                                logger.info(
                                    f"Discovered secret from vault {vault_name}/{content_name} with censor_text"
                                )
                    except Exception as e:
                        logger.warning(
                            f"Failed to read vault content {vault_name}/{content_name}: {e}"
                        )
                else:
                    # No censor_text, read file content for traditional censoring
                    content_path = vault_manager.get_vault_content_path(vault_name, content_name)
                    if not (content_path and content_path.exists()):
                        logger.warning(
                            f"Invalid vault found: {vault_name} {content_name} (missing)"
                        )
                        continue

                    try:
                        # Read vault content (assume it's text)
                        content_text = content_path.read_text(
                            encoding="utf-8", errors="ignore"
                        ).strip()
                        if not content_text:
                            logger.warning(
                                f"Invalid vault found: {vault_name} {content_name} (empty)"
                            )
                        else:
                            vault_secrets.add(content_text)
                            secret_mapping[content_text] = f"{vault_name}/{content_name}"
                            secrets_discovered += 1
                            if verbose:
                                logger.info(
                                    f"Discovered secret from vault {vault_name}/{content_name}"
                                )
                    except Exception as e:
                        logger.warning(
                            f"Failed to read vault content {vault_name}/{content_name}: {e}"
                        )

        if verbose:
            logger.info(f"Discovered {secrets_discovered} vault secrets for censoring")

    except Exception as e:
        logger.exception(f"Failed to discover vault secrets: {e}")
        raise

    return vault_secrets, secret_mapping, censor_text_mapping


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
        vault_secrets, secret_mapping, censor_text_mapping = discover_vault_secrets(verbose=verbose)
        # /!\ secret_mapping contains the secret values. Process with extra care.

        # Replace vault secrets (more specific)
        for secret in vault_secrets:
            if secret and secret.strip() and secret.strip() in censored_text:
                # Use censor_text if available, otherwise use [REDACTED-VAULT]
                replacement = censor_text_mapping.get(secret.strip(), "[REDACTED-VAULT]")
                censored_text = censored_text.replace(secret.strip(), replacement)

                # Only count as replacement and report if not using censor_text (unexpected vault content)
                if secret.strip() not in censor_text_mapping:
                    replacements_made += 1
                    if verbose:
                        vault_identifier = secret_mapping.get(secret.strip(), "unknown vault")
                        logger.info(f"Censored vault secret from {vault_identifier} in text")
                elif verbose:
                    vault_identifier = secret_mapping.get(secret.strip(), "unknown vault")
                    logger.info(f"Applied censor_text replacement for {vault_identifier} in text")

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
        True if unexpected censoring issues occurred (excluding safe censor_text), False otherwise
    """
    if disable_censoring:
        if export_cfg.verbose:
            logger.info("Censoring disabled via --disable-censoring flag")
        return False

    # Discover vault secrets
    vault_secrets, secret_mapping, censor_text_mapping = discover_vault_secrets(
        verbose=export_cfg.verbose
    )
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
        censor_text_mapping=censor_text_mapping,
    )

    # Separate results by type
    clean_files = [r for r in censoring_results if not r.censored]
    sanitized_files = [r for r in censoring_results if r.sanitized]
    excluded_files = [r for r in censoring_results if r.censored and not r.sanitized]

    logger.info(
        f"Censoring complete: {len(clean_files)} clean, {len(sanitized_files)} sanitized, {len(excluded_files)} excluded"
    )

    _generate_censoring_report(censoring_results, from_path)

    # Generate notification for excluded files but allow export to proceed
    if excluded_files:
        # Create notification about censoring activity
        _create_censoring_notification(censoring_results, from_path, export_blocked=False)

        excluded_file_names = [str(r.file_path.relative_to(from_path)) for r in excluded_files]
        logger.warning(
            f"Export proceeding despite {len(excluded_files)} files with sensitive content that could not be sanitized: {excluded_file_names[:5]}{'...' if len(excluded_file_names) > 5 else ''}"
        )
        # Note: Export continues despite excluded files - notifications will alert about issues

    # Generate censoring report and notification if any files were processed
    if sanitized_files or excluded_files:
        # Only create notification for unexpected censoring, not safe censor_text replacements
        # (excluded files already got their notification above)
        if sanitized_files:
            unexpected_sanitized_files = [
                r for r in censoring_results if r.sanitized and not r.safely_redacted
            ]

            if unexpected_sanitized_files:
                # Create notification about unexpected censoring activity
                _create_censoring_notification(
                    unexpected_sanitized_files, from_path, export_blocked=False
                )
    else:
        if export_cfg.verbose:
            logger.info("No files required censoring")
        return False  # No censoring occurred

    if export_cfg.verbose and (sanitized_files or excluded_files):
        # Calculate counts for verbose logging
        unexpected_sanitized_count = len(
            [r for r in censoring_results if r.sanitized and not r.safely_redacted]
        )
        safe_files = len(sanitized_files) - unexpected_sanitized_count

        log_parts = []
        if unexpected_sanitized_count > 0:
            log_parts.append(
                f"{unexpected_sanitized_count} files with unexpected sensitive content sanitized"
            )
        if safe_files > 0:
            log_parts.append(f"{safe_files} files with expected vault content replaced")
        if excluded_files:
            log_parts.append(f"{len(excluded_files)} files excluded due to sensitive filenames")

        if log_parts:
            logger.info(f"Export proceeding: {', '.join(log_parts)}")
        else:
            logger.info("Export proceeding")

    # Only return True (censoring occurred) if there were unexpected issues
    # Safe censor_text replacements should not trigger exit code 1
    has_unexpected_issues = (
        len([r for r in censoring_results if r.sanitized and not r.safely_redacted]) > 0
        or len(excluded_files) > 0
    )
    return has_unexpected_issues


def _generate_censoring_report(censoring_results, from_path: Path) -> None:
    """Generate a YAML report of censored files in ARTIFACT_DIR."""
    try:
        from datetime import datetime

        # Separate safe vs unexpected censoring
        safe_censoring_by_reason = {}
        unexpected_censoring_by_reason = {}

        for r in censoring_results:
            if r.censored:
                reason = r.reason
                file_path = (
                    str(r.file_path.relative_to(from_path))
                    if r.file_path.is_relative_to(from_path)
                    else str(r.file_path)
                )

                # Use the safely_redacted flag to categorize
                if r.safely_redacted:
                    # Safe censoring - expected vault content with censor_text
                    if reason not in safe_censoring_by_reason:
                        safe_censoring_by_reason[reason] = []
                    safe_censoring_by_reason[reason].append(file_path)
                else:
                    # Unexpected censoring - unexpected sensitive content
                    if reason not in unexpected_censoring_by_reason:
                        unexpected_censoring_by_reason[reason] = []
                    unexpected_censoring_by_reason[reason].append(file_path)

        # Count different types of censoring
        safe_censored_files = sum(len(files) for files in safe_censoring_by_reason.values())
        unexpected_censored_files = sum(
            len(files) for files in unexpected_censoring_by_reason.values()
        )
        total_censored_files = safe_censored_files + unexpected_censored_files

        # Create report data
        report_data = {
            "timestamp": datetime.now().isoformat(),
            "source_directory": str(from_path),
            "total_files": len(censoring_results),
            "clean_files": len([r for r in censoring_results if not r.censored]),
            "censored_files": total_censored_files,
            "safe_censored_files": safe_censored_files,
            "unexpected_censored_files": unexpected_censored_files,
            "safe_censoring_by_reason": safe_censoring_by_reason,
            "unexpected_censoring_by_reason": unexpected_censoring_by_reason,
            # Keep old format for compatibility
            "censored_by_reason": {**safe_censoring_by_reason, **unexpected_censoring_by_reason},
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
        # censoring_results now contains only unexpected/problematic censoring results
        # (safe censor_text replacements are excluded)
        sanitized_files = [r.file_path for r in censoring_results if r.sanitized]
        excluded_files = [r.file_path for r in censoring_results if r.censored and not r.sanitized]

        if not sanitized_files and not excluded_files:
            return  # No unexpected censoring occurred

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
                # Analyze what types of censoring occurred using the safely_redacted flag
                sanitized_results = [r for r in censoring_results if r.sanitized]

                # Categorize types of censoring that occurred
                has_sanitized_replacements = any(r.safely_redacted for r in sanitized_results)
                has_placeholder_replacements = any(not r.safely_redacted for r in sanitized_results)

                message_parts.append(
                    f"""
{len(sanitized_files)} file(s) had sensitive content sanitized and included in export:

{format_file_list(sanitized_files)}"""
                )

                # Generate appropriate description based on censoring types
                if has_sanitized_replacements and has_placeholder_replacements:
                    message_parts.append(
                        "Some sensitive content was replaced with sanitized values, and other sensitive content (passwords, API keys, tokens) was replaced with placeholder text."
                    )
                elif has_sanitized_replacements and not has_placeholder_replacements:
                    message_parts.append("Sensitive content was replaced with sanitized values.")
                else:
                    message_parts.append(
                        "Sensitive content (passwords, API keys, tokens) was replaced with placeholder text."
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
