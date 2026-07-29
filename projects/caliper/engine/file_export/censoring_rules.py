"""
Censoring rules and patterns for Caliper artifact filtering.

This module defines the patterns and rules used to identify sensitive content
in artifacts before upload.
"""

from __future__ import annotations

import re

# Keyword patterns to detect in file content
# These are compiled regex patterns that match common sensitive data patterns
KEYWORD_PATTERNS = [
    # Password patterns
    r"password\s*[:=]\s*\S+",
    r"pwd\s*[:=]\s*\S+",
    r"passwd\s*[:=]\s*\S+",
    # API key patterns
    r"api[_-]?key\s*[:=]\s*\S+",
    r"apikey\s*[:=]\s*\S+",
    r"api[_-]?secret\s*[:=]\s*\S+",
    # Token patterns
    r"token\s*[:=]\s*\S+",
    r"secret[_-]?token\s*[:=]\s*\S+",
    r"access[_-]?token\s*[:=]\s*\S+",
    r"refresh[_-]?token\s*[:=]\s*\S+",
    # Bearer tokens
    r"Bearer\s+[A-Za-z0-9+/=]+",
    r"bearer\s+[A-Za-z0-9+/=]+",
    # Specific service API keys
    r"sk-[a-zA-Z0-9]{32,}",  # OpenAI API keys
    r"ghp_[a-zA-Z0-9]{36}",  # GitHub personal access tokens
    r"gho_[a-zA-Z0-9]{36}",  # GitHub OAuth tokens
    r"ghu_[a-zA-Z0-9]{36}",  # GitHub user-to-server tokens
    r"ghs_[a-zA-Z0-9]{36}",  # GitHub server-to-server tokens
    r"ghr_[a-zA-Z0-9]{36}",  # GitHub refresh tokens
    # AWS patterns
    r"AKIA[0-9A-Z]{16}",  # AWS Access Key ID
    r"aws[_-]?secret[_-]?access[_-]?key\s*[:=]\s*\S+",
    # Database connection strings
    r"mongodb://[^/\s]+:[^@\s]+@",
    r"mysql://[^/\s]+:[^@\s]+@",
    r"postgresql://[^/\s]+:[^@\s]+@",
    # Generic credential patterns
    r"credential\s*[:=]\s*\S+",
    r"secret\s*[:=]\s*\S+",
    r"private[_-]?key\s*[:=]\s*\S+",
]

# Compile patterns for better performance
COMPILED_KEYWORD_PATTERNS = [re.compile(pattern, re.IGNORECASE) for pattern in KEYWORD_PATTERNS]

# File patterns to always censor (by filename)
SENSITIVE_FILE_PATTERNS = [
    r".*\.pem$",  # PEM certificate files
    r".*\.key$",  # Private key files
    r".*\.p12$",  # PKCS#12 certificate files
    r".*\.pfx$",  # PKCS#12 certificate files (Windows)
    r".*secret.*",  # Any file with "secret" in the name
    r".*credential.*",  # Any file with "credential" in the name
    r".*password.*",  # Any file with "password" in the name
    r".*\.ssh/.*",  # SSH directory contents
    r".*/\.ssh/.*",  # SSH directory contents (with path)
    r".*id_rsa.*",  # SSH private keys
    r".*id_dsa.*",  # DSA private keys
    r".*id_ecdsa.*",  # ECDSA private keys
    r".*id_ed25519.*",  # Ed25519 private keys
    r".*\.env$",  # Environment files
    r".*\.env\..*",  # Environment files with suffixes
]

# Compile file patterns for better performance
COMPILED_FILE_PATTERNS = [re.compile(pattern, re.IGNORECASE) for pattern in SENSITIVE_FILE_PATTERNS]


def matches_sensitive_filename(filename: str) -> bool:
    """
    Check if a filename matches any sensitive file pattern.

    Args:
        filename: The filename or path to check

    Returns:
        bool: True if the filename indicates a sensitive file
    """
    return any(pattern.match(filename) for pattern in COMPILED_FILE_PATTERNS)


def find_sensitive_content_in_text(content: str) -> list[str]:
    """
    Find sensitive content patterns in text.

    Args:
        content: Text content to scan

    Returns:
        list[str]: List of pattern descriptions that matched
    """
    matches = []
    for i, pattern in enumerate(COMPILED_KEYWORD_PATTERNS):
        if pattern.search(content):
            # Return the original pattern string for logging
            matches.append(KEYWORD_PATTERNS[i])

    return matches
