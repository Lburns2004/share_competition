#!/usr/bin/env python3
"""
Share-price showdown updater (no API keys).

Every run (GitHub Actions, every ~5 min on weekdays):
  1. pulls live quotes + 5-min intraday bars (Yahoo Finance chart API) and FX rates
  2. converts everything to GBP
  3. after 21:30 London (or the next morning if a run was missed), "finalises" the day:
     refreshes full price history, awards points (append-only), recomputes baselines
  4. writes data/*.json and builds ./_site for GitHub Pages

Yahoo blocks plain Python HTTP clients (HTTP 429 "Too Many Requests") by TLS fingerprint, so
requests go through `curl_cffi` (browser impersonation) when it is installed, with polite
pacing, retries with back-off, and an "idle" mode that makes no requests when markets are shut.

Outputs to $GITHUB_OUTPUT:  deploy=true|false   committed=true|false
"""
import bisect, json, os, random, shutil, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

try:                                                # browser-impersonating HTTP client (pip install curl_cffi)
    from curl_cffi import requests as curl_requests
except Exception:                                   # noqa: BLE001
    curl_requests = None

ROOT = Path(__file__).resolve().parent.parent
DATA, SITE, OUT = ROOT / "data", ROOT / "site", ROOT / "_site"
CFG = json.loads((ROOT / "config.json").read_text())
LONDON = ZoneInfo(CFG.get("timezone", "Europe/London"))
PLAYERS = CFG["players"]

DEFAULT_HOSTS = "https://query1.finance.yahoo.com,https://query2.finance.yahoo.com"
# Optional override (e.g. your own proxy) via the YAHOO_HOSTS repo variable / env var.
HOSTS = [h.strip().rstrip("/") for h in (os.environ.get("YAHOO_HOSTS") or DEFAULT_HOSTS).split(",") if h.strip()]
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
RETRY_DELAYS = [float(x) for x in (os.environ.get("YF_DELAYS") or "0,4,10,20").split(",")]   # secs before each attempt
PACE = float(os.environ.get("YF_PACE") or "0.4")                                             # gap between requests

CUTOFF_H, CUTOFF_M = (int(x) for x in CFG.get("finalise_after", "21:30").split(":"))
LIVE_START, LIVE_END = (7, 55), (21, 10)           # London time window in which live prices are fetched
BASIS = CFG.get("scoring_basis", "gbp")            # "gbp" or "local"
PTS_N = CFG["points"]["normal"]
PTS_W = CFG["points"]["week_close"]


# ----------------------------------------------------------------- helpers
def now_utc():
    s = os.environ.get("TRACKER_NOW")              # test hook, e.g. 2026-11-09T21:35 (London)
    if s:
        dt = datetime.fromisoformat(s)
        return (dt.replace(tzinfo=LONDON) if dt.tzinfo is None else dt).astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


class Http:
    """GET helper. Uses curl_cffi (Chrome TLS/HTTP2 fingerprint) when available, else urllib."""

    def __init__(self):
        self.s, self.kind, self.calls = None, "urllib (no browser impersonation - Yahoo may block this)", 0
        if curl_requests is not None:
            try:
                imp = os.environ.get("CURL_IMPERSONATE") or "chrome"
                self.s = curl_requests.Session(impersonate=imp)
                self.kind = f"curl_cffi (impersonate={imp})"
            except Exception as e:                  # noqa: BLE001
                log(f"curl_cffi could not start ({e}); falling back to urllib")

    def get(self, url, timeout=25):
        """-> (status, headers, body_text). Raises only on network errors."""
        self.calls += 1
        if self.s is not None:
            r = self.s.get(url, timeout=timeout)
            return r.status_code, r.headers, r.text
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.headers, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode("utf-8", "replace")


HTTP = Http()


class FetchError(RuntimeError):
    pass


def pace():
    time.sleep(PACE + random.uniform(0, PACE))


def warm_up():
    """Pick up Yahoo's session cookie once per run (what yfinance does); failure is harmless."""
    if HTTP.s is None or not all("yahoo.com" in h for h in HOSTS):
        return
    try:
        HTTP.get("https://fc.yahoo.com", timeout=15)
    except Exception as e:                          # noqa: BLE001
        log(f"  warm-up skipped: {e}")


