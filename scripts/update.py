#!/usr/bin/env python3
"""
Share-price showdown updater (stdlib only, no API keys).

Every run (GitHub Actions, every ~5 min on weekdays):
  1. pulls live quotes + 5-min intraday bars (Yahoo Finance chart API) and FX rates
  2. converts everything to GBP
  3. after 21:30 London, once per day, "finalises" the day: refreshes full price
     history, awards points (append-only), recomputes baselines
  4. writes data/*.json and builds ./_site for GitHub Pages

Outputs to $GITHUB_OUTPUT:  deploy=true|false   committed=true|false
"""
import bisect, json, os, shutil, sys, time, urllib.parse, urllib.request
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DATA, SITE, OUT = ROOT / "data", ROOT / "site", ROOT / "_site"
CFG = json.loads((ROOT / "config.json").read_text())
LONDON = ZoneInfo(CFG.get("timezone", "Europe/London"))
PLAYERS = CFG["players"]
HOSTS = os.environ.get(
    "YAHOO_HOSTS", "https://query1.finance.yahoo.com,https://query2.finance.yahoo.com"
).split(",")
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
CUTOFF_H, CUTOFF_M = (int(x) for x in CFG.get("finalise_after", "21:30").split(":"))
BASIS = CFG.get("scoring_basis", "gbp")           # "gbp" or "local"
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


