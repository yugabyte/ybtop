"""File-based latency-histogram merge, delta, ranking, FDR and template grouping.

Pure-Python (no numpy/scipy) so the raw data pipeline works without the optional
``[histogram]`` extra. The actual multimodality detection lives in
``histogram_detect.py`` and is the only part that needs numpy/scipy/diptest.

Snapshots store, per node, a list of rows shaped as::

    {"queryid", "query", "dbname", "calls", "yb_latency_histogram": {bucket_label: count}}

where ``yb_latency_histogram`` is already the canonical flat map produced by
``snapshot_write.normalize_latency_histogram``. Cumulative mode uses the merged current
snapshot as-is; delta mode subtracts the prior snapshot's per-bucket counts (same pattern
as the pg_stat_statements delta), so no database or snapshot tables are required.
"""

from __future__ import annotations

import re
from typing import Any, Optional

# Confidence tier ladder (mirrors the standalone yb_histogram_multimodality.py).
CONFIDENCE_TIER_RANK: dict[str, int] = {
    "not_flagged": 0,
    "unconfirmed": 1,
    "moderate": 2,
    "high": 3,
    "very_high": 4,
}

CONFIDENCE_TIER_DESCRIPTIONS: dict[str, str] = {
    "not_flagged": "no multimodality detected",
    "unconfirmed": "flagged by shape, dip test unavailable",
    "moderate": "dip test significant (p <= 0.05)",
    "high": "dip test strong (p <= 0.01)",
    "very_high": "dip test very strong (p <= 0.001)",
}

MIN_TIER_CHOICES: tuple[str, ...] = ("very_high", "high", "moderate", "unconfirmed", "all")


