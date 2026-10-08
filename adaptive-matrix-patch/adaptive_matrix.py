"""Adaptive Experiment Matrix — PAPER ONLY.

Runs parameter variants on the SAME histories/ranking snapshot already collected by
AdaptiveRunner. It never imports adaptive_live and never performs network requests.
"""
from __future__ import annotations
import copy
import json
import math
import sqlite3
import statistics as stats
import time
from pathlib import Path

from adaptive import config_hash
from adaptive_portfolio import PaperPortfolio


def load_matrix_config(path=None):
    p = Path(path or Path(__file__).with_name("adaptive_matrix_config.json"))
    cfg = json.loads(p.read_text(encoding="utf-8"))
    if not cfg.get("ENABLED", True):
        return cfg
    if not 1 <= int(cfg["MAX_ACTIVE_VARIANTS"]) <= 40:
        raise ValueError("MAX_ACTIVE_VARIANTS must be 1..40")
    return cfg


def _variant(base, vid, name, family, **changes):
    cfg = copy.deepcopy(base)
    cfg.update(changes)
    return {
        "variantId": vid,
        "name": name,
        "family": family,
        "generation": 0,
        "parentVariantId": None,
        "config": cfg,
        "configHash": config_hash(cfg),
    }


def build_generation_zero(base):
    """Curated parameter regions, not a Cartesian product."""
    v = []
    add = lambda vid, name, family, **kw: v.append(_variant(base, vid, name, family, **kw))
    add("AMR-V001", "CONTROL", "CONTROL")

    for i, z in enumerate((-1.2, -1.5, -1.8, -2.3, -2.6), 2):
        add(f"AMR-V{i:03d}", f"Z {z:g}", "DEVIATION",
            Z_RETURN_ENTRY=z, PRICE_Z_ENTRY=z)

    for i, edge in enumerate((1.2, 1.4, 1.6, 2.5, 3.0), 7):
        add(f"AMR-V{i:03d}", f"Edge {edge:g}x", "EDGE",
            MIN_EDGE_MULTIPLIER=edge)

    for i, act in enumerate((50, 60, 80, 90), 12):
        add(f"AMR-V{i:03d}", f"Activity p{act}", "ACTIVITY",
            MIN_ACTIVITY_PERCENTILE=act)

    for i, hold in enumerate((5, 10, 15, 30), 16):
        add(f"AMR-V{i:03d}", f"Hold {hold}m", "EXIT",
            MAX_HOLD_MINUTES=hold)

    for i, ttl in enumerate((2, 3), 20):
        add(f"AMR-V{i:03d}", f"Maker TTL {ttl}m", "EXECUTION",
            ORDER_TTL_MINUTES=ttl)

    for i, atr in enumerate((1.0, 1.25, 1.75, 2.0), 22):
        add(f"AMR-V{i:03d}", f"ATR stop {atr:g}", "STOP",
            ATR_STOP_MULTIPLIER=atr)

    for i, risk in enumerate((0.002, 0.004, 0.005), 26):
        add(f"AMR-V{i:03d}", f"Risk {risk*100:.1f}%", "RISK",
            RISK_PER_TRADE=risk)

    add("AMR-V029", "Broader balanced", "COMBO",
        Z_RETURN_ENTRY=-1.5, PRICE_Z_ENTRY=-1.5,
        MIN_EDGE_MULTIPLIER=1.6, MIN_ACTIVITY_PERCENTILE=60,
        MAX_HOLD_MINUTES=10, ATR_STOP_MULTIPLIER=1.25)

    add("AMR-V030", "Strict quality", "COMBO",
        Z_RETURN_ENTRY=-2.3, PRICE_Z_ENTRY=-2.3,
        MIN_EDGE_MULTIPLIER=2.5, MIN_ACTIVITY_PERCENTILE=80,
        MAX_HOLD_MINUTES=20, ATR_STOP_MULTIPLIER=1.5)

    return v


def _concentration(values):
    vals = [abs(float(x)) for x in values if x is not None]
    total = sum(vals)
    return max(vals) / total if total else 1.0


def _stability(metrics):
    br = metrics.get("breakdown") or {}
    symbol = _concentration((br.get("symbol") or {}).values())
    regime = _concentration((br.get("volatilityRegime") or {}).values())
    # 1 = diversified/stable, 0 = concentrated.
    return max(0.0, min(1.0, 1.0 - 0.55*symbol - 0.30*regime))


