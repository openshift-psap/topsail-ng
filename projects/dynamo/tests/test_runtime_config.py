"""Tests for Dynamo runtime configuration."""

from __future__ import annotations

from projects.dynamo.orchestration.runtime_config import (
    _deep_merge,
    _normalize_string_or_list,
    derive_namespace,
    version_tuple,
)


class TestNormalizeStringOrList:
    def test_none_returns_empty(self):
        assert _normalize_string_or_list(None, "test") == []

    def test_empty_string_returns_empty(self):
        assert _normalize_string_or_list("", "test") == []

    def test_single_string_returns_list(self):
        assert _normalize_string_or_list("aggregated", "test") == ["aggregated"]

    def test_list_passthrough(self):
        assert _normalize_string_or_list(["a", "b"], "test") == ["a", "b"]

    def test_bracket_string_parsed(self):
        result = _normalize_string_or_list("[aggregated, disaggregated]", "test")
        assert result == ["aggregated", "disaggregated"]

    def test_quoted_bracket_string_parsed(self):
        result = _normalize_string_or_list("['aggregated', 'disaggregated']", "test")
        assert result == ["aggregated", "disaggregated"]


class TestDeepMerge:
    def test_override_scalar(self):
        assert _deep_merge({"a": 1}, {"a": 2}) == {"a": 2}

    def test_merge_nested_dicts(self):
        base = {"a": {"b": 1, "c": 2}}
        override = {"a": {"c": 3, "d": 4}}
        assert _deep_merge(base, override) == {"a": {"b": 1, "c": 3, "d": 4}}

    def test_override_replaces_non_dict(self):
        assert _deep_merge({"a": [1, 2]}, {"a": [3]}) == {"a": [3]}


class TestDeriveNamespace:
    def test_basic(self):
        assert derive_namespace("my-job", "dynamo", 63) == "dynamo-my-job"

    def test_already_prefixed(self):
        assert derive_namespace("dynamo-test", "dynamo", 63) == "dynamo-test"

    def test_truncation(self):
        ns = derive_namespace("very-long-job-name-that-exceeds", "dynamo", 20)
        assert len(ns) <= 20

    def test_special_chars_slugified(self):
        ns = derive_namespace("My Job/Test", "dynamo", 63)
        assert "/" not in ns
        assert " " not in ns


class TestVersionTuple:
    def test_semver(self):
        assert version_tuple("4.17.3") == (4, 17, 3)

    def test_with_prefix(self):
        assert version_tuple("v1.2.1") == (1, 2, 1)

    def test_openshift_version(self):
        assert version_tuple("4.19.9-0.nightly") == (4, 19, 9)
