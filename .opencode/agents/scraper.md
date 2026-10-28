---
description: Runs and maintains the liveworldwebcams.com scraper — sitemap crawling, page parsing, DB updates, and rate limiting
mode: primary
---

You are working on a webcam metadata scraper for liveworldwebcams.com.

## Project structure

- `lww_scraper.py` — Main scraper script (Python, stdlib + requests + bs4)
- `README.md` — Documentation
- `TODO.md` — Reference sites and TODO items
- Database at `/tmp/lww_webcams.db` during scraping (project filesystem has I/O errors)
- Database copied to `data/lww_webcams.db` in the repo after each scrape completes
- Virtual env at `.venv/`
- TODO.md contains analysis of 9 alternative webcam sites for expanding the DB

## Scraper behavior

- Discovers URLs from `sitemap_index.xml` (~9 sitemap files, ~2,145 total URLs)
- Filters to ~2,123 actual webcam pages (excludes articles, tags, category listings)
- Parses each page for: title, description, location, country, region, categories, about text, thumbnail, YouTube stream URL, video ID
- Stores in SQLite with upsert on URL primary key
- Rate limits at 5s per request (configurable via `REQUEST_DELAY` constant)
- Skips already-fetched pages by default; use `--resume` to refresh older entries

## Database schema

```sql
CREATE TABLE webcams (
    url TEXT PRIMARY KEY,
    id TEXT,
    title TEXT,
    description TEXT,
    location TEXT,
    country TEXT,
    region TEXT,
    categories TEXT,          -- JSON array
    about_text TEXT,
    thumbnail_url TEXT,
    stream_url TEXT,          -- YouTube embed URL
    video_id TEXT,
    youtube_watch_url TEXT,
    lastmod TEXT,
    fetched_at TEXT NOT NULL
);
```

## Key constraints

- Working DB is always `/tmp/lww_webcams.db` — the scraper copies it to `data/lww_webcams.db` after finishing
- Use Chrome user agent (not a bot UA)
- When testing, use `--limit N` for small batches
- User agent is `Chrome/131.0 on Windows`

## Common tasks

- Run a crawl: `python3 lww_scraper.py [--limit N] [--quiet]`
- Monitor progress: `tail -f scraper.log` (full log with DEBUG per-URL entries)
- Live console: scraper outputs INFO to stdout (no need for `--quiet` unless you want silence)
- Progress summary: appears every 60s and at completion — shows success/fail/skip counts, pages/min, ETA
- Check DB stats: `python3 -c "import sqlite3; c=sqlite3.connect('/tmp/lww_webcams.db'); print(c.execute('SELECT COUNT(*) FROM webcams').fetchone()[0])"`
- Query countries: `SELECT country, COUNT(*) FROM webcams WHERE country != '' GROUP BY country ORDER BY 2 DESC`
- Add new fields: ALTER TABLE + update parse functions + upsert query
- The scraper auto-skips already-fetched URLs — no need to clear DB between runs
