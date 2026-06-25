# CLAUDE.md — Huurhuis-Alert

> Lees dit eerst. Volledige briefing voor een nieuwe sessie.

## Wat is dit?

Een scraper die elke ~5 min Alkmaarse **huurwoningen** zoekt op makelaarssites +
Pararius + Funda, filtert op criteria, en een **Telegram-alert** stuurt bij een
nieuwe match. Hoofdgetal in de alert = **€/m²** (goedkoop / gemiddeld / duur t.o.v.
de Alkmaar-benchmark). Eigenaar: Djari.

**Waarom makelaarssites direct?** Woningen verschijnen vaak eerst op de eigen site
van de makelaar en pas later op Funda/Pararius. Daar zit de snelheidswinst.

## Twee scripts

| Script | Wanneer | Doel |
|--------|---------|------|
| `scraper.py` | elke ~5 min (cron-job.org → Actions) | live alerts |
| `research.py` | wekelijks (+ handmatig) | €/m²-benchmark + makelaar-ranglijst |

`common.py` = gedeelde basis (config, Scrape.do, SQLite, Telegram).
`makelaars.py` = de makelaar-configs (selectors per site).

## Zoekcriteria (in `common.py` → SEARCH_CRITERIA)

- Stad: **Alkmaar** · Max huur: **€1.500** · Min **45 m²** (50+ = "ruim") · Min **2 kamers**
- Buitenruimte (balkon/tuin) + gemeubileerd = bonus in de alert, geen harde filter
- Verhuurde woningen worden NIET gealert (wel meegenomen in de €/m²-research)

## Bronnen & status (live gevalideerd, juni 2026)

| Bron | Werkt | Scrape.do-modus | Aanpak |
|------|-------|-----------------|--------|
| 123Wonen (12) | ✅ | normaal (1 cr) | eigen platform |
| 072Wonen (7) | ✅ | normaal | eigen platform |
| Van der Borden (4) | ✅ | normaal | Realworks/WP |
| Pinedo (4) | ✅ | **super+render** | DDoS-Guard "One moment"-challenge |
| Leygraaf (3) | ✅ | **super** | zonder super soms geblokkeerd (flaky) |
| Pararius (12) | ✅ | **super** | Cloudflare → 502 zonder super |
| Funda (8) | ✅ | **super+render** | Akamai; JSON-LD eerst, anders DOM |
| Kloes & Goudsblom | ➖ | normaal | werkt; meestal geen Alkmaar (regio Castricum) |
| Verhuurmakelaar Bas | ➖ | normaal | werkt; nu alleen verhuurd-aanbod (correct gefilterd) |
| **ikwilhuren (MVGM)** | ⚠️ | normaal | geen Alkmaar-stock nu; stad-filter is client-side JS |
| **Vos** | ⚠️ | render | al4-widget haalt data uit aparte JS-API — niet opgelost |

**Credit-strategie:** super-mode (~10 cr) is duur en alleen nodig bij anti-bot.
Default = normale fetch (1 cr). Super staat AAN voor: Pararius, Funda, Pinedo,
Leygraaf (zie `super`/`render` per makelaar in `makelaars.py`, en `_FETCH_MODE`
in `scraper.py` voor de detailpagina's).

## Hoe de makelaar-scraper werkt (`scrape_makelaar`)

Link-mode: pak alle `<a>` waarvan de href `link_re` matcht, dedupe op URL.
`city_filter` houdt alleen woningen waar de stad in href óf ankertekst staat.
"verhuurd" wordt overgeslagen. Prijs/m² komen uit de ankertekst als die er staan,
anders vult Claude Haiku ze uit de detailpagina (samen met balkon/tuin/staat/etc).

**Nieuwe makelaar toevoegen:** entry in `makelaars.py` met `key`, `name`, `url`,
`link_re`, `city_filter`, `render`. Valideer op `debug_<key>.html` (Actions-artifact).

## Secrets (GitHub repo Secrets, NOOIT in git)

| Secret | Doel |
|--------|------|
| `SCRAPE_DO_TOKEN` | Scrape.do — scraping + anti-bot |
| `ANTHROPIC_API_KEY` | Claude Haiku — woningkenmerken |
| `TELEGRAM_BOT_TOKEN` | Telegram bot (zonder = DRY_RUN) |
| `TELEGRAM_CHAT_ID` | Doel-chat van de alerts |

## Hosting

- **GitHub Actions** (public repo → gratis onbeperkt), getriggerd door
  **cron-job.org** elke 5 min via `workflow_dispatch`.
- SQLite (`listings.db`) leeft in de Actions-cache (dedup tussen runs).
- Eerste run = **seed mode**: alles opslaan zonder te spammen.

## Lokaal draaien

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # vul tokens in
export $(grep -v '^#' .env | xargs)

python scraper.py --dry-run --force            # alles, geen Telegram
python scraper.py --dry-run --force --only=pararius   # één bron
python research.py --dry-run                   # benchmark berekenen, niets schrijven
```

## Logs (Actions)

```bash
gh run list --workflow=alert.yml --limit 10
gh run view <id> --log
```

## Backlog / tunen

1. **Vos** — al4-widget laadt uit een aparte JS-API (de `data-url` endpoints geven
   ook alleen de shell). Vergt reverse-engineeren van de Realworks-feed. Lage prio:
   Pararius/Funda pakken vos-aanbod tóch op. Best-effort 0 voor nu.
2. **ikwilhuren** — heeft zelden Alkmaar-aanbod; filter is client-side JS. Config
   vangt 't zodra een Alkmaar-listing op de aanbodpagina verschijnt. Niet kritisch.
3. **€/m²-benchmark** — vult zich automatisch in `data/prices.json` na de 1e
   research-run (maandag, of handmatig `research.yml` dispatchen).
4. **Leygraaf** in de gaten houden — op super gezet; als 't stabiel is kan 't terug
   naar normaal voor credits.

## Status: LIVE ✅

Draait elke 5 min via cron-job.org → `alert.yml`. Secrets staan. Eerste seed-run
gedaan. 7 bronnen leveren ~41 woningen; alerts gaan naar de Telegram-groep.

## Regels

- Geen secrets committen · geen `git push --force` · geen push naar `main` zonder "ja"
- Geen login-scrapers (Facebook etc.) — alleen publiek aanbod
- Simpel > clever. Geen feature creep bij bug-fixes.
