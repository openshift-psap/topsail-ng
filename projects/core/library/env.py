import os
import pathlib
import time
import traceback
import logging

ARTIFACT_DIR = None

def __set_artifact_dir(value):
    global ARTIFACT_DIR
    ARTIFACT_DIR = value


def init():
    global ARTIFACT_DIR
    if "ARTIFACT_DIR" in os.environ:
        artifact_dir = pathlib.Path(os.environ["ARTIFACT_DIR"])

    else:
        env_forge_base_dir = pathlib.Path(os.environ.get("FORGE_BASE_DIR", "/tmp"))
        artifact_dir = env_forge_base_dir / f"forge_{time.strftime('%Y%m%d-%H%M')}"

        artifact_dir.mkdir(parents=True, exist_ok=True)
        os.environ["ARTIFACT_DIR"] = str(artifact_dir)

    artifact_dir.mkdir(parents=True, exist_ok=True)

    __set_artifact_dir(artifact_dir)


def NextArtifactDir(name, *, lock=None, counter_p=None):
    if lock:
        with lock:
            next_count = counter_p[0]
            counter_p[0] += 1
    else:
        next_count = next_artifact_index()

    dirname = ARTIFACT_DIR / f"{next_count:03d}__{name}"

    return TempArtifactDir(dirname)


class TempArtifactDir(object):
    def __init__(self, dirname):
        self.dirname = pathlib.Path(dirname)
        self.previous_dirname = None

    def __enter__(self):
        self.previous_dirname = ARTIFACT_DIR
        os.environ["ARTIFACT_DIR"] = str(self.dirname)
        self.dirname.mkdir(exist_ok=True)

        __set_artifact_dir(self.dirname)

        return True

    def __exit__(self, ex_type, ex_value, exc_traceback):
        if ex_value:
            logging.error(f"Caught exception {ex_type.__name__}: {ex_value}")
            with open(ARTIFACT_DIR / "FAILURE", "a") as f:
                print(f"{ex_type.__name__}: {ex_value}", file=f)
                print(''.join(traceback.format_exception(None, value=ex_value, tb=exc_traceback)), file=f)

        os.environ["ARTIFACT_DIR"] = str(self.previous_dirname)
        __set_artifact_dir(self.previous_dirname)

        return False # If we returned True here, any exception would be suppressed!


def next_artifact_index():
    return len(list(ARTIFACT_DIR.glob("*__*")))
