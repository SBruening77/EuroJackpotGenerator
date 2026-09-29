#!/usr/bin/env python3
"""Eurojackpot-Tipp: Ziehungen laden und einen nachvollziehbaren Tipp erzeugen."""

from __future__ import annotations

import html
import json
import random
import re
import ssl
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DATA = ROOT / "data" / "draws.json"
PRICE_FILE = ROOT / "data" / "field-price.json"
CA_BUNDLE = ROOT / "data" / "windows-roots.pem"
PRICE_PAGE = "https://www.westlotto.de/service/hilfe-faqs/hilfe_faq.html"
HOST = "127.0.0.1"
PORT = 8765
RULE_START = date(2022, 3, 25)
ODDS = 139_838_160
JACKPOT_CAP = 120_000_000
SLIP_FIELDS = 9
PAUSE_SECONDS = 0.05
SOURCE = "https://www.eurojackpot.com/wlinfo/WL_InfoService"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

METHODS = {
    "frequency": "Häufig",
    "overdue": "Lange nicht gezogen",
    "random": "Zufall",
}
METHOD_NOTES = {
    "frequency": (
        "Die fünf Zahlen und zwei Eurozahlen, die seit dem 25.03.2022 "
        "am häufigsten gezogen wurden."
    ),
    "overdue": (
        "Die fünf Zahlen und zwei Eurozahlen mit der längsten Pause "
        "seit der letzten Ziehung."
    ),
    "random": (
        "Gleichverteilte Zufallskombination. Dieselbe Chance wie jeder "
        "andere gültige Tipp."
    ),
}

_rng = random.SystemRandom()
_state_lock = threading.Lock()
_ssl_lock = threading.Lock()
_ssl_context = None
_price_lock = threading.Lock()
_price_error = None
_state = {
    "refreshing": False,
    "done": 0,
    "total": 0,
    "currentDate": None,
    "error": None,
    "message": None,
}


def _empty_cache():
    return {"draws": [], "emptyDates": [], "updatedAt": None}


def load_cache():
    if not DATA.exists():
        return _empty_cache()
    try:
        with DATA.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return _empty_cache()
    if not isinstance(payload, dict):
        return _empty_cache()
    payload.setdefault("draws", [])
    payload.setdefault("emptyDates", [])
    payload.setdefault("updatedAt", None)
    return payload


def save_cache(cache):
    DATA.parent.mkdir(parents=True, exist_ok=True)
    cache["updatedAt"] = datetime.now().isoformat(timespec="seconds")
    temporary = DATA.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(DATA)


def draw_dates(start, end):
    current = start
    while current <= end:
        if current.weekday() in (1, 4):
            yield current
        current += timedelta(days=1)


def _write_windows_roots(target: Path):
    """Python aus GnuCOBOL hat kein CA-Bundle. Die Windows-Stammzertifikate füllen die Lücke."""
    target.parent.mkdir(parents=True, exist_ok=True)
    script = (
        "$certs = Get-ChildItem -Path Cert:\\LocalMachine\\Root; "
        "$chunks = foreach ($cert in $certs) { "
        "'-----BEGIN CERTIFICATE-----'; "
        "[Convert]::ToBase64String($cert.RawData, 'InsertLineBreaks'); "
        "'-----END CERTIFICATE-----' }; "
        "$chunks -join \"`n\""
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    text = completed.stdout.strip()
    if "BEGIN CERTIFICATE" not in text:
        raise RuntimeError("Windows-Zertifikate konnten nicht gelesen werden.")
    target.write_text(text + "\n", encoding="ascii", errors="ignore")


def ssl_context():
    global _ssl_context
    with _ssl_lock:
        if _ssl_context is not None:
            return _ssl_context
        cafile = ssl.get_default_verify_paths().openssl_cafile
        if cafile and Path(cafile).is_file():
            _ssl_context = ssl.create_default_context()
        else:
            if not CA_BUNDLE.is_file() or CA_BUNDLE.stat().st_size < 1000:
                _write_windows_roots(CA_BUNDLE)
            _ssl_context = ssl.create_default_context(cafile=str(CA_BUNDLE))
        return _ssl_context


def fetch_payload(draw_day: date):
    query = urlencode(
        {
            "client": "jsn",
            "gruppe": "ZahlenUndQuoten",
            "ewGewsum": "ja",
            "historie": "ja",
            "spielart": "EJ",
            "adg": "ja",
            "lang": "de",
            "datum": draw_day.isoformat(),
        }
    )
    request = Request(
        f"{SOURCE}?{query}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urlopen(request, timeout=25, context=ssl_context()) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8"))