def _score(metrics, cfg):
    n = int(metrics.get("trades") or 0)
    exp = metrics.get("expectancyNet")
    if exp is None:
        return {
            "score": 0.0, "sampleFactor": 0.0, "stabilityFactor": 0.0,
            "drawdownFactor": 1.0, "profitFactorFactor": 0.0,
            "symbolConcentration": 1.0, "regimeConcentration": 1.0
        }
    sample = min(1.0, math.sqrt(n / max(1, cfg["TARGET_TRADES"])))
    stability = _stability(metrics)
    maxdd = float(metrics.get("maxDrawdown") or 0.0)
    drawdown_factor = 1.0 / (1.0 + maxdd / max(0.01, cfg["MAX_VARIANT_DRAWDOWN_PCT"]))
    pf = metrics.get("profitFactor")
    pf_factor = 1.0 if pf is None and n else max(0.0, min(1.5, float(pf)/2.0))
    expectancy_norm = max(0.0, float(exp)) / max(1e-9, cfg["STARTING_CAPITAL"])
    score = expectancy_norm * sample * stability * drawdown_factor * pf_factor * 1_000_000
    br = metrics.get("breakdown") or {}
    return {
        "score": score,
        "sampleFactor": sample,
        "stabilityFactor": stability,
        "drawdownFactor": drawdown_factor,
        "profitFactorFactor": pf_factor,
        "symbolConcentration": _concentration((br.get("symbol") or {}).values()),
        "regimeConcentration": _concentration((br.get("volatilityRegime") or {}).values()),
    }


def _status(metrics, score_parts, cfg):
    n = int(metrics.get("trades") or 0)
    exp = metrics.get("expectancyNet")
    pf = metrics.get("profitFactor")
    dd = float(metrics.get("maxDrawdown") or 0.0)
    if n == 0:
        return "WARMUP"
    if n < cfg["MIN_TRADES_FOR_ELIMINATION"]:
        return "INSUFFICIENT_SAMPLE"
    if exp is not None and exp <= 0:
        return "ELIMINATED"
    if dd > cfg["MAX_VARIANT_DRAWDOWN_PCT"]:
        return "ELIMINATED"
    if pf is not None and pf <= cfg["MIN_PROFIT_FACTOR"]:
        return "ELIMINATED"
    if n < cfg["MIN_TRADES_FOR_PROMISING"]:
        return "COLLECTING"
    if score_parts["stabilityFactor"] < 0.25:
        return "UNSTABLE"
    return "PROMISING"


def _adjusted_expectancy(trades, cost_multiplier):
    pnl = []
    for t in trades:
        gross = float(t.get("grossPnL") or 0)
        costs = float(t.get("fees") or 0) + float(t.get("spreadCost") or 0) + float(t.get("slippageCost") or 0)
        pnl.append(gross - costs*cost_multiplier)
    return stats.mean(pnl) if pnl else None


