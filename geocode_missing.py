#!/usr/bin/env python3
"""
Geocode webcam entries missing coordinates.

Two strategies:
1. Nominatim (OpenStreetMap) for entries with country/region data
2. OpenAI-compatible LLM to extract location from title, then Nominatim to geocode

Usage:
    python3 geocode_missing.py          # Dry run — show what would be filled
    python3 geocode_missing.py --apply  # Actually update the database
"""

import sqlite3
import time
import requests
import re
import argparse
import random
from datetime import datetime, timezone

DB_PATH = "/tmp/lww_webcams.db"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
LLM_API_URL = "http://10.0.2.2:8080/upstream/unsloth-Qwen3.6-35B-A3B-standard/v1/chat/completions"
LLM_MODEL = "unsloth/Qwen3.6-35B-A3B-GGUF:Q4_K_XL"
NOMINATIM_DELAY = 1.1  # Nominatim requires ≥1s between requests
LLM_DELAY = 0.5


def load_missing(conn):
    """Return entries missing latitude (and thus longitude)."""
    return conn.execute("""
        SELECT rowid, title, country, region, source
        FROM webcams
        WHERE latitude = "" OR latitude IS NULL
    """).fetchall()


def geocode_nominatim(query: str) -> tuple[float, float] | None:
    """Geocode via Nominatim. Returns (lat, lon) or None."""
    params = {
        "q": query,
        "format": "json",
        "limit": 1,
        "countrycodes": "",
        "addressdetails": 1,
    }
    try:
        resp = requests.get(NOMINATIM_URL, params=params, timeout=15, headers={
            "User-Agent": "LWW-Geocoder/1.0 (erik@taal.nl)"
        })
        resp.raise_for_status()
        results = resp.json()
        if not results:
            return None
        r = results[0]
        return float(r["lat"]), float(r["lon"])
    except Exception:
        return None


def parse_title_with_llm(title: str) -> str | None:
    """Use LLM to extract a geocodeable location string from the title."""
    prompt = f"""You are a geocoder. Given a webcam title, extract the most specific
location (city, region, or landmark) that can be used to find coordinates.

Rules:
- Output ONLY the location string, nothing else
- Prefer city/region over country when available
- If the title mentions a specific place (airport, street, landmark), use that
- If only a country is mentioned, output just the country
- If no location can be inferred, output "unknown"

Title: {title}

Location string:"""

    try:
        resp = requests.post(LLM_API_URL, json={
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 80,
            "temperature": 0,
        }, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"].strip()
        # Remove surrounding quotes if any
        text = text.strip('"\'')
        if text.lower() == "unknown" or not text:
            return None
        return text
    except Exception:
        return None


def geocode_entry(rowid: int, title: str, country: str | None, region: str | None, conn):
    """Try to geocode one entry. Returns the query string used or None."""
    queries = []

    # Strategy 1: region + country (most specific)
    if region and country:
        queries.append(f"{region}, {country}")

    # Strategy 2: country only
    if country:
        queries.append(country)

    # Strategy 3: parse title with LLM
    title_hint = None
    if not country or not region:
        title_hint = parse_title_with_llm(title)
        if title_hint and title_hint != "unknown":
            queries.append(title_hint)

    # Strategy 4: fallback — try title directly (may work for well-known places)
    # Extract the first part of title before " – " or " – "
    short = re.split(r"\s*[–—-]\s*", title)[0].strip()
    if short and (not queries or short != title):
        queries.append(short)

    for q in queries:
        result = geocode_nominatim(q)
        if result:
            lat, lon = result
            conn.execute(
                "UPDATE webcams SET latitude = ?, longitude = ? WHERE rowid = ?",
                (str(lat), str(lon), rowid),
            )
            return q
        time.sleep(NOMINATIM_DELAY)

    return None


def main(apply: bool = False):
    conn = sqlite3.connect(DB_PATH)
    entries = load_missing(conn)
    print(f"Found {len(entries)} entries without coordinates\n")

    if not apply:
        # Categorize
        with_country = sum(1 for _, _, c, _, _ in entries if c)
        with_region = sum(1 for _, _, _, r, _ in entries if r)
        title_only = 378 - with_country

        print("=== DRY RUN ===")
        print(f"Total missing coords: 378")
        print(f"  With country:        {with_country}")
        print(f"  With region:         {with_region}")
        print(f"  Title-only:          {title_only}")
        print(f"\nTo apply changes, run: python3 geocode_missing.py --apply\n")

        # Show top 20 samples with expected geocode queries
        print("Sample geocode targets (first 20):")
        for rowid, title, country, region, source in entries[:20]:
            queries = []
            if region and country:
                queries.append(f"{region}, {country}")
            if country:
                queries.append(country)
            queries.append(re.split(r"\s*[–—-]\s*", title)[0].strip())
            print(f"  [{rowid}] {title[:55]:55s} → {' | '.join(queries)}")

        conn.close()
        return

    # Apply mode
    print("=== APPLYING ===")
    success = 0
    failed = 0
    skipped = 0

    for i, (rowid, title, country, region, source) in enumerate(entries, 1):
        used = geocode_entry(rowid, title, country, region, conn)
        if used:
            success += 1
            conn.commit()  # Commit after each success to preserve progress
            print(f"[{i}/378] ✓ {title[:55]:55s} → {used}")
        else:
            failed += 1
            print(f"[{i}/378] ✗ {title[:55]:55s} → failed (tried: {used or 'none'})")
        time.sleep(0.3)

        if i % 10 == 0:
            print(f"  Progress: {i}/378, success={success}, failed={failed}")

    conn.commit()

    # Summary
    remaining = conn.execute("""
        SELECT COUNT(*) FROM webcams
        WHERE latitude = "" OR latitude IS NULL
    """).fetchone()[0]

    print(f"\n=== SUMMARY ===")
    print(f"Cooled:          {success}")
    print(f"Failed:          {failed}")
    print(f"Remaining:       {remaining}")
    print(f"Success rate:    {success/(success+failed)*100:.1f}%")

    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Geocode webcam entries missing coordinates")
    parser.add_argument("--apply", action="store_true", help="Actually update the database")
    args = parser.parse_args()
    main(apply=args.apply)
