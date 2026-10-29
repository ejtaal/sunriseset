#!/usr/bin/env python3
"""
Find a random webcam at sunrise or sunset, grab its highest-res live frame,
and display it. Updates reliability and resolution stats in the database.

Workflow:
  1. Load all webcams with known coordinates and a video ID
  2. Calculate sun position for each
  3. Filter for sunrise (-2° to -1°, rising) or sunset (0° to 2°, setting)
  4. Pick one at random (up to 5 retries)
  5. Grab 2 seconds of the highest-res stream and extract a frame
  6. Check for black/frozen images
  7. Update reliability (±1) and highest_resolution in DB
  8. Display the frame with feh

Requires: pandas, Pillow, yt-dlp, ffmpeg, feh
"""

import math
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone, timedelta
from io import BytesIO

import pandas as pd
from PIL import Image

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
# Database helpers
# ---------------------------------------------------------------------------
DB_PATH = "/tmp/lww_webcams.db"
LATEST_FRAME_PATH = os.path.join(tempfile.gettempdir(), "wls_latest_frame.jpg")


def _get_reliability(conn, video_id: str) -> int:
    """Get current reliability score for a video, default 5."""
    row = conn.execute(
        "SELECT reliability FROM webcams WHERE video_id = ?", (video_id,)
    ).fetchone()
    return row[0] if row else 5


def _update_result(conn, video_id: str, resolution: str, success: bool) -> None:
    """Update reliability (+1 success, -1 failure) and highest_resolution."""
    current = _get_reliability(conn, video_id)
    new_reliability = max(0, min(10, current + (1 if success else -1)))

    # Update resolution to the highest seen
    existing = conn.execute(
        "SELECT highest_resolution FROM webcams WHERE video_id = ?", (video_id,)
    ).fetchone()
    existing_res = existing[0] if existing else None

    # Compare resolutions: keep the higher one
    def _res_to_int(r):
        if not r or "x" not in r:
            return 0
        try:
            w, h = r.split("x")
            return int(w) * int(h)
        except (ValueError, TypeError):
            return 0

    new_res_pixels = _res_to_int(resolution)
    existing_res_pixels = _res_to_int(existing_res) if existing_res else 0
    best_resolution = resolution if new_res_pixels >= existing_res_pixels else existing_res

    conn.execute("""
        UPDATE webcams
        SET reliability = ?, highest_resolution = ?
        WHERE video_id = ?
    """, (new_reliability, best_resolution, video_id))


