# Adaptive Experiment Matrix patch

Run from the QuantLab AI project root:

```bash
python apply_adaptive_matrix.py
python -m unittest discover -s tests -v
```

The patch:
- adds 30 Generation-0 PAPER variants,
- uses the same Adaptive market snapshot (no extra Binance requests),
- isolates each variant's ledger/state,
- adds sample-size, stability and drawdown penalties,
- adds cost stress (+25%, +50%, +100%),
- computes a Pareto front for trades/hour vs expectancy vs drawdown,
- adds read-only Matrix API endpoints and a dashboard table,
- never imports or enables `adaptive_live.py`.

Backups of modified files are created as `*.before-matrix.bak`.
