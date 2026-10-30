#!/usr/bin/env python3
"""Export webcams with coordinates and quality to JSON."""

import argparse
import json
import sqlite3
import sys


def main():
    parser = argparse.ArgumentParser(description="Export webcams to JSON")
    parser.add_argument(
        "--db",
        default="/tmp/lww_webcams.db",
        help="Path to SQLite database (default: /tmp/lww_webcams.db)",
    )
    parser.add_argument(
        "--output",
        default="data/webcams_export.json",
        help="Output JSON file (default: data/webcams_export.json)",
    )
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    rows = conn.execute(
        """
        SELECT url, title, location, latitude, longitude,
               view_quality, ai_description
        FROM webcams
        WHERE latitude IS NOT NULL AND latitude != ''
          AND longitude IS NOT NULL AND longitude != ''
          AND view_quality IS NOT NULL
        ORDER BY title
        """
    ).fetchall()
    conn.close()

    results = []
    for url, title, location, lat, lng, quality, desc in rows:
        entry = {
            "url": url,
            "title": title or "",
            "location": location or "",
            "lat": float(lat),
            "long": float(lng),
            "quality": quality,
            "ai_description": desc or "",
        }
        results.append(entry)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"Exported {len(results)} webcams → {args.output}")


if __name__ == "__main__":
    main()
