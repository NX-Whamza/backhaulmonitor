"""PCN (Predicted Channel Normal) calculator.

Computes off-target dB, fade margins, and link budget analysis using
designed values from SMAP bh_report_history and live RF telemetry from Zabbix.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from src.config import settings
from src.topology.hostname_parser import tower_tag_variants


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


def _get_tx_power(rf_snapshot: dict, far_end_rf: Optional[dict] = None) -> Optional[float]:
    """Get TX power — near-end first, fall back to far-end for XPIC configs
    where the near-end active carriers don't report TX power."""
    tx = rf_snapshot.get("txpower")
    if tx is not None:
        return tx
    if far_end_rf:
        return far_end_rf.get("txpower")
    return None


def calc_off_target(
    pcn: dict, rf_snapshot: dict, far_end_rf: Optional[dict] = None,
) -> Optional[float]:
    """Calculate off-target dB: expected RSL vs current RSL.

    Formula: OFFTARGET = rxcoordPower + (tx_power - coordPower) - current_rsl

    Computes the expected RSL at actual TX power and compares against the
    current RSL. Positive = link below designed target (degraded).
    For XPIC, falls back to far-end TX power when near-end doesn't have it.
    """
    tx_power = _get_tx_power(rf_snapshot, far_end_rf)
    current_rsl = rf_snapshot.get("rsl")
    coord_power = pcn.get("coordPower1(dBm)")
    rx_coord_power = pcn.get("rxcoordPower1(dBm)")

    if any(v is None for v in [tx_power, current_rsl, coord_power, rx_coord_power]):
        return None

    expected_rsl = rx_coord_power + (tx_power - coord_power)
    return round(expected_rsl - current_rsl, 1)


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
    far_end_rf: Optional[dict] = None,
) -> dict[str, Any]:
    """Build a comprehensive link health assessment.

    Combines PCN, live RF, baseline trend, and weather into a single
    diagnostic verdict that a NOC tech can act on.
    """
    assessment: dict[str, Any] = {}

    # Off-target from PCN (Grafana formula)
    if pcn:
        assessment["off_target_db"] = calc_off_target(pcn, rf_snapshot, far_end_rf)
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
    # For XPIC, falls back to far-end TX power when near-end doesn't have it
    if pcn:
        coord_power = pcn.get("coordPower1(dBm)")
        rx_coord = pcn.get("rxcoordPower1(dBm)")
        tx_power = _get_tx_power(rf_snapshot, far_end_rf)
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
        # SMAP MCP gateway carries a large fixed per-request overhead (server-side
        # query is ~sub-100ms, but wire time is consistently 8-13s). An 8s timeout
        # silently killed every PCN and tower-coord call, blanking both cards.
        self._client = httpx.AsyncClient(timeout=25.0, headers=headers)
        self._req_id = 0
        self._pcn_cache: dict[str, dict] = {}
        self._coords_cache: dict[str, dict] = {}

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
        """Query SMAP bh_report_history for PCN designed values.

        Results are cached in memory — FCC coordination data doesn't change.
        """
        cache_key = f"{hostname}|{tower_a}|{tower_z}"
        if cache_key in self._pcn_cache:
            return self._pcn_cache[cache_key]

        import asyncio
        try:
            result = await asyncio.wait_for(
                self._get_pcn_inner(hostname, tower_a, tower_z),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            result = None

        if result is not None:
            self._pcn_cache[cache_key] = result
        return result

    async def _get_pcn_inner(self, hostname: str, tower_a: str, tower_z: str) -> Optional[dict]:
        """PCN lookup strategy (catalog has query restrictions on text columns):
        1. Exact hostname match
        2. Single-tower query (site1/site2) with client-side model filtering
        3. Hostname guessing with exact matches

        Tier 1 and 2 run in parallel — the exact-host query can take 5-10s
        when it misses, so we don't block tower queries behind it.
        """
        import asyncio

        # Extract model prefix for client-side filtering
        dot_idx = hostname.find(".")
        prefix = hostname[:dot_idx] if dot_idx > 0 else hostname
        parts = prefix.split("-")
        model = parts[1].upper() if len(parts) >= 2 else ""
        model_prefix = f"BH-{model}" if model else ""

        # Build ALL search tasks (tier 1 + tier 2) and race them.
        # Exact-host queries can take 5-10s on a miss, so running them
        # in parallel with tower queries prevents timeout starvation.
        ta_variants = tower_tag_variants(tower_a) if tower_a else []
        tz_variants = tower_tag_variants(tower_z) if tower_z else []

        async def _tagged(coro, source: str):
            r = await coro
            if r:
                r["_pcn_source"] = source
            return r

        tasks = [
            asyncio.create_task(_tagged(self._query_pcn_by_host(hostname), "SMAP bh_report_history")),
            # bh_PCN_data — small CoMSearch table, fast site-pair lookup
            asyncio.create_task(_tagged(self._query_pcn_data(tower_a, tower_z), "CoMSearch (bh_PCN_data)")),
        ]
        _seen: set[str] = set()
        for t in ta_variants:
            if t not in _seen:
                tasks.append(asyncio.create_task(_tagged(
                    self._query_pcn_by_tower(t, model_prefix, tz_variants),
                    "SMAP bh_report_history",
                )))
                _seen.add(t)
        for t in tz_variants:
            if t not in _seen:
                tasks.append(asyncio.create_task(_tagged(
                    self._query_pcn_by_tower(t, model_prefix, ta_variants),
                    "SMAP bh_report_history",
                )))
                _seen.add(t)

        try:
            for coro in asyncio.as_completed(tasks):
                try:
                    r = await coro
                    if r:
                        for t in tasks:
                            t.cancel()
                        return r
                except Exception:
                    continue
        finally:
            for t in tasks:
                t.cancel()

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
                # Include tower name variants for hostname guessing
                ta_vars = tower_tag_variants(tower_a)
                tz_vars = tower_tag_variants(tower_z)
                tower_pairs = list(dict.fromkeys(
                    [(ta, tz) for ta in ta_vars for tz in tz_vars]
                    + [(tz, ta) for tz in tz_vars for ta in ta_vars]
                ))
                for ta, tz in tower_pairs:
                    if num_suffix:
                        guesses.append(f"BH-{model}-{band}-{num_suffix}.{ta}.{tz}")
                    guesses.append(f"BH-{model}-{band}-{ta}.{tz}")
                guesses = list(dict.fromkeys(guesses))  # deduplicate

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

    async def _query_pcn_by_tower(
        self, tower: str, model_prefix: str = "",
        other_towers: list[str] | None = None,
    ) -> Optional[dict]:
        """Query by single tower name, filter by model + other tower client-side.

        ``other_towers`` is a list of tower name variants for the opposite end
        of the link.  When provided, results where the non-queried site column
        matches one of these variants are strongly preferred — this prevents
        returning the wrong link when a tower has multiple BH devices.
        """
        safe_tower = tower.replace("'", "''")
        ot_upper = {t.upper() for t in other_towers} if other_towers else set()

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
                parsed = [
                    dict(zip(columns, r)) if isinstance(r, list) else r
                    for r in rows
                ]

                # Filter by model prefix
                if model_prefix:
                    mp = model_prefix.upper()
                    candidates = [
                        rd for rd in parsed
                        if (rd.get("host") or "").upper().startswith(mp)
                    ]
                else:
                    candidates = parsed

                if not candidates:
                    continue

                # Prefer rows where the other site matches the opposite tower
                if ot_upper:
                    other_col = "site2" if col == "site1" else "site1"
                    for rd in candidates:
                        if (rd.get(other_col) or "").upper() in ot_upper:
                            return rd
                    # Don't fall back — returning a different link's data
                    # is worse than returning nothing.  Let the caller try
                    # other tower variants or fall through to tier 3.
                    continue

                # No cross-reference — fall back to first model match
                return candidates[0]

        return None

    # Columns shared by bh_report_history and bh_PCN_data
    _PCN_COORD_COLUMNS = (
        "site1, site2, radiomodel1, radiomodel2, "
        "mainmodel1, mainmodel2, `maindiameter1(ft)`, `maindiameter2(ft)`, "
        "`maxPower1(dBm)`, `maxPower2(dBm)`, "
        "`coordPower1(dBm)`, `coordPower2(dBm)`, "
        "`rxmaxPower1(dBm)`, `rxmaxPower2(dBm)`, "
        "`rxcoordPower1(dBm)`, `rxcoordPower2(dBm)`, "
        "`azimuth12(deg)`, `azimuth21(deg)`, "
        "`distance(mi)`, `ground1(ft)`, `ground2(ft)`, band"
    )

    @staticmethod
    def _pcn_tower_variants(tower: str) -> list[str]:
        """Broader tower name variants for bh_PCN_data matching.

        Extends tower_tag_variants with directional-suffix stripping:
        TX-CALLISBURG-NO-1-H → [..., CALLISBURG-NO-1, CALLISBURG]
        """
        import re
        variants = list(tower_tag_variants(tower))
        # Strip trailing direction+number: -NO-1, -CN-1, -SO-2, -EA-1, etc.
        for v in list(variants):
            stripped = re.sub(r"-(NO|SO|EA|WE|NE|NW|SE|SW|CN|FX)-\d+$", "", v)
            if stripped and stripped != v and stripped not in variants:
                variants.append(stripped)
        return variants

    async def _query_pcn_data(
        self, tower_a: str, tower_z: str,
    ) -> Optional[dict]:
        """Query bh_PCN_data (CoMSearch FCC coordination) by tower pair.

        This table is small (~5k rows) so we use LIKE queries for broad
        matching — handles spelling variations (HUDSONOAKS↔HUDSONOAK),
        missing state prefixes (HOHQ↔TX-HOHQ2), etc.
        """
        import asyncio

        ta_variants = self._pcn_tower_variants(tower_a)
        tz_variants = self._pcn_tower_variants(tower_z)

        # Build LIKE search keys: use the shortest meaningful core of each
        # tower name for broad SQL matching, then cross-reference client-side.
        like_keys: list[str] = []
        for v in list(dict.fromkeys(ta_variants + tz_variants)):
            safe = v.replace("'", "''").replace("%", "")
            if len(safe) >= 3:
                like_keys.append(safe)
        # Deduplicate preserving order
        like_keys = list(dict.fromkeys(like_keys))

        async def _search(key: str) -> list[dict]:
            safe = key.replace("'", "''")
            data = await self._call_tool("smap__query", {
                "repo_path": "nextlink",
                "source_name": "smap",
                "sql": (
                    f"SELECT {self._PCN_COORD_COLUMNS} "
                    f"FROM bh_PCN_data WHERE "
                    f"site1 LIKE '%{safe}%' OR site2 LIKE '%{safe}%' "
                    "LIMIT 20"
                ),
            })
            if not data or not data.get("rows"):
                return []
            columns = data.get("columns", [])
            return [
                dict(zip(columns, r)) if isinstance(r, list) else r
                for r in data["rows"]
            ]

        # Run all LIKE queries in parallel
        search_results = await asyncio.gather(
            *[_search(k) for k in like_keys],
            return_exceptions=True,
        )

        # Collect all unique rows
        all_rows: list[dict] = []
        seen_pairs: set[tuple] = set()
        for batch in search_results:
            if isinstance(batch, Exception) or not batch:
                continue
            for rd in batch:
                pair = ((rd.get("site1") or ""), (rd.get("site2") or ""))
                if pair not in seen_pairs:
                    seen_pairs.add(pair)
                    all_rows.append(rd)

        if not all_rows:
            return None

        # Cross-reference: find rows where BOTH towers match
        # Checks (in order of strength):
        #   1. Exact match
        #   2. Substring either way  (HOHQ ↔ TX-HOHQ2)
        #   3. Long common prefix    (TX-WEATHERFORD-REVER ↔ TX-WEATHERFORD-FX-REVERE)
        # When both towers reduce to the same stripped stem (co-located sectors
        # like NE-WAYNE-SW-1 / NE-WAYNE-NW-2 → both "NE-WAYNE"), the shared
        # variants can't discriminate the endpoints. Drop them from the pair
        # check so matching relies on the directional variants that differ.
        ambiguous = {v.upper() for v in ta_variants} & {v.upper() for v in tz_variants}
        ta_match = [v for v in ta_variants if v.upper() not in ambiguous]
        tz_match = [v for v in tz_variants if v.upper() not in ambiguous]

        for rd in all_rows:
            s1 = (rd.get("site1") or "").upper()
            s2 = (rd.get("site2") or "").upper()
            # A valid pair matches one tower on site1 and the OTHER on site2.
            # Requiring distinct columns prevents a single site (e.g. a shared
            # tower) from satisfying both ends and returning a different link.
            if ((self._tower_matches(s1, ta_match) and self._tower_matches(s2, tz_match)) or
                (self._tower_matches(s2, ta_match) and self._tower_matches(s1, tz_match))):
                return rd
        return None

    @staticmethod
    def _tower_matches(site: str, variants: list[str]) -> bool:
        """Check if a SMAP site name matches any tower variant."""
        site_u = site.upper()
        for v in variants:
            vu = v.upper()
            if vu == site_u or vu in site_u or site_u in vu:
                return True
            # Common-prefix match
            pfx = min(len(vu), len(site_u))
            common = 0
            for a, b in zip(vu, site_u):
                if a != b:
                    break
                common += 1
            if common >= 12 or (pfx > 0 and common >= pfx * 0.75):
                return True
        return False

    async def get_tower_coords(self, tower: str) -> Optional[dict]:
        """Get tower lat/lon from SMAP tower data. Cached in memory."""
        if tower in self._coords_cache:
            return self._coords_cache[tower]

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
            result = {"lat": float(lat), "lon": float(lon)}
            self._coords_cache[tower] = result
            return result
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
