import html
import json
import logging
import os
import pathlib

import yaml

import projects.core.notifications.github.api as github_api
import projects.core.notifications.slack.api as slack_api
from projects.caliper.orchestration.postprocess import POSTPROCESS_STATUS_FILENAME
from projects.core.library import ci as ci_lib

logger = logging.getLogger(__name__)


GITHUB_APP_PEM_FILE = "topsail-bot.2024-09-18.private-key.pem"
GITHUB_APP_CLIENT_ID_FILE = "topsail-bot.clientid"
SLACK_TOKEN_FILE = "topsail-bot.slack-token"

DEFAULT_REPO_OWNER = "openshift-psap"
DEFAULT_REPO_NAME = "forge"


def get_secrets(notification_vault=None):
    """Get secrets directory, preferring vault system over environment variables.

    Args:
        notification_vault: Name of the notification vault to use for secrets

    Returns:
        tuple: (secret_dir_path, source_description) or (None, None) if not found
    """
    # Try vault system first if vault name is provided
    if notification_vault:
        try:
            from projects.core.library import vault as vault_lib

            # Get the vault manager and check if the vault exists
            vault_manager = vault_lib.get_vault_manager()
            vault_def = vault_manager._vault_cache.get(notification_vault)
            if vault_def and vault_def.secret_dir and vault_def.secret_dir.exists():
                logger.info(f"Using notification secrets from vault: {notification_vault}")
                return vault_def.secret_dir, f"vault:{notification_vault}"
            else:
                logger.warning(f"Vault {notification_vault} not found or directory doesn't exist")
        except Exception as e:
            logger.warning(f"Failed to get secrets from vault {notification_vault}: {e}")

    # Fall back to environment variables for backward compatibility
    SECRET_ENV_KEYS = (
        "PSAP_FORGE_NOTIFICATIONS_SECRET_PATH",
        "PSAP_FORGE_JUMP_CI_SECRET_PATH",
    )

    secret_env_key = None
    warn = []
    for secret_env_key in SECRET_ENV_KEYS:
        if os.environ.get(secret_env_key):
            break
        warn.append(f"{secret_env_key} not defined, cannot access the Github secrets")
    else:
        for warning in warn:
            logger.warning(warning)
        return None, None

    secret_dir = pathlib.Path(os.environ[secret_env_key])
    if not secret_dir.exists():
        logger.fatal(f"{secret_env_key} points to a non-existing directory ...")
        return None, None

    logger.info(f"Using notification secrets from environment variable: {secret_env_key}")
    return secret_dir, secret_env_key


def send_notification(
    message, github=True, slack=False, dry_run=False, pr_number=None, notification_vault=None
):
    """Send a generic notification message to GitHub and/or Slack.

    Args:
        message: The notification message content
        github: Whether to send to GitHub (default True)
        slack: Whether to send to Slack (default False)
        dry_run: Whether to only log the message without sending (default False)
        pr_number: Optional PR number, auto-detected if None
        notification_vault: Optional vault name to get notification secrets from

    Returns:
        bool: False if any notification failed, True if all succeeded
    """
    if pr_number is None:
        pr_number = get_pr_number()
    if not pr_number:
        logger.warning("PR number not available, cannot send the GH notification")
        github = False

    if not github_api:
        logger.info("Github API not available, don't send notification to github")
        github = False

    if os.environ.get("JOB_TYPE") == "periodic":
        logger.info("Running from a Periodic job, don't send notification to github")
        github = False

    secret_dir, secret_env_key = get_secrets(notification_vault)
    if secret_dir is None:
        if github:
            logger.error(
                "Cannot send GitHub notification: no secrets available (vault or environment variables)"
            )
        if slack:
            logger.error(
                "Cannot send Slack notification: no secrets available (vault or environment variables)"
            )
        return False

    failed = False
    if github and not send_notification_to_github(
        *get_github_secrets(secret_dir, secret_env_key),
        message,
        pr_number,
        dry_run,
    ):
        failed = True

    if slack and not send_notification_to_slack(
        get_slack_secrets(secret_dir, secret_env_key),
        message,
        pr_number,
        dry_run,
    ):
        failed = True

    return not failed


###


