"""
Artifact censoring module for Caliper.

This module provides censoring capabilities to filter artifacts before upload:
1. Keyword-based censoring - filters files containing specified keywords/patterns
2. Filename-based censoring - filters files with sensitive filename patterns
"""

from __future__ import annotations

import logging
import stat
from dataclasses import dataclass
from pathlib import Path

from .censoring_rules import (
    COMPILED_KEYWORD_PATTERNS,
    matches_sensitive_filename,
)

logger = logging.getLogger(__name__)


@dataclass
class CensoringResult:
    """Result of censoring operation on a single file."""

    file_path: Path
    censored: bool
    reason: str
    sanitized: bool = False  # True if content was sanitized in-place

    def __str__(self):
        if self.sanitized:
            return f"SANITIZED: {self.file_path} ({self.reason})"
        elif self.censored:
            return f"EXCLUDED: {self.file_path} ({self.reason})"
        else:
            return f"ALLOWED: {self.file_path} ({self.reason})"


class ArtifactCensor:
    """Main censoring class that applies keyword and secret filtering."""

    def __init__(
        self,
        vault_secrets: set[str] | None = None,
        secret_mapping: dict[str, str] | None = None,
        verbose: bool = False,
        dry_run: bool = False,
    ):
        """
        Initialize the artifact censor.

        Args:
            vault_secrets: Set of secret strings loaded from vaults
            secret_mapping: Dict mapping secret strings to vault/content identifiers
            verbose: Enable verbose logging
            dry_run: Skip file modifications while preserving analysis
        """
        self.vault_secrets = vault_secrets or set()
        self.secret_mapping = secret_mapping or {}
        self.verbose = verbose
        self.dry_run = dry_run

        if self.verbose:
            logger.info(f"Initialized censoring with {len(self.vault_secrets)} vault secrets")

    def _is_text_file(self, file_path: Path) -> bool:
        """Check if file is likely a text file based on extension and content sample."""
        # Common text file extensions
        text_extensions = {
            ".txt",
            ".log",
            ".yaml",
            ".yml",
            ".json",
            ".xml",
            ".csv",
            ".md",
            ".rst",
            ".py",
            ".js",
            ".html",
            ".css",
            ".sql",
            ".sh",
            ".bash",
            ".conf",
            ".cfg",
            ".ini",
            ".properties",
            ".out",
            ".err",
        }

        if file_path.suffix.lower() in text_extensions:
            return True

        # For files without clear extension, try to detect if it's text
        try:
            # Read only first 1KB to avoid loading large files into memory
            with open(file_path, "rb") as f:
                sample = f.read(1024)
            if not sample:
                return True  # Empty file is text

            # Check if most bytes are printable ASCII or common UTF-8
            printable_ratio = sum(1 for b in sample if 32 <= b <= 126 or b in (9, 10, 13)) / len(
                sample
            )
            return printable_ratio > 0.7

        except Exception:
            return False  # If we can't read it, assume it's binary

    def _sanitize_file_content(self, file_path: Path) -> CensoringResult:
        """Sanitize file content by replacing sensitive patterns."""
        try:
            # First check if filename itself is sensitive - sanitize these files
            if matches_sensitive_filename(str(file_path)):
                # Replace content with censoring message
                censored_content = (
                    f"Content censored by caliper - sensitive filename: {file_path.name}\n"
                )

                # Write sanitized content back to file (skip if dry run)
                if not self.dry_run:
                    file_path.write_text(censored_content, encoding="utf-8")

                return CensoringResult(
                    file_path, True, f"sensitive filename pattern: {file_path.name}", sanitized=True
                )

            # Skip non-text files to avoid reading binary content
            if not self._is_text_file(file_path):
                return CensoringResult(file_path, False, "non-text file")

            content = file_path.read_text(encoding="utf-8", errors="ignore")
            sanitized = False
            reasons = []

            # Import KEYWORD_PATTERNS for reporting
            from .censoring_rules import KEYWORD_PATTERNS

            # Check for keyword patterns - redact matched spans in place
            keyword_detected = False
            matched_patterns = set()
            for i, pattern in enumerate(COMPILED_KEYWORD_PATTERNS):
                matches = list(pattern.finditer(content))
                if matches:
                    keyword_detected = True
                    matched_patterns.add(i)

            if keyword_detected:
                # Replace all matched spans with redacted text, preserving other content
                # Process patterns in reverse order by position to maintain string indices
                all_matches = []
                for i, pattern in enumerate(COMPILED_KEYWORD_PATTERNS):
                    for match in pattern.finditer(content):
                        all_matches.append((match.start(), match.end(), i))

                # Sort by start position in reverse order
                all_matches.sort(reverse=True)

                # Replace each match with [REDACTED]
                for start, end, _pattern_idx in all_matches:
                    content = content[:start] + "[REDACTED]" + content[end:]

                # Add reasons for all matched patterns
                for pattern_idx in sorted(matched_patterns):
                    reasons.append(f"contains keyword pattern: {KEYWORD_PATTERNS[pattern_idx]}")

                sanitized = True

            # Always check for vault secrets, independent of keyword pattern detection
            for secret in self.vault_secrets:
                if secret and secret.strip() and secret.strip() in content:
                    content = content.replace(secret.strip(), "*******")
                    sanitized = True
                    vault_identifier = self.secret_mapping.get(secret.strip(), "unknown vault")
                    reasons.append(f"contains vault secret: {vault_identifier}")

            if sanitized:
                # Write sanitized content back to original file (skip if dry run)
                if not self.dry_run:
                    try:
                        file_path.write_text(content, encoding="utf-8")
                    except PermissionError:
                        # Handle read-only files by making them writable
                        try:
                            # Make file writable
                            file_path.chmod(file_path.stat().st_mode | stat.S_IWUSR)
                            file_path.write_text(content, encoding="utf-8")
                        except Exception:
                            # If we still can't write, abort this file export
                            raise

                reason = reasons[0] if reasons else "sensitive content detected"
                return CensoringResult(file_path, True, reason, sanitized=True)

            return CensoringResult(file_path, False, "content check passed")

        except Exception as e:
            logger.warning(f"Error sanitizing file {file_path}: {e}")
            return CensoringResult(file_path, True, f"sanitization failed: {e}")

    def censor_files(self, file_paths: list[Path]) -> tuple[list[Path], list[CensoringResult]]:
        """
        Apply censoring to a list of file paths, sanitizing content where possible.

        Args:
            file_paths: List of file paths to check

        Returns:
            tuple: (processed_files, censoring_results)
                processed_files contains original paths for clean files,
                sanitized paths for files with content replacements,
                and excludes files with sensitive filenames
        """
        processed_files = []
        results = []

        for file_path in file_paths:
            if not file_path.is_file():
                # Skip directories and non-existent files
                processed_files.append(file_path)
                continue

            result = self._sanitize_file_content(file_path)
            results.append(result)

            if not result.censored:
                # Clean file, include original
                processed_files.append(file_path)
            elif result.sanitized:
                # File had content sanitized in-place, include original path
                processed_files.append(file_path)
                if self.verbose:
                    logger.info(f"Sanitized: {result}")
            else:
                # File excluded entirely (e.g., sensitive filename)
                if self.verbose:
                    logger.info(f"Excluded: {result}")

        return processed_files, results


