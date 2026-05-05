#!/usr/bin/env python3
"""Daily Fintech Market Scan — posts a structured fintech news digest to Slack #market-scan."""

import calendar
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

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
CUTOFF_UTC = NOW_UTC - timedelta(hours=24)

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
#
# Capital Markets:  Bloomberg, Broadridge, ICE, S&P, Nasdaq, LSEG, Euronext,
#                   FIS, DTCC, SGX, ION, BlackRock, CME Group, CBOE, Tradeweb,
#                   Deutsche Boerse
# Data & Analytics: S&P Global, Moody's, MSCI, FactSet, Morningstar,
#                   Snowflake, AlphaSense
# Wealth/AM:        SEI, SS&C, Computershare, Broadridge, ABN AMRO, Amundi,
#                   Aegon, Transamerica
# Insurance:        Swiss Re, Guidewire, Duck Creek, Esure
# Payments:         Visa, Mastercard, Stripe, Adyen, PayPal, Fiserv

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

# ── Pitchbook queries (run via Anthropic API + MCP) ───────────────────────────
#
# Each tuple: (natural-language query, Pitchbook collection list)
# Collections: PITCHBOOK_NEWS | LEVERAGED_COMMENTARY_AND_DATA_NEWS | THIRD_PARTY_NEWS

PITCHBOOK_QUERIES: list[tuple[str, list[str]]] = [
    (
        "Latest fintech news: payments, open banking, BNPL, cross-border payments, digital wallets in the past 24 hours",
        ["PITCHBOOK_NEWS", "THIRD_PARTY_NEWS"],
    ),
    (
        "Capital markets fintech news: trading technology, market structure, clearing, settlement, electronic trading in the past 24 hours",
        ["PITCHBOOK_NEWS", "THIRD_PARTY_NEWS"],
    ),
    (
        "Insurtech and insurance technology news: embedded insurance, MGA, parametric, claims tech in the past 24 hours",
        ["PITCHBOOK_NEWS", "THIRD_PARTY_NEWS"],
    ),
    (
        "Wealthtech, robo-advisors, asset management technology, private wealth platform news in the past 24 hours",
        ["PITCHBOOK_NEWS", "THIRD_PARTY_NEWS"],
    ),
    (
        "Fintech lending, credit technology, mortgage tech, SME lending, BNPL news in the past 24 hours",
        ["PITCHBOOK_NEWS", "THIRD_PARTY_NEWS"],
    ),
    (
        "Crypto, blockchain, digital assets, DeFi, stablecoin, tokenization, CBDC news in the past 24 hours",
        ["PITCHBOOK_NEWS", "THIRD_PARTY_NEWS"],
    ),
    (
        "Fintech data analytics, RegTech, compliance technology, alternative data, AI in finance news in the past 24 hours",
        ["PITCHBOOK_NEWS", "THIRD_PARTY_NEWS"],
    ),
    (
        # Strategic network: Capital Markets + Data & Analytics
        "News about Bloomberg, Broadridge, Nasdaq, LSEG, ICE, S&P Global, Euronext, "
        "Deutsche Boerse, DTCC, CME Group, CBOE, Tradeweb, BlackRock, FIS, "
        "Moody's, MSCI, FactSet, Morningstar, Snowflake, AlphaSense in the past 24 hours",
        ["PITCHBOOK_NEWS", "THIRD_PARTY_NEWS"],
    ),
    (
        # Strategic network: Wealth + Insurance + Payments
        "News about SEI, SS&C, Computershare, ABN AMRO, Amundi, Aegon, Transamerica, "
        "Swiss Re, Guidewire, Duck Creek, Esure, "
        "Visa, Mastercard, Stripe, Adyen, PayPal, Fiserv in the past 24 hours",
        ["PITCHBOOK_NEWS", "THIRD_PARTY_NEWS"],
    ),
]


# ── RSS helpers ─────────────────────────────────────────────────────────────────

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


