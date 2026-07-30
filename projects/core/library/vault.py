"""
Vault management system for FORGE projects

This module provides:
- Vault definition loading and validation
- Project vault requirements checking
- Content verification against filesystem
- Integration with existing secret dereferencing system
"""

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import yaml

if TYPE_CHECKING:
    pass

import projects.core.library.env as env

logger = logging.getLogger(__name__)


@dataclass
class VaultContent:
    """Represents a single piece of content in a vault"""

    name: str
    description: str
    filename: str | None = None
    sensible: bool = True
    _vault: Optional["VaultDefinition"] = None

    def __post_init__(self):
        # Default filename to the content name if not specified
        if self.filename is None:
            self.filename = self.name

    @property
    def is_sensible(self) -> bool:
        """Whether this content should be considered sensitive for censoring purposes"""
        return self.sensible

    @property
    def file_path(self) -> Path | None:
        """Get the full absolute path to this content file"""
        if self._vault is None:
            return None

        secret_dir = self._vault.secret_dir
        if secret_dir is None:
            return None

        return secret_dir / self.filename


@dataclass
class VaultDefinition:
    """Represents a complete vault definition"""

    name: str
    env_key: str
    description: str
    content: dict[str, VaultContent]

    @property
    def secret_dir(self) -> Path | None:
        """Get the secret directory path from environment"""
        if self.env_key not in os.environ:
            return None
        return Path(os.environ[self.env_key])


