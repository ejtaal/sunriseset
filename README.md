# Live World Webcams Scraper

Scrapes **liveworldwebcams.com** and stores webcam metadata in a local SQLite database.

## Features

- Extracts **1,929+** live webcams from 111+ countries
- Stores static metadata: name, location, country, region, theme/category, URL, thumbnail, description
- Incremental updates: skips already-fetched pages
- No external dependencies beyond Python stdlib

## Database Schema

```sql
CREATE TABLE webcams (
    url             TEXT PRIMARY KEY,
    id              TEXT,            -- readable slug ID
    title           TEXT,            -- webcam name
    description     TEXT,            -- meta description
    location        TEXT,            -- specific location/address
    country         TEXT,            -- country name
    region          TEXT,            -- state/province/region
    categories      TEXT,            -- JSON array of category tags
    about_text      TEXT,            -- "About this webcam" description
    thumbnail_url   TEXT,            -- thumbnail image URL
    lastmod         TEXT,            -- sitemap last-modified date
    fetched_at      TEXT             -- when this record was last fetched
);
```

## Usage

```bash
# Full crawl (from scratch)
python3 lww_scraper.py

# Resume: skip already-fetched pages
python3 lww_scraper.py

# Crawl first N only
python3 lww_scraper.py --limit 50

# Less verbose
python3 lww_scraper.py --quiet

# Custom database path
python3 lww_scraper.py --db /tmp/lww_webcams.db
```

## Database

The database is stored at `/tmp/lww_webcams.db` by default (due to filesystem constraints on some systems).

## Sample Queries

```python
import sqlite3, json

conn = sqlite3.connect("lww_webcams.db")

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

conn.close()
```

## Data Coverage

| Metric | Value |
|--------|-------|
| Total webcams | 1,929 |
| Countries | 111 |
| US states/regions | 50+ |
| Canadian provinces | 13 |
| UK regions | 4 |
| Themes | 60+ |

Top countries: United States (577), Canada (114), United Kingdom (107), Argentina (57), Germany (51)

Top themes: Cities and Towns (383), Beaches (237), Lakes and Rivers (80), Airports (50), Mountains (45)
