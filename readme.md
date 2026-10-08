# QuantLab AI

Market terminal and paper trading laboratory with optional protected Binance account viewing. **No real order submission, leverage or shorts.** Six isolated simulated 100 USDT accounts trade BTCUSDT, ETHUSDT and SOLUSDT using deterministic rules. The terminal additionally observes BNBUSDT, XRPUSDT, DOGEUSDT, ADAUSDT, AVAXUSDT, LINKUSDT, DOTUSDT and LTCUSDT. The name does not imply AI-generated trading decisions.

## Run

Python 3.12+, no third-party packages:

```sh
python server.py
# http://localhost:8000
```

Or use persistent Docker storage:

```sh
docker compose up --build -d
```

Keep the named `quantlab-data` volume. Never run `docker compose down -v` unless deliberately deleting the experiment. Production requires HTTPS through a reverse proxy or hosting provider. The public market and paper dashboard is shared. Private Binance balances require a separate access token; there are no settings mutation or trade-submission endpoints.

## Terminal and optional account viewing

- Terminal: 11 markets, charts of 120 closed candles, six rule-based observations per market and a hypothetical entry-cost calculator. No exchange order is submitted. Signals and estimates become unavailable for stale, incomplete or failed market data. Each additional market refreshes every 60 seconds, independently of the paper worker.
- Paper laboratory: the original six accounts keep trading only BTC, ETH and SOL. Adding watchlist markets does not rewrite the experiment or its historical allocation.
- Binance account: disconnected by default; shows spot holdings and open orders for BTC, ETH and SOL once configured. Holdings do not establish entry cost or real PnL, which are not inferred here.

API configuration is deferred. When ready, set `BINANCE_API_KEY`, `BINANCE_API_SECRET` (HMAC key), and `QUANTLAB_ACCOUNT_TOKEN` in the **server process environment** and restart. Use a new Binance key restricted to reading. The panel token must be a separate randomly generated secret of at least 32 characters. The application does not load `.env` automatically. Docker users must explicitly pass environment variables to the container. Never commit credentials.

Enter only the panel token into the account tab. Binance keys never go to the browser. The account endpoint uses an Authorization header, never URL parameters. Account data is cleared when switching application tabs, hiding the browser tab or choosing “Ukryj dane”; tokens are not persisted. Results are cached for 30 seconds, failures for 60 seconds. `/api/state` never includes private balances or credentials.

The Binance adapter permits only GET `/api/v3/account` and GET `/api/v3/openOrders`. No order creation, cancellation, withdrawal or transfer functions exist. Real account connectivity has not been tested; automated tests use mocked responses.

## Hosting

Dockerfile is ready for a Docker web service, including Render. Set `DATABASE_PATH=/data/quantlab.sqlite3`, attach a **persistent disk at `/data`**, and use one instance. Health check: `/healthz`. The application binds `0.0.0.0:$PORT` (default 8000). A persistent disk/always-on server can require a paid plan; do not deploy this SQLite version on ephemeral storage. Select a hosting workspace and approve hosting costs before provisioning. GitHub stores the source; GitHub Pages cannot run this backend.

Binance public market data must be reachable from the hosting region. The public workers only call GET `time` and `klines` on `https://data-api.binance.vision/api/v3/`. Optional private reads are isolated in `terminal.py`. On network, geographic restriction or rate-limit errors the paper worker backs off to at most five minutes; the UI reports stale/error state and never fabricates data. `/healthz` indicates web-process health; `/api/state` exposes market-data status separately.

## Execution semantics

- UTC 1-minute candles; Binance server time determines whether a candle is closed.
- First launch warms up 120 closed candles without trading. Thereafter all missing candles are replayed in paginated chronological batches.
- All three markets must have the same next timestamp. Missing data halts progress instead of silently skipping candles.
- Signal uses the previous closed candle; fill uses the next candle's open, recorded only once that next candle has closed. This is a simulated fill model, not a promise of executable market prices.
- Shared cash per strategy across three symbols. Fixed allocation order BTC, ETH, SOL. One position per symbol; no pyramiding. SELL closes the entire position.
- BUY spends at most 25% of current cash including its 0.10% fee; slippage 0.02% adverse on entry and exit. Fractional simulated quantities; no exchange lot/minimum-notional restrictions are modeled.
- SMA/EMA crossovers 9/21. EMA seeded at oldest close in the rolling 120-bar window. RSI uses simple average gains/losses over 14 changes (not Wilder smoothing): buy <30, sell >55. Bollinger 20/2: buy below lower band, exit at mean. Breakout: above previous 20 highs, exit below previous 10 lows. Momentum 10: buy >0.3%, exit <0%.
- SQLite `BEGIN IMMEDIATE` atomically commits all six accounts, fills, positions, market bars, equity and global last-processed timestamp. Unique fill keys prevent duplicates. Replay after a crash resumes after the last committed minute.
- Global cursor applies to all three markets; transactions are deterministic across restarts. Run **one worker process** per database.
- Equity = cash + positions marked at latest closed prices. Unrealized PnL includes entry fees, excludes prospective exit fees/slippage. Realized PnL deducts both entry/exit costs. Total PnL = equity − 100; return = total PnL / 100 × 100. Trade count counts BUY and SELL fills separately.
- Complete fill history retained and downloadable as CSV; UI shows latest 200 fills. Latest 120 candles per market and seven days of equity points retained; chart shows latest 240 points.

## API and tests

Read-only: `/api/state`, `/api/trades.csv`, `/healthz`.
Protected read-only: `/api/exchange/account` (Bearer panel token; returns 401 unless authorized).

```sh
python -m unittest discover -s tests -v
```

Tests cover accounting, allocation cap, no shorts/pyramiding, duplicate replay and restart, atomic rollback, candle gaps, closed-candle validation and deterministic replay. GitHub Actions runs the suite on pushes and pull requests.

The responsive dashboard includes a web app manifest for a standalone home-screen shortcut. It requires an online server; no offline trading or service-worker caching is implemented.
