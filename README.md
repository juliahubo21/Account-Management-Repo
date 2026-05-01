# Account Management Daily Scanner

A Python script that runs daily at 10:00 America/New_York via GitHub Actions, scans the Affinity list **Account Management Master**, and posts new interactions to Slack `#account-management`.

## What it surfaces

- **Upcoming meetings** with tracked accounts in the next 21 days.
- **Recent emails** to/from tracked accounts in the past 24 hours.
- **Recent notes** on tracked accounts in the past 24 hours.

For an interaction to be posted, it must have at least one Motive Partners participant (email domain `@motivepartners.com`) AND at least one account-side participant (any other domain).

## Slack message format

```
📅 New Meeting: Goldman Sachs   ← clickable link to motivepartners.affinity.co/companies/<id>
Date: Monday 18th May 2026 at 10:00 AM EDT
Subject: Goldman Sachs / Motive
Participants:
 • Motive Partners: Mike Campbell, Ramin Niroumand
 • Goldman Sachs: Tucker York, Julian Salisbury
<!-- key: meeting:6823177053 -->
```

Emails and notes use the same template with `📧 New Email:` instead of `📅 New Meeting:`. The trailing `<!-- key: ... -->` line is the dedup marker — read by the next run from Slack channel history so we don't repost.

## Architecture

Pure Python, no LLM in the loop:
- `scripts/scan.py` — the scanner. Calls Affinity REST API V1 directly for lists, list entries, meetings, notes, and emails (`/interactions?type=3`). Calls Slack Web API for channel resolution, history (dedup), and message posting.
- `requirements.txt` — just `requests`.
- `.github/workflows/account-mgmt-scan.yml` — daily cron + `workflow_dispatch` for manual runs.

## Required GitHub repo secrets

| Secret | Purpose |
| --- | --- |
| `AFFINITY_API_KEY` | Affinity API key (Settings → API in Affinity) |
| `SLACK_BOT_TOKEN`  | Bot token (`xoxb-…`) for the Slack app, invited to `#account-management`. Required scopes: `channels:history`, `channels:read`, `chat:write` (plus `groups:history` if the channel is private) |

## Schedule and DST

The cron triggers at both **14:00 UTC** and **15:00 UTC** every day, but the first step gates on `TZ=America/New_York date +%H == 10`, so only one cron actually runs the scan per day. This handles DST automatically without configuration changes.

`workflow_dispatch` (manual run via the Actions UI) bypasses the time gate.

## Running manually

GitHub UI: Actions → **Account Management Daily Scan** → **Run workflow** → green button.

## Local testing

```bash
pip install -r requirements.txt
export AFFINITY_API_KEY=...
export SLACK_BOT_TOKEN=xoxb-...
export SLACK_CHANNEL_NAME=account-management
python scripts/scan.py
```

The script prints its window calculations, list ID, channel ID, dedup key count, and a per-post log line, finishing with a summary of `posted / skipped (dedup) / skipped (no Motive↔account pair) / errors`.

## Operations

- **Token refresh.** Watch for Affinity / Slack auth failures in the workflow logs. Bot tokens don't auto-expire; Affinity keys rotate per your firm's policy.
- **Inactive-repo cron suspension.** GitHub disables scheduled workflows on repos that haven't been pushed to in 60 days. Any commit (a comment tweak) keeps it active.
- **Failed runs don't auto-retry.** If a single day's run fails, missed interactions for that day stay missed until the next successful run, since the dedup key is what guarantees uniqueness — not the schedule.

## Editing behavior

Behavior changes (different list, different window, different format, different participant rules) are made by editing `scripts/scan.py` and pushing. Constants at the top of the file (`LIST_NAME`, `MOTIVE_DOMAIN`, `MEETING_AHEAD_DAYS`, `EMAIL_NOTE_BACK_HOURS`, `WORKSPACE`) are the most common knobs.
