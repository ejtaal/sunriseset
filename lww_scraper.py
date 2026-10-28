#!/usr/bin/env python3
"""
Live World Webcams (liveworldwebcams.com) scraper -- static metadata edition.

Extracts name, location, country, region, theme/category, URL, thumbnail
and description for every webcam listed in the site's sitemaps.
Stores everything in a local SQLite database.

Usage:
    python3 lww_scraper.py                # full crawl
    python3 lww_scraper.py --limit 50     # crawl first N only
    python3 lww_scraper.py --resume       # skip pages fetched in last 7 days
    python3 lww_scraper.py --quiet        # less verbose output
"""

import argparse
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_URL = "https://liveworldwebcams.com"
SITEMAP_INDEX = f"{BASE_URL}/sitemap_index.xml"
DB_PATH = os.path.join("/tmp", "lww_webcams.db")
DEFAULT_DB_PATH = DB_PATH
REPO_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
REPO_DB_PATH = os.path.join(REPO_DATA_DIR, "lww_webcams.db")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
REQUEST_DELAY = 0.5  # seconds between requests
MAX_RETRIES = 3
RETRY_DELAY = 3
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})

# Logging: dual output to file + console for monitoring
LOG_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(LOG_DIR, "scraper.log")

logger = logging.getLogger("lww_scraper")
logger.setLevel(logging.DEBUG)

# File handler — full detail
fh = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
fh.setLevel(logging.DEBUG)
fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
logger.addHandler(fh)

# Console handler — INFO+
ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.INFO)
ch.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S"))
logger.addHandler(ch)

log = logger

# Known non-webcam slugs to skip immediately
NON_WEBCAM_SLUGS = {
    "live-satellite-tracker",
    "bird-migration-map",
    "live-earthquake-map",
    "northern-lights-tonight",
    "live-webcam-map",
    "what-s-happening-now",
    "embed-a-webcam",
    "articles",
    "about-us",
    "contact",
    "faq",
    "privacy-policy",
    "terms-of-use",
    "recently-added-webcams",
    "warmest-places",
    "coldest-places",
    "trending-webcams",
}

# Known continent/category slugs
NON_WEBCAM_CATEGORY_SLUGS = {
    "americas", "europe", "asia", "oceania", "africa", "caribbean",
    "united-states", "canada", "united-kingdom",
}

# ---------------------------------------------------------------------------
# Sitemap parsing
# ---------------------------------------------------------------------------
NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9",
      "img": "http://www.google.com/schemas/sitemap-image/1.1"}
IMG_NS = "http://www.google.com/schemas/sitemap-image/1.1"

def _xml_find(parent, tag: str):
    """Find first child element matching a tag (with sitemap namespace)."""
    if isinstance(parent, str):
        from xml.etree import ElementTree as ET
        root = ET.fromstring(parent)
        return root.find(f"{{{NS['s']}}}{tag}")
    return parent.find(f"{{{NS['s']}}}{tag}")

def _parse_sitemap_xml(text: str) -> list:
    """Parse sitemap XML text and return root Element."""
    from xml.etree import ElementTree as ET
    return ET.fromstring(text)

def fetch_sitemap_index() -> list[str]:
    """Fetch sitemap index and return list of sitemap URLs."""
    log.info("Fetching sitemap index …")
    resp = SESSION.get(SITEMAP_INDEX, timeout=30)
    resp.raise_for_status()
    root = _parse_sitemap_xml(resp.text)
    urls = []
    ns = f"{{{NS['s']}}}"
    for child in root.findall(f"{ns}sitemap"):
        loc_el = child.find(f"{ns}loc")
        if loc_el is not None and loc_el.text:
            urls.append(loc_el.text.strip())
    log.info("Found %d sitemap files", len(urls))
    return urls

