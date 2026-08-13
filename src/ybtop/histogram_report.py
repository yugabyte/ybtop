"""``ybtop histogram`` orchestration: read snapshot files, detect latency multimodality.

No database access - operates purely on the file-based snapshots written by ``ybtop watch``
(cumulative counters) and computes deltas by subtracting consecutive snapshots.
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
    HistogramDepsError,
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
# Text formatting (mirrors the standalone yb_histogram_multimodality.py line shapes)
# --------------------------------------------------------------------------------------
def _fmt_num(v: Any, digits: int = 2) -> str:
    if v is None:
        return "?"
    try:
        return f"{float(v):.{digits}f}"
    except (TypeError, ValueError):
        return str(v)


def _format_peak_pair(pp: dict[str, Any]) -> str:
    """One adjacent mode split: 'p1->p2ms(xR)'."""
    s = f"{_fmt_num(pp.get('peak1_ms'))}->{_fmt_num(pp.get('peak2_ms'))}ms"
    if pp.get("gap_ratio") is not None:
        s += f"(x{_fmt_num(pp.get('gap_ratio'), 1)})"
    return s


def _format_peak_pairs(pairs: Any) -> str:
    """All valid adjacent mode splits, comma-separated (low→high latency)."""
    if not pairs:
        return ""
    return ", ".join(_format_peak_pair(pp) for pp in pairs)


def _spread_suffix(r: dict[str, Any]) -> str:
    """'peaks=.. spread=lo-hims(xR) gap=p1->p2ms(xGR)[, p2->p3ms(...)]' — every valid peak pair."""
    parts: list[str] = []
    n_peaks = r.get("n_raw_peaks")
    if n_peaks is not None:
        parts.append(f"peaks={n_peaks}")
    lo = r.get("latency_min_ms")
    hi = r.get("latency_max_ms")
    ratio = r.get("latency_spread_ratio")
    if lo is not None and hi is not None:
        spread = f"spread={_fmt_num(lo)}-{_fmt_num(hi)}ms"
        if ratio is not None:
            spread += f"(x{_fmt_num(ratio, 0)})"
        parts.append(spread)
    gap = _format_peak_pairs(r.get("peak_pairs"))
    if gap:
        parts.append(f"gap={gap}")
    return (" " + " ".join(parts)) if parts else ""


def _overflow_suffix(r: dict[str, Any]) -> str:
    if not r.get("overflow_flag"):
        return ""
    return (
        f" overflow={r.get('overflow_count')}"
        f"({_fmt_num((r.get('overflow_ratio') or 0) * 100, 1)}%)"
    )


def _dip_txt(r: dict[str, Any]) -> str:
    dip = r.get("dip_p")
    if dip is None:
        return "-"
    return _fmt_num(dip, 4)


def _row_line(r: dict[str, Any]) -> str:
    tag = "[FLAGGED]" if r.get("flag") else "[   ok  ]"
    tmpl = ""
    if r.get("template_rank") and r.get("template_member_count"):
        tmpl = f" tmpl={r['template_rank']}/{r['template_member_count']}"
    query = " ".join(str(r.get("query") or "").split())
    if len(query) > 160:
        query = query[:157] + "..."
    return (
        f"{tag} rank={r.get('confidence_rank')} tier={r.get('confidence_tier')} "
        f"queryid={r.get('queryid')} calls={r.get('calls')} "
        f"bc={_fmt_num(r.get('bc'), 4)} dip_p={_dip_txt(r)}"
        f"{_spread_suffix(r)}{_overflow_suffix(r)}{tmpl} query={query}"
    )


def _group_line(g: dict[str, Any]) -> str:
    peaks = ",".join(str(p) for p in (g.get("peak_counts") or [])) or "-"
    gap = ""
    pairs_txt = _format_peak_pairs(g.get("peak_pairs"))
    if pairs_txt:
        gap = f" gap={pairs_txt}"
    else:
        gr = g.get("gap_ms_range")
        ratio = g.get("gap_ratio_range")
        if gr:
            gtxt = _fmt_num(gr[0]) if gr[0] == gr[1] else f"{_fmt_num(gr[0])}-{_fmt_num(gr[1])}"
            rtxt = ""
            if ratio:
                rtxt = (
                    f"(x{_fmt_num(ratio[0], 1)})"
                    if ratio[0] == ratio[1]
                    else f"(x{_fmt_num(ratio[0], 1)}-{_fmt_num(ratio[1], 1)})"
                )
            gap = f" gap={gtxt}ms{rtxt}"
    tmpl = " ".join(str(g.get("template") or "").split())
    if len(tmpl) > 120:
        tmpl = tmpl[:117] + "..."
    return (
        f"[group] {g.get('member_count')}x best={g.get('best_confidence_tier')} "
        f"(rank {g.get('best_confidence_rank')}) peaks={peaks}{gap} "
        f"queryids={g.get('queryids')} template={tmpl}"
    )


def format_report_text(report: dict[str, Any]) -> str:
    lines: list[str] = []
    snaps = report.get("snapshots") or {}
    mode = report.get("mode")
    header = f"# ybtop histogram - mode={mode} snapshot={snaps.get('index')}/{snaps.get('count')}"
    header += f" file={snaps.get('current')}"
    if mode == "delta" and snaps.get("previous"):
        header += f" (delta vs {snaps.get('previous')})"
    lines.append(header)
    if not report.get("diptest_available"):
        lines.append(
            "[warn] diptest not installed - Stage 3 dip test skipped; shape-flagged rows "
            "are reported as 'unconfirmed'. Install ybtop[histogram] for confirmation."
        )
    fdr = report.get("fdr")
    if fdr:
        lines.append(
            f"[fdr] BH q={fdr.get('q')} family_m={fdr.get('m')} downgraded={fdr.get('downgraded')}"
        )
    diag = report.get("diag") or {}
    lines.append(
        f"[diag] {diag.get('distinct_templates')} distinct templates, "
        f"{diag.get('recurring_templates')} recur; total_rows={report.get('total_rows')}"
    )
    for g in report.get("template_groups") or []:
        if int(g.get("member_count") or 0) > 1 or int(g.get("best_confidence_rank") or 0):
            lines.append(_group_line(g))
    results = report.get("results") or []
    if not results:
        lines.append("(no rows match the current filters)")
    for r in results:
        lines.append(_row_line(r))
    return "\n".join(lines)


def run_histogram(*, data_dir: str, min_calls: int = 30) -> None:
    """`ybtop histogram`: text report of multimodal statements in the latest snapshot.

    Always analyzes the newest snapshot's cumulative totals, with FDR correction on (default q)
    and the default confidence-tier floor. Delta analysis and interactive filtering live in the
    browser's Latency modes tab; this command is the quick "is anything split right now?" check.
    Requires the ``[histogram]`` extra.
    """
    try:
        report = analyze(data_dir, mode="cumulative", min_calls=min_calls)
    except HistogramDepsError as exc:
        raise SystemExit(str(exc))
    except HistogramAnalysisError as exc:
        raise SystemExit(f"ybtop histogram: {exc}")
    print(format_report_text(report))


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
