# TOPSAIL-NG Notifications Integration

This directory contains the notifications system that integrates with the run_ci entrypoint to send success/failure notifications.

## Overview

The notifications system automatically sends notifications when CI operations complete, supporting both GitHub PR comments and Slack messages.

## Integration Points

### CI Entrypoint Integration

The notifications are automatically triggered from `projects/core/ci_entrypoint/prepare_ci.py` in the `postchecks()` function, which is called after every CI operation through `run_ci.py`.

### Supported Platforms

1. **GitHub**: Posts comments to PR threads using GitHub App authentication
2. **Slack**: Posts messages to configured Slack channels with intelligent threading

## Configuration

### Environment Variables

- `TOPSAIL_NOTIFICATION_DRY_RUN=true/false`: Enable dry run mode (shows what would be sent without actually sending)
- `TOPSAIL_ENABLE_SLACK_NOTIFICATIONS=true/false`: Enable/disable Slack notifications (default: false)
- GitHub notifications are enabled by default when appropriate secrets are available

### Required Secrets

The system looks for secret files in directories specified by these environment variables (in order):
- `PSAP_ODS_SECRET_PATH`
- `CRC_MAC_AI_SECRET_PATH`
- `CONTAINER_BENCH_SECRET_PATH`

**GitHub App secrets:**
- `topsail-bot.2024-09-18.private-key.pem`: GitHub App private key
- `topsail-bot.clientid`: GitHub App client ID

**Slack secrets:**
- `topsail-bot.slack-token`: Slack bot token

### Enable Slack Notifications

```bash
export TOPSAIL_ENABLE_SLACK_NOTIFICATIONS=true
run my_project test
```

## Architecture

```
run_ci.py
├── prepare_ci.prepare() (before execution)
├── [CI operation execution]
└── prepare_ci.postchecks() (after execution)
    └── send_job_completion_notification()
        ├── GitHub: send_job_completion_notification_to_github()
        └── Slack: send_job_completion_notification_to_slack()
```

## Message Format

### Success Notifications
- GitHub: Green circle emoji with success message and links to artifacts
- Slack: Check mark emoji with formatted success details

### Failure Notifications
- GitHub: Red circle emoji with failure details and failure logs
- Slack: Error emoji with failure summary and artifact links

Both formats include:
- Test configuration (from variable_overrides.yaml)
- Links to test results and reports
- Failure details (if applicable)
- Execution duration
- CI environment context

## Dependencies

- `requests`: For GitHub API calls
- `slack_sdk`: For Slack API integration
- `pyjwt[crypto]`: For GitHub App JWT authentication

## Files

- `send.py`: Main notifications controller
- `github/api.py`: GitHub API integration
- `github/gen_jwt.py`: GitHub App JWT token generation
- `slack/api.py`: Slack API integration
