"""
Tests for the caliper artifact censoring module.

Covers:
- Keyword pattern matching and redaction
- Filename-based sensitive file detection
- Content sanitization with in-place replacement
- Overlapping regex span merging
- Dry-run mode behavior
- Separation of censored vs sanitized files
- File exclusion vs sanitization classification
- End-to-end censoring with temporary directories
- Bearer token pattern deduplication
- Vault secret replacement
"""

from __future__ import annotations

from pathlib import Path

from projects.caliper.engine.file_export.censoring import (
    ArtifactCensor,
    CensoringResult,
    _merge_overlapping_spans,
    apply_censoring_to_artifacts,
)
from projects.caliper.engine.file_export.censoring_rules import (
    COMPILED_KEYWORD_PATTERNS,
    KEYWORD_PATTERNS,
    matches_sensitive_filename,
)

# ---------------------------------------------------------------------------
# _merge_overlapping_spans
# ---------------------------------------------------------------------------


class TestMergeOverlappingSpans:
    def test_empty_list(self):
        assert _merge_overlapping_spans([]) == []

    def test_single_span(self):
        assert _merge_overlapping_spans([(0, 5)]) == [(0, 5)]

    def test_non_overlapping_spans(self):
        spans = [(0, 5), (10, 15), (20, 25)]
        assert _merge_overlapping_spans(spans) == [(0, 5), (10, 15), (20, 25)]

    def test_overlapping_spans_are_merged(self):
        spans = [(0, 10), (5, 15)]
        assert _merge_overlapping_spans(spans) == [(0, 15)]

    def test_adjacent_spans_are_merged(self):
        spans = [(0, 5), (5, 10)]
        assert _merge_overlapping_spans(spans) == [(0, 10)]

    def test_nested_spans_are_merged(self):
        spans = [(0, 20), (5, 10)]
        assert _merge_overlapping_spans(spans) == [(0, 20)]

    def test_multiple_overlapping_groups(self):
        spans = [(0, 5), (3, 8), (10, 15), (12, 18)]
        assert _merge_overlapping_spans(spans) == [(0, 8), (10, 18)]

    def test_unsorted_input_is_handled(self):
        spans = [(10, 15), (0, 5), (3, 8)]
        assert _merge_overlapping_spans(spans) == [(0, 8), (10, 15)]

    def test_fully_contained_spans(self):
        spans = [(0, 100), (10, 20), (30, 40), (50, 60)]
        assert _merge_overlapping_spans(spans) == [(0, 100)]


# ---------------------------------------------------------------------------
# matches_sensitive_filename
# ---------------------------------------------------------------------------


class TestMatchesSensitiveFilename:
    def test_pem_file(self):
        assert matches_sensitive_filename("server.pem")

    def test_key_file(self):
        assert matches_sensitive_filename("private.key")

    def test_p12_file(self):
        assert matches_sensitive_filename("cert.p12")

    def test_pfx_file(self):
        assert matches_sensitive_filename("cert.pfx")

    def test_env_file(self):
        assert matches_sensitive_filename(".env")

    def test_env_with_suffix(self):
        assert matches_sensitive_filename(".env.production")

    def test_secret_in_name(self):
        assert matches_sensitive_filename("my_secret_config.yaml")

    def test_credential_in_name(self):
        assert matches_sensitive_filename("credential_store.json")

    def test_password_in_name(self):
        assert matches_sensitive_filename("password_file.txt")

    def test_ssh_key(self):
        assert matches_sensitive_filename("id_rsa")

    def test_ecdsa_key(self):
        assert matches_sensitive_filename("id_ecdsa")

    def test_ed25519_key(self):
        assert matches_sensitive_filename("id_ed25519")

    def test_normal_text_file(self):
        assert not matches_sensitive_filename("readme.txt")

    def test_normal_yaml_file(self):
        assert not matches_sensitive_filename("config.yaml")

    def test_normal_log_file(self):
        assert not matches_sensitive_filename("output.log")

    def test_path_with_secret_in_basename(self):
        assert matches_sensitive_filename("/some/path/secret_config.yaml")

    def test_case_insensitive_pem(self):
        assert matches_sensitive_filename("CERT.PEM")

    def test_case_insensitive_secret(self):
        assert matches_sensitive_filename("MY_SECRET.txt")


# ---------------------------------------------------------------------------
# Keyword pattern matching
# ---------------------------------------------------------------------------


