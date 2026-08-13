"""Multimodality detection for latency histograms

Tiered pipeline in :func:`detect_modes`:

- Stage 0 - Prescreen: Sarle bimodality coefficient (BC); rows below ``bc_threshold`` dismissed.
- Stage 1 - Density reconstruction + peak finding on Gaussian-smoothed log2(ms) density.
- Stage 2 - Valley/significance check on raw counts (octave separation + valley ratio).
- Stage 3 - Confirmatory Hartigan dip test on a pseudo-sample reconstructed from the histogram.

numpy/scipy are required for Stages 1-2 (and BC uses numpy here). ``diptest`` is optional: when
missing, Stage 3 is skipped and ``dip_p`` stays ``None`` (rows land in the ``unconfirmed`` tier),
matching the browser viewer which never runs the dip test.
"""

from __future__ import annotations

from typing import Any, Optional

from ybtop.histogram import confidence_tier, parse_histogram_buckets

# Point-mass pseudo-sample cap for the dip test on very high-call queries.
_PSEUDO_SAMPLE_CAP = 20000


class HistogramDepsError(RuntimeError):
    """Raised when numpy/scipy (required for detection) are unavailable."""


def _require_numpy_scipy() -> tuple[Any, Any, Any]:
    try:
        import numpy as np  # noqa: PLC0415
        from scipy.ndimage import gaussian_filter1d  # noqa: PLC0415
        from scipy.signal import find_peaks  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - env dependent
        raise HistogramDepsError(
            "Latency multimodality detection needs numpy and scipy. "
            "Install the optional extra: pip install 'ybtop[histogram]'"
        ) from exc
    return np, gaussian_filter1d, find_peaks


def diptest_available() -> bool:
    try:
        import diptest  # noqa: F401,PLC0415

        return True
    except ImportError:
        return False


def _base_result(row: dict[str, Any], total_calls: int) -> dict[str, Any]:
    return {
        "queryid": row.get("queryid"),
        "query": row.get("query"),
        "dbname": row.get("dbname"),
        "calls": int(total_calls),
        "flag": False,
        "reason": "",
        "bc": None,
        "dip_p": None,
        "n_raw_peaks": 0,
        "n_modes_estimate": None,
        "latency_min_ms": None,
        "latency_max_ms": None,
        "latency_spread_ratio": None,
        "peak_pairs": [],
        "confidence_tier": "not_flagged",
    }


def _log2_mid(np: Any, lo: float, hi: float) -> float:
    """Arithmetic bucket midpoint taken in log2(ms) space (matches the reference).

    Uses the arithmetic mean ``(lo + hi) / 2`` (floored at 1e-3 ms so a 0-lower-bound bucket
    still has a finite log) rather than a geometric mean; this keeps peak-pair gaps read back
    as ``2 ** mid`` consistent with how the density is laid out.
    """
    return float(np.log2(max((lo + hi) / 2.0, 1e-3)))


def _bimodality_coefficient(np: Any, buckets: list[tuple[float, float, int]]) -> Optional[float]:
    """Sarle's (uncorrected) bimodality coefficient over weighted log2(ms) midpoints.

    BC = (skew^2 + 1) / kurtosis. A uniform distribution gives 5/9 ~= 0.5556, the conventional
    flag threshold; higher values indicate a more bimodal shape. Returns ``None`` when fewer
    than 30 weighted samples back the moment estimate (too noisy to trust), mirroring the
    reference ``bimodality_coefficient``.
    """
    mids: list[float] = []
    weights: list[float] = []
    for lo, hi, c in buckets:
        if c <= 0:
            continue
        mids.append(_log2_mid(np, lo, hi))
        weights.append(float(c))
    if not weights:
        return None
    x = np.array(mids, dtype=float)
    w = np.array(weights, dtype=float)
    n = float(w.sum())
    if n < 30:
        return None
    mean = float(np.average(x, weights=w))
    var = float(np.average((x - mean) ** 2, weights=w))
    std = var**0.5
    if std == 0:
        return 0.0
    skew = float(np.average(((x - mean) / std) ** 3, weights=w))
    kurt = float(np.average(((x - mean) / std) ** 4, weights=w))  # non-excess
    if kurt <= 0:
        return None
    return (skew**2 + 1.0) / kurt


