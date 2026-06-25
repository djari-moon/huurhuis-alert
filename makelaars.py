#!/usr/bin/env python3
"""
Makelaar-configs voor Alkmaar
=============================
Per makelaar: hoe we hun aanbodpagina uitlezen. Bepaald door de echte HTML
te inspecteren (zie debug_<key>.html bij twijfel).

Velden:
  key          — uniek id, ook de `source` in de DB en debug_<key>.html
  name         — weergavenaam in de Telegram-alert
  url          — aanbodpagina (huur, gefilterd op Alkmaar waar mogelijk)
  link_re      — regex: een href is een woning-detail als deze matcht
  city_filter  — alleen woningen meenemen als dit (lowercase) in href OF
                 ankertekst voorkomt. Leeg = geen filter (site is al Alkmaar).
  super        — Scrape.do super-mode (residential proxy, ~10 credits). Alleen
                 nodig bij anti-bot (Pinedo=DDoS-Guard). Default False = 1 credit.
  render       — True als de woningen via JavaScript geladen worden (headless
                 Chromium, meer credits). Anders gewone fetch.

Bijna alle sites tonen het aanbod server-side (link-mode): we pakken de
anchors die `link_re` matchen, dedupen op URL, en de AI haalt m²/kamers/etc.
uit de detailpagina. Prijs + m² proberen we ook uit de ankertekst te halen.
"""

MAKELAARS = [
    {
        "key": "072wonen",
        "name": "072Wonen",
        "url": "https://www.072wonen.nl/woningaanbod/huur/alkmaar?locationofinterest=Alkmaar",
        "link_re": r"/woningaanbod/huur/alkmaar/[^/?]+",
        "city_filter": "",          # URL is al op Alkmaar gefilterd
        "render": False,
    },
    {
        "key": "123wonen",
        "name": "123Wonen",
        "url": "https://www.123wonen.nl/huurwoningen/in/alkmaar/van/alkmaar",
        "link_re": r"/huur/alkmaar/[^/]+/[^/]+",
        "city_filter": "",
        "render": False,
    },
    {
        "key": "ikwilhuren",
        "name": "ikwilhuren.nu (MVGM)",
        "url": "https://ikwilhuren.nu/aanbod/",
        "link_re": r"/object/[^/]+",
        "city_filter": "alkmaar",   # toont heel NL → filter op Alkmaar (stad in href)
        "pages": 5,                 # zoekform is POST+CSRF → eerste 5 pagina's doorlopen
        "render": False,
    },
    {
        "key": "pinedo",
        "name": "Pinedo Makelaardij",
        "url": "https://pinedo.nl/huurwoningen/",
        "link_re": r"/woningaanbod/[a-z0-9][a-z0-9-]+/",
        "city_filter": "alkmaar",   # toont ook Haarlem etc.
        "super": True,              # DDoS-Guard "One moment" challenge
        "render": True,
    },
    {
        "key": "vanderborden",
        "name": "Van der Borden",
        "url": "https://www.vanderborden.nl/aanbod/huur/",
        "link_re": r"/aanbod/huur/[a-z0-9][a-z0-9-]+/",
        "city_filter": "",          # Alkmaar-regio makelaar
        "render": False,
    },
    {
        "key": "kloesen",
        "name": "Kloes & Goudsblom",
        "url": "https://www.kloesengoudsblom.nl/aanbod/woningaanbod/beschikbaar/huur/",
        "link_re": r"huis-\d+|appartement-\d+|woonhuis-\d+",
        "city_filter": "alkmaar",   # regio Castricum/Egmond/Schagen → filter
        "render": False,
    },
    {
        "key": "leygraaf",
        "name": "Leygraaf Makelaars",
        "url": "https://leygraafmakelaars.nl/huurwoningen/",
        "link_re": r"/woningen/[a-z0-9-]+-\d{4}",
        "city_filter": "alkmaar",
        "super": True,              # zonder super soms geblokkeerd (flaky 3↔0)
        "render": False,
    },
    {
        "key": "vos",
        "name": "Vos Makelaardij",
        "url": "https://www.vosmakelaardij.nl/aanbod/woningaanbod/huur/aantal-50/",
        "link_re": r"huis-\d+|appartement-\d+|woonhuis-\d+",
        "city_filter": "alkmaar",
        "render": True,             # al4-widget → JS-geladen
        "render_wait": 9000,        # widget-XHR tijd geven
        "block_resources": False,   # alle JS/resources laden
        "wait_selector": "a[href*='huis-'], a[href*='appartement-']",
    },
    {
        "key": "verhuurmakelaarbas",
        "name": "Verhuurmakelaar Bas",
        "url": "https://verhuurmakelaarbas.nl/huurwoningen/",
        "link_re": r"/object/[a-z0-9-]+",
        "city_filter": "alkmaar",   # multi-stad → filter op ankertekst
        "render": False,
    },
]
