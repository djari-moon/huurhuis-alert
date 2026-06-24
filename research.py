#!/usr/bin/env python3
"""
Huurhuis-Alert — research / discovery
====================================
Draait af en toe (niet elke 5 min). Twee doelen:

  1. €/m²-BENCHMARK voor Alkmaar — uit echt aanbod (beschikbaar + verhuurd)
     van de makelaarssites + Pararius + Funda. Schrijft data/prices.json,
     dat de live-scraper gebruikt om "goedkoop / gemiddeld / duur" te bepalen.

  2. MAKELAAR-RANGLIJST — wie verhuurt er het meest in Alkmaar. Schrijft
     data/makelaars_ranking.json zodat je weet welke makelaars het belangrijkst
     zijn om te monitoren.

Gebruik:
  python research.py            # scrape + schrijf data/*.json
  python research.py --dry-run  # alleen tonen, niets wegschrijven
"""

import os
import re
import sys
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from common import log, SEARCH_CRITERIA, scrape_do_fetch
from makelaars import MAKELAARS

CITY = SEARCH_CRITERIA["city"]
CITY_LABEL = SEARCH_CRITERIA["city_label"]
DRY_RUN = "--dry-run" in sys.argv

_AREA_RE = re.compile(r"(\d{2,4})\s*m[²2]")


def parse_price(text: str) -> int:
    if not text:
        return 0
    m = re.search(r"€\s*([\d.]{3,})", text) or re.search(r"([\d.]{3,})\s*,-", text)
    if m:
        try:
            return int(re.sub(r"[.\s]", "", m.group(1)))
        except ValueError:
            return 0
    for c in re.finditer(r"\b(\d[\d.]{2,4})\b", text):
        v = int(re.sub(r"[.\s]", "", c.group(1)))
        if 300 <= v <= 9999:
            return v
    return 0


def parse_area(text: str) -> int:
    m = _AREA_RE.search(text or "")
    return int(m.group(1)) if m else 0


def _base(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


# ── Datapunten verzamelen (prijs + m² uit zoekpagina-tekst) ───────────────────


def harvest_makelaar(cfg: dict) -> tuple:
    """Geeft (datapunten, listing_count) — incl. verhuurd (juist goed voor €/m²)."""
    html = scrape_do_fetch(cfg["url"], super_mode=True, retries=1, geo_code="nl",
                           render=cfg.get("render", False))
    if not html:
        return [], 0
    soup = BeautifulSoup(html, "html.parser")
    link_re = re.compile(cfg["link_re"], re.I)
    city = cfg.get("city_filter", "").lower()
    points, seen = [], set()
    for a in soup.find_all("a", href=True):
        if not link_re.search(a["href"]):
            continue
        full = urljoin(_base(cfg["url"]), a["href"])
        if full in seen:
            continue
        seen.add(full)
        text = a.get_text(" ", strip=True)
        if city and city not in (a["href"] + " " + text).lower():
            continue
        price, area = parse_price(text), parse_area(text)
        if price and area:
            points.append(price / area)
    return points, len(seen)


def harvest_pararius() -> tuple:
    base = "https://www.pararius.nl"
    url = f"{base}/huurwoningen/{CITY}"
    html = scrape_do_fetch(url, super_mode=True, retries=1, geo_code="nl")
    if not html:
        return [], 0
    soup = BeautifulSoup(html, "html.parser")
    items = soup.select("section.listing-search-item, li.search-list__item--listing")
    points = []
    for it in items:
        price = parse_price(_t(it, ".listing-search-item__price"))
        area = parse_area(_t(it, ".illustrated-features__item--surface-area"))
        if price and area:
            points.append(price / area)
    return points, len(items)


def harvest_funda() -> tuple:
    """Best-effort: Funda Alkmaar huur (beschikbaar + verhuurd). Selectors
    tunen op debug_funda_research.html na de eerste run."""
    points, count = [], 0
    for avail in ("", "&availability=%5B%22unavailable%22%5D"):
        url = f"https://www.funda.nl/zoeken/huur?selected_area=%5B%22{CITY}%22%5D{avail}"
        html = scrape_do_fetch(url, super_mode=True, retries=2, geo_code="nl",
                               render=True, render_wait=4000)
        if not html:
            continue
        try:
            with open("debug_funda_research.html", "w", encoding="utf-8") as fh:
                fh.write(html[:600_000])
        except OSError:
            pass
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(" ", strip=True)
        # ruwe paren prijs+m² uit de hele pagina (tunen indien nodig)
        for chunk in re.findall(r"€\s*[\d.]{3,}[^€]{0,60}?\d{2,4}\s*m[²2]", text):
            p, a = parse_price(chunk), parse_area(chunk)
            if p and a:
                points.append(p / a)
                count += 1
    return points, count


def _t(el, sel):
    f = el.select_one(sel)
    return f.get_text(strip=True) if f else ""


# ── Statistiek ─────────────────────────────────────────────────────────────────


def percentile(values: list, pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def main():
    log.info("=== Huurhuis research gestart — %s ===", CITY_LABEL)

    sources = [("pararius", harvest_pararius), ("funda", harvest_funda)]
    for cfg in MAKELAARS:
        sources.append((cfg["key"], lambda c=cfg: harvest_makelaar(c)))

    all_points, ranking = [], {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(fn): name for name, fn in sources}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                pts, cnt = fut.result()
                all_points.extend(pts)
                ranking[name] = cnt
                log.info("%-16s → %d woningen, %d €/m²-datapunten", name, cnt, len(pts))
            except Exception as e:
                log.error("%s fout: %s", name, e)
                ranking[name] = 0

    # filter uitschieters (parkeerplaatsen, opslag): €5–€60 /m²
    clean = [p for p in all_points if 5 <= p <= 60]
    log.info("€/m²-datapunten: %d totaal, %d na opschoning", len(all_points), len(clean))

    prices = {
        "city": CITY_LABEL,
        "sample_size": len(clean),
        "p25_per_m2": round(percentile(clean, 0.25), 1),
        "median_per_m2": round(percentile(clean, 0.50), 1),
        "p75_per_m2": round(percentile(clean, 0.75), 1),
    }
    ranking_sorted = dict(sorted(ranking.items(), key=lambda kv: kv[1], reverse=True))

    log.info("Benchmark: p25=%(p25_per_m2)s  mediaan=%(median_per_m2)s  p75=%(p75_per_m2)s €/m²", prices)
    log.info("Makelaar-ranglijst (op aantal): %s", ranking_sorted)

    if DRY_RUN:
        log.info("[DRY-RUN] niets weggeschreven")
        return

    os.makedirs("data", exist_ok=True)
    with open("data/prices.json", "w", encoding="utf-8") as fh:
        json.dump(prices, fh, indent=2, ensure_ascii=False)
    with open("data/makelaars_ranking.json", "w", encoding="utf-8") as fh:
        json.dump(ranking_sorted, fh, indent=2, ensure_ascii=False)
    log.info("Geschreven: data/prices.json + data/makelaars_ranking.json")


if __name__ == "__main__":
    main()
