#!/usr/bin/env python3
"""
Cross-venue paper-trading arbitrage scanner — ETH, BTC, XRP, SOL
================================================================

What it does
------------
Every poll interval it fetches REAL live bid/ask quotes for ETH, BTC, XRP and
SOL from several exchanges (Binance, Coinbase, Kraken, OKX — using only their
free public endpoints), then for each asset finds the cheapest ask (buy venue)
and the highest bid (sell venue) across exchanges.

When the spread between them is larger than the two taker fees (plus your
configured minimum), it "executes" a PAPER trade:

  * cash on the buy venue pays for the coin
  * coin inventory on the sell venue is sold
  * the net USD result is booked into your paper ledger

While it runs, a local DASHBOARD is served (loopback only) at:

    http://127.0.0.1:8899

with live charts of portfolio value / realized profit, per-asset and venue
breakdowns and the full paper trades ledger.

Nothing ever leaves your laptop. There is NO code path that can move real
money: a real-payments "live" mode is intentionally not implemented (see
README, "Going live").

Usage
-----
    python3 arb_paper_bot.py                 # scan loop + dashboard on 127.0.0.1:8899
    python3 arb_paper_bot.py --once          # single scan, print, exit (no dashboard)
    python3 arb_paper_bot.py --no-dashboard  # scan loop without the web dashboard
    python3 arb_paper_bot.py --config my_config.json

Files
-----
    config.json       settings (venues, fees, sizes — edit freely)
    dashboard.html    the web dashboard (served at http://127.0.0.1:8899)
    paper_state.json  paper balances + equity history (auto-created, persists)
    paper_trades.csv  append-only trade log (auto-created)
"""

import argparse
import csv
import http.server
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULT_CONFIG = {
    "mode": "paper",
    "assets": ["ETH", "BTC", "XRP", "SOL"],
    "poll_interval_seconds": 15,
    "trade_size_usd": 100,
    "min_net_spread_bps": 5,
    "taker_fees_bps": {"binance": 10, "coinbase": 60, "kraken": 26, "okx": 10},
    "starting_cash_per_venue_usd": 1000,
    "coin_seed_usd": 100,
    "state_file": "paper_state.json",
    "trades_log": "paper_trades.csv",
    "http_timeout_seconds": 8,
    "quote_max_age_seconds": 30,
    "dashboard_enabled": True,
    "dashboard_host": "127.0.0.1",
    "dashboard_port": 8899,
    "history_max_points": 2000
}

ASSET_USDT = {"ETH": "ETHUSDT", "BTC": "BTCUSDT", "XRP": "XRPUSDT", "SOL": "SOLUSDT"}
ASSET_COINBASE = {"ETH": "ETH-USD", "BTC": "BTC-USD", "XRP": "XRP-USD", "SOL": "SOL-USD"}
ASSET_KRAKEN = {"ETH": "ETHUSD", "BTC": "XBTUSD", "XRP": "XRPUSD", "SOL": "SOLUSD"}
KRAKEN_BASE = {"ETH": "ETH", "BTC": "XBT", "XRP": "XRP", "SOL": "SOL"}
ASSET_OKX = {"ETH": "ETH-USDT", "BTC": "BTC-USDT", "XRP": "XRP-USDT", "SOL": "SOL-USDT"}

STATE_LOCK = threading.Lock()


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    print(f"[{now_iso()}] {msg}", flush=True)