class VaultManager:
    """Manages vault definitions and validation"""

    def __init__(self, vault_definitions_dir: Path = None):
        self.vault_definitions_dir = vault_definitions_dir or env.FORGE_HOME / "vaults"
        self._vault_cache: dict[str, VaultDefinition] = {}
        self._load_vault_definitions()

    def _load_vault_definitions(self):
        """Load all vault definitions from the vaults directory"""
        if not self.vault_definitions_dir.exists():
            logger.warning(
                f"Vault definitions directory does not exist: {self.vault_definitions_dir}"
            )
            return

        for vault_file in self.vault_definitions_dir.glob("*.yaml"):
            try:
                vault_def = self._load_vault_definition(vault_file)
                self._vault_cache[vault_def.name] = vault_def
                logger.debug(f"Loaded vault definition: {vault_def.name}")
            except Exception as e:
                logger.error(f"Failed to load vault definition from {vault_file}: {e}")

    def _load_vault_definition(self, vault_file: Path) -> VaultDefinition:
        """Load a single vault definition from a YAML file"""
        with open(vault_file) as f:
            data = yaml.safe_load(f)

        # Vault name is derived from filename (without .yaml extension)
        vault_name = vault_file.stem

        # Parse content definitions
        content = {}
        for content_name, content_def in data.get("content", {}).items():
            if isinstance(content_def, dict):
                # New format with file mapping and description
                filename = content_def.get("file", content_name)
                description = content_def.get("description", "")  # Don't provide default
                sensible = content_def.get("sensible", True)  # Default to True
            else:
                # Legacy format - content_def is the description
                filename = content_name
                description = content_def if content_def else ""
                sensible = True  # Sensible by default

            content[content_name] = VaultContent(
                name=content_name, description=description, filename=filename, sensible=sensible
            )

        vault_def = VaultDefinition(
            name=vault_name,
            env_key=data["env_key"],
            description=data.get("description", ""),
            content=content,
        )

        # Set vault reference on all content items
        for content_item in vault_def.content.values():
            content_item._vault = vault_def

        return vault_def

    def get_vault(self, vault_name: str) -> VaultDefinition | None:
        """Get a vault definition by name"""
        return self._vault_cache.get(vault_name)

    def list_vaults(self) -> list[str]:
        """List all available vault names"""
        return list(self._vault_cache.keys())

    def validate_vault(self, vault_name: str, strict: bool = True) -> bool:
        """
        Validate that a vault's actual content matches its definition

        Args:
            vault_name: Name of the vault to validate
            strict: If True, fail if any content is missing. If False, only warn.

        Returns:
            True if vault is valid, False if validation failed
        """
        # Override strict parameter if globally disabled
        global _strict_validation_enabled
        effective_strict = strict and _strict_validation_enabled
        vault = self.get_vault(vault_name)
        if vault is None:
            logger.error(f"Vault '{vault_name}' is not defined")
            return False

        all_valid = True

        # Validate vault description
        if not vault.description or not vault.description.strip():
            msg = f"Vault '{vault_name}' is missing description"
            if effective_strict:
                logger.error(msg)
                all_valid = False
            else:
                logger.warning(msg)

        # Check if environment variable is set
        if vault.env_key not in os.environ:
            msg = f"Vault '{vault_name}' requires environment variable {vault.env_key} to be set"
            if effective_strict:
                logger.error(msg)
                return False
            else:
                logger.warning(msg)
                return True

        secret_dir = vault.secret_dir
        if not secret_dir.exists():
            msg = f"Vault '{vault_name}' secret directory does not exist: {secret_dir}"
            if effective_strict:
                logger.error(msg)
                return False
            else:
                logger.warning(msg)
                return True

        # Validate each piece of content
        for content_name, content_def in vault.content.items():
            # Validate content description
            if not content_def.description or not content_def.description.strip():
                msg = f"Vault '{vault_name}' content '{content_name}' is missing description"
                if effective_strict:
                    logger.error(msg)
                    all_valid = False
                else:
                    logger.warning(msg)

            # Validate content file exists
            content_path = content_def.file_path
            if content_path is None or not content_path.exists():
                msg = f"Vault '{vault_name}' missing content '{content_name}' at: {content_path}"
                if effective_strict:
                    logger.error(msg)
                    all_valid = False
                else:
                    logger.warning(msg)

        # Check for extra files in vault directory that aren't defined
        defined_files = {content_def.filename for content_def in vault.content.values()}

        for file_path in secret_dir.iterdir():
            if file_path.is_file():
                filename = file_path.name

                # Ignore files that start with "secretsync" (automated sync tool files)
                if filename.startswith("secretsync"):
                    logger.debug(f"Ignoring secretsync file: {filename}")
                    continue

                if filename not in defined_files:
                    msg = f"Vault '{vault_name}' contains extra file '{filename}' at '{file_path}' not defined in specification"
                    # Always warn for extra files, never fail validation
                    logger.warning(msg)

                    # Add CI notification for extra vault files
                    from projects.core.library import ci as ci_lib

                    notification_msg = f"Extra vault file detected: {vault_name}/{filename}\n\nThis file exists in the vault but is not defined in the vault specification. This is not an error but should be reviewed for security purposes."
                    ci_lib.add_notification_file(
                        name=f"vault_extra_file_{vault_name}_{filename}",
                        message=notification_msg,
                    )
                    logger.debug(
                        f"Added notification for extra vault file: {vault_name}/{filename}"
                    )

        if all_valid:
            logger.info(f"Vault '{vault_name}' validation passed")

        return all_valid

    def validate_project_vaults(self, project_name: str, strict: bool = True) -> bool:
        """
        Validate all vaults required by a project

        Args:
            project_name: Name of the project
            strict: If True, fail if any vault is invalid

        Returns:
            True if all project vaults are valid
        """
        project_vaults = self.load_project_vault_requirements(project_name)
        if not project_vaults:
            logger.info(f"Project '{project_name}' has no vault requirements")
            return True

        all_valid = True
        for vault_requirement in project_vaults:
            vault_name = vault_requirement.get("name")
            if not vault_name:
                logger.error(f"Project '{project_name}' has vault requirement without 'name' field")
                all_valid = False
                continue

            if not self.validate_vault(vault_name, strict=strict):
                all_valid = False

        return all_valid

    def load_project_vault_requirements(self, project_name: str) -> list[dict]:
        """Load vault requirements for a specific project"""
        vaults_file = env.FORGE_HOME / "projects" / project_name / "orchestration" / "vaults.yaml"

        if not vaults_file.exists():
            return []

        try:
            with open(vaults_file) as f:
                data = yaml.safe_load(f)

            # Handle both list format and dict format
            if isinstance(data, list):
                return data
            elif isinstance(data, dict) and "vaults" in data:
                return data["vaults"]
            else:
                logger.warning(f"Project vault file has unexpected format: {vaults_file}")
                return []

        except Exception as e:
            logger.error(f"Failed to load project vault requirements from {vaults_file}: {e}")
            return []

    def get_vault_content_path(self, vault_name: str, content_name: str) -> Path | None:
        """
        Get the full path to a specific piece of vault content

        Args:
            vault_name: Name of the vault
            content_name: Name of the content within the vault

        Returns:
            Path to the content file, or None if not found/accessible
        """
        vault = self.get_vault(vault_name)
        if not vault:
            return None

        if content_name not in vault.content:
            logger.error(f"Content '{content_name}' not defined in vault '{vault_name}'")
            return None

        content_def = vault.content[content_name]
        return content_def.file_path

    def validate_all_vaults(self, strict: bool = True) -> bool:
        """Validate all defined vaults"""
        all_valid = True

        for vault_name in self.list_vaults():
            if not self.validate_vault(vault_name, strict=strict):
                all_valid = False

        return all_valid