def load_webcams() -> pd.DataFrame:
    """Load webcams with known coordinates and a video ID."""
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query("""
        SELECT url, title, country, region, latitude, longitude, source,
               stream_url, video_id, reliability
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
# Video ID extraction
# ---------------------------------------------------------------------------
def extract_video_id(url: str) -> str | None:
    """Extract YouTube video ID from a URL, embed string, or raw ID."""
    if not url:
        return None
    url = str(url)
    for pattern in [
        r"youtube\.com/embed/([A-Za-z0-9_-]{11})",
        r"youtube\.com/watch\?v=([A-Za-z0-9_-]{11})",
        r"youtu\.be/([A-Za-z0-9_-]{11})",
    ]:
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    if re.match(r"^[A-Za-z0-9_-]{11}$", url):
        return url
    return None


# ---------------------------------------------------------------------------
# Screengrab
# ---------------------------------------------------------------------------
def _is_black_or_frozen(image_path: str) -> bool:
    """
    Check if an image is mostly black, frozen, or has very low visual content.

    Returns True if the image appears to be black/frozen/unusable.
    """
    try:
        img = Image.open(image_path).convert("RGB")

        # Use numpy if available for fast analysis
        try:
            import numpy as np
            arr = np.array(img)
            avg_brightness = arr.mean()
            if avg_brightness < 10:
                return True
            variances = [arr[:, :, c].var() for c in range(3)]
            if np.mean(variances) < 25:
                return True
            return False
        except ImportError:
            pass

        # Fallback: sample every Nth pixel to avoid processing full 1080p image
        w, h = img.size
        step = max(1, max(w, h) // 50)  # ~50 samples per dimension
        pixels = []
        for y in range(0, h, step):
            for x in range(0, w, step):
                pixels.append(img.getpixel((x, y)))

        avg_brightness = sum(sum(p) / 3 for p in pixels) / len(pixels)
        if avg_brightness < 10:
            return True

        r_vals = [p[0] for p in pixels]
        g_vals = [p[1] for p in pixels]
        b_vals = [p[2] for p in pixels]

        def _variance(vals):
            mean = sum(vals) / len(vals)
            return sum((v - mean) ** 2 for v in vals) / len(vals)

        avg_var = (_variance(r_vals) + _variance(g_vals) + _variance(b_vals)) / 3
        if avg_var < 25:
            return True

        return False

    except Exception:
        return True


def _get_frame_resolution(frame_path: str) -> str | None:
    """Get resolution of a frame image using Pillow."""
    try:
        img = Image.open(frame_path)
        w, h = img.size
        return f"{w}x{h}"
    except Exception:
        return None


def grab_live_frame(video_id: str):
    """
    Download 2 seconds from the highest-res live stream and extract a frame.

    Returns dict with:
        frame_path: path to the .jpg file (or None on failure)
        resolution: resolution string like "1920x1080" (or None)
        is_black: True if the frame appears black/frozen (or None if failed)
    """
    tmp_mp4 = os.path.join(tempfile.gettempdir(), f"wls_frame_{video_id}.mp4")
    tmp_jpg = os.path.join(tempfile.gettempdir(), f"wls_frame_{video_id}.jpg")
    tmp_part = tmp_mp4 + ".part"

    # Clean up stale files
    for f in (tmp_mp4, tmp_jpg, tmp_part):
        if os.path.isfile(f):
            os.remove(f)

    try:
        # Download 2 seconds — best available video quality
        result = os.system(
            f'yt-dlp -o "{tmp_mp4}" -f "bestvideo+bestaudio/bestvideo/best" '
            f'--download-sections "*00:00-00:02" '
            f'"https://www.youtube.com/embed/{video_id}?autoplay=1&mute=1" '
            f'>/dev/null 2>&1'
        )

        if result != 0 or not os.path.isfile(tmp_mp4) or os.path.getsize(tmp_mp4) < 1000:
            return {"frame_path": None, "resolution": None, "is_black": None}

        # Extract single frame at ~1 second into the clip
        ret = os.system(
            f'ffmpeg -y -i "{tmp_mp4}" -ss 00:00:01 -frames:v 1 -q:v 1 -update 1 '
            f'"{tmp_jpg}" 2>/dev/null'
        )

        if ret != 0 or not os.path.isfile(tmp_jpg) or os.path.getsize(tmp_jpg) < 1000:
            return {"frame_path": None, "resolution": None, "is_black": None}

        resolution = _get_frame_resolution(tmp_jpg)
        is_black = _is_black_or_frozen(tmp_jpg)

        # Copy to persistent path for AI description
        if tmp_jpg is not None and not is_black:
            try:
                shutil.copy2(tmp_jpg, LATEST_FRAME_PATH)
            except Exception:
                pass

        return {"frame_path": tmp_jpg, "resolution": resolution, "is_black": is_black}

    finally:
        if os.path.isfile(tmp_mp4):
            os.remove(tmp_mp4)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print("=" * 70)
    print("📷 Webcam Sunrise/Sunset — Random Pick")
    print("=" * 70)

    # Load and calculate sun positions
    df = load_webcams()
    now = datetime.now(timezone.utc)
    print(f"Time: {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"Webcams with coords + video: {len(df)}")

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
            "video_id": row["video_id"],
            "reliability": int(row.get("reliability") or 5),
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

    # Try up to 5 random webcams until we get a valid frame
    MAX_ATTEMPTS = 5
    conn = sqlite3.connect(DB_PATH)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        picked = matches.sample(1).iloc[0]
        direction = "☀️ SUNRISE" if picked["is_rising"] else "🌅 SUNSET"

        print(f"\n{direction}")
        print(f"  Title:       {picked['title']}")
        print(f"  Location:    {picked['country'] or '—'}, {picked['region'] or '—'}")
        print(f"  Coords:      {picked['lat']:.4f}, {picked['lon']:.4f}")
        print(f"  Sun angle:   {picked['altitude']:.2f}°")
        print(f"  Source:      {picked['source']}")
        print(f"  Reliability: {picked['reliability']}/10")

        video_id = str(picked["video_id"])

        print(f"\nGrabbing live frame …")
        result = grab_live_frame(video_id)

        success = (
            result["frame_path"] is not None
            and os.path.isfile(result["frame_path"])
            and result["is_black"] is not None
            and not result["is_black"]
        )

        _update_result(conn, video_id, result["resolution"], success)
        conn.commit()

        if success:
            print(f"  Frame grabbed ({os.path.getsize(result['frame_path'])} bytes)")
            if result["resolution"]:
                print(f"  Resolution:  {result['resolution']}")
            rel = _get_reliability(conn, video_id)
            print(f"  Reliability: {rel}/10")
            print(f"\n  Displaying with feh …")
            os.system(f"feh --auto-zoom --image-bg '#333333' '{result['frame_path']}'")
            conn.close()
            return
        else:
            status = "video unavailable"
            if result["is_black"] is True:
                status = "black/frozen frame"
            elif result["frame_path"] is None:
                status = "download failed"
            print(f"  {status}.")
            if attempt < MAX_ATTEMPTS:
                print(f"  Trying another … ({attempt}/{MAX_ATTEMPTS})")
                continue

    print("\nNo working streams found after 5 attempts.")
    conn.close()


if __name__ == "__main__":
    main()