class TestKeywordPatterns:
    """Test that compiled keyword patterns detect sensitive content."""

    def _matches_any_pattern(self, text: str) -> bool:
        return any(p.search(text) for p in COMPILED_KEYWORD_PATTERNS)

    def test_password_pattern(self):
        assert self._matches_any_pattern("password=hunter2")

    def test_password_colon(self):
        assert self._matches_any_pattern("password: my_password")

    def test_api_key_pattern(self):
        assert self._matches_any_pattern("api_key=abc123xyz")

    def test_api_secret_pattern(self):
        assert self._matches_any_pattern("api-secret=supersecret")

    def test_token_pattern(self):
        # Test specific token patterns that are enabled (general token= is disabled)
        assert self._matches_any_pattern("api_token=eyJhbGciOiJIUzI1NiJ9")

    def test_bearer_token(self):
        assert self._matches_any_pattern("Bearer eyJhbGciOiJIUzI1NiJ9")

    def test_bearer_case_insensitive(self):
        # Since patterns are compiled with IGNORECASE, lowercase should match too
        assert self._matches_any_pattern("bearer eyJhbGciOiJIUzI1NiJ9")

    def test_openai_key(self):
        assert self._matches_any_pattern("sk-abcdefghijklmnopqrstuvwxyz123456")

    def test_github_token(self):
        assert self._matches_any_pattern("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij")

    def test_aws_access_key(self):
        assert self._matches_any_pattern("AKIAIOSFODNN7EXAMPLE")

    def test_mongodb_uri(self):
        assert self._matches_any_pattern("mongodb://user:pass@host")

    def test_postgresql_uri(self):
        assert self._matches_any_pattern("postgresql://admin:secret@db.example.com")

    def test_clean_text(self):
        assert not self._matches_any_pattern("This is a normal log line with no secrets")

    def test_clean_json(self):
        assert not self._matches_any_pattern('{"name": "test", "value": 42}')

    def test_no_duplicate_bearer_pattern(self):
        # After removing the redundant lowercase bearer pattern,
        # verify the single pattern still works for both cases
        bearer_patterns = [p for p in KEYWORD_PATTERNS if "earer" in p]
        assert len(bearer_patterns) == 1, (
            f"Expected exactly 1 bearer pattern, found {len(bearer_patterns)}: {bearer_patterns}"
        )


# ---------------------------------------------------------------------------
# CensoringResult
# ---------------------------------------------------------------------------


class TestCensoringResult:
    def test_sanitized_str(self):
        r = CensoringResult(Path("test.txt"), censored=True, reason="keyword", sanitized=True)
        assert "SANITIZED" in str(r)

    def test_excluded_str(self):
        r = CensoringResult(Path("test.txt"), censored=True, reason="keyword", sanitized=False)
        assert "EXCLUDED" in str(r)

    def test_allowed_str(self):
        r = CensoringResult(Path("test.txt"), censored=False, reason="clean", sanitized=False)
        assert "ALLOWED" in str(r)


# ---------------------------------------------------------------------------
# ArtifactCensor._sanitize_file_content
# ---------------------------------------------------------------------------


class TestSanitizeFileContent:
    def test_clean_file_passes(self, tmp_path):
        f = tmp_path / "clean.txt"
        f.write_text("Nothing sensitive here\n", encoding="utf-8")

        censor = ArtifactCensor()
        result = censor._sanitize_file_content(f)

        assert not result.censored
        assert not result.sanitized

    def test_password_is_redacted(self, tmp_path):
        f = tmp_path / "config.txt"
        f.write_text("password=hunter2\nother_line\n", encoding="utf-8")

        censor = ArtifactCensor()
        result = censor._sanitize_file_content(f)

        assert result.censored
        assert result.sanitized
        content = f.read_text()
        assert "hunter2" not in content
        assert "[REDACTED]" in content
        # Other content should be preserved
        assert "other_line" in content

    def test_sensitive_filename_is_sanitized(self, tmp_path):
        f = tmp_path / "secret_config.yaml"
        f.write_text("important: data\n", encoding="utf-8")

        censor = ArtifactCensor()
        result = censor._sanitize_file_content(f)

        assert result.censored
        assert result.sanitized
        content = f.read_text()
        assert "Content censored by caliper" in content
        assert "important: data" not in content

    def test_vault_secret_is_replaced(self, tmp_path):
        f = tmp_path / "log.txt"
        f.write_text("connecting with supersecrettoken123\n", encoding="utf-8")

        censor = ArtifactCensor(
            vault_secrets={"supersecrettoken123"},
            secret_mapping={"supersecrettoken123": "vault/token"},
        )
        result = censor._sanitize_file_content(f)

        assert result.censored
        assert result.sanitized
        content = f.read_text()
        assert "supersecrettoken123" not in content
        assert "[REDACTED-VAULT]" in content

    def test_binary_file_skipped(self, tmp_path):
        f = tmp_path / "image.dat"
        f.write_bytes(bytes(range(256)))

        censor = ArtifactCensor()
        result = censor._sanitize_file_content(f)

        assert not result.censored
        assert not result.sanitized

    def test_dry_run_does_not_modify_file(self, tmp_path):
        original_content = "password=hunter2\n"
        f = tmp_path / "config.txt"
        f.write_text(original_content, encoding="utf-8")

        censor = ArtifactCensor(dry_run=True)
        result = censor._sanitize_file_content(f)

        assert result.censored
        assert result.sanitized
        # File should NOT be modified in dry run
        assert f.read_text() == original_content

    def test_overlapping_patterns_produce_correct_output(self, tmp_path):
        # token=xxx matches "token" pattern. "access_token=xxx" also matches.
        # These could overlap if the text has "access_token=myvalue"
        f = tmp_path / "config.txt"
        f.write_text("access_token=myvalue123\nother content\n", encoding="utf-8")

        censor = ArtifactCensor()
        result = censor._sanitize_file_content(f)

        assert result.sanitized
        content = f.read_text()
        # The redacted content should not be corrupted
        assert "[REDACTED]" in content
        assert "other content" in content
        # Original secret should be gone
        assert "myvalue123" not in content

    def test_multiple_secrets_on_same_line(self, tmp_path):
        f = tmp_path / "multi.txt"
        f.write_text("password=abc api_key=xyz\n", encoding="utf-8")

        censor = ArtifactCensor()
        result = censor._sanitize_file_content(f)

        assert result.sanitized
        content = f.read_text()
        assert "abc" not in content
        assert "xyz" not in content
        assert content.count("[REDACTED]") >= 2