def _filter_and_validate_vaults(
    vault_manager: VaultManager, vaults: list[str], strict: bool = None
):
    """
    Filter vault manager to only include specified vaults and validate them

    Args:
        vault_manager: The vault manager instance to filter
        vaults: List of vault names to keep and validate
        strict: If True, fail on validation errors. If False, warn only. If None, use global setting.
    Raises:
        ValueError: If requested vaults don't exist and strict=True
        RuntimeError: If vault validation fails and strict=True
    """
    if strict is None:
        strict = _strict_validation_enabled

    # Filter to only keep specified vaults, but don't remove others if this is not the first call
    available_vaults = set(vault_manager.list_vaults())
    requested_vaults = set(vaults)

    # Check for requested vaults that don't exist
    missing_vaults = requested_vaults - available_vaults
    if missing_vaults:
        msg = f"Requested vaults not found: {sorted(missing_vaults)}"
        if strict:
            raise ValueError(msg)
        else:
            logger.warning(msg)
            # Remove missing vaults from requested list
            requested_vaults = requested_vaults - missing_vaults

    logger.info(
        f"Processing {len(requested_vaults)} vaults with strict={strict}: {sorted(requested_vaults)}"
    )

    # Validate that the requested vaults match their specifications
    validation_failed = False
    for vault_name in requested_vaults:
        logger.info(f"Validating vault: {vault_name}")
        if not vault_manager.validate_vault(vault_name, strict=strict):
            if strict:
                logger.error(f"Vault '{vault_name}' failed validation")
                validation_failed = True
            else:
                logger.warning(f"Vault '{vault_name}' failed validation (non-strict mode)")

    if validation_failed and strict:
        raise RuntimeError("One or more mandatory vaults failed validation")

    if validation_failed:
        logger.info("Optional vault validation failed (warnings may have been issued)")
    else:
        logger.info(f"All {'mandatory' if strict else 'optional'} vaults validated successfully")

    return not validation_failed


# Global vault manager instance
_vault_manager: VaultManager | None = None

# Global strict validation flag
_strict_validation_enabled: bool = True


def disable_strict_validation():
    """Disable strict validation globally for all vault operations"""
    global _strict_validation_enabled
    _strict_validation_enabled = False
    logger.info("Vault strict validation disabled globally")


def enable_strict_validation():
    """Enable strict validation globally for all vault operations"""
    global _strict_validation_enabled
    _strict_validation_enabled = True
    logger.info("Vault strict validation enabled globally")


def is_strict_validation_enabled() -> bool:
    """Check if strict validation is currently enabled"""
    global _strict_validation_enabled
    return _strict_validation_enabled


def init(
    vaults: list[str] = None,
    mandatory_vaults: list[str] = None,
    optional_vaults: list[str] = None,
):
    """Initialize the vault manager

    Args:
        vaults: List of vaults (legacy parameter, treated as mandatory)
        mandatory_vaults: List of mandatory vaults (must be available or automation fails)
        optional_vaults: List of optional vaults (validated with strict=False)

    - Initialize the vault manager

    if vaults is passed:
    - Remove the unnecessary vaults if vault
    - Validate that the vaults match the specifications

    """

    global _vault_manager, _strict_validation_enabled
    if _vault_manager is not None:
        logger.warning("VaultManager already initialized", stack_info=True)
        return

    _vault_manager = VaultManager()

    # Handle legacy single vaults list (treated as mandatory)
    if vaults:
        mandatory_vaults = (mandatory_vaults or []) + vaults

    # If no vaults specified at all, return early
    if not mandatory_vaults and not optional_vaults:
        return

    # Validate mandatory vaults with strict=True
    if mandatory_vaults:
        logger.info(f"Validating mandatory vaults: {mandatory_vaults}")
        _filter_and_validate_vaults(_vault_manager, mandatory_vaults, strict=True)

    # Validate optional vaults with strict=False
    if optional_vaults:
        logger.info(f"Validating optional vaults: {optional_vaults}")
        _filter_and_validate_vaults(_vault_manager, optional_vaults, strict=False)


