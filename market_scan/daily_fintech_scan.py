#!/usr/bin/env python3
"""Daily Fintech Market Scan — posts a structured fintech news digest to Slack #market-scan."""

import calendar
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse

import feedparser
import pytz
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateutil_parser

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "C0APAPQRTPC")
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
PITCHBOOK_MCP_SERVER_URL = os.environ.get("PITCHBOOK_MCP_SERVER_URL", "")
PITCHBOOK_MCP_AUTH_TOKEN = os.environ.get("PITCHBOOK_MCP_AUTH_TOKEN", "")

EST = pytz.timezone("America/New_York")
NOW_UTC = datetime.now(timezone.utc)
FRESH_CUTOFF_UTC = NOW_UTC - timedelta(hours=24)       # only fetch news from last 24h
DEDUP_LOOKBACK_UTC = NOW_UTC - timedelta(days=7)       # check Slack history 7 days back

# ── Fintech Verticals ─────────────────────────────────────────────────────────

VERTICALS = [
    {
        "name": "Payments",
        "emoji": "💳",
        "keywords": [
            "payment", "payments", "card network", "point of sale", "checkout",
            "merchant acquiring", "open banking", "buy now pay later", "bnpl",
            "remittance", "cross-border payment", "money transfer", "digital wallet",
            "e-wallet", "contactless", "real-time payment", "rtp", "ach", "payfac",
            "payment facilitator", "acquiring", "issuing", "interchange",
        ],
    },
    {
        "name": "Capital Markets",
        "emoji": "📊",
        "keywords": [
            "capital markets", "stock exchange", "clearing house", "settlement",
            "equity trading", "bond market", "derivatives", "fixed income", "repo market",
            "prime brokerage", "market structure", "post-trade", "dark pool",
            "market maker", "t+1", "execution venue", "electronic trading",
            "fix protocol", "trade finance", "securities lending",
        ],
    },
    {
        "name": "Lending & Credit",
        "emoji": "🏦",
        "keywords": [
            "lending", "loan", "credit", "mortgage", "underwriting",
            "loan origination", "heloc", "auto loan", "student loan", "sme lending",
            "small business loan", "credit score", "credit bureau",
            "debt financing", "credit facility", "syndicated loan", "fintech lender",
        ],
    },
    {
        "name": "Insurance",
        "emoji": "🛡️",
        "keywords": [
            "insurance", "insurtech", "insurer", "claims", "reinsurance",
            "property and casualty", "life insurance", "health insurance",
            "parametric insurance", "embedded insurance", "mga",
            "managing general agent", "actuarial", "underwriting platform",
        ],
    },
    {
        "name": "Wealth & Asset Management",
        "emoji": "💼",
        "keywords": [
            "wealth management", "asset management", "wealthtech", "portfolio management",
            "robo-advisor", "financial planning", "retirement planning", "401k",
            "etf", "fund management", "family office", "registered investment advisor",
            "private wealth", "private banking",
        ],
    },
    {
        "name": "Digital Assets",
        "emoji": "🔗",
        "keywords": [
            "crypto", "cryptocurrency", "blockchain", "defi", "stablecoin",
            "nft", "digital asset", "tokenization", "web3", "cbdc",
            "bitcoin", "ethereum", "smart contract", "decentralized finance",
            "tokenized",
        ],
    },
    {
        "name": "Data & Analytics",
        "emoji": "📈",
        "keywords": [
            "data analytics", "artificial intelligence", "machine learning",
            "alternative data", "financial data", "risk analytics",
            "regtech", "market data", "generative ai", "large language model",
            "predictive analytics", "esg data", "credit data", "compliance technology",
        ],
    },
    {
        "name": "Business Services",
        "emoji": "🏢",
        "keywords": [
            "accounts payable", "accounts receivable", "treasury management",
            "working capital", "embedded finance", "banking-as-a-service", "baas",
            "corporate banking", "expense management", "ap automation",
            "invoice financing", "supply chain finance",
        ],
    },
]

# ── Strategics (Motive thesis companies) ─────────────────────────────────────