def send_notification_to_github(pem_file, client_id, message, pr_number, dry_run):
    """Send a generic notification message to GitHub."""
    org, repo = get_org_repo()

    abort = False

    if None in (pr_number,):
        logger.error("github: Cannot figure out the PR number")
        abort = True

    if None in (org, repo):
        logger.error("github: Cannot access the org/repo")
        abort = True

    if None in (pem_file, client_id):
        logger.error("github: Cannot access the secret files")
        abort = True

    if abort:
        logger.error("github: Aborting due to previous error(s).")
        return False

    user_token = github_api.get_user_token(pem_file, client_id, org, repo)
    if not user_token:
        logger.error("github: Couldn't fetch the user token. Is the app installed in the repo?")
        return False

    if dry_run:
        logger.info(f"Github notification:\n{message}")
        logger.info("***")
        logger.info("***")
        logger.info("***\n")

        return True

    resp = github_api.send_notification(org, repo, user_token, pr_number, message)

    if not resp.ok:
        logger.fatal(f"Github notification post failed :/ {resp.text}")

    return resp.ok


def get_github_notification_message(finish_reason: str, status: str, pr_number: int):
    def get_link(name, path, is_raw_file=False, base=None, is_dir=False):
        return f"[{name}]({get_ocpci_link(path, is_raw_file, base, is_dir)})"

    def get_italics(text):
        return f"*{text}*"

    def get_bold(text):
        return f"**{text}**"

    status_icon = ":green_circle:" if finish_reason == "success" else ":red_circle:"

    return get_common_message(
        finish_reason,
        f"{status_icon} {status} {status_icon}",
        get_link,
        get_italics,
        get_bold,
    )


def _get_notification_content(artifact_dir: pathlib.Path, get_link, get_bold) -> str:
    """
    Extract notification content from 000__ci_metadata/notifications/ directory

    Args:
        artifact_dir: Path to the artifact directory
        get_link: Function to format links
        get_bold: Function to format bold text

    Returns:
        Formatted notification content string
    """
    notifications_dir = ci_lib.get_ci_metadata_dir() / "notifications"
    failures_file = artifact_dir / "FAILURES.txt"

    # Guard: Check if notifications directory exists
    if not notifications_dir.exists():
        return _get_fallback_failure_content(failures_file, get_link, get_bold)

    # Guard: Check if there are any notification files
    notification_files = sorted(notifications_dir.glob("*.txt"))
    if not notification_files:
        return _get_fallback_failure_content(failures_file, get_link, get_bold)

    content = ""

    # Process each notification file
    for notification_file in notification_files:
        try:
            with open(notification_file, encoding="utf-8") as f:
                notification_content = f.read().strip()

            # Guard: Skip empty files
            if not notification_content:
                continue

            # Extract title from filename (strip prefix until __ and .txt extension)
            filename = notification_file.name
            if "__" in filename:
                title = filename.split("__", 1)[1].rsplit(".txt", 1)[0]
            else:
                title = filename.rsplit(".txt", 1)[0]

            # Replace underscores with spaces for better readability
            title = title.replace("_", " ")

            content += f"""
{get_bold(title)}:
"""
            # Format as quote blocks for better text wrapping
            quoted_content = "\n".join(
                f"> {line}" if line.strip() else ">" for line in notification_content.split("\n")
            )
            content += f"""
{quoted_content}
"""
        except Exception as e:
            logger.warning(f"Failed to read {notification_file}: {e}")
            content += f"""
• Error reading {notification_file.name}
"""

    # Add links to HTML reports
    html_report_file = artifact_dir / "failure_analysis_report.html"
    if html_report_file.exists():
        content += f"""
• {get_link("Detailed failure analysis report", "failure_analysis_report.html")}
"""

    config_review_file = artifact_dir / "config_review.html"
    if config_review_file.exists():
        content += f"""
• {get_link("Config review report", "config_review.html")}
"""

    # Add link to FAILURES file if it exists (but don't include content)
    if failures_file.exists():
        content += f"""
• {get_link("Raw failure details", "FAILURES.txt", is_raw_file=True)}
"""

    return content


def _get_fallback_failure_content(failures_file: pathlib.Path, get_link, get_bold) -> str:
    """
    Fallback to legacy FAILURES file processing when no notifications exist

    Args:
        failures_file: Path to the FAILURES file
        get_link: Function to format links
        get_bold: Function to format bold text

    Returns:
        Formatted failure content string or empty string
    """
    # Guard: Return early if FAILURES file doesn't exist
    if not failures_file.exists():
        return ""

    try:
        with open(failures_file) as f:
            lines = f.readlines()

        # Guard: Return early for empty files
        if not lines:
            return """
• Failure indicator: Empty.
"""

        DEFAULT_HEAD = 10
        try:
            head = lines.index("---\n")
        except ValueError:
            head = DEFAULT_HEAD

        return f"""
• {get_link("Failure indicator", "FAILURES.txt", is_raw_file=True)}:
```
{"".join(lines[:head])}
{"[...]" if len(lines) > head else ""}
```
"""
    except Exception as e:
        logger.warning(f"Failed to read FAILURES file: {e}")
        return f"""
• Error reading FAILURES file: {e}
"""


