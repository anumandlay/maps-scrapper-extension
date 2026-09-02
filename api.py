import os
import pychrome
import subprocess
import time
import random
import urllib.parse
import pyautogui
import json
import requests
from flask import Flask, request, jsonify
from bs4 import BeautifulSoup
# from urllib.parse import urlparse, parse_qs  # SERP disabled
from urllib.parse import urlparse
import threading

app = Flask(__name__)

# ─────────────────────────────────────────────────────────────
# GLOBALS
# ─────────────────────────────────────────────────────────────
KEYWORDS = []

CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
DEFAULT_MAX_PAGES = 10

CURRENT_KEYWORD  = None
CURRENT_SNO      = None
CURRENT_COUNTRY  = None

MAPS_POSTED_KEYS  = set()    # dedupe maps-data POSTs within one keyword run


# ─────────────────────────────────────────────────────────────
# REST API CONFIG
# GET  /maps-jobs?page=&limit=        list jobs (newest sno first)
# POST /maps-jobs                     {keyword, country}
# PATCH /maps-jobs/:sno               {status}
# ─────────────────────────────────────────────────────────────
MAPS_JOBS_URL  = "https://salescrm.vughy.com/api/v1/maps-jobs"
MAPS_DATA_URL  = "https://salescrm.vughy.com/api/v1/maps-data"

HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}
PAGE_LIMIT = 100
REQUEST_TIMEOUT = 20


# ─────────────────────────────────────────────────────────────
# SAFE JSON HELPERS
# ─────────────────────────────────────────────────────────────

def safe_json(resp):
    """
    Parse response JSON safely.
    Returns parsed value, or None if the body is empty / not valid JSON.
    Logs the raw text on failure so you can see what the server sent.
    """
    text = resp.text.strip()
    if not text:
        return None
    try:
        return resp.json()
    except Exception:
        print(f"[WARN] Non-JSON response (status {resp.status_code}): {text[:200]}")
        return None


def rows_from_payload(data) -> list:
    """
    Unwrap the production envelope:
      { success, data: { data: [...jobs], meta: {page, limit, total, totalPages} } }
    Also accepts a bare list or a dict with a list under common keys.
    """
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []

    inner = data.get("data", data)
    if isinstance(inner, list):
        return inner
    if isinstance(inner, dict):
        for key in ("data", "rows", "keywords", "results", "items"):
            if isinstance(inner.get(key), list):
                return inner[key]
    for key in ("data", "rows", "keywords", "results", "items"):
        if isinstance(data.get(key), list):
            return data[key]
    return []


def rows_from_response(resp) -> list:
    return rows_from_payload(safe_json(resp))


def meta_from_payload(data) -> dict:
    if not isinstance(data, dict):
        return {}
    inner = data.get("data")
    if isinstance(inner, dict) and isinstance(inner.get("meta"), dict):
        return inner["meta"]
    if isinstance(data.get("meta"), dict):
        return data["meta"]
    return {}


def extract_sno(obj):
    if not isinstance(obj, dict):
        return None
    for key in ("sno", "id", "lastID"):
        if obj.get(key) is not None:
            try:
                return int(obj[key])
            except (TypeError, ValueError):
                pass
    inner = obj.get("data")
    if isinstance(inner, dict):
        return extract_sno(inner)
    return None


# def parse_result_field(raw) -> list:
#     """SERP disabled — maps-only mode."""
#     if isinstance(raw, list):
#         return raw
#     if isinstance(raw, str) and raw.strip():
#         try:
#             parsed = json.loads(raw)
#             return parsed if isinstance(parsed, list) else []
#         except Exception:
#             return []
#     return []


# ─────────────────────────────────────────────────────────────
# DB HELPERS
# ─────────────────────────────────────────────────────────────

def init_db():
    """No-op — using remote API."""
    print(f"DB ready — maps job queue at {MAPS_JOBS_URL}")