def fetch_sitemap_urls(sitemap_url: str) -> list[dict]:
    """Fetch a post sitemap and return list of {url, lastmod, image} dicts."""
    resp = SESSION.get(sitemap_url, timeout=30)
    resp.raise_for_status()
    root = _parse_sitemap_xml(resp.text)
    results = []
    ns = f"{{{NS['s']}}}"
    for url_elem in root.findall(f"{ns}url"):
        loc_el = url_elem.find(f"{ns}loc")
        lastmod_el = url_elem.find(f"{ns}lastmod")

        # Image (Google sitemap image namespace)
        img_loc = ""
        for child in url_elem:
            tag = child.tag
            if "google.com" in tag and "loc" in tag:
                if child.text:
                    img_loc = child.text.strip()
                break

        entry = {}
        if loc_el is not None and loc_el.text:
            entry["url"] = loc_el.text.strip()
        if lastmod_el is not None and lastmod_el.text:
            entry["lastmod"] = lastmod_el.text.strip()
        if img_loc:
            entry["image"] = img_loc
        if "url" in entry:
            results.append(entry)
    return results

def collect_all_webcam_urls() -> list[dict]:
    """Fetch all sitemaps, filter to webcam-only URLs, return list."""
    sitemap_urls = fetch_sitemap_index()
    all_entries: list[dict] = []
    seen_urls = set()

    for sm_url in sitemap_urls:
        log.info("Fetching %s …", sm_url)
        try:
            entries = fetch_sitemap_urls(sm_url)
            log.info("  → %d entries", len(entries))
        except Exception as e:
            log.warning("  → failed: %s", e)
            continue
        for e in entries:
            if e["url"] not in seen_urls:
                seen_urls.add(e["url"])
                all_entries.append(e)

    log.info("Total unique URLs: %d", len(all_entries))

    # Filter: only keep URLs that are likely webcam pages
    filtered = []
    for e in all_entries:
        url = e["url"]
        path = urlparse(url).path.rstrip("/")
        slug = path.split("/")[-1] if path else ""

        # Skip known non-webcam pages
        if slug in NON_WEBCAM_SLUGS:
            continue

        # Skip category/archive listing pages (they contain no actual webcam data)
        if slug in NON_WEBCAM_CATEGORY_SLUGS:
            continue
        # Skip pages that are just category directories (e.g. /category/usa/)
        if re.search(r"/category/[^/]+$", url):
            continue
        # Skip tag pages
        if re.search(r"/tag/[^/]+$", url):
            continue
        # Skip article pages
        if re.search(r"/lww_articles/", url):
            continue

        # Must contain "webcam" somewhere in the URL (case-insensitive)
        if "webcam" not in url.lower():
            continue

        filtered.append(e)

    log.info("Filtered: %d → %d webcam pages", len(all_entries), len(filtered))
    return filtered

# ---------------------------------------------------------------------------
# HTML parser -- extract static metadata from a webcam page
# ---------------------------------------------------------------------------

KNOWN_THEMES = {
    "cities and towns", "beaches", "lakes and rivers", "ports", "mountains",
    "landscapes", "ski slopes", "railways", "wildlife", "airports",
    "birds", "cruise ships", "traffic", "universities",
    "world heritage sites", "surfing", "lighthouses", "indoor",
    "volcanoes", "amusement parks", "aurora borealis", "glaciers",
    "underwater", "zoo", "space", "markets", "time travel",
}

KNOWN_REGIONS = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
    "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york",
    "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington",
    "washington, d.c.", "west virginia", "wisconsin", "wyoming",
    "alberta", "british columbia", "manitoba", "new brunswick",
    "newfoundland and labrador", "northwest territories", "nova scotia",
    "ontario", "prince edward island", "quebec", "saskatchewan",
    "england", "northern ireland", "scotland", "wales",
}