def get_common_message(finish_reason: str, status: str, get_link, get_italics, get_bold):
    message = ""

    message += f"""\
{get_bold(status)}
"""

    message += f"""
• Link to the {get_link("test results", "", is_dir=True)}.
"""
    # Check for Caliper postprocess status and generated reports
    artifact_dir = pathlib.Path(os.environ.get("ARTIFACT_DIR", ""))
    caliper_status_path = None

    # Search for POSTPROCESS_STATUS_FILENAME in artifact directory and subdirectories
    for status_file in artifact_dir.glob(f"**/{POSTPROCESS_STATUS_FILENAME}"):
        caliper_status_path = status_file
        break

    if caliper_status_path and caliper_status_path.exists():
        try:
            with open(caliper_status_path) as f:
                caliper_status = yaml.safe_load(f)

            visualize_step = caliper_status.get("steps", {}).get("visualize", {})
            if visualize_step.get("status") == "ok" and visualize_step.get("paths"):
                paths = visualize_step["paths"]
                message += f"""
• Generated {len(paths)} Caliper report(s):
"""
                for path in paths:
                    # Just list the path, no link
                    message += f"  - {path}\n"

                # Also link to the reports index if it exists
                reports_index_path = artifact_dir / "reports_index.html"
                if reports_index_path.exists():
                    message += f"""
• Link to the {get_link("reports index", "reports_index.html")}.
"""
            else:
                message += """
• Caliper postprocess completed but no reports generated.
"""
        except Exception as e:
            logger.warning("Failed to parse POSTPROCESS_STATUS_FILENAME: %s", e)
            message += """
• Failed to parse POSTPROCESS_STATUS_FILENAME ...
"""

    # Include fournos_launcher generated notification content
    fournos_notification_md = artifact_dir / "NOTIFICATION-github.md"
    if fournos_notification_md.exists():
        try:
            with open(fournos_notification_md, encoding="utf-8") as f:
                fournos_content = f.read().strip()
            if fournos_content:
                message += f"""
{fournos_content}
"""
        except Exception as e:
            logger.warning("Failed to read NOTIFICATION-github.md: %s", e)

    if (var_over := ci_lib.get_ci_metadata_dir() / "pr_config.txt").exists():
        with open(var_over) as f:
            message += f"""
{get_bold("Test configuration")}:
```
{f.read().strip()}
```
"""
    elif (var_over := ci_lib.get_ci_metadata_dir() / "variable_overrides.yaml").exists():
        with open(var_over) as f:
            message += f"""
{get_bold("Test configuration")}:
```
{f.read().strip()}
```
"""
    else:
        message += """
• No test configuration (`variable_overrides.yaml/pr_config.txt`) available.
"""

    # Get notification content from dedicated function
    artifact_dir = pathlib.Path(os.environ.get("ARTIFACT_DIR", ""))
    notification_content = _get_notification_content(artifact_dir, get_link, get_bold)
    message += notification_content

    message += "• " + get_link("Execution logs", "run.log", is_raw_file=True)

    return message


# Warning:
# Slack API messages format is different from the GUI
# https://api.slack.com/reference/surfaces/formatting


def get_slack_thread_message(finish_reason, status):
    def get_link(name, path, is_raw_file=False, base=None, is_dir=False):
        return f"<{get_ocpci_link(path, is_raw_file, base, is_dir)}|{name}>"

    def get_italics(text):
        return f"_{text}_"

    def get_bold(text):
        return f"*{text}*"

    status_icon = ":done-circle-check:" if finish_reason == "success" else ":no-red-circle:"

    # Check for fournos_launcher generated Slack notification content
    artifact_dir = pathlib.Path(os.environ.get("ARTIFACT_DIR", ""))
    fournos_notification_md = artifact_dir / "NOTIFICATION.md"

    if fournos_notification_md.exists():
        try:
            with open(fournos_notification_md, encoding="utf-8") as f:
                fournos_content = f.read().strip()
            if fournos_content:
                # Return custom notification content with status icon
                return f"{status_icon} {get_bold(status)}\n\n{fournos_content}"
        except (OSError, UnicodeDecodeError) as e:
            logger.warning(f"Failed to read {fournos_notification_md}: {e}")
            # Fall through to default message

    # Fallback to standard message if no custom notification
    return get_common_message(
        finish_reason, f"{status_icon} {status}", get_link, get_italics, get_bold
    )


