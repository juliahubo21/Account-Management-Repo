#!/usr/bin/env python3
"""
Account Management weekly summary.

Posts THREE Slack messages every Friday, each prefixed with the same header:
  1. PAST INTERACTIONS         (last 7 days, meetings only)
  2. UPCOMING INTERACTIONS     (next 7 days, meetings only)
  3. NO INTERACTIONS           (accounts with nothing in either window)

If any single section exceeds the Slack display threshold, it is split into
multiple messages at company-block boundaries (never mid-company).

Env vars:
    AFFINITY_API_KEY      Affinity REST API key (V1)
    SLACK_BOT_TOKEN       Slack bot token (xoxb-...)
    SLACK_CHANNEL_NAME    Channel name (default: account-management)
    SLACK_CHANNEL_ID      (optional) Channel ID to skip conversations.list
"""
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

# ── Config ────────────────────────────────────────────────────────────────────
AFFINITY_BASE = "https://api.affinity.co"
SLACK_BASE    = "https://slack.com/api"
LIST_NAME     = "Account Management Master"
AFFINITY_URL  = "https://motivepartners.affinity.co"
MOTIVE_DOMAIN = "motivepartners.com"
ROOM_ALIASES  = {"nyreception@motivepartners.com"}
PAST_DAYS     = 7
UPCOMING_DAYS = 7
NY_TZ         = ZoneInfo("America/New_York")
DIVIDER       = "─" * 26
MAX_TITLE     = 80
MAX_EXT_NAMES = 5
OWNERS_FIELD_FALLBACK = 5617247
# Slack splits messages over ~4000 chars in the UI; use a safe threshold
# below that and split at clean boundaries to avoid mid-company cuts.
SLACK_MSG_LIMIT = 3500

AFFINITY_KEY        = os.environ["AFFINITY_API_KEY"]
SLACK_TOKEN         = os.environ["SLACK_BOT_TOKEN"]
CHANNEL_NAME        = os.environ.get("SLACK_CHANNEL_NAME", "account-management")
CHANNEL_ID_OVERRIDE = os.environ.get("SLACK_CHANNEL_ID") or None

# ── HTTP sessions ─────────────────────────────────────────────────────────────
aff = requests.Session()
aff.headers.update({
    "Authorization": f"Bearer {AFFINITY_KEY}",
    "Accept": "application/json",
})

slk = requests.Session()
slk.headers["Authorization"] = f"Bearer {SLACK_TOKEN}"


def log(msg):  print(msg, flush=True)
def warn(msg): print(f"[warn] {msg}", file=sys.stderr, flush=True)


# ── Affinity helpers ──────────────────────────────────────────────────────────
def aff_get(path, **params):
    r = aff.get(f"{AFFINITY_BASE}{path}", params=params, timeout=30)
    if r.status_code != 200:
        warn(f"Affinity GET {path} -> {r.status_code}: {r.text[:200]}")
        return None
    try:
        return r.json()
    except Exception as e:
        warn(f"Affinity GET {path}: JSON error: {e}")
        return None


LIST_KEYS = (
    "data", "interactions", "meetings", "emails", "events",
    "list_entries", "field_values", "results", "items",
    "fields", "lists", "persons",
)


def unpack(data, label=""):
    if data is None: return []
    if isinstance(data, list): return data
    if isinstance(data, dict):
        for k in LIST_KEYS:
            v = data.get(k)
            if isinstance(v, list): return v
        warn(f"{label}: unexpected response keys: {list(data.keys())[:6]}")
    return []


def parse_iso(s):
    if not s: return None
    if isinstance(s, (int, float)):
        return datetime.fromtimestamp(s, tz=timezone.utc)
    s = str(s)
    if s.endswith("Z"): s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def iso_z(dt):
    return dt.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Slack helpers ─────────────────────────────────────────────────────────────
def slk_get(method, **params):
    r = slk.get(f"{SLACK_BASE}/{method}", params=params, timeout=30)
    data = r.json()
    if not data.get("ok"):
        warn(f"Slack GET {method}: {data.get('error')}")
        return None
    return data