def _plain_page(raw_html: str) -> str:
    text = re.sub(r"(?is)<script\b.*?</script>", " ", raw_html)
    text = re.sub(r"(?is)<style\b.*?</style>", " ", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text)


def _parse_euro_amount(raw: str) -> float:
    if "," in raw:
        return float(raw.replace(".", "").replace(",", "."))
    return float(raw)


def load_field_price():
    if not PRICE_FILE.is_file():
        return None
    try:
        with PRICE_FILE.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    amount = payload.get("amount")
    if not isinstance(amount, (int, float)) or amount < 0:
        return None
    if not isinstance(payload.get("fee"), (int, float)):
        return None
    if payload.get("slipFields") != SLIP_FIELDS:
        return None
    return payload


def fetch_field_price():
    """Gesamtpreis in NRW: Einsatz je Tipp plus Bearbeitungsgebühr für eine Ziehung."""
    request = Request(
        PRICE_PAGE,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
    )
    with urlopen(request, timeout=25, context=ssl_context()) as response:
        raw_html = response.read().decode("utf-8", errors="replace")
    text = _plain_page(raw_html)
    stake_match = re.search(
        r"Eurojackpot\s+Der Einsatz beträgt\s+(\d{1,2},\d{2})\s+Euro je Tipp",
        text,
        re.IGNORECASE,
    )
    if stake_match is None:
        raise ValueError("Bei WestLotto steht kein Eurojackpot-Einsatz.")
    fee_match = re.search(
        r"Normal\s+1 Woche\s+(\d{1,2},\d{2})",
        text[stake_match.end(): stake_match.end() + 600],
        re.IGNORECASE,
    )
    if fee_match is None:
        raise ValueError("Bei WestLotto steht keine Bearbeitungsgebühr für eine Ziehung.")
    stake = _parse_euro_amount(stake_match.group(1))
    fee = _parse_euro_amount(fee_match.group(1))
    payload = {
        "amount": round(stake * SLIP_FIELDS + fee, 2),
        "stake": stake,
        "fee": fee,
        "slipFields": SLIP_FIELDS,
        "region": "NRW",
        "currency": "EUR",
        "source": PRICE_PAGE,
        "fetchedAt": datetime.now().isoformat(timespec="seconds"),
    }
    PRICE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = PRICE_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(PRICE_FILE)
    return payload


def field_price_for_status(refresh=False):
    global _price_error
    with _price_lock:
        if refresh or load_field_price() is None:
            try:
                fetch_field_price()
                _price_error = None
            except (HTTPError, URLError, TimeoutError, ValueError, OSError) as error:
                if load_field_price() is None:
                    _price_error = f"Der Preis pro Feld konnte nicht ermittelt werden: {error}"
        return load_field_price(), _price_error


def _load_draw(draw_day: date):
    last_error = None
    for _attempt in range(2):
        try:
            time.sleep(PAUSE_SECONDS)
            return parse_draw(fetch_payload(draw_day), draw_day)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, ValueError) as error:
            last_error = error
    return "fail", f"Abruf für {draw_day.isoformat()} fehlgeschlagen: {last_error}"


def _class_one(rows):
    for row in rows or []:
        if row.get("klasse") == 1:
            return row
    return None


