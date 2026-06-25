"""PCN (Predicted Channel Normal) calculator.

Computes off-target dB, fade margins, and link budget analysis using
designed values from SMAP bh_report_history and live RF telemetry from Zabbix.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from src.config import settings


# ITU-R P.838 approximate specific attenuation at 25 mm/hr (dB/km)
RAIN_ATTENUATION_BY_BAND: dict[int, float] = {
    5: 0.1,
    6: 0.2,
    11: 1.5,
    18: 5.0,
    24: 8.0,
    60: 15.0,
    70: 17.0,
    80: 18.0,
}


def estimate_rain_attenuation(band_ghz: int, rain_rate_mm_hr: float) -> float:
    """Estimate rain attenuation in dB/km for a given band and rain rate.

    Uses ITU-R P.838 reference values scaled linearly from the 25 mm/hr
    reference point. This is a simplification — actual ITU calculation uses
    power-law coefficients — but good enough for field diagnostics.
    """
    ref = RAIN_ATTENUATION_BY_BAND.get(band_ghz, 0.0)
    if ref == 0 or rain_rate_mm_hr <= 0:
        return 0.0
    # Scale linearly from reference at 25 mm/hr
    return round(ref * (rain_rate_mm_hr / 25.0), 2)


def calc_off_target(pcn: dict, rf_snapshot: dict) -> Optional[float]:
    """Calculate off-target dB using Grafana BH dashboard formula.

    Formula: OFFTARGET = (zab_power - coordPower) + rxmaxPower - zab_rsl

    This adjusts the FCC coordinated RSL for actual TX power and compares
    against current RSL. Positive = link below designed target (degraded).
    """
    zab_power = rf_snapshot.get("txpower")
    zab_rsl = rf_snapshot.get("rsl")
    coord_power = pcn.get("coordPower1(dBm)")
    rx_max_power = pcn.get("rxmaxPower1(dBm)")

    if any(v is None for v in [zab_power, zab_rsl, coord_power, rx_max_power]):
        return None

    return round((zab_power - coord_power) + rx_max_power - zab_rsl, 1)


def calc_baseline_delta(rf_snapshot: dict, baseline: Optional[dict]) -> Optional[float]:
    """Calculate RSL deviation from 7-day Zabbix baseline.

    Positive = current RSL is below baseline (degraded from recent trend).
    """
    zab_rsl = rf_snapshot.get("rsl")
    if zab_rsl is None or not baseline or baseline.get("baseline") is None:
        return None
    return round(baseline["baseline"] - zab_rsl, 1)


def calc_fade_margin(pcn: dict, rf_snapshot: dict) -> Optional[float]:
    """Calculate available fade margin in dB.

    Fade margin = current RSL - receiver threshold.
    Uses rxcoordPower as the coordinated minimum (FCC floor).
    Higher = more headroom before link drops.
    """
    rsl = rf_snapshot.get("rsl")
    rx_coord = pcn.get("rxcoordPower1(dBm)")
    if rsl is None or rx_coord is None:
        return None
    return round(rsl - rx_coord, 1)


def calc_modulation_headroom(rf_snapshot: dict) -> Optional[dict]:
    """Calculate modulation headroom — how far from link failure.

    Returns margin_pct (100% = at max, 0% = at min) and steps_below.
    """
    cur_mod = rf_snapshot.get("rxmod")
    max_mod = rf_snapshot.get("maxmod")
    min_mod = rf_snapshot.get("minmod")

    if cur_mod is None or max_mod is None:
        return None

    steps_below = max_mod - cur_mod
    margin_pct = None
    if min_mod is not None and max_mod > min_mod:
        margin_pct = round((cur_mod - min_mod) / (max_mod - min_mod) * 100, 1)

    return {
        "current_mod": cur_mod,
        "max_mod": max_mod,
        "min_mod": min_mod,
        "steps_below": steps_below,
        "margin_pct": margin_pct,
        "at_max": cur_mod >= max_mod,
    }


def build_link_assessment(
    pcn: Optional[dict],
    rf_snapshot: dict,
    baseline: Optional[dict],
    weather: Optional[dict],
    band_ghz: Optional[int] = None,
    distance_mi: Optional[float] = None,
) -> dict[str, Any]:
    """Build a comprehensive link health assessment.

    Combines PCN, live RF, baseline trend, and weather into a single
    diagnostic verdict that a NOC tech can act on.
    """
    assessment: dict[str, Any] = {}

    # Off-target from PCN (Grafana formula)
    if pcn:
        assessment["off_target_db"] = calc_off_target(pcn, rf_snapshot)
        assessment["fade_margin_db"] = calc_fade_margin(pcn, rf_snapshot)
        assessment["pcn_rx_coord"] = pcn.get("rxcoordPower1(dBm)")
        assessment["pcn_coord_power"] = pcn.get("coordPower1(dBm)")
        assessment["pcn_max_power"] = pcn.get("maxPower1(dBm)")
        assessment["pcn_antenna_model"] = pcn.get("mainmodel1")
        assessment["pcn_antenna_size_ft"] = pcn.get("maindiameter1(ft)")
        assessment["pcn_radio_model"] = pcn.get("radiomodel1")
        assessment["pcn_azimuth"] = pcn.get("azimuth12(deg)")

    # Baseline delta (Zabbix 7-day trend)
    assessment["baseline_delta_db"] = calc_baseline_delta(rf_snapshot, baseline)

    # Adjusted expected RSL — coord RSL corrected for actual TX power
    if pcn:
        coord_power = pcn.get("coordPower1(dBm)")
        rx_coord = pcn.get("rxcoordPower1(dBm)")
        tx_power = rf_snapshot.get("txpower")
        if all(v is not None for v in [coord_power, rx_coord, tx_power]):
            adjusted = round(rx_coord + (tx_power - coord_power), 1)
            assessment["adjusted_expected_rsl"] = adjusted

    # Modulation headroom
    assessment["modulation"] = calc_modulation_headroom(rf_snapshot)

    # RSL vs baseline
    current_rsl = rf_snapshot.get("rsl")
    if current_rsl is not None and baseline:
        baseline_rsl = baseline.get("baseline")
        if baseline_rsl is not None:
            assessment["rsl_delta_db"] = round(current_rsl - baseline_rsl, 1)
            assessment["baseline_rsl"] = baseline_rsl
            assessment["baseline_stddev"] = baseline.get("stddev")

    assessment["current_rsl"] = current_rsl
    assessment["snr"] = rf_snapshot.get("snr")
    assessment["ber"] = rf_snapshot.get("ber")
    assessment["tx_power"] = rf_snapshot.get("txpower")

    # Rain fade estimate
    if weather and band_ghz and distance_mi:
        rain_rate = weather.get("rain_rate_mm_hr", 0)
        if rain_rate > 0:
            fade_per_km = estimate_rain_attenuation(band_ghz, rain_rate)
            distance_km = distance_mi * 1.60934
            assessment["estimated_rain_fade_db"] = round(fade_per_km * distance_km, 1)

    return assessment


class CatalogClient:
    """Client for codexCatalog MCP — PCN data, tower info, etc."""

    def __init__(self) -> None:
        self._url = settings.smap_url
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if settings.smap_auth_header:
            headers["Authorization"] = settings.smap_auth_header
        self._client = httpx.AsyncClient(timeout=10.0, headers=headers)
        self._req_id = 0

    async def _call_tool(self, tool_name: str, arguments: dict) -> Optional[dict]:
        """Call a codexCatalog MCP tool and return parsed result."""
        self._req_id += 1
        payload = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
            "id": self._req_id,
        }
        try:
            resp = await self._client.post(self._url, json=payload)
            if resp.status_code != 200:
                return None
            body = resp.json()
            if body.get("result", {}).get("isError"):
                return None
            # Try structuredContent first, fall back to text parsing
            structured = body.get("result", {}).get("structuredContent", {}).get("result")
            if structured:
                return structured
            content = body.get("result", {}).get("content", [])
            if not content:
                return None
            import json
            return json.loads(content[0].get("text", "{}"))
        except Exception:
            return None

    async def get_pcn(self, hostname: str, tower_a: str = "", tower_z: str = "") -> Optional[dict]:
        """Query SMAP bh_report_history for PCN designed values."""
        import asyncio
        try:
            return await asyncio.wait_for(
                self._get_pcn_inner(hostname, tower_a, tower_z),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            return None

    async def _get_pcn_inner(self, hostname: str, tower_a: str, tower_z: str) -> Optional[dict]:
        """PCN lookup strategy (catalog has query restrictions on text columns):
        1. Exact hostname match
        2. Single-tower query (site1/site2) with client-side model filtering
        3. Hostname guessing with exact matches
        """
        # Extract model prefix for client-side filtering
        dot_idx = hostname.find(".")
        prefix = hostname[:dot_idx] if dot_idx > 0 else hostname
        parts = prefix.split("-")
        model = parts[1].upper() if len(parts) >= 2 else ""
        model_prefix = f"BH-{model}" if model else ""

        # 1. Try exact hostname
        result = await self._query_pcn_by_host(hostname)
        if result:
            return result

        # 2. Single-tower query with client-side filtering (run in parallel)
        towers_to_check = [t for t in [tower_a, tower_z] if t]
        if towers_to_check:
            import asyncio
            tower_results = await asyncio.gather(
                *[self._query_pcn_by_tower(t, model_prefix) for t in towers_to_check],
                return_exceptions=True
            )
            for r in tower_results:
                if r and not isinstance(r, Exception):
                    return r

        # 3. Hostname guessing — limited to most likely patterns only
        if tower_a and tower_z and model:
            band_candidates = []
            num_suffix = ""
            for p in parts[2:]:
                if p.isdigit():
                    val = int(p)
                    if val in (5, 6, 11, 18, 24, 60, 70, 80):
                        band_candidates.append(p)
                    elif val <= 9:
                        num_suffix = p

            if band_candidates:
                band = band_candidates[0]
                guesses = []
                for ta, tz in [(tower_a, tower_z), (tower_z, tower_a)]:
                    if num_suffix:
                        guesses.append(f"BH-{model}-{band}-{num_suffix}.{ta}.{tz}")
                    guesses.append(f"BH-{model}-{band}-{ta}.{tz}")

                # Run guesses in parallel
                guess_results = await asyncio.gather(
                    *[self._query_pcn_by_host(g) for g in guesses],
                    return_exceptions=True,
                )
                for r in guess_results:
                    if r and not isinstance(r, Exception):
                        return r

        return None

    async def _query_pcn_by_host(self, hostname: str) -> Optional[dict]:
        safe_host = hostname.replace("'", "''")
        data = await self._call_tool("smap__query", {
            "repo_path": "nextlink",
            "source_name": "smap",
            "sql": (
                "SELECT host, site1, site2, radiomodel1, radiomodel2, "
                "mainmodel1, mainmodel2, `maindiameter1(ft)`, `maindiameter2(ft)`, "
                "`maxPower1(dBm)`, `maxPower2(dBm)`, "
                "`coordPower1(dBm)`, `coordPower2(dBm)`, "
                "`rxmaxPower1(dBm)`, `rxmaxPower2(dBm)`, "
                "`rxcoordPower1(dBm)`, `rxcoordPower2(dBm)`, "
                "`azimuth12(deg)`, `azimuth21(deg)`, "
                "`distance(mi)`, `ground1(ft)`, `ground2(ft)`, band "
                f"FROM bh_report_history WHERE host = '{safe_host}' "
                "LIMIT 1"
            ),
        })
        return self._parse_pcn_result(data)

    _PCN_COLUMNS = (
        "SELECT host, site1, site2, radiomodel1, radiomodel2, "
        "mainmodel1, mainmodel2, `maindiameter1(ft)`, `maindiameter2(ft)`, "
        "`maxPower1(dBm)`, `maxPower2(dBm)`, "
        "`coordPower1(dBm)`, `coordPower2(dBm)`, "
        "`rxmaxPower1(dBm)`, `rxmaxPower2(dBm)`, "
        "`rxcoordPower1(dBm)`, `rxcoordPower2(dBm)`, "
        "`azimuth12(deg)`, `azimuth21(deg)`, "
        "`distance(mi)`, `ground1(ft)`, `ground2(ft)`, band "
    )

    async def _query_pcn_by_tower(self, tower: str, model_prefix: str = "") -> Optional[dict]:
        """Query by single tower name, filter by model client-side."""
        safe_tower = tower.replace("'", "''")

        # Try site1, then site2
        for col in ["site1", "site2"]:
            data = await self._call_tool("smap__query", {
                "repo_path": "nextlink",
                "source_name": "smap",
                "sql": f"{self._PCN_COLUMNS} FROM bh_report_history WHERE {col} = '{safe_tower}' LIMIT 20",
            })
            if data and data.get("rows"):
                columns = data.get("columns", [])
                rows = data["rows"]
                # Filter for matching model prefix
                if model_prefix:
                    mp = model_prefix.upper()
                    for row in rows:
                        rd = dict(zip(columns, row)) if isinstance(row, list) else row
                        if (rd.get("host") or "").upper().startswith(mp):
                            return rd
                # No model filter or no match — return first
                return dict(zip(columns, rows[0])) if isinstance(rows[0], list) else rows[0]

        return None

    async def get_tower_coords(self, tower: str) -> Optional[dict]:
        """Get tower lat/lon from SMAP tower data."""
        data = await self._call_tool("smap__get_tower", {
            "repo_path": "nextlink",
            "source_name": "smap",
            "site": tower,
        })
        if not data or not isinstance(data, dict):
            return None
        towers = data.get("towers", [])
        if not towers:
            return None
        t = towers[0]
        lat = t.get("lat")
        lon = t.get("long") or t.get("lon")
        if lat and lon:
            return {"lat": float(lat), "lon": float(lon)}
        return None

    @staticmethod
    def _parse_pcn_result(data: Optional[dict]) -> Optional[dict]:
        if not data:
            return None
        columns = data.get("columns", [])
        rows = data.get("rows", [])
        if not rows:
            return None
        if isinstance(rows[0], dict):
            return rows[0]
        return dict(zip(columns, rows[0]))

    async def close(self) -> None:
        await self._client.aclose()


# Keep backward-compatible alias
PcnClient = CatalogClient
