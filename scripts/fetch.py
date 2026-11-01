#!/usr/bin/env python3
"""Henter sluttkurser og utbytte for papirporteføljen fra EODHD.

Modi:
  prices-daily  aksjene i config/daily.txt + OBX (man–tor)
  prices-full   alle aksjene i config/universe.csv (fredag)
  dividends     utbytte for aksjene i config/daily.txt (lørdag)
  all           prices-full + dividends (manuell kjøring / første gang)

Bare standardbiblioteket brukes. API-nøkkelen leses fra miljøvariabelen
EODHD_API_TOKEN og skrives aldri ut.
"""
import csv
import datetime as dt
import json
import os
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://eodhd.com/api"
START = "2026-10-30"
DIV_FROM = "2026-10-01"      # utbytte med ex-dato fra og med denne lagres
DIV_HISTORY = "2024-01-01"   # lengre historikk brukes bare til å flagge avvikende beløp
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = os.environ.get("EODHD_API_TOKEN", "")
MODES = {"prices-daily", "prices-full", "dividends", "all"}

calls = 0
errors = []
notes = []


class LimitReached(Exception):
    pass


def path(*parts):
    return os.path.join(ROOT, *parts)


def get(endpoint, **params):
    """Ett API-kall. Returnerer JSON eller None. Kaster LimitReached ved kvote brukt opp."""
    global calls
    params.update(api_token=TOKEN, fmt="json")
    url = f"{BASE}/{endpoint}?{urllib.parse.urlencode(params)}"
    calls += 1
    req = urllib.request.Request(url, headers={"User-Agent": "papirportefolje-data/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:160].replace(TOKEN, "***")
        errors.append(f"{endpoint}: HTTP {e.code} {body}")
        if e.code in (402, 429):
            raise LimitReached()
        return None
    except Exception as e:  # nettverksfeil o.l. – ikke skriv ut URL (inneholder nøkkel)
        errors.append(f"{endpoint}: {type(e).__name__}")
        return None
    try:
        return json.loads(text)
    except ValueError:
        errors.append(f"{endpoint}: ikke JSON: {text[:160]}")
        return None


def read_csv(name):
    with open(path("data", name), newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(name, fields, rows):
    with open(path("data", name), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def load_universe():
    with open(path("config", "universe.csv"), newline="", encoding="utf-8") as f:
        return {r["ticker"]: r["symbol"].split("|") for r in csv.DictReader(f)}


def load_daily():
    with open(path("config", "daily.txt"), encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip() and not l.startswith("#")]


def oslo_today():
    # Oslo er UTC+1 (vinter) / UTC+2 (sommer); jobben kjører ettermiddag UTC,
    # så UTC-datoen er alltid lik Oslo-datoen.
    return dt.datetime.now(dt.timezone.utc).date()


def prev_weekday(d):
    d -= dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


# ---------------------------------------------------------------- kurser
def update_prices(tickers, universe):
    rows = read_csv("prices.csv")
    have = {(r["date"], r["ticker"]) for r in rows}
    last = {}
    for r in rows:
        if r["date"] > last.get(r["ticker"], ""):
            last[r["ticker"]] = r["date"]
    today = oslo_today()
    today_s = today.isoformat()
    added = []

    def add(date, ticker, close, source):
        if (date, ticker) in have or date < START or date > today_s:
            return
        have.add((date, ticker))
        new = {"date": date, "ticker": ticker, "close": f"{close:g}", "source": source}
        rows.append(new)
        added.append(new)

    # 1) Fyll hull med sluttkurs-endepunktet (bare når en dag mangler)
    for t in tickers:
        lt = last.get(t)
        if lt and lt >= prev_weekday(today).isoformat():
            continue
        frm = START if not lt else (dt.date.fromisoformat(lt) + dt.timedelta(days=1)).isoformat()
        for sym in universe[t]:
            data = get(f"eod/{sym}", **{"from": frm, "to": today_s})
            if isinstance(data, list) and data:
                for d in data:
                    c = num(d.get("close"))
                    if c:
                        add(d["date"][:10], t, c, "eod")
                if len(universe[t]) > 1:
                    notes.append(f"{t} hentet som {sym}")
                break

    # 2) Dagens kurs fra sanntidsendepunktet (15 min forsinket), ett samlet kall
    missing = [t for t in tickers if (today_s, t) not in have and today.weekday() < 5]
    syms = {t: universe[t][0] for t in missing}
    if syms:
        first, *rest = list(syms.values())
        params = {"s": ",".join(rest)} if rest else {}
        data = get(f"real-time/{first}", **params)
        items = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
        by_sym = {i.get("code"): i for i in items if isinstance(i, dict)}
        for t, sym in syms.items():
            i = by_sym.get(sym)
            if not i:
                continue
            ts = i.get("timestamp")
            c = num(i.get("close"))
            if isinstance(ts, (int, float)) and c:
                d = dt.datetime.fromtimestamp(ts, dt.timezone.utc).date().isoformat()
                if d == today_s:
                    add(d, t, c, "rt")

    rows.sort(key=lambda r: (r["date"], r["ticker"]))
    write_csv("prices.csv", ["date", "ticker", "close", "source"], rows)
    return added


# ---------------------------------------------------------------- utbytte
def fx_rate(pair, date, cache):
    key = (pair, date)
    if key in cache:
        return cache[key]
    d = dt.date.fromisoformat(date)
    data = get(f"eod/{pair}.FOREX", **{"from": (d - dt.timedelta(days=7)).isoformat(), "to": date})
    rate = None
    if isinstance(data, list) and data:
        rate = num(data[-1].get("close"))
    cache[key] = rate
    return rate


def update_dividends(tickers, universe):
    old = read_csv("dividends.csv")
    keep = [r for r in old if r["ticker"] not in tickers]
    cache = {}
    new_rows = []
    for t in tickers:
        sym = universe[t][0]
        data = get(f"div/{sym}", **{"from": DIV_HISTORY})
        if not isinstance(data, list):
            keep += [r for r in old if r["ticker"] == t]  # behold gamle rader ved feil
            continue
        rows_t = []
        for d in data:
            v = num(d.get("value"))
            if not v:
                continue
            cur = (d.get("currency") or "NOK").upper()
            ex = (d.get("date") or "")[:10]
            if cur == "NOK":
                fx = 1.0
            elif ex >= DIV_FROM:
                fx = fx_rate(f"{cur}NOK", min(ex, oslo_today().isoformat()), cache)
            else:
                fx = None  # eldre utbytte i annen valuta trengs ikke omregnet
            rows_t.append({
                "ticker": t, "ex_date": ex, "pay_date": (d.get("paymentDate") or "")[:10],
                "declared": (d.get("declarationDate") or "")[:10], "value": f"{v:g}",
                "currency": cur, "fx": "" if fx is None else f"{fx:g}",
                "value_nok": "" if fx is None else f"{round(v * fx, 4):g}", "flag": "",
            })
        # Flagg beløp som avviker kraftig fra tidligere utbytte i samme valuta (f.eks. feil valuta)
        for r in rows_t:
            same = [float(x["value"]) for x in rows_t if x["currency"] == r["currency"]]
            med = statistics.median(same) if len(same) >= 3 else None
            v = float(r["value"])
            if not r["value_nok"]:
                r["flag"] = "mangler valutakurs"
            elif med and (v < med / 4 or v > med * 4):
                r["flag"] = "sjekk beløp/valuta"
        new_rows += [r for r in rows_t if r["ex_date"] >= DIV_FROM]
    rows = sorted(keep + new_rows, key=lambda r: (r["ex_date"], r["ticker"]))
    write_csv("dividends.csv", ["ticker", "ex_date", "pay_date", "declared", "value",
                                "currency", "fx", "value_nok", "flag"], rows)
    return new_rows


# ---------------------------------------------------------------- kvote
def account_usage():
    """Leser kontoens kallteller. Bare tall lagres – aldri navn eller e-post."""
    data = get("user")
    if not isinstance(data, dict):
        return None
    return {k: data.get(k) for k in ("apiRequests", "apiRequestsDate", "dailyRateLimit", "extraLimit")}


def counted(before, after):
    """Kall trukket denne kjøringen: økning i dagstelleren + nedgang i bonusen (extraLimit)."""
    try:
        return (int(after["apiRequests"]) - int(before["apiRequests"])) + \
               (int(before.get("extraLimit") or 0) - int(after.get("extraLimit") or 0))
    except (TypeError, ValueError, KeyError):
        return ""


def log_usage(mode, before, after, own_calls):
    fields = ["run_at_utc", "mode", "script_requests", "api_before", "api_after",
              "counted_this_run", "daily_limit", "extra_limit"]
    file = path("data", "usage.csv")
    rows = []
    if os.path.exists(file):
        with open(file, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    b = (before or {}).get("apiRequests")
    a = (after or {}).get("apiRequests")
    same_day = before and after and before.get("apiRequestsDate") == after.get("apiRequestsDate")
    rows.append({
        "run_at_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode": mode, "script_requests": own_calls,
        "api_before": "" if b is None else b, "api_after": "" if a is None else a,
        "counted_this_run": counted(before, after) if same_day else "",
        "daily_limit": (after or before or {}).get("dailyRateLimit", ""),
        "extra_limit": (after or before or {}).get("extraLimit", ""),
    })
    with open(file, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------- main
def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "prices-daily"
    if mode not in MODES:
        sys.exit(f"Ukjent modus: {mode}")
    if not TOKEN:
        sys.exit("EODHD_API_TOKEN mangler (legg den inn som repository secret).")
    universe = load_universe()
    daily = [t for t in load_daily() if t in universe]
    result = {}
    before = account_usage()
    start_calls = calls
    try:
        if mode in ("prices-daily", "prices-full", "all"):
            tickers = list(universe) if mode != "prices-daily" else daily + ["OBX"]
            # Eide aksjer og OBX først, i tilfelle kvoten tar slutt underveis
            tickers = sorted(set(tickers), key=lambda t: (t not in daily + ["OBX"], t))
            result["prices_added"] = len(update_prices(tickers, universe))
        if mode in ("dividends", "all"):
            result["dividend_rows"] = len(update_dividends(daily, universe))
    except LimitReached:
        errors.append("API-kvoten for i dag er brukt opp; resten hentes neste kjøring.")
    own = calls - start_calls
    after = account_usage()
    log_usage(mode, before, after, own)
    result["account"] = after
    status = {
        "run_at_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode": mode, "api_calls": calls, **result, "notes": notes, "errors": errors,
    }
    with open(path("data", "status.json"), "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(json.dumps(status, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
