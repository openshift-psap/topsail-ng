import logging
logging.getLogger().setLevel(logging.INFO)
import os, sys
import pathlib
import yaml
import shutil
import subprocess

import re
from collections import defaultdict
import jsonpath_ng
import copy

from . import env
from . import run

FORGE_HOME = pathlib.Path(__file__).parents[3]
VARIABLE_OVERRIDES_FILENAME = "variable_overrides.yaml"

project = None # the project config will be populated in init()

class TempValue(object):
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

        return False # If we returned True here, any exception would be suppressed!


class Config:
    def __init__(self, config_path):
        self.config_path = config_path

        if not self.config_path.exists():
            msg = f"Configuration file '{self.config_path}' does not exist :/"
            logging.error(msg)
            raise ValueError(msg)

        logging.info(f"Loading configuration from {self.config_path} ...")
        with open(self.config_path) as config_f:
            self.config = yaml.safe_load(config_f)


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

        self.config["presets"] = {}

        for name in "cluster", "project", "exec_list":
            if name in self.config: continue
            self.config[name] = {}

        for name in "_only_", "prepare", "pre_cleanup", "test":
            if name in self.config["exec_list"]: continue
            self.config["exec_list"][name] = None

        for name in "name", "args":
            if name in self.config["project"]: continue
            self.config["project"][name] = None

        for name in "name",:
            if name in self.config["cluster"]: continue
            self.config["cluster"][name] = None



    def apply_config_overrides(self, *, ignore_not_found=False, variable_overrides_path=None, log=True):
        if variable_overrides_path is None:
            variable_overrides_path = env.ARTIFACT_DIR / VARIABLE_OVERRIDES_FILENAME

        if not variable_overrides_path.exists():
            logging.debug(f"apply_config_overrides: {variable_overrides_path} does not exist, nothing to override.")

            return

        with open(variable_overrides_path) as f:
            variable_overrides = yaml.safe_load(f)

        if not isinstance(variable_overrides, dict):
            msg = f"Wrong type for the variable overrides file. Expected a dictionnary, got {variable_overrides.__class__.__name__}"
            logging.fatal(msg)
            raise ValueError(msg)

        for key, value in variable_overrides.items():
            MAGIC_DEFAULT_VALUE = object()
            handled_secretly = True # current_value MUST NOT be printed below.
            current_value = self.get_config(key, MAGIC_DEFAULT_VALUE, print=False, warn=False, handled_secretly=handled_secretly)
            if current_value == MAGIC_DEFAULT_VALUE:
                if ignore_not_found:
                    continue

                raise ValueError(f"Config key '{key}' does not exist, and cannot create it at the moment :/")

            self.set_config(key, value, print=False)
            actual_value = self.get_config(key, print=False) # ensure that key has been set, raises an exception otherwise
            if log:
                logging.info(f"config override: {key} --> {actual_value}")


    def apply_preset(self, name):
        values = self.get_preset(name)
        if not values:
            raise ValueError(f"No preset found with name '{name}'")

        logging.info(f"Applying preset '{name}' ==> {values}")
        for key, value in values.items():
            if key == "extends":
                for extend_name in value:
                    self.apply_preset(extend_name)
                continue

            msg = f"preset[{name}] {key} --> {value}"
            logging.info(msg)
            with open(env.ARTIFACT_DIR / "presets_applied", "a") as f:
                print(msg, file=f)

            self.set_config(key, value, print=False)

    def load_presets(self, preset_dir):
        for preset_file in preset_dir.glob("*.yaml"):
            with open(preset_file) as preset_f:
                preset_dict = yaml.safe_load(preset_f)
            if "__multiple" in preset_dict:
                self.config["presets"].update(preset_dict)
            else:
                self.config["presets"][preset_file.stem] = preset_dict

        self.save_config()

    def get_preset(self, name):
        return self.config["presets"].get(name)

    def apply_presets_from_project_args(self):

        for arg_name in self.get_config("project.args", print=False) or []:
            self.apply_preset(arg_name)


    def get_config(self, jsonpath, default_value=..., warn=True, print=True, handled_secretly=False):
        try:
            value = jsonpath_ng.parse(jsonpath).find(self.config)[0].value
        except IndexError as ex:
            if default_value != ...:
                if warn:
                    logging.warning(f"get_config: {jsonpath} --> missing. Returning the default value: {default_value}")
                return default_value

            logging.error(f"get_config: {jsonpath} --> {ex}")
            raise KeyError(f"Key '{jsonpath}' not found in {self.config_path}")

        if isinstance(value, str) and value.startswith("*$@"):
            print = False

        value = self.resolve_reference(value, handled_secretly)

        if print and not handled_secretly:
            logging.info(f"get_config: {jsonpath} --> {value}")

        return value


    def set_config(self, jsonpath, value, print=True):
        try:
            self.get_config(jsonpath, print=False, handled_secretly=True) # will raise an exception if the jsonpath does not exist
            jsonpath_ng.parse(jsonpath).update(self.config, value)
        except Exception as ex:
            logging.error(f"set_config: {jsonpath}={value} --> {ex}")
            raise

        if print:
            logging.info(f"set_config: {jsonpath} --> {value}")

        self.save_config()


    def save_config(self):
        with open(self.config_path, "w") as f:
            yaml.dump(self.config, f, indent=4, default_flow_style=False, sort_keys=False)


    def resolve_reference(self, value, handled_secretly=False):
        if not isinstance(value, str): return value
        if "@" not in value: return value

        # --- #

        def secret_file_dereference():
            if not handled_secretly:
                msg = f"{value} is a secret dereference, but get_config(..., handled_secretly=False). Aborting"
                logging.fatal(msg)
                raise ValueError(msg)

            ref_key = value.removeprefix("*$@")
            ref_value = self.get_config(ref_key, print=False)

            secret_dir = pathlib.Path(os.environ[self.get_config("secrets.dir.env_key", print=False)])
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
            logging.fatal(msg)
            raise ValueError(msg)

        if not (value.startswith("@") or "{@" in value):
            # don't go further if the derefence anchor isn't found
            return value

        # --- #


        new_value = simple_dereference() if value.startswith("@") \
            else multi_dereference()

        if not handled_secretly:
            logging.info(f"resolve_reference: {value} ==> '{new_value}'")

        return copy.deepcopy(new_value)


def __get_config_path(orchestration_dir):
    config_file_src = orchestration_dir / "config.yaml"
    config_path_final = pathlib.Path(env.ARTIFACT_DIR / "config.yaml")

    if not config_file_src.exists():
        raise ValueError(f"Cannot find the source config file at {config_file_src}")

    if config_path_final.exists():
        logging.info(f"Reloading the config file from FORGE project directory {config_file_src} ...")
        return config_path_final, config_file_src

    logging.info(f"Copying the configuration from {config_file_src} to the artifact dir ...")
    shutil.copyfile(config_file_src, config_path_final)

    return config_path_final, config_file_src


def init(orchestration_dir):
    global project

    if project:
        logging.info("config.init: project config already configured.")
        return

    config_path, src_config = __get_config_path(orchestration_dir)

    project = Config(config_path)

    repo_var_overrides = env.ARTIFACT_DIR / VARIABLE_OVERRIDES_FILENAME

    project.ensure_core_fields()
    project.load_presets(src_config.parent / "presets.d")
    project.apply_config_overrides()
    project.apply_presets_from_project_args()
    project.apply_config_overrides() # reapply so that the value overrides are applied last
