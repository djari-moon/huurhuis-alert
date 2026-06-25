#!/usr/bin/env python3
"""
Huurhuis-Alert — live scraper
=============================
Draait elke ~5 min (GitHub Actions + cron-job.org). Loopt over de bronnen,
filtert op criteria, scoort met Claude Haiku, en stuurt een Telegram-alert
bij een nieuwe match. €/m² is het hoofdgetal.

Bronnen:
  1. Lokale makelaars (makelaars.py)  → snelst, hier verschijnt het eerst
  2. Pararius                          → groot huuraanbod, makkelijk
  3. Funda                             → breed, maar traag/lastig (best-effort)

Gebruik:
  python scraper.py --dry-run --force   # geen Telegram, geen quiet hours
  python scraper.py --only=pararius     # alleen één bron testen
"""

import re
import sys
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from common import (
    log, SEARCH_CRITERIA, ANTHROPIC_API_KEY,
    Listing, init_db, listing_exists, save_listing,
    scrape_do_fetch, _tg_send, send_failure_alert, load_price_benchmark,
)
from makelaars import MAKELAARS

try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

CITY = SEARCH_CRITERIA["city"]
CITY_LABEL = SEARCH_CRITERIA["city_label"]
PRICE_MAX = SEARCH_CRITERIA["price_max"]

ONLY = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--only=")), "")

# ── Parse-helpers ─────────────────────────────────────────────────────────────

_AREA_RE = re.compile(r"(\d{2,4})\s*m[²2]")


def parse_price(text: str) -> int:
    """Pakt een huurprijs uit tekst. Ondersteunt '€ 1.495', '1.495,-' en
    losse bedragen. Vermijdt huisnummers/m² door op een plausibele range
    (€300–€9999) te filteren bij losse getallen."""
    if not text:
        return 0
    m = re.search(r"€\s*([\d.]{3,})", text) or re.search(r"([\d.]{3,})\s*,-", text)
    if m:
        try:
            return int(re.sub(r"[.\s]", "", m.group(1)))
        except ValueError:
            return 0
    for cand in re.finditer(r"\b(\d[\d.]{2,4})\b", text):
        val = int(re.sub(r"[.\s]", "", cand.group(1)))
        if 300 <= val <= 9999:
            return val
    return 0


def parse_area(text: str) -> int:
    m = _AREA_RE.search(text or "")
    return int(m.group(1)) if m else 0


def _hash_id(source: str, url: str) -> str:
    import hashlib
    return f"{source}_{hashlib.sha1(url.encode()).hexdigest()[:16]}"