KNOWN_COUNTRIES = {
    "united states", "canada", "argentina", "mexico", "brazil", "chile",
    "panama", "ecuador", "bermuda", "colombia", "costa rica", "guatemala",
    "honduras", "peru", "venezuela", "barbados", "the bahamas",
    "germany", "norway", "italy", "spain", "netherlands", "austria",
    "finland", "belarus", "belgium", "bosnia-herzegovina", "bulgaria",
    "croatia", "cyprus", "czech republic", "denmark", "estonia",
    "faroe islands", "france", "georgia", "greece", "greenland",
    "hungary", "iceland", "ireland", "jersey", "latvia", "lithuania",
    "malta", "montenegro", "north macedonia", "poland", "portugal",
    "romania", "serbia", "slovakia", "slovenia", "sweden", "switzerland",
    "ukraine", "vatican city", "united kingdom", "japan", "russia",
    "taiwan", "israel", "turkey", "thailand", "indonesia", "south korea",
    "armenia", "azerbaijan", "china", "india", "iran", "jordan",
    "kyrgyzstan", "lebanon", "malaysia", "maldives", "nepal", "pakistan",
    "palestinian territories", "philippines", "qatar", "saudi arabia",
    "singapore", "united arab emirates", "vietnam", "new zealand",
    "australia", "french polynesia", "guam", "new caledonia",
    "south africa", "namibia", "kenya", "botswana", "congo (drc)",
    "congo", "egypt", "morocco", "cape verde", "mauritius", "tanzania",
    "zimbabwe", "jamaica", "us virgin islands", "british virgin islands",
    "curacao", "dominican republic", "sint maarten", "st. barts",
    "cayman islands", "anguilla", "aruba", "bonaire", "puerto rico",
    "turks and caicos",
}

CONTINENTS = {
    "america", "europe", "asia", "oceania", "africa", "caribbean",
    "the caribbean",
}


def extract_title(soup: BeautifulSoup) -> str:
    """Extract the webcam title."""
    h1 = soup.find("h1")
    if h1:
        return h1.get_text(strip=True)
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        return og["content"]
    title_tag = soup.find("title")
    if title_tag:
        return title_tag.get_text(strip=True)
    return ""


def extract_description(soup: BeautifulSoup) -> str:
    """Extract meta description."""
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        return meta["content"]
    og = soup.find("meta", property="og:description")
    if og and og.get("content"):
        return og["content"]
    return ""


def extract_categories(soup: BeautifulSoup) -> list[str]:
    """Extract categories from the page (not from global navigation)."""
    categories = []

    # Skip the global nav area -- extract categories from the body content only
    # Find the main content area (typically after the nav)
    nav = soup.find("nav")
    body_content = soup.find("body")
    if not body_content:
        body_content = soup

    # Remove nav elements from consideration
    for tag in body_content.find_all("nav"):
        tag.decompose()

    # Look for category pills/badges in the body content
    for tag in body_content.find_all(re.compile(r"a|span", re.I)):
        cls = tag.get("class", [])
        if isinstance(cls, str):
            cls = cls.split()
        cls_str = " ".join(c.lower() for c in cls)

        # Only look at tags with category-like classes
        if not any(k in cls_str for k in ["category", "tag", "taxonomy", "cam-tag", "cam-category", "nav-mega__sublink"]):
            continue

        text = tag.get_text(strip=True)
        if not text or len(text) < 2:
            continue

        # Skip navigation noise
        skip_words = {"all", "view", "more", "hide", "show", "back", "home",
                      "live", "share", "report", "embed", "cancel", "yes"}
        if text.lower() in skip_words:
            continue

        categories.append(text)

    # Look for breadcrumb trail in the body content
    breadcrumb = body_content.find(class_=re.compile(r"breadcrumb", re.I))
    if breadcrumb:
        for item in breadcrumb.find_all("a"):
            text = item.get_text(strip=True)
            if text and text not in [c.lower() for c in categories] and text not in ("Home", "All"):
                if len(text) > 2:
                    categories.append(text)

    # Deduplicate preserving order
    seen = set()
    unique = []
    for c in categories:
        cl = c.lower().strip()
        if cl not in seen:
            seen.add(cl)
            unique.append(c)
    return unique


