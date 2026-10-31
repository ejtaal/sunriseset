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

import base64
import logging
import math
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from io import BytesIO

LOG_FILE = "/tmp/classify.log"
logger = logging.getLogger("classify")

import pandas as pd
from PIL import Image

# ---------------------------------------------------------------------------
# AI Vision API (OpenAI-compatible)
# ---------------------------------------------------------------------------
VISION_API_URL = "http://10.0.2.2:8080/upstream/unsloth-Qwen3.6-35B-A3B-standard/v1/chat/completions"
VISION_MODEL = "unsloth/Qwen3.6-35B-A3B-GGUF:Q4_K_XL"
LLM_API_URL = VISION_API_URL
LLM_MODEL = VISION_MODEL


def _has_description(conn, video_id: str) -> bool:
    """Check if a video has a valid AI description (reviewed within 60 days)."""
    row = conn.execute(
        "SELECT ai_description, last_reviewed FROM webcams WHERE video_id = ?",
        (video_id,)
    ).fetchone()
    if not row or not row[0] or row[0] == '':
        return False  # No description
    if not row[1]:
        return False  # No review date → treat as stale
    try:
        review_date = datetime.fromisoformat(row[1])
    except (ValueError, TypeError):
        return False
    # Compare naive datetimes (both are UTC, stored without tzinfo)
    return (datetime.now() - review_date) < timedelta(days=60)

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

    return {"altitude": altitude, "azimuth": azimuth, "hour_angle": hour_angle}


def _sun_position(utc_dt: datetime, lat: float, lon: float) -> dict:
    """Sun position with rising/setting direction (via hour angle)."""
    pos = _sun_position_core(utc_dt, lat, lon)
    # hour_angle < 0 → before solar noon → sun rising (east)
    # hour_angle > 0 → after solar noon → sun setting (west)
    pos["is_rising"] = pos["hour_angle"] < 0
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


