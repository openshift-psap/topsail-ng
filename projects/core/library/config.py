import copy
import functools
import inspect
import logging
import os
import pathlib
import re
import sys
import types

import click
import jsonpath_ng
import yaml

from projects.core.ci_entrypoint.prepare_ci import CI_METADATA_DIRNAME

from . import env

logger = logging.getLogger(__name__)

VARIABLE_OVERRIDES_FILENAME = "000__ci_metadata/variable_overrides.yaml"

project = None  # the project config will be populated in init()


class TempValue:
    """This context changes temporarily the value of a configuration field"""

    def __init__(self, config, key, value):
        self.config = config
        self.key = key
        self.value = value
        self.prev_value = None

    def __enter__(self):
        self.prev_value = self.config.get_config(self.key, print=False)
        self.config.set_config(self.key, self.value)

        return True

    def __exit__(self, ex_type, ex_value, exc_traceback):
        self.config.set_config(self.key, self.prev_value)

        return False  # If we returned True here, any exception would be suppressed!


class Config:
    def __init__(self, config_path):
        self.config_path = config_path

        if not self.config_path.exists():
            msg = f"Configuration file '{self.config_path}' does not exist :/"
            logger.error(msg)
            raise ValueError(msg)

        logger.info(f"Loading configuration from {self.config_path} ...")
        with open(self.config_path) as config_f:
            self.config = yaml.safe_load(config_f)

        if self.config is None:
            self.config = {}

        if not isinstance(self.config, dict):
            raise ValueError(
                f"YAML loaded from {self.config_path} isn't a dictionnary ({self.config.__class__.__name__})"
            )

    def ensure_core_fields(self):
        """
                The JumpCI currently passes these values:
        -----
        cluster.name: mac
        exec_list._only_: true
        exec_list.test_ci: true
        project.args:
        - init
        project.name: skeleton
        -----
        We will get rid of that when we remove the JumpCI.
        """

        # Define mandatory fields structure
        mandatory_fields = {
            "presets": {},  # Special case: always create as empty dict
            "project": dict.fromkeys(["name", "args"]),
            "ci_job": dict.fromkeys(["name", "fjob", "cluster", "exclusive", "hardware", "owner"]),
        }

        # Apply the mandatory field structure
        for section_name, section_fields in mandatory_fields.items():
            # Create section if it doesn't exist
            if section_name not in self.config:
                self.config[section_name] = {}

            # Handle special case for presets (always overwrite with empty dict)
            if section_name == "presets":
                self.config[section_name] = section_fields
                continue

            # Add missing fields to the section
            for field_name, default_value in section_fields.items():
                if field_name in self.config[section_name]:
                    continue
                self.config[section_name][field_name] = default_value

    def save_config_overrides(self):
        variable_overrides_path = env.ARTIFACT_DIR / VARIABLE_OVERRIDES_FILENAME

        if not variable_overrides_path.exists():
            logger.debug(
                f"save_config_overrides: {variable_overrides_path} does not exist, nothing to save."
            )
            self.config["overrides"] = {}
            return

        with open(variable_overrides_path) as f:
            variable_overrides = yaml.safe_load(f)

        self.config["overrides"] = variable_overrides

    def _create_first_parent_config_key(self, key: str, value) -> None:
        """Create a new config key if its parent exists and is a dict."""
        key_parts = key.split(".")
        if len(key_parts) <= 1:
            raise ValueError(
                f"Config key '{key}' does not exist, and cannot create it at the moment :/"
            )

        parent_key = ".".join(key_parts[:-1])
        try:
            parent_value = self.get_config(
                parent_key, print=False, warn=False, handled_secretly=True
            )
        except Exception as e:
            raise ValueError(
                f"Config key '{key}' does not exist, and cannot create it at the moment :/"
            ) from e

        if not isinstance(parent_value, dict):
            raise ValueError(
                f"Config key '{key}' does not exist, and cannot create it at the moment :/"
            )

        # Parent exists and is a dict, create the new key
        child_key = key_parts[-1]
        parent_value[child_key] = value

    def apply_config_overrides(
        self, *, ignore_not_found=False, variable_overrides_path=None, log=True
    ):
        if variable_overrides_path is None:
            variable_overrides_path = env.ARTIFACT_DIR / VARIABLE_OVERRIDES_FILENAME

        if not variable_overrides_path.exists():
            logger.debug(
                f"apply_config_overrides: {variable_overrides_path} does not exist, nothing to override."
            )

            return

        with open(variable_overrides_path) as f:
            variable_overrides = yaml.safe_load(f)

        if not isinstance(variable_overrides, dict):
            msg = f"Wrong type for the variable overrides file. Expected a dictionnary, got {variable_overrides.__class__.__name__}"
            logger.fatal(msg)
            raise ValueError(msg)

        # Setup presets_applied.txt file for writing variable overrides
        dest_txt = env.ARTIFACT_DIR / CI_METADATA_DIRNAME / "presets_applied.txt"
        dest_txt.parent.mkdir(parents=True, exist_ok=True)

        # Collect all override messages to write to file once
        file_messages = []

        try:
            for key, value in variable_overrides.items():
                MAGIC_DEFAULT_VALUE = object()
                handled_secretly = True  # current_value MUST NOT be printed below.
                current_value = self.get_config(
                    key,
                    MAGIC_DEFAULT_VALUE,
                    print=False,
                    warn=False,
                    handled_secretly=handled_secretly,
                )
                if current_value == MAGIC_DEFAULT_VALUE:
                    try:
                        # Try to create the key if parent exists and is a dict
                        self._create_first_parent_config_key(key, value)
                        self.save_config()
                    except ValueError:
                        if not ignore_not_found:
                            raise

                        if log:
                            msg = f"config override IGNORED: {key} --> {value}"
                            logger.info(msg)
                            file_messages.append(msg)
                        continue

                    self.save_config()
                    if log:
                        msg = f"config override (new key): {key} --> {value}"
                        logger.info(msg)
                        file_messages.append(msg)
                    continue

                self.set_config(key, value, print=False)
                actual_value = self.get_config(
                    key, print=False
                )  # ensure that key has been set, raises an exception otherwise
                if log:
                    msg = f"config override: {key} --> {actual_value}"
                    logger.info(msg)
                    file_messages.append(msg)
        finally:
            # Write all collected messages to file, even if processing failed
            if file_messages:
                with open(dest_txt, "a") as f:
                    for msg in file_messages:
                        print(msg, file=f)

    def apply_preset(self, name):
        values = self.get_preset(name)
        if not values:
            raise ValueError(f"No preset found with name '{name}'")

        logger.info(f"Applying preset '{name}' ==> {values}")
        dest_txt = env.ARTIFACT_DIR / CI_METADATA_DIRNAME / "presets_applied.txt"
        dest_txt.parent.mkdir(parents=True, exist_ok=True)

        # Collect preset messages to write to file once
        preset_messages = []

        for key, value in values.items():
            if key == "extends":
                for extend_name in value or []:
                    self.apply_preset(extend_name)
                continue

            msg = f"preset[{name}] {key} --> {value}"
            logger.info(msg)
            preset_messages.append(msg)

            self.set_config(key, value, print=False)

        # Write all collected preset messages to file once
        if preset_messages:
            with open(dest_txt, "a") as f:
                for msg in preset_messages:
                    print(msg, file=f)

    def load_presets(self, preset_dir):
        for preset_file in preset_dir.glob("*.yaml"):
            with open(preset_file) as preset_f:
                preset_dict = yaml.safe_load(preset_f)
            if "__multiple" in preset_dict:
                # Filter out __multiple key when merging presets
                filtered_dict = {k: v for k, v in preset_dict.items() if k != "__multiple"}
                self.config["presets"].update(filtered_dict)
            else:
                self.config["presets"][preset_file.stem] = preset_dict

        self.save_config()

    def get_preset(self, name):
        return self.config["presets"].get(name)

    def apply_presets_from_project_args(self, *, lenient=False):
        for arg_name in self.get_config("project.args", print=False) or []:
            try:
                self.apply_preset(arg_name)
            except Exception as e:
                if not lenient:
                    raise
                logger.warning(
                    "apply_presets_from_project_args: failed to apply preset %r: %s", arg_name, e
                )

    def apply_presets_from_cluster_config(self, *, lenient_presets=False):
        """Apply cluster-specific configuration if ci_job.cluster matches cluster_config keys."""

        # Get the current cluster name from ci_job.cluster
        cluster_name = None
        try:
            cluster_name = self.get_config("ci_job.cluster", print=False)
        except KeyError:
            pass

        # If ci_job.cluster isn't set, try to load from forge-config ConfigMap
        if not cluster_name:
            cluster_name = self._get_cluster_from_configmap()

        if not cluster_name:
            logger.info(
                "apply_presets_from_cluster_config: no cluster name found (ci_job.cluster or forge-config ConfigMap), skipping cluster config application."
            )
            logging.info(
                "Use this command to configure the cluster name in the current namespace:\n"
                "oc create configmap forge-config --from-literal=cluster=$CLUSTER_NAME"
            )

            return

        # Apply preset named cluster_{cluster_name}
        preset_name = f"cluster_{cluster_name}"

        if not self.get_preset(preset_name):
            logging.info(
                f"apply_presets_from_cluster_config: no preset named '{preset_name}', nothing to apply."
            )
            return

        try:
            self.apply_preset(preset_name)
            logger.info(f"Applied cluster preset: {preset_name}")
        except Exception as e:
            if not lenient_presets:
                raise
            logger.warning(
                "apply_presets_from_cluster_config: failed to apply cluster preset %r: %s",
                preset_name,
                e,
            )

    def _get_cluster_from_configmap(self):
        """Get cluster name from forge-config ConfigMap as fallback."""
        import subprocess

        try:
            logger.debug(
                "apply_presets_from_cluster_config: trying to get cluster name from forge-config ConfigMap"
            )

            # Run oc get cm forge-config -o yaml
            result = subprocess.run(
                ["oc", "get", "cm", "forge-config", "-o", "yaml"],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                logger.debug(
                    f"apply_presets_from_cluster_config: failed to get forge-config ConfigMap: {result.stderr.strip()}"
                )
                return None

            # Parse the YAML output
            configmap_data = yaml.safe_load(result.stdout)
            if not configmap_data or "data" not in configmap_data:
                logger.debug(
                    "apply_presets_from_cluster_config: forge-config ConfigMap has no data section"
                )
                return None

            cluster_name = configmap_data["data"].get("cluster")
            if cluster_name:
                logger.info(
                    f"apply_presets_from_cluster_config: loaded cluster name '{cluster_name}' from forge-config ConfigMap"
                )
                return cluster_name.strip()
            else:
                logger.debug(
                    "apply_presets_from_cluster_config: no 'cluster' field found in forge-config ConfigMap data"
                )
                return None

        except subprocess.TimeoutExpired:
            logger.warning(
                "apply_presets_from_cluster_config: timeout getting forge-config ConfigMap"
            )
            return None
        except Exception as e:
            logger.debug(
                f"apply_presets_from_cluster_config: error getting cluster from ConfigMap: {e}"
            )
            return None

    def has_config(self, jsonpath):
        try:
            _ = (
                jsonpath_ng.parse(jsonpath).find(self.config)[0].value
            )  # raises an IndexError if jsonpath isn't found
            return True
        except IndexError:
            return False

    def get_config(
        self, jsonpath, default_value=..., warn=True, print=True, handled_secretly=False
    ):
        try:
            value = jsonpath_ng.parse(jsonpath).find(self.config)[0].value
        except IndexError as ex:
            if default_value != ...:
                if warn:
                    logger.warning(
                        f"get_config: {jsonpath} --> missing. Returning the default value: {default_value}"
                    )
                return default_value

            logger.error(f"get_config: {jsonpath} --> {ex}")
            raise KeyError(f"Key '{jsonpath}' not found in {self.config_path}") from ex

        if isinstance(value, str) and value.startswith("*$@"):
            print = False

        value = self.resolve_reference(value, handled_secretly)

        if print and not handled_secretly:
            logger.info(f"get_config: {jsonpath} --> {value}")

        return value

    def set_config(self, jsonpath, value, print=True):
        try:
            self.get_config(
                jsonpath, print=False, handled_secretly=True
            )  # will raise an exception if the jsonpath does not exist
            jsonpath_ng.parse(jsonpath).update(self.config, value)
        except Exception as ex:
            logger.error(f"set_config: {jsonpath}={value} --> {ex}")
            raise

        if print:
            logger.info(f"set_config: {jsonpath} --> {value}")

        self.save_config()

    def save_config(self, dest=None):
        config_path = dest if dest is not None else self.config_path
        with open(config_path, "w") as f:
            yaml.dump(self.config, f, indent=4, default_flow_style=False, sort_keys=False)

    def resolve_reference(self, value, handled_secretly=False):
        if not isinstance(value, str):
            return value
        if "@" not in value:
            return value

        # --- #

        def secret_file_dereference():
            if not handled_secretly:
                msg = f"{value} is a secret dereference, but get_config(..., handled_secretly=False). Aborting"
                logger.fatal(msg)
                raise ValueError(msg)

            ref_key = value.removeprefix("*$@")
            ref_value = self.get_config(ref_key, print=False)

            secret_dir = pathlib.Path(
                os.environ[self.get_config("secrets.dir.env_key", print=False)]
            )
            secret_value = (secret_dir / ref_value).read_text().strip()

            return secret_value

        # --- #

        def simple_dereference():
            ref_key = value[1:]
            return self.get_config(ref_key)

        def multi_dereference():
            new_value = value
            for ref in re.findall(r"\{@.*?\}", value):
                ref_key = ref.strip("{@}")
                ref_value = self.get_config(ref_key, print=False)
                new_value = new_value.replace(ref, str(ref_value))

            return new_value

        # --- #

        if value.startswith("*$@"):
            return secret_file_dereference()

        if value.startswith("*@"):
            # value can be printed here, it's a reference to a secret, not a secret value
            msg = f"resolve_reference: '*@' references not supported (not sure how to handle it wrt to secrets) --> {value}"
            logger.fatal(msg)
            raise ValueError(msg)

        if not (value.startswith("@") or "{@" in value):
            # don't go further if the derefence anchor isn't found
            return value

        # --- #

        new_value = simple_dereference() if value.startswith("@") else multi_dereference()

        if not handled_secretly:
            logger.info(f"resolve_reference: {value} ==> '{new_value}'")

        return copy.deepcopy(new_value)

    def filter_out_used_overrides(self):
        """
        Remove the config fields that apply to the current config.
        Keep only the overrides that do not apply.
        """

        overrides = self.get_config("overrides", {}) or {}
        new_overrides = {}
        for key, value in overrides.items():
            if self.has_config(key):
                continue
            new_overrides[key] = value

        self.set_config("overrides", new_overrides, print=False)


def __get_config_path(orchestration_dir):
    config_file_src = orchestration_dir / "config.yaml"
    config_dir_src = orchestration_dir / "config.d"
    config_chunk_files = _get_config_chunk_files(config_dir_src)
    config_path_final = pathlib.Path(env.ARTIFACT_DIR / "config.yaml")

    if not config_file_src.exists() and not config_chunk_files:
        raise ValueError(
            f"Cannot find the source config file at {config_file_src} "
            f"or config YAML chunks under {config_dir_src}"
        )

    if config_path_final.exists():
        config_path_final.unlink()

    logger.info(
        f"Consolidating the configuration from {config_file_src} "
        f"and {config_dir_src} to the artifact dir ..."
    )
    consolidated_config = _load_project_config(config_file_src, config_chunk_files)

    with open(config_path_final, "w") as config_f:
        yaml.safe_dump(
            consolidated_config,
            config_f,
            indent=4,
            default_flow_style=False,
            sort_keys=False,
        )

    return config_path_final, config_file_src


def _get_config_chunk_files(config_dir_src):
    if not config_dir_src.is_dir():
        return []

    return sorted([*config_dir_src.glob("*.yaml"), *config_dir_src.glob("*.yml")])


def _load_project_config(config_file_src, config_chunk_files):
    config = {}
    if config_file_src.exists():
        with open(config_file_src) as config_f:
            config = yaml.safe_load(config_f) or {}

    if not config_chunk_files:
        return config

    for chunk_file in config_chunk_files:
        if chunk_file.name.startswith("."):
            logger.info(f"Ignore hidden file '{chunk_file.parent}'")
            continue

        with open(chunk_file) as chunk_f:
            chunk_value = yaml.safe_load(chunk_f)

        key = chunk_file.stem
        if key in config:
            raise ValueError(
                f"Configuration section '{key}' is defined in both "
                f"{config_file_src} and {chunk_file}"
            )
        config[key] = chunk_value

    return config


REQUIRES_ANNOTATION_ARG_NAME = "_cfg"


# annotation
def requires(**config_kwargs):
    def decorator(func):
        if REQUIRES_ANNOTATION_ARG_NAME not in inspect.signature(func).parameters.keys():
            raise SyntaxError(
                f"Function '{func.__name__}' must accept "
                f"a {REQUIRES_ANNOTATION_ARG_NAME} parameter."
            )

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            nonlocal config_kwargs

            config_obj = types.SimpleNamespace()

            for field_name, config_path in config_kwargs.items():
                config_obj.__dict__[field_name] = project.get_config(config_path)

            kwargs[REQUIRES_ANNOTATION_ARG_NAME] = config_obj

            return func(*args, **kwargs)

        return wrapper

    return decorator


def init(orchestration_dir, *, apply_config_overrides=True, apply_cluster_config=True):
    global project

    if project:
        logger.info("config.init: project config already configured.")
        return

    config_path, src_config = __get_config_path(orchestration_dir)

    project = Config(config_path)

    if not apply_config_overrides:
        logger.info(
            "config.init: running with 'apply_config_overrides=False', "
            "skipping the overrides. Saving it as 'overrides' "
            "field in the project configuration."
        )
        project.save_config_overrides()
        project.save_config()
        return

    project.ensure_core_fields()
    project.load_presets(src_config.parent / "presets.d")

    # Ensure presets_applied.txt exists even if empty
    presets_applied_file = env.ARTIFACT_DIR / CI_METADATA_DIRNAME / "presets_applied.txt"
    presets_applied_file.parent.mkdir(parents=True, exist_ok=True)
    presets_applied_file.touch(exist_ok=True)

    # Derive lenient_presets from Click context when available, fallback to sys.argv
    lenient_presets = False
    try:
        ctx = click.get_current_context()
        lenient_presets = ctx.info_name == "resolve-fournos-config"
    except (ImportError, RuntimeError):
        # Fallback to sys.argv when Click context is not available
        lenient_presets = "resolve-fournos-config" in sys.argv

    if lenient_presets:
        logging.info("Fournos resolve step detected. Applying the presets in lenient mode.")

    project.apply_config_overrides(ignore_not_found=lenient_presets)
    project.apply_presets_from_project_args(lenient=lenient_presets)
    if apply_cluster_config:
        project.apply_presets_from_cluster_config(lenient_presets=lenient_presets)
    project.apply_config_overrides(
        ignore_not_found=lenient_presets
    )  # reapply so that the value overrides are applied last


def reload(orchestration_dir, *, apply_config_overrides=True, apply_cluster_config=True):
    global project

    project = None

    artifact_config = env.ARTIFACT_DIR / "config.yaml"
    if artifact_config.exists():
        artifact_config.unlink()

    presets_applied = env.ARTIFACT_DIR / CI_METADATA_DIRNAME / "presets_applied.txt"
    if presets_applied.exists():
        presets_applied.unlink()

    init(
        orchestration_dir,
        apply_config_overrides=apply_config_overrides,
        apply_cluster_config=apply_cluster_config,
    )
    return project


def write_variables_override(presets=None, variables_dict=None):
    """Write configuration overrides to the variables_override.yaml file in ARTIFACT_DIR.

    This is a top-level function that doesn't require project config to be initialized.

    Args:
        presets: List of presets to set in project.args
        variables_dict: Dictionary of additional configuration paths and values
    """
    from pathlib import Path

    if not env.ARTIFACT_DIR:
        raise ValueError(
            "env.ARTIFACT_DIR is not initialized. "
            "Call env.init() before using write_variables_override()"
        )

    artifact_dir = Path(env.ARTIFACT_DIR)
    override_file = artifact_dir / VARIABLE_OVERRIDES_FILENAME

    # Ensure the directory exists
    override_file.parent.mkdir(parents=True, exist_ok=True)

    # Load existing overrides or create empty structure
    if override_file.exists():
        with open(override_file) as f:
            loaded = yaml.safe_load(f)
            if loaded is not None and not isinstance(loaded, dict):
                raise ValueError(
                    f"Override file {override_file} must contain a YAML dictionary, got {type(loaded).__name__}"
                )
            overrides = loaded or {}
    else:
        overrides = {}

    # Set presets in project.args
    if presets:
        if len(presets) == 1 and "," in presets[0]:
            presets = list(presets[0].split(","))
        presets = list(presets)  # enforce list type for proper yaml serialization
        overrides["project.args"] = presets
        logger.info(f"write_variables_override: project.args --> {presets}")

    # Add additional variables to top level
    if variables_dict:
        for jsonpath, value in variables_dict.items():
            overrides[jsonpath] = value
            logger.info(f"write_variables_override: {jsonpath} --> {value}")

    # Write back to file
    with open(override_file, "w") as f:
        yaml.dump(overrides, f, indent=4, default_flow_style=False, sort_keys=False)

    total_items = (1 if presets else 0) + (len(variables_dict) if variables_dict else 0)
    logger.info(f"write_variables_override: wrote {total_items} override(s) to {override_file}")