def http_get_json(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": "arb-paper-bot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Venue fetchers — each returns {asset: {"bid": float, "ask": float}} or raises
# ---------------------------------------------------------------------------

def fetch_binance(cfg, assets):
    symbols = json.dumps([ASSET_USDT[a] for a in assets], separators=(",", ":"))
    url = ("https://api.binance.com/api/v3/ticker/bookTicker?symbols="
           + urllib.parse.quote(symbols))
    data = http_get_json(url, cfg["http_timeout_seconds"])
    reverse = {ASSET_USDT[a]: a for a in assets}
    out = {}
    for row in data:
        asset = reverse[row["symbol"]]
        out[asset] = {"bid": float(row["bidPrice"]), "ask": float(row["askPrice"])}
    return out


def fetch_coinbase(cfg, assets):
    out = {}
    for a in assets:
        try:
            d = http_get_json(
                f"https://api.exchange.coinbase.com/products/{ASSET_COINBASE[a]}/ticker",
                cfg["http_timeout_seconds"])
            bid, ask = d.get("bid"), d.get("ask")
            if bid and ask:
                out[a] = {"bid": float(bid), "ask": float(ask)}
        except Exception:
            pass
    if not out:
        raise RuntimeError("no quotes from coinbase")
    return out


def fetch_kraken(cfg, assets):
    url = "https://api.kraken.com/0/public/Ticker?pair=" + ",".join(
        ASSET_KRAKEN[a] for a in assets)
    d = http_get_json(url, cfg["http_timeout_seconds"])
    if d.get("error"):
        raise RuntimeError("kraken API error: " + "; ".join(d["error"]))
    result = d["result"]
    keys = [k for k in result.keys() if k != "last"]
    out = {}
    for a in assets:
        base = KRAKEN_BASE[a]
        key = next((k for k in keys
                    if k.upper().endswith(("ZUSD", "USD")) and base in k.upper()), None)
        if not key:
            continue
        tick = result[key]
        bid, ask = tick["b"][0], tick["a"][0]
        out[a] = {"bid": float(bid), "ask": float(ask)}
    if not out:
        raise RuntimeError("no quotes from kraken")
    return out


def fetch_okx(cfg, assets):
    wanted = {ASSET_OKX[a] for a in assets}
    d = http_get_json("https://www.okx.com/api/v5/market/tickers?instType=SPOT",
                      cfg["http_timeout_seconds"])
    if d.get("code") != "0":
        raise RuntimeError("okx API error: " + str(d.get("msg")))
    out = {}
    reverse = {ASSET_OKX[a]: a for a in assets}
    for row in d["data"]:
        inst = row.get("instId")
        if inst in wanted and row.get("bidPx") and row.get("askPx"):
            out[reverse[inst]] = {"bid": float(row["bidPx"]), "ask": float(row["askPx"])}
    if not out:
        raise RuntimeError("no quotes from okx")
    return out


VENUES = {
    "binance": fetch_binance,
    "coinbase": fetch_coinbase,
    "kraken": fetch_kraken,
    "okx": fetch_okx,
}


# ---------------------------------------------------------------------------
# Paper state / ledger
# ---------------------------------------------------------------------------

def load_config(path):
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    if cfg.get("mode") != "paper":
        raise SystemExit(
            "\nREFUSED to start: config mode is not \"paper\".\n"
            "This build has NO live-trading capability — there is no code that\n"
            "places real orders anywhere. Going live is a manual, deliberate step\n"
            "(see README, section 'Going live').\n")
    return cfg


def load_state(cfg, assets):
    path = os.path.join(HERE, cfg["state_file"])
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
        state.setdefault("history", [])
        for a in assets:
            for v in state["venues"].values():
                v["coins"].setdefault(a, 0.0)
        return state

    state = {
        "started_at": now_iso(),
        "venues": {
            name: {
                "cash": float(cfg["starting_cash_per_venue_usd"]),
                "coins": {a: 0.0 for a in assets},
                "seeded": {a: False for a in assets}
            }
            for name in VENUES
        },
        "stats": {
            "scans": 0,
            "opportunities_seen": 0,
            "trades": 0,
            "realized_net_usd": 0.0
        },
        "history": [],
        "last_prices": {}
    }
    save_state(cfg, state)
    return state


def save_state(cfg, state):
    path = os.path.join(HERE, cfg["state_file"])
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


def append_trade_row(cfg, row):
    path = os.path.join(HERE, cfg["trades_log"])
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "asset", "buy_venue", "buy_price", "sell_venue",
                        "sell_price", "gross_bps", "net_bps", "size_usd",
                        "coin_amount", "est_net_usd"])
        w.writerow(row)