def extract_location(soup: BeautifulSoup) -> str:
    """Extract location/address string."""
    # Look for 📍 marker
    text = soup.get_text()
    loc_m = re.search(r"📍\s*(.+?)(?:\n|$)", text)
    if loc_m:
        return loc_m.group(1).strip()

    # Try data-location attribute
    loc_tag = soup.find(attrs={"data-location": True})
    if loc_tag and loc_tag.get("data-location"):
        return loc_tag["data-location"]

    return ""


def extract_about_text(soup: BeautifulSoup) -> str:
    """Extract the "About this webcam" description."""
    text = soup.get_text()

    # Try "About this webcam" section
    about_m = re.search(
        r"(?:About\s+this\s+(?:webcam|camera)[:\s]*)(.*?)(?:About\s|Things to Do|Weather|Source|Frequently|Similar)",
        text, re.DOTALL | re.IGNORECASE
    )
    if about_m:
        return about_m.group(1).strip()

    return ""


def extract_stream_url(soup: BeautifulSoup) -> dict:
    """Extract YouTube stream URL and video ID from iframe or page content."""
    stream_url = ""
    video_id = ""

    # Look for YouTube iframe
    for iframe in soup.find_all("iframe"):
        src = iframe.get("src", "") or iframe.get("data-src", "")
        if "youtube.com/embed/" in src or "youtu.be/" in src:
            stream_url = src
            # Extract video ID from embed URL
            id_m = re.search(r"youtube\.com/embed/([A-Za-z0-9_-]{11})", src)
            if id_m:
                video_id = id_m.group(1)
            break

    # Also check for srcdoc (embedded iframe in srcdoc attribute)
    srcdoc_tag = soup.find(attrs={"srcdoc": True}) or soup.find(attrs={"data-srcdoc": True})
    if not stream_url and srcdoc_tag:
        srcdoc = srcdoc_tag.get("srcdoc", srcdoc_tag.get("data-srcdoc", ""))
        iframe_src = re.search(r'src="([^"]*youtube\.com/embed/[^"]+)"', srcdoc)
        if iframe_src:
            stream_url = iframe_src.group(1)
            id_m = re.search(r"youtube\.com/embed/([A-Za-z0-9_-]{11})", stream_url)
            if id_m:
                video_id = id_m.group(1)

    # Fallback: search for YouTube embed URL in script/data tags
    if not stream_url:
        texts = soup.get_text()
        ytm = re.search(r'https?://(?:www\.)?youtube\.com/embed/([A-Za-z0-9_-]{11})', texts)
        if ytm:
            video_id = ytm.group(1)
            stream_url = f"https://www.youtube.com/embed/{video_id}"

    # Also check for YouTube watch URL
    watch_url = ""
    if not watch_url:
        wtm = re.search(r'https?://(?:www\.)?youtube\.com/watch\?v=([A-Za-z0-9_-]{11})', soup.get_text())
        if wtm:
            watch_url = f"https://www.youtube.com/watch?v={wtm.group(1)}"
        elif video_id:
            watch_url = f"https://www.youtube.com/watch?v={video_id}"

    return {
        "stream_url": stream_url,
        "video_id": video_id,
        "youtube_watch_url": watch_url,
    }


def extract_coordinates(soup: BeautifulSoup) -> tuple[str, str]:
    """Extract latitude and longitude from the webcam map div."""
    lat = ""
    lng = ""

    map_div = soup.find("div", id="webcam-map")
    if map_div:
        lat = map_div.get("data-lat", "")
        lng = map_div.get("data-lng", "") or map_div.get("data-lon", "")

    return lat, lng


def classify_categories(categories: list[str]) -> tuple[str, str]:
    """Classify categories into (country, region)."""
    country = ""
    region = ""

    for c in categories:
        cl = c.lower().strip()

        # Continent -- skip
        if cl in CONTINENTS:
            continue

        # Known country
        if cl in KNOWN_COUNTRIES and not country:
            country = c
            continue

        # Known region/state
        if cl in KNOWN_REGIONS and not region:
            region = c
            continue

    return country, region