def fetch_chart(symbol, interval, rng):
    q = (f"/v8/finance/chart/{urllib.parse.quote(symbol)}"
         f"?interval={interval}&range={rng}&includePrePost=false")
    last = None
    for attempt in range(3):
        for host in HOSTS:
            try:
                req = urllib.request.Request(host + q, headers={"User-Agent": UA, "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=25) as r:
                    j = json.load(r)
                res = (j.get("chart") or {}).get("result")
                if res:
                    return res[0]
                last = RuntimeError(str((j.get("chart") or {}).get("error")))
            except Exception as e:                  # noqa: BLE001
                last = e
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Yahoo fetch failed for {symbol} ({interval}/{rng}): {last}")


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
    cur_set = sorted({p["currency"] for p in PLAYERS if p["currency"] != "GBP"})
    fx = {"GBP": {"now": 1.0, "prev": 1.0, "bars": []}}
    for cur in cur_set:
        res = fetch_chart(fx_symbol(cur), "5m", "1d")
        m = res["meta"]
        now_v = m.get("regularMarketPrice")
        fx[cur] = {"now": now_v, "prev": m.get("chartPreviousClose") or now_v, "bars": bars(res)}
        time.sleep(0.3)
    stocks = {}
    for p in PLAYERS:
        res = fetch_chart(p["symbol"], "5m", "1d")
        stocks[p["name"]] = {"meta": res["meta"], "bars": bars(res)}
        time.sleep(0.3)
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
        price_gbp = price_local / f["now"]
        prev_gbp = (prev_local / f["prev"]) if (prev_local and traded_today) else None
        chg = (price_gbp - prev_gbp) if prev_gbp else None
        pct = ((price_gbp / prev_gbp - 1) * 100) if prev_gbp else None
        pct_local = ((price_local / prev_local - 1) * 100) if (prev_local and traded_today) else None
        intraday = []
        if traded_today:
            for t, c in s["bars"]:
                if datetime.fromtimestamp(t, tz).date() == today_ex:
                    intraday.append([t, r(c / fx_at(f, t), 3)])
        out.append({
            "name": p["name"], "stock": p["stock"], "symbol": p["symbol"], "currency": p["currency"],
            "color": p.get("color"), "status": status, "traded_today": traded_today,
            "price_local": r(price_local, 3), "price_gbp": r(price_gbp, 3), "fx": r(f["now"], 5),
            "prev_close_gbp": r(prev_gbp, 3), "change_gbp": r(chg, 3),
            "change_pct": r(pct, 3), "change_pct_local": r(pct_local, 3),
            "last_trade": rmt, "intraday": intraday,
        })
    return out


def value_added(live_players, baselines):
    res = []
    for lp in live_players:
        b = baselines.get(lp["name"])
        if not b:
            continue
        local_pct = (lp["price_local"] / b["price_local"] - 1) * 100
        fx_pct = (b["fx"] / lp["fx"] - 1) * 100
        total = (lp["price_gbp"] / b["price_gbp"] - 1) * 100
        res.append({
            "name": lp["name"], "stock": lp["stock"], "start": b["requested_start"], "baseline_date": b["date"],
            "start_price_gbp": r(b["price_gbp"], 3), "now_price_gbp": lp["price_gbp"],
            "share_pct_local": r(local_pct, 2), "fx_effect_pct": r(fx_pct, 2), "total_pct": r(total, 2),
            "gain_gbp": r(lp["price_gbp"] - b["price_gbp"], 3),
        })
    return sorted(res, key=lambda x: -x["total_pct"])


# -------------------------------------------------------------- finalising
def last_final_date(now):
    ln = now.astimezone(LONDON)
    after = (ln.hour, ln.minute) >= (CUTOFF_H, CUTOFF_M) and ln.weekday() < 5
    return (ln.date() if after else ln.date() - timedelta(days=1)), after


def week_friday(d):
    return d + timedelta(days=4 - d.weekday())


def apply_points(days, check_date):
    """Friday close = double points. If Friday was a full market holiday the last
    trading day of that week gets the double instead (once Friday has passed)."""
    dset = [date.fromisoformat(d["date"]) for d in days]
    for d, dd in zip(days, dset):
        fri = week_friday(dd)
        wc = dd.weekday() == 4 or (fri <= check_date and not any(dd < x <= fri for x in dset))
        d["week_close"] = wc
        d["points"] = PTS_W if wc else PTS_N


def standings(days):
    tot = {p["name"]: {"name": p["name"], "stock": p["stock"], "color": p.get("color"), "points": 0, "wins": 0}
           for p in PLAYERS}
    for d in days:
        for w in d["winners"]:
            tot[w]["points"] += d["points"]
            tot[w]["wins"] += 1
    rows = sorted(tot.values(), key=lambda x: (-x["points"], -x["wins"], x["name"]))
    rank, prev = 0, None
    for i, row in enumerate(rows):
        key = (row["points"], row["wins"])
        if key != prev:
            rank = i + 1
        row["rank"], prev = rank, key
    return rows


def finalise(now, stocks_live, old):
    """Refresh full history, append new scored days, rebuild baselines."""
    check_date, after = last_final_date(now)
    log(f"finalising up to {check_date} (after cutoff: {after})")
    cur_set = sorted({p["currency"] for p in PLAYERS if p["currency"] != "GBP"})
    fxs = {"GBP": None}
    for cur in cur_set:
        fxs[cur] = Series(daily(fetch_chart(fx_symbol(cur), "1d", "max")))
        time.sleep(0.3)
    ser = {}
    for p in PLAYERS:
        ser[p["name"]] = Series(daily(fetch_chart(p["symbol"], "1d", "max")))
        time.sleep(0.3)

    def fxv(cur, d):
        return 1.0 if cur == "GBP" else fxs[cur].asof(d)

    # data-freshness guard: if a stock traded today, today's daily bar must exist
    if after and stocks_live:
        for p in PLAYERS:
            m = stocks_live[p["name"]]["meta"]
            tz = ZoneInfo(m.get("exchangeTimezoneName") or "UTC")
            rmt = m.get("regularMarketTime")
            if rmt and datetime.fromtimestamp(rmt, tz).date() == check_date and check_date.isoformat() not in ser[p["name"]].map:
                raise RuntimeError(f"daily bar for {p['name']} on {check_date} not available yet - retrying next run")

    # ---- score new days (append-only; frozen once written)
    days = list((old or {}).get("days", []))
    have = {d["date"] for d in days}
    start = CFG["points_start"]
    all_dates = sorted({d for s in ser.values() for d in s.d if start <= d <= check_date.isoformat()})
    for d in all_dates:
        if d in have:
            continue
        res = {}
        for p in PLAYERS:
            s = ser[p["name"]]
            if d not in s.map:
                continue
            i = s.d.index(d)
            if i == 0:
                continue
            pd_, c, pc = s.d[i - 1], s.v[i], s.v[i - 1]
            g_now, g_prev = c / fxv(p["currency"], d), pc / fxv(p["currency"], pd_)
            res[p["name"]] = {"pct": r((g_now / g_prev - 1) * 100, 3), "pct_local": r((c / pc - 1) * 100, 3),
                              "price_gbp": r(g_now, 3)}
        key = "pct" if BASIS == "gbp" else "pct_local"
        winners = []
        if len(res) >= 2:
            score = {n: round(v[key], 2) for n, v in res.items()}      # ties judged at 2 d.p.
            best = max(score.values())
            winners = [n for n, v in score.items() if v == best]
        days.append({"date": d, "weekday": date.fromisoformat(d).strftime("%a"), "results": res, "winners": winners})
    days.sort(key=lambda x: x["date"])
    apply_points(days, check_date)

    # ---- baselines for "value added"
    baselines = {}
    for p in PLAYERS:
        s = ser[p["name"]]
        i = bisect.bisect_right(s.d, p["start"]) - 1       # close on start date, else last close before it
        if i < 0:
            i = 0
        d = s.d[i]
        fxb = fxv(p["currency"], d)
        baselines[p["name"]] = {"requested_start": p["start"], "date": d, "price_local": r(s.v[i], 4),
                                "fx": r(fxb, 5), "price_gbp": r(s.v[i] / fxb, 4)}

    # ---- chart history (GBP). Daily for last 5y, weekly before that.
    hist = {}
    cut5 = (now.astimezone(LONDON).date() - timedelta(days=365 * 5 + 7)).isoformat()
    for p in PLAYERS:
        s = ser[p["name"]]
        pts = []
        for d, v in zip(s.d, s.v):
            f = fxv(p["currency"], d)
            if f:
                pts.append((d, v / f))
        keep = []
        for idx, (d, v) in enumerate(pts):
            if d >= cut5:
                keep.append((d, v))
                continue
            wk = date.fromisoformat(d).isocalendar()[:2]
            nxt = date.fromisoformat(pts[idx + 1][0]).isocalendar()[:2] if idx + 1 < len(pts) else None
            if nxt != wk:                                  # last trading day of the week
                keep.append((d, v))
        hist[p["name"]] = {
            "stock": p["stock"], "color": p.get("color"),
            "d": [k[0] for k in keep], "p": [round(k[1], 3) for k in keep],
        }
    history = {"updated": now.isoformat(), "series": hist}

    board = {
        "updated": now.isoformat(),
        "finalised_through": check_date.isoformat() if after else (old or {}).get("finalised_through"),
        "scoring_basis": BASIS, "points": CFG["points"], "points_start": start,
        "baselines": baselines, "days": days, "standings": standings(days),
    }
    return board, history


# -------------------------------------------------------------------- main
def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, separators=(",", ":"), ensure_ascii=False))