def _expand_to_pseudo_samples(
    np: Any, buckets: list[tuple[float, float, int]], cap: int = _PSEUDO_SAMPLE_CAP
) -> Any:
    """Point-mass reconstruction in log2(ms): each bucket midpoint repeated ``count`` times.

    Capped at ``cap`` via seeded proportional resampling for very high-call queries (matches
    the reference ``expand_to_pseudo_samples``). Known caveat: no within-bucket spread before
    the dip test.
    """
    mids: list[float] = []
    weights: list[float] = []
    for lo, hi, c in buckets:
        if c <= 0:
            continue
        mids.append(_log2_mid(np, lo, hi))
        weights.append(float(c))
    mids_arr = np.array(mids, dtype=float)
    w = np.array(weights, dtype=float)
    total = float(w.sum())
    if total <= cap:
        return np.repeat(mids_arr, w.astype(int))
    rng = np.random.default_rng(0)
    probs = w / total
    return rng.choice(mids_arr, size=cap, p=probs)


def detect_modes(
    buckets: list[tuple[float, float, int]],
    *,
    row: Optional[dict[str, Any]] = None,
    min_calls: int = 30,
    bc_threshold: float = 0.555,
    dip_alpha: float = 0.05,
    min_octave_separation: float = 0.5,
    min_valley_ratio: float = 0.75,
) -> dict[str, Any]:
    """Detect latency bimodality/multimodality for one statement's parsed buckets.

    ``buckets`` is the sorted ``(lo, hi, count)`` list from
    :func:`ybtop.histogram.parse_histogram_buckets` (overflow bucket already stripped).
    """
    np, gaussian_filter1d, find_peaks = _require_numpy_scipy()
    row = row or {}
    total = int(sum(c for _, _, c in buckets))
    result = _base_result(row, total)

    # Overall latency spread (independent of modality): lowest lo / highest hi with calls.
    nonzero = [(lo, hi) for lo, hi, c in buckets if c > 0]
    if nonzero:
        lat_min = min(lo for lo, _ in nonzero)
        lat_max = max(hi for _, hi in nonzero)
        result["latency_min_ms"] = round(lat_min, 4)
        result["latency_max_ms"] = round(lat_max, 4)
        result["latency_spread_ratio"] = (
            round(lat_max / lat_min, 4) if lat_min > 0 else None
        )

    if total < min_calls:
        result["reason"] = "insufficient_calls"
        result["confidence_tier"] = confidence_tier(result)
        return result

    # Stage 0 - Prescreen.
    bc = _bimodality_coefficient(np, buckets)
    result["bc"] = bc
    if bc is None or bc < bc_threshold:
        result["reason"] = "bc_below_threshold"
        result["confidence_tier"] = confidence_tier(result)
        return result

    # Stage 1 - Width-normalized density in log2(ms) space, then peak finding.
    mids = np.array([_log2_mid(np, lo, hi) for lo, hi, _ in buckets])
    widths = np.array(
        [(np.log2(hi) - np.log2(lo)) if lo > 0 else np.log2(hi) for lo, hi, _ in buckets]
    )
    counts = np.array([c for _, _, c in buckets], dtype=float)
    positive = widths[widths > 0]
    fallback_width = float(positive.min()) if positive.size else 1.0
    widths = np.where(widths > 0, widths, fallback_width)
    density = counts / widths
    dsum = float(density.sum())
    if dsum > 0:
        density = density / dsum

    smoothed = gaussian_filter1d(density, sigma=1.0)  # scipy default mode="reflect"
    avg_spacing = float(np.mean(np.diff(mids))) if len(mids) > 1 else 1.0
    distance = max(1, int(round(min_octave_separation / max(avg_spacing, 1e-6))))
    prominence = 0.03 * float(smoothed.max()) if smoothed.max() > 0 else None
    peaks, _props = find_peaks(smoothed, prominence=prominence, distance=distance)
    result["n_raw_peaks"] = int(len(peaks))

    if len(peaks) < 2:
        result["reason"] = "single_peak_after_smoothing"
        result["confidence_tier"] = confidence_tier(result)
        return result

    # Stage 2 - Valley / significance check on raw (unsmoothed) counts. Keep every valid
    # adjacent peak pair (low→high latency). The report/viewer list all of them in the gap column.
    valid_pairs: list[dict[str, Any]] = []
    for i in range(len(peaks) - 1):
        p1, p2 = int(peaks[i]), int(peaks[i + 1])
        if mids[p2] - mids[p1] < min_octave_separation:
            continue
        valley = float(counts[p1:p2 + 1].min())
        smaller_peak = min(float(counts[p1]), float(counts[p2]))
        if smaller_peak <= 0:
            continue
        ratio = valley / smaller_peak
        if ratio > min_valley_ratio:
            continue  # dip too shallow
        peak1_ms = float(2 ** mids[p1])
        peak2_ms = float(2 ** mids[p2])
        valid_pairs.append(
            {
                "peak1_ms": round(peak1_ms, 4),
                "peak2_ms": round(peak2_ms, 4),
                "valley_ratio": round(float(ratio), 4),
                "gap_ms": round(peak2_ms - peak1_ms, 4),
                "gap_ratio": round(peak2_ms / peak1_ms, 4) if peak1_ms > 0 else None,
            }
        )

    if not valid_pairs:
        result["reason"] = "no_significant_valley"
        result["confidence_tier"] = confidence_tier(result)
        return result

    # Shape-based flag holds even if the dip test is unavailable.
    result["flag"] = True
    result["peak_pairs"] = valid_pairs
    result["n_modes_estimate"] = int(len(peaks))

    # Stage 3 - Confirmatory Hartigan dip test (optional).
    if diptest_available():
        import diptest  # noqa: PLC0415

        sample = _expand_to_pseudo_samples(np, buckets)
        if sample.size >= 4 and float(np.ptp(sample)) > 0:
            try:
                _dip, pval = diptest.diptest(sample)
                result["dip_p"] = float(pval)
                if float(pval) >= dip_alpha:
                    result["flag"] = False
                    result["reason"] = "dip_test_not_significant"
            except Exception:  # noqa: BLE001 - never let the dip test crash a scan
                result["dip_p"] = None

    result["confidence_tier"] = confidence_tier(result)
    return result


def analyze_histogram_row(
    row: dict[str, Any],
    *,
    min_calls: int = 30,
    overflow_ratio_threshold: float = 0.02,
    **detect_kwargs: Any,
) -> dict[str, Any]:
    """Run detection on one merged/delta row and attach overflow-tail diagnostics.

    ``row["buckets"]`` is the flat ``{bucket_label: count}`` map. The open-ended ``[max,)``
    overflow bucket is stripped for peak finding but surfaced as ``overflow_count`` /
    ``overflow_ratio`` / ``overflow_flag`` -- a large tail can be a genuine extra ("third")
    mode that the finite-width peak finder cannot characterize.
    """
    buckets, overflow_count = parse_histogram_buckets(row.get("buckets") or {})
    total = int(sum(c for _, _, c in buckets)) + int(overflow_count)
    result = detect_modes(buckets, row=row, min_calls=min_calls, **detect_kwargs)
    result["overflow_count"] = int(overflow_count)
    result["overflow_ratio"] = (overflow_count / total) if total else 0.0
    result["overflow_flag"] = (
        result["overflow_ratio"] > overflow_ratio_threshold and total >= min_calls
    )
    return result