def parse_draw(payload, requested: date):
    """Return ('ok', draw), ('empty', None) or ('fail', message)."""
    if not isinstance(payload, dict):
        return "fail", "Die Quelle hat eine Fehlermeldung geliefert."
    error = payload.get("error")
    if error:
        if "keine Daten" in str(error):
            return "empty", None
        return "fail", "Die Quelle hat eine Fehlermeldung geliefert."
    head = payload.get("head") or {}
    if head.get("fehlenDaten"):
        return "empty", None

    main = []
    euro = []
    ziehungen = ((payload.get("zahlen") or {}).get("hauptlotterie") or {}).get(
        "ziehungen"
    ) or []
    for row in ziehungen:
        label = str(row.get("bezeichnung") or "")
        numbers = [int(value) for value in (row.get("zahlenSortiert") or [])]
        if label.startswith("5 aus"):
            main = numbers
        elif label.startswith("2 aus"):
            euro = numbers

    if len(main) != 5 or len(euro) != 2:
        return "empty", None
    if any(number < 1 or number > 50 for number in main):
        return "fail", f"Ungültige Zahlen am {requested.isoformat()}."
    if any(number < 1 or number > 12 for number in euro):
        return "fail", f"Ungültige Eurozahlen am {requested.isoformat()}."

    jackpot = None
    next_jackpot = None
    odds = None
    next_date = (head.get("folgeZiehung") or {}).get("datum")
    quote_rows = (
        ((payload.get("auswertung") or {}).get("quoten") or {}).get("hauptlotterie")
        or {}
    ).get("ziehungen") or []
    for block in quote_rows:
        drawn = _class_one(block.get("gewinnklassen"))
        if drawn is not None and jackpot is None:
            jackpot = drawn.get("jackpot")
        expected = block.get("erwarteteGewinnsummen") or {}
        if expected.get("folgeziehung"):
            next_date = expected.get("folgeziehung")
        upcoming = _class_one(expected.get("gewinnklassen"))
        if upcoming is not None:
            if next_jackpot is None:
                next_jackpot = upcoming.get("jackpot")
            if odds is None and upcoming.get("chance"):
                odds = upcoming.get("chance")

    return "ok", {
        "date": requested.isoformat(),
        "main": sorted(main),
        "euro": sorted(euro),
        "jackpot": jackpot,
        "nextJackpot": next_jackpot,
        "nextDrawDate": next_date,
        "odds": int(odds) if isinstance(odds, (int, float)) and odds else ODDS,
    }


def missing_dates(cache, today: date):
    known = {item["date"] for item in cache["draws"]}
    known.update(cache.get("emptyDates") or [])
    return [
        draw_day
        for draw_day in draw_dates(RULE_START, today)
        if draw_day.isoformat() not in known
    ]


def _set_state(**updates):
    with _state_lock:
        _state.update(updates)


def public_progress():
    with _state_lock:
        return {
            "refreshing": _state["refreshing"],
            "progress": {
                "done": _state["done"],
                "total": _state["total"],
                "currentDate": _state["currentDate"],
            },
            "error": _state["error"],
            "message": _state["message"],
        }


