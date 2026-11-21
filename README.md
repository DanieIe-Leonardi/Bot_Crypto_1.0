# Paper Arbitrage Scanner — ETH / BTC / XRP / SOL

A single-file Python app that runs on your local laptop. It polls **real, live
bid/ask quotes** for ETH, BTC, XRP and SOL from four exchanges (Binance,
Coinbase, Kraken, OKX — free public endpoints, no accounts or API keys) and
**paper-trades** the best cross-exchange spread into a local ledger — with a
built-in **web dashboard** (`http://127.0.0.1:8899`) showing profit, equity
curve, per-venue balances and the full trade ledger.

- **100% paper.** There is no code path anywhere that can place a real order,
  move funds, or touch a wallet. The config refuses to start unless
  `"mode": "paper"`, and even that only gates the scanner itself.
- Real data, real fees, honest accounting.
- All state is local: `paper_state.json` (balances + equity history, resumable)
  and `paper_trades.csv` (append-only trade log) — both feed the dashboard.

## Web dashboard (built in)

While the scanner runs (not in `--once` mode), it serves a fully designed local
dashboard at:

    http://127.0.0.1:8899

It shows, auto-refreshing every 5 s:

- **KPI cards** — portfolio value, realized paper net, trade count + win rate,
  net-positive spreads seen vs scans, best and worst trade
- **Charts** — portfolio value over time, cumulative realized net over time,
  trades + est. net per asset, net-spread (bps) distribution
- **Per-venue balances** — paper cash and coin inventory (marked to market)
- **Full trades ledger** — filterable by asset / text / profitable-only

The server binds to `127.0.0.1` only — it is never reachable from outside the
machine. Charts use Chart.js from a CDN; without internet the tables still
work. Disable entirely with `--no-dashboard` or `"dashboard_enabled": false`
(port/host configurable in `config.json`).

## Requirements

- Python 3.8+ (standard library only — nothing to install)
- An internet connection

## Run

```bash
python3 arb_paper_bot.py            # continuous scan, one line per poll, Ctrl+C to stop
python3 arb_paper_bot.py --once     # single scan and exit
python3 arb_paper_bot.py --config my_config.json
```

Example output (real data, 2026-11-21):

```
asset  best buy (cheapest ask)  best sell (highest bid)  gross spread  taker fees  net spread  paper action
ETH    coinbase ask 1,700.34    okx bid 1,703.19         +16.76 bps    70 bps      -53.24 bps  -
BTC    coinbase ask 60,491.46   binance bid 60,590.52    +16.38 bps    70 bps      -53.62 bps  -
```

Each poll it also prints your paper portfolio (cash per venue + coin
inventory, marked to the latest real price), number of trades, and cumulative
realized net.

## Configuration (`config.json`)

| key | meaning |
|---|---|
| `mode` | must be `"paper"` in this build |
| `assets` | default `["ETH","BTC","XRP","SOL"]` |
| `poll_interval_seconds` | scan cadence (15s default; keep ≥ 10s to be polite to free APIs) |
| `trade_size_usd` | notional per paper trade |
| `min_net_spread_bps` | only trade when net spread ≥ this (default 5 bps) |
| `taker_fees_bps` | taker fee per venue in basis points; set to match your actual account tier |
| `starting_cash_per_venue_usd` | paper cash per venue (default $1,000 each) |
| `coin_seed_usd` | sell-side coin inventory pre-positioned per venue, valued at first quote |
| `state_file` / `trades_log` | local file names |
| `dashboard_enabled` / `dashboard_host` / `dashboard_port` | built-in web dashboard (see above) |
| `history_max_points` | equity-curve points kept (default 2000 ≈ 8 h at 15 s polls) |

## How the paper simulation models a trade

1. Buy the coin on the venue with the cheapest **ask**, paying that venue's
   cash; taker fee deducted from received coin.
2. Sell on the venue with the highest **bid**, consuming that venue's coin
   inventory; taker fee deducted from proceeds.
3. Net USD = proceeds − cost, booked into the ledger and CSV.

Honest simplifications (deliberate, so the first `net spread` number you see
is the *optimistic* bound):

- **Transfer/rebalancing of coin between venues is not modeled.** Real fees +
  transfer time are the reason cross-exchange arb rarely clears for retail.
- Quotes from the four venues are fetched a few hundred ms–seconds apart.
- `net spread` assumes simultaneous fills at the visible top-of-book with no
  slippage beyond the quoted size.
- OKX/quote prices are in USDT; treated as USD (typically <5 bps offset).

## What you should expect to learn

On healthy markets, live cross-venue spreads for the majors are typically
5–25 bps, while retail taker fees sum to 36–120+ bps. So most scans correctly
show **no trade** — that is the point of the paper phase: the app will surface
the rare moments when a net-positive spread actually appears (big volatility,
venue-specific dislocations) and record exactly how big and how frequent they
are in your region/venues. Set `min_net_spread_bps` to your real rounded-trip
cost before believing any "opportunity".

## Going live

This build intentionally contains **no exchange order/trading code of any
kind** — no API-key handling, no order placement, no withdrawals. If the
paper data shows a persistent, real edge over weeks, going live means using
the official trading APIs of one or two exchanges with real capital, and it
should be implemented as its own reviewed step (API keys, order execution,
in-venue balances, risk limits), not bolted on. Any app or contract that
advertises "guaranteed profit" arbitrage — including flash-loan contracts
circulating online with screenshots of pasted "backtest results" — is a
reliable red flag, not a reference.

## Per-asset note

Exchange-level scanning works the same for all four assets here. If you later
want on-chain (DEX/flash-loan) arbitrage for ETH/WBTC specifically, that is a
separate, much more adversarial domain (MEV, gas, racing other searchers) —
start from the paper scanner's data before spending on-chain gas.
