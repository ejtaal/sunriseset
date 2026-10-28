#!/usr/bin/env python3
"""
Webcam Live Stream (webcamlivestream.com) scraper — static metadata edition.

Extracts name, location, country, region, YouTube stream URL and coordinates
from webcamlivestream.com. Stores in the same SQLite DB as the LWW scraper
with a 'source' field to distinguish.

Usage:
    python3 wls_scraper.py                # full crawl
    python3 wls_scraper.py --limit 50     # crawl first N only
    python3 wls_scraper.py --resume       # refresh all records (not just new)
    python3 wls_scraper.py --quiet        # less verbose output
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
BASE_URL = "https://webcamlivestream.com"
DB_PATH = os.path.join("/tmp", "lww_webcams.db")
REPO_DATA_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DB_PATH = os.path.join(REPO_DATA_DIR, "data", "lww_webcams.db")
LOG_FILE = os.path.join(REPO_DATA_DIR, "wls_scraper.log")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
REQUEST_DELAY = 5  # seconds between requests

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})

logger = logging.getLogger("wls_scraper")
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

# Known countries to scan (fallback if sitemap is unavailable)
KNOWN_COUNTRIES = [
    "england", "scotland", "wales", "northern-ireland",  # UK
    "usa", "canada", "australia", "new-zealand",
    "ireland", "france", "germany", "netherlands", "belgium",
    "spain", "portugal", "italy", "switzerland", "austria",
    "norway", "sweden", "finland", "denmark", "iceland",
    "japan", "south-korea", "china", "india", "thailand",
    "brazil", "argentina", "mexico", "south-africa",
    "egypt", "turkey", "israel", "uae",
]

# Known non-camera slugs to skip
NON_CAMERA_SLUGS = {"home", "featured", "most-popular", "map", "contact", "add-camera"}

# ---------------------------------------------------------------------------
# URL Discovery — fetch from sitemaps
# ---------------------------------------------------------------------------
def discover_camera_urls() -> list[str]:
    """Fetch all webcam sitemap files and extract camera URLs."""
    log.info("=" * 60)
    log.info("Discovering camera URLs from sitemaps …")
    log.info("=" * 60)

    # Fetch sitemap index to find all sitemap files
    log.info("Fetching sitemap index …")
    try:
        resp = SESSION.get(f"{BASE_URL}/sitemap_index.xml", timeout=15)
        resp.raise_for_status()
        sitemaps = re.findall(r'<loc>([^<]+)</loc>', resp.text)
    except Exception as e:
        log.warning("Failed to fetch sitemap index: %s", e)
        sitemaps = []

    # Filter to webcam sitemaps
    webcam_sitemaps = [s for s in sitemaps if "webcam" in s.lower()]
    if not webcam_sitemaps:
        log.warning("No webcam sitemaps found in index. Trying known patterns …")
        # Fallback: try known sitemap patterns
        for i in range(1, 20):
            url = f"{BASE_URL}/webcam-sitemap{i}.xml"
            try:
                resp = SESSION.get(url, timeout=5)
                if resp.status_code == 200 and len(resp.text) > 100:
                    webcam_sitemaps.append(url)
            except:
                break

    if not webcam_sitemaps:
        log.error("No webcam sitemap files found.")
        return []

    all_urls: list[str] = []
    seen: set[str] = set()

    for sm_url in webcam_sitemaps:
        log.info("Fetching %s …", sm_url)
        try:
            resp = SESSION.get(sm_url, timeout=15)
            resp.raise_for_status()

            # Extract URLs using regex (namespace handling is fragile with ElementTree)
            urls = re.findall(r'<loc>([^<]+)</loc>', resp.text)
            log.info("  → %d URLs", len(urls))

            for url in urls:
                url = url.strip()
                # Skip non-camera URLs
                if not re.search(r'/[a-z][a-z0-9-]+/[a-z0-9-]+/[a-z0-9-]+/', url):
                    continue
                if url not in seen:
                    seen.add(url)
                    all_urls.append(url)
        except Exception as e:
            log.warning("  → failed: %s", e)
            continue

    log.info("Total unique camera URLs: %d", len(all_urls))
    return all_urls


# ---------------------------------------------------------------------------
# HTML Parser — extract metadata from a camera page
# ---------------------------------------------------------------------------
def extract_title(soup: BeautifulSoup) -> str:
    """Extract the camera title."""
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


def extract_breadcrumb(soup: BeautifulSoup) -> list[str]:
    """Extract breadcrumb items for location info."""
    bc = soup.find(class_=re.compile(r"breadcrumb", re.I))
    if not bc:
        return []
    return [a.get_text(strip=True) for a in bc.find_all("a")]


def extract_coordinates(soup: BeautifulSoup) -> tuple[str, str]:
    """Extract latitude and longitude from the webcam map div."""
    lat = ""
    lng = ""

    # First try the standard #webcam-map div
    map_div = soup.find("div", id="webcam-map")
    if map_div:
        lat = map_div.get("data-lat", "")
        lng = map_div.get("data-lng", "") or map_div.get("data-lon", "")
        if lat and lng:
            return lat, lng

    # Fallback: search all elements for data-lat/data-lng pairs
    for tag in soup.find_all(True):
        if tag.get("data-lat") and not lat:
            lat = tag["data-lat"]
        if (tag.get("data-lng") or tag.get("data-lon")) and not lng:
            lng = tag.get("data-lng") or tag.get("data-lon", "")
        if lat and lng:
            break

    return lat, lng


def extract_stream_url(soup: BeautifulSoup) -> dict:
    """Extract YouTube stream URL and video ID from iframe or page content."""
    stream_url = ""
    video_id = ""

    # Look for YouTube iframe
    for iframe in soup.find_all("iframe"):
        src = iframe.get("src", "") or iframe.get("data-src", "")
        if "youtube.com/embed/" in src or "youtu.be/" in src:
            stream_url = src
            id_m = re.search(r"youtube\.com/embed/([A-Za-z0-9_-]{11})", src)
            if id_m:
                video_id = id_m.group(1)
            break

    # Fallback: search for YouTube embed URL in text
    if not stream_url:
        texts = soup.get_text()
        ytm = re.search(r"https?://(?:www\.)?youtube\.com/embed/([A-Za-z0-9_-]{11})", texts)
        if ytm:
            video_id = ytm.group(1)
            stream_url = f"https://www.youtube.com/embed/{video_id}"

    watch_url = ""
    if video_id:
        watch_url = f"https://www.youtube.com/watch?v={video_id}"

    return {
        "stream_url": stream_url,
        "video_id": video_id,
        "youtube_watch_url": watch_url,
    }


def classify_location(breadcrumb: list[str], title: str) -> tuple[str, str]:
    """Classify breadcrumb/title into (country, region)."""
    country = ""
    region = ""

    # Known countries
    known_countries = {
        "england", "scotland", "wales", "northern ireland", "united kingdom", "uk",
        "usa", "united states", "united states of america",
        "canada", "australia", "new zealand", "ireland",
        "france", "germany", "netherlands", "belgium", "luxembourg",
        "spain", "portugal", "italy", "switzerland", "austria",
        "norway", "sweden", "finland", "denmark", "iceland",
        "japan", "south korea", "china", "india", "thailand",
        "brazil", "argentina", "mexico", "south africa",
        "egypt", "turkey", "israel", "uae",
    }

    # Known US states
    known_states = {
        "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
        "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
        "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
        "maine", "maryland", "massachusetts", "michigan", "minnesota",
        "mississippi", "missouri", "montana", "nebraska", "nevada",
        "new hampshire", "new jersey", "new mexico", "new york",
        "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
        "pennsylvania", "rhode island", "south carolina", "south dakota",
        "tennessee", "texas", "utah", "vermont", "virginia", "washington",
        "west virginia", "wisconsin", "wyoming",
    }

    # Known Canadian provinces
    known_provinces = {
        "alberta", "british columbia", "manitoba", "new brunswick",
        "newfoundland and labrador", "northwest territories", "nova scotia",
        "ontario", "prince edward island", "quebec", "saskatchewan",
    }

    known_regions = known_countries | known_states | known_provinces

    # Check breadcrumb (last items are usually most specific)
    for item in reversed(breadcrumb):
        if item in ("Home", "Webcam Live Stream"):
            continue
        cl = item.lower().strip()
        if cl in known_regions and not country:
            country = item
        elif cl not in known_regions and not region:
            region = item

    # Fallback: check title for country mentions
    if not country:
        title_lower = title.lower()
        for c in known_countries:
            if c in title_lower:
                country = c.capitalize() if c not in ("usa", "uk", "uae") else c.upper()
                break

    return country, region


def parse_camera_page(url: str, soup: BeautifulSoup) -> dict | None:
    """Extract all metadata. Returns dict or None if not a camera page."""
    title = extract_title(soup)

    # Must reference a camera
    if "cam" not in title.lower() and "webcam" not in title.lower():
        return None

    description = extract_description(soup)
    breadcrumb = extract_breadcrumb(soup)
    lat, lng = extract_coordinates(soup)
    stream_data = extract_stream_url(soup)
    country, region = classify_location(breadcrumb, title)

    return {
        "title": title,
        "description": description,
        "location": " / ".join(breadcrumb) if breadcrumb else "",
        "country": country,
        "region": region,
        "categories": json.dumps([]),  # no theme categories on this site
        "about_text": description,
        "stream_url": stream_data["stream_url"],
        "video_id": stream_data["video_id"],
        "youtube_watch_url": stream_data["youtube_watch_url"],
        "latitude": lat,
        "longitude": lng,
    }


def fetch_and_parse(url: str) -> dict | None:
    """Fetch and parse a camera page."""
    try:
        resp = SESSION.get(url, timeout=20)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "lxml")
        return parse_camera_page(url, soup)
    except Exception as e:
        log.debug("Error fetching %s: %s", url, e)
        return None


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def init_db(db_path: str) -> sqlite3.Connection:
    """Create/open the SQLite database and ensure source column exists."""
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

    # Add source column if it doesn't exist (migration)
    try:
        conn.execute("ALTER TABLE webcams ADD COLUMN source TEXT")
    except Exception:
        pass

    # Add coordinate columns if missing (migration)
    for col in ("stream_url", "video_id", "youtube_watch_url", "latitude", "longitude"):
        try:
            conn.execute(f"ALTER TABLE webcams ADD COLUMN {col} TEXT")
        except Exception:
            pass

    conn.commit()
    return conn


def upsert_webcam(conn: sqlite3.Connection, entry: dict, source: str = "webcamlivestream.com"):
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
    """Deterministic DB ID from URL."""
    path = urlparse(url).path.rstrip("/")
    slug = path.lower().replace("/", "_").replace(" ", "_")
    return slug.strip("_")


def copy_db_to_repo(src_db: str, dest_db: str) -> None:
    """Copy the working DB to the repo data/ folder for versioning."""
    os.makedirs(os.path.dirname(dest_db), exist_ok=True)
    if os.path.exists(src_db):
        shutil.copy2(src_db, dest_db)
        log.info("Database copied to: %s", dest_db)
    else:
        log.warning("Source DB not found, skipping copy: %s", src_db)


class ProgressTracker:
    """Tracks scrape progress and writes periodic summaries."""

    def __init__(self, total: int):
        self.total = total
        self.done = 0
        self.failed = 0
        self.skipped = 0
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
        """Call once per URL processed."""
        elapsed = time.time() - self.start_time
        rate = self.done / elapsed if elapsed > 0 else 0
        eta = (self.total - self.done) / rate if rate > 0 else 0

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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Scrape Webcam Live Stream metadata")
    parser.add_argument("--limit", type=int, default=0,
                        help="Only process first N camera URLs (0 = all)")
    parser.add_argument("--resume", action="store_true",
                        help="Refresh all records (not just new ones)")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress progress output")
    parser.add_argument("--db", type=str, default=DB_PATH,
                        help=f"Database path (default: {DB_PATH})")
    args = parser.parse_args()

    if args.quiet:
        logger.setLevel(logging.WARNING)
        for h in logger.handlers:
            if isinstance(h, logging.StreamHandler):
                h.setLevel(logging.WARNING)

    log.info("=" * 60)
    log.info("Webcam Live Stream Scraper")
    log.info("=" * 60)

    # Step 1: Collect URLs
    camera_urls = discover_camera_urls()
    if not camera_urls:
        log.error("No camera URLs found.")
        sys.exit(1)

    if args.limit > 0:
        camera_urls = camera_urls[:args.limit]
        log.info("Limiting to first %d entries", args.limit)

    # Step 2: Init DB
    log.info("Database: %s", args.db)
    conn = init_db(args.db)

    cur = conn.execute("SELECT COUNT(*) FROM webcams")
    existing = cur.fetchone()[0]
    log.info("Existing records: %d", existing)

    # Step 3: Scrape
    total = len(camera_urls)
    tracker = ProgressTracker(total)

    for i, url in enumerate(camera_urls, 1):
        # Skip URLs already in the DB (unless --resume)
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
            "thumbnail_url": "",
            "lastmod": "",
            "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

        upsert_webcam(conn, record, source="webcamlivestream.com")
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

    # Stats by source
    cur = conn.execute("""
        SELECT source, COUNT(*) as cnt
        FROM webcams WHERE source IS NOT NULL AND source != ''
        GROUP BY source ORDER BY cnt DESC
    """)
    source_stats = cur.fetchall()

    # Stats by country
    cur = conn.execute("""
        SELECT country, COUNT(*) as cnt
        FROM webcams WHERE country != '' AND source = 'webcamlivestream.com'
        GROUP BY country ORDER BY cnt DESC LIMIT 20
    """)
    country_stats = cur.fetchall()

    # Stats by region
    cur = conn.execute("""
        SELECT region, COUNT(*) as cnt
        FROM webcams WHERE region != '' AND source = 'webcamlivestream.com'
        GROUP BY region ORDER BY cnt DESC LIMIT 20
    """)
    region_stats = cur.fetchall()

    # Stats by coordinate coverage
    with_coords = conn.execute(
        "SELECT COUNT(*) FROM webcams WHERE latitude != '' AND source = 'webcamlivestream.com'"
    ).fetchone()[0]
    with_stream = conn.execute(
        "SELECT COUNT(*) FROM webcams WHERE stream_url != '' AND source = 'webcamlivestream.com'"
    ).fetchone()[0]

    log.info("\n" + "=" * 60)
    log.info("COMPLETE")
    log.info("=" * 60)
    log.info("  Records scraped:  %d", tracker.done)
    log.info("  Skipped:          %d", tracker.skipped)
    log.info("  Failed:           %d", tracker.failed)
    log.info("  Total in DB:      %d", total_db)
    log.info("  With coords:      %d", with_coords)
    log.info("  With stream URL:  %d", with_stream)

    if source_stats:
        log.info("\n  Sources:")
        for source, cnt in source_stats:
            log.info("    %-30s %d", source, cnt)

    if country_stats:
        log.info("\n  Top countries:")
        for country, cnt in country_stats:
            log.info("    %-25s %d", country, cnt)

    if region_stats:
        log.info("\n  Top regions:")
        for region, cnt in region_stats:
            log.info("    %-30s %d", region, cnt)

    # Sample records
    cur = conn.execute("""
        SELECT url, title, country, region, latitude, longitude
        FROM webcams WHERE source = 'webcamlivestream.com'
        ORDER BY rowid LIMIT 10
    """)
    log.info("\n  Sample records (webcamlivestream.com):")
    log.info("  %-55s %-20s %-20s  %-12s  %-12s", "Title", "Country", "Region", "Lat", "Lng")
    log.info("  " + "-" * 130)
    for row in cur.fetchall():
        url, title, country, region, lat, lng = row
        t = (title or "")[:55]
        c = (country or "")[:20]
        r = (region or "")[:20]
        log.info("  %-55s %-20s %-20s  %-12s  %-12s", t, c, r, lat or "-", lng or "-")

    conn.close()
    log.info("\nDatabase saved to: %s", args.db)
    copy_db_to_repo(args.db, REPO_DB_PATH)


if __name__ == "__main__":
    main()