def refresh_worker():
    added = 0
    failed = 0
    try:
        field_price_for_status(refresh=True)
        cache = load_cache()
        today = date.today()
        pending = missing_dates(cache, today)
        _set_state(done=0, total=len(pending), currentDate=None, error=None, message=None)
        if pending:
            workers = min(6, len(pending))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(_load_draw, draw_day): draw_day for draw_day in pending
                }
                for index, future in enumerate(as_completed(futures), start=1):
                    draw_day = futures[future]
                    try:
                        status, draw = future.result()
                    except Exception as error:
                        status, draw = (
                            "fail",
                            f"Abruf für {draw_day.isoformat()} fehlgeschlagen: {error}",
                        )
                    if status == "ok":
                        cache["draws"].append(draw)
                        added += 1
                    elif status == "empty" and draw_day < today:
                        cache["emptyDates"].append(draw_day.isoformat())
                    elif status == "fail":
                        failed += 1
                    if added and added % 25 == 0:
                        cache["draws"].sort(key=lambda item: item["date"])
                        save_cache(cache)
                    _set_state(done=index, currentDate=draw_day.isoformat())
                    if index == 1 or index % 25 == 0 or index == len(pending):
                        print(f"{index}/{len(pending)} {draw_day.isoformat()}", flush=True)

        cache["draws"].sort(key=lambda item: item["date"])
        cache["emptyDates"] = sorted(set(cache["emptyDates"]))
        save_cache(cache)
        if failed:
            _set_state(
                error=(
                    f"{failed} Ziehungen konnten nicht geladen werden. "
                    "Ein erneutes Aktualisieren versucht sie noch einmal."
                )
            )
        if added:
            message = f"{added} Ziehungen aktualisiert."
        elif not failed:
            message = "Keine neuen Ziehungen."
        else:
            message = "Aktualisierung unvollständig."
        _set_state(message=message, currentDate=None)
        print(message, flush=True)
    except Exception:
        traceback.print_exc()
        _set_state(error="Die Aktualisierung wurde abgebrochen.")
    finally:
        _set_state(refreshing=False)


def start_refresh():
    with _state_lock:
        if _state["refreshing"]:
            return False
        _state["refreshing"] = True
        _state["error"] = None
        _state["message"] = None
    threading.Thread(target=refresh_worker, daemon=True).start()
    return True


def analyze(draws):
    ordered = sorted(draws, key=lambda item: item["date"])
    total = len(ordered)
    main_count = {number: 0 for number in range(1, 51)}
    euro_count = {number: 0 for number in range(1, 13)}
    main_last = {}
    euro_last = {}
    for index, draw in enumerate(ordered):
        for number in draw["main"]:
            main_count[number] += 1
            main_last[number] = index
        for number in draw["euro"]:
            euro_count[number] += 1
            euro_last[number] = index

    def rows(counts, last_seen, limit):
        items = []
        for number in range(1, limit + 1):
            seen = last_seen.get(number)
            if seen is None:
                gap = total
                last_date = None
            else:
                gap = total - 1 - seen
                last_date = ordered[seen]["date"]
            items.append(
                {
                    "number": number,
                    "count": counts[number],
                    "gap": gap,
                    "lastSeen": last_date,
                }
            )
        return items

    return rows(main_count, main_last, 50), rows(euro_count, euro_last, 12)


def pick_top(rows, count, field):
    ranked = sorted(rows, key=lambda row: (-row[field], row["number"]))
    return sorted(row["number"] for row in ranked[:count])


def latest_draw(draws):
    if not draws:
        return None
    return max(draws, key=lambda item: item["date"])


def build_status():
    cache = load_cache()
    draws = cache["draws"]
    latest = latest_draw(draws)
    main_rows, euro_rows = analyze(draws) if draws else ([], [])
    recent = list(reversed(draws[-12:]))
    field_price, field_price_error = field_price_for_status()
    status = {
        "drawCount": len(draws),
        "fieldPrice": None if field_price is None else field_price.get("amount"),
        "fieldStake": None if field_price is None else field_price.get("stake"),
        "fieldFee": None if field_price is None else field_price.get("fee"),
        "slipFields": SLIP_FIELDS if field_price is None else field_price.get("slipFields") or SLIP_FIELDS,
        "fieldPriceSource": None if field_price is None else field_price.get("source"),
        "fieldPriceError": field_price_error,
        "ruleStart": RULE_START.isoformat(),
        "odds": (latest or {}).get("odds") or ODDS,
        "jackpotCap": JACKPOT_CAP,
        "latest": latest,
        "nextJackpot": None if latest is None else latest.get("nextJackpot"),
        "nextDrawDate": None if latest is None else latest.get("nextDrawDate"),
        "frequencies": {"main": main_rows, "euro": euro_rows},
        "recent": recent,
        "updatedAt": cache.get("updatedAt"),
    }
    status.update(public_progress())
    return status