def parse_webcam_page(url: str, soup: BeautifulSoup) -> dict | None:
    """Extract all static metadata. Returns dict or None if not a webcam page."""
    title = extract_title(soup)

    # Must be a webcam page
    if "webcam" not in title.lower():
        return None

    description = extract_description(soup)
    categories = extract_categories(soup)
    location = extract_location(soup)
    about = extract_about_text(soup)
    country, region = classify_categories(categories)

    # Extract stream URLs
    stream_data = extract_stream_url(soup)

    # Extract coordinates
    lat, lng = extract_coordinates(soup)

    return {
        "title": title,
        "description": description,
        "location": location,
        "country": country,
        "region": region,
        "categories": json.dumps(categories),
        "about_text": about,
        "stream_url": stream_data["stream_url"],
        "video_id": stream_data["video_id"],
        "youtube_watch_url": stream_data["youtube_watch_url"],
        "latitude": lat,
        "longitude": lng,
    }


def fetch_and_parse(url: str) -> dict | None:
    """Fetch and parse a webcam page."""
    try:
        resp = SESSION.get(url, timeout=20)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "lxml")
        return parse_webcam_page(url, soup)
    except Exception as e:
        log.debug("Error fetching %s: %s", url, e)
        return None

# ---------------------------------------------------------------------------
# Database setup
# ---------------------------------------------------------------------------
def init_db(db_path: str) -> sqlite3.Connection:
    """Create the SQLite database."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-64000")
        conn.execute("PRAGMA mmap_size=0")
    except Exception:
        pass

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS webcams (
            url             TEXT PRIMARY KEY,
            id              TEXT,
            title           TEXT,
            description     TEXT,
            location        TEXT,
            country         TEXT,
            region          TEXT,
            categories      TEXT,
            about_text      TEXT,
            thumbnail_url   TEXT,
            stream_url      TEXT,
            video_id        TEXT,
            youtube_watch_url TEXT,
            latitude        TEXT,
            longitude       TEXT,
            lastmod         TEXT,
            fetched_at      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_webcams_country ON webcams(country);
        CREATE INDEX IF NOT EXISTS idx_webcams_region ON webcams(region);
        CREATE INDEX IF NOT EXISTS idx_webcams_title ON webcams(title);
        CREATE INDEX IF NOT EXISTS idx_webcams_fetched ON webcams(fetched_at);
    """)
    # Add new columns if they don't exist (migration)
    for col in ("stream_url", "video_id", "youtube_watch_url", "latitude", "longitude"):
        try:
            conn.execute(f"ALTER TABLE webcams ADD COLUMN {col} TEXT")
        except Exception:
            pass  # column already exists
    conn.commit()
    return conn


def upsert_webcam(conn: sqlite3.Connection, entry: dict, source: str = "liveworldwebcams.com"):
    """Insert or update a webcam record."""
    conn.execute("""
        INSERT INTO webcams (url, id, title, description, location, country,
                             region, categories, about_text, thumbnail_url,
                             stream_url, video_id, youtube_watch_url,
                             latitude, longitude, lastmod, fetched_at, source)
        VALUES (:url, :id, :title, :description, :location, :country, :region,
                :categories, :about_text, :thumbnail_url,
                :stream_url, :video_id, :youtube_watch_url,
                :latitude, :longitude, :lastmod, :fetched_at, :source)
        ON CONFLICT(url) DO UPDATE SET
            id              = excluded.id,
            title           = excluded.title,
            description     = excluded.description,
            location        = excluded.location,
            country         = excluded.country,
            region          = excluded.region,
            categories      = excluded.categories,
            about_text      = excluded.about_text,
            thumbnail_url   = excluded.thumbnail_url,
            stream_url      = excluded.stream_url,
            video_id        = excluded.video_id,
            youtube_watch_url = excluded.youtube_watch_url,
            latitude        = excluded.latitude,
            longitude       = excluded.longitude,
            lastmod         = excluded.lastmod,
            fetched_at      = excluded.fetched_at,
            source          = excluded.source
    """, {**entry, "source": source})


