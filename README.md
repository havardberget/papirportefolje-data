# Papirportefølje – kursdata

Data til et fiktivt papirportefølje-eksperiment på Oslo Børs (læring, ingen ekte penger).

En GitHub Actions-jobb henter sluttkurser og utbytte fra [EODHD](https://eodhd.com) med eget API-abonnement (gratisnivå) og lagrer dem her:

- `data/prices.csv` – sluttkurser per børsdag (`source`: `eod` = sluttkurs-endepunkt, `rt` = dagens kurs fra 15 min forsinket endepunkt etter børsslutt)
- `data/dividends.csv` – utbytte med utbyttedato (ex-dato), omregnet til NOK
- `data/status.json` – siste kjøring, antall API-kall og eventuelle feil
- `data/usage.csv` – kontoens kallteller før og etter hver kjøring (for å se hvordan EODHD teller kall)

Hentes: børsdager etter stengetid (alle aksjer på fredager, eide aksjer resten av uken) og utbytte lørdag morgen. API-nøkkelen ligger som repository secret `EODHD_API_TOKEN`.