class AdaptiveMatrix:
    def __init__(self, directory, base_config):
        self.directory = Path(directory)
        self.cfg = load_matrix_config()
        self.base = copy.deepcopy(base_config)
        self.root = self.directory / "adaptive_matrix"
        self.root.mkdir(parents=True, exist_ok=True)
        self.meta = sqlite3.connect(self.directory/"adaptive_matrix.sqlite3", check_same_thread=False, timeout=30)
        self.meta.row_factory = sqlite3.Row
        self.meta.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS matrix_variants(
                variant_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                family TEXT NOT NULL,
                generation INTEGER NOT NULL,
                config_hash TEXT NOT NULL,
                config_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS matrix_rankings(
                time INTEGER NOT NULL,
                variant_id TEXT NOT NULL,
                rank INTEGER NOT NULL,
                score REAL NOT NULL,
                metrics_json TEXT NOT NULL,
                PRIMARY KEY(time, variant_id)
            );
            CREATE TABLE IF NOT EXISTS matrix_champion_history(
                time INTEGER PRIMARY KEY,
                variant_id TEXT,
                score REAL
            );
        """)
        self.variants = {}
        self.last_cycle = {"matrixCycleMs": None, "variantEvaluationMs": None, "variantsProcessed": 0}
        defs = build_generation_zero(self.base)[:int(self.cfg["MAX_ACTIVE_VARIANTS"])]
        for d in defs:
            row = self.meta.execute("SELECT config_hash,status FROM matrix_variants WHERE variant_id=?", (d["variantId"],)).fetchone()
            if row and row["config_hash"] != d["configHash"]:
                raise ValueError(f"Matrix variant config changed for {d['variantId']}; create a new variant ID")
            status = row["status"] if row else "WARMUP"
            if not row:
                with self.meta:
                    self.meta.execute(
                        "INSERT INTO matrix_variants VALUES(?,?,?,?,?,?,?,?)",
                        (d["variantId"], d["name"], d["family"], d["generation"], d["configHash"],
                         json.dumps(d["config"], sort_keys=True), status, int(time.time()*1000))
                    )
            d["status"] = status
            d["portfolio"] = PaperPortfolio(self.root/f"{d['variantId']}.sqlite3", d["config"])
            self.variants[d["variantId"]] = d

    def process(self, histories, ranking, now, context_ok=True, manual_kill=False):
        if not self.cfg.get("ENABLED", True):
            return
        start = time.perf_counter()
        processed = 0
        variant_ms = 0.0
        for vid in sorted(self.variants):
            d = self.variants[vid]
            if d["status"] == "ELIMINATED":
                continue
            r = copy.deepcopy(ranking)
            for i, row in enumerate(r):
                row["top"] = i < int(d["config"]["TOP_N"])
            t0 = time.perf_counter()
            d["portfolio"].process(histories, r, now, context_ok=context_ok, manual_kill=manual_kill)
            variant_ms += (time.perf_counter()-t0)*1000
            processed += 1
        self.last_cycle = {
            "matrixCycleMs": round((time.perf_counter()-start)*1000, 2),
            "variantEvaluationMs": round(variant_ms, 2),
            "variantsProcessed": processed,
        }
        self._persist_ranking(now)

    def _variant_view(self, d):
        snap = d["portfolio"].snapshot()
        metrics = snap["metrics"]
        parts = _score(metrics, self.cfg)
        status = _status(metrics, parts, self.cfg)
        if d["status"] != "ELIMINATED" and status == "ELIMINATED":
            d["status"] = "ELIMINATED"
            with self.meta:
                self.meta.execute("UPDATE matrix_variants SET status=? WHERE variant_id=?", (status, d["variantId"]))
        else:
            d["status"] = status if d["status"] != "ELIMINATED" else d["status"]
        trades = snap.get("trades") or []
        stress = {
            "base": metrics.get("expectancyNet"),
            "costPlus25": _adjusted_expectancy(trades, 1.25),
            "costPlus50": _adjusted_expectancy(trades, 1.50),
            "costPlus100": _adjusted_expectancy(trades, 2.00),
        }
        return {
            "variantId": d["variantId"], "name": d["name"], "family": d["family"],
            "generation": d["generation"], "configHash": d["configHash"], "status": d["status"],
            "config": d["config"], "cash": snap["cash"], "equity": snap["equity"],
            "positions": len(snap["positions"]), "pending": len(snap["pending"]),
            "metrics": metrics, "scoreParts": parts, "score": parts["score"], "stress": stress,
        }

    def _views(self):
        rows = [self._variant_view(d) for d in self.variants.values()]
        rows.sort(key=lambda x: (-x["score"], x["variantId"]))
        for i, row in enumerate(rows, 1):
            row["rank"] = i
        return rows

    @staticmethod
    def _pareto(rows):
        out = []
        for a in rows:
            ma = a["metrics"]
            ea = ma.get("expectancyNet")
            ta = ma.get("tradesPerHour")
            da = ma.get("maxDrawdown")
            if ea is None or ta is None or da is None:
                continue
            dominated = False
            for b in rows:
                if a is b:
                    continue
                mb = b["metrics"]
                eb, tb, db = mb.get("expectancyNet"), mb.get("tradesPerHour"), mb.get("maxDrawdown")
                if eb is None or tb is None or db is None:
                    continue
                if eb >= ea and tb >= ta and db <= da and (eb > ea or tb > ta or db < da):
                    dominated = True
                    break
            if not dominated:
                out.append(a["variantId"])
        return out

    def _persist_ranking(self, now):
        rows = self._views()
        with self.meta:
            for r in rows:
                self.meta.execute(
                    "INSERT OR REPLACE INTO matrix_rankings VALUES(?,?,?,?,?)",
                    (now, r["variantId"], r["rank"], r["score"], json.dumps(r["metrics"], allow_nan=False))
                )
            champion = rows[0] if rows else None
            self.meta.execute(
                "INSERT OR REPLACE INTO matrix_champion_history VALUES(?,?,?)",
                (now, champion["variantId"] if champion else None, champion["score"] if champion else 0.0)
            )

    def snapshot(self):
        rows = self._views()
        frontier = self._pareto(rows)
        champion = rows[0]["variantId"] if rows else None
        return {
            "mode": "PAPER",
            "liveReady": False,
            "generation": int(self.cfg["GENERATION"]),
            "variantCount": len(rows),
            "champion": champion,
            "paretoFront": frontier,
            "cycle": dict(self.last_cycle),
            "variants": rows,
        }

    def variant_snapshot(self, variant_id):
        d = self.variants.get(variant_id)
        return self._variant_view(d) if d else None

    def frontier_snapshot(self):
        s = self.snapshot()
        ids = set(s["paretoFront"])
        return {"paretoFront": [v for v in s["variants"] if v["variantId"] in ids]}

    def stress_snapshot(self):
        return {"variants": [
            {"variantId": v["variantId"], "status": v["status"], "stress": v["stress"]}
            for v in self._views()
        ]}
