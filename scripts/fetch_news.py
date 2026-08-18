#!/usr/bin/env python3
"""
12F hourly refresh script.

Pulls fresh headlines from each configured source's RSS feed, sorts them into
the site's existing categories (politics / markets / christian / world /
tech), and rewrites the static sections of index.html between AUTO: markers.
Nothing here talks to any AI model or paid API -- it's plain RSS parsing plus
deterministic templating, designed to run unattended, forever, on GitHub
Actions' own schedule.

If a feed is down or returns nothing, that source is just skipped for this
run -- we never let one flaky feed break the whole refresh.
"""
import html
import json
import random
import re
import socket
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
        "domain": "espn.com", "bias": "bias-C", "lean": {"sports": "Sports"},
        "single_category": "sports",
        "feeds": {"sports": "https://www.espn.com/espn/rss/news"},
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
        "domain": "nypost.com", "bias": "bias-R", "lean": {"world": "World"},
        "single_category": "world",
        "feeds": {"world": "https://nypost.com/feed/"},
    },
}

BIAS_LABEL = {"bias-L": "Left", "bias-CL": "Lean Left", "bias-C": "Center", "bias-CR": "Lean Right", "bias-R": "Right"}
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


def extract_image(entry):
    for key in ("media_content", "media_thumbnail"):
        media = entry.get(key)
        if media:
            for m in media:
                url = m.get("url")
                if url:
                    return url
    for enc in entry.get("enclosures", []) or []:
        etype = (enc.get("type") or "")
        if etype.startswith("image") or not etype:
            if enc.get("href"):
                return enc["href"]
    for field in ("summary", "description"):
        raw = entry.get(field)
        if raw:
            m = IMG_TAG_RE.search(raw)
            if m:
                return m.group(1)
    content = entry.get("content")
    if content:
        for c in content:
            m = IMG_TAG_RE.search(c.get("value", ""))
            if m:
                return m.group(1)
    return None


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
    out = []
    for e in feed.entries[:limit]:
        title = clean_text(e.get("title", ""))
        link = e.get("link", "")
        if not title or not link:
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
            "image": extract_image(e),
            "dt": entry_datetime(e),
        })
    SOURCE_STATS.append((source_name, category, len(out), "ok"))
    return out


def collect_all(now):
    """Fetch every configured feed. Returns dict: category -> list[item]."""
    SOURCE_STATS.clear()
    by_category = {c: [] for c in ALL_CATEGORIES}
    for source_name, cfg in SOURCES.items():
        forced_cat = cfg.get("single_category")
        for category, url in cfg["feeds"].items():
            target_cat = forced_cat or category
            items = fetch_source_category(source_name, cfg, target_cat, url)
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


def render_flat_item(item, now, with_photo=False):
    dek = truncate(item["summary"], 170) or "Read the full story at the source link below."
    meta_time = format_meta_time(item["dt"], now)
    today = "yes" if is_today(item["dt"], now) else "no"
    byline = f'<div class="byline"><span class="source-tag">{esc(item["source"])}</span><span class="lean">{esc(item["lean"])}</span>{bias_meter(item["bias"])}</div>'
    meta = f'<div class="meta"><span class="mono">{esc(meta_time)}</span><a href="{esc(item["url"])}" target="_blank" rel="noopener noreferrer">{esc(item["domain"])} ↗</a></div>'
    ts = ts_of(item)
    if with_photo and item["image"]:
        return (f'<div class="flat-item flat-item-photo" data-cat="{item["category"]}" data-today="{today}" data-ts="{ts}">\n'
                f'        <div class="media"><img class="cover-photo" src="{esc(item["image"])}" alt="{esc(item["title"])}" '
                f'referrerpolicy="no-referrer" onerror="this.closest(\'.media\').style.display=\'none\'"><div class="photo-credit">Photo: via {esc(item["source"])}</div></div>\n'
                f'        {byline}\n'
                f'        <h3>{esc(item["title"])}</h3>\n'
                f'        <p>{esc(dek)}</p>\n'
                f'        {meta}\n'
                f'      </div>')
    thumb = f'<img class="thumb" src="{esc(item["image"])}" alt="{esc(item["title"])}" referrerpolicy="no-referrer" onerror="this.style.display=\'none\'">' if item["image"] else ""
    if thumb:
        return (f'<div class="flat-item" data-cat="{item["category"]}" data-today="{today}" data-ts="{ts}">\n'
                f'        {byline}\n'
                f'        <div class="story-row">\n'
                f'          {thumb}\n'
                f'          <div class="story-col">\n'
                f'            <h3>{esc(item["title"])}</h3>\n'
                f'            <p>{esc(dek)}</p>\n'
                f'            {meta}\n'
                f'          </div>\n'
                f'        </div>\n'
                f'      </div>')
    return (f'<div class="flat-item" data-cat="{item["category"]}" data-today="{today}" data-ts="{ts}">\n'
            f'        {byline}\n'
            f'        <h3>{esc(item["title"])}</h3>\n'
            f'        <p>{esc(dek)}</p>\n'
            f'        {meta}\n'
            f'      </div>')


def js_str(s):
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'


def render_ticker_js(items):
    lines = []
    for i, item in enumerate(items):
        tag = "BREAKING" if i == 0 else ""
        lines.append(f'    [{js_str(tag)},{js_str(item["title"])},{js_str(item["source"])},{js_str(item["url"])}],')
    return "  const headlines = [\n" + "\n".join(lines) + "\n  ];"


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
    update_archive(by_category, now)

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
    subs = pick(top_pool, 6, used)
    briefing = pick(top_pool, 4, used)
    digest = pick(top_pool, 4, used)
    ticker_items = pick(top_pool, 18, used) or (lead + subs)
    wire_items = pick(top_pool, 20, set()) or ticker_items  # wire allowed to overlap ticker

    # Markets / Christian sections: diversified across every source that
    # feeds that category (not just recency) so, e.g., the Christian World
    # News grid actually shows CBN, Christian Post, Christianity Today,
    # Religion News Service and Faithwire alongside WORLD Magazine, and
    # Markets shows MarketWatch/Investing.com alongside the wire services.
    markets_pool = diversify(by_category["markets"])
    christian_pool = diversify(by_category["christian"])
    markets_used = set()
    christian_used = set()
    markets_lead = pick_with_image(markets_pool, markets_used)
    markets_items = ([markets_lead] if markets_lead else []) + pick(markets_pool, 5, markets_used)
    christian_lead = pick_with_image(christian_pool, christian_used)
    christian_items = ([christian_lead] if christian_lead else []) + pick(christian_pool, 7, christian_used)

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
        m_html = render_flat_item(markets_items[0], now, with_photo=True)
        for it in markets_items[1:]:
            m_html += "\n" + render_flat_item(it, now)
        html_text = replace_between(html_text, "<!-- AUTO:MARKETS_START -->", "<!-- AUTO:MARKETS_END -->", m_html)

    if christian_items:
        c_html = render_flat_item(christian_items[0], now, with_photo=True)
        for it in christian_items[1:]:
            c_html += "\n" + render_flat_item(it, now)
        html_text = replace_between(html_text, "<!-- AUTO:CHRISTIAN_START -->", "<!-- AUTO:CHRISTIAN_END -->", c_html)

    if ticker_items:
        html_text = replace_between(html_text, "<!-- AUTO:TICKER_START -->", "<!-- AUTO:TICKER_END -->", render_ticker_js(ticker_items))
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