def fetch_rss(feed: dict) -> list[dict]:
    articles = []
    try:
        parsed = feedparser.parse(feed["url"])
        for entry in parsed.entries:
            pub = _parse_entry_date(entry)
            if pub < CUTOFF_UTC:
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
            summary = BeautifulSoup(raw_summary, "html.parser").get_text()[:400]
            articles.append({
                "title": title,
                "url": link,
                "summary": summary,
                "source": feed["name"],
                "date": pub,
            })
    except Exception as exc:
        log.warning("Feed '%s' failed: %s", feed["name"], exc)
    return articles


# ── Pitchbook via Anthropic API + MCP ───────────────────────────────────────────

def _extract_markdown_links(text: str) -> list[tuple[str, str]]:
    """Parse [Title](URL) pairs from a Pitchbook-formatted response."""
    return re.findall(r"\[([^\[\]]+)\]\((https?://[^\)]+)\)", text)


def pitchbook_fetch_news(seen_urls: set[str]) -> list[dict]:
    """
    Call the Anthropic API, optionally bridging to the Pitchbook MCP server,
    to fetch today's fintech news.

    Requires ANTHROPIC_API_KEY.
    When PITCHBOOK_MCP_SERVER_URL is also set the Pitchbook tools are live;
    otherwise the call still runs but without real-time Pitchbook data.
    """
    if not ANTHROPIC_API_KEY:
        log.info("ANTHROPIC_API_KEY not set — skipping Pitchbook fetch")
        return []

    import anthropic  # lazy import so missing package only fails this step

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    today = NOW_UTC.strftime("%Y-%m-%d")
    yesterday = CUTOFF_UTC.strftime("%Y-%m-%d")

    # Build MCP server config if server URL is provided
    mcp_server: dict | None = None
    use_mcp_beta = bool(PITCHBOOK_MCP_SERVER_URL)
    if use_mcp_beta:
        mcp_server = {"type": "url", "url": PITCHBOOK_MCP_SERVER_URL, "name": "pitchbook"}
        if PITCHBOOK_MCP_AUTH_TOKEN:
            mcp_server["authorization_token"] = PITCHBOOK_MCP_AUTH_TOKEN
        log.info("Pitchbook MCP server configured: %s", PITCHBOOK_MCP_SERVER_URL)
    else:
        log.info("No PITCHBOOK_MCP_SERVER_URL — calling Anthropic API without live MCP")

    articles: list[dict] = []
    seen_pb_urls: set[str] = set()

    for query, collections in PITCHBOOK_QUERIES:
        prompt = (
            f"Today is {today}.\n\n"
            "Use the pitchbook_get_news_analysis tool to retrieve recent fintech news.\n"
            f"Query: {query}\n"
            f"Collections: {collections}\n"
            f"min_date: {yesterday}\n"
            f"max_date: {today}\n\n"
            "After retrieving results, list every source article as:\n"
            "Sources:\n"
            "- [Exact Article Title](https://full-article-url)\n"
        )

        try:
            if use_mcp_beta:
                response = client.beta.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=4000,
                    messages=[{"role": "user", "content": prompt}],
                    betas=["mcp-client-2025-04-04"],
                    mcp_servers=[mcp_server],
                )
            else:
                response = client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=4000,
                    messages=[{"role": "user", "content": prompt}],
                )

            # Collect all text from the response (text blocks + tool result text)
            full_text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    full_text += block.text + "\n"

            for title, url in _extract_markdown_links(full_text):
                if url in seen_urls or url in seen_pb_urls:
                    continue
                seen_pb_urls.add(url)
                articles.append({
                    "title": title,
                    "url": url,
                    "summary": "",
                    "source": "Pitchbook",
                    "date": NOW_UTC,
                })

        except Exception as exc:
            log.warning("Pitchbook query failed (%s): %s", query[:60], exc)

    log.info("Pitchbook: %d articles fetched across %d queries", len(articles), len(PITCHBOOK_QUERIES))
    return articles


