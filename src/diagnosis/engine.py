"""Diagnosis engine — combines all signals into a verdict.

Takes RF telemetry, PCN designed values, weather, and radio status
and produces a human-readable diagnosis for NOC techs.
"""

from __future__ import annotations

from typing import Any, Optional

from src.pcn.calculator import (
    build_link_assessment,
    calc_baseline_delta,
    calc_modulation_headroom,
    calc_off_target,
)


# Verdict categories
VERDICT_RAIN_FADE = "rain_fade"
VERDICT_OFF_TARGET = "off_target"
VERDICT_HARDWARE = "hardware_issue"
VERDICT_ALIGNMENT = "alignment_issue"
VERDICT_INTERFERENCE = "interference"
VERDICT_NORMAL = "normal"
VERDICT_UNKNOWN = "insufficient_data"


def diagnose_link(
    rf_snapshot: dict,
    pcn: Optional[dict],
    baseline: Optional[dict],
    weather: Optional[dict],
    far_end_rf: Optional[dict] = None,
    band_ghz: Optional[int] = None,
    distance_mi: Optional[float] = None,
    snmp_down: bool = False,
    active_problems: Optional[list] = None,
) -> dict[str, Any]:
    """Run full diagnosis on a BH link and return a structured verdict.

    Returns:
        dict with keys: verdict, confidence, severity, details, recommendations
    """
    findings: list[str] = []
    scores: dict[str, float] = {
        VERDICT_RAIN_FADE: 0.0,
        VERDICT_OFF_TARGET: 0.0,
        VERDICT_HARDWARE: 0.0,
        VERDICT_ALIGNMENT: 0.0,
        VERDICT_INTERFERENCE: 0.0,
        VERDICT_NORMAL: 0.0,
    }

    # ── Active Zabbix problems ────────────────────────────────────────
    if active_problems:
        findings.append(f"{len(active_problems)} active Zabbix problem(s)")

    # ── SNMP Down — near-end unreachable ──────────────────────────────
    if snmp_down:
        scores[VERDICT_HARDWARE] += 30
        findings.append("Near-end SNMP unreachable — no live RF telemetry from this end")
        if far_end_rf and far_end_rf.get("rsl") is not None:
            far_rsl = far_end_rf["rsl"]
            findings.append(
                f"Far-end receiving signal (RSL {far_rsl:.1f} dB) — "
                "link appears operational, near-end management issue"
            )
            scores[VERDICT_NORMAL] += 20

    # ── Check 1: Is it raining (now or recently)? ──────────────────────
    if weather:
        rain_rate = weather.get("rain_rate_mm_hr", 0)
        recent = weather.get("recent_rain", {})
        recovering = weather.get("rain_fade_recovering", False)

        if rain_rate > 0 and band_ghz:
            from src.pcn.calculator import estimate_rain_attenuation
            fade = estimate_rain_attenuation(band_ghz, rain_rate)
            if fade > 2.0:
                scores[VERDICT_RAIN_FADE] += 40
                findings.append(
                    f"Rain detected: {rain_rate:.1f} mm/hr, "
                    f"estimated fade {fade:.1f} dB/km at {band_ghz} GHz"
                )
            elif fade > 0.5:
                scores[VERDICT_RAIN_FADE] += 20
                findings.append(f"Light rain: {rain_rate:.1f} mm/hr, minor fade expected")
        elif recovering and recent.get("had_rain"):
            scores[VERDICT_RAIN_FADE] += 30
            total = recent.get("total_rain_mm", 0)
            peak = recent.get("max_rain_mm", 0)
            peak_time = (recent.get("max_rain_time") or "").split("T")[-1]
            findings.append(
                f"Recent rain detected: {total} mm in last 6h "
                f"(peak {peak} mm at {peak_time}) — link may be recovering"
            )

    # ── Check 2: Off-target from PCN design ─────────────────────────────
    if pcn:
        off_target = calc_off_target(pcn, rf_snapshot)
        if off_target is not None:
            if abs(off_target) <= 3.0:
                scores[VERDICT_NORMAL] += 30
                findings.append(f"On-target: {off_target:+.1f} dB from PCN design (within ±3 dB)")
            elif off_target > 3.0:
                scores[VERDICT_OFF_TARGET] += 30 + min(off_target * 2, 30)
                findings.append(f"Off-target: {off_target:+.1f} dB below PCN design")
            else:
                findings.append(f"Running hot: {off_target:+.1f} dB above PCN design (unusual)")
                scores[VERDICT_ALIGNMENT] += 10

    # ── Check 3: Baseline trend (Zabbix 7-day) ─────────────────────────
    baseline_delta = calc_baseline_delta(rf_snapshot, baseline)
    if baseline_delta is not None:
        if abs(baseline_delta) <= 2.0:
            scores[VERDICT_NORMAL] += 20
            findings.append(f"Zabbix baseline stable: {baseline_delta:+.1f} dB from 7d median")
        elif baseline_delta >= 3.0:
            if scores[VERDICT_RAIN_FADE] > 0:
                scores[VERDICT_RAIN_FADE] += 20
                findings.append(f"RSL dropped {baseline_delta:+.1f} dB from 7d baseline — consistent with rain/weather")
            else:
                scores[VERDICT_HARDWARE] += 20
                findings.append(f"RSL dropped {baseline_delta:+.1f} dB from 7d baseline — no rain detected")

    # ── Check 4: Modulation headroom ──────────────────────────────────
    mod = calc_modulation_headroom(rf_snapshot)
    if mod:
        if mod["at_max"]:
            scores[VERDICT_NORMAL] += 20
            findings.append("Modulation at max — link healthy")
        elif mod.get("margin_pct") is not None:
            margin = mod["margin_pct"]
            if margin < 20:
                scores[VERDICT_HARDWARE] += 20
                findings.append(f"Modulation margin critical: {margin:.0f}% (near link failure)")
            elif margin < 50:
                findings.append(f"Modulation margin low: {margin:.0f}%")

    # ── Check 5: Both ends degraded? ──────────────────────────────────
    current_rsl = rf_snapshot.get("rsl")
    if far_end_rf:
        far_rsl = far_end_rf.get("rsl")
        if far_rsl is not None and current_rsl is not None:
            if abs(far_rsl - current_rsl) < 3.0:
                findings.append("Both ends show similar RSL — path issue (rain/obstruction)")
                scores[VERDICT_RAIN_FADE] += 10
            else:
                findings.append(
                    f"Asymmetric RSL: near={current_rsl:.1f}, far={far_rsl:.1f} — "
                    "possible single-end hardware issue"
                )
                scores[VERDICT_HARDWARE] += 15

    # ── Determine verdict ─────────────────────────────────────────────
    if not findings:
        return {
            "verdict": VERDICT_UNKNOWN,
            "confidence": 0,
            "severity": "unknown",
            "findings": ["Insufficient data to diagnose"],
            "recommendations": ["Check Zabbix connectivity and verify hostname"],
        }

    best_verdict = max(scores, key=scores.get)  # type: ignore[arg-type]
    best_score = scores[best_verdict]
    total = sum(scores.values()) or 1
    confidence = min(round(best_score / total * 100), 100)

    severity = _classify_severity(rf_snapshot, baseline, mod, snmp_down, far_end_rf)
    recommendations = _get_recommendations(best_verdict, findings, mod, snmp_down)

    return {
        "verdict": best_verdict,
        "confidence": confidence,
        "severity": severity,
        "findings": findings,
        "recommendations": recommendations,
        "scores": {k: round(v, 1) for k, v in scores.items() if v > 0},
    }


