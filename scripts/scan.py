#!/usr/bin/env python3
"""
Account Management daily scanner.

Reads the "Account Management Master" list from Affinity, finds upcoming
meetings (next 21 days) and recent emails/notes (past 24 hours) involving
both Motive Partners and the tracked accounts, and posts each new
interaction to Slack #account-management with a strict format.

Dedup: parses <!-- key: meeting:<id> -->, <!-- key: note:<id> -->, and
<!-- key: email:<id> --> markers from the last 200 messages of channel
history. Items already posted are skipped.

Env vars (all required):
    AFFINITY_API_KEY    Affinity REST API key (V1)
    SLACK_BOT_TOKEN     Slack bot token (xoxb-...)
    SLACK_CHANNEL_NAME  Channel to post to (e.g. "account-management")

Exit codes:
    0  scan completed without per-item errors
    1  fatal config / setup error (list not found, channel not found, etc.)
    2  scan completed but one or more per-item errors occurred
"""
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

AFFINITY_BASE = "https://api.affinity.co"
SLACK_BASE = "https://slack.com/api"

LIST_NAME = "Account Management Master"
MOTIVE_DOMAIN = "motivepartners.com"
WORKSPACE = "motivepartners"
MEETING_AHEAD_DAYS = 21
EMAIL_NOTE_BACK_HOURS = 24
SLACK_HISTORY_LIMIT = 200

NY_TZ = ZoneInfo("America/New_York")

AFFINITY_KEY = os.environ["AFFINITY_API_KEY"]
SLACK_TOKEN = os.environ["SLACK_BOT_TOKEN"]
CHANNEL_NAME = os.environ["SLACK_CHANNEL_NAME"]
# Optional: skip channel listing entirely if the ID is already known. Useful
# for private channels where the bot doesn't have groups:read scope.
CHANNEL_ID_OVERRIDE = os.environ.get("SLACK_CHANNEL_ID") or None

aff = requests.Session()
aff.headers.update({
    "Authorization": f"Bearer {AFFINITY_KEY}",
    "X-Affinity-Api-Version": "2024-01-01",
    "Accept": "application/json",
})

slk = requests.Session()
slk.headers["Authorization"] = f"Bearer {SLACK_TOKEN}"


def log(msg):
    print(msg, flush=True)


def warn(msg):
    print(f"[warn] {msg}", file=sys.stderr, flush=True)


def aff_get(path, **params):
    r = aff.get(f"{AFFINITY_BASE}{path}", params=params, timeout=30)
    if r.status_code != 200:
        warn(f"Affinity GET {path} {params} → HTTP {r.status_code}: {r.text[:300]}")
        return None
    try:
        return r.json()
    except Exception as e:
        warn(f"Affinity GET {path}: invalid JSON: {e}: body={r.text[:300]}")
        return None


def slk_get(method, **params):
    r = slk.get(f"{SLACK_BASE}/{method}", params=params, timeout=30)
    data = r.json()
    if not data.get("ok"):
        warn(f"Slack GET {method} failed: {data.get('error')}")
        return None
    return data


