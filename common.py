#!/usr/bin/env python3
"""
Huurhuis-Alert — gedeelde basis
================================
Config, Scrape.do-fetch, SQLite, Telegram. Gebruikt door zowel
`scraper.py` (live alerts) als `research.py` (Funda discovery + €/m²).

Eén scraping-provider: Scrape.do (anti-bot bypass). Geen Playwright lokaal.
"""

import os
import sys
import json
import time
import sqlite3
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests as req_lib

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("huurhuis")

# ── Secrets (komen uit GitHub Secrets / .env, NOOIT in git) ──────────────────

SCRAPE_DO_TOKEN = os.environ.get("SCRAPE_DO_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

DRY_RUN = "--dry-run" in sys.argv or not TELEGRAM_BOT_TOKEN

# ── Zoekcriteria (pas aan naar wens) ─────────────────────────────────────────

SEARCH_CRITERIA = {
    "city": "alkmaar",       # Funda/Pararius slug, lowercase
    "city_label": "Alkmaar",
    "price_max": 1500,       # max huur p/mnd — harde filter
    "price_min": 0,
    "area_min_m2": 45,       # harde filter: kleiner = skip
    "area_ideal_m2": 50,     # 50+ = "ruim genoeg" in de alert
    "rooms_min": 2,          # harde filter
    "prefer_outdoor": True,  # balkon/tuin = bonus (niet hard)
    "prefer_furnished": True,  # gemeubileerd = bonus (niet hard)
}

# €/m² benchmark voor Alkmaar. research.py overschrijft dit met echte data
# (data/prices.json). Tot die tijd: ruwe schatting.
PRICE_PER_M2_FALLBACK = {
    "cheap": 18.0,      # < dit = goede deal
    "expensive": 24.0,  # > dit = duur
}

DB_PATH = os.environ.get("HUURHUIS_DB", "listings.db")
PRICES_JSON = "data/prices.json"


def load_price_benchmark() -> dict:
    """Lees de €/m²-benchmark uit research-data, val terug op de schatting."""
    try:
        with open(PRICES_JSON, encoding="utf-8") as fh:
            data = json.load(fh)
        return {
            "cheap": float(data.get("p25_per_m2", PRICE_PER_M2_FALLBACK["cheap"])),
            "expensive": float(data.get("p75_per_m2", PRICE_PER_M2_FALLBACK["expensive"])),
            "median": float(data.get("median_per_m2", 0)) or None,
            "n": int(data.get("sample_size", 0)),
        }
    except (FileNotFoundError, ValueError, KeyError, json.JSONDecodeError):
        return {**PRICE_PER_M2_FALLBACK, "median": None, "n": 0}


# ── Data model ───────────────────────────────────────────────────────────────


@dataclass
class Listing:
    id: str
    source: str            # "pararius", "funda", of makelaar-key
    title: str             # adres
    url: str
    price: int = 0         # huur p/mnd
    area_m2: int = 0
    rooms: int = 0
    location: str = ""     # bv. "Alkmaar"
    makelaar: str = ""     # naam van de makelaar (indien bekend)
    description: str = ""  # detailpagina-tekst voor AI
    detail_incomplete: bool = False
    # AI-gevulde velden:
    bedrooms: int = 0
    outdoor: str = ""      # "balkon" / "tuin" / "dakterras" / "geen"
    condition: str = ""    # "nieuwbouw" / "gerenoveerd" / "normaal" / "gedateerd"
    furnished: str = ""    # "kaal" / "gestoffeerd" / "gemeubileerd"
    energy_label: str = ""
    available_from: str = ""

    @property
    def price_per_m2(self) -> float:
        return round(self.price / self.area_m2, 1) if self.area_m2 else 0.0


# ── Database ──────────────────────────────────────────────────────────────────


def init_db(path: str = DB_PATH):
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS listings (
            id TEXT PRIMARY KEY,
            source TEXT,
            title TEXT,
            url TEXT,
            price INTEGER,
            area_m2 INTEGER,
            rooms INTEGER,
            location TEXT,
            makelaar TEXT,
            price_per_m2 REAL,
            outdoor TEXT,
            condition TEXT,
            furnished TEXT,
            energy_label TEXT,
            first_seen TEXT,
            last_seen TEXT
        )
        """
    )
    conn.commit()
    return conn


def listing_exists(conn, listing_id: str) -> bool:
    return conn.execute("SELECT 1 FROM listings WHERE id = ?", (listing_id,)).fetchone() is not None


def save_listing(conn, lst: Listing):
    now = datetime.now(timezone.utc).isoformat()
    if listing_exists(conn, lst.id):
        conn.execute(
            "UPDATE listings SET last_seen = ?, price = ? WHERE id = ?",
            (now, lst.price, lst.id),
        )
    else:
        conn.execute(
            """INSERT INTO listings
               (id, source, title, url, price, area_m2, rooms, location, makelaar,
                price_per_m2, outdoor, condition, furnished, energy_label, first_seen, last_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (lst.id, lst.source, lst.title, lst.url, lst.price, lst.area_m2, lst.rooms,
             lst.location, lst.makelaar, lst.price_per_m2, lst.outdoor, lst.condition,
             lst.furnished, lst.energy_label, now, now),
        )
    conn.commit()


# ── Scrape.do ──────────────────────────────────────────────────────────────────


def scrape_do_fetch(url: str, render: bool = False, retries: int = 1, super_mode: bool = True,
                    timeout: int = 45, render_wait: int = 5000, geo_code: str = "nl",
                    set_cookies: str = "", wait_selector: str = "",
                    block_resources: bool | None = None) -> str | None:
    """Haal een pagina op via Scrape.do met retry + backoff.

    super=true  — residential proxy + anti-bot bypass (nodig voor Funda/Akamai).
    render=true — headless Chromium voor JS-sites.
    geoCode=nl  — Nederlands IP (belangrijk voor NL-sites).
    """
    if not SCRAPE_DO_TOKEN:
        log.error("SCRAPE_DO_TOKEN niet geconfigureerd")
        return None

    for attempt in range(retries + 1):
        try:
            params = {"token": SCRAPE_DO_TOKEN, "url": url}
            if super_mode:
                params["super"] = "true"
            if geo_code:
                params["geoCode"] = geo_code
            if set_cookies:
                params["setCookies"] = set_cookies
            if render:
                params["render"] = "true"
                params["customWait"] = str(render_wait)
                if block_resources is False:
                    params["blockResources"] = "false"
                if wait_selector:
                    params["waitSelector"] = wait_selector

            resp = req_lib.get("https://api.scrape.do", params=params, timeout=timeout)
            log.info("Scrape.do: status=%d size=%d → %s", resp.status_code, len(resp.content), url[:80])

            if resp.status_code == 200 and len(resp.text) > 500:
                return resp.text
            if resp.status_code == 401:
                log.error("Scrape.do: ongeldige API token")
                return None
            if resp.status_code == 429:
                log.warning("Scrape.do: rate limit / credits op")

        except req_lib.RequestException as e:
            log.error("Scrape.do request mislukt: %s", e)

        if attempt < retries:
            wait = 2 ** (attempt + 1)
            log.info("Retry %d/%d na %ds ...", attempt + 1, retries, wait)
            time.sleep(wait)

    return None


# ── Telegram ───────────────────────────────────────────────────────────────────


def _tg_send(text: str, disable_preview: bool = False) -> bool:
    if DRY_RUN:
        log.info("[DRY-RUN] Telegram:\n%s", text)
        return True
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = req_lib.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview,
        }, timeout=30)
        if resp.ok:
            return True
        log.error("Telegram fout: %s %s", resp.status_code, resp.text[:200])
    except req_lib.RequestException as e:
        log.error("Telegram request mislukt: %s", e)
    return False


def send_failure_alert(error_msg: str, attempts: int):
    if DRY_RUN or not TELEGRAM_BOT_TOKEN:
        return
    text = (
        "🚨 <b>HUURHUIS-SCRAPER GEFAALD</b>\n\n"
        f"Gecrasht na {attempts} pogingen.\n\n"
        f"<b>Fout:</b> <code>{error_msg[:500]}</code>\n\n"
        "⚠️ Geen monitoring tot de volgende run."
    )
    _tg_send(text, disable_preview=True)