def _base_of(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _title_from_href(href: str) -> str:
    """Maak een leesbare titel uit een detail-URL-slug."""
    slug = href.rstrip("/").split("/")[-1]
    slug = re.sub(r"[-_]?\d{6,}[a-z0-9]*$", "", slug)   # Realworks-id eraf
    slug = re.sub(r"[-_]+", " ", slug).strip()
    return slug.title() or "Huurwoning"


def _clean_title(text: str, href: str) -> str:
    """Adres = tekst vóór de prijs; anders uit de href."""
    if text:
        head = re.split(r"€|huurprijs|\bp/m|\bper maand", text, flags=re.I)[0].strip()
        head = re.sub(r"^(te huur|nieuw in verhuur|status:\s*te huur|onder optie)[:\s]*", "", head, flags=re.I).strip()
        if 4 <= len(head) <= 80:
            return head
    return _title_from_href(href)


def _dump_debug(name: str, html: str):
    try:
        with open(f"debug_{name}.html", "w", encoding="utf-8") as fh:
            fh.write(html[:600_000])
    except OSError:
        pass


def _text(el, selector: str) -> str:
    found = el.select_one(selector)
    return found.get_text(strip=True) if found else ""


# ── Pararius ────────────────────────────────────────────────────────────────────


def scrape_pararius() -> list:
    base = "https://www.pararius.nl"
    url = f"{base}/huurwoningen/{CITY}/0-{PRICE_MAX}"
    html = scrape_do_fetch(url, super_mode=True, retries=1, geo_code="nl")  # Cloudflare → super nodig
    if not html:
        log.warning("Pararius: geen HTML")
        return []
    _dump_debug("pararius", html)

    soup = BeautifulSoup(html, "html.parser")
    items = soup.select("section.listing-search-item, li.search-list__item--listing")
    log.info("Pararius: %d cards", len(items))

    listings = []
    for it in items:
        link_el = it.select_one("a.listing-search-item__link--title, a.listing-search-item__link")
        if not link_el or not link_el.get("href"):
            continue
        url_full = urljoin(base, link_el["href"])
        listings.append(Listing(
            id=_hash_id("pararius", url_full), source="pararius",
            title=link_el.get_text(strip=True),
            url=url_full,
            price=parse_price(_text(it, ".listing-search-item__price")),
            area_m2=parse_area(_text(it, ".illustrated-features__item--surface-area")),
            rooms=int(re.search(r"\d+", _text(it, ".illustrated-features__item--number-of-rooms") or "0").group() or 0),
            location=_text(it, ".listing-search-item__sub-title") or CITY_LABEL,
            makelaar=_text(it, ".listing-search-item__info"),
        ))
    return listings


# ── Funda (best-effort: JSON-LD eerst, dan DOM) ───────────────────────────────


def scrape_funda() -> list:
    url = (f"https://www.funda.nl/zoeken/huur?selected_area=%5B%22{CITY}%22%5D"
           f"&price=%220-{PRICE_MAX}%22")
    html = scrape_do_fetch(url, super_mode=True, retries=2, geo_code="nl",
                           render=True, render_wait=4000)
    if not html:
        log.warning("Funda: geen HTML")
        return []
    _dump_debug("funda", html)

    listings = _parse_funda_jsonld(html)
    if listings:
        log.info("Funda: %d via JSON-LD", len(listings))
        return listings

    soup = BeautifulSoup(html, "html.parser")
    listings, seen = [], set()
    for a in soup.find_all("a", href=True):
        if "/detail/huur/" not in a["href"]:
            continue
        url_full = urljoin("https://www.funda.nl", a["href"])
        if url_full in seen:
            continue
        seen.add(url_full)
        listings.append(Listing(
            id=_hash_id("funda", url_full), source="funda",
            title=_clean_title(a.get_text(" ", strip=True), a["href"]),
            url=url_full, location=CITY_LABEL,
        ))
    log.info("Funda: %d via DOM", len(listings))
    return listings


def _parse_funda_jsonld(html: str) -> list:
    listings = []
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") == "ItemList":
            for el in data.get("itemListElement", []):
                item = el.get("item", el) if isinstance(el, dict) else {}
                u = item.get("url", "")
                if u and "/huur/" in u:
                    listings.append(Listing(
                        id=_hash_id("funda", u), source="funda",
                        title=item.get("name", ""), url=u, location=CITY_LABEL))
    return listings


# ── Generieke makelaar-scraper (link-mode) ────────────────────────────────────


def scrape_makelaar(cfg: dict) -> list:
    key = cfg["key"]
    base = _base_of(cfg["url"])
    link_re = re.compile(cfg["link_re"], re.I)
    city = cfg.get("city_filter", "").lower()

    # Paginatie: cfg["pages"]=N → ?page=0..N-1 doorlopen. Anders 1 pagina.
    pages = cfg.get("pages", 1)
    sep = "&" if "?" in cfg["url"] else "?"
    urls = [f"{cfg['url']}{sep}page={i}" for i in range(pages)] if pages > 1 else [cfg["url"]]

    listings = {}
    for n, url in enumerate(urls):
        html = scrape_do_fetch(
            url, super_mode=cfg.get("super", False), retries=1, geo_code="nl",
            render=cfg.get("render", False), render_wait=cfg.get("render_wait", 5000),
            wait_selector=cfg.get("wait_selector", ""),
            block_resources=cfg.get("block_resources"),
        )
        if not html:
            continue
        _dump_debug(key if n == 0 else f"{key}_p{n}", html)
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not link_re.search(href):
                continue
            url_full = urljoin(base, href)
            if url_full in listings:
                continue
            text = a.get_text(" ", strip=True)
            hay = (href + " " + text).lower()
            if city and city not in hay:
                continue
            if re.search(r"\bverhuurd\b", hay):   # alleen beschikbaar aanbod alerten
                continue
            listings[url_full] = Listing(
                id=_hash_id(key, url_full), source=key, makelaar=cfg["name"],
                title=_clean_title(text, href), url=url_full,
                price=parse_price(text), area_m2=parse_area(text),
                location=CITY_LABEL,
            )
    log.info("Makelaar %s: %d woningen", key, len(listings))
    return list(listings.values())


# ── Detailpagina's (parallel) ─────────────────────────────────────────────────


# Per-bron fetch-modus voor detailpagina's. Alleen waar nodig super/render
# (= dure credits): Funda (Akamai), Pinedo (DDoS-Guard), Vos (JS-widget).
_FETCH_MODE = {m["key"]: (m.get("super", False), m.get("render", False)) for m in MAKELAARS}
_FETCH_MODE["funda"] = (True, True)
_FETCH_MODE["pararius"] = (True, False)   # Cloudflare → super nodig


def _fetch_detail(lst: Listing) -> Listing:
    sup, ren = _FETCH_MODE.get(lst.source, (False, False))
    html = scrape_do_fetch(lst.url, super_mode=sup, retries=1, geo_code="nl", render=ren)
    if html and len(html) > 3000:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "svg"]):
            tag.decompose()
        text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))
        if len(text) > 200:
            lst.description = text[:15000]
            if not lst.price:
                lst.price = parse_price(text)
            if not lst.area_m2:
                lst.area_m2 = parse_area(text)
        else:
            lst.detail_incomplete = True
    else:
        lst.detail_incomplete = True
    return lst


