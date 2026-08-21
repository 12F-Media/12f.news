#!/usr/bin/env python3
"""
12F hourly refresh script.

Pulls fresh headlines from each configured source's RSS feed (or, for a
handful of sources whose RSS has been discontinued, a source's own free
public JSON endpoint -- see FETCHERS below), sorts them into the site's
existing categories (politics / markets / christian / world / tech / sports /
culture), and rewrites the static sections of index.html between AUTO:
markers. Nothing here talks to any AI model or paid API -- it's plain
RSS/JSON parsing plus deterministic templating, designed to run unattended,
forever, on GitHub Actions' own schedule.

If a feed is down or returns nothing, that source is just skipped for this
run -- we never let one flaky feed break the whole refresh.
"""
import html
import json
import os
import random
import re
import socket
import struct
import sys
import unicodedata
from datetime import datetime, timezone, timedelta

import feedparser

# Hard cap on every network call this script makes. feedparser has no
# per-request timeout of its own -- without this, a single slow or
# bot-protected source (several of the 20 configured here are known to be
# aggressive about it) could stall the entire hourly run indefinitely.
socket.setdefaulttimeout(15)

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:
    ET = timezone(timedelta(hours=-4))  # rough EDT fallback

INDEX_PATH = "index.html"
ARCHIVE_PATH = "archive.json"
UA = "Mozilla/5.0 (compatible; 12FNewsBot/1.0; +https://12f.news)"

# ---------------------------------------------------------------------------
# Source configuration
# ---------------------------------------------------------------------------
# "feeds" maps category -> RSS URL for outlets that publish separate topic
# feeds. "single_category" is used for outlets whose entire output maps to
# one section of the site (a Christian-news outlet, a markets outlet, etc.)
# regardless of which of their feeds we pull from.

SOURCES = {
    "The Hill": {
        "domain": "thehill.com", "bias": "bias-C", "lean": {"politics": "Politics", "markets": "Business", "world": "Foreign Policy", "tech": "Technology"},
        "feeds": {
            "politics": "https://thehill.com/homenews/feed/",
            "markets": "https://thehill.com/business/feed/",
            "world": "https://thehill.com/policy/international/feed/",
            "tech": "https://thehill.com/policy/technology/feed/",
        },
    },
    "Fox News": {
        "domain": "foxnews.com", "bias": "bias-CR", "lean": {"politics": "Politics", "world": "World", "tech": "Technology"},
        "feeds": {
            "politics": "https://moxie.foxnews.com/google-publisher/politics.xml",
            "world": "https://moxie.foxnews.com/google-publisher/world.xml",
            "tech": "https://moxie.foxnews.com/google-publisher/tech.xml",
        },
    },
    "NPR": {
        "domain": "npr.org", "bias": "bias-CL", "lean": {"politics": "National", "markets": "Business", "world": "World", "tech": "Technology"},
        "feeds": {
            "politics": "https://feeds.npr.org/1014/rss.xml",
            "markets": "https://feeds.npr.org/1006/rss.xml",
            "world": "https://feeds.npr.org/1004/rss.xml",
            "tech": "https://feeds.npr.org/1019/rss.xml",
        },
    },
    "Newsmax": {
        "domain": "newsmax.com", "bias": "bias-R", "lean": {"politics": "Politics", "markets": "Business", "world": "World"},
        "feeds": {
            "politics": "https://www.newsmax.com/rss/Politics/1/",
            "markets": "https://www.newsmax.com/rss/markets/7/",
            "world": "https://www.newsmax.com/rss/GlobalTalk/162/",
        },
    },
    "NBC News": {
        "domain": "nbcnews.com", "bias": "bias-CL", "lean": {"politics": "Politics", "markets": "Business", "world": "World", "tech": "Technology"},
        "feeds": {
            "politics": "https://feeds.nbcnews.com/nbcnews/public/politics",
            "markets": "https://feeds.nbcnews.com/nbcnews/public/business",
            "world": "https://feeds.nbcnews.com/nbcnews/public/world",
            "tech": "https://feeds.nbcnews.com/nbcnews/public/tech",
        },
    },
    "CNBC": {
        "domain": "cnbc.com", "bias": "bias-C", "lean": {"politics": "Politics", "markets": "Business", "tech": "Technology"},
        "feeds": {
            "politics": "https://www.cnbc.com/id/10000113/device/rss/rss.html",
            "markets": "https://www.cnbc.com/id/10000664/device/rss/rss.html",
            "tech": "https://www.cnbc.com/id/19854910/device/rss/rss.html",
        },
    },
    "ESPN": {
        # ESPN decommissioned its public RSS feeds -- espn.com/espn/rss/news
        # now 404s/redirects. ESPN's own web app still runs on this
        # unofficial-but-open JSON endpoint, so this source is fetched via
        # fetch_espn_json() (see "fetcher" below) instead of feedparser.
        # Feed keys here are just labels (single_category forces every one
        # of them into "sports" regardless of the key name).
        "domain": "espn.com", "bias": "bias-C", "lean": {"sports": "Sports"},
        "single_category": "sports", "fetcher": "espn_json",
        "feeds": {
            "nfl": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/news",
            "nba": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/news",
            "mlb": "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/news",
            "nhl": "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/news",
            "cfb": "https://site.api.espn.com/apis/site/v2/sports/football/college-football/news",
        },
    },
    "Condé Nast Traveler": {
        "domain": "cntraveler.com", "bias": "bias-C", "lean": {"world": "Travel"},
        "single_category": "world",
        "feeds": {"world": "https://www.cntraveler.com/feed/rss"},
    },
    "CBN News": {
        "domain": "cbn.com", "bias": "bias-CR", "lean": {"christian": "World"},
        "single_category": "christian",
        "feeds": {"christian": "https://www1.cbn.com/rss-cbn-articles-cbnnews.xml"},
    },
    "Christian Post": {
        "domain": "christianpost.com", "bias": "bias-CR", "lean": {"christian": "Faith"},
        "single_category": "christian",
        "feeds": {"christian": "https://www.christianpost.com/rss"},
    },
    "WORLD Magazine": {
        "domain": "wng.org", "bias": "bias-CR", "lean": {"christian": "Faith"},
        "single_category": "christian",
        "feeds": {"christian": "https://wng.org/feeds/rss/topics.rss"},
    },
    "Religion News Service": {
        "domain": "religionnews.com", "bias": "bias-C", "lean": {"christian": "Faith"},
        "single_category": "christian",
        "feeds": {"christian": "https://religionnews.com/feed/"},
    },
    "Faithwire": {
        "domain": "faithwire.com", "bias": "bias-CR", "lean": {"christian": "World"},
        "single_category": "christian",
        "feeds": {"christian": "https://www.faithwire.com/feed"},
    },
    "MarketWatch": {
        "domain": "marketwatch.com", "bias": "bias-C", "lean": {"markets": "Business"},
        "single_category": "markets",
        "feeds": {"markets": "https://feeds.content.dowjones.io/public/rss/mw_topstories"},
    },
    "Investing.com": {
        "domain": "investing.com", "bias": "bias-C", "lean": {"markets": "Business"},
        "single_category": "markets",
        "feeds": {"markets": "https://www.investing.com/rss/market_overview.rss"},
    },
    "Christianity Today": {
        "domain": "christianitytoday.com", "bias": "bias-C", "lean": {"christian": "Faith"},
        "single_category": "christian",
        "feeds": {"christian": "https://feeds.christianitytoday.com/christianitytoday/ctmag"},
    },
    "WSJ": {
        "domain": "wsj.com", "bias": "bias-C", "lean": {"world": "World"},
        "single_category": "world",
        "feeds": {"world": "https://feeds.a.dj.com/rss/RSSWorldNews.xml"},
    },
    "Washington Post": {
        "domain": "washingtonpost.com", "bias": "bias-CL", "lean": {"politics": "National", "world": "World"},
        "feeds": {
            "politics": "https://feeds.washingtonpost.com/rss/national",
            "world": "https://feeds.washingtonpost.com/rss/world",
        },
    },
    "The Independent": {
        "domain": "independent.co.uk", "bias": "bias-CL", "lean": {"world": "World"},
        "single_category": "world",
        "feeds": {"world": "https://www.the-independent.com/news/uk/rss"},
    },
    "Politico": {
        "domain": "politico.com", "bias": "bias-CL", "lean": {"politics": "Politics"},
        "single_category": "politics",
        "feeds": {"politics": "https://www.politico.com/rss/politicopicks.xml"},
    },
    "Yahoo Sports": {
        "domain": "sports.yahoo.com", "bias": "bias-C", "lean": {"sports": "Sports"},
        "single_category": "sports",
        "feeds": {"sports": "https://sports.yahoo.com/rss/"},
    },
    "Vanity Fair": {
        "domain": "vanityfair.com", "bias": "bias-CL", "lean": {"culture": "Culture"},
        "single_category": "culture",
        "feeds": {"culture": "https://www.vanityfair.com/feed/rss"},
    },
    "New York Post": {
        # The general https://nypost.com/feed/ mixes every section together
        # (including Page Six celebrity content) and forced everything into
        # "world" -- switched to NYP's own per-section feeds so each story
        # lands in its real category, same pattern as The Hill/Fox/NBC/CNBC
        # above. exclude_domains is a belt-and-suspenders filter: some NYP
        # sections co-syndicate Page Six stories, and this drops any entry
        # that links out to pagesix.com regardless of which feed it came
        # from, so "New York Post" never sends a reader to Page Six.
        "domain": "nypost.com", "bias": "bias-R",
        "lean": {"politics": "Politics", "world": "World", "markets": "Business", "sports": "Sports", "culture": "Entertainment"},
        "exclude_domains": ["pagesix.com"],
        "feeds": {
            "politics": "https://nypost.com/politics/feed/",
            "world": "https://nypost.com/world-news/feed/",
            "markets": "https://nypost.com/business/feed/",
            "sports": "https://nypost.com/sports/feed/",
            "culture": "https://nypost.com/entertainment/feed/",
        },
    },
}

