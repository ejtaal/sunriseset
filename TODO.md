# Webcam Site Analysis — Scraping Feasibility

## Summary

Analyzed 9 webcam compilation sites (max 5 requests each) for potential data source.
Ranked by **data richness**, **extractability**, and **complementarity** with existing LWW DB.

## Rankings

### 1. webcamlivestream.com — BEST MATCH ⭐⭐⭐⭐⭐
- **Size:** ~2,026 webcams
- **Structure:** WordPress site with country → state → city hierarchy
- **Per-page data:**
  - `data-lat` / `data-lng` coordinates on `#webcam-map` div
  - YouTube embed URLs
  - JSON-LD VideoObject with description
  - Breadcrumb: `Home > Country > State > City`
- **Extractability:** HIGH — standard WordPress structure, similar to LWW
- **Schema match:** Coordinates ✓, Location ✓, Stream URL ✓, Title ✓
- **Notes:** No sitemap (returns 0 URLs), but country pages have 100+ camera links each

### 2. openwebcamdb.com — GOOD BACKEND DATA ⭐⭐⭐⭐
- **Size:** 1,867 public webcams, 54 countries
- **Structure:** Modern SPA with Tailwind CSS
- **Per-page data:**
  - API at `/api/v1/webcams` (returns 401 — requires auth)
  - Country-tagged listings
  - Search by name or place
- **Extractability:** MEDIUM — API exists but requires auth key
- **Schema match:** Country ✓, Location ✓, Stream URL (maybe), Coordinates (maybe)
- **Notes:** Has a `/map` page showing all webcams. If API key can be obtained, this would be high-value.

### 3. volvevision.com — STRONG LOCATION DATA ⭐⭐⭐⭐
- **Size:** 6,000+ cameras, 52 countries
- **Structure:** Modern SPA with JSON-LD structured data
- **Per-page data:**
  - Structured location: "City, State, Country"
  - JSON-LD ItemList with 6 items
  - Map element with data attributes
  - Camera cards with owner info
- **Extractability:** MEDIUM — SPA, requires JS rendering for full data
- **Schema match:** Location ✓, Country ✓, Region ✓, Stream URL (maybe), Coordinates (maybe)
- **Notes:** Cameras from multiple owners (e.g. windy.com). Some stream URLs found (windy.com embeds).

### 4. geocam.ru — MODERATE DATA ⭐⭐⭐
- **Size:** ~1,000+ cameras (estimated from country pages)
- **Structure:** WordPress with country → city hierarchy
- **Per-page data:**
  - 30+ country pages
  - JSON-LD on camera pages
  - Map elements
- **Extractability:** MEDIUM — WordPress, but URL structure is different
- **Schema match:** Location ✓, Country ✓, Stream URL (maybe), Coordinates (maybe)
- **Notes:** `/en/online/` returns 404. Country pages like `/en/us/` exist. URL structure: `/en/[country]/[city-slug]/`

### 5. livewebcamstream.com — WEAK ⭐⭐
- **Size:** ~912 cameras, 57 countries
- **Structure:** Mostly app promotion
- **Issues:** Listing pages return 404. Sitemap exists but returns 0 URLs. No clear camera pages found.
- **Extractability:** LOW
- **Notes:** Appears to be more of an app landing page than a functional webcam site.

### 6. iplivecams.com — WEAK ⭐⭐
- **Size:** 5,000+ (claimed)
- **Structure:** Minimal camera elements found (4 cards on homepage)
- **Extractability:** LOW
- **Notes:** Limited HTML structure for scraping.

### 7. worldcam.tv — MINIMAL ⭐
- **Size:** 90+ countries (claimed)
- **Structure:** Nearly empty page — only 3 links (cookies, privacy, terms)
- **Extractability:** VERY LOW
- **Notes:** Site appears to be a redirect or placeholder.

### 8. liveearthmap.com — MINIMAL ⭐
- **Size:** Uses NASA/USGS/OpenSky data
- **Structure:** Minimal HTML
- **Extractability:** VERY LOW
- **Notes:** More of a map overlay than a webcam listing.

### 9. globenow.tv — MINIMAL ⭐
- **Size:** News + webcams
- **Structure:** Has API endpoints but no clear camera listing
- **Extractability:** LOW
- **Notes:** Focuses on live TV and news, not webcams.

## Recommendation

**Priority 1: webcamlivestream.com** — This is the closest match to LWW in structure and data richness.
- Coordinates available via `data-lat`/`data-lng`
- Location hierarchy in breadcrumbs
- YouTube embed URLs
- Similar URL pattern: `/country/state/city-cam-name/`

**Priority 2: openwebcamdb.com** — If the API key can be obtained (check their docs or contact them).

**Priority 3: volvevision.com** — Good location data, but SPA makes scraping harder.

## Next Steps

1. Build a scraper for webcamlivestream.com (similar structure to LWW)
2. Add to database with a `source` field to distinguish sources
3. Deduplicate by URL/location to avoid overlap with existing LWW data