# ---------------------------------------------------------------------------
# ArtifactCensor.censor_files
# ---------------------------------------------------------------------------


class TestCensorFiles:
    def test_separates_clean_sanitized_excluded(self, tmp_path):
        """Verify that clean, sanitized, and excluded files are handled correctly."""
        clean = tmp_path / "readme.txt"
        clean.write_text("Hello world\n", encoding="utf-8")

        sensitive_content = tmp_path / "config.yaml"
        sensitive_content.write_text("password=mysecret\n", encoding="utf-8")

        sensitive_name = tmp_path / "secret_data.yaml"
        sensitive_name.write_text("key: value\n", encoding="utf-8")

        censor = ArtifactCensor()
        processed, results = censor.censor_files([clean, sensitive_content, sensitive_name])

        # All three should be in processed (clean + 2 sanitized)
        assert len(processed) == 3
        assert clean in processed
        assert sensitive_content in processed
        assert sensitive_name in processed

        # Check result classifications
        clean_results = [r for r in results if not r.censored]
        sanitized_results = [r for r in results if r.sanitized]

        assert len(clean_results) == 1
        assert len(sanitized_results) == 2

    def test_nonexistent_file_is_passed_through(self, tmp_path):
        nonexistent = tmp_path / "does_not_exist.txt"
        censor = ArtifactCensor()
        processed, results = censor.censor_files([nonexistent])

        assert nonexistent in processed
        assert len(results) == 0

    def test_verbose_logging(self, tmp_path):
        f = tmp_path / "config.txt"
        f.write_text("password=test\n", encoding="utf-8")

        censor = ArtifactCensor(verbose=True)
        processed, results = censor.censor_files([f])

        assert len(processed) == 1
        assert results[0].sanitized


# ---------------------------------------------------------------------------
# apply_censoring_to_artifacts
# ---------------------------------------------------------------------------