def fetch_details(listings: list) -> list:
    if not listings:
        return listings
    log.info("Detailpagina's ophalen voor %d listings ...", len(listings))
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(_fetch_detail, l) for l in listings]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as e:
                log.warning("Detail fout: %s", e)
    return listings


# ── AI scoring (Claude Haiku) — de simpele velden ─────────────────────────────

AI_PROMPT = """Je analyseert een Nederlandse huurwoning-advertentie. Geef ALLEEN
geldige JSON terug, geen uitleg. Velden:

{
  "price_per_month": <kale of totale huur per maand in euro, int, 0 als onbekend>,
  "area_m2": <woonoppervlak in m2, int, 0 als onbekend>,
  "rooms": <aantal kamers, int, 0 als onbekend>,
  "bedrooms": <aantal slaapkamers, int>,
  "outdoor": <"balkon" | "tuin" | "dakterras" | "geen">,
  "condition": <"nieuwbouw" | "gerenoveerd" | "normaal" | "gedateerd">,
  "furnished": <"kaal" | "gestoffeerd" | "gemeubileerd">,
  "energy_label": <"A" t/m "G" of "">,
  "available_from": <datum of "per direct" of "">
}

Wees conservatief: gebruik "geen"/"normaal"/"" als het niet duidelijk uit de tekst blijkt."""


def score_ai(lst: Listing) -> Listing:
    if not HAS_ANTHROPIC or not ANTHROPIC_API_KEY or lst.detail_incomplete:
        return lst
    text = f"TITEL: {lst.title}\n\n{lst.description}"[:14000]
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=300, temperature=0,
            messages=[{"role": "user", "content": f"{AI_PROMPT}\n\n---\n{text}"}],
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        d = json.loads(raw)
        lst.price = lst.price or int(d.get("price_per_month") or 0)
        lst.area_m2 = lst.area_m2 or int(d.get("area_m2") or 0)
        lst.rooms = lst.rooms or int(d.get("rooms") or 0)
        lst.bedrooms = int(d.get("bedrooms") or 0)
        lst.outdoor = d.get("outdoor", "")
        lst.condition = d.get("condition", "")
        lst.furnished = d.get("furnished", "")
        lst.energy_label = d.get("energy_label", "")
        lst.available_from = d.get("available_from", "")
    except Exception as e:
        log.warning("AI scoring fout voor %s: %s", lst.title[:40], e)
    return lst


# ── Filters + €/m²-oordeel ─────────────────────────────────────────────────────


def passes_filters(lst: Listing) -> bool:
    if lst.price and lst.price > PRICE_MAX:
        return False
    if lst.area_m2 and lst.area_m2 < SEARCH_CRITERIA["area_min_m2"]:
        return False
    if lst.rooms and lst.rooms < SEARCH_CRITERIA["rooms_min"]:
        return False
    return True


def price_verdict(lst: Listing, bench: dict) -> str:
    ppm = lst.price_per_m2
    if not ppm:
        return ""
    if ppm < bench["cheap"]:
        return f"🟢 €{ppm}/m² — goedkoop voor {CITY_LABEL}"
    if ppm > bench["expensive"]:
        return f"🔴 €{ppm}/m² — aan de dure kant"
    return f"🟡 €{ppm}/m² — gemiddeld voor {CITY_LABEL}"


# ── Telegram alert ─────────────────────────────────────────────────────────────