def apply_censoring_to_artifacts(
    artifact_paths: list[Path],
    censoring_enabled: bool = True,
    verbose: bool = False,
    vault_secrets: set[str] | None = None,
    secret_mapping: dict[str, str] | None = None,
    dry_run: bool = False,
) -> tuple[list[Path], list[CensoringResult]]:
    """
    Apply censoring to artifact paths, sanitizing sensitive content in-place.

    This function processes artifacts by:
    - Replacing sensitive content patterns with "Content censored by caliper" in-place
    - Replacing vault secrets with "*******" in-place
    - Excluding files with sensitive filename patterns (.pem, .key, files with "secret" in name, etc.)

    Args:
        artifact_paths: List of artifact file paths
        censoring_enabled: Whether to apply censoring
        verbose: Enable verbose logging
        vault_secrets: Set of vault secret strings to censor
        secret_mapping: Dict mapping secret strings to vault/content identifiers
        dry_run: Skip file modifications while preserving analysis

    Returns:
        tuple: (processed_paths, censoring_results)
            processed_paths contains original paths for clean and sanitized files,
            and excludes files with sensitive filenames
    """
    if not censoring_enabled:
        if verbose:
            logger.info("Censoring disabled, allowing all artifacts")
        return artifact_paths, []

    # Apply censoring using keyword and filename patterns
    censor = ArtifactCensor(
        vault_secrets=vault_secrets or set(),
        secret_mapping=secret_mapping or {},
        verbose=verbose,
        dry_run=dry_run,
    )
    processed_paths, results = censor.censor_files(artifact_paths)

    # Log summary
    sanitized_count = len([r for r in results if r.sanitized])
    excluded_count = len([r for r in results if r.censored and not r.sanitized])
    clean_count = len([r for r in results if not r.censored])

    if verbose or sanitized_count > 0 or excluded_count > 0:
        logger.info(
            f"Censoring complete: {clean_count} clean, {sanitized_count} sanitized, {excluded_count} excluded"
        )

    return processed_paths, results