class TestApplyCensoringToArtifacts:
    def test_censoring_disabled_returns_all(self, tmp_path):
        f = tmp_path / "secret.pem"
        f.write_text("private key data\n", encoding="utf-8")

        paths, results = apply_censoring_to_artifacts([f], censoring_enabled=False, verbose=True)

        assert f in paths
        assert len(results) == 0

    def test_end_to_end_with_mixed_files(self, tmp_path):
        """End-to-end test with a directory containing various file types."""
        # Clean file
        clean = tmp_path / "output.log"
        clean.write_text("Test completed successfully\n", encoding="utf-8")

        # File with password
        with_secret = tmp_path / "app.conf"
        with_secret.write_text("database_password=abc123\nhost=localhost\n", encoding="utf-8")

        # File with sensitive filename
        sensitive_name = tmp_path / "credentials.json"
        sensitive_name.write_text('{"user": "admin"}\n', encoding="utf-8")

        # Binary file
        binary = tmp_path / "image.png"
        binary.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(100))

        all_paths = [clean, with_secret, sensitive_name, binary]
        processed, results = apply_censoring_to_artifacts(all_paths, censoring_enabled=True)

        # Clean and binary files pass through; secret-content and sensitive-name are sanitized
        assert clean in processed
        assert binary in processed

        # Verify content was sanitized
        assert "abc123" not in with_secret.read_text()
        assert "[REDACTED]" in with_secret.read_text()

        # Verify sensitive filename content was replaced
        assert "Content censored by caliper" in sensitive_name.read_text()

    def test_dry_run_preserves_all_files(self, tmp_path):
        f = tmp_path / "config.txt"
        original = "password=secret123\n"
        f.write_text(original, encoding="utf-8")

        processed, results = apply_censoring_to_artifacts([f], censoring_enabled=True, dry_run=True)

        # File content should be unchanged in dry run
        assert f.read_text() == original
        # But results should still report what would be censored
        assert len(results) == 1
        assert results[0].sanitized

    def test_vault_secrets_are_censored(self, tmp_path):
        f = tmp_path / "log.txt"
        # Use content that won't match keyword patterns but contains a vault secret
        f.write_text("Connecting to host with my-vault-secret-value as auth\n", encoding="utf-8")

        processed, results = apply_censoring_to_artifacts(
            [f],
            censoring_enabled=True,
            vault_secrets={"my-vault-secret-value"},
            secret_mapping={"my-vault-secret-value": "test-vault/api-key"},
        )

        content = f.read_text()
        assert "my-vault-secret-value" not in content
        assert "*******" in content

    def test_summary_counts_are_correct(self, tmp_path):
        # Create files of each type
        clean = tmp_path / "clean.txt"
        clean.write_text("all good\n", encoding="utf-8")

        sensitive = tmp_path / "app.cfg"
        sensitive.write_text("api_key=abc\n", encoding="utf-8")

        _, results = apply_censoring_to_artifacts([clean, sensitive], censoring_enabled=True)

        sanitized_count = len([r for r in results if r.sanitized])
        excluded_count = len([r for r in results if r.censored and not r.sanitized])
        clean_count = len([r for r in results if not r.censored])

        assert clean_count == 1
        assert sanitized_count == 1
        assert excluded_count == 0


# ---------------------------------------------------------------------------
# End-to-end directory censoring
# ---------------------------------------------------------------------------


class TestEndToEndDirectoryCensoring:
    def test_full_directory_scan(self, tmp_path):
        """Simulate the full censoring workflow on a directory tree."""
        # Create a realistic artifact tree
        subdir = tmp_path / "test_run" / "artifacts"
        subdir.mkdir(parents=True)

        (subdir / "pod_logs.txt").write_text("INFO: Pod started\nINFO: Pod ready\n")
        (subdir / "config.yaml").write_text("password: supersecret\nhost: example.com\n")
        (subdir / "results.json").write_text('{"score": 95, "status": "pass"}\n')
        (subdir / "server.pem").write_text("-----BEGIN CERTIFICATE-----\nfake\n")

        # Collect all files
        all_paths = list(tmp_path.rglob("*"))
        file_paths = [p for p in all_paths if p.is_file()]

        processed, results = apply_censoring_to_artifacts(file_paths, censoring_enabled=True)

        # All files should be in processed (all get sanitized, none fully excluded)
        assert len(processed) == 4

        # Check specific files
        config_content = (subdir / "config.yaml").read_text()
        assert "supersecret" not in config_content
        assert "[REDACTED]" in config_content
        assert "host: example.com" in config_content

        pem_content = (subdir / "server.pem").read_text()
        assert "Content censored by caliper" in pem_content

        pod_content = (subdir / "pod_logs.txt").read_text()
        assert "Pod started" in pod_content  # Clean content preserved

    def test_read_only_file_handling(self, tmp_path):
        """Test that read-only files can be sanitized."""
        import stat

        f = tmp_path / "readonly_config.txt"
        f.write_text("password=readonly_secret\n", encoding="utf-8")
        f.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

        censor = ArtifactCensor()
        result = censor._sanitize_file_content(f)

        assert result.sanitized
        content = f.read_text()
        assert "readonly_secret" not in content
        assert "[REDACTED]" in content

    def test_empty_file_passes_clean(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_text("", encoding="utf-8")

        censor = ArtifactCensor()
        result = censor._sanitize_file_content(f)

        assert not result.censored
        assert not result.sanitized

    def test_utf8_content_preserved(self, tmp_path):
        f = tmp_path / "unicode.txt"
        f.write_text("Ünîcödé content: password=test123\nMore: über cool\n", encoding="utf-8")

        censor = ArtifactCensor()
        result = censor._sanitize_file_content(f)

        assert result.sanitized
        content = f.read_text()
        assert "test123" not in content
        assert "über cool" in content