def get_slack_channel_message(anchor: str, pr_data: dict):
    """Generates the Slack's notification main thread message."""

    org, repo = get_org_repo()

    message = f"🧵 {anchor}"

    if not pr_data:
        return message

    message += f"""

```{pr_data["title"]}```

Link to the <{pr_data["html_url"]}|PR>.
"""

    return message


def send_notification_to_slack(
    token,
    message,
    pr_number,
    dry_run,
):
    """Send a generic notification message to Slack."""
    if not token:
        return False

    client = slack_api.init_client(token)
    if not client:
        return False

    org, repo = get_org_repo()
    is_periodic = False
    pr_data = None
    pr_created_at = None

    if pr_number:
        if github_api:
            pr_created_at, pr_data = github_api.fetch_pr_data(org, repo, pr_number)

        anchor = f"Thread for PR #{pr_number}"
    elif os.environ.get("JOB_TYPE") == "periodic":
        periodic_name = os.environ["JOB_NAME_SAFE"]
        anchor = f"Thread for Periodic job `{periodic_name}`"
        is_periodic = True
    else:
        anchor = "Thread for tests without PRs"

    channel_msg_ts, channel_message = slack_api.search_channel_message(
        client, anchor, not_before=pr_created_at
    )

    if not channel_msg_ts:
        if is_periodic:
            channel_message = anchor
        else:
            channel_message = get_slack_channel_message(anchor, pr_data)

        if dry_run:
            logger.info("Posting Slack channel notification ...")
        else:
            channel_msg_ts, ok = slack_api.send_message(client, message=channel_message)
            if not ok:
                return False

    if dry_run:
        logger.info(f"Slack channel notification:\n{channel_message}")
        logger.info(f"Slack thread notification:\n{message}")
        logger.info("***")
        logger.info("***")
        logger.info("***\n")

        return True

    _, ok = slack_api.send_message(client, message=message, main_ts=channel_msg_ts)

    return ok


###


def get_pr_number():
    return os.environ.get("PULL_NUMBER")


# returns a tuple (base_link, link_suffix)
def get_ci_base_link(is_raw_file=False, is_dir=False):
    if os.environ.get("OPENSHIFT_CI") == "true":
        try:
            job_spec = json.loads(os.environ["JOB_SPEC"])
        except KeyError:
            logger.error("JOB_SPEC environment variable is not set")
            raise
        except json.JSONDecodeError as e:
            logger.error("Failed to parse JOB_SPEC as JSON: %s", e)
            raise
        test_name = os.environ["JOB_NAME_SAFE"]
        test_path = os.environ.get(
            "FORGE_OPENSHIFT_CI_STEP_DIR", "FORGE_OPENSHIFT_CI_STEP_DIR_missing"
        )
        job = job_spec["job"]
        build_id = job_spec["buildid"]

        if job_spec["type"] == "periodic":
            link_path = f"logs/{job}/{build_id}"

        else:
            pull_number = job_spec["refs"]["pulls"][0]["number"]
            github_org = job_spec["refs"]["org"]
            github_repo = job_spec["refs"]["repo"]

            link_path = f"pr-logs/pull/{github_org}_{github_repo}/{pull_number}/{job}/{build_id}"

        return (
            "https://gcsweb-ci.apps.ci.l2s4.p1.openshiftapps.com/gcs/test-platform-results/"
            + link_path
            + f"/artifacts/{test_name}/{test_path}",
            "",
        )

    else:
        logger.warning(
            "Test not running from a well-known CI engine, cannot extract the artifacts link."
        )

        return "https://no_known_ci_engine/", "?no_ext=true"


def get_org_repo():
    return os.environ.get("REPO_OWNER", DEFAULT_REPO_OWNER), os.environ.get(
        "REPO_NAME", DEFAULT_REPO_NAME
    )