STRATEGICS: list[tuple[str, str]] = [
    (r"Bloomberg", "Bloomberg"),
    (r"Broadridge", "Broadridge"),
    (r"\bICE\b", "ICE"),
    (r"Intercontinental Exchange", "ICE"),
    (r"S&P Global", "S&P Global"),
    (r"\bNasdaq\b", "Nasdaq"),
    (r"\bLSEG\b", "LSEG"),
    (r"London Stock Exchange", "LSEG"),
    (r"\bEuronext\b", "Euronext"),
    (r"\bFIS\b", "FIS"),
    (r"\bDTCC\b", "DTCC"),
    (r"\bSGX\b", "SGX"),
    (r"ION Group", "ION Group"),
    (r"\bBlackRock\b", "BlackRock"),
    (r"CME Group", "CME Group"),
    (r"\bCBOE\b", "CBOE"),
    (r"\bTradeweb\b", "Tradeweb"),
    (r"Deutsche B.?rse", "Deutsche Börse"),
    (r"Moody.s", "Moody's"),
    (r"\bMSCI\b", "MSCI"),
    (r"\bFactSet\b", "FactSet"),
    (r"\bMorningstar\b", "Morningstar"),
    (r"\bSnowflake\b", "Snowflake"),
    (r"\bAlphaSense\b", "AlphaSense"),
    (r"\bSEI\b", "SEI"),
    (r"\bSS&C\b", "SS&C"),
    (r"\bComputershare\b", "Computershare"),
    (r"ABN.?AMRO", "ABN AMRO"),
    (r"\bAmundi\b", "Amundi"),
    (r"\bAegon\b", "Aegon"),
    (r"\bTransamerica\b", "Transamerica"),
    (r"Swiss Re", "Swiss Re"),
    (r"\bGuidewire\b", "Guidewire"),
    (r"Duck Creek", "Duck Creek"),
    (r"\bEsure\b", "Esure"),
    (r"\bVisa\b", "Visa"),
    (r"\bMastercard\b", "Mastercard"),
    (r"\bStripe\b", "Stripe"),
    (r"\bAdyen\b", "Adyen"),
    (r"\bPayPal\b", "PayPal"),
    (r"\bFiserv\b", "Fiserv"),
]

# ── RSS Sources ───────────────────────────────────────────────────────────────

RSS_FEEDS = [
    {"name": "PYMNTS",          "url": "https://www.pymnts.com/feed/"},
    {"name": "FinTech Global",  "url": "https://fintech.global/feed/"},
    {"name": "Finextra",        "url": "https://www.finextra.com/rss/headlines.aspx"},
    {"name": "Markets Media",   "url": "https://marketsmedia.com/feed/"},
    {"name": "InsurTech News",  "url": "https://www.insurtech.me/feed/"},
    {"name": "The Trade News",  "url": "https://www.thetradenews.com/feed/"},
    {"name": "GlobeNewswire",   "url": "https://www.globenewswire.com/RssFeed/industry/Financial+Technology"},
    {"name": "Tearsheet",       "url": "https://tearsheet.co/feed/"},
    {"name": "Crunchbase News", "url": "https://news.crunchbase.com/feed/"},
    {"name": "CoinDesk",        "url": "https://www.coindesk.com/arc/outboundfeeds/rss/"},
    {"name": "The Block",       "url": "https://www.theblock.co/rss.xml"},
    {"name": "Fintech Nexus",   "url": "https://fintechnexus.com/feed/"},
    {"name": "American Banker", "url": "https://www.americanbanker.com/feed"},
]


# ── Normalization helpers (for dedup) ──────────────────────────────────────────

def normalize_url(url: str) -> str:
    """Strip query params, fragment, www., trailing slash; lowercase. Collapses tracking variants."""
    try:
        p = urlparse(url.strip().lower())
        netloc = p.netloc.replace("www.", "")
        path = p.path.rstrip("/")
        return urlunparse(("https", netloc, path, "", "", ""))
    except Exception:
        return url.strip().lower()


