"""Latency-histogram multimodality analysis for snapshot files and watch sidecars.

No database access — operates on the file-based snapshots written by ``ybtop watch``
(cumulative counters) and computes deltas by subtracting consecutive snapshots.
``build_analysis_artifact`` powers ``watch --snapshot-latency-analysis`` sidecars for the
browser Latency modes tab.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ybtop import __version__
from ybtop.histogram import (
    CONFIDENCE_TIER_RANK,
    apply_bh_correction,
    delta_latency_histograms,
    group_by_query_template,
    has_latency_histogram_data,
    latency_histograms_per_node_lists,
    merge_latency_histograms,
    rank_by_confidence,
)
from ybtop.histogram_detect import (
    analyze_histogram_row,
    diptest_available,
)
from ybtop.snapshot_write import load_snapshot_json, read_manifest_entries


class HistogramAnalysisError(RuntimeError):
    """User-facing analysis failure (missing data, bad selection, etc.)."""


def _resolve_index(
    entries: list[dict[str, Any]], *, index: Optional[int], snapshot: Optional[str]
) -> int:
    n = len(entries)
    if snapshot:
        matches = [i for i, e in enumerate(entries) if snapshot in str(e.get("file", ""))]
        if not matches:
            raise HistogramAnalysisError(f"No snapshot in manifest matches '{snapshot}'.")
        return matches[-1]
    if index is None:
        return n - 1
    if index < 0:
        idx = n + index
    else:
        idx = index - 1  # 1-based
    if idx < 0 or idx >= n:
        raise HistogramAnalysisError(
            f"Snapshot index {index} out of range (1..{n})."
        )
    return idx


def analyze(
    data_dir: str,
    *,
    mode: str = "auto",
    index: Optional[int] = None,
    snapshot: Optional[str] = None,
    min_calls: int = 30,
    min_tier: str = "high",
    fdr_correct: bool = True,
    fdr_q: float = 0.05,
    flagged_only: bool = False,
) -> dict[str, Any]:
    out_dir = Path(data_dir)
    entries = read_manifest_entries(out_dir)
    if not entries:
        raise HistogramAnalysisError(
            f"No ybtop.manifest.json entries found in {out_dir.resolve()}."
        )
    idx = _resolve_index(entries, index=index, snapshot=snapshot)
    cur_entry = entries[idx]
    cur_doc = load_snapshot_json(out_dir, str(cur_entry.get("file")))
    if cur_doc is None:
        raise HistogramAnalysisError(f"Could not read snapshot {cur_entry.get('file')}.")
    if not has_latency_histogram_data(cur_doc):
        raise HistogramAnalysisError(
            "Selected snapshot has no latency_histograms section. Re-run watch with "
            "--snapshot-latency-histograms (requires a cluster exposing yb_latency_histogram)."
        )

    prev_doc: Optional[dict[str, Any]] = None
    if idx > 0:
        prev_doc = load_snapshot_json(out_dir, str(entries[idx - 1].get("file")))

    prev_has_data = has_latency_histogram_data(prev_doc)
    if mode == "auto":
        effective_mode = "delta" if prev_has_data else "cumulative"
    else:
        effective_mode = mode
    if effective_mode == "delta" and not prev_has_data:
        effective_mode = "cumulative"

    cur_merged = merge_latency_histograms(latency_histograms_per_node_lists(cur_doc))
    if effective_mode == "delta":
        prev_merged = merge_latency_histograms(latency_histograms_per_node_lists(prev_doc))
        rows = delta_latency_histograms(cur_merged, prev_merged)
    else:
        rows = cur_merged

    results = [analyze_histogram_row(r, min_calls=min_calls) for r in rows]

    fdr_info: Optional[dict[str, Any]] = None
    if fdr_correct:
        fdr_info = apply_bh_correction(results, q=fdr_q)

    # Filter first, then rank/group over just the displayed rows (matches the reference
    # ordering, so ranks and template rollups describe what's actually shown).
    display = _filter_results(results, min_tier=min_tier, flagged_only=flagged_only)
    display = rank_by_confidence(display)
    groups = group_by_query_template(display)

    n_distinct = len(groups)
    n_recurring = sum(1 for g in groups if int(g.get("member_count") or 0) > 1)

    return {
        "mode": effective_mode,
        "min_calls": min_calls,
        "min_tier": min_tier,
        "flagged_only": flagged_only,
        "diptest_available": diptest_available(),
        "fdr": fdr_info,
        "snapshots": {
            "current": cur_entry.get("file"),
            "current_utc": cur_entry.get("utc"),
            "previous": entries[idx - 1].get("file") if effective_mode == "delta" else None,
            "previous_utc": entries[idx - 1].get("utc") if effective_mode == "delta" else None,
            "index": idx + 1,
            "count": len(entries),
        },
        "diag": {"distinct_templates": n_distinct, "recurring_templates": n_recurring},
        "total_rows": len(results),
        "results": display,
        "template_groups": groups,
    }


def _filter_results(
    results: list[dict[str, Any]], *, min_tier: str, flagged_only: bool
) -> list[dict[str, Any]]:
    out = list(results)
    if flagged_only:
        out = [r for r in out if r.get("flag")]
    if min_tier and min_tier != "all":
        threshold = CONFIDENCE_TIER_RANK.get(min_tier, 0)
        out = [
            r
            for r in out
            if CONFIDENCE_TIER_RANK.get(r.get("confidence_tier", "not_flagged"), 0) >= threshold
        ]
    return out


# --------------------------------------------------------------------------------------
# Precomputed analysis artifacts (sidecar files for offline / browser viewing)
# --------------------------------------------------------------------------------------
ANALYSIS_ARTIFACT_SCHEMA = 1


def build_analysis_artifact(
    data_dir: str,
    *,
    index: Optional[int] = None,
    snapshot: Optional[str] = None,
    min_calls: int = 30,
    fdr_q: float = 0.05,
) -> dict[str, Any]:
    """Precompute the full (dip-confirmed) latency report for one snapshot, for offline viewing.

    Returns a JSON-serializable artifact carrying both the ``cumulative`` report (this
    snapshot's totals) and the ``delta`` report (vs the immediately prior snapshot, or ``None``
    when there is no prior snapshot with histogram data). Both are emitted unfiltered
    (``min_tier=all``) so the browser keeps its interactive tier / flagged filters. Requires the
    ``[histogram]`` extra (raises :class:`~ybtop.histogram_detect.HistogramDepsError` otherwise).
    """
    cumulative = analyze(
        data_dir,
        mode="cumulative",
        index=index,
        snapshot=snapshot,
        min_calls=min_calls,
        min_tier="all",
        fdr_correct=True,
        fdr_q=fdr_q,
        flagged_only=False,
    )
    delta_report = analyze(
        data_dir,
        mode="delta",
        index=index,
        snapshot=snapshot,
        min_calls=min_calls,
        min_tier="all",
        fdr_correct=True,
        fdr_q=fdr_q,
        flagged_only=False,
    )
    # analyze() falls back to cumulative when no prior histogram data exists; only keep a genuine
    # delta so the viewer can tell "no prior" apart from a real Δ.
    delta = delta_report if delta_report.get("mode") == "delta" else None
    return {
        "schema": ANALYSIS_ARTIFACT_SCHEMA,
        "ybtop_version": __version__,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "diptest_available": diptest_available(),
        "params": {"min_calls": min_calls, "fdr_q": fdr_q},
        "cumulative": cumulative,
        "delta": delta,
    }