def get_github_secrets(secret_dir, secret_env_key):
    pem_file = secret_dir / GITHUB_APP_PEM_FILE
    client_id_file = secret_dir / GITHUB_APP_CLIENT_ID_FILE

    if not pem_file.exists():
        logger.warning(f"Github App private key does not exists ({pem_file}) in {secret_env_key}")
        return None, None

    if not client_id_file.exists():
        logger.warning(
            f"Github App clientid file does not exists ({client_id_file}) in {secret_env_key}"
        )
        return None, None

    client_id_content = client_id_file.read_text().strip()

    return pem_file, client_id_content


def get_slack_secrets(secret_dir, secret_env_key):
    token_file = secret_dir / SLACK_TOKEN_FILE

    if not token_file.exists():
        logger.warning(
            f"{token_file.name} not found in {secret_env_key}. Cannot send the Slack notification"
        )
        return None

    return token_file.read_text()


def get_ocpci_link(path, is_raw_file=False, base=None, is_dir=False):
    if base is None:
        base, suffix = get_ci_base_link(is_raw_file, is_dir)
    else:
        suffix = None

    link = base + (f"/{path}" if path else "") + (suffix if suffix else "")

    return link


""" # example of a regression_summary file:
entries_count: 3
failures: 0
kpis_count: 2
message: Performed 6 KPI regression analyses over 3 entries x 2 KPIs. 0 KPIs didn't
  pass.
no_history: 0
not_analyzed: 0
significant_performance_increase: 0
total_points: 6
"""


def send_cpt_notification(regression_summary_path, title, slack, dry_run):
    summary_path = pathlib.Path(regression_summary_path)
    if not summary_path.exists():
        logger.fatal(f"Regression summary doesn't exist :/ ({regression_summary_path})")
        return True

    try:
        with open(summary_path) as f:
            summary = yaml.safe_load(f)
    except Exception as e:
        logger.fatal(f"Failed to load regression summary: {e}")
        return True

    secret_dir, secret_env_key = get_secrets(None)
    if secret_dir is None:
        return True

    failed = False
    if slack:
        failed = send_cpt_notification_to_slack(secret_dir, secret_env_key, title, summary, dry_run)

    return failed


def send_cpt_notification_to_slack(secret_dir, secret_env_key, title, summary, dry_run):
    token = get_slack_secrets(secret_dir, secret_env_key)
    if not token:
        return True

    client = slack_api.init_client(token)
    if not client:
        logger.fatal("Couldn't get the slack client ...")
        return True
    safe_title = html.escape(title)
    channel_msg_ts, channel_message = slack_api.search_channel_message(client, safe_title)

    if not channel_msg_ts:
        channel_message = f"🧵 Thread for `{title}` continuous performance testing"
        if dry_run:
            logger.info(f"Posting Slack channel notification ...\n{channel_message}")
        else:
            channel_msg_ts, channel_ok = slack_api.send_message(client, message=channel_message)

    try:
        thread_message = get_slack_cpt_message(summary)
    except Exception as e:
        logger.fatal(f"Failed to generate the slack notification message: {e}")
        return True

    if dry_run:
        logger.info(f"Posting Slack thread notification ...\n{thread_message}")
        ok = True
    else:
        _, ok = slack_api.send_message(client, message=thread_message, main_ts=channel_msg_ts)

    return not ok


def get_slack_cpt_message(summary):
    def get_link(name, path, is_raw_file=False, base=None, is_dir=False):
        return f"<{get_ocpci_link(path, is_raw_file, base, is_dir)}|{name}>"

    def get_italics(text):
        return f"_{text}_"

    def get_bold(text):
        return f"*{text}*"

    status_icon = ":no-red-circle:" if summary.get("failures") else ":done-circle-check:"

    reports_index_link = ""
    if (pathlib.Path(os.environ.get("ARTIFACT_DIR", "")) / "reports_index.html").exists():
        reports_index_link = f"• Link to the {get_link('reports index', 'reports_index.html')}.\n"

    return f"""{status_icon} {get_bold(summary["message"])}

• Link to the {get_link("test results", "", is_dir=True)}.
{reports_index_link}
- `{summary["entries_count"]}` entries were tested against `{summary["kpis_count"]}` KPIs
- `{summary["failures"]}` failed
- `{summary["no_history"]}` had no history
- `{summary["not_analyzed"]}` were not analyzed
- `{summary["significant_performance_increase"]}` had a significant performance degradation
- `{summary["total_points"]}` points were checked for regression.
    """