def get_vault_manager() -> VaultManager:
    """Get the global vault manager instance"""

    if _vault_manager is None:
        raise RuntimeError("VaultManager not initialized. Call vault.init() first.")

    return _vault_manager


def validate_project_vaults(project_name: str, strict: bool = True) -> bool:
    """Convenience function to validate project vaults"""

    return get_vault_manager().validate_project_vaults(project_name, strict=strict)


def get_vault_content_path(vault_name: str, content_name: str) -> Path | None:
    """Convenience function to get vault content path"""

    return get_vault_manager().get_vault_content_path(vault_name, content_name)


# phase vault #


def _phase_vault_get_for_phase(phase: str) -> list[str]:
    """Get vaults needed for a specific phase.

    Args:
        phase: Phase name ('resolve-only', 'test', 'prepare', 'all')

    Returns:
        List of vault names for the specified phase
    """
    from projects.core.library import config

    # Get vaults for specific phase, defaulting to empty list if phase doesn't exist
    return config.project.get_config(f"vaults.{phase}", [], warn=False)


def _get_framework_conditional_vaults() -> tuple[list[str], list[str]]:
    """Get conditionally enabled vaults based on configuration.

    Returns:
        tuple: (mandatory_vaults, optional_vaults)
    """
    mandatory_vaults = []
    optional_vaults = []

    from projects.core.library.export import (
        caliper_agentic_list_vaults,
        caliper_export_list_optional_vaults,
        caliper_export_list_vaults,
    )

    # Get conditionally enabled caliper vaults
    mandatory_vaults += caliper_export_list_vaults()
    optional_vaults += caliper_export_list_optional_vaults()
    optional_vaults += caliper_agentic_list_vaults()

    return mandatory_vaults, optional_vaults


def phase_vault_init(
    phase: str, include_framework_vaults=True, extra_mandatory=None, extra_optional=None
) -> None:
    """Initialize vaults for a specific phase."""

    if phase == "resolve-fournos-config":
        logger.info("No need to initialize the vaults for the resolve step")
        return

    # Get global mandatory vaults (always loaded)
    global_mandatory = _phase_vault_get_for_phase("all")

    # Get phase-specific mandatory vaults
    phase_mandatory = _phase_vault_get_for_phase(phase)

    # Combine all mandatory vaults from config
    mandatory_vaults = global_mandatory + phase_mandatory

    # Get global optional vaults (always loaded optionally)
    global_optional = _phase_vault_get_for_phase("all-optional")

    # Get phase-specific optional vaults
    phase_optional = _phase_vault_get_for_phase(f"{phase}-optional")

    # Combine all optional vaults from config
    optional_vaults = global_optional + phase_optional

    # Add conditional vaults (same mechanism as fournos resolve)
    conditional_mandatory, conditional_optional = (
        _get_framework_conditional_vaults() if include_framework_vaults else ([], [])
    )

    mandatory_vaults += conditional_mandatory
    optional_vaults += conditional_optional

    if extra_mandatory:
        mandatory_vaults += extra_mandatory

    if extra_optional:
        optional_vaults += extra_optional

    if not mandatory_vaults and not optional_vaults:
        logger.info(f"No vault to initialize for phase '{phase}'")
        return

    # Initialize both mandatory and optional vaults in a single call
    # Mandatory vaults: strict=True (automation fails if missing/invalid)
    # Optional vaults: strict=False (automation continues with warnings if missing/invalid)
    init(mandatory_vaults=mandatory_vaults, optional_vaults=optional_vaults)


def phase_vault_list_all() -> list[str]:
    """List all vaults from project config (includes both mandatory and optional)."""
    from projects.core.library import config

    vault_config = config.project.get_config("vaults")

    # Handle both old format (list) and new format (dict with categories)
    if isinstance(vault_config, list):
        return vault_config

    # New format: collect all vaults from all categories
    all_vaults = []
    for _category, vaults in vault_config.items():
        if isinstance(vaults, list):
            all_vaults.extend(vaults)

    # Remove duplicates while preserving order
    seen = set()
    unique_vaults = []
    for _vault in all_vaults:
        if _vault in seen:
            continue

        seen.add(_vault)
        unique_vaults.append(_vault)

    return unique_vaults
