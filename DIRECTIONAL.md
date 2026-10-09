# Directional Adaptive PAPER

An isolated deterministic experiment with 100 USDT in `data/directional_adaptive.sqlite3`. The six original strategy accounts, Adaptive portfolio, Matrix history and microstructure history remain in their existing databases. No Binance private endpoint, margin account, futures account or order submission is used. Synthetic SHORT is a ledger calculation, not an exchange transaction. `LIVE READY: NO`.

## How the running experiment works

The existing public collector supplies complete 1-minute candles and sampled public bid/ask and aggressive trades. Directional decisions use only closed candles and a contemporaneous microstructure snapshot. Missing or stale book/trade coverage, spread shock, clock drift, incomplete candles, a market shock, conflicting horizons, weak probability or insufficient net EV mean FLAT. The model uses distinct 5/15/30-minute scores with configurable weights. These are initial *uncalibrated* priors, not empirically validated probabilities. BTC returns, rolling beta, cross-sectional breadth and relative strength are among the inputs. LONG, SHORT and FLAT are compared after estimated costs and penalties. A positive signal does not mean demonstrated statistical edge.

At most 20 currently ranked USDT markets are eligible for new orders. Held markets and pending orders remain under observation even if the ranking changes. Opportunities are sorted by estimated utility across markets before the portfolio admits new orders. Expected return, drawdown, execution quality, concentration and correlated exposure enter the utility/risk checks. The separate `directional.kill` file in the data directory prevents new entries and pending fills; stop and time exits can still run.

The default `LONG_SHORT`/`HYBRID` model can be configured **before a fresh experiment database is created** with optional `portfolioMode` = `LONG_ONLY`, `SHORT_ONLY`, `LONG_SHORT`, `MARKET_NEUTRAL` or `CONTROL_FLAT_BASELINE`, and optional `signalStyle` = `HYBRID`, `TREND_FOLLOWING` or `MEAN_REVERSION`. Set `DIRECTIONAL_CONFIG_PATH` to the new JSON and `DIRECTIONAL_DATABASE_PATH` to its own new SQLite path; the default files stay intact. A changed config is rejected against an existing database by its hash. `MARKET_NEUTRAL` submits only BTC-beta-matched LONG/SHORT pairs, scales proposed quantities to equal estimated BTC beta, and fills both legs only when both limits strictly trade through in the same later candle. Otherwise both wait or expire. This is an experimental entry constraint; exits may leave temporary directional exposure, limited by the normal portfolio caps.

Maker entry is a fixed limit at the observed bid for LONG and ask for SHORT. It can fill only in a later closed candle when price strictly trades through the limit and proposed quantity is at most 1% of that candle's volume. It expires after the configured TTL; there is no chase. This is a candle proxy and cannot establish queue priority or real fill probability. A target exit is also a later strict maker trade-through. Stops, time stops and an adverse current EV/regime use taker execution; taker exit includes half-spread, slippage and fee. Short PnL is `(entry - exit) * quantity` before costs. No leverage: full short notional is reserved as collateral, and gross/net/side/symbol/correlation limits apply. Funding rate is zero when no history exists, with `fundingNotModelled=true` saved per trade.

Open positions, pending orders, per-symbol 1-minute cursors and config hash survive restart. The order and decision key is `experiment:model:symbol:1m:candleCloseTimestamp`; SQLite primary keys prevent duplicates. Recovered candles replay existing pending orders and exits; old market snapshots never create retroactive entries. A changed directional config requires a new experiment database rather than rewriting past results. Raw private credentials are never read by this module.

## Statistical evidence

Realized NET trades are grouped by direction, regime and edge class. A seeded bootstrap gives 95% percentile intervals. Fewer than 100 realized trades per class have `INSUFFICIENT_SAMPLE` and no confidence interval. A class with upper bound <=0 pauses entries; lower bound >0 can use the full configured risk; uncertain/new classes collect evidence at 35% of that risk. Drawdown reduces sizing, never increases it to recover losses. `P(final equity > ...)` at 100/250/500/1000/2500/5000 trades and estimated first sustained crossing of 80/90/95% at 1–5000 trades appear only after the sample threshold. Resampling assumes future trades resemble the observed sample; it is not proof of independence or a guarantee. Sequence permutations, cost stress and concentration diagnostics are reported separately.

Future 5/15/30-minute labels are written only after those candles close. The dashboard uses 15-minute labels for accuracy, precision/recall, Brier score and probability calibration buckets. Chronological 60/20/20 train/validation/OOS diagnostics are exposed, but **no fitted model, parameter search or independently validated OOS promotion is implemented**. Therefore walk-forward research is not yet ready to certify an edge.

Directional shadow signals observe public sampled books and aggTrades at 1/2/5/10/30/60 seconds from the actual decision timestamp. Touch and strict trade-through use aggressive trades on the opposite side of the proposed maker order. Missing continuous coverage, reconnect, trade-ID gap, restart, event queue overflow or missing book quote causes a censored observation, never a synthetic zero. Reported maker fill stress is only a trade-through-rate proxy; real queue fills remain unknown.

## Storage and API

`directional_experiments`, `directional_models`, `directional_predictions`, `directional_decisions`, `directional_positions`, `directional_orders`, `directional_trades`, `directional_equity`, `directional_labels`, `directional_edge_classes`, `directional_statistics`, `directional_monte_carlo`, `directional_calibration`, `directional_regime_stats`, `directional_shadow`, `directional_shadow_observations`, `directional_model_rankings`, `directional_candidates`, `directional_state`. Some research tables are reserved for future evaluated model generations and remain empty; they are not evidence of implemented automated model search.

`GET /api/directional` returns the separate account, decisions, exposures, open positions, actual closed PAPER trades, statistics, calibration and shadow coverage. The dashboard section is read-only. No mutation endpoint exists.

Run tests: `python -m unittest discover -s tests -q`.

## Remaining research limitations

- Uncalibrated direction probabilities and assumed conditional move sizes can misstate EV. Edge classification uses actual PAPER closed trades, not model EV.
- Candle trade-through is not a real maker fill; 1% volume participation is a conservative proxy, not exchange queue simulation. No historical funding rates or real exchange rebates are imported.
- Position exits have taker spreads from the current public quote when available; historical replay uses the configured maximum spread. Exact tick path and stop gap execution are unknown.
- The server evaluates one active configuration at a time. The optional directional modes do not create a parallel Adaptive Experiment Matrix with independent accounts. Automatically generated variants and full walk-forward model promotion are not implemented. Parallel comparisons require separate capital, execution ledgers and out-of-sample validation before claiming readiness.
- Monte Carlo resamples observed trades independently. Correlated market regimes and future distribution shifts can make its estimates optimistic.
