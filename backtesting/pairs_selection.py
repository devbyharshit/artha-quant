"""Pair candidate generation for pairs trading.

Two independent methods — same-sector return correlation, and Engle-Granger
cointegration on log prices — both restricted to same-sector symbol pairs
(the economic-linkage rationale behind the user's own example pairs, e.g.
ONGC/IOC, ICICIBANK/AXISBANK) and computed on an in-sample window only, so
selection can never see the data it will later be judged on.

`Personal Holding` in configs/universe.yaml is a mixed bag (ETFs, gold, and
individually-added holdings) rather than a real economic sector, so it is
excluded from both methods — pairing across it would inject spurious
candidates for the wrong reason (see docs/09-pairs-trading-plan.md §3.1).

Candidates are de-duplicated greedily by selection-metric strength so no
symbol appears in more than one surviving pair: that keeps every pair's
capital and price risk independent, which matters for both position sizing
and for not overstating the pooled portfolio's apparent diversification.
"""
import itertools
from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from statsmodels.tsa.stattools import coint

EXCLUDED_SECTORS = {"Personal Holding"}
MIN_OVERLAP_BARS = 60


@dataclass
class PairCandidate:
    symbol_a: str
    symbol_b: str
    sector: str
    method: str
    metric: float  # correlation coefficient (higher is stronger), or cointegration p-value (lower is stronger)


def _same_sector_pairs(sector_by_symbol: Dict[str, str], symbols: List[str]):
    by_sector: Dict[str, List[str]] = {}
    for s in symbols:
        sector = sector_by_symbol.get(s)
        if sector is None or sector in EXCLUDED_SECTORS:
            continue
        by_sector.setdefault(sector, []).append(s)
    for sector, members in by_sector.items():
        for a, b in itertools.combinations(sorted(members), 2):
            yield a, b, sector


def _dedupe_by_symbol(candidates: List[PairCandidate]) -> List[PairCandidate]:
    used = set()
    kept = []
    for c in candidates:
        if c.symbol_a in used or c.symbol_b in used:
            continue
        kept.append(c)
        used.add(c.symbol_a)
        used.add(c.symbol_b)
    return kept


def select_correlation_pairs(
    data: Dict[str, pd.DataFrame],
    sector_by_symbol: Dict[str, str],
    start: Optional[pd.Timestamp] = None,
    end: Optional[pd.Timestamp] = None,
    min_correlation: float = 0.60,
) -> List[PairCandidate]:
    candidates = []
    for a, b, sector in _same_sector_pairs(sector_by_symbol, list(data)):
        ra = data[a]["close"].loc[start:end].pct_change().dropna()
        rb = data[b]["close"].loc[start:end].pct_change().dropna()
        aligned = pd.concat([ra, rb], axis=1, join="inner").dropna()
        if len(aligned) < MIN_OVERLAP_BARS:
            continue
        corr = float(aligned.iloc[:, 0].corr(aligned.iloc[:, 1]))
        if corr >= min_correlation:
            candidates.append(PairCandidate(a, b, sector, "correlation", corr))
    candidates.sort(key=lambda c: -c.metric)
    return _dedupe_by_symbol(candidates)


def select_cointegration_pairs(
    data: Dict[str, pd.DataFrame],
    sector_by_symbol: Dict[str, str],
    start: Optional[pd.Timestamp] = None,
    end: Optional[pd.Timestamp] = None,
    max_pvalue: float = 0.05,
) -> List[PairCandidate]:
    candidates = []
    for a, b, sector in _same_sector_pairs(sector_by_symbol, list(data)):
        la = np.log(data[a]["close"].loc[start:end])
        lb = np.log(data[b]["close"].loc[start:end])
        aligned = pd.concat([la, lb], axis=1, join="inner").dropna()
        if len(aligned) < MIN_OVERLAP_BARS:
            continue
        _, pvalue, _ = coint(aligned.iloc[:, 0], aligned.iloc[:, 1])
        if pvalue <= max_pvalue:
            candidates.append(PairCandidate(a, b, sector, "cointegration", float(pvalue)))
    candidates.sort(key=lambda c: c.metric)
    return _dedupe_by_symbol(candidates)
