#!/usr/bin/env python3
"""
Find a random webcam at sunrise or sunset, grab its thumbnail, and display it.

Workflow:
  1. Load all webcams with coordinates
  2. Calculate sun position for each
  3. Filter for sunrise (-2° to -1°, rising) or sunset (0° to 2°, setting)
  4. Pick one at random
  5. Fetch the YouTube thumbnail
  6. Display it with feh

Requires: pandas, requests, feh
"""

import math
import os
import random
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone, timedelta

import pandas as pd
import requests

# ---------------------------------------------------------------------------
# NOAA Solar Calculator
# ---------------------------------------------------------------------------

def _julian_date(dt: datetime) -> float:
    """Compute Julian Date from a datetime (UTC)."""
    y, m = dt.year, dt.month
    d = dt.day + dt.hour / 24.0 + dt.minute / 1440.0 + dt.second / 86400.0
    if m <= 2:
        y -= 1
        m += 12
    A = math.floor(y / 100)
    B = 2 - A + math.floor(A / 4)
    return math.floor(365.25 * (y + 4716)) + math.floor(30.6001 * (m + 1)) + d + B - 1524.5


def _sun_position_core(utc_dt: datetime, lat: float, lon: float) -> dict:
    """Core sun position calculation (NOAA algorithm)."""
    jd = _julian_date(utc_dt)
    T = (jd - 2451545.0) / 36525.0

    L0 = (280.46646 + T * (36000.76983 + 0.0003032 * T)) % 360
    M = (357.52911 + T * (35999.05029 - 0.0001537 * T)) % 360
    e = 0.016708634 - T * (0.000042037 + 0.0000001267 * T)

    C = ((1.914602 - T * (0.004817 + 0.000014 * T)) * math.sin(math.radians(M))
         + (0.019993 - 0.000101 * T) * math.sin(math.radians(2 * M))
         + 0.000289 * math.sin(math.radians(3 * M)))
    sun_true_lon = L0 + C

    omega = 125.04 - 1934.136 * T
    sun_app_lon = sun_true_lon - 0.00569 - 0.00478 * math.sin(math.radians(omega))

    obliq_mean = 23.0 + (26.0 + (21.448 - T * (46.815 + T * (0.00059 - T * 0.001813))) / 60.0) / 60.0
    obliq_corr = obliq_mean + 0.00256 * math.cos(math.radians(omega))
    decl = math.degrees(math.asin(math.sin(math.radians(obliq_corr)) * math.sin(math.radians(sun_app_lon))))

    y_val = math.tan(math.radians(obliq_corr / 2.0)) ** 2
    EqT = 4.0 * math.degrees(
        y_val * math.sin(2 * math.radians(L0))
        - 2 * e * math.sin(math.radians(M))
        + 4 * e * y_val * math.sin(math.radians(M)) * math.cos(2 * math.radians(L0))
        - 0.5 * y_val * y_val * math.sin(4 * math.radians(L0))
        - 1.25 * e * e * math.sin(2 * math.radians(M)))

    hour_angle = 15.0 * (utc_dt.hour + utc_dt.minute / 60.0 + utc_dt.second / 3600.0 + lon / 15.0) - 180.0
    hour_angle = (hour_angle + 180.0) % 360.0 - 180.0

    sin_alt = (math.sin(math.radians(lat)) * math.sin(math.radians(decl))
               + math.cos(math.radians(lat)) * math.cos(math.radians(decl))
               * math.cos(math.radians(hour_angle)))
    sin_alt = max(-1.0, min(1.0, sin_alt))
    altitude = math.degrees(math.asin(sin_alt))

    cos_az = ((math.sin(math.radians(lat)) * math.sin(math.radians(decl))
               - math.sin(math.radians(altitude)))
              / (math.cos(math.radians(lat)) * math.cos(math.radians(altitude))))
    cos_az = max(-1.0, min(1.0, cos_az))
    azimuth = math.degrees(math.acos(cos_az))
    if hour_angle > 0:
        azimuth = 360.0 - azimuth

    return {"altitude": altitude, "azimuth": azimuth}


def _sun_position(utc_dt: datetime, lat: float, lon: float) -> dict:
    """Sun position with rising/setting direction."""
    pos = _sun_position_core(utc_dt, lat, lon)
    future_dt = utc_dt + timedelta(seconds=60)
    future = _sun_position_core(future_dt, lat, lon)
    pos["is_rising"] = future["altitude"] > pos["altitude"]
    return pos


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DB_PATH = "/tmp/lww_webcams.db"