def db_id(url: str) -> str:
    """Deterministic DB ID from URL -- use full path to avoid collisions."""
    path = urlparse(url).path.rstrip("/")
    # Convert to a safe slug: lowercase, replace / with _
    slug = path.lower().replace("/", "_").replace(" ", "_")
    return slug.strip("_")


class ProgressTracker:
    """Tracks scrape progress and writes periodic summaries."""

    def __init__(self, total: int, log_file: str):
        self.total = total
        self.done = 0
        self.failed = 0
        self.skipped = 0
        self.log_file = log_file
        self.last_progress = time.time()
        self.start_time = time.time()

    def record(self, success: bool = False, skip: bool = False):
        if skip:
            self.skipped += 1
        elif success:
            self.done += 1
        else:
            self.failed += 1

    def tick(self, url: str = ""):
        """Call once per URL processed. Logs progress every 60s and at milestones."""
        elapsed = time.time() - self.start_time
        rate = self.done / elapsed if elapsed > 0 else 0
        eta = (self.total - self.done) / rate if rate > 0 else 0

        # Always log current URL
        log.debug("[%d/%d] %s", self.done + self.failed + self.skipped + 1, self.total, url)

        # Periodic summary every 60s
        now = time.time()
        if now - self.last_progress >= 60 or self.done + self.failed + self.skipped >= self.total:
            pct = 100 * (self.done + self.skipped) / self.total if self.total else 0
            log.info(
                "Progress: %d/%d (%.1f%%) — success=%d failed=%d skipped=%d — %.1f pages/min, ETA ~%.0fm",
                self.done + self.failed + self.skipped,
                self.total,
                pct,
                self.done,
                self.failed,
                self.skipped,
                self.done / (elapsed / 60) if elapsed > 0 else 0,
                eta / 60,
            )
            self.last_progress = now


def copy_db_to_repo(src_db: str, dest_db: str) -> None:
    """Copy the working DB to the repo data/ folder for versioning."""
    os.makedirs(os.path.dirname(dest_db), exist_ok=True)
    if os.path.exists(src_db):
        shutil.copy2(src_db, dest_db)
        log.info("Database copied to: %s", dest_db)
    else:
        log.warning("Source DB not found, skipping copy: %s", src_db)



# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Scrape Live World Webcams metadata")
    parser.add_argument("--limit", type=int, default=0,
                        help="Only process first N webcam URLs (0 = all)")
    parser.add_argument("--resume", action="store_true",
                        help="Skip pages fetched in last 7 days")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress progress output")
    parser.add_argument("--db", type=str, default=DB_PATH,
                        help=f"Database path (default: {DB_PATH})")
    args = parser.parse_args()

    if args.quiet:
        logging.getLogger().setLevel(logging.WARNING)

    log.info("=" * 60)
    log.info("Live World Webcams Scraper (static metadata)")
    log.info("=" * 60)

    # Step 1: Collect URLs
    webcam_urls = collect_all_webcam_urls()
    if not webcam_urls:
        log.error("No webcam URLs found.")
        sys.exit(1)

    if args.limit > 0:
        webcam_urls = webcam_urls[:args.limit]
        log.info("Limiting to first %d entries", args.limit)

    # Step 2: Init DB
    log.info("Database: %s", args.db)
    conn = init_db(args.db)

    cur = conn.execute("SELECT COUNT(*) FROM webcams")
    existing = cur.fetchone()[0]
    log.info("Existing records: %d", existing)

    # Step 3: Scrape
    total = len(webcam_urls)
    tracker = ProgressTracker(total, LOG_FILE)

    for i, entry in enumerate(webcam_urls, 1):
        url = entry["url"]

        # Skip URLs already in the DB (unless --resume is used and we want to refresh)
        if not args.resume:
            cur = conn.execute("SELECT 1 FROM webcams WHERE url = ?", (url,))
            if cur.fetchone():
                tracker.record(skip=True)
                tracker.tick(url)
                continue

        result = fetch_and_parse(url)
        if not result:
            tracker.record(success=False)
            tracker.tick(url)
            continue

        record = {
            "id": db_id(url),
            "url": url,
            **result,
            "thumbnail_url": entry.get("image", ""),
            "lastmod": entry.get("lastmod", ""),
            "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

        upsert_webcam(conn, record, source="liveworldwebcams.com")
        conn.commit()
        tracker.record(success=True)
        tracker.tick(url)

        # Rate limiting
        if i % 20 == 0:
            log.info("  (batch of 20, %.1fs delay ...)", REQUEST_DELAY)
            time.sleep(REQUEST_DELAY)
        else:
            time.sleep(REQUEST_DELAY * 0.5)

    # Step 4: Summary
    conn.commit()
    cur = conn.execute("SELECT COUNT(*) FROM webcams")
    total_db = cur.fetchone()[0]

    # Stats by country
    cur = conn.execute("""
        SELECT country, COUNT(*) as cnt
        FROM webcams WHERE country != ''
        GROUP BY country ORDER BY cnt DESC LIMIT 20
    """)
    country_stats = cur.fetchall()

    # Stats by region
    cur = conn.execute("""
        SELECT region, COUNT(*) as cnt
        FROM webcams WHERE region != ''
        GROUP BY region ORDER BY cnt DESC LIMIT 20
    """)
    region_stats = cur.fetchall()

    # Stats by theme (exclude countries and regions from themes)
    cur = conn.execute("""
        SELECT json_extract(categories, '$[0]') as theme, COUNT(*) as cnt
        FROM webcams WHERE categories != '[]'
        GROUP BY theme ORDER BY cnt DESC LIMIT 20
    """)
    theme_stats = cur.fetchall()
    # Filter out country/region names from theme stats
    all_countries = set(r[0] for r in country_stats)
    all_regions = set(r[0] for r in region_stats)
    theme_stats = [(t, c) for t, c in theme_stats if t not in all_countries and t not in all_regions]

    log.info("\n" + "=" * 60)
    log.info("COMPLETE")
    log.info("=" * 60)
    log.info("  Records scraped:  %d", success)
    log.info("  Skipped (resume): %d", skipped)
    log.info("  Failed:           %d", failed)
    log.info("  Total in DB:      %d", total_db)

    if country_stats:
        log.info("\n  Top countries:")
        for country, cnt in country_stats:
            log.info("    %-25s %d", country, cnt)

    if region_stats:
        log.info("\n  Top regions:")
        for region, cnt in region_stats:
            log.info("    %-30s %d", region, cnt)

    if theme_stats:
        log.info("\n  Top themes:")
        for theme, cnt in theme_stats:
            log.info("    %-25s %d", theme or "-", cnt)

    # Show sample records
    cur = conn.execute("""
        SELECT url, title, country, region, categories, thumbnail_url
        FROM webcams ORDER BY rowid LIMIT 10
    """)
    log.info("\n  Sample records:")
    log.info("  %-55s %-25s %-20s  %s", "Title", "Country", "Region", "Thumbnail")
    log.info("  " + "-" * 130)
    for row in cur.fetchall():
        url, title, country, region, cats, thumb = row
        t = (title or "")[:55]
        c = (country or "")[:25]
        r = (region or "")[:20]
        thumb_short = (thumb or "")[:40] if thumb else "-"
        log.info("  %-55s %-25s %-20s  %s", t, c, r, thumb_short)

    conn.close()
    log.info("\nDatabase saved to: %s", args.db)
    copy_db_to_repo(args.db, REPO_DB_PATH)


if __name__ == "__main__":
    main()
