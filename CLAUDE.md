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

## Bronnen & status (laatste validatie)

| Bron | Werkt | Aanpak |
|------|-------|--------|
| 123Wonen, 072Wonen | ✅ | server-rendered, eigen platform |
| Pinedo, Van der Borden, Leygraaf | ✅ | server-rendered (Realworks/WP) |
| Kloes & Goudsblom | ✅ | werkt; vaak geen Alkmaar-aanbod (regio Castricum) |
| Verhuurmakelaar Bas | ✅ | multi-stad, city_filter op ankertekst, verhuurd-filter |
| Pararius | ✅ | groot huuraanbod, makkelijk |
| **ikwilhuren (MVGM)** | ⚠️ | paginatie/sort tunen — stad-filter werkt niet via URL |
| **Vos** | ⚠️ | JS-widget (al4), render=true — selector tunen na 1e render-fetch |
| **Funda** | ⚠️ | Akamai → super+render; JSON-LD eerst, anders DOM — tunen op debug |

Geen enkele makelaarssite heeft anti-bot; gewone fetch werkt. Funda is de enige
met zware bescherming (Scrape.do `super=true`).

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

1. **ikwilhuren** — juiste Alkmaar-URL/paginatie vinden (client-side filter).
2. **Vos** — selector bepalen op de gerenderde HTML (`debug_vos.html`).
3. **Funda** — JSON-LD/DOM-selectors valideren op `debug_funda.html`.
4. **cron-job.org** instellen op de `alert.yml` workflow_dispatch endpoint.
5. €/m²-benchmark verschijnt automatisch in `data/prices.json` na de 1e research-run.

## Regels

- Geen secrets committen · geen `git push --force` · geen push naar `main` zonder "ja"
- Geen login-scrapers (Facebook etc.) — alleen publiek aanbod
- Simpel > clever. Geen feature creep bij bug-fixes.