def _record_review(conn, video_id: str) -> None:
    """Record that this video was reviewed now."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE webcams SET last_reviewed = ? WHERE video_id = ?",
        (now, video_id)
    )


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


def grab_live_frame(video_id: str, title: str = ""):
    """
    Download 2 seconds from the highest-res live stream and extract a frame.

    Returns dict with:
        frame_path: path to the .jpg file (or None on failure)
        resolution: resolution string like "1920x1080" (or None)
        is_black: True if the frame appears black/frozen (or None if failed)
        error: error message from yt-dlp (or None)
    """
    tmp_mp4 = os.path.join(tempfile.gettempdir(), f"wls_frame_{video_id}.mp4")
    tmp_jpg = os.path.join(tempfile.gettempdir(), f"wls_frame_{video_id}.jpg")
    tmp_part = tmp_mp4 + ".part"
    tmp_ytdlp_log = os.path.join(tempfile.gettempdir(), f"wls_ytdlp_{video_id}.log")
    yt_url = f"https://www.youtube.com/embed/{video_id}?autoplay=1&mute=1"

    # Clean up stale files
    for f in (tmp_mp4, tmp_jpg, tmp_part, tmp_ytdlp_log):
        if os.path.isfile(f):
            os.remove(f)

    try:
        # Download 2 seconds — best available video quality
        result = os.system(
            f'yt-dlp -o "{tmp_mp4}" -f "bestvideo+bestaudio/bestvideo/best" '
            f'--download-sections "*00:00-00:02" '
            f'--cookies-from-browser chromium '
            f'"https://www.youtube.com/embed/{video_id}?autoplay=1&mute=1" '
            f'>"{tmp_ytdlp_log}" 2>&1'
        )

        # Read yt-dlp output for error message and YouTube title
        error = None
        yt_title = None
        # Known yt-dlp status messages that are NOT titles
        _ytdlp_status = {"downloading", "live", "waiting", "extractor", "fixup",
                         "merging", "deleting", "reconnecting", "requesting",
                         "file doesn't exist", "no information"}
        try:
            with open(tmp_ytdlp_log) as f:
                log = f.read().strip()
                if log:
                    # Extract YouTube title from yt-dlp log
                    # Format: [youtube] <video_id>: Title: <title>
                    #          [youtube] <video_id>: <title>
                    title_match = re.search(
                        r'\[youtube\]\s+' + re.escape(video_id) + r':\s*Title:\s*(.+?)(?:\s*$|\s*\[)',
                        log, re.IGNORECASE
                    )
                    if not title_match:
                        # Try alternate format: [youtube] <id>: <title>
                        title_match = re.search(
                            r'\[youtube\]\s+' + re.escape(video_id) + r':\s*(.+?)(?:\s*\n\[|\s*$)',
                            log, re.IGNORECASE
                        )
                    if title_match:
                        candidate = title_match.group(1).strip()
                        # Skip yt-dlp status messages
                        if not any(candidate.lower().startswith(s) for s in _ytdlp_status):
                            yt_title = candidate

                    # Extract the last error line
                    lines = log.split('\n')
                    for line in reversed(lines):
                        if 'ERROR' in line or 'error' in line or 'not available' in line.lower():
                            error = line.strip()
                            break
                    if error is None and result != 0:
                        error = log.split('\n')[-1].strip() or f"exit code {result}"
                    elif error is None and log:
                        error = log.split('\n')[-1].strip()[:200]
        except Exception:
            pass

        if error:
            logger.info(f"  yt-dlp: {error}")

        if result != 0 or not os.path.isfile(tmp_mp4) or os.path.getsize(tmp_mp4) < 1000:
            return {"frame_path": None, "resolution": None, "is_black": None, "error": error, "yt_title": yt_title}

        # Extract single frame at ~1 second into the clip
        ret = os.system(
            f'ffmpeg -y -i "{tmp_mp4}" -ss 00:00:01 -frames:v 1 -q:v 1 -update 1 '
            f'"{tmp_jpg}" 2>/dev/null'
        )

        if ret != 0 or not os.path.isfile(tmp_jpg) or os.path.getsize(tmp_jpg) < 1000:
            return {"frame_path": None, "resolution": None, "is_black": None, "yt_title": yt_title}

        resolution = _get_frame_resolution(tmp_jpg)
        is_black = _is_black_or_frozen(tmp_jpg)

        # Copy to persistent path for AI description
        if tmp_jpg is not None and not is_black:
            try:
                shutil.copy2(tmp_jpg, LATEST_FRAME_PATH)
            except Exception:
                pass

        return {"frame_path": tmp_jpg, "resolution": resolution, "is_black": is_black, "error": None, "yt_title": yt_title}

    finally:
        if os.path.isfile(tmp_mp4):
            os.remove(tmp_mp4)
        if os.path.isfile(tmp_ytdlp_log):
            os.remove(tmp_ytdlp_log)


def assess_image_quality(image_path: str) -> tuple[int, str]:
    """Assess image quality from file. Returns (score 0-10, reason).
    
    Low scores for: monochromatic, B&W, blurry, washed out, low contrast.
    """
    try:
        img = Image.open(image_path).convert("RGB")
        pixels = list(img.getdata())
        width, height = img.size
        
        # 1. Color saturation — check std dev of saturation
        hsv_img = img.convert("HSV")
        hsv_pixels = list(hsv_img.getdata())
        saturations = [p[1] for p in hsv_pixels]
        avg_sat = sum(saturations) / len(saturations)
        sat_std = (sum((s - avg_sat) ** 2 for s in saturations) / len(saturations)) ** 0.5
        
        # 2. Sharpness — use Laplacian variance
        gray_img = img.convert("L")
        sharpness = gray_img.filter(ImageFilter.SHARPEN)
        gray_pixels = list(gray_img.getdata())
        sharp_pixels = list(sharpness.getdata())
        sharpness_diff = [abs(g - s) for g, s in zip(gray_pixels, sharp_pixels)]
        avg_sharpness = sum(sharpness_diff) / len(sharpness_diff)
        
        # 3. Contrast — std dev of luminance
        luminance = [0.299 * p[0] + 0.587 * p[1] + 0.114 * p[2] for p in pixels]
        avg_lum = sum(luminance) / len(luminance)
        lum_std = (sum((l - avg_lum) ** 2 for l in luminance) / len(luminance)) ** 0.5
        
        # Scoring
        score = 10
        reasons = []
        
        # Low saturation → monochromatic/B&W
        if avg_sat < 30:
            score -= 5
            reasons.append("monochromatic/near-B&W")
        elif avg_sat < 60:
            score -= 3
            reasons.append("low color saturation")
        
        # Low sharpness → blurry
        if avg_sharpness < 10:
            score -= 4
            reasons.append("blurry")
        elif avg_sharpness < 20:
            score -= 2
            reasons.append("slightly blurry")
        
        # Low contrast → washed out
        if lum_std < 20:
            score -= 4
            reasons.append("washed out/low contrast")
        elif lum_std < 40:
            score -= 2
            reasons.append("low contrast")
        
        # Cap score
        score = max(1, min(10, score))
        reason = ", ".join(reasons) if reasons else "good image quality"
        
        return score, reason
    except Exception:
        return 5, "unable to assess"


def describe_frame(frame_path: str, retry: int = 0) -> str | None:
    """
    Send a frame to the vision API and return an AI-generated description.
    Image resized to max 640x480 before sending. Returns None on failure.
    max_tokens doubles with each retry (4096 → 8192 → 16384).
    """
    try:
        with Image.open(frame_path) as img:
            img.thumbnail((640, 480))
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=75)
            img_b64 = base64.b64encode(buf.getvalue()).decode()

        # Double max_tokens with each retry: 8192 → 16384 → 32768
        max_tokens = 8192 * (2 ** retry)

        payload = {
            "model": VISION_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Describe this webcam scene in 3-5 sentences. Focus on:\n- Overall setting (coast, city, mountain, etc.)\n- Main visible elements (buildings, water, landscape)\n- Sky conditions and lighting — estimate sky coverage as a percentage (e.g. 'sky visible in ~70% of frame')\n- Camera perspective (wide/narrow, high/low angle) — note if FOV is wide panoramic or tight\n\nNo weather details, no itemized foreground lists. Keep it natural and concise."
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}
                        }
                    ]
                }
            ],
            "max_tokens": max_tokens
        }

        req = urllib.request.Request(
            VISION_API_URL,
            data=__import__("json").dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST"
        )

        with urllib.request.urlopen(req, timeout=300) as resp:
            data = __import__("json").loads(resp.read())
            return data["choices"][0]["message"]["content"].strip()

    except Exception as exc:
        logger.error(f"describe_frame failed: {type(exc).__name__}: {exc}")
        return None


def assess_view_quality(description: str) -> tuple[int | None, str]:
    """Score a webcam view quality 1-10 from the AI description.

    Returns (score, raw_response) where raw_response contains the full
    LLM content + reasoning for debugging quality issues.
    """
    import re as _re

    try:
        payload = {
            "model": LLM_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": f"""Score this webcam's suitability for viewing sunrise/sunset on a scale of 1-10.