def set_output(k, v):
    f = os.environ.get("GITHUB_OUTPUT")
    if f:
        with open(f, "a") as fh:
            fh.write(f"{k}={v}\n")
    log(f"output {k}={v}")


def build_site():
    if OUT.exists():
        shutil.rmtree(OUT)
    shutil.copytree(SITE, OUT)
    (OUT / "data").mkdir(exist_ok=True)
    for f in ("live.json", "leaderboard.json", "history.json"):
        shutil.copy(DATA / f, OUT / "data" / f)
    (OUT / ".nojekyll").write_text("")


def main():
    force = "--force" in sys.argv or os.environ.get("FORCE") == "1"
    now = now_utc()
    ln = now.astimezone(LONDON)
    today = ln.date()
    board_p, hist_p = DATA / "leaderboard.json", DATA / "history.json"
    old = json.loads(board_p.read_text()) if board_p.exists() else None

    fx, stocks = fetch_live()
    live_players = build_live(fx, stocks, now)

    check_date, after = last_final_date(now)
    due = force or old is None or (after and old.get("finalised_through") != check_date.isoformat())
    committed = False
    if due:
        board, history = finalise(now, stocks, old)
        write_json(board_p, board)
        write_json(hist_p, history)
        committed = True
    else:
        board = old
    # ---- today block
    traded = [p for p in live_players if p["traded_today"] and p["change_pct"] is not None]
    key = "change_pct" if BASIS == "gbp" else "change_pct_local"
    day_today = next((d for d in board["days"] if d["date"] == today.isoformat()), None)
    pts_today = PTS_W if today.weekday() == 4 else PTS_N
    if day_today:
        today_blk = {"date": day_today["date"], "status": "final", "winners": day_today["winners"],
                     "points": day_today["points"],
                     "ranking": sorted([{"name": n, "pct": v["pct"]} for n, v in day_today["results"].items()],
                                       key=lambda x: -x["pct"])}
    elif len(traded) >= 2:
        sc = {p["name"]: round(p[key], 2) for p in traded}
        best = max(sc.values())
        today_blk = {"date": today.isoformat(), "status": "provisional", "points": pts_today,
                     "winners": [n for n, v in sc.items() if v == best],
                     "ranking": sorted([{"name": p["name"], "pct": p["change_pct"]} for p in traded],
                                       key=lambda x: -x["pct"])}
    else:
        today_blk = {"date": today.isoformat(), "status": "waiting" if today.weekday() < 5 else "weekend",
                     "points": pts_today, "winners": [], "ranking": []}
    prev_days = [d for d in board["days"] if d["date"] < today.isoformat() and d["winners"]]
    yd = prev_days[-1] if prev_days else None
    live = {
        "updated": now.isoformat(), "title": CFG.get("title"), "scoring_basis": BASIS,
        "finalise_after": CFG.get("finalise_after", "21:30"),
        "players": live_players, "today": today_blk,
        "yesterday": ({"date": yd["date"], "winners": yd["winners"], "points": yd["points"],
                       "ranking": sorted([{"name": n, "pct": v["pct"]} for n, v in yd["results"].items()],
                                         key=lambda x: -x["pct"])} if yd else None),
        "value_added": value_added(live_players, board["baselines"]),
    }
    write_json(DATA / "live.json", live)

    any_open = any(p["status"] == "open" for p in live_players)
    deploy = force or due or any_open
    if deploy:
        build_site()
    set_output("deploy", "true" if deploy else "false")
    set_output("committed", "true" if committed else "false")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:                              # noqa: BLE001
        log(f"::warning::Update skipped - {e}")
        set_output("deploy", "false")
        set_output("committed", "false")
        sys.exit(0 if os.environ.get("SOFT_FAIL", "1") == "1" else 1)