def slk_post(method, payload):
    r = slk.post(
        f"{SLACK_BASE}/{method}", json=payload, timeout=30,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    data = r.json()
    if not data.get("ok"):
        warn(f"Slack POST {method}: {data.get('error')} — body={str(payload)[:200]}")
        return None
    return data


def find_channel_id(name):
    cursor = None
    while True:
        params = {"limit": 1000, "types": "public_channel,private_channel"}
        if cursor: params["cursor"] = cursor
        data = slk_get("conversations.list", **params)
        if not data: return None
        for ch in data.get("channels", []):
            if ch.get("name") == name: return ch["id"]
        cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not cursor: return None


# ── Slack mrkdwn formatting ────────────────────────────────────────────────────
def slk_escape(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def slk_link(url, label):
    safe = (label or "").replace("|", "/")
    return f"<{url}|{slk_escape(safe)}>"


def co_url(co_id):
    return f"{AFFINITY_URL}/companies/{co_id}"


def co_link_bold(name, co_id):
    return f"*{slk_link(co_url(co_id), name)}*"


def co_link_plain(name, co_id):
    return slk_link(co_url(co_id), name)


# ── List / entry helpers ──────────────────────────────────────────────────────
def find_list_id(name):
    data = aff_get("/lists")
    for lst in unpack(data, "/lists"):
        if lst.get("name") == name: return lst["id"]
    return None


def get_list_entries(list_id):
    out, token = [], None
    while True:
        p = {"page_size": 500}
        if token: p["page_token"] = token
        data = aff_get(f"/lists/{list_id}/list-entries", **p)
        if data is None: return out
        items = data if isinstance(data, list) else data.get("list_entries", [])
        out.extend(items)
        token = None if isinstance(data, list) else data.get("next_page_token")
        if not token: return out


def find_owners_field_id(list_id):
    data = aff_get("/fields", list_id=list_id)
    for f in unpack(data, "/fields"):
        if (f.get("name") or "").lower() in ("owners", "owner", "deal owner"):
            log(f"Found owners field: id={f['id']} name={f['name']}")
            return f["id"]
    log(f"Owners field not found dynamically; using fallback {OWNERS_FIELD_FALLBACK}")
    return OWNERS_FIELD_FALLBACK


# ── Person / owner caches ──────────────────────────────────────────────────────
_person_cache = {}
_owners_cache = {}


def get_person_name(person_id):
    if person_id is None: return ""
    if person_id in _person_cache:
        return _person_cache[person_id]
    data = aff_get(f"/persons/{person_id}")
    if not data:
        _person_cache[person_id] = ""
        return ""
    first = (data.get("first_name") or "").strip()
    last  = (data.get("last_name")  or "").strip()
    name  = f"{first} {last}".strip()
    _person_cache[person_id] = name
    return name


def get_owners_for_entry(entry_id, owners_field_id):
    if not entry_id or not owners_field_id:
        return []
    if entry_id in _owners_cache:
        return _owners_cache[entry_id]
    data = aff_get("/field-values", list_entry_id=entry_id)
    owners = []
    for fv in unpack(data, f"/field-values entry={entry_id}"):
        if str(fv.get("field_id")) != str(owners_field_id):
            continue
        value = fv.get("value")
        ids = []
        if isinstance(value, list):
            ids = [v.get("id") if isinstance(v, dict) else v for v in value]
        elif isinstance(value, dict):
            ids = [value.get("id") or value.get("person_id")]
        elif value:
            ids = [value]
        for pid in ids:
            if pid:
                name = get_person_name(pid)
                if name: owners.append(name)
    _owners_cache[entry_id] = owners
    return owners


# ── Participant handling ──────────────────────────────────────────────────────
def classify(email):
    if not email or "@" not in email: return "drop"
    if email.lower() in ROOM_ALIASES:  return "drop"
    return "motive" if email.split("@", 1)[1].lower() == MOTIVE_DOMAIN else "external"


def name_from_email(email):
    local = email.split("@")[0]
    return local.replace(".", " ").replace("_", " ").title()


def normalize_persons(raw):
    out = []
    for p in (raw or []):
        if isinstance(p, str):
            if "@" in p:
                out.append({"name": name_from_email(p), "email": p.lower()})
        elif isinstance(p, dict):
            first = (p.get("first_name") or p.get("firstName") or "").strip()
            last  = (p.get("last_name")  or p.get("lastName")  or "").strip()
            name  = f"{first} {last}".strip() or p.get("name", "")
            emails = p.get("emails") or p.get("emailAddresses") or []
            email = ""
            if isinstance(emails, list) and emails:
                e0 = emails[0]
                email = e0 if isinstance(e0, str) else (e0.get("address") or e0.get("email") or "")
            email = email or p.get("primary_email") or p.get("primaryEmail") or ""
            if isinstance(email, dict): email = email.get("address", "")
            email = (email or "").lower()
            if not name and email:
                name = name_from_email(email)
            if name or email:
                out.append({"name": name, "email": email})
    return out


def split_attendees(raw_persons):
    motive, external, seen = [], [], set()
    for p in normalize_persons(raw_persons):
        key = p["email"] or p["name"]
        if key in seen: continue
        seen.add(key)
        cls = classify(p["email"])
        if cls == "drop":   continue
        if cls == "motive": motive.append(p["name"])
        else:               external.append(p["name"])
    return motive, external


def has_motive(item):
    return any(
        classify(p["email"]) == "motive"
        for p in normalize_persons(item["raw"])
    )


# ── Meeting fetching ─────────────────────────────────────────────────────────
DATE_FIELDS = (
    "start_time", "startTime", "date", "sent_at", "sentAt",
    "occurred_at", "occurredAt", "scheduled_for", "scheduledFor",
    "timestamp", "created_at", "createdAt",
)


def first_dt(item):
    for f in DATE_FIELDS:
        v = item.get(f) if isinstance(item, dict) else None
        if v:
            dt = parse_iso(v)
            if dt: return dt
    return None


def fetch_meetings(org_id, start, end):
    data = aff_get(
        "/interactions",
        type=0, organization_id=org_id,
        start_time=iso_z(start), end_time=iso_z(end),
        page_size=100,
    )
    out = []
    for m in unpack(data, f"/interactions meetings org={org_id}"):
        dt = first_dt(m)
        if not dt: continue
        raw = []
        for f in ("persons", "attendees", "participants", "attendee_persons"):
            v = m.get(f)
            if isinstance(v, list): raw.extend(v)
        out.append({
            "id":    m.get("id"),
            "dt":    dt,
            "title": m.get("title") or m.get("subject") or "(no title)",
            "raw":   raw,
        })
    return out


_PREFIX = re.compile(r"^(re|fw|fwd):\s*", re.I)


def norm_title(t):
    return _PREFIX.sub("", (t or "").strip()).lower()


def dedupe_meetings(items):
    seen = {}
    for item in items:
        key = (ny_date(item["dt"]), norm_title(item["title"]))
        if key not in seen or item["dt"] < seen[key]["dt"]:
            seen[key] = item
    return list(seen.values())


# ── Date formatting ────────────────────────────────────────────────────────────
def ny_date(dt_utc):
    return dt_utc.astimezone(NY_TZ).date()


def fmt_month_day(d):
    return datetime(d.year, d.month, d.day).strftime("%b %-d")


def fmt_range(start_utc, end_utc):
    s = start_utc.astimezone(NY_TZ)
    e = end_utc.astimezone(NY_TZ)
    if s.month == e.month and s.year == e.year:
        return f"{s.strftime('%b')} {s.day}–{e.day}"
    return f"{s.strftime('%b %-d')}–{e.strftime('%b %-d')}"


# ── Bullet rendering ──────────────────────────────────────────────────────────
def truncate(s, n):
    s = (s or "").strip()
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def fmt_externals(externals):
    if not externals: return ""
    if len(externals) <= MAX_EXT_NAMES:
        return ", ".join(externals)
    head = ", ".join(externals[:MAX_EXT_NAMES])
    return f"{head} (+ {len(externals) - MAX_EXT_NAMES} more)"


def render_bullet(item):
    motive, external = split_attendees(item["raw"])
    if not motive: return None
    title = truncate(item["title"], MAX_TITLE)
    m_str = ", ".join(f"*{n}*" for n in motive)
    if external:
        return f"• _{title}_ — {m_str} with {fmt_externals(external)}"
    return f"• _{title}_ — {m_str}"


# ── Chunk builders ───────────────────────────────────────────────────────────
def build_company_chunks(groups):
    """Each chunk = one company's full block (header line + bullets), no trailing newline."""
    chunks = []
    for co_name, co_id, d, items in sorted(groups, key=lambda x: (x[2], x[0])):
        bullets = [b for b in (render_bullet(i) for i in items) if b]
        if not bullets: continue
        chunk = f"{co_link_bold(co_name, co_id)} · {fmt_month_day(d)}\n" + "\n".join(bullets)
        chunks.append(chunk)
    return chunks


def build_owner_chunks(no_interaction):
    """Each chunk = one owner line."""
    owner_map = defaultdict(list)
    for co_name, co_id, owner_name in no_interaction:
        owner_map[owner_name].append((co_name, co_id))
    chunks = []
    for owner in sorted(owner_map):
        cos   = sorted(owner_map[owner], key=lambda x: x[0])
        links = " · ".join(co_link_plain(n, cid) for n, cid in cos)
        chunks.append(f"_{owner}:_ {links}")
    return chunks


# ── Section posting ────────────────────────────────────────────────────────
def post_section(channel_id, header, section_title, chunks, empty_msg, separator="\n\n"):
    """Post a section as 1+ Slack messages.

    Each message starts with: header \n DIVIDER \n section_title \n
    Chunks are appended joined by `separator`. When adding the next chunk
    would exceed SLACK_MSG_LIMIT, flush the current buffer and start a new
    message with the same prefix.
    """
    prefix = f"{header}\n{DIVIDER}\n{section_title}\n"

    if not chunks:
        slk_post("chat.postMessage", {
            "channel": channel_id,
            "text":    prefix + empty_msg,
            "mrkdwn":  True,
        })
        return 1

    posted = 0
    buf = []
    buf_len = len(prefix)

    def flush():
        nonlocal posted
        if not buf: return
        text = prefix + separator.join(buf)
        res = slk_post("chat.postMessage", {
            "channel": channel_id, "text": text, "mrkdwn": True,
        })
        if res: posted += 1

    for chunk in chunks:
        added = (len(separator) + len(chunk)) if buf else len(chunk)
        if buf and (buf_len + added) > SLACK_MSG_LIMIT:
            flush()
            buf = [chunk]
            buf_len = len(prefix) + len(chunk)
        else:
            buf.append(chunk)
            buf_len += added

    flush()
    return posted


# ── Main ────────────────────────────────────────────────────────────────────
def main():
    now = datetime.now(timezone.utc)
    today_ny  = now.astimezone(NY_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    today_utc = today_ny.astimezone(timezone.utc)

    past_start = today_utc - timedelta(days=PAST_DAYS)
    past_end   = today_utc
    upc_start  = today_utc
    upc_end    = today_utc + timedelta(days=UPCOMING_DAYS)

    log(f"Past:     {past_start.date()} -> {past_end.date()}")
    log(f"Upcoming: {upc_start.date()} -> {upc_end.date()}")

    list_id = find_list_id(LIST_NAME)
    if not list_id:
        warn(f"List '{LIST_NAME}' not found"); return 1
    log(f"List '{LIST_NAME}' id={list_id}")

    channel_id = CHANNEL_ID_OVERRIDE or find_channel_id(CHANNEL_NAME)
    if not channel_id:
        warn(f"Channel '{CHANNEL_NAME}' not found"); return 1
    log(f"Channel id={channel_id}")

    owners_field_id = find_owners_field_id(list_id)

    entries = get_list_entries(list_id)
    log(f"List entries: {len(entries)}")

    past_groups, upcoming_groups, no_interaction = [], [], []

    for entry in entries:
        if entry.get("entity_type") not in (1, "organization"):
            continue
        entity   = entry.get("entity") or {}
        co_id    = entity.get("id") or entry.get("entity_id")
        co_name  = entity.get("name") or f"org #{co_id}"
        entry_id = entry.get("id")
        if not co_id: continue

        try:
            past_items = fetch_meetings(co_id, past_start, past_end)
            upc_items  = fetch_meetings(co_id, upc_start, upc_end)
        except Exception as e:
            warn(f"{co_name}: fetch error: {e}")
            past_items, upc_items = [], []

        past_items = dedupe_meetings([i for i in past_items if has_motive(i)])
        upc_items  = dedupe_meetings([i for i in upc_items  if has_motive(i)])

        def group_by_date(items, target):
            by_date = defaultdict(list)
            for item in items:
                by_date[ny_date(item["dt"])].append(item)
            for d, its in by_date.items():
                target.append((co_name, co_id, d, its))

        group_by_date(past_items, past_groups)
        group_by_date(upc_items,  upcoming_groups)

        if not past_items and not upc_items:
            owners     = get_owners_for_entry(entry_id, owners_field_id)
            owner_name = owners[0] if owners else "Unassigned"
            no_interaction.append((co_name, co_id, owner_name))

    log(f"Past groups: {len(past_groups)} | Upcoming: {len(upcoming_groups)} | No interactions: {len(no_interaction)}")

    # Date ranges
    full_range = fmt_range(past_start, upc_end)
    past_range = fmt_range(past_start, past_end - timedelta(seconds=1))
    upc_range  = fmt_range(today_utc, upc_end)

    # Common header on every message
    header = f"*Account Management Master — Weekly Update ({full_range})*"

    # Build chunks for each section
    past_chunks = build_company_chunks(past_groups)
    upc_chunks  = build_company_chunks(upcoming_groups)
    ni_chunks   = build_owner_chunks(no_interaction)

    total_posted = 0

    # 1. Past Interactions
    total_posted += post_section(
        channel_id, header,
        f"\U0001f4cb *PAST INTERACTIONS ({past_range})*",
        past_chunks,
        "_No meetings in the last 7 days._",
        separator="\n\n",
    )

    # 2. Upcoming Interactions
    total_posted += post_section(
        channel_id, header,
        f"\U0001f4c5 *UPCOMING INTERACTIONS ({upc_range})*",
        upc_chunks,
        "_No meetings scheduled in the next 7 days._",
        separator="\n\n",
    )

    # 3. No Interactions
    total_posted += post_section(
        channel_id, header,
        f"⚠️ *NO INTERACTIONS ({len(no_interaction)} accounts)*",
        ni_chunks,
        "_All accounts had a meeting in the last 7 days or have one scheduled in the next 7 days._",
        separator="\n",
    )

    log(f"Posted {total_posted} message(s) to #{CHANNEL_NAME}")
    return 0 if total_posted >= 3 else 1


if __name__ == "__main__":
    sys.exit(main())