BIAS_LABEL = {"bias-L": "Left", "bias-CL": "Leans Left", "bias-C": "Center", "bias-CR": "Leans Right", "bias-R": "Right"}
CAT_KICKER = {
    "politics": "Politics", "markets": "Business", "world": "World", "tech": "Technology",
    "christian": "Faith", "sports": "Sports", "culture": "Culture",
}
ALL_CATEGORIES = ("politics", "world", "markets", "christian", "tech", "sports", "culture")

IMG_TAG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)
TAG_RE = re.compile(r'<[^>]+>')
WS_RE = re.compile(r'\s+')


def clean_text(raw):
    if not raw:
        return ""
    text = TAG_RE.sub(' ', raw)
    text = html.unescape(text)
    text = unicodedata.normalize("NFKC", text)
    text = WS_RE.sub(' ', text).strip()
    return text


def truncate(text, limit):
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(' ', 1)[0].rstrip(',.;:') + '…'


def extract_all_images(entry):
    """Every candidate image URL a feed entry offers, in the order its own
    fields list them, deduplicated but NOT yet quality-filtered or picked
    from -- see pick_best_image() further down for that. Pulls from every
    source a single feed entry might carry an image in: media_content/
    media_thumbnail (RSS Media RSS extensions -- usually the outlet's own
    deliberately-sized art), enclosures, then falls back to whatever <img>
    tags show up inline in the summary/description or full content HTML
    (order matters here too -- the first inline image in body copy is far
    more likely to be the article's real lead photo than the fifth, so
    later inline images are kept as later-priority candidates, not
    discarded outright)."""
    urls = []
    for key in ("media_content", "media_thumbnail"):
        for m in entry.get(key) or []:
            url = m.get("url")
            if url:
                urls.append(url)
    for enc in entry.get("enclosures", []) or []:
        etype = (enc.get("type") or "")
        if (etype.startswith("image") or not etype) and enc.get("href"):
            urls.append(enc["href"])
    for field in ("summary", "description"):
        raw = entry.get(field)
        if raw:
            urls.extend(IMG_TAG_RE.findall(raw))
    content = entry.get("content")
    if content:
        for c in content:
            urls.extend(IMG_TAG_RE.findall(c.get("value", "")))
    return list(dict.fromkeys(urls))  # de-dupe, preserve first-seen order


def entry_datetime(entry):
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def format_meta_time(dt, now):
    """Mirrors the site's existing byline style: '9:35 AM ET' for today,
    weekday abbreviation for this week, 'Aug 15' for anything older."""
    if dt is None:
        return "Recently"
    local = dt.astimezone(ET)
    local_now = now.astimezone(ET)
    delta_days = (local_now.date() - local.date()).days
    if delta_days == 0:
        return local.strftime("%-I:%M %p ET") if hasattr(local, "strftime") else local.strftime("%I:%M %p ET").lstrip("0")
    if 0 < delta_days < 7:
        return local.strftime("%a")
    return local.strftime("%b %-d") if _supports_dash(local) else local.strftime("%b %d").replace(" 0", " ")


def _supports_dash(_dt):
    try:
        datetime.now().strftime("%-I")
        return True
    except Exception:
        return False


def is_today(dt, now):
    if dt is None:
        return False
    return dt.astimezone(ET).date() == now.astimezone(ET).date()


# Per-run record of every feed we attempted, so we can verify -- without
# needing shell/log access -- that every configured source is actually
# coming through. Read back after a run via status.txt, committed to the
# repo but never referenced by index.html, so it's never rendered on the
# live page.
SOURCE_STATS = []


def fetch_source_category(source_name, cfg, category, url, limit=8):
    try:
        feed = feedparser.parse(url, agent=UA)
    except Exception as ex:
        print(f"WARN: exception fetching {source_name}/{category}: {ex}", file=sys.stderr)
        SOURCE_STATS.append((source_name, category, 0, f"error: {ex}"))
        return []
    if not feed.entries:
        print(f"WARN: no entries for {source_name}/{category} ({url})", file=sys.stderr)
        SOURCE_STATS.append((source_name, category, 0, "0 entries"))
        return []
    # Optional per-source domain blocklist (e.g. New York Post's own feeds
    # sometimes co-syndicate Page Six celebrity stories) -- filtered before
    # the limit is applied so a blocked entry never crowds out a real one.
    exclude_domains = cfg.get("exclude_domains") or []
    out = []
    for e in feed.entries:
        if len(out) >= limit:
            break
        title = clean_text(e.get("title", ""))
        link = e.get("link", "")
        if not title or not link:
            continue
        if exclude_domains and any(dom in link for dom in exclude_domains):
            continue
        summary = clean_text(e.get("summary", "") or e.get("description", ""))
        out.append({
            "source": source_name,
            "domain": cfg["domain"],
            "bias": cfg["bias"],
            "lean": cfg.get("lean", {}).get(category, CAT_KICKER.get(category, "")),
            "category": category,
            "title": title,
            "summary": summary,
            "url": link,
            "image": pick_best_image(extract_all_images(e), _ARTICLE_PROBE_BUDGET),
            "dt": entry_datetime(e),
        })
    SOURCE_STATS.append((source_name, category, len(out), "ok"))
    return out