def load_webcams() -> pd.DataFrame:
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query("""
        SELECT url, title, country, region, latitude, longitude, source,
               stream_url, video_id
        FROM webcams
        WHERE latitude != '' AND longitude != ''
          AND latitude IS NOT NULL AND longitude IS NOT NULL
          AND video_id != '' AND video_id IS NOT NULL
    """, conn)
    conn.close()
    df["latitude"] = pd.to_numeric(df["latitude"], errors="coerce")
    df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")
    df = df.dropna(subset=["latitude", "longitude"])
    return df


# ---------------------------------------------------------------------------
# YouTube thumbnail
# ---------------------------------------------------------------------------
def extract_video_id(url: str) -> str | None:
    """Extract YouTube video ID from a URL, embed string, or raw ID."""
    if not url:
        return None
    url = str(url)
    # Try to extract from URL patterns
    for pattern in [
        r"youtube\.com/embed/([A-Za-z0-9_-]{11})",
        r"youtube\.com/watch\?v=([A-Za-z0-9_-]{11})",
        r"youtu\.be/([A-Za-z0-9_-]{11})",
    ]:
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    # If it's already a clean 11-char video ID, return it
    if re.match(r"^[A-Za-z0-9_-]{11}$", url):
        return url
    return None


def get_youtube_thumbnail(video_id: str, quality: str = "maxresdefault") -> str:
    """Return the URL for a YouTube thumbnail."""
    return f"https://img.youtube.com/vi/{video_id}/{quality}.jpg"


def download_thumbnail(url: str, dest: str) -> bool:
    """Download an image from URL to dest path. Returns True on success."""
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code == 200 and len(resp.content) > 1000:
            with open(dest, "wb") as f:
                f.write(resp.content)
            return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print("=" * 70)
    print("📷 Webcam Sunrise/Sunset — Random Pick")
    print("=" * 70)

    # Load and calculate
    df = load_webcams()
    now = datetime.now(timezone.utc)
    print(f"Time: {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"Webcams with coords: {len(df)}")

    results = []
    for _, row in df.iterrows():
        pos = _sun_position(now, row["latitude"], row["longitude"])
        results.append({
            "title": row["title"],
            "country": row["country"],
            "region": row["region"],
            "lat": row["latitude"],
            "lon": row["longitude"],
            "source": row["source"],
            "url": row["url"],
            "altitude": pos["altitude"],
            "is_rising": pos["is_rising"],
        })

    sun_df = pd.DataFrame(results)

    # Filter sunrise/sunset
    sunrise_mask = (sun_df["is_rising"]) & (sun_df["altitude"] >= -2) & (sun_df["altitude"] <= -1)
    sunset_mask = (~sun_df["is_rising"]) & (sun_df["altitude"] >= 0) & (sun_df["altitude"] <= 2)
    matches = sun_df[sunrise_mask | sunset_mask]

    if matches.empty:
        print("\nNo webcams at sunrise or sunset right now.")
        print("  Try running again later.")
        return

    print(f"\nFound {len(matches)} webcam(s) at sunrise/sunset")

    # Pick one at random
    picked = matches.sample(1).iloc[0]
    direction = "☀️ SUNRISE" if picked["is_rising"] else "🌅 SUNSET"

    print(f"\n{direction}")
    print(f"  Title:       {picked['title']}")
    print(f"  Location:    {picked['country'] or '—'}, {picked['region'] or '—'}")
    print(f"  Coords:      {picked['lat']:.4f}, {picked['lon']:.4f}")
    print(f"  Sun angle:   {picked['altitude']:.2f}°")
    print(f"  Source:      {picked['source']}")
    print()

    # Try to grab thumbnail
    print("Fetching thumbnail …")
    video_id = extract_video_id(str(picked["url"]))
    if not video_id:
        video_id = extract_video_id(str(picked.get("stream_url", "")))
    if not video_id:
        video_id = str(picked.get("video_id", "")) or None

    if video_id:
        thumb_url = get_youtube_thumbnail(video_id, "maxresdefault")
        tmp_file = os.path.join(tempfile.gettempdir(), f"wls_thumb_{video_id}.jpg")
    else:
        # Fallback: try hqdefault
        if video_id:
            thumb_url = get_youtube_thumbnail(video_id, "hqdefault")
            tmp_file = os.path.join(tempfile.gettempdir(), f"wls_thumb_{video_id}.jpg")
        else:
            print("  No YouTube video ID found — skipping image.")
            print(f"\n  Open in browser: {picked['url']}")
            return

    if download_thumbnail(thumb_url, tmp_file):
        print(f"  Downloaded: {thumb_url}")
        print(f"  Displaying with feh …\n")
        os.system(f"feh --auto-zoom --border-color '#333333' '{tmp_file}'")
    else:
        print(f"  Could not download thumbnail from {thumb_url}")
        print(f"\n  Open in browser: {picked['url']}")


if __name__ == "__main__":
    main()