SCORING RULES (strict):
- Sky coverage is the PRIMARY factor
- 1-2: No sky, interior, close-up, or sky < 10%
- 3-4: Sky 10-20% with narrow/tight framing
- 5-6: Sky 20-40% or moderate FOV
- 7-8: Sky 40-70% OR wide FOV with some sky
- 9-10: Sky 70%+ AND wide panoramic FOV

Output ONLY the number 1-10. No text, no explanation.

Description:
{description}"""
                }
            ],
            "max_tokens": 1024,
            "temperature": 0,
        }
        req = urllib.request.Request(
            LLM_API_URL,
            data=__import__("json").dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = __import__("json").loads(resp.read())
            text = data["choices"][0]["message"]["content"].strip()
            # Reasoning models put answer in reasoning_content
            reasoning = data["choices"][0]["message"].get("reasoning_content", "")
            raw_response = f"content: {text}\nreasoning: {reasoning}"
            val = None
            for src in [text, reasoning]:
                # Strict: just a single digit 1-9 or 10
                m = _re.match(r'^\s*(\d+)\s*$', src)
                if m:
                    val = int(m.group(1))
                    break
                # Also try "Score: X" or "Result: X" as first match
                if not val:
                    m = _re.search(r'(?:score|result)[\s:]+(\d+)', src, _re.IGNORECASE)
                    if m:
                        val = int(m.group(1))
                        break
            if val and 1 <= val <= 10:
                return val, raw_response
            # Fallback: last digit 1-10 on its own line or at start of reasoning
            for src in [text, reasoning]:
                for line in src.split("\n"):
                    line = line.strip()
                    m = _re.match(r'^\s*(\d{1,2})\s*$', line)
                    if m:
                        val = int(m.group(1))
                        if 1 <= val <= 10:
                            return val, raw_response
            # Last resort: last digit 1-10 anywhere (but skip percentages)
            for src in [text, reasoning]:
                # Skip lines that look like percentages (e.g. "10%", "75%")
                clean_lines = [_re.sub(r'\d+\s*%', '', l) for l in src.split("\n")]
                clean = "\n".join(clean_lines)
                nums = _re.findall(r'\b([1-9]|10)\b', clean)
                if nums:
                    val = int(nums[-1])
                    if 1 <= val <= 10:
                        return val, raw_response
            return None, raw_response
    except Exception:
        return None, ""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    import argparse

    parser = argparse.ArgumentParser(description="Webcam sunrise/sunset finder")
    parser.add_argument("--classify", type=int, default=0, help="Run N iterations to classify streams (no feh)")
    parser.add_argument("--overwrite", action="store_true", help="Re-classify even if descriptions already exist")
    parser.add_argument("--feh-timeout", type=int, default=0, help="Set feh display timeout in seconds (default: none)")
    args = parser.parse_args()

    # Set up logging to stdout and file
    logger = logging.getLogger("classify")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        fmt = logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S")
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)
        fh = logging.FileHandler(LOG_FILE)
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    logger.info("=" * 70)
    logger.info("📷 Webcam Sunrise/Sunset — Random Pick" if args.classify == 0 else "📷 Webcam Stream Classifier")
    logger.info("=" * 70)

    # Load webcam data once
    df = load_webcams()
    logger.info(f"Webcams with coords + video: {len(df)}")

    # Determine how many classification rounds to run
    num_rounds = args.classify if args.classify > 0 else 1

    # Load DB to check existing classifications
    conn = sqlite3.connect(DB_PATH)
    classified_ids = set()
    unclassified_ids = set()
    expired_count = 0

    success_count = 0
    fail_count = 0
    all_urls_tried = set()
    classified_this_session = []  # track (rowid, title, video_id) for summary

    for rnd in range(1, num_rounds + 1):
        # Recalculate sun position each round (can take many minutes for AI)
        now = datetime.now(timezone.utc)
        logger.info(f"\n{'═' * 70}")
        logger.info(f"Round {rnd}/{num_rounds} — {now.strftime('%H:%M:%S UTC')}")
        logger.info(f"{'═' * 70}")

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
        sunrise_mask = (sun_df["is_rising"]) & (sun_df["altitude"] >= -2) & (sun_df["altitude"] <= -1)
        sunset_mask = (~sun_df["is_rising"]) & (sun_df["altitude"] >= 0) & (sun_df["altitude"] <= 2)
        matches = sun_df[sunrise_mask | sunset_mask]

        if matches.empty:
            logger.info("\nNo webcams at sunrise or sunset right now.")
            logger.info("  Try running again later.")
            break

        logger.info(f"\nFound {len(matches)} webcam(s) at sunrise/sunset")

        # Update classified/unclassified sets based on current DB state
        for _, row in matches.iterrows():
            vid = str(row["video_id"])
            if _has_description(conn, vid):
                classified_ids.add(vid)
            else:
                unclassified_ids.discard(vid)  # remove old unclassified, will be re-added

        unclassified_ids_now = set(r.video_id for r in matches.itertuples()) - classified_ids
        expired_count_now = 0
        for vid in unclassified_ids_now:
            row = conn.execute(
                "SELECT ai_description, last_reviewed FROM webcams WHERE video_id = ?",
                (vid,)
            ).fetchone()
            if row and row[0] and row[0] != '' and row[1]:
                expired_count_now += 1

        prefix = "[OVERWRITE] " if args.overwrite else ""
        logger.info(f"{prefix}Already classified: {len(classified_ids)} | Need classification: {len(unclassified_ids_now)}")

        # Bias selection: 90% from unclassified, 10% from all
        if unclassified_ids_now and len(unclassified_ids_now) > 0 and random.random() < 0.9:
            candidates = matches[matches["video_id"].astype(str).isin(unclassified_ids_now)]
            if candidates.empty:
                candidates = matches
        else:
            candidates = matches

        # Try up to 5 random webcams until we get a valid frame
        MAX_ATTEMPTS = 5
        found = False
        round_success = False
        round_result = None
        round_video_id = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            picked = candidates.sample(1).iloc[0]
            video_id = str(picked["video_id"])
            round_video_id = video_id
            direction = "☀️ SUNRISE" if picked["is_rising"] else "🌅 SUNSET"
            already_classified = video_id in classified_ids

            logger.info(f"\n{direction} | {'[CLASSIFYING]' if not already_classified else '[ALREADY CLASSIFIED]'}")
            logger.info(f"  Title:       {picked['title']}")
            logger.info(f"  Location:    {picked['country'] or '—'}, {picked['region'] or '—'}")
            logger.info(f"  Coords:      {picked['lat']:.4f}, {picked['lon']:.4f}")
            yt_url = f"https://www.youtube.com/watch?v={video_id}"
            all_urls_tried.add(yt_url)
            logger.info(f"  ── {yt_url} ──")
            logger.info(f"  Sun angle:   {picked['altitude']:.2f}°")
            logger.info(f"  Has desc:    {'yes' if already_classified else 'no'}")

            logger.info(f"  Grabbing live frame …")
            round_result = grab_live_frame(video_id, picked['title'])

            round_success = (
                round_result["frame_path"] is not None
                and os.path.isfile(round_result["frame_path"])
                and round_result["is_black"] is not None
                and not round_result["is_black"]
            )

            if not round_success:
                status = "video unavailable"
                if round_result.get("is_black") is True:
                    status = "black/frozen frame"
                elif round_result.get("error"):
                    status = round_result["error"][:150]
                elif round_result["frame_path"] is None:
                    status = "download failed"
                logger.info(f"  {status}.")
                if attempt < MAX_ATTEMPTS:
                    logger.info(f"  Trying another … ({attempt}/{MAX_ATTEMPTS})")
                    logger.info(f"  Waiting 30s before retry …")
                    time.sleep(30)
                    continue
                logger.info(f"  Giving up on this one.")
                fail_count += 1
                found = True
                break

            logger.info(f"  Frame grabbed ({os.path.getsize(round_result['frame_path'])} bytes)")
            if round_result["resolution"]:
                logger.info(f"  Resolution:  {round_result['resolution']}")

            # Update DB title from YouTube if we got a new title
            yt_title = round_result.get("yt_title")
            if yt_title and yt_title.strip():
                db_title = conn.execute(
                    "SELECT title FROM webcams WHERE video_id = ?", (video_id,)
                ).fetchone()[0]
                if db_title != yt_title:
                    conn.execute(
                        "UPDATE webcams SET title = ? WHERE video_id = ?",
                        (yt_title, video_id),
                    )
                    conn.commit()
                    logger.info(f"  Title updated: {yt_title[:100]}")

            if already_classified:
                # Already classified — update reliability/resolution and skip AI
                _update_result(conn, video_id, round_result["resolution"], True)
                _record_review(conn, video_id)
                conn.commit()
                logger.info(f"  Reliability: {_get_reliability(conn, video_id)}/10")
                logger.info(f"  AI description: already exists (skipped)")
                classified_ids.add(video_id)
                success_count += 1
                found = True
                break

            # AI description for unclassified — generate first, then show frame
            # Assess image quality before LLM
            img_quality, img_reason = assess_image_quality(round_result["frame_path"])
            if img_quality < 4:
                logger.info(f"  Image quality: {img_quality}/10 ({img_reason})")
                logger.info("  Skipping LLM — poor image quality")
                # Generate description noting image issues
                desc = f"Low-quality image ({img_reason}). Wide-angle view of the scene with limited visibility due to image degradation."
                vq = img_quality
                _elapsed = 0
                conn.execute(
                    "UPDATE webcams SET ai_description = ?, view_quality = ? WHERE video_id = ?",
                    (desc, vq, video_id),
                )
                _record_review(conn, video_id)
                _update_result(conn, video_id, round_result["resolution"], True)
                conn.commit()
                logger.info(f"  Reliability: {_get_reliability(conn, video_id)}/10")
                logger.info(f"  View quality: {vq}/10 (auto-assessed: poor image)")
                logger.info(f"  AI time:        0s (LLM skipped)")
                logger.info(f"  Description:  {desc}")
                rowid = conn.execute("SELECT rowid FROM webcams WHERE video_id = ?", (video_id,)).fetchone()[0]
                classified_this_session.append((rowid, video_id, picked['title']))
                classified_ids.add(video_id)
                success_count += 1
                found = True
                break

            logger.info("  Generating AI description …")
            import time as _time
            _t0 = _time.time()

            # Retry AI description up to 3 times (max_tokens: 8192 → 16384 → 32768)
            desc = None
            for _attempt in range(3):
                if _attempt > 0:
                    logger.info(f"  Retrying AI description (attempt {_attempt + 1}/3, max_tokens={8192 * (2 ** _attempt)}) …")
                    time.sleep(3)
                desc = describe_frame(round_result["frame_path"], retry=_attempt)
                if desc:
                    break

            _elapsed = _time.time() - _t0
            if desc:
                # Retry view quality up to 3 times
                vq = None
                vq_reasoning = ""
                for _attempt in range(3):
                    if _attempt > 0:
                        logger.info(f"  Retrying view quality (attempt {_attempt + 1}/3) …")
                        time.sleep(3)
                    vq, vq_reasoning = assess_view_quality(desc)
                    if vq is not None:
                        break
                conn.execute(
                    "UPDATE webcams SET ai_description = ?, view_quality = ?, view_quality_reasoning = ? WHERE video_id = ?",
                    (desc, vq, vq_reasoning, video_id),
                )
                _record_review(conn, video_id)
                # Update reliability/resolution AFTER AI description
                _update_result(conn, video_id, round_result["resolution"], True)
                conn.commit()
                logger.info(f"  Reliability: {_get_reliability(conn, video_id)}/10")
                classified_ids.add(video_id)
                logger.info(f"  Description stored (len={len(desc)})")
                if vq:
                    logger.info(f"  View quality:   {vq}/10")
                else:
                    logger.info("  View quality:   N/A (LLM assessment failed)")
                if vq_reasoning:
                    logger.info(f"  Quality raw:    {vq_reasoning[:4000]}")
                logger.info(f"  Description:    {desc}")
                logger.info(f"  AI time:        {_elapsed:.0f}s")
                # Track for summary
                rowid = conn.execute("SELECT rowid FROM webcams WHERE video_id = ?", (video_id,)).fetchone()[0]
                classified_this_session.append((rowid, video_id, picked['title']))
                success_count += 1
            else:
                logger.info(f"  Could not generate AI description ({_elapsed:.0f}s).")
                # Still update reliability/resolution after failed AI attempt
                _update_result(conn, video_id, round_result["resolution"], False)
                conn.commit()
                logger.info(f"  Reliability: {_get_reliability(conn, video_id)}/10")
                fail_count += 1

            # Display frame with feh after AI description is generated
            logger.info("  Displaying with feh …")
            import os as _os
            env = dict(_os.environ)
            feh_cmd = ["feh", "--auto-zoom", "--image-bg", "#333333", round_result["frame_path"]]
            if args.feh_timeout > 0:
                feh_cmd.insert(0, "timeout")
                feh_cmd.insert(1, str(args.feh_timeout))
            subprocess.Popen(
                feh_cmd,
                env=env,
                close_fds=True,
            )
            # Lower feh window behind others (non-fatal)
            try:
                subprocess.run(["sh", "-c",
                    "sleep 0.5 && xdotool search --name feh windowlower 2>/dev/null"],
                    capture_output=True, timeout=5, env=env)
            except Exception:
                pass
            found = True
            break

        if not found:
            logger.info("  No working stream found.")
            fail_count += 1

        # Pause between rounds
        if rnd < num_rounds:
            logger.info("  Waiting 5s before next round …")
            time.sleep(5)

    # Summary
    logger.info(f"\n{'═' * 70}")
    logger.info(f"Classification complete: {success_count} success, {fail_count} failed")
    if all_urls_tried:
        logger.info(f"\n{'─' * 70}")
        logger.info(f"URLs tried ({len(all_urls_tried)} unique):")
        for url in sorted(all_urls_tried):
            logger.info(f"  {url}")
        logger.info(f"{'─' * 70}")

    # Classify streams summary
    if classified_this_session:
        logger.info(f"\n{'═' * 70}")
        logger.info(f"Cameras classified this session ({len(classified_this_session)}):")
        for rowid, video_id, title in classified_this_session:
            info = conn.execute("""
                SELECT reliability, view_quality, highest_resolution, last_reviewed
                FROM webcams WHERE rowid = ?
            """, (rowid,)).fetchone()
            if info:
                rel, vq, res, reviewed = info
                logger.info(f"\n  Title:          {title}")
                logger.info(f"  URL:            https://www.youtube.com/watch?v={video_id}")
                logger.info(f"  Reliability:    {rel}/10")
                logger.info(f"  View quality:   {vq if vq else 'N/A'}")
                logger.info(f"  Resolution:     {res if res else 'N/A'}")
                logger.info(f"  Reviewed:       {reviewed if reviewed else 'N/A'}")
                # Get description
                desc = conn.execute("SELECT ai_description FROM webcams WHERE rowid = ?", (rowid,)).fetchone()[0]
                logger.info(f"  Description:    {desc}")
    conn.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN ai_description IS NOT NULL AND ai_description != '' THEN 1 ELSE 0 END) as with_desc,
            SUM(CASE WHEN view_quality IS NOT NULL THEN 1 ELSE 0 END) as with_vq,
            SUM(CASE WHEN highest_resolution IS NOT NULL THEN 1 ELSE 0 END) as with_res,
            ROUND(AVG(reliability), 1) as avg_rel
        FROM webcams
    """).fetchone()

    conn.close()


if __name__ == "__main__":
    main()