def fetch_espn_json(source_name, cfg, category, url, limit=8):
    """ESPN has no public RSS anymore -- this hits the same open JSON
    endpoint ESPN's own web app uses (site.api.espn.com/apis/site/v2/...),
    still a free, unauthenticated, non-AI, non-paid API, consistent with
    how every other source here is fetched."""
    import urllib.request

    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as ex:
        print(f"WARN: exception fetching {source_name}/{category}: {ex}", file=sys.stderr)
        SOURCE_STATS.append((source_name, category, 0, f"error: {ex}"))
        return []
    articles = data.get("articles") or []
    if not articles:
        print(f"WARN: no entries for {source_name}/{category} ({url})", file=sys.stderr)
        SOURCE_STATS.append((source_name, category, 0, "0 entries"))
        return []
    exclude_domains = cfg.get("exclude_domains") or []
    out = []
    for a in articles:
        if len(out) >= limit:
            break
        if a.get("type") not in (None, "Story"):
            continue  # skip video-clip entries, keep real articles
        title = clean_text(a.get("headline", ""))
        link = ((a.get("links") or {}).get("web") or {}).get("href", "")
        if not title or not link:
            continue
        if exclude_domains and any(dom in link for dom in exclude_domains):
            continue
        summary = clean_text(a.get("description", ""))
        image_urls = [im.get("url") for im in (a.get("images") or []) if im.get("url")]
        image = pick_best_image(image_urls, _ARTICLE_PROBE_BUDGET)
        dt = None
        pub = a.get("published")
        if pub:
            try:
                dt = datetime.strptime(pub, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except Exception:
                dt = None
        out.append({
            "source": source_name,
            "domain": cfg["domain"],
            "bias": cfg["bias"],
            "lean": cfg.get("lean", {}).get(category, CAT_KICKER.get(category, "")),
            "category": category,
            "title": title,
            "summary": summary,
            "url": link,
            "image": image,
            "dt": dt,
        })
    SOURCE_STATS.append((source_name, category, len(out), "ok"))
    return out


FETCHERS = {
    "espn_json": fetch_espn_json,
}


def collect_all(now):
    """Fetch every configured feed. Returns dict: category -> list[item]."""
    SOURCE_STATS.clear()
    _ARTICLE_PROBE_BUDGET[0] = ARTICLE_MAX_PROBES_PER_RUN
    by_category = {c: [] for c in ALL_CATEGORIES}
    for source_name, cfg in SOURCES.items():
        forced_cat = cfg.get("single_category")
        fetcher = FETCHERS.get(cfg.get("fetcher"), fetch_source_category)
        for category, url in cfg["feeds"].items():
            target_cat = forced_cat or category
            items = fetcher(source_name, cfg, target_cat, url)
            by_category[target_cat].extend(items)
    for cat, items in by_category.items():
        items.sort(key=lambda it: it["dt"] or datetime(1970, 1, 1, tzinfo=timezone.utc), reverse=True)
    return by_category


# ---------------------------------------------------------------------------
# HTML rendering helpers (match the existing hand-authored markup exactly)
# ---------------------------------------------------------------------------

def esc(text):
    return html.escape(text or "", quote=True)


def ts_of(item):
    """Unix epoch seconds for an item, or 0 if unknown -- embedded as
    data-ts on every card so client-side JS can sort by real recency
    instead of only whatever order the server picked."""
    return int(item["dt"].timestamp()) if item["dt"] else 0


# ---------------------------------------------------------------------------
# Persistent archive: every article we've ever fetched, deduped by URL, kept
# forever (not just whatever's currently on the front page) so search and
# "browse older stories" have real history to draw on. Written as a flat
# JSON file the static site fetches client-side -- there's no backend/
# database here, this is the whole "database."
# ---------------------------------------------------------------------------

def load_archive():
    try:
        with open(ARCHIVE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def update_archive(by_category, now):
    archive = load_archive()
    seen = {a["url"] for a in archive}
    added = 0
    for items in by_category.values():
        for it in items:
            if it["url"] in seen:
                continue
            seen.add(it["url"])
            archive.append({
                "title": it["title"],
                "summary": truncate(it["summary"], 200),
                "url": it["url"],
                "source": it["source"],
                "domain": it["domain"],
                "bias": it["bias"],
                "category": it["category"],
                "image": it["image"],
                "ts": ts_of(it) or int(now.timestamp()),
            })
            added += 1
    archive.sort(key=lambda a: a["ts"], reverse=True)
    with open(ARCHIVE_PATH, "w", encoding="utf-8") as f:
        json.dump(archive, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Archive: +{added} new item(s), {len(archive)} total")
    return archive


def backfill_from_archive(by_category, archive, min_count=8):
    """If a category came back thin this hour -- a source is down, blocked,
    or just quiet (ESPN's feed, for example, frequently returns nothing) --
    top it up with the most recent archived items for that category so the
    section/tab never goes empty just because one source had a bad hour.
    Archived items used this way are tagged backfilled=True so downstream
    selection can treat them as lower priority than genuinely fresh ones.

    Candidates are taken newest-first (archive is already sorted that way),
    but photo-bearing ones are drawn from that recency-ordered list before
    photo-less ones -- same photo-first principle as pick_photo_priority()
    applies here too, so a degraded-feed hour doesn't quietly starve a
    category's photo cards just because backfill filled its slots without
    regard for which archived items actually had an image."""
    for cat, items in by_category.items():
        fresh_count = len(items)
        if fresh_count >= min_count:
            continue
        have = {it["url"] for it in items}
        needed = min_count - fresh_count
        with_image, without_image = [], []
        for a in archive:
            if a["category"] != cat or a["url"] in have:
                continue
            have.add(a["url"])
            (with_image if a.get("image") else without_image).append(a)
        chosen = (with_image + without_image)[:needed]
        added = 0
        for a in chosen:
            items.append({
                "source": a["source"], "domain": a["domain"], "bias": a["bias"],
                "lean": SOURCES.get(a["source"], {}).get("lean", {}).get(cat, CAT_KICKER.get(cat, "")),
                "category": cat, "title": a["title"], "summary": a.get("summary", ""),
                "url": a["url"], "image": a.get("image"),
                "dt": datetime.fromtimestamp(a["ts"], tz=timezone.utc) if a.get("ts") else None,
                "backfilled": True,
            })
            added += 1
        if added:
            print(f"Backfilled {cat} with {added} archived item(s) ({fresh_count} fresh this hour)")
        items.sort(key=lambda it: it["dt"] or datetime(1970, 1, 1, tzinfo=timezone.utc), reverse=True)
    return by_category


def bias_dot_mini(bias):
    label = BIAS_LABEL.get(bias, "Center")
    return f'<span class="bias-dot-mini {bias}" title="Source bias rating: {label}"></span>'


def bias_meter(bias):
    label = BIAS_LABEL.get(bias, "Center")
    return (f'<span class="bias-meter {bias}" title="Source bias rating: {label}">'
            f'<span class="bias-track"><span class="bias-dot"></span></span>'
            f'<span class="bias-label">{label}</span></span>')


def render_briefing_item(item):
    dek = truncate(item["summary"], 140) or "Read the full story for details."
    return (f'<li><b>{esc(item["title"])}</b> — {esc(dek)} '
            f'<span class="cite"><a href="{esc(item["url"])}" target="_blank" rel="noopener noreferrer">{esc(item["source"])}</a>'
            f'{bias_dot_mini(item["bias"])}</span></li>')


def render_digest_item(item, now):
    dek = truncate(item["summary"], 150) or item["title"]
    meta_time = format_meta_time(item["dt"], now)
    today = "yes" if is_today(item["dt"], now) else "no"
    kicker = CAT_KICKER.get(item["category"], "News")
    return (f'<li data-cat="{item["category"]}" data-today="{today}" data-ts="{ts_of(item)}"><b>{esc(kicker)}:</b> {esc(dek)} '
            f'<span class="meta"><a href="{esc(item["url"])}" target="_blank" rel="noopener noreferrer">{esc(item["source"])} · {esc(meta_time)} ↗</a>'
            f'{bias_dot_mini(item["bias"])}</span></li>')


def render_lead_story(item, now):
    dek = truncate(item["summary"], 220) or "Read the full story at the source link below."
    meta_time = format_meta_time(item["dt"], now)
    today = "yes" if is_today(item["dt"], now) else "no"
    media = ""
    if item["image"]:
        media = (f'<div class="media"><img class="cover-photo" src="{esc(item["image"])}" alt="{esc(item["title"])}" '
                  f'referrerpolicy="no-referrer" onerror="this.closest(\'.media\').style.display=\'none\'">'
                  f'<div class="photo-credit">Photo: via {esc(item["source"])}</div></div>')
    return (f'<article class="lead-story" data-cat="{item["category"]}" data-today="{today}" data-ts="{ts_of(item)}">\n'
            f'        <div class="byline"><span class="source-tag">{esc(item["source"])}</span><span class="lean">{esc(item["lean"])}</span>{bias_meter(item["bias"])}</div>\n'
            f'{media}\n'
            f'        <h2>{esc(item["title"])}</h2>\n'
            f'        <p class="dek">{esc(dek)}</p>\n'
            f'        <div class="byline"><span class="mono">{esc(meta_time)}</span><span>·</span><a href="{esc(item["url"])}" target="_blank" rel="noopener noreferrer">{esc(item["domain"])} ↗</a></div>\n'
            f'      </article>')


def render_sub_story(item, now):
    dek = truncate(item["summary"], 160) or "Read the full story at the source link below."
    meta_time = format_meta_time(item["dt"], now)
    today = "yes" if is_today(item["dt"], now) else "no"
    thumb = f'<img class="thumb" src="{esc(item["image"])}" alt="{esc(item["title"])}" referrerpolicy="no-referrer" onerror="this.style.display=\'none\'">' if item["image"] else ""
    return (f'<article class="sub-story" data-cat="{item["category"]}" data-today="{today}" data-ts="{ts_of(item)}">\n'
            f'        <div class="byline"><span class="source-tag">{esc(item["source"])}</span><span class="lean">{esc(item["lean"])}</span>{bias_meter(item["bias"])}</div>\n'
            f'        <div class="story-row">\n'
            f'          {thumb}\n'
            f'          <div class="story-col">\n'
            f'            <h3>{esc(item["title"])}</h3>\n'
            f'            <p>{esc(dek)}</p>\n'
            f'            <div class="byline"><span class="mono">{esc(meta_time)}</span><span>·</span><a href="{esc(item["url"])}" target="_blank" rel="noopener noreferrer">{esc(item["domain"])} ↗</a></div>\n'
            f'          </div>\n'
            f'        </div>\n'
            f'      </article>')


def js_str(s):
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'


def render_wire_js(items, now):
    lines = []
    for item in items:
        t = format_meta_time(item["dt"], now)
        text = f'{item["source"]}: {truncate(item["title"], 80)}'
        lines.append(f'    [{js_str(t)}, {js_str(text)}, {js_str(item["url"])}, {js_str(item["source"])}],')
    return "  const wireItems = [\n" + "\n".join(lines) + "\n  ];"


def render_bias_js():
    parts = [f'"{name}": "{cfg["bias"]}"' for name, cfg in SOURCES.items()]
    body = ", ".join(parts)
    return "  const BIAS = {\n    " + body + ",\n  };"


STATUS_PATH = "status.txt"


def write_source_status(now):
    """Plain-text diagnostics written to their own file (status.txt) --
    NOT embedded in index.html and never rendered on the page. Lets us
    verify what each configured feed returned this run (via the GitHub
    API/repo, not the live site) without needing shell/log access to the
    Actions runner."""
    stamp = now.astimezone(ET).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"Source status as of {stamp}:"]
    for source_name, category, count, note in SOURCE_STATS:
        lines.append(f"  {source_name} / {category}: {count} items ({note})")
    with open(STATUS_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def replace_between(html_text, start_marker, end_marker, new_body, inline=False):
    pattern = re.compile(re.escape(start_marker) + r'.*?' + re.escape(end_marker), re.DOTALL)
    if not pattern.search(html_text):
        raise RuntimeError(f"Markers not found: {start_marker} .. {end_marker}")
    sep = "" if inline else "\n"
    return pattern.sub(lambda _m: start_marker + sep + new_body + sep + end_marker, html_text)


def pick(pool, n, used):
    out = []
    for item in pool:
        if item["url"] in used:
            continue
        out.append(item)
        used.add(item["url"])
        if len(out) >= n:
            break
    return out


def pick_with_image(pool, used):
    """Like pick(pool, 1, used), but prefers a candidate that has an image
    when one is available nearby, so the front-page lead story (and each
    section's featured card) doesn't end up photo-less just because the
    single freshest item happened not to carry an image."""
    fallback = None
    for item in pool:
        if item["url"] in used:
            continue
        if item["image"]:
            used.add(item["url"])
            return item
        if fallback is None:
            fallback = item
    if fallback is not None:
        used.add(fallback["url"])
    return fallback


def pick_photo_priority(pool, n, used):
    """Like pick(pool, n, used), but fills the n slots with photo-bearing
    candidates first (in pool order), only falling back to photo-less items
    if the pool doesn't have enough images to go around. Used anywhere a
    template renders an image box next to the headline (homepage sub-stories,
    every category grid's cards) so those slots don't sit empty/blank just
    because a photo-less item happened to be picked first -- not every
    article needs a photo, but a slot that's designed to show one should
    almost always get an article that actually has one."""
    with_photo, without_photo = [], []
    for item in pool:
        if item["url"] in used:
            continue
        (with_photo if item["image"] else without_photo).append(item)

    out = []
    for item in with_photo:
        if len(out) >= n:
            break
        out.append(item)
        used.add(item["url"])
    if len(out) < n:
        for item in without_photo:
            if len(out) >= n:
                break
            out.append(item)
            used.add(item["url"])
    return out


# ---------------------------------------------------------------------------
# Article media quality guardrails
# ---------------------------------------------------------------------------
# Every article's image slot (lead story, sub-stories, category cards,
# Markets, Christian World News) runs through pick_best_image() below:
# given every candidate URL a feed entry offered (see extract_all_images()
# above), it discards obvious placeholders/tracking pixels and -- whenever
# an entry actually offered more than one real candidate -- measures each
# one's real resolution and keeps the highest, falling straight through to
# the next candidate whenever one turns out too small to be worth it. No
# aspect-ratio requirement here, since every image box on the site shows
# the full photo uncropped (object-fit:contain, see 12f.css) rather than
# pushing into it, so a portrait or square photo is exactly as usable as a
# landscape one -- only resolution matters.

ARTICLE_MIN_WIDTH = 200
ARTICLE_MIN_HEIGHT = 150
ARTICLE_MAX_PROBES_PER_RUN = 90  # hard cap on how many candidate images
                                  # this run will actually download to
                                  # measure, so a run with lots of
                                  # low-quality candidates in a row can't
                                  # stall the hourly refresh chasing image
                                  # dimensions the way a slow feed could.

# Shared, mutable single-element counter -- see pick_best_image(). Reset to
# ARTICLE_MAX_PROBES_PER_RUN at the top of every collect_all() call so each
# hourly run gets a fresh budget rather than draining across runs.
_ARTICLE_PROBE_BUDGET = [ARTICLE_MAX_PROBES_PER_RUN]

# Filename/path substrings that reliably indicate a generic fallback image
# rather than a real photo -- sized/cropped article art almost never
# contains any of these in its URL, but a site's own "no image available"
# stand-in, a tracking pixel, or a bare logo/icon reliably does.
_PLACEHOLDER_URL_HINTS = (
    "placeholder", "default", "generic", "no-image", "noimage", "no_image",
    "missing", "blank", "1x1", "pixel.gif", "pixel.png", "spacer", "sprite",
    "fallback", "stub", "avatar", "favicon", "logo",
)


def looks_like_placeholder(url):
    """True for anything that's clearly not a real article photo -- vector
    icons/logos (.svg is never a news photo) and known generic-fallback
    filename patterns."""
    if not url:
        return True
    low = url.lower().split("?", 1)[0]
    if low.endswith(".svg"):
        return True
    return any(hint in low for hint in _PLACEHOLDER_URL_HINTS)


def _parse_image_dimensions(data):
    """Reads (width, height) from a PNG/JPEG/GIF/WEBP image's own header
    bytes, entirely by hand -- feedparser is this script's only third-party
    dependency, and this keeps it that way rather than adding an imaging
    library just to read a size header. Returns None for anything that
    doesn't parse as a recognized, complete header (truncated download,
    exotic format, etc.), which the caller treats as "couldn't verify" and
    skips it rather than trusting an unmeasured image."""
    if not data:
        return None
    # PNG: 8-byte signature, then an IHDR chunk whose first 8 bytes of data
    # are big-endian uint32 width/height.
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        if len(data) >= 24 and data[12:16] == b'IHDR':
            return struct.unpack('>II', data[16:24])
        return None
    # GIF87a / GIF89a: 6-byte signature, then little-endian uint16 width/height.
    if data[:6] in (b'GIF87a', b'GIF89a'):
        if len(data) >= 10:
            return struct.unpack('<HH', data[6:10])
        return None
    # JPEG: walk the marker segments looking for the first Start-Of-Frame
    # marker, which carries a 1-byte precision field then big-endian uint16
    # height, then width.
    if data[:2] == b'\xff\xd8':
        i, n = 2, len(data)
        sof_markers = (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                       0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF)
        while i + 4 <= n:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker == 0xFF:
                i += 1
                continue
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg_len = struct.unpack('>H', data[i + 2:i + 4])[0]
            if marker in sof_markers:
                if i + 9 > n:
                    return None  # header was cut off by our download cap
                h, w = struct.unpack('>HH', data[i + 5:i + 9])
                return w, h
            i += 2 + seg_len
        return None
    # WEBP: RIFF container. VP8X carries an explicit canvas size; the
    # simple VP8 (lossy) and VP8L (lossless) bitstreams each pack width/
    # height into their own compact header layout.
    if data[:4] == b'RIFF' and len(data) >= 16 and data[8:12] == b'WEBP':
        chunk = data[12:16]
        if chunk == b'VP8X' and len(data) >= 30:
            w = 1 + (data[24] | (data[25] << 8) | (data[26] << 16))
            h = 1 + (data[27] | (data[28] << 8) | (data[29] << 16))
            return w, h
        if chunk == b'VP8 ' and len(data) >= 30:
            w = struct.unpack('<H', data[26:28])[0] & 0x3FFF
            h = struct.unpack('<H', data[28:30])[0] & 0x3FFF
            return w, h
        if chunk == b'VP8L' and len(data) >= 25:
            bits = struct.unpack('<I', data[21:25])[0]
            w = (bits & 0x3FFF) + 1
            h = ((bits >> 14) & 0x3FFF) + 1
            return w, h
        return None
    return None


def probe_image_dimensions(url, timeout=8, max_bytes=300_000):
    """Downloads just enough of an image's bytes to read its dimensions from
    the format's own header (see _parse_image_dimensions) -- never the whole
    file -- so checking a candidate that turns out to be too small stays
    cheap. Returns None on any failure (network error, unrecognized format,
    truncated header)."""
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Range": f"bytes=0-{max_bytes - 1}",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(max_bytes)
    except Exception:
        return None
    return _parse_image_dimensions(data)


_article_image_dims_cache = {}


def pick_best_image(urls, budget):
    """Given every candidate image URL a feed entry offered (in the order
    extract_all_images() returned them -- media:content/thumbnail first,
    then enclosures, then inline <img> tags), picks the single best one to
    feature. First discards anything that's obviously not a real article
    photo (looks_like_placeholder() -- generic fallbacks, tracking pixels,
    bare icons/logos) at zero network cost. If more than one real candidate
    is left, downloads just enough of each to measure its actual
    resolution and keeps the highest-resolution one that clears
    ARTICLE_MIN_WIDTH/HEIGHT -- so a small thumbnail a feed happened to
    list first never wins over a full-size photo listed second or third.
    `budget` is a shared, mutable single-element counter (see
    _ARTICLE_PROBE_BUDGET) decremented once per real network probe, so one
    article with many candidate images -- or a whole run of them -- can't
    stall the hourly refresh chasing dimensions; once it runs out, any
    remaining unmeasured candidates are simply skipped from the resolution
    comparison rather than probed. Falls back to the first non-placeholder
    candidate (unmeasured, if the budget ran out or every real candidate
    failed to measure/clear the size floor) so an article never loses its
    only photo just because we couldn't verify it; falls back further to
    the very first raw candidate if literally everything looked like a
    placeholder, on the theory that a mislabeled real photo beats no photo
    at all. Returns None only if there were no candidate URLs at all."""
    if not urls:
        return None
    candidates = [u for u in urls if not looks_like_placeholder(u)]
    if not candidates:
        return urls[0]
    if len(candidates) == 1:
        url = candidates[0]
        # Nothing to rank it against, but it's still worth learning this
        # image's shape (if the budget allows) -- render-time portrait
        # handling (see is_portrait_image()) needs every hero-eligible
        # photo's orientation, not just ones that had a sibling to compare
        # against, and a single-candidate article is the common case.
        if url not in _article_image_dims_cache and budget[0] > 0:
            budget[0] -= 1
            _article_image_dims_cache[url] = probe_image_dimensions(url)
        return url

    best_url, best_area = None, -1
    for url in candidates:
        if url in _article_image_dims_cache:
            dims = _article_image_dims_cache[url]
        else:
            if budget[0] <= 0:
                continue  # out of probe budget -- leave unmeasured, don't rank it
            budget[0] -= 1
            dims = probe_image_dimensions(url)
            _article_image_dims_cache[url] = dims
        if not dims:
            continue
        w, h = dims
        if w < ARTICLE_MIN_WIDTH or h < ARTICLE_MIN_HEIGHT:
            continue
        area = w * h
        if area > best_area:
            best_url, best_area = url, area

    return best_url if best_url is not None else candidates[0]


def is_portrait_image(url):
    """True only if we actually measured this image (via pick_best_image's
    probing, cached in _article_image_dims_cache) and it's clearly taller
    than it is wide -- a portrait source photo that a wide 16:9 hero box
    would crop down to a sliver of, cutting off the subject's head or most
    of the frame. Returns False (never None) when we don't have a
    measurement -- probing failed, or the run's probe budget ran out --
    since an unmeasured photo should still get the normal hero treatment
    rather than being silently downgraded."""
    if not url:
        return False
    dims = _article_image_dims_cache.get(url)
    if not dims:
        return False
    w, h = dims
    return h > w * 1.15


def diversify(pool, key=lambda it: it["source"]):
    """Round-robin a recency-sorted pool across a key (source, or category)
    so one prolific outlet can't crowd out everyone else -- e.g. WORLD
    Magazine posting six times in a row no longer buries CBN/Christian
    Post/Faithwire, and Fox News posting frequently no longer buries NPR/
    NBC/The Hill in Politics. Recency is preserved within each bucket and
    bucket order follows first-seen (i.e. most-recently-active) order."""
    buckets = {}
    order = []
    for item in pool:
        k = key(item)
        if k not in buckets:
            buckets[k] = []
            order.append(k)
        buckets[k].append(item)
    out = []
    i = 0
    remaining = sum(len(v) for v in buckets.values())
    while remaining:
        k = order[i % len(order)]
        if buckets[k]:
            out.append(buckets[k].pop(0))
            remaining -= 1
        i += 1
    return out


GROUP_A_CATEGORIES = {"politics", "markets", "christian"}
# Every other category (world, tech, sports, culture) renders as Group B.
# Deliberately procedural rather than one shared card template repeated
# down the page -- see render_group_a()/render_group_b() below for why.


def render_hero_fallback(item, now, dek_limit=180):
    """Used instead of render_medium_hero()/render_split_hero()'s photo
    treatment whenever the chosen image is portrait-oriented (see
    is_portrait_image()) -- forcing a tall image into a wide 16:9 hero box
    would crop away the subject's head or most of the frame, so a portrait
    photo gets routed here instead: a left-aligned thumbnail (cropped far
    less destructively at thumbnail size) next to the full headline/dek,
    rather than a wide photo slot at all."""
    dek = truncate(item["summary"], dek_limit) or "Read the full story at the source link below."
    meta_time = format_meta_time(item["dt"], now)
    today = "yes" if is_today(item["dt"], now) else "no"
    byline = f'<div class="byline"><span class="source-tag">{esc(item["source"])}</span><span class="lean">{esc(item["lean"])}</span>{bias_meter(item["bias"])}</div>'
    meta = f'<div class="meta"><span class="mono">{esc(meta_time)}</span><a href="{esc(item["url"])}" target="_blank" rel="noopener noreferrer">{esc(item["domain"])} ↗</a></div>'
    thumb = ""
    if item["image"]:
        thumb = f'<img class="thumb cg-hero-thumb" src="{esc(item["image"])}" alt="{esc(item["title"])}" referrerpolicy="no-referrer" onerror="this.style.display=\'none\'">'
    return (f'<div class="cg-block cg-hero-fallback" data-cat="{item["category"]}" data-today="{today}" data-ts="{ts_of(item)}">\n'
            f'        {thumb}\n'
            f'        <div class="chf-col">\n'
            f'          {byline}\n'
            f'          <h3>{esc(item["title"])}</h3>\n'
            f'          <p>{esc(dek)}</p>\n'
            f'          {meta}\n'
            f'        </div>\n'
            f'      </div>')


def render_medium_hero(item, now):
    """Group A's opening card: one medium, bounded photo (max-height:360px,
    16:9, object-fit:cover -- see 12f.css) with a headline underneath.
    A portrait-oriented image is routed to render_hero_fallback() instead
    (see is_portrait_image())."""
    if item["image"] and is_portrait_image(item["image"]):
        return render_hero_fallback(item, now, dek_limit=170)
    dek = truncate(item["summary"], 170) or "Read the full story at the source link below."
    meta_time = format_meta_time(item["dt"], now)
    today = "yes" if is_today(item["dt"], now) else "no"
    byline = f'<div class="byline"><span class="source-tag">{esc(item["source"])}</span><span class="lean">{esc(item["lean"])}</span>{bias_meter(item["bias"])}</div>'
    meta = f'<div class="meta"><span class="mono">{esc(meta_time)}</span><a href="{esc(item["url"])}" target="_blank" rel="noopener noreferrer">{esc(item["domain"])} ↗</a></div>'
    media = ""
    if item["image"]:
        media = (f'<div class="media"><img class="hero-medium-photo" src="{esc(item["image"])}" alt="{esc(item["title"])}" '
                  f'referrerpolicy="no-referrer" onerror="this.closest(\'.media\').style.display=\'none\'">'
                  f'<div class="photo-credit">Photo: via {esc(item["source"])}</div></div>')
    return (f'<div class="cg-block cg-hero-medium" data-cat="{item["category"]}" data-today="{today}" data-ts="{ts_of(item)}">\n'
            f'        {media}\n'
            f'        {byline}\n'
            f'        <h3>{esc(item["title"])}</h3>\n'
            f'        <p>{esc(dek)}</p>\n'
            f'        {meta}\n'
            f'      </div>')


def render_strip_item(item, now):
    """One card in Group A's 3-across horizontal text strip -- headline
    only, no image, no dek, so three fit side by side without crowding."""
    meta_time = format_meta_time(item["dt"], now)
    today = "yes" if is_today(item["dt"], now) else "no"
    byline = f'<div class="byline"><span class="source-tag">{esc(item["source"])}</span><span class="lean">{esc(item["lean"])}</span></div>'
    meta = f'<div class="meta"><span class="mono">{esc(meta_time)}</span><a href="{esc(item["url"])}" target="_blank" rel="noopener noreferrer">{esc(item["domain"])} ↗</a></div>'
    return (f'<div class="cg-strip-item" data-cat="{item["category"]}" data-today="{today}" data-ts="{ts_of(item)}">\n'
            f'          {byline}\n'
            f'          <h4>{esc(item["title"])}</h4>\n'
            f'          {meta}\n'
            f'        </div>')


def render_mixed_item(item, now, with_thumb):
    """One card in Group A's closing 2-column grid -- alternates a small
    thumbnail card with a text-only card (with_thumb picked by the caller),
    same 1-thumb / 1-text-only rhythm the WSJ section fronts use."""
    dek = truncate(item["summary"], 140) or "Read the full story at the source link below."
    meta_time = format_meta_time(item["dt"], now)
    today = "yes" if is_today(item["dt"], now) else "no"
    byline = f'<div class="byline"><span class="source-tag">{esc(item["source"])}</span><span class="lean">{esc(item["lean"])}</span></div>'
    meta = f'<div class="meta"><span class="mono">{esc(meta_time)}</span><a href="{esc(item["url"])}" target="_blank" rel="noopener noreferrer">{esc(item["domain"])} ↗</a></div>'
    if with_thumb and item["image"]:
        thumb = f'<img class="thumb" src="{esc(item["image"])}" alt="{esc(item["title"])}" referrerpolicy="no-referrer" onerror="this.style.display=\'none\'">'
        return (f'<div class="cg-mixed-item" data-cat="{item["category"]}" data-today="{today}" data-ts="{ts_of(item)}">\n'
                f'          {byline}\n'
                f'          <div class="story-row">\n'
                f'            {thumb}\n'
                f'            <div class="story-col">\n'
                f'              <h3>{esc(item["title"])}</h3>\n'
                f'              <p>{esc(dek)}</p>\n'
                f'              {meta}\n'
                f'            </div>\n'
                f'          </div>\n'
                f'        </div>')
    return (f'<div class="cg-mixed-item" data-cat="{item["category"]}" data-today="{today}" data-ts="{ts_of(item)}">\n'
            f'          {byline}\n'
            f'          <h3>{esc(item["title"])}</h3>\n'
            f'          <p>{esc(dek)}</p>\n'
            f'          {meta}\n'
            f'        </div>')


def render_split_hero(item, now):
    """Group B's opening card: headline + dek on the left, a bounded 16:9
    photo (max-height:360px, object-fit:cover) on the right, side by side
    instead of stacked -- the one place on a category tab where the photo
    isn't the full-width lead element. A portrait-oriented image is routed
    to render_hero_fallback() instead (see is_portrait_image())."""
    if item["image"] and is_portrait_image(item["image"]):
        return render_hero_fallback(item, now, dek_limit=200)
    dek = truncate(item["summary"], 200) or "Read the full story at the source link below."
    meta_time = format_meta_time(item["dt"], now)
    today = "yes" if is_today(item["dt"], now) else "no"
    byline = f'<div class="byline"><span class="source-tag">{esc(item["source"])}</span><span class="lean">{esc(item["lean"])}</span>{bias_meter(item["bias"])}</div>'
    meta = f'<div class="meta"><span class="mono">{esc(meta_time)}</span><a href="{esc(item["url"])}" target="_blank" rel="noopener noreferrer">{esc(item["domain"])} ↗</a></div>'
    media = ""
    if item["image"]:
        media = (f'<div class="chs-media"><img class="hero-split-photo" src="{esc(item["image"])}" alt="{esc(item["title"])}" '
                  f'referrerpolicy="no-referrer" onerror="this.closest(\'.chs-media\').style.display=\'none\'"><div class="photo-credit">Photo: via {esc(item["source"])}</div></div>')
    return (f'<div class="cg-block cg-hero-split" data-cat="{item["category"]}" data-today="{today}" data-ts="{ts_of(item)}">\n'
            f'        <div class="chs-text">\n'
            f'          {byline}\n'
            f'          <h3>{esc(item["title"])}</h3>\n'
            f'          <p>{esc(dek)}</p>\n'
            f'          {meta}\n'
            f'        </div>\n'
            f'        {media}\n'
            f'      </div>')


def render_dense_item(item, now, with_thumb):
    """One row in Group B's dense headline list -- compact, no dek, an
    occasional small thumbnail rather than a photo on every row."""
    meta_time = format_meta_time(item["dt"], now)
    today = "yes" if is_today(item["dt"], now) else "no"
    byline = f'<div class="byline"><span class="source-tag">{esc(item["source"])}</span><span class="lean">{esc(item["lean"])}</span></div>'
    meta = f'<div class="meta"><span class="mono">{esc(meta_time)}</span><a href="{esc(item["url"])}" target="_blank" rel="noopener noreferrer">{esc(item["domain"])} ↗</a></div>'
    if with_thumb and item["image"]:
        thumb = f'<img class="thumb thumb-sm" src="{esc(item["image"])}" alt="{esc(item["title"])}" referrerpolicy="no-referrer" onerror="this.style.display=\'none\'">'
        return (f'<div class="cg-dense-item" data-cat="{item["category"]}" data-today="{today}" data-ts="{ts_of(item)}">\n'
                f'          {thumb}\n'
                f'          <div class="cg-dense-col">\n'
                f'            {byline}\n'
                f'            <h4>{esc(item["title"])}</h4>\n'
                f'            {meta}\n'
                f'          </div>\n'
                f'        </div>')
    return (f'<div class="cg-dense-item cg-dense-item--text" data-cat="{item["category"]}" data-today="{today}" data-ts="{ts_of(item)}">\n'
            f'          {byline}\n'
            f'          <h4>{esc(item["title"])}</h4>\n'
            f'          {meta}\n'
            f'        </div>')


def render_group_a(items, now):
    """Politics / Markets / Christian World News: medium hero -> 3-across
    text strip -> 2-column mixed grid (alternating thumb / text-only). Each
    block is its own full-width row (.cg-block spans the outer .flat-grid's
    columns -- see 12f.css) so the inner strip/grid layouts are free to use
    their own column counts independent of the outer grid."""
    if not items:
        return ""
    hero = render_medium_hero(items[0], now)
    rest = items[1:]
    strip_items, grid_items = rest[:3], rest[3:]
    parts = [hero]
    if strip_items:
        strip_body = "\n".join(render_strip_item(it, now) for it in strip_items)
        parts.append(f'      <div class="cg-block cg-strip">\n{strip_body}\n      </div>')
    if grid_items:
        mixed_body = "\n".join(
            render_mixed_item(it, now, with_thumb=(i % 2 == 0) and bool(it["image"]))
            for i, it in enumerate(grid_items)
        )
        parts.append(f'      <div class="cg-block cg-mixed2">\n{mixed_body}\n      </div>')
    return "\n".join(parts)


def render_group_b(items, now):
    """World / Tech / Sports / Culture: split hero (headline left, photo
    right) -> a dense headline list with an occasional small thumbnail
    (every 3rd row) rather than a photo on every row."""
    if not items:
        return ""
    hero = render_split_hero(items[0], now)
    rest = items[1:]
    parts = [hero]
    if rest:
        dense_body = "\n".join(
            render_dense_item(it, now, with_thumb=(i % 3 == 0) and bool(it["image"]))
            for i, it in enumerate(rest)
        )
        parts.append(f'      <div class="cg-block cg-dense">\n{dense_body}\n      </div>')
    return "\n".join(parts)


def render_grid_body(cat, items, now):
    """Dispatches a category's items to its layout group -- Group A
    (politics/markets/christian) or Group B (world/tech/sports/culture).
    Shared by every category tab's grid and by the dedicated Markets /
    Christian World News homepage sections, so a category's layout looks
    the same whether you're viewing it as a tab or as its own section."""
    if not items:
        return ""
    return render_group_a(items, now) if cat in GROUP_A_CATEGORIES else render_group_b(items, now)


def render_cat_grid(cat, items, now):
    """A single category's full, dedicated card grid -- lives hidden inside
    the lead column and is revealed (in place of the mixed top-stories feed)
    whenever that category's nav chip is active. Every category gets one of
    these now (not just Markets/Christian), and its items are drawn from the
    same shared `used` dedup set as the homepage's lead/subs/briefing/digest,
    so a category tab never shows an emptier, gappier echo of the homepage
    nor a story that's already been shown elsewhere on the page."""
    if not items:
        return f'<div class="flat-grid cat-grid" data-cat-grid="{cat}"></div>'
    group = "a" if cat in GROUP_A_CATEGORIES else "b"
    return f'<div class="flat-grid cat-grid cat-grid--{group}" data-cat-grid="{cat}">\n{render_grid_body(cat, items, now)}\n      </div>'


def render_explore_list(items, now):
    """Compact teaser list (same markup as the 'What's News' digest) used by
    the homepage-only 'browse more' rail panels, so the left/right columns
    never run shorter than the lead column and leave visible blank space
    beneath them."""
    return "\n".join(render_digest_item(it, now) for it in items) if items else ""


def build_top_pool(by_category, categories):
    """A single feed for the front page's Top Stories / Briefing / Digest /
    Ticker / Wire that's a genuine mix -- politics, world (incl. sports and
    travel via ESPN/Condé Nast Traveler), markets, Christian, and tech all
    represented -- rather than whichever category happened to publish most
    recently dominating the whole front page."""
    per_cat = {c: diversify(by_category[c]) for c in categories}
    out = []
    i = 0
    remaining = sum(len(v) for v in per_cat.values())
    while remaining:
        c = categories[i % len(categories)]
        if per_cat[c]:
            out.append(per_cat[c].pop(0))
            remaining -= 1
        i += 1
    return out


def main():
    now = datetime.now(timezone.utc)
    by_category = collect_all(now)

    total = sum(len(v) for v in by_category.values())
    print(f"Fetched {total} items across {len(by_category)} categories")
    for cat, items in by_category.items():
        print(f"  {cat}: {len(items)}")

    # Grow the permanent archive with whatever real items we got this run,
    # even if it's a thin run -- articles that make it in never age out of
    # search/browse, regardless of what ends up on the front page below.
    archive = update_archive(by_category, now)

    # Top up any category that came back thin this run (a blocked/broken
    # feed -- ESPN's has been unreliable -- or just a quiet news hour) with
    # recent archived items, so a single bad source never empties out an
    # entire section or tab.
    by_category = backfill_from_archive(by_category, archive)

    total = sum(len(v) for v in by_category.values())
    if total < 10:
        print("Too few items fetched (possible widespread feed outage) -- aborting without touching index.html", file=sys.stderr)
        sys.exit(1)

    used = set()

    # Top pool: a genuine mix of politics + world + markets + Christian +
    # tech + sports + culture, round-robined both across categories and
    # across sources within each category, so the front page reflects a real
    # cross-section of what's breaking right now instead of whichever single
    # category or outlet happened to post most recently.
    top_pool = build_top_pool(by_category, ALL_CATEGORIES)

    lead_item = pick_with_image(top_pool, used)
    lead = [lead_item] if lead_item else []
    # Sub-stories render a photo box next to the headline whenever the item
    # has one, so fill those 6 slots with photo-bearing items first (falling
    # back to photo-less ones only if the pool runs short) rather than
    # whichever 6 happened to be freshest.
    subs = pick_photo_priority(top_pool, 6, used)
    briefing = pick(top_pool, 4, used)
    digest = pick(top_pool, 4, used)
    wire_items = pick(top_pool, 20, set()) or (lead + subs)  # wire allowed to overlap other sections

    # Every category (not just Markets/Christian) now gets its own dedicated,
    # fully-populated card grid -- diversified across sources within that
    # category and drawn from the SAME shared `used` dedup set as the
    # homepage's lead/subs/briefing/digest, so a category tab can never show
    # a story that's already sitting on the homepage, and never needs to
    # fall back to filtering the small mixed top_pool down to a handful of
    # gappy leftovers. If a category still comes back thin even after
    # archive backfill, top it up ignoring `used` entirely (a little
    # recirculation beats an empty tab).
    cat_items = {}
    for c in ALL_CATEGORIES:
        pool = diversify(by_category[c])
        lead_it = pick_with_image(pool, used)
        # Same photo-priority treatment as subs above: every card in a
        # category grid has an image slot, so fill it with photo-bearing
        # articles first rather than whichever came up next in pool order.
        items = ([lead_it] if lead_it else []) + pick_photo_priority(pool, 9, used)
        if len(items) < 6:
            have = {it["url"] for it in items}
            for it in by_category[c]:
                if len(items) >= 6:
                    break
                if it["url"] in have:
                    continue
                have.add(it["url"])
                items.append(it)
        cat_items[c] = items

    markets_items = cat_items.get("markets", [])
    christian_items = cat_items.get("christian", [])

    # Homepage-only, desktop-only "browse more" teaser panels for the left/
    # right rails -- reuses items already selected above (no extra dedup
    # needed). Oversupplied (up to 4 per topic) on purpose: client-side JS
    # trims each panel down to however many items fit before it would run
    # past the bottom of the lead column, so the panel's bottom edge lines
    # up with the top-stories column instead of over- or under-shooting it.
    explore_right_items = (cat_items.get("tech") or [])[:4] + (cat_items.get("world") or [])[:4]
    explore_left_items = (cat_items.get("sports") or [])[:4] + (cat_items.get("culture") or [])[:4]

    if not lead:
        print("No lead story available -- aborting without touching index.html", file=sys.stderr)
        sys.exit(1)

    html_text = open(INDEX_PATH, encoding="utf-8").read()

    briefing_html = "\n".join(render_briefing_item(it) for it in briefing) if briefing else html_text
    if briefing:
        html_text = replace_between(html_text, "<!-- AUTO:BRIEFING_START -->", "<!-- AUTO:BRIEFING_END -->", briefing_html)

    if digest:
        digest_html = "\n".join(render_digest_item(it, now) for it in digest)
        html_text = replace_between(html_text, "<!-- AUTO:DIGEST_START -->", "<!-- AUTO:DIGEST_END -->", digest_html)

    lead_html = render_lead_story(lead[0], now)
    lead_html += '\n\n      <div class="hr"></div>\n'
    for i, s in enumerate(subs):
        lead_html += "\n" + render_sub_story(s, now)
        if i < len(subs) - 1:
            lead_html += '\n\n      <div class="hr"></div>\n'
    html_text = replace_between(html_text, "<!-- AUTO:LEAD_START -->", "<!-- AUTO:LEAD_END -->", lead_html)

    if markets_items:
        html_text = replace_between(html_text, "<!-- AUTO:MARKETS_START -->", "<!-- AUTO:MARKETS_END -->", render_grid_body("markets", markets_items, now))

    if christian_items:
        html_text = replace_between(html_text, "<!-- AUTO:CHRISTIAN_START -->", "<!-- AUTO:CHRISTIAN_END -->", render_grid_body("christian", christian_items, now))

    catgrids_html = "\n".join(render_cat_grid(c, cat_items.get(c, []), now) for c in ALL_CATEGORIES)
    html_text = replace_between(html_text, "<!-- AUTO:CATGRIDS_START -->", "<!-- AUTO:CATGRIDS_END -->", catgrids_html)

    html_text = replace_between(html_text, "<!-- AUTO:EXPLORE_RIGHT_START -->", "<!-- AUTO:EXPLORE_RIGHT_END -->", render_explore_list(explore_right_items, now))
    html_text = replace_between(html_text, "<!-- AUTO:EXPLORE_LEFT_START -->", "<!-- AUTO:EXPLORE_LEFT_END -->", render_explore_list(explore_left_items, now))

    if wire_items:
        html_text = replace_between(html_text, "<!-- AUTO:WIRE_START -->", "<!-- AUTO:WIRE_END -->", render_wire_js(wire_items, now))

    html_text = replace_between(html_text, "<!-- AUTO:BIAS_START -->", "<!-- AUTO:BIAS_END -->", render_bias_js())

    stamp = now.astimezone(ET).strftime("%b %-d, %Y, %-I:%M %p ET") if _supports_dash(now) else now.astimezone(ET).strftime("%b %d, %Y, %I:%M %p ET")
    html_text = replace_between(html_text, "<!-- AUTO:UPDATED_START -->", "<!-- AUTO:UPDATED_END -->", stamp, inline=True)

    write_source_status(now)

    open(INDEX_PATH, "w", encoding="utf-8").write(html_text)
    print("index.html rewritten successfully.")


if __name__ == "__main__":
    main()
