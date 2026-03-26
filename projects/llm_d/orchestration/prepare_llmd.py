import logging
logger = logging.getLogger(__name__)

from projects.core.library import env, config, run


def prepare():
    ns = config.project.get_config("prepare.namespace.name")
    logger.warning(f"Hello prepare {ns}")
    pass


def cleanup():
    logger.warning("Hello cleanup")
    pass