def _classify_severity(
    rf_snapshot: dict,
    baseline: Optional[dict],
    mod: Optional[dict],
    snmp_down: bool = False,
    far_end_rf: Optional[dict] = None,
) -> str:
    """Classify severity: normal, minor, moderate, severe, critical."""
    rsl = rf_snapshot.get("rsl")
    if rsl is None:
        if snmp_down:
            # SNMP down but far-end has signal = minor (management issue)
            # SNMP down and no far-end = moderate (can't assess link)
            if far_end_rf and far_end_rf.get("rsl") is not None:
                return "minor"
            return "moderate"
        return "unknown"

    if baseline:
        delta = abs(rsl - baseline.get("baseline", rsl))
    else:
        delta = 0

    # Modulation margin takes priority
    if mod and mod.get("margin_pct") is not None:
        if mod["margin_pct"] <= 10:
            return "critical"
        if mod["margin_pct"] <= 25:
            return "severe"

    if delta <= 2:
        return "normal"
    if delta <= 5:
        return "minor"
    if delta <= 10:
        return "moderate"
    if delta <= 20:
        return "severe"
    return "critical"


def _get_recommendations(
    verdict: str,
    findings: list[str],
    mod: Optional[dict],
    snmp_down: bool = False,
) -> list[str]:
    """Generate actionable recommendations based on verdict."""
    recs: list[str] = []

    if snmp_down:
        recs.append("Verify SNMP agent is running and reachable on the near-end radio")
        recs.append("Check device management IP, SNMP community string, and firewall rules")
        # If far-end shows signal, note that the link itself is likely fine
        if any("link appears operational" in f for f in findings):
            recs.append("Far end confirms signal — link is up, this is a management-plane issue")
    elif verdict == VERDICT_RAIN_FADE:
        recs.append("Monitor — rain fade is temporary, link should recover when weather clears")
        recs.append("Check if link has adequate fade margin for this rain region")

    elif verdict == VERDICT_OFF_TARGET:
        recs.append("Compare current RSL to PCN designed value — may need alignment")
        recs.append("Check antenna alignment, connectors, and feedline")
        recs.append("Verify TX power matches coordinated power")

    elif verdict == VERDICT_HARDWARE:
        recs.append("Inspect radio hardware — possible failing component")
        recs.append("Check TX power, BER, and error counters")
        recs.append("Compare near-end and far-end RF snapshots for asymmetry")

    elif verdict == VERDICT_ALIGNMENT:
        recs.append("Schedule tower crew for antenna alignment check")
        recs.append("Verify azimuth matches PCN design")

    elif verdict == VERDICT_NORMAL:
        recs.append("Link appears healthy — no action needed")

    if mod and mod.get("margin_pct") is not None and mod["margin_pct"] < 30:
        recs.append(f"WARNING: Modulation margin at {mod['margin_pct']:.0f}% — link near failure threshold")

    return recs