def normalize_title(title: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace, take first 80 chars."""
    t = re.sub(r"[^a-z0-9 ]", " ", title.lower())
    t = re.sub(r"\s+", " ", t).strip()
    return t[:80]


# ── RSS fetch ──────────────────────────────────────────────────────────────────

def _parse_entry_date(entry) -> datetime:
    for attr in ("published_parsed", "updated_parsed"):
        val = getattr(entry, attr, None)
        if val:
            return datetime.fromtimestamp(calendar.timegm(val), tz=timezone.utc)
    for attr in ("published", "updated"):
        raw = getattr(entry, attr, None)
        if raw:
            try:
                return dateutil_parser.parse(raw).astimezone(timezone.utc)
            except Exception:
                pass
    return NOW_UTC


def _clean_summary(raw_html: str) -> str:
    """Strip HTML, collapse whitespace, return first sentence (capped at 220 chars)."""
    text = BeautifulSoup(raw_html or "", "html.parser").get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    # First sentence, gracefully
    m = re.search(r"^(.{30,220}?[.!?])(?:\s|$)", text)
    if m:
        return m.group(1)
    return text[:200] + ("…" if len(text) > 200 else "")


def fetch_rss(feed: dict) -> list[dict]:
    articles = []
    try:
        parsed = feedparser.parse(feed["url"])
        for entry in parsed.entries:
            pub = _parse_entry_date(entry)
            if pub < FRESH_CUTOFF_UTC:
                continue
            title = (entry.get("title") or "").strip()
            link = (entry.get("link") or "").strip()
            if not title or not link:
                continue
            content_list = entry.get("content", [])
            raw_summary = (
                entry.get("summary")
                or (content_list[0].get("value") if content_list else "")
                or ""
            )
            articles.append({
                "title": title,
                "url": link,
                "summary": _clean_summary(raw_summary),
                "source": feed["name"],
                "date": pub,
                "norm_url": normalize_url(link),
                "norm_title": normalize_title(title),
            })
    except Exception as exc:
        log.warning("Feed '%s' failed: %s", feed["name"], exc)
    return articles


# ── Slack helpers ─────────────────────────────────────────────────────────────

def slack_get_recent_signatures() -> tuple[set[str], set[str]]:
    """
    Return (normalized_urls, normalized_titles) seen in the channel
    in the last 7 days. Used to prevent re-posting the same article
    even if its URL has different tracking params or its title
    appears with slight variation.
    """
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()

    url_re = re.compile(r"https?://[^\s>|\"']+")
    # Slack mrkdwn link format: <url|display text>
    slack_link_re = re.compile(r"<(https?://[^|>]+)\|([^>]+)>")

    cursor = None
    pages = 0
    try:
        while pages < 5:  # cap at 5 pages = up to 1000 messages
            params = {
                "channel": SLACK_CHANNEL_ID,
                "limit": 200,
                "oldest": str(DEDUP_LOOKBACK_UTC.timestamp()),
            }
            if cursor:
                params["cursor"] = cursor
            resp = requests.get(
                "https://slack.com/api/conversations.history",
                headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
                params=params,
                timeout=20,
            )
            data = resp.json()
            if not data.get("ok"):
                log.warning("Slack history error: %s", data.get("error"))
                break
            for msg in data.get("messages", []):
                text = msg.get("text", "") + " " + json.dumps(msg.get("blocks", []))
                # Capture full URLs and Slack-styled link displays separately.
                for u in url_re.findall(text):
                    seen_urls.add(normalize_url(u))
                for u, display in slack_link_re.findall(text):
                    seen_urls.add(normalize_url(u))
                    seen_titles.add(normalize_title(display))
            cursor = data.get("response_metadata", {}).get("next_cursor") or None
            pages += 1
            if not cursor:
                break
    except Exception as exc:
        log.warning("Could not read Slack history: %s", exc)

    return seen_urls, seen_titles


# ── Classification ────────────────────────────────────────────────────────────

def classify_article(article: dict) -> list[str]:
    body = (article["title"] + " " + article["summary"]).lower()
    return [v["name"] for v in VERTICALS if any(kw in body for kw in v["keywords"])]


def find_strategics(article: dict) -> list[str]:
    body = article["title"] + " " + article["summary"]
    found = []
    seen_names: set[str] = set()
    for pattern, display in STRATEGICS:
        if display not in seen_names and re.search(pattern, body, re.IGNORECASE):
            found.append(display)
            seen_names.add(display)
    return found


# ── Slack Block Kit ───────────────────────────────────────────────────────────

def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _article_line(a: dict) -> str:
    """
    Format: • *<url|Title>* — succinct summary
    Hyperlink only on the bold lead. Source omitted (listed at the bottom).
    """
    title = _esc(a["title"])
    summary = _esc(a["summary"]).strip()
    if summary:
        return f"• *<{a['url']}|{title}>* — {summary}"
    return f"• *<{a['url']}|{title}>*"


def _strategic_line(article: dict, companies: list[str]) -> str:
    """
    Format: • *<url|Company / Company — Title>* — succinct summary
    Hyperlink covers the company-prefixed lead phrase only.
    """
    label = " / ".join(companies[:2])
    title = _esc(article["title"])
    lead = f"{label} — {title}"
    summary = _esc(article["summary"]).strip()
    if summary:
        return f"• *<{article['url']}|{lead}>* — {summary}"
    return f"• *<{article['url']}|{lead}>*"


def build_blocks(
    date_str: str,
    vertical_news: dict,
    strategic_articles: list,
) -> list[dict]:
    blocks: list[dict] = []

    blocks.append({
        "type": "header",
        "text": {
            "type": "plain_text",
            "text": f"\U0001f5de️ Daily Fintech Market Scan — {date_str}",
            "emoji": True,
        },
    })
    blocks.append({"type": "divider"})

    for v in VERTICALS:
        articles = vertical_news.get(v["name"], [])
        if not articles:
            continue
        lines = "\n".join(_article_line(a) for a in articles[:4])
        text = f"*{v['emoji']} {v['name']}*\n{lines}"
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": text[:2990]},
        })

    if strategic_articles:
        blocks.append({"type": "divider"})
        strat_lines = []
        seen_norm_urls: set[str] = set()
        for article, companies in strategic_articles[:8]:
            nu = article.get("norm_url") or normalize_url(article["url"])
            if nu in seen_norm_urls:
                continue
            seen_norm_urls.add(nu)
            strat_lines.append(_strategic_line(article, companies))
        strat_text = "*\U0001f52d Strategics Watch*\n" + "\n".join(strat_lines)
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": strat_text[:2990]},
        })

    blocks.append({"type": "divider"})
    blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": (
                "_Sources: FinTech Global · PYMNTS · Finextra · Markets Media "
                "· GlobeNewswire · Crunchbase · InsurTech News · The Trade News "
                "· CoinDesk · The Block · Tearsheet · Fintech Nexus · American Banker "
                f"— past 24h as of {NOW_UTC.strftime('%H:%M UTC')}_"
            ),
        }],
    })
    return blocks


def slack_post(blocks: list, fallback: str) -> None:
    if DRY_RUN:
        log.info("DRY RUN — not posting. Blocks:\n%s", json.dumps(blocks, indent=2, ensure_ascii=False))
        return
    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            "Content-Type": "application/json",
        },
        json={
            "channel": SLACK_CHANNEL_ID,
            "blocks": blocks,
            "text": fallback,
            "unfurl_links": False,
            "unfurl_media": False,
        },
        timeout=20,
    )
    result = resp.json()
    if not result.get("ok"):
        raise RuntimeError(f"Slack post failed: {result.get('error')}")
    log.info("Posted successfully (ts=%s)", result.get("ts"))


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("Starting Daily Fintech Market Scan (dry_run=%s)", DRY_RUN)
    now_est = NOW_UTC.astimezone(EST)
    date_str = now_est.strftime("%-m/%-d/%Y")

    # Step 1: Build dedup set from last 7 days of Slack history
    seen_urls, seen_titles = slack_get_recent_signatures()
    log.info(
        "Dedup set: %d normalized URLs, %d normalized titles (last 7 days)",
        len(seen_urls), len(seen_titles),
    )

    # Step 2: Fetch RSS feeds (last 24h only)
    all_articles: list[dict] = []
    for feed in RSS_FEEDS:
        batch = fetch_rss(feed)
        log.info("  %-18s %d articles", feed["name"] + ":", len(batch))
        all_articles.extend(batch)

    # Step 3: Deduplicate (within batch and against Slack history)
    seen_url_in_batch: set[str] = set()
    seen_title_in_batch: set[str] = set()
    fresh: list[dict] = []
    skipped_dupe = 0
    for a in all_articles:
        nu = a["norm_url"]
        nt = a["norm_title"]
        if nu in seen_urls or nt in seen_titles:
            skipped_dupe += 1
            continue
        if nu in seen_url_in_batch or nt in seen_title_in_batch:
            skipped_dupe += 1
            continue
        seen_url_in_batch.add(nu)
        seen_title_in_batch.add(nt)
        fresh.append(a)
    log.info("Fresh after dedup: %d / %d (skipped %d dupes)", len(fresh), len(all_articles), skipped_dupe)

    # Step 4: Classify
    vertical_news: dict[str, list] = {v["name"]: [] for v in VERTICALS}
    strategic_articles: list[tuple] = []
    for article in fresh:
        matched_verticals = classify_article(article)
        companies = find_strategics(article)
        if companies:
            strategic_articles.append((article, companies))
        for v_name in matched_verticals:
            vertical_news[v_name].append(article)

    total = sum(len(v) for v in vertical_news.values())
    log.info(
        "Classified: %d articles across %d verticals, %d strategic mentions",
        total,
        sum(1 for v in vertical_news.values() if v),
        len(strategic_articles),
    )

    if total == 0 and not strategic_articles:
        log.info("No new articles to post — skipping today's digest")
        return

    # Step 5: Build and post
    blocks = build_blocks(date_str, vertical_news, strategic_articles)
    fallback = f"\U0001f5de️ Daily Fintech Market Scan — {date_str}"
    slack_post(blocks, fallback)
    log.info("Done.")


if __name__ == "__main__":
    main()