def read_trades_csv(cfg, limit=500, assets=None):
    path = os.path.join(HERE, cfg["trades_log"])
    if not os.path.exists(path):
        return []
    with open(path, "r", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if assets:
        rows = [r for r in rows if r.get("asset") in assets]
    return list(reversed(rows))[-limit:]


def seed_inventory_if_needed(state, cfg, venue, asset, price):
    v = state["venues"][venue]
    if v["seeded"].get(asset):
        return
    qty = float(cfg["coin_seed_usd"]) / price
    v["coins"][asset] = max(v["coins"][asset], qty)
    v["seeded"][asset] = True


def portfolio_value(state):
    total = sum(v["cash"] for v in state["venues"].values())
    for a, price in state.get("last_prices", {}).items():
        qty = sum(v["coins"].get(a, 0.0) for v in state["venues"].values())
        total += qty * price
    return total


def record_history(cfg, state):
    hist = state.setdefault("history", [])
    s = state["stats"]
    hist.append({
        "t": now_iso(),
        "value": portfolio_value(state),
        "net": s["realized_net_usd"],
        "scans": s["scans"],
        "trades": s["trades"]
    })
    cap = int(cfg["history_max_points"])
    if len(hist) > cap:
        del hist[:len(hist) - cap]


# ---------------------------------------------------------------------------
# The scanner
# ---------------------------------------------------------------------------

def fetch_all_quotes(cfg, assets):
    quotes = {}
    errors = {}
    for name, fn in VENUES.items():
        try:
            quotes[name] = fn(cfg, assets)
        except Exception as e:
            errors[name] = f"{type(e).__name__}: {e}"
    return quotes, errors


def run_scan(cfg, assets, state):
    quotes, errors = fetch_all_quotes(cfg, assets)
    state["stats"]["scans"] += 1

    for err_venue, err in sorted(errors.items()):
        log(f"WARN   {err_venue} unavailable this scan: {err}")

    fees = cfg["taker_fees_bps"]
    state["last_prices"] = {}
    rows = []
    for a in assets:
        candidates = []
        for venue, q in quotes.items():
            if a in q and q[a]["bid"] > 0 and q[a]["ask"] > 0:
                candidates.append((venue, q[a]["bid"], q[a]["ask"]))
        if len(candidates) < 2:
            rows.append((a, "fewer than 2 venues quoting", "-",
                         "-", "-", "-", "skip"))
            continue

        buy_venue, _, best_ask = min(candidates, key=lambda c: c[2])
        sell_venue, best_bid, _ = max(candidates, key=lambda c: c[1])
        state["last_prices"][a] = (best_bid + best_ask) / 2.0

        for venue, _, _ in candidates:
            seed_inventory_if_needed(state, cfg, venue, a, best_ask)

        if sell_venue == buy_venue:
            rows.append((a, "single venue only", "-", "-", "-", "-", "skip"))
            continue

        gross_bps = (best_bid - best_ask) / best_ask * 1e4
        fee_bps = fees.get(buy_venue, 0) + fees.get(sell_venue, 0)
        net_bps = gross_bps - fee_bps

        if net_bps >= cfg["min_net_spread_bps"]:
            state["stats"]["opportunities_seen"] += 1
            note = paper_trade(cfg, state, a, buy_venue, sell_venue,
                               best_ask, best_bid, gross_bps, net_bps)
        else:
            note = "-"

        rows.append((a, f"{buy_venue} ask {best_ask:,.2f}",
                     f"{sell_venue} bid {best_bid:,.2f}",
                     f"{gross_bps:+.2f} bps", f"{fee_bps:.0f} bps",
                     f"{net_bps:+.2f} bps", note))
    return rows


def paper_trade(cfg, state, asset, buy_venue, sell_venue, ask, bid,
                gross_bps, net_bps):
    size_usd = float(cfg["trade_size_usd"])
    buy = state["venues"][buy_venue]
    sell = state["venues"][sell_venue]

    buy_frac = cfg["taker_fees_bps"].get(buy_venue, 0) / 1e4
    sell_frac = cfg["taker_fees_bps"].get(sell_venue, 0) / 1e4

    inventory_value = sell["coins"][asset] * bid
    size_usd = min(size_usd, buy["cash"], inventory_value * (1 - buy_frac))
    if size_usd <= 1.0:
        return "NO FUNDS on paper venue"

    coin_needed = size_usd * (1 - buy_frac) / ask
    proceeds = coin_needed * (1 - sell_frac) * bid
    est_net_usd = proceeds - size_usd

    buy["cash"] -= size_usd
    buy["coins"][asset] += coin_needed
    sell["coins"][asset] -= coin_needed
    sell["cash"] += proceeds

    state["stats"]["trades"] += 1
    state["stats"]["realized_net_usd"] += est_net_usd
    append_trade_row(cfg, [
        now_iso(), asset, buy_venue, f"{ask:.6f}", sell_venue, f"{bid:.6f}",
        f"{gross_bps:.2f}", f"{net_bps:.2f}", f"{size_usd:.2f}",
        f"{coin_needed:.8f}", f"{est_net_usd:.4f}"
    ])
    return (f"TRADE ${size_usd:,.2f} -> "
            f"{'+' if est_net_usd >= 0 else '-'}${abs(est_net_usd):.4f} (paper)")


def print_rows(rows):
    headers = ["asset", "best buy (cheapest ask)", "best sell (highest bid)",
               "gross spread", "taker fees", "net spread", "paper action"]
    widths = [max(len(r[i]) for r in rows + [headers]) for i in range(len(headers))]
    fmt = lambda r: "  ".join(c.ljust(w) for c, w in zip(r, widths))
    print()
    print(fmt(headers))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print(fmt(r))
    print(flush=True)


def print_portfolio(cfg, state, assets):
    total = 0.0
    parts = []
    for venue, v in state["venues"].items():
        total += v["cash"]
        parts.append(f"{venue}: ${v['cash']:,.2f}")
    for a in assets:
        qty = sum(v["coins"].get(a, 0.0) for v in state["venues"].values())
        price = state["last_prices"].get(a)
        value = qty * price if price else 0.0
        total += value
        parts.append(f"{a} x{qty:.6f}")
    s = state["stats"]
    print(f"  paper portfolio: ${total:,.2f}    trades: {s['trades']}    "
          f"realized net (all trades): ${s['realized_net_usd']:,.4f}    "
          f"opportunities seen: {s['opportunities_seen']} / {s['scans']} scans", flush=True)
    if parts:
        print("  " + " | ".join(parts), flush=True)


# ---------------------------------------------------------------------------
# Local dashboard (loopback-only web server)
# ---------------------------------------------------------------------------

def build_api_payload(cfg, assets, state):
    return {
        "now": now_iso(),
        "mode": "PAPER — no real-money code path in this build",
        "started_at": state["started_at"],
        "assets": assets,
        "fees_bps": cfg["taker_fees_bps"],
        "venues": {name: {"cash": v["cash"], "coins": v["coins"]}
                   for name, v in state["venues"].items()},
        "last_prices": state.get("last_prices", {}),
        "stats": state["stats"],
        "history": state.get("history", []),
        "trades": read_trades_csv(cfg, limit=1000, assets=assets)
    }


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html", "/dashboard.html"):
            self._serve_file(os.path.join(HERE, "dashboard.html"),
                             "text/html; charset=utf-8")
        elif path == "/api/state":
            with STATE_LOCK:
                payload = build_api_payload(self.server.cfg, self.server.assets,
                                            self.server.state)
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def _serve_file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            self.send_error(404, "dashboard.html missing")
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def start_dashboard(cfg, assets, state):
    host = cfg.get("dashboard_host", "127.0.0.1")
    port = int(cfg.get("dashboard_port", 8899))
    try:
        srv = http.server.ThreadingHTTPServer((host, port), DashboardHandler)
    except OSError as e:
        log(f"dashboard: could not bind {host}:{port} ({e}) — continuing without it")
        return None
    srv.cfg, srv.assets, srv.state = cfg, assets, state
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="paper arbitrage scanner (real data, no real money)")
    ap.add_argument("--once", action="store_true", help="single scan then exit")
    ap.add_argument("--no-dashboard", action="store_true", help="disable the local web dashboard")
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    args = ap.parse_args()

    cfg = load_config(args.config)
    assets = [a.upper() for a in cfg["assets"]]
    state = load_state(cfg, assets)

    print("=" * 88)
    print("  PAPER ARBITRAGE SCANNER — real market data, ZERO real money")
    print(f"  assets: {', '.join(assets)}    venues: {', '.join(VENUES)}")
    print(f"  trade size: ${cfg['trade_size_usd']}    min net spread to trade: "
          f"{cfg['min_net_spread_bps']} bps    poll: {cfg['poll_interval_seconds']}s")
    print(f"  taker fees used (bps): {cfg['taker_fees_bps']}")
    print("  everything is simulated in paper_state.json — Ctrl+C any time")
    print("=" * 88, flush=True)

    dashboard = None
    if not args.once and cfg["dashboard_enabled"] and not args.no_dashboard:
        dashboard = start_dashboard(cfg, assets, state)
        if dashboard:
            log(f"DASHBOARD live at http://{cfg['dashboard_host']}:"
                f"{cfg['dashboard_port']}  (loopback only — never exposed)")
        else:
            log("dashboard: disabled this run")

    try:
        while True:
            try:
                rows = run_scan(cfg, assets, state)
                print_rows(rows)
                print_portfolio(cfg, state, assets)
                with STATE_LOCK:
                    record_history(cfg, state)
                    save_state(cfg, state)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                log(f"scan error (will retry): {type(e).__name__}: {e}")

            if args.once:
                break
            time.sleep(cfg["poll_interval_seconds"])
    except KeyboardInterrupt:
        pass
    finally:
        if dashboard:
            dashboard.shutdown()

    print("\n" + "=" * 88)
    print("  SESSION SUMMARY (paper)")
    print_portfolio(cfg, state, assets)
    print("  trade log: " + os.path.join(HERE, cfg["trades_log"]))
    print("  saved state (resume later): " + os.path.join(HERE, cfg["state_file"]))
    print("=" * 88)


if __name__ == "__main__":
    main()