def fetch_jobs_page(page: int = 1, limit: int = PAGE_LIMIT):
    resp = requests.get(
        MAPS_JOBS_URL,
        params={"page": page, "limit": min(limit, PAGE_LIMIT)},
        headers=HEADERS,
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    payload = safe_json(resp)
    return rows_from_payload(payload), meta_from_payload(payload)


def fetch_all_jobs() -> list:
    all_rows = []
    page = 1
    while True:
        rows, meta = fetch_jobs_page(page)
        all_rows.extend(rows)
        total_pages = int(meta.get("totalPages") or 1)
        if page >= total_pages or not rows:
            break
        page += 1
    return all_rows


def find_job(sno=None, keyword=None):
    """Walk paginated GET results (newest first) until a match is found."""
    page = 1
    while True:
        rows, meta = fetch_jobs_page(page)
        for row in rows:
            if sno is not None:
                try:
                    if int(row.get("sno", -1)) == int(sno):
                        return row
                except (TypeError, ValueError):
                    pass
            elif keyword is not None and row.get("keyword") == keyword:
                return row
        total_pages = int(meta.get("totalPages") or 1)
        if page >= total_pages or not rows:
            return None
        page += 1


def patch_job(sno: int, payload: dict, raise_on_error: bool = False):
    resp = requests.patch(
        f"{MAPS_JOBS_URL}/{sno}",
        json=payload,
        headers=HEADERS,
        timeout=REQUEST_TIMEOUT,
    )
    if not resp.ok:
        print(f"[WARN] PATCH sno={sno} -> HTTP {resp.status_code}: {resp.text[:200]}")
        if raise_on_error:
            resp.raise_for_status()
    return resp


def extract_country(row: dict) -> str:
    """Read country from a maps-jobs row."""
    if not isinstance(row, dict):
        return ""
    for key in ("country", "Country", "country_name", "countryName"):
        val = row.get(key)
        if val and str(val).strip():
            return str(val).strip()
    return ""


def build_search_query(keyword: str, country: str = "") -> str:
    """Combine keyword + country for Google search when country is not already in keyword."""
    keyword = (keyword or "").strip()
    country = (country or "").strip()
    if not keyword:
        return country
    if not country:
        return keyword
    if country.lower() in keyword.lower():
        return keyword
    return f"{keyword} {country}"


def insert_keyword(keyword: str, country: str = "") -> int:
    """
    POST a new maps job. API assigns sno and sets status='pending'.
    Body: {keyword, country}
    """
    keyword = (keyword or "").strip()
    country = (country or "").strip()
    if not keyword:
        raise ValueError("keyword is required")

    payload = {"keyword": keyword}
    if country:
        payload["country"] = country
    try:
        resp = requests.post(
            MAPS_JOBS_URL, json=payload, headers=HEADERS, timeout=REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        sno = extract_sno(safe_json(resp) or {})
        print(
            f"DB: inserted maps job keyword='{keyword}' country='{country or '-'}' "
            f"-> sno={sno}"
        )
        return int(sno)
    except requests.HTTPError:
        if resp.status_code == 409:
            existing_sno = get_sno_for_keyword(keyword)
            print(f"DB: keyword='{keyword}' already exists -> sno={existing_sno}")
            return existing_sno
        raise
    except Exception as e:
        print(f"[DB ERROR] insert_keyword failed for '{keyword}': {e}")
        raise


def get_sno_for_keyword(keyword: str):
    """Return the newest sno for a keyword, or None if not found."""
    try:
        job = find_job(keyword=keyword)
        return int(job["sno"]) if job else None
    except Exception as e:
        print(f"[DB ERROR] get_sno_for_keyword failed: {e}")
        return None


# def append_page_data(sno: int, page, page_dict: dict):
#     """
#     SERP disabled — maps-only mode.
#     Previously appended parsed SERP pages to serp-results result array.
#     """
#     try:
#         job = find_job(sno=sno)
#         existing_result = parse_result_field(job.get("result") if job else None)
#         try:
#             page_num = int(page)
#         except (TypeError, ValueError):
#             page_num = page
#         existing_result.append({"page": page_num, **page_dict})
#         patch_job(sno, {"result": existing_result}, raise_on_error=True)
#         print(f"Saved page {page_num} into API (sno={sno})")
#     except Exception as e:
#         print(f"append_page_data ERROR: {e}")


def get_pending_keyword():
    """
    Fetch maps-jobs, pick oldest status='pending',
    PATCH to 'processing', return sno, keyword, country.
    Returns dict or None.
    """
    try:
        rows = fetch_all_jobs()
        if not rows:
            print("[QUEUE] No jobs returned from maps-jobs API")
            return None

        pending = [r for r in rows if str(r.get("status", "")).lower() == "pending"]
        pending.sort(key=lambda r: int(r.get("sno", 0)))

        if not pending:
            counts = {}
            for r in rows:
                st = str(r.get("status", "unknown")).lower()
                counts[st] = counts.get(st, 0) + 1
            summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
            print(
                f"[QUEUE] No pending maps jobs ({len(rows)} total: {summary}). "
                "Only status='pending' is picked. PATCH a job back to pending to re-run."
            )
            return None

        row = pending[0]
        sno = int(row["sno"])
        country = extract_country(row)

        try:
            patch_job(sno, {"status": "processing"})
        except Exception as pe:
            print(f"[WARN] mark-processing PATCH failed for sno={sno}: {pe}")

        print(
            f"DB: picked pending maps job sno={sno} keyword='{row['keyword']}' "
            f"country='{country or '-'}'"
        )
        return {
            "sno":     sno,
            "keyword": row["keyword"],
            "country": country,
        }

    except Exception as e:
        print(f"[DB ERROR] get_pending_keyword: {e}")
        return None


def update_status(sno: int, status: str):
    """PATCH just the status field for a given sno."""
    try:
        patch_job(sno, {"status": status})
    except Exception as e:
        print(f"[DB ERROR] update_status sno={sno}: {e}")


# ─────────────────────────────────────────────────────────────
# MAPS DATA API
# ─────────────────────────────────────────────────────────────

MAPS_PLACE_EXTRACT_JS = """
(() => {
    const text = (el) => (el && (el.textContent || '').trim()) || '';
    const pickText = (...selectors) => {
        for (const sel of selectors) {
            const el = document.querySelector(sel);
            const t = text(el);
            if (t) return t;
        }
        return '';
    };
    const pickHref = (...selectors) => {
        for (const sel of selectors) {
            const el = document.querySelector(sel);
            if (el && el.href) return el.href;
        }
        return '';
    };

    let name = pickText('h1.DUwDvf', 'h1.fontHeadlineLarge');
    if (!name) {
        const h1 = document.querySelector('h1');
        name = text(h1);
    }

    const addrEl = document.querySelector('[data-item-id="address"]');
    const address = text(addrEl).replace(/\\s+/g, ' ');

    let phone = '';
    const phoneEl = document.querySelector('[data-item-id^="phone:"]');
    if (phoneEl) {
        phone = text(phoneEl);
        if (!phone) {
            const id = phoneEl.getAttribute('data-item-id') || '';
            phone = id.replace(/^phone:tel:/, '').replace(/^phone:/, '');
        }
    }
    if (!phone) {
        const btn = [...document.querySelectorAll('button[aria-label]')].find((b) => {
            const label = b.getAttribute('aria-label') || '';
            return label.startsWith('Phone:') || label.startsWith('Call');
        });
        if (btn) {
            phone = (btn.getAttribute('aria-label') || '').split(':').slice(1).join(':').trim();
        }
    }

    const website = pickHref('a[data-item-id="authority"]', 'a[aria-label*="Website"]');

    let service_type = '';
    const catBtn = document.querySelector('button.DkEaL');
    if (catBtn) service_type = text(catBtn);

    let maps_link = window.location.href || '';
    if (!maps_link.includes('/maps/place/')) {
        const placeA = document.querySelector('a[href*="/maps/place/"]');
        maps_link = placeA ? placeA.href : '';
    }
    if (maps_link) maps_link = maps_link.split('?')[0];

    let email = '';
    const mail = document.querySelector('a[href^="mailto:"]');
    if (mail) email = mail.href.replace('mailto:', '').split('?')[0];

    return {
        agency_name: name || null,
        address: address || null,
        phone: phone || null,
        email: email || null,
        website: website || null,
        service_type: service_type || null,
        google_maps_link: maps_link || null
    };
})();
"""


def post_listing_from_record(record: dict, fallback_name: str = "", fallback_link: str = "") -> bool:
    """Dedupe and POST whatever fields we have to maps-data."""
    global MAPS_POSTED_KEYS

    if fallback_name and not record.get("agency_name"):
        record["agency_name"] = fallback_name
    if fallback_link and not record.get("google_maps_link"):
        record["google_maps_link"] = normalize_maps_link(fallback_link)

    if CURRENT_COUNTRY and not record.get("country"):
        record["country"] = CURRENT_COUNTRY

    payload = {k: v for k, v in record.items() if v}
    if not payload:
        print("[MAPS] No fields to post for listing")
        return False

    dedupe_key = payload.get("google_maps_link") or payload.get("agency_name") or ""
    if dedupe_key and dedupe_key in MAPS_POSTED_KEYS:
        print(f"[MAPS] Skipping duplicate: {dedupe_key[:80]}")
        return False

    if dedupe_key:
        MAPS_POSTED_KEYS.add(dedupe_key)

    posted = post_maps_record(record)
    print(f"[MAPS] Record: {json.dumps(payload, ensure_ascii=False)}")
    return posted


def extract_and_post_listing(tab, list_name: str = "", place_href: str = "") -> bool:
    """Read place details from live DOM via CDP and POST immediately."""
    try:
        result = tab.Runtime.evaluate(
            expression=MAPS_PLACE_EXTRACT_JS,
            returnByValue=True,
        )
        data = (result.get("result") or {}).get("value") or {}
        if not isinstance(data, dict):
            print("[MAPS] CDP extract returned non-dict")
            return False
        return post_listing_from_record(data, fallback_name=list_name, fallback_link=place_href)
    except Exception as e:
        print(f"[MAPS] CDP extract error: {e}")
        return False


def normalize_maps_link(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url.split("?")[0])
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")


def post_maps_record(record: dict) -> bool:
    """POST to maps-data with whatever parsed fields are present (all optional)."""
    payload = {k: v for k, v in record.items() if v}
    if not payload:
        return False
    try:
        resp = requests.post(
            MAPS_DATA_URL, json=payload, headers=HEADERS, timeout=REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        label = (
            payload.get("agency_name")
            or payload.get("google_maps_link")
            or payload.get("phone")
            or "listing"
        )
        print(
            f"Maps API: posted '{label}' "
            f"fields={list(payload.keys())}"
        )
        return True
    except Exception as e:
        print(f"[MAPS API ERROR] post failed for '{record.get('agency_name')}': {e}")
        return False


def parse_maps_detail_html(html: str) -> dict:
    """Parse a Google Maps place detail panel from raw page HTML."""
    soup = BeautifulSoup(html, "html.parser")

    name = ""
    for sel in ("h1.DUwDvf", "h1.fontHeadlineLarge", "h1"):
        tag = soup.select_one(sel)
        if tag and tag.get_text(strip=True):
            name = tag.get_text(strip=True)
            break

    address = ""
    addr_el = soup.find(attrs={"data-item-id": "address"})
    if addr_el:
        address = addr_el.get_text(" ", strip=True)

    phone = ""
    for btn in soup.find_all("button", attrs={"data-item-id": True}):
        item_id = btn.get("data-item-id", "")
        if item_id.startswith("phone:"):
            phone = btn.get_text(" ", strip=True)
            if not phone:
                phone = item_id.replace("phone:tel:", "").replace("phone:", "")
            break
    if not phone:
        for btn in soup.find_all("button", attrs={"aria-label": True}):
            label = btn["aria-label"]
            if label.startswith("Phone:") or label.startswith("Call"):
                phone = label.split(":", 1)[-1].strip()
                break

    website = ""
    auth = soup.find("a", attrs={"data-item-id": "authority"})
    if auth and auth.get("href"):
        website = auth["href"]
    if not website:
        for a in soup.find_all("a", href=True):
            label = a.get("aria-label", "")
            if "Website" in label or a.get("data-item-id") == "authority":
                website = a["href"]
                break

    service_type = ""
    for btn in soup.find_all("button", class_=lambda x: x and "DkEaL" in str(x)):
        text = btn.get_text(strip=True)
        if text:
            service_type = text
            break

    email = ""
    mailto = soup.find("a", href=lambda x: x and x.startswith("mailto:"))
    if mailto:
        email = mailto["href"].replace("mailto:", "").split("?")[0]

    maps_link = ""
    og = soup.find("meta", property="og:url")
    if og and og.get("content"):
        maps_link = og["content"]
    if not maps_link:
        canon = soup.find("link", rel="canonical")
        if canon and canon.get("href"):
            maps_link = canon["href"]
    if not maps_link:
        for a in soup.find_all("a", href=True):
            if "/maps/place/" in a["href"]:
                maps_link = a["href"]
                break

    city = ""
    if address:
        parts = [p.strip() for p in address.split(",")]
        if len(parts) >= 2:
            city = parts[-2]

    record = {
        "agency_name":      name or None,
        "address":          address or None,
        "phone":            phone or None,
        "email":            email or None,
        "city":             city or None,
        "service_type":     service_type or None,
        "website":          website or None,
        "google_maps_link": normalize_maps_link(maps_link) or None,
    }
    return {k: v for k, v in record.items() if v}


def open_maps_via_search(tab) -> str:
    """
    Enter Google Maps via UI clicks on the current tab (no Page.navigate).
    Expects google.com/search after perform_google_search().
    """
    check = tab.Runtime.evaluate(
        expression="window.location.href",
        returnByValue=True,
    )
    current_url = (check.get("result") or {}).get("value") or ""

    if "google.com/maps" in current_url:
        print("[MAPS] Already on Google Maps — continuing without navigation")
        return "ALREADY_ON_MAPS"

    if "google.com/search" not in current_url:
        print("[MAPS] Not on Google Search — skipping (no direct URL navigation)")
        return "NOT_ON_SEARCH"

    print("[MAPS] On Google Search — clicking Maps tab (no URL navigation)")

    time.sleep(random.uniform(1.5, 3))
    tab.Runtime.evaluate(
        expression="window.scrollTo({ top: 0, behavior: 'smooth' });",
        returnByValue=True,
    )
    time.sleep(random.uniform(1, 2))

    result = tab.Runtime.evaluate(
        expression="""
        (() => {
            const mapsTab =
                document.querySelector('a[href*="tbm=map"]') ||
                [...document.querySelectorAll('a')].find(a => {
                    const t = (a.textContent || '').trim();
                    return t === 'Maps';
                });
            if (mapsTab) {
                mapsTab.scrollIntoView({ behavior: 'smooth', block: 'center' });
                mapsTab.click();
                return 'MAPS_TAB_CLICKED';
            }

            const moreLink = [...document.querySelectorAll('a')].find(a => {
                const t = (a.textContent || '').trim();
                const h = a.href || '';
                return /more places|view all/i.test(t)
                    || h.includes('/maps/search/')
                    || h.includes('maps.google.com');
            });
            if (moreLink) {
                moreLink.scrollIntoView({ behavior: 'smooth', block: 'center' });
                moreLink.click();
                return 'MAPS_LINK_CLICKED';
            }

            return 'MAPS_ENTRY_NOT_FOUND';
        })();
        """,
        returnByValue=True,
    )
    status = (result.get("result") or {}).get("value") or "MAPS_ENTRY_NOT_FOUND"
    print(f"[MAPS] Enter Maps via click: {status}")

    if status not in ("MAPS_ENTRY_NOT_FOUND", "NOT_ON_SEARCH"):
        tab.wait(6)
        time.sleep(random.uniform(2, 4))

    return status


def scrape_maps_for_keyword(keyword: str, tab, country: str = ""):
    """
    Search → Maps tab → scroll until end → click listings → maps-data.
    No Page.navigate calls.
    """
    global MAPS_POSTED_KEYS, CURRENT_KEYWORD, CURRENT_COUNTRY

    MAPS_POSTED_KEYS = set()
    CURRENT_KEYWORD  = keyword
    CURRENT_COUNTRY  = (country or "").strip() or None

    print(
        f"\n[MAPS] Starting scrape for '{keyword}' "
        f"country='{CURRENT_COUNTRY or '-'}' (clicks only, no URL navigation)"
    )

    try:
        entry = open_maps_via_search(tab)
        if entry in ("MAPS_ENTRY_NOT_FOUND", "NOT_ON_SEARCH"):
            print("[MAPS] Could not open Maps from current page — skipping")
            return

        clicked_links = set()
        no_new_rounds   = 0
        max_no_new      = 6

        while no_new_rounds < max_no_new:
            result = tab.Runtime.evaluate(
                expression="""
                (() => {
                    const links = [...document.querySelectorAll('a[href*="/maps/place/"]')];
                    const seen = new Set();
                    const out = [];
                    for (const a of links) {
                        const href = a.href.split('?')[0];
                        if (seen.has(href)) continue;
                        seen.add(href);
                        out.push({
                            href: href,
                            name: a.getAttribute('aria-label') || a.textContent.trim()
                        });
                    }
                    return out;
                })();
                """,
                returnByValue=True,
            )
            listings = (result.get("result") or {}).get("value") or []
            new_listings = [x for x in listings if x["href"] not in clicked_links]

            if not new_listings:
                no_new_rounds += 1
            else:
                no_new_rounds = 0

            for item in new_listings:
                href = item["href"]
                clicked_links.add(href)

                click_result = tab.Runtime.evaluate(
                    expression=f"""
                    (() => {{
                        const target = {json.dumps(href)};
                        const a = [...document.querySelectorAll('a[href*="/maps/place/"]')]
                            .find(x => x.href.split('?')[0] === target);
                        if (!a) return "NOT_FOUND";
                        a.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
                        a.click();
                        return "CLICKED";
                    }})();
                    """,
                    returnByValue=True,
                )
                status = (click_result.get("result") or {}).get("value")
                print(f"[MAPS] {status}: {item.get('name') or href}")

                if status != "CLICKED":
                    continue

                time.sleep(random.uniform(2.5, 4))
                list_name = item.get("name") or ""
                posted = extract_and_post_listing(tab, list_name=list_name, place_href=href)
                if not posted:
                    print("[MAPS] CDP post failed — trying extension fallback")
                    pyautogui.hotkey("ctrl", "space")
                    time.sleep(random.uniform(2, 3))

            scroll_result = tab.Runtime.evaluate(
                expression="""
                (() => {
                    const feed = document.querySelector('div[role="feed"]');
                    if (feed) {
                        const before = feed.scrollTop;
                        feed.scrollTop += 900;
                        return feed.scrollTop > before ? 'FEED_SCROLLED' : 'FEED_AT_END';
                    }
                    window.scrollBy({ top: 800, behavior: 'smooth' });
                    return 'PAGE_SCROLLED';
                })();
                """,
                returnByValue=True,
            )
            print(f"[MAPS] Scroll: {(scroll_result.get('result') or {}).get('value')}")
            time.sleep(random.uniform(2, 4))

        print(f"[MAPS] Finished '{keyword}': {len(clicked_links)} listings clicked, "
              f"{len(MAPS_POSTED_KEYS)} posted to maps-data")
    finally:
        pass


# ─────────────────────────────────────────────────────────────
# SERP PARSING (disabled — maps-only mode)
# ─────────────────────────────────────────────────────────────
# def extract_sponsored_results(soup):
#
#     sponsored = []
#     position  = 0
#
#     def process_ad_block(block):
#         nonlocal position
#         link = block.find("a", href=True)
#         if not link:
#             return
#         url = link.get("href", "")
#         if not (url.startswith("/aclk") or url.startswith("http")):
#             return
#
#         position += 1
#
#         title_tag = (
#             block.find("div", class_=lambda x: x and "CCgQ5" in x)
#             or block.find("span", attrs={"role": "heading"})
#             or block.find("div",  attrs={"role": "heading"})
#         )
#         title = title_tag.get_text(strip=True) if title_tag else link.get_text(strip=True)
#
#         cite        = block.find("cite") or block.find("span", class_="VuuXrf")
#         display_url = cite.get_text(strip=True) if cite else ""
#
#         snippet_tag = (
#             block.find("div", class_=lambda x: x and "MUxGbd" in x)
#             or block.find("div", class_=lambda x: x and "yDYNvb" in x)
#         )
#         snippet = snippet_tag.get_text(" ", strip=True) if snippet_tag else ""
#
#         if url.startswith("/aclk"):
#             parsed = parse_qs(urlparse(url).query)
#             url    = parsed.get("adurl", [url])[0]
#
#         sponsored.append({
#             "position":    position,
#             "title":       title,
#             "url":         url,
#             "site_name":   display_url.split(" ")[0] if display_url else "",
#             "display_url": display_url,
#             "snippet":     snippet,
#         })
#
#     tads = soup.find("div", id="tads")
#     if tads:
#         ad_blocks = (
#             tads.find_all("div", class_=lambda x: x and "uEierd" in x)
#             or tads.find_all("div", attrs={"data-text-ad": "1"})
#             or tads.find_all("div", recursive=False)
#         )
#         for block in ad_blocks:
#             process_ad_block(block)
#
#     tadsb = soup.find("div", id="tadsb")
#     if tadsb:
#         for block in tadsb.find_all("div", recursive=False):
#             process_ad_block(block)
#
#     print(f"Found {len(sponsored)} sponsored results")
#     return sponsored
#
#
# def extract_ai_overview(soup):
#
#     ai_data = {"available": False, "heading": "", "sections": [], "sources": []}
#
#     not_available = soup.find("span", {"jsname": "lGsj1"})
#     if not_available and "display:none" not in (not_available.get("style", "")):
#         return ai_data
#
#     ai_container = soup.find("div", id=lambda x: x and x.startswith("B2Jtyd"))
#
#     if not ai_container:
#         heading_tag = (
#             soup.find("div", class_="cUzNTd")
#             or soup.find("div", class_="Fzsovc")
#             or soup.find("div", {"jsname": "cUzNTd"})
#         )
#         if heading_tag:
#             ai_container = heading_tag.find_parent(
#                 "div", class_=lambda x: x and "EyBRub" in str(x)
#             )
#
#     if not ai_container:
#         ai_container = soup.find("div", class_=lambda x: x and "EyBRub" in str(x))
#
#     if not ai_container:
#         return ai_data
#
#     heading_tag = (
#         ai_container.find("div", class_="cUzNTd")
#         or ai_container.find("div", class_="Fzsovc")
#         or soup.find("div", class_="cUzNTd")
#         or soup.find("div", class_="Fzsovc")
#     )
#     if heading_tag:
#         ai_data["heading"] = heading_tag.get_text(strip=True)
#
#     if not ai_data["heading"]:
#         for tag in ai_container.find_all(["div", "span"]):
#             txt = tag.get_text(strip=True)
#             if txt.lower() in ("ai overview", "ai overviews"):
#                 ai_data["heading"] = txt
#                 break
#
#     if not ai_data["heading"]:
#         return ai_data
#
#     ai_data["available"] = True
#
#     for block in ai_container.find_all("div", class_="n6owBd"):
#         text = block.get_text(" ", strip=True)
#         if text:
#             ai_data["sections"].append({"type": "text", "content": text})
#
#     for heading in ai_container.find_all("div", class_="otQkpb"):
#         heading_text = heading.get_text(" ", strip=True)
#         parent  = heading.find_parent("div", attrs={"data-bfc": ""})
#         items   = []
#         if parent:
#             sibling = parent.find_next_sibling("div", attrs={"data-bfc": ""})
#             if sibling:
#                 list_items = sibling.find_all("li", class_="Z1qcYe")
#                 for li in list_items:
#                     t = li.get_text(" ", strip=True)
#                     if t:
#                         items.append(t)
#                 if not items:
#                     t = sibling.get_text(" ", strip=True)
#                     if t:
#                         items.append(t)
#         if heading_text or items:
#             ai_data["sections"].append({"type": "section", "heading": heading_text, "items": items})
#
#     for table in ai_container.find_all("table", class_="NRefec"):
#         rows = []
#         for tr in table.find_all("tr"):
#             cells = [td.get_text(" ", strip=True) for td in tr.find_all(["th", "td"])]
#             if cells:
#                 rows.append(cells)
#         if rows:
#             ai_data["sections"].append({"type": "table", "rows": rows})
#
#     seen_urls = set()
#     for link in ai_container.find_all("a", class_="muU3oe"):
#         href = link.get("href", "")
#         if href and href not in seen_urls:
#             seen_urls.add(href)
#             ai_data["sources"].append(href)
#
#     return ai_data
#
#
# def extract_people_also_search_for(soup):
#
#     pasf_data = {"available": False, "heading": "", "results": []}
#
#     bres = soup.find("div", id="bres")
#     if not bres:
#         return pasf_data
#
#     heading_tag = bres.find(["span", "div"], class_=lambda x: x and "mgAbYb" in str(x))
#     if heading_tag:
#         pasf_data["heading"] = heading_tag.get_text(" ", strip=True)
#
#     cards    = bres.find_all("a", class_=lambda x: x and "ngTNl" in str(x))
#     seen     = set()
#     position = 0
#
#     for card in cards:
#         href = card.get("href", "")
#         if not href:
#             continue
#         text_tag = card.find("span", class_=lambda x: x and "dg6jd" in str(x))
#         query    = text_tag.get_text(" ", strip=True) if text_tag else ""
#         if not query or query in seen:
#             continue
#         seen.add(query)
#         position += 1
#         pasf_data["results"].append({
#             "position": position,
#             "query":    query,
#             "url":      f"https://www.google.com{href}"
#         })
#
#     if pasf_data["results"]:
#         pasf_data["available"] = True
#
#     print(f"Found {len(pasf_data['results'])} 'People also search for' results")
#     return pasf_data
#
#
# def parse_html(html: str) -> dict:
#     """Parse raw Google SERP HTML and return a structured page dict."""
#
#     soup = BeautifulSoup(html, "html.parser")
#
#     ai_overview            = extract_ai_overview(soup)
#     sponsored_data         = extract_sponsored_results(soup)
#     people_also_search_for = extract_people_also_search_for(soup)
#
#     if ai_overview["available"]:
#         print(f"AI Overview: {ai_overview['heading']} | "
#               f"Sections: {len(ai_overview['sections'])}, "
#               f"Sources: {len(ai_overview['sources'])}")
#     else:
#         print("No AI Overview present")
#
#     all_data    = []
#     num_results = 50
#     results     = soup.find_all("div", class_=lambda x: x and "tF2Cxc" in x)
#     print(f"Found {len(results)} organic results")
#
#     for idx, result in enumerate(results, start=1):
#         if len(all_data) >= num_results:
#             break
#
#         data     = {"position": idx}
#         link_tag = result.find("a", href=True)
#         url      = link_tag.get("href") if link_tag else ""
#
#         if url.startswith("/url?"):
#             parsed = parse_qs(urlparse(url).query)
#             url    = parsed.get("q", [""])[0]
#
#         data["url"]         = url
#         title_tag           = result.find("h3")
#         data["title"]       = title_tag.get_text(strip=True) if title_tag else ""
#         site_tag            = result.find("span", class_="VuuXrf")
#         data["site_name"]   = site_tag.get_text(strip=True) if site_tag else ""
#         cite_tag            = result.find("cite")
#         data["display_url"] = cite_tag.get_text(" ", strip=True) if cite_tag else ""
#
#         snippet_tag = result.find("div", class_="VwiC3b")
#         snippet     = ""
#         if snippet_tag:
#             for a in snippet_tag.find_all("a"):
#                 a.extract()
#             snippet = snippet_tag.get_text(" ", strip=True)
#         data["snippet"] = snippet
#
#         if data["title"] and data["url"]:
#             all_data.append(data)
#
#     print({
#         "ai_overview":            ai_overview,
#         "sponsored_results":      sponsored_data,
#         "organic_results":        all_data,
#         "people_also_search_for": people_also_search_for,
#     })
#     return {
#         "ai_overview":            ai_overview,
#         "sponsored_results":      sponsored_data,
#         "organic_results":        all_data,
#         "people_also_search_for": people_also_search_for,
#     }


# ─────────────────────────────────────────────────────────────
# ROUTE 1 — Receive HTML from Chrome extension (maps only)
# ─────────────────────────────────────────────────────────────
@app.route("/htmls", methods=["POST"])
def receive_html():

    global MAPS_POSTED_KEYS

    body    = request.json or {}
    html    = body.get("html", "")
    keyword = body.get("keyword") or CURRENT_KEYWORD
    country = (body.get("country") or CURRENT_COUNTRY or "").strip()

    record = parse_maps_detail_html(html)
    if country and not record.get("country"):
        record["country"] = country

    posted = post_listing_from_record(record)
    payload = {k: v for k, v in record.items() if v}
    if not payload:
        return jsonify({
            "status":  "skipped",
            "mode":    "maps",
            "keyword": keyword,
            "message": "No fields parsed from HTML",
        })

    return jsonify({
        "status":  "success" if posted else "error",
        "mode":    "maps",
        "keyword": keyword,
        "record":  payload,
    })

    # ── SERP receive_html (disabled — maps-only mode) ──
    # global CURRENT_PAGE_NUM, MAPS_POSTED_KEYS
    # page    = body.get("page")    or CURRENT_PAGE_NUM
    # if not keyword:
    #     return jsonify({"status": "error", "message": "No active keyword"}), 400
    # page_label = f"page {page}"
    # page_dict = parse_html(html)
    # sno = body.get("sno") or CURRENT_SNO or get_sno_for_keyword(keyword)
    # if sno is None:
    #     sno = insert_keyword(keyword, DEFAULT_MAX_PAGES)
    # append_page_data(sno, page, page_dict)
    # return jsonify({...})


# ─────────────────────────────────────────────────────────────
# ROUTE 2 — Receive keywords & queue for automation
# ─────────────────────────────────────────────────────────────
@app.route("/keywords", methods=["POST"])
def receive_keywords():

    global KEYWORDS

    data    = request.json
    keywords = data.get("keywords", "")
    country  = (data.get("country") or "").strip()

    if isinstance(keywords, str):
        KEYWORDS = [k.strip() for k in keywords.split(",") if k.strip()]
    elif isinstance(keywords, list):
        KEYWORDS = [str(k).strip() for k in keywords if str(k).strip()]
    else:
        KEYWORDS = []

    print(f"\nReceived Keywords: {KEYWORDS} country='{country or '-'}'")

    for kw in KEYWORDS:
        insert_keyword(kw, country=country)

    return jsonify({
        "status":  "queued",
        "count":   len(KEYWORDS),
        "keywords": KEYWORDS,
        "country": country or None,
    })


# ─────────────────────────────────────────────────────────────
# CHROME AUTOMATION — UI clicks / typing only (no Page.navigate)
# ─────────────────────────────────────────────────────────────

def _current_url(tab) -> str:
    result = tab.Runtime.evaluate(
        expression="window.location.href",
        returnByValue=True,
    )
    return (result.get("result") or {}).get("value") or ""


def _needs_google_home(url: str) -> bool:
    """True when we must open google.com via address bar before searching."""
    if "google.com/search" in url:
        return False
    if "google.com" in url and "/maps" not in url and "accounts.google" not in url:
        return False
    return True


def open_google_home_via_ui(tab) -> bool:
    """Open Google homepage using address-bar typing — never Page.navigate."""
    url = _current_url(tab)
    if not _needs_google_home(url):
        print(f"[UI] Already on Google web context: {url[:80]}")
        return True

    print("[UI] Opening google.com via address bar (no Page.navigate)")
    pyautogui.hotkey("ctrl", "l")
    time.sleep(random.uniform(0.4, 0.8))
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.15)
    pyautogui.write("google.com", interval=random.uniform(0.05, 0.1))
    pyautogui.press("enter")
    tab.wait(5)
    time.sleep(random.uniform(1.5, 3))
    return "google.com" in _current_url(tab) and "/maps" not in _current_url(tab)


def focus_google_search_box(tab) -> bool:
    """Focus the visible Google search input on homepage, SERP, or Maps."""
    result = tab.Runtime.evaluate(
        expression="""
        (() => {
            const selectors = [
                'textarea[name="q"]',
                'input[name="q"]',
                'input[title="Search"]',
                'textarea[title="Search"]'
            ];
            for (const sel of selectors) {
                const el = document.querySelector(sel);
                if (el) {
                    el.focus();
                    el.click();
                    return true;
                }
            }
            return false;
        })();
        """,
        returnByValue=True,
    )
    return bool((result.get("result") or {}).get("value"))


def perform_google_search(tab, keyword: str, country: str = "") -> bool:
    """
    Run a Google web search by typing in the search box.
    Never calls Page.navigate or loads a search URL directly.
    """
    search_query = build_search_query(keyword, country)
    print(f"[UI] Searching via search box: '{search_query}'")

    if not open_google_home_via_ui(tab):
        print("[UI] WARN: could not confirm google.com — trying search box anyway")

    if not focus_google_search_box(tab):
        if not open_google_home_via_ui(tab) or not focus_google_search_box(tab):
            print("[UI] ERROR: Google search box not found")
            return False

    time.sleep(random.uniform(0.4, 0.9))
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.15)
    pyautogui.write(search_query, interval=random.uniform(0.04, 0.09))
    time.sleep(random.uniform(0.4, 0.8))
    pyautogui.press("enter")

    tab.wait(5)
    time.sleep(random.uniform(2, 4))

    url = _current_url(tab)
    ok = "google.com/search" in url
    print(f"[UI] Search result URL: {url[:100]}")
    if not ok:
        print("[UI] WARN: expected google.com/search after Enter")
    return ok


def start_searching():
    """
    Background worker — maps-only mode.
    Picks pending keywords, searches via UI, scrapes Maps → maps-data API.
    """
    global CURRENT_KEYWORD, CURRENT_SNO, CURRENT_COUNTRY

    subprocess.Popen([
        CHROME_PATH,
        "--remote-debugging-port=9222",
        "--user-data-dir=C:\\chrome_debug"
    ])

    print("Launching Chrome...")
    time.sleep(5)

    browser = pychrome.Browser(url="http://127.0.0.1:9222")
    tab     = browser.new_tab()
    tab.start()
    tab.Page.enable()
    tab.Runtime.enable()

    while True:

        job = get_pending_keyword()

        if not job:
            print("No pending keywords. Waiting...")
            time.sleep(10)
            continue

        sno     = job["sno"]
        keyword = job["keyword"]
        country = job.get("country") or ""

        try:
            print(
                f"\n[MAPS] Processing keyword: '{keyword}' "
                f"country: '{country or '-'}'"
            )

            CURRENT_KEYWORD = keyword
            CURRENT_SNO     = sno
            CURRENT_COUNTRY = country or None

            if not perform_google_search(tab, keyword, country=country):
                raise RuntimeError(f"UI search failed for '{keyword}'")

            scrape_maps_for_keyword(keyword, tab, country=country)

            update_status(sno, "completed")
            print(
                f"Completed Maps job sno={sno} keyword='{keyword}' "
                f"country='{country or '-'}'"
            )

        except Exception as e:
            print(f"SCRAPER ERROR for '{keyword}': {e}")
            update_status(sno, "failed")
            continue

    # ── SERP scraping loop (disabled — maps-only mode) ──
    # max_pages = job["num_pages"]
    # CURRENT_PAGE_NUM = 1
    # pyautogui.hotkey("ctrl", "space")  # SERP page capture
    # while page_count < max_pages:
    #     ... scroll, click Next, extension per SERP page ...
    # append_page_data(sno, page, page_dict)


# ─────────────────────────────────────────────────────────────
# RUN
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"Maps jobs API:     {MAPS_JOBS_URL}")
    print(f"Maps data API:     {MAPS_DATA_URL}")
    init_db()
    print("Starting Flask on http://127.0.0.1:5000 (maps-only mode)")

    # Single background thread picks up all pending keywords indefinitely
    threading.Thread(
        target=start_searching,
        daemon=True
    ).start()

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=False
    )