def _ranked(rows, field):
    ordered = sorted(rows, key=lambda row: (-row[field], row["number"]))
    return [row["number"] for row in ordered]


def _slip_from_ranking(main_rows, euro_rows, field):
    main_order = _ranked(main_rows, field)
    euro_order = _ranked(euro_rows, field)
    fields = []
    for index in range(SLIP_FIELDS):
        main = sorted(main_order[index * 5:(index + 1) * 5])
        first = euro_order[(index * 2) % len(euro_order)]
        second = euro_order[(index * 2 + 1) % len(euro_order)]
        if first == second:
            second = euro_order[(index * 2 + 2) % len(euro_order)]
        fields.append({"main": main, "euro": sorted((first, second))})
    return fields


def _random_slip():
    seen = set()
    fields = []
    while len(fields) < SLIP_FIELDS:
        main = tuple(sorted(_rng.sample(range(1, 51), 5)))
        euro = tuple(sorted(_rng.sample(range(1, 13), 2)))
        if (main, euro) in seen:
            continue
        seen.add((main, euro))
        fields.append({"main": list(main), "euro": list(euro)})
    return fields


def build_suggestion(method):
    if method not in METHODS:
        raise ValueError(method)
    cache = load_cache()
    draws = cache["draws"]
    if not draws:
        return None
    main_rows, euro_rows = analyze(draws)
    main_by = {row["number"]: row for row in main_rows}
    euro_by = {row["number"]: row for row in euro_rows}
    if method == "frequency":
        fields = _slip_from_ranking(main_rows, euro_rows, "count")
    elif method == "overdue":
        fields = _slip_from_ranking(main_rows, euro_rows, "gap")
    else:
        fields = _random_slip()
    first = fields[0]
    latest = latest_draw(draws)
    return {
        "method": method,
        "methodLabel": METHODS[method],
        "main": first["main"],
        "euro": first["euro"],
        "fields": fields,
        "mainDetails": [main_by[number] for number in first["main"]],
        "euroDetails": [euro_by[number] for number in first["euro"]],
        "note": METHOD_NOTES[method],
        "odds": latest.get("odds") or ODDS,
        "nextJackpot": latest.get("nextJackpot"),
        "nextDrawDate": latest.get("nextDrawDate"),
        "basedOnDraws": len(draws),
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC), **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if path == "/api/status":
            self._json(build_status())
            return
        if path == "/api/suggestion":
            self._suggestion()
            return
        if path in ("/", ""):
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/refresh":
            content_length = int(self.headers.get("Content-Length") or 0)
            if content_length:
                self.rfile.read(content_length)
            started = start_refresh()
            payload = {"started": started}
            payload.update(public_progress())
            self._json(payload, 202 if started else 200)
            return
        if path == "/api/suggestion":
            self._suggestion()
            return
        self.send_error(404)

    def _suggestion(self):
        method = (parse_qs(urlparse(self.path).query).get("method") or ["frequency"])[0]
        try:
            payload = build_suggestion(method)
        except ValueError:
            self._json({"error": "Unbekanntes Verfahren."}, 400)
            return
        if payload is None:
            self._json(
                {"error": "Noch keine Ziehungen geladen. Bitte zuerst aktualisieren."},
                409,
            )
            return
        self._json(payload)

    def log_message(self, fmt, *args):
        message = fmt % args
        if "/api/status" in message:
            return
        sys.stderr.write("%s - %s\n" % (self.log_date_time_string(), message))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    STATIC.mkdir(parents=True, exist_ok=True)
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Eurojackpot-Tipp: http://{HOST}:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Beendet.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