def slk_post(method, payload):
    r = slk.post(
        f"{SLACK_BASE}/{method}",
        json=payload,
        timeout=30,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    data = r.json()
    if not data.get("ok"):
        warn(f"Slack POST {method} failed: {data.get('error')} body={payload}")
        return None
    return data


def find_list_id(name):
    data = aff_get("/lists")
    if data is None:
        return None
    items = data if isinstance(data, list) else data.get("lists", [])
    for lst in items:
        if lst.get("name") == name:
            return lst.get("id")
    return None


def get_list_entries(list_id):
    out, page_token = [], None
    while True:
        params = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token
        data = aff_get(f"/lists/{list_id}/list-entries", **params)
        if data is None:
            return out
        if isinstance(data, list):
            out.extend(data)
            return out
        out.extend(data.get("list_entries", []))
        page_token = data.get("next_page_token")
        if not page_token:
            return out


def find_channel_id(name):
    cursor = None
    while True:
        params = {"limit": 1000, "types": "public_channel,private_channel"}
        if cursor:
            params["cursor"] = cursor
        data = slk_get("conversations.list", **params)
        if not data:
            return None
        for ch in data.get("channels", []):
            if ch.get("name") == name:
                return ch["id"]
        cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return None


KEY_RE = re.compile(r"<!--\s*key:\s*(meeting|note|email):(\d+)\s*-->")


def load_already_posted(channel_id):
    data = slk_get("conversations.history", channel=channel_id, limit=SLACK_HISTORY_LIMIT)
    if not data:
        return set()
    keys = set()
    for msg in data.get("messages", []):
        for m in KEY_RE.finditer(msg.get("text", "")):
            keys.add(f"{m.group(1)}:{m.group(2)}")
    return keys


def parse_iso(s):
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def ny_format(dt_utc):
    """Format UTC datetime as 'Friday 1st May 2026 at 3:00 PM EDT'."""
    dt_ny = dt_utc.astimezone(NY_TZ)
    day = dt_ny.day
    if 11 <= day <= 13:
        suffix = "th"
    elif day % 10 == 1:
        suffix = "st"
    elif day % 10 == 2:
        suffix = "nd"
    elif day % 10 == 3:
        suffix = "rd"
    else:
        suffix = "th"
    return dt_ny.strftime(f"%A {day}{suffix} %B %Y at %-I:%M %p %Z")


def classify(email):
    if not email or "@" not in email:
        return "drop"
    domain = email.split("@", 1)[1].lower().strip()
    if domain == MOTIVE_DOMAIN:
        return "motive"
    return "account"


def normalize_persons(raw):
    """Turn a heterogeneous person list into [{name, email}], dropping incomplete entries.

    Handles V1 snake_case (first_name, last_name, emails) and V2 camelCase
    (firstName, lastName, emailAddresses).
    """
    out = []
    for p in raw or []:
        if not isinstance(p, dict):
            continue
        first = (p.get("first_name") or p.get("firstName") or "").strip()
        last = (p.get("last_name") or p.get("lastName") or "").strip()
        name = f"{first} {last}".strip() or (p.get("name") or "").strip()

        emails = (
            p.get("emails")
            or p.get("emailAddresses")
            or p.get("email_addresses")
        )
        if isinstance(emails, list) and emails:
            first_entry = emails[0]
            email = first_entry if isinstance(first_entry, str) else first_entry.get("address") or first_entry.get("email")
        else:
            email = p.get("email")
            if isinstance(email, dict):
                email = email.get("address") or email.get("email")

        if not name or not email:
            continue
        out.append({"name": name, "email": email})
    return out


def split_participants(raw_persons):
    """Return ([motive_names], [account_names]) from a raw person list, dedup by email."""
    motive, account, seen = [], [], set()
    for p in normalize_persons(raw_persons):
        key = p["email"].lower()
        if key in seen:
            continue
        seen.add(key)
        side = classify(p["email"])
        if side == "motive":
            motive.append(p["name"])
        elif side == "account":
            account.append(p["name"])
    return motive, account


def iso_z(dt):
    """ISO 8601 with Z suffix and no microseconds (Affinity's expected format)."""
    return dt.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


# Keys we've seen Affinity wrap a list under, across V1 and V2 endpoints.
LIST_WRAPPER_KEYS = ("data", "interactions", "meetings", "emails", "events",
                     "notes", "results", "items", "list_entries")


def extract_list(data, debug_label):
    """Pull a list out of a heterogeneous Affinity response.

    Affinity sometimes returns a bare list, sometimes a {"<key>": [...],
    "next_page_token": ...} envelope where <key> varies by endpoint
    (interactions, meetings, emails, data, etc.). If we can't find a list,
    log the actual shape and return [] rather than iterating dict keys
    (which would raise 'str' has no attribute 'get').
    """
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in LIST_WRAPPER_KEYS:
            v = data.get(key)
            if isinstance(v, list):
                return v
        warn(f"{debug_label}: unexpected response shape; "
             f"top-level keys: {list(data.keys())[:10]}")
        return []
    warn(f"{debug_label}: response is neither list nor dict: {type(data).__name__}")
    return []


def fetch_interactions(org_id, type_int, win_start, win_end):
    """V1 /interactions endpoint. type_int: 0 = meeting, 3 = email."""
    data = aff_get(
        "/interactions",
        type=type_int,
        organization_id=org_id,
        start_time=iso_z(win_start),
        end_time=iso_z(win_end),
        page_size=100,
    )
    return extract_list(data, f"/interactions type={type_int} org={org_id}")


DATE_FIELDS = ("start_time", "startTime", "date", "sent_at", "sentAt",
               "timestamp", "occurred_at", "occurredAt", "scheduled_for",
               "scheduledFor", "created_at", "createdAt")


def first_iso(item):
    """Find the first parseable ISO timestamp across known field names."""
    for f in DATE_FIELDS:
        v = item.get(f) if isinstance(item, dict) else None
        if v:
            dt = parse_iso(v)
            if dt:
                return dt
    return None


_logged_shape_for = set()


def log_first_item_shape(label, items):
    """One-time per-label log of the first item's top-level keys for diagnostics."""
    if label in _logged_shape_for or not items:
        return
    if isinstance(items[0], dict):
        log(f"[shape] {label} item keys: {sorted(items[0].keys())[:30]}")
    _logged_shape_for.add(label)


def fetch_meetings(org_id, win_start, win_end):
    items = fetch_interactions(org_id, 0, win_start, win_end)
    log_first_item_shape("meetings", items)
    out = []
    for m in items:
        start = first_iso(m)
        if not start:
            continue
        out.append({
            "type": "meeting",
            "id": m.get("id"),
            "subject": m.get("title") or m.get("subject") or "(no subject)",
            "start": start,
            "raw_persons": (
                m.get("attendees")
                or m.get("participants")
                or m.get("persons")
                or m.get("attendee_persons")
                or m.get("attendeePersons")
                or []
            ),
        })
    return out


def fetch_emails(org_id, win_start, win_end):
    items = fetch_interactions(org_id, 3, win_start, win_end)
    log_first_item_shape("emails", items)
    out = []
    for e in items:
        sent = first_iso(e)
        if not sent:
            continue
        raw = []
        for f in ("from", "to", "cc", "participants", "persons"):
            val = e.get(f)
            if isinstance(val, dict):
                raw.append(val)
            elif isinstance(val, list):
                raw.extend(val)
        out.append({
            "type": "email",
            "id": e.get("id"),
            "subject": e.get("subject") or "(no subject)",
            "start": sent,
            "raw_persons": raw,
        })
    return out


def fetch_notes(org_id, win_start, win_end):
    """V2 /v2/companies/{id}/notes endpoint. Filters by createdAt client-side."""
    data = aff_get(f"/v2/companies/{org_id}/notes")
    items = extract_list(data, f"/v2/companies/{org_id}/notes")
    log_first_item_shape("notes", items)
    out = []
    for n in items:
        created = first_iso(n)
        if not created or not (win_start <= created <= win_end):
            continue
        raw = []
        if n.get("creator"):
            raw.append(n["creator"])
        for p in n.get("associatedPersons") or n.get("persons") or []:
            raw.append(p)
        content = (n.get("content") or n.get("contentText") or "").replace("\n", " ").strip()
        out.append({
            "type": "note",
            "id": n.get("id"),
            "subject": content[:80] or "(note)",
            "start": created,
            "raw_persons": raw,
        })
    return out


def build_message(item, account_name, company_id):
    url = f"https://{WORKSPACE}.affinity.co/companies/{company_id}"
    when = ny_format(item["start"])
    motive_names = ", ".join(item["motive"])
    account_names = ", ".join(item["account"])
    head_emoji, head_label = (
        ("📅", "New Meeting") if item["type"] == "meeting"
        else ("📧", "New Email")
    )
    return (
        f"{head_emoji} {head_label}: <{url}|{account_name}>\n"
        f"Date: {when}\n"
        f"Subject: {item['subject']}\n"
        f"Participants:\n"
        f" • Motive Partners: {motive_names}\n"
        f" • {account_name}: {account_names}\n"
        f"<!-- key: {item['type']}:{item['id']} -->"
    )


def main():
    now = datetime.now(timezone.utc)
    meeting_window = (now, now + timedelta(days=MEETING_AHEAD_DAYS))
    en_window = (now - timedelta(hours=EMAIL_NOTE_BACK_HOURS), now)

    log(f"Now (UTC): {now.isoformat()}")
    log(f"Meeting window: {meeting_window[0].isoformat()} → {meeting_window[1].isoformat()}")
    log(f"Email/note window: {en_window[0].isoformat()} → {en_window[1].isoformat()}")

    list_id = find_list_id(LIST_NAME)
    if not list_id:
        warn(f"Affinity list '{LIST_NAME}' not found")
        return 1
    log(f"Affinity list '{LIST_NAME}' id={list_id}")

    if CHANNEL_ID_OVERRIDE:
        channel_id = CHANNEL_ID_OVERRIDE
        log(f"Slack channel '{CHANNEL_NAME}' id={channel_id} (from SLACK_CHANNEL_ID env)")
    else:
        channel_id = find_channel_id(CHANNEL_NAME)
        if not channel_id:
            warn(f"Slack channel '{CHANNEL_NAME}' not found via conversations.list "
                 f"— set SLACK_CHANNEL_ID env to the channel ID to skip listing "
                 f"(needed for private channels without groups:read scope)")
            return 1
        log(f"Slack channel '{CHANNEL_NAME}' id={channel_id}")

    already = load_already_posted(channel_id)
    log(f"Already posted (from last {SLACK_HISTORY_LIMIT} channel messages): {len(already)} keys")

    entries = get_list_entries(list_id)
    log(f"List entries: {len(entries)}")

    posted_this_run = set()
    posted_count = 0
    skipped_dedup = 0
    skipped_nopair = 0
    error_count = 0

    for entry in entries:
        # V1: list entry has top-level entity_type (1=organization), entity_id, and entity{}.
        if entry.get("entity_type") not in (1, "organization"):
            continue
        entity = entry.get("entity") or {}
        org_id = entity.get("id") or entry.get("entity_id")
        account_name = entity.get("name") or f"org #{org_id}"
        if not org_id:
            continue

        try:
            items = (
                fetch_meetings(org_id, *meeting_window)
                + fetch_notes(org_id, *en_window)
                + fetch_emails(org_id, *en_window)
            )
        except Exception as e:
            warn(f"{account_name} ({org_id}): fetch failed: {e}")
            error_count += 1
            continue

        for item in items:
            key = f"{item['type']}:{item['id']}"
            if key in already or key in posted_this_run:
                skipped_dedup += 1
                continue
            motive, account = split_participants(item["raw_persons"])
            if not motive or not account:
                skipped_nopair += 1
                continue
            item["motive"] = motive
            item["account"] = account
            text = build_message(item, account_name, org_id)
            res = slk_post("chat.postMessage", {
                "channel": channel_id,
                "text": text,
                "mrkdwn": True,
            })
            if res:
                posted_count += 1
                posted_this_run.add(key)
                log(f"posted {key} → {account_name} :: {item['subject'][:60]}")
            else:
                error_count += 1

    log("--- summary ---")
    log(f"posted: {posted_count}")
    log(f"skipped (already in slack history): {skipped_dedup}")
    log(f"skipped (no Motive↔account participant pair): {skipped_nopair}")
    log(f"errors: {error_count}")
    return 0 if error_count == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