# --------------------------------------------------------------------------------------
# Snapshot document helpers
# --------------------------------------------------------------------------------------
def latency_histograms_per_node_lists(doc: Optional[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    if not doc:
        return []
    section = (doc.get("latency_histograms") or {}).get("per_node")
    if not isinstance(section, dict):
        return []
    return [v for v in section.values() if isinstance(v, list)]


def has_latency_histogram_data(doc: Optional[dict[str, Any]]) -> bool:
    for rows in latency_histograms_per_node_lists(doc):
        if rows:
            return True
    return False


# --------------------------------------------------------------------------------------
# Merge across nodes / delta across snapshots
# --------------------------------------------------------------------------------------
def _merge_key(r: dict[str, Any]) -> tuple[str, str]:
    qid = str(r.get("queryid") or "")
    db = r.get("dbname")
    dbn = "" if db is None else str(db).strip()
    return (qid, dbn)


def _coerce_buckets(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for label, cnt in raw.items():
        try:
            c = int(cnt)
        except (TypeError, ValueError):
            continue
        if c:
            out[str(label)] = out.get(str(label), 0) + c
    return out


def merge_latency_histograms(
    per_node: list[list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Sum per-bucket counts and call totals across nodes, keyed by (queryid, dbname)."""
    acc: dict[tuple[str, str], dict[str, Any]] = {}
    for rows in per_node:
        for r in rows or []:
            mk = _merge_key(r)
            if mk not in acc:
                acc[mk] = {
                    "queryid": mk[0],
                    "dbname": mk[1] or None,
                    "query": r.get("query"),
                    "calls": 0,
                    "buckets": {},
                }
            a = acc[mk]
            a["calls"] += int(r.get("calls") or 0)
            for label, cnt in _coerce_buckets(r.get("yb_latency_histogram")).items():
                a["buckets"][label] = a["buckets"].get(label, 0) + cnt
            if not a.get("query") and r.get("query"):
                a["query"] = r.get("query")
    out = list(acc.values())
    out.sort(key=lambda x: int(x.get("calls") or 0), reverse=True)
    return out


def delta_latency_histograms(
    cur_merged: list[dict[str, Any]], prev_merged: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Per-key Δ between two merged result sets.

    Subtracts prior per-bucket counts; negative residual buckets are clamped to 0 (counter
    reset / statement rotation safety, mirroring the call-rate chart clamping). Keys with no
    positive residual buckets are dropped.
    """
    prev_map = {_merge_key(r): r for r in (prev_merged or [])}
    out: list[dict[str, Any]] = []
    for cur in cur_merged or []:
        p = prev_map.get(_merge_key(cur))
        cur_buckets = cur.get("buckets") or {}
        prev_buckets = (p.get("buckets") or {}) if p else {}
        d_buckets: dict[str, int] = {}
        for label, c in cur_buckets.items():
            d = int(c) - int(prev_buckets.get(label, 0) or 0)
            if d > 0:
                d_buckets[label] = d
        if not d_buckets:
            continue
        d_calls = int(cur.get("calls") or 0) - (int(p.get("calls") or 0) if p else 0)
        out.append(
            {
                "queryid": cur.get("queryid"),
                "dbname": cur.get("dbname"),
                "query": cur.get("query"),
                "calls": max(0, d_calls),
                "buckets": d_buckets,
            }
        )
    out.sort(key=lambda x: int(x.get("calls") or 0), reverse=True)
    return out


# --------------------------------------------------------------------------------------
# Bucket-label parsing
# --------------------------------------------------------------------------------------
# Matches ``[lo,hi)`` bucket labels. The high group is optional so the open-ended overflow
# bucket ``[max_latency,)`` (queries beyond YugabyteDB's top HDR bound) is recognized and
# reported separately rather than given a synthetic finite upper bound.
_BUCKET_RE = re.compile(
    r"[\[\(]\s*([+-]?[0-9]*\.?[0-9]+(?:[eE][+-]?[0-9]+)?)\s*,\s*"
    r"([+-]?[0-9]*\.?[0-9]+(?:[eE][+-]?[0-9]+)?)?\s*[\)\]]"
)


def parse_histogram_buckets(
    hist: dict[str, Any],
) -> tuple[list[tuple[float, float, int]], int]:
    """Parse a flat ``{bucket_label: count}`` map into sorted ``(lo, hi, count)`` tuples.

    Returns ``(buckets, overflow_count)``. The open-ended overflow bucket ``[max,)`` has no
    finite width and cannot be peak-found, so its count is summed into ``overflow_count`` and
    excluded from ``buckets`` (mirroring the reference ``parse_histogram``). Zero-count and
    unparseable labels are dropped. Buckets are sorted ascending by lower bound.
    """
    buckets: list[tuple[float, float, int]] = []
    overflow_count = 0
    for label, cnt in (hist or {}).items():
        try:
            c = int(cnt)
        except (TypeError, ValueError):
            continue
        if c == 0:
            continue
        m = _BUCKET_RE.search(str(label))
        if not m:
            continue
        try:
            lo = float(m.group(1))
        except (TypeError, ValueError):
            continue
        hi_raw = m.group(2)
        if hi_raw is None or hi_raw == "":  # overflow bucket "[max_latency,)"
            overflow_count += c
            continue
        try:
            hi = float(hi_raw)
        except (TypeError, ValueError):
            continue
        buckets.append((lo, hi, c))
    buckets.sort(key=lambda b: b[0])
    return buckets, overflow_count


# --------------------------------------------------------------------------------------
# Confidence tiers, ranking, BH-FDR, template grouping
# --------------------------------------------------------------------------------------
def confidence_tier(result: dict[str, Any]) -> str:
    if not result.get("flag"):
        return "not_flagged"
    dip_p = result.get("dip_p")
    if dip_p is None:
        return "unconfirmed"
    if dip_p <= 0.001:
        return "very_high"
    if dip_p <= 0.01:
        return "high"
    return "moderate"


def rank_by_confidence(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Assign ``confidence_rank`` (1 = strongest) in place; returns the ordered list.

    Primary signal: ``dip_p`` ascending (smaller p = stronger evidence against unimodality;
    rows without a dip p-value sort to the back at 1.0). Tiebreaker: ``bc`` descending, which
    still discriminates when several rows share a dip_p pinned at the 0.0 table floor. This
    matches the reference ``_confidence_sort_key``.
    """

    def sort_key(r: dict[str, Any]) -> tuple[float, float]:
        dip = r.get("dip_p")
        dip_key = float(dip) if dip is not None else 1.0
        return (dip_key, -float(r.get("bc") or 0.0))

    ordered = sorted(results, key=sort_key)
    for i, r in enumerate(ordered, start=1):
        r["confidence_rank"] = i
    return ordered


def apply_bh_correction(results: list[dict[str, Any]], q: float = 0.05) -> dict[str, Any]:
    """Benjamini-Hochberg step-up across all rows carrying a dip p-value (the true family).

    Adds ``dip_p_bh`` (monotone step-up adjusted p-value), ``bh_rank``, ``bh_m``,
    ``bh_significant`` and downgrades any flagged row that fails the batch-adjusted threshold
    to ``flag=False, reason='fdr_not_significant'``. The adjusted p-values are computed via a
    running minimum from largest rank down to smallest so they are monotone non-decreasing in
    rank (standard BH step-up), matching the reference implementation.
    """
    family = [r for r in results if r.get("dip_p") is not None]
    m = len(family)
    if m == 0:
        return {"m": 0, "q": q, "downgraded": 0}
    family.sort(key=lambda r: float(r["dip_p"]))
    raw_ps = [float(r["dip_p"]) for r in family]

    adjusted = [0.0] * m
    running_min = 1.0
    for i in range(m - 1, -1, -1):
        candidate = raw_ps[i] * m / (i + 1)
        running_min = min(running_min, candidate)
        adjusted[i] = min(running_min, 1.0)

    downgraded = 0
    for i, r in enumerate(family):
        r["bh_rank"] = i + 1
        r["bh_m"] = m
        r["dip_p_bh"] = float(adjusted[i])
        sig = adjusted[i] <= q
        r["bh_significant"] = sig
        if not sig and r.get("flag"):
            r["flag"] = False
            r["reason"] = "fdr_not_significant"
            r["confidence_tier"] = confidence_tier(r)
            downgraded += 1
    return {"m": m, "q": q, "downgraded": downgraded}


_REWRITE_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_IN_LIST_RE = re.compile(r"\bIN\s*\([^)]*\)", re.I)
_PLACEHOLDER_RE = re.compile(r"\$\d+")
_WS_RE = re.compile(r"\s+")


def normalize_query_template(query: Optional[str]) -> str:
    """Collapse a query into a template that ignores per-call comments and IN-list arity.

    1. Strip embedded per-call comments (e.g. ``/*rewritten_pid='123'*/``).
    2. Collapse any ``IN (...)`` list (literals or ``$N``) to a canonical ``IN (...)``.
    3. Collapse remaining ``$N`` placeholders (positions shift after step 2) to ``$N``.
    4. Collapse whitespace.

    Table/column names are preserved, so different predicate/projection columns stay distinct.
    """
    if not query:
        return ""
    q = _REWRITE_COMMENT_RE.sub("", str(query))
    q = _IN_LIST_RE.sub("IN (...)", q)
    q = _PLACEHOLDER_RE.sub("$N", q)
    q = _WS_RE.sub(" ", q).strip()
    return q


def group_by_query_template(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group rows by normalized template; mutate rows in place, return group summaries."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        key = normalize_query_template(r.get("query"))
        r["query_template"] = key
        groups.setdefault(key, []).append(r)

    summaries: list[dict[str, Any]] = []
    for key, members in groups.items():
        members.sort(key=lambda r: int(r.get("confidence_rank") or 10**9))
        for rank, r in enumerate(members, start=1):
            r["template_rank"] = rank
            r["template_member_count"] = len(members)
        best = members[0]
        peak_counts = sorted(
            {int(r["n_raw_peaks"]) for r in members if r.get("n_raw_peaks")}
        )
        # Adjacent peak-pair gaps (ms / ratio) across all pairs of every member that
        # reached the peak-pair stage — used for cross-member range summaries.
        gap_ms_vals: list[float] = []
        gap_ratio_vals: list[float] = []
        for r in members:
            for pp in r.get("peak_pairs") or []:
                if pp.get("gap_ms") is not None:
                    gap_ms_vals.append(float(pp["gap_ms"]))
                if pp.get("gap_ratio") is not None:
                    gap_ratio_vals.append(float(pp["gap_ratio"]))
        summaries.append(
            {
                "template": key,
                "member_count": len(members),
                "best_confidence_rank": int(best.get("confidence_rank") or 0),
                "best_confidence_tier": best.get("confidence_tier", "not_flagged"),
                "queryids": [r.get("queryid") for r in members],
                "peak_counts": peak_counts,
                # Best member's full adjacent-pair list (display); ranges span every pair.
                "peak_pairs": list(best.get("peak_pairs") or []),
                "gap_ms_range": [min(gap_ms_vals), max(gap_ms_vals)] if gap_ms_vals else None,
                "gap_ratio_range": (
                    [min(gap_ratio_vals), max(gap_ratio_vals)] if gap_ratio_vals else None
                ),
            }
        )
    summaries.sort(key=lambda g: int(g.get("best_confidence_rank") or 10**9))
    return summaries