def send_alert(lst: Listing, bench: dict) -> bool:
    ruim = "✅" if lst.area_m2 >= SEARCH_CRITERIA["area_ideal_m2"] else ""
    lines = [f"🏠 <b>{lst.title or 'Huurwoning'}</b>", f"📍 {lst.location}"]

    spec = []
    if lst.price:
        spec.append(f"€{lst.price:,}/mnd".replace(",", "."))
    if lst.area_m2:
        spec.append(f"{lst.area_m2}m² {ruim}".strip())
    if lst.rooms:
        spec.append(f"{lst.rooms} kamers")
    if spec:
        lines.append("💰 " + " · ".join(spec))

    verdict = price_verdict(lst, bench)
    if verdict:
        lines.append(verdict)

    tags = []
    if lst.outdoor and lst.outdoor != "geen":
        tags.append(f"🌿 {lst.outdoor.capitalize()}")
    if lst.condition in ("nieuwbouw", "gerenoveerd"):
        tags.append(f"✨ {lst.condition.capitalize()}")
    if lst.furnished == "gemeubileerd":
        tags.append("🛋 Gemeubileerd")
    if lst.energy_label:
        tags.append(f"⚡ Label {lst.energy_label}")
    if tags:
        lines.append(" · ".join(tags))

    if lst.available_from:
        lines.append(f"📅 Beschikbaar: {lst.available_from}")
    if lst.makelaar:
        lines.append(f"🏢 {lst.makelaar}")
    lines.append(f"🔗 Bron: {lst.source}")
    lines.append(f'<a href="{lst.url}">👉 Bekijk woning</a>')
    return _tg_send("\n".join(lines))


# ── Run ────────────────────────────────────────────────────────────────────────


def _build_sources():
    sources = [("pararius", scrape_pararius), ("funda", scrape_funda)]
    for cfg in MAKELAARS:
        sources.append((cfg["key"], lambda c=cfg: scrape_makelaar(c)))
    if ONLY:
        sources = [(n, fn) for n, fn in sources if n == ONLY]
        log.info("--only=%s → %d bron(nen)", ONLY, len(sources))
    return sources


def _run():
    conn = init_db()
    bench = load_price_benchmark()
    log.info("€/m² benchmark: goedkoop<%.1f, duur>%.1f (n=%d)",
             bench["cheap"], bench["expensive"], bench.get("n", 0))

    sources = _build_sources()
    all_listings, seen = [], set()
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(fn): name for name, fn in sources}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                for lst in fut.result():
                    if lst.id not in seen:
                        seen.add(lst.id)
                        all_listings.append(lst)
            except Exception as e:
                log.error("Bron %s fout: %s", name, e)
    log.info("Totaal %d unieke listings van %d bronnen", len(all_listings), len(sources))

    nieuw = [l for l in all_listings if not listing_exists(conn, l.id)]
    log.info("%d nieuw t.o.v. database", len(nieuw))

    db_count = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    seed = db_count == 0 and len(nieuw) > 5
    if seed:
        log.info("SEED MODE: DB leeg, %d listings opslaan zonder alert (eerste run)", len(nieuw))

    if nieuw:
        fetch_details(nieuw)
        with ThreadPoolExecutor(max_workers=5) as pool:
            nieuw = list(pool.map(score_ai, nieuw))

    alerts = 0
    for lst in nieuw:
        if not passes_filters(lst):
            log.info("Gefilterd: %s — €%s %sm² %sk", lst.title[:40], lst.price, lst.area_m2, lst.rooms)
            save_listing(conn, lst)
            continue
        if seed:
            save_listing(conn, lst)
            continue
        if send_alert(lst, bench):
            save_listing(conn, lst)
            alerts += 1
            log.info("ALERT: %s — €%s — %s", lst.title[:50], lst.price, lst.url)
        else:
            log.error("Niet opgeslagen (Telegram mislukt, retry volgende run): %s", lst.title[:40])

    for lst in all_listings:
        if listing_exists(conn, lst.id):
            save_listing(conn, lst)

    conn.close()
    log.info("=== Klaar: %d totaal, %d nieuw, %d alerts ===", len(all_listings), len(nieuw), alerts)


MAX_RETRIES = 3
BACKOFF = [10, 30, 60]


def main():
    log.info("=== Huurhuis-Alert gestart — %s, max €%s ===", CITY_LABEL, PRICE_MAX)
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            _run()
            return
        except Exception as exc:
            last_err = exc
            log.error("Poging %d/%d gefaald: %s", attempt, MAX_RETRIES, exc, exc_info=True)
            if attempt < MAX_RETRIES:
                time.sleep(BACKOFF[attempt - 1])
    log.error("ALLE pogingen gefaald")
    send_failure_alert(str(last_err), MAX_RETRIES)
    sys.exit(1)


if __name__ == "__main__":
    main()