def fetch_chart(symbol, interval, rng):
    q = (f"/v8/finance/chart/{urllib.parse.quote(symbol)}"
         f"?interval={interval}&range={rng}&includePrePost=false")
    last = "no attempt made"
    tag = f"{symbol} {interval}/{rng}"
    for attempt, delay in enumerate(RETRY_DELAYS, 1):
        if delay:
            time.sleep(delay + random.uniform(0, 1.5))
        for host in HOSTS:
            label = host.split("//")[-1].split(".")[0]
            try:
                status, hdrs, body = HTTP.get(host + q)
            except Exception as e:                  # noqa: BLE001
                last = f"{type(e).__name__}: {e}"
                log(f"  [{tag}] {label} try {attempt}/{len(RETRY_DELAYS)}: {last}")
                continue
            if status == 200:
                try:
                    chart = (json.loads(body) or {}).get("chart") or {}
                except ValueError:
                    last = "response was not JSON"
                    log(f"  [{tag}] {label} try {attempt}/{len(RETRY_DELAYS)}: {last}")
                    continue
                res = chart.get("result")
                if res:
                    return res[0]
                last = f"Yahoo returned no data ({chart.get('error')})"
                log(f"  [{tag}] {label} try {attempt}/{len(RETRY_DELAYS)}: {last}")
                continue
            last = f"HTTP {status}"
            log(f"  [{tag}] {label} try {attempt}/{len(RETRY_DELAYS)}: {last}")
            if status == 404:
                raise FetchError(f"{symbol}: symbol not found on Yahoo (HTTP 404) - check config.json")
            if status == 429:
                ra = (hdrs.get("Retry-After") if hdrs else None) or ""
                time.sleep(min(int(ra), 30) if ra.isdigit() else 0.5)
    raise FetchError(f"Yahoo fetch failed for {symbol} ({interval}/{rng}): {last}")


def bars(res):
    """[(epoch, close)] with None closes removed."""
    ts = res.get("timestamp") or []
    cl = ((res.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    return [(t, float(c)) for t, c in zip(ts, cl) if c is not None]


def daily(res):
    """[(YYYY-MM-DD, close)] in exchange-local dates, de-duplicated, sorted."""
    tz = ZoneInfo(res["meta"].get("exchangeTimezoneName") or "UTC")
    out = {}
    for t, c in bars(res):
        out[datetime.fromtimestamp(t, tz).date().isoformat()] = c
    return sorted(out.items())


class Series:
    def __init__(self, pairs):
        self.d = [p[0] for p in pairs]
        self.v = [p[1] for p in pairs]
        self.map = dict(pairs)

    def asof(self, d):
        i = bisect.bisect_right(self.d, d) - 1
        return self.v[i] if i >= 0 else None


def fx_symbol(cur):
    return f"GBP{cur}=X"


def r(x, n=4):
    return None if x is None else round(x, n)


# -------------------------------------------------------------------- live
def fetch_live():
    # Shares first: they have no fallback, so if Yahoo is blocking us we find out straight away.
    stocks = {}
    for p in PLAYERS:
        res = fetch_chart(p["symbol"], "5m", "1d")
        stocks[p["name"]] = {"meta": res["meta"], "bars": bars(res)}
        pace()
    fx = {"GBP": {"now": 1.0, "prev": 1.0, "bars": []}}
    for cur in sorted({p["currency"] for p in PLAYERS if p["currency"] != "GBP"}):
        res = fetch_chart(fx_symbol(cur), "5m", "1d")
        m = res["meta"]
        now_v = m.get("regularMarketPrice")
        fx[cur] = {"now": now_v, "prev": m.get("chartPreviousClose") or now_v, "bars": bars(res)}
        pace()
    return fx, stocks


def fx_at(fxd, ts):
    """latest FX bar at or before ts (falls back to current)."""
    b = fxd["bars"]
    if not b:
        return fxd["now"]
    i = bisect.bisect_right([x[0] for x in b], ts) - 1
    return b[i][1] if i >= 0 else b[0][1]


def build_live(fx, stocks, now):
    out = []
    for p in PLAYERS:
        s = stocks[p["name"]]
        m = s["meta"]
        tz = ZoneInfo(m.get("exchangeTimezoneName") or "UTC")
        today_ex = now.astimezone(tz).date()
        rmt = m.get("regularMarketTime")
        traded_today = bool(rmt) and datetime.fromtimestamp(rmt, tz).date() == today_ex
        reg = (m.get("currentTradingPeriod") or {}).get("regular") or {}
        nowts = int(now.timestamp())
        in_session = bool(reg) and reg["start"] <= nowts < reg["end"]
        is_open = in_session and traded_today
        status = "open" if is_open else ("pre" if reg and nowts < reg.get("start", 0) else "closed")

        f = fx[p["currency"]]
        price_local = m.get("regularMarketPrice")
        prev_local = m.get("chartPreviousClose")
        price_