# ── Slack helpers ─────────────────────────────────────────────────────────────

def slack_get_recent_urls() -> set[str]:
    """Return URLs posted to the channel in the last 24 h for deduplication."""
    url_re = re.compile(r"https?://[^\s>|\"']+")
    seen: set[str] = set()
    try:
        resp = requests.get(
            "https://slack.com/api/conversations.history",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            params={
                "channel": SLACK_CHANNEL_ID,
                "limit": 100,
                "oldest": str(CUTOFF_UTC.timestamp()),
            },
            timeout=15,
        )
        data = resp.json()
        if not data.get("ok"):
            log.warning("Slack history error: %s", data.get("error"))
            return seen
        for msg in data.get("messages", []):
            text = msg.get("text", "") + json.dumps(msg.get("blocks", []))
            seen.update(url_re.findall(text))
    except Exception as exc:
        log.warning("Could not read Slack history: %s", exc)
    return seen


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
    return f"• <{a['url']}|{_esc(a['title'])}> _({a['source']})_"


def build_blocks(
    date_str: str,
    vertical_news: dict,
    strategic_articles: list,
    pitchbook_active: bool,
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
        seen_urls: set[str] = set()
        for article, companies in strategic_articles[:8]:
            if article["url"] in seen_urls:
                continue
            seen_urls.add(article["url"])
            label = " / ".join(companies[:2])
            strat_lines.append(
                f"• *{label}* — <{article['url']}|{_esc(article['title'])}> _({article['source']})_"
            )
        strat_text = "*\U0001f52d Strategics Watch*\n" + "\n".join(strat_lines)
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": strat_text[:2990]},
        })

    blocks.append({"type": "divider"})
    source_line = (
        "_Sources: Pitchbook · FinTech Global · PYMNTS · Finextra · Markets Media "
        if pitchbook_active else
        "_Sources: FinTech Global · PYMNTS · Finextra · Markets Media "
    )
    source_line += (
        "· GlobeNewswire · Crunchbase · InsurTech News · The Trade News "
        "· CoinDesk · Tearsheet · American Banker "
        f"— past 24h as of {NOW_UTC.strftime('%H:%M UTC')}_"
    )
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": source_line}],
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
    log.info(
        "Starting Daily Fintech Market Scan (dry_run=%s, pitchbook_mcp=%s)",
        DRY_RUN,
        bool(PITCHBOOK_MCP_SERVER_URL),
    )
    now_est = NOW_UTC.astimezone(EST)
    date_str = now_est.strftime("%-m/%-d/%Y")

    # Step 1: Read recent Slack posts for deduplication
    seen_urls = slack_get_recent_urls()
    log.info("Dedup set: %d URLs already posted in last 24h", len(seen_urls))

    # Step 2: Fetch RSS feeds
    all_articles: list[dict] = []
    for feed in RSS_FEEDS:
        batch = fetch_rss(feed)
        log.info("  %-18s %d articles", feed["name"] + ":", len(batch))
        all_articles.extend(batch)

    # Step 3: Fetch Pitchbook news via Anthropic API + MCP
    pb_articles = pitchbook_fetch_news(seen_urls)
    all_articles.extend(pb_articles)
    pitchbook_active = bool(pb_articles)

    # Step 4: Deduplicate
    seen_in_batch: set[str] = set()
    fresh: list[dict] = []
    for a in all_articles:
        if a["url"] not in seen_urls and a["url"] not in seen_in_batch:
            seen_in_batch.add(a["url"])
            fresh.append(a)
    log.info("Fresh articles: %d / %d total", len(fresh), len(all_articles))

    # Step 5: Classify into verticals and flag strategics
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

    # Step 6: Build and post
    blocks = build_blocks(date_str, vertical_news, strategic_articles, pitchbook_active)
    fallback = f"\U0001f5de️ Daily Fintech Market Scan — {date_str}"
    slack_post(blocks, fallback)
    log.info("Done.")


if __name__ == "__main__":
    main()
