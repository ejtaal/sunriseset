# Live World Webcams Scraper

Scrapes **liveworldwebcams.com** and **webcamlivestream.com**, stores webcam metadata in a local SQLite database, and finds webcams at sunrise/sunset.

## Features

- Scrapes 2,997+ live webcams across 120+ countries
- Incremental updates: skips already-fetched pages
- Sunrise/sunset finder: calculates sun position for each webcam
- Live frame grabbing: captures actual stream frames via yt-dlp
- Reliability tracking: scores stream reliability over time

## Database Schema

```sql
CREATE TABLE webcams (
    url                  TEXT PRIMARY KEY,
    id                   TEXT,            -- readable slug ID
    title                TEXT,            -- webcam name
    description          TEXT,            -- meta description
    location             TEXT,            -- specific location/address
    country              TEXT,            -- country name
    region               TEXT,            -- state/province/region
    categories           TEXT,            -- JSON array of category tags
    about_text           TEXT,            -- "About this webcam" description
    thumbnail_url        TEXT,            -- thumbnail image URL
    stream_url           TEXT,            -- YouTube embed URL
    video_id             TEXT,            -- YouTube video ID extracted from stream_url
    youtube_watch_url    TEXT,            -- direct YouTube watch URL
    latitude             TEXT,            -- geographic latitude
    longitude            TEXT,            -- geographic longitude
    lastmod              TEXT,            -- sitemap last-modified date
    fetched_at           TEXT,            -- when this record was last fetched
    source               TEXT,            -- 'liveworldwebcams.com' or 'webcamlivestream.com'
    reliability          INTEGER DEFAULT 5, -- stream reliability 0-10 (+1 success, -1 failure/black)
    view_quality         INTEGER,         -- view quality (manual assessment, not auto-populated)
    highest_resolution   TEXT             -- highest captured resolution, e.g. '1920x1080'
);
```

## Database

The database is stored at `/tmp/lww_webcams.db` (due to filesystem constraints on some systems).

## Usage

```bash
# Scrape liveworldwebcams.com
python3 lww_scraper.py

# Scrape webcamlivestream.com
python3 wls_scraper.py

# Find a random webcam at sunrise/sunset and grab its live frame
python3 sunrise_sunset_cameras.py

# Less verbose scraping
python3 lww_scraper.py --quiet

# Custom database path
python3 lww_scraper.py --db /tmp/lww_webcams.db
```

## Sample Queries

```python
import sqlite3, json

conn = sqlite3.connect("/tmp/lww_webcams.db")

# All webcams in Florida
rows = conn.execute("""
    SELECT title, location FROM webcams
    WHERE country = 'United States' AND region = 'Florida'
    ORDER BY title
""").fetchall()

# Beach webcams
rows = conn.execute("""
    SELECT title, country, region
    FROM webcams
    WHERE categories LIKE '%Beaches%'
    LIMIT 20
""").fetchall()

# Top 10 countries
rows = conn.execute("""
    SELECT country, COUNT(*) as cnt
    FROM webcams
    GROUP BY country
    ORDER BY cnt DESC
    LIMIT 10
""").fetchall()

# Most reliable webcams with a captured resolution
rows = conn.execute("""
    SELECT title, country, reliability, highest_resolution, source
    FROM webcams
    WHERE reliability >= 8 AND highest_resolution IS NOT NULL
    ORDER BY reliability DESC
""").fetchall()

# Webcams with manual view quality ratings
rows = conn.execute("""
    SELECT title, country, region, view_quality, source
    FROM webcams
    WHERE view_quality IS NOT NULL
    ORDER BY view_quality DESC
""").fetchall()

conn.close()
```

## Data Coverage

| Metric | Value |
|--------|-------|
| Total webcams | 2,997 |
| liveworldwebcams.com | 1,929 |
| webcamlivestream.com | 1,068 |
| Countries | 120+ |
| With coordinates | 2,619 (87%) |
| With stream URL | 2,104 (70%) |

## Reliability & Quality Scoring

### Reliability (column: `reliability`, 0–10)

Automatically updated by `sunrise_sunset_cameras.py` on each screengrab:

| Event | Change |
|-------|--------|
| Successful frame grab | +1 |
| Download failed | -1 |
| Video unavailable | -1 |
| Black/frozen frame | -1 |

Clamped to range 0–10. Starts at **5** for new videos.

### View Quality (column: `view_quality`, NULL or 1–10)

Not auto-populated. Assessed manually over time based on:

- **Sky visibility** — how much of the sky is visible
- **Landscape/city inclusion** — does the frame show the surroundings
- **FOV width** — wider field of view scores higher
- **Clarity** — image sharpness and detail

Values are assigned manually by reviewing the captured frames.

## Dependencies

- Python 3.10+
- `pandas`, `requests`, `BeautifulSoup4` (scrapers)
- `Pillow` (frame quality analysis)
- `yt-dlp` (live stream frame capture)
- `ffmpeg` (frame extraction)
- `feh` (image display)
