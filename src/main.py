"""Backhaul Monitor — BH link diagnostic tool for NOC techs.

Type in a BH hostname or tower name, get PCN comparison, weather check,
live RF telemetry, and a diagnosis verdict.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.config import settings
from src.zabbix.client import ZabbixClient
from src.weather.client import WeatherClient
from src.pcn.calculator import CatalogClient, build_link_assessment
from src.radio.client import RadioClient
from src.diagnosis.engine import diagnose_link
from src.topology.hostname_parser import parse_bh_hostname


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    app.state.zabbix = ZabbixClient()
    app.state.weather = WeatherClient()
    app.state.catalog = CatalogClient()
    app.state.radio = RadioClient()
    yield
    # Shutdown
    await app.state.zabbix.close()
    await app.state.weather.close()
    await app.state.catalog.close()
    await app.state.radio.close()


app = FastAPI(
    title="Backhaul Monitor",
    description="BH link diagnostic tool for NOC technicians",
    version="0.1.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# ── Pages ─────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Main page — search for a BH link."""
    return templates.TemplateResponse(request, "index.html", {
        "radio_user": settings.radio_username,
        "radio_pass": settings.radio_password,
    })


@app.get("/health")
async def health():
    return {"status": "ok"}


# ── API endpoints ─────────────────────────────────────────────────────

def _snmp_is_down(problems: list) -> bool:
    """Check if any active problem indicates SNMP collection failure."""
    return any("snmp" in p.get("name", "").lower() for p in problems)


def _sanitize_rf(rf: dict, snmp_down: bool) -> dict:
    """Replace zero RF values with None when SNMP is down (no real data)."""
    if not snmp_down or not rf:
        return rf
    sanitized = dict(rf)
    for key in ("rsl", "snr", "txpower", "rxmod", "txmod", "maxmod", "minmod",
                "ber", "rxcap", "txcap", "channel_width"):
        if key in sanitized and sanitized[key] == 0:
            sanitized[key] = None
        elif key in sanitized and sanitized[key] == 0.0:
            sanitized[key] = None
    sanitized["snmp_down"] = True
    return sanitized


def _merge_companion_rf(
    primary: dict, companion: dict,
    primary_pol: str = "", comp_pol: str = "",
) -> dict:
    """Merge companion polarization carriers into primary RF snapshot."""
    if not primary:
        return companion or {}
    if not companion:
        return primary
    merged = dict(primary)
    _KEYS = ("rsl", "snr", "rxmod", "rxmod_name", "txmod", "maxmod",
             "maxmod_name", "minmod", "minmod_name", "ber", "txpower",
             "rxcap", "txcap")

    prim_radios = [dict(r) for r in (merged.get("all_radios") or [])]
    if not prim_radios and merged.get("rsl") is not None:
        prim_radios = [{"radio_id": "0", **{k: merged.get(k) for k in _KEYS}}]

    comp_radios = [dict(r) for r in (companion.get("all_radios") or [])]
    if not comp_radios and companion.get("rsl") is not None:
        comp_radios = [{"radio_id": "comp", **{k: companion.get(k) for k in _KEYS}}]

    if primary_pol:
        for r in prim_radios:
            r["radio_id"] = f"{r['radio_id']} ({primary_pol})"
    if comp_pol:
        for r in comp_radios:
            r["radio_id"] = f"{r['radio_id']} ({comp_pol})"

    if comp_radios:
        merged["all_radios"] = prim_radios + comp_radios
        merged["radio_count"] = len(merged["all_radios"])

    return merged


@app.get("/api/diagnose/{hostname}")
async def api_diagnose(request: Request, hostname: str):
    """Full diagnostic for a BH hostname.

    Runs all checks in parallel: RF snapshot, PCN lookup, weather,
    baseline trend, far-end RF — then produces a verdict.
    """
    zabbix: ZabbixClient = request.app.state.zabbix
    weather_client: WeatherClient = request.app.state.weather
    catalog: CatalogClient = request.app.state.catalog

    # Parse hostname for link topology
    link_info = parse_bh_hostname(hostname)
    rf_prefix = link_info.rf_key_prefix if link_info else None
    far_end = link_info.far_end_hostname if link_info else None
    band_ghz = link_info.band_ghz if link_info else None
    tower_a = link_info.tower_a if link_info else ""
    tower_z = link_info.tower_z if link_info else ""
    companion_hostname = link_info.companion_hostname if link_info else None

    if not rf_prefix:
        rf_prefix = "av.wireless"

    # Phase 1: Run ALL queries in parallel
    async def _find_far():
        if tower_a and tower_z:
            return await zabbix.find_far_end(hostname, tower_a, tower_z)
        return None

    async def _get_tower_coords():
        for t in [tower_a, tower_z]:
            if t:
                coords = await catalog.get_tower_coords(t)
                if coords:
                    return coords
        return None

    async def _get_companion_rf():
        if not companion_hostname or not rf_prefix:
            return None
        return await zabbix.get_rf_snapshot(companion_hostname, rf_prefix)

    phase1 = await asyncio.gather(
        zabbix.get_rf_snapshot(hostname, rf_prefix),      # 0
        catalog.get_pcn(hostname, tower_a, tower_z),      # 1
        zabbix.get_rsl_trend(hostname, rf_prefix, days=7), # 2
        zabbix.get_rsl_history(hostname, rf_prefix),      # 3
        zabbix.get_active_problems(hostname),             # 4
        zabbix.resolve_host(hostname),                    # 5
        _find_far(),                                       # 6
        _get_tower_coords(),                               # 7
        _get_companion_rf(),                               # 8
        return_exceptions=True,
    )

    rf_snapshot = phase1[0] if not isinstance(phase1[0], Exception) else {}
    pcn = phase1[1] if not isinstance(phase1[1], Exception) else None
    baseline = phase1[2] if not isinstance(phase1[2], Exception) else None
    rsl_history = phase1[3] if not isinstance(phase1[3], Exception) else None
    problems = phase1[4] if not isinstance(phase1[4], Exception) else []
    host_data = phase1[5] if not isinstance(phase1[5], Exception) else None
    zabbix_far_end = phase1[6] if not isinstance(phase1[6], Exception) else None
    tower_coords = phase1[7] if not isinstance(phase1[7], Exception) else None
    comp_rf = phase1[8] if not isinstance(phase1[8], Exception) else None

    host_ip = host_data.get("ip") if host_data else None
    if zabbix_far_end:
        far_end = zabbix_far_end

    # Determine polarization labels for -H/-V paired links
    primary_pol, comp_pol = "", ""
    if companion_hostname:
        for tower in [tower_a, tower_z]:
            if tower.endswith('-H'):
                primary_pol, comp_pol = "H", "V"
                break
            elif tower.endswith('-V'):
                primary_pol, comp_pol = "V", "H"
                break

    # Merge companion polarization carriers (e.g. 4+0 configs)
    if comp_rf and isinstance(comp_rf, dict) and comp_rf.get("rsl") is not None:
        rf_snapshot = _merge_companion_rf(rf_snapshot, comp_rf, primary_pol, comp_pol)

    # Compute companion far-end hostname
    comp_far = None
    if far_end:
        far_info = parse_bh_hostname(far_end)
        if far_info and far_info.companion_hostname:
            comp_far = far_info.companion_hostname

    # Phase 2: Far-end RF/IP + Weather (both depend on phase 1 results)
    async def _get_far_end_data():
        if not far_end or not rf_prefix:
            return None, None
        results = await asyncio.gather(
            zabbix.get_rf_snapshot(far_end, rf_prefix),
            zabbix.resolve_host(far_end),
            return_exceptions=True,
        )
        rf = results[0] if not isinstance(results[0], Exception) else None
        host = results[1] if not isinstance(results[1], Exception) else None
        return rf, host.get("ip") if host else None

    async def _get_weather():
        if not tower_coords:
            return None
        return await weather_client.check_rain_fade(
            tower_coords["lat"], tower_coords["lon"], band_ghz
        )

    async def _get_comp_far_data():
        if not comp_far or not rf_prefix:
            return None, None
        results = await asyncio.gather(
            zabbix.get_rf_snapshot(comp_far, rf_prefix),
            zabbix.resolve_host(comp_far),
            return_exceptions=True,
        )
        rf = results[0] if not isinstance(results[0], Exception) else None
        host = results[1] if not isinstance(results[1], Exception) else None
        return rf, host.get("ip") if host else None

    phase2 = await asyncio.gather(
        _get_far_end_data(),
        _get_weather(),
        _get_comp_far_data(),
        return_exceptions=True,
    )

    far_end_data = phase2[0] if not isinstance(phase2[0], Exception) else (None, None)
    far_end_rf, far_end_ip = far_end_data if far_end_data else (None, None)
    weather_data = phase2[1] if not isinstance(phase2[1], Exception) else None
    comp_far_data = phase2[2] if not isinstance(phase2[2], Exception) else (None, None)
    comp_far_rf, comp_far_ip = comp_far_data if comp_far_data else (None, None)

    # Merge companion far-end carriers
    if comp_far_rf and isinstance(comp_far_rf, dict):
        if far_end_rf and isinstance(far_end_rf, dict):
            far_end_rf = _merge_companion_rf(far_end_rf, comp_far_rf, primary_pol, comp_pol)
        else:
            far_end_rf = comp_far_rf
    if not far_end_ip and comp_far_ip:
        far_end_ip = comp_far_ip

    # Sanitize zero RF values when SNMP is down
    snmp_down = _snmp_is_down(problems)
    rf_snapshot = _sanitize_rf(rf_snapshot, snmp_down)

    # Fill in band from PCN if parser didn't extract it
    if not band_ghz and pcn and pcn.get("band"):
        import re
        band_match = re.match(r"(\d+)\s*GHz", pcn["band"])
        if band_match:
            band_ghz = int(band_match.group(1))

    # Extract distance from PCN
    distance_mi = None
    if pcn and pcn.get("distance(mi)"):
        try:
            distance_mi = float(pcn["distance(mi)"])
        except (ValueError, TypeError):
            pass

    # Build assessment
    assessment = build_link_assessment(
        pcn=pcn,
        rf_snapshot=rf_snapshot,
        baseline=baseline,
        weather=weather_data,
        band_ghz=band_ghz,
        distance_mi=distance_mi,
    )

    # Run diagnosis
    verdict = diagnose_link(
        rf_snapshot=rf_snapshot,
        pcn=pcn,
        baseline=baseline,
        weather=weather_data,
        far_end_rf=far_end_rf if isinstance(far_end_rf, dict) else None,
        band_ghz=band_ghz,
        distance_mi=distance_mi,
        snmp_down=snmp_down,
        active_problems=problems if problems else None,
    )

    return {
        "hostname": hostname,
        "ip": host_ip,
        "link_info": {
            "model": link_info.model if link_info else None,
            "radio_family": link_info.radio_family if link_info else None,
            "band_ghz": band_ghz or (link_info.band_ghz if link_info else None),
            "tower_a": tower_a or None,
            "tower_z": tower_z or None,
            "far_end": far_end if far_end != hostname else None,
            "far_end_ip": far_end_ip,
            "companion": companion_hostname,
            "companion_far_end": comp_far,
            "technology": link_info.technology if link_info else None,
            "rain_sensitivity": link_info.rain_sensitivity if link_info else None,
        },
        "rf_snapshot": rf_snapshot,
        "far_end_rf": far_end_rf if isinstance(far_end_rf, dict) else None,
        "pcn": pcn,
        "baseline": baseline,
        "rsl_history": rsl_history,
        "weather": weather_data,
        "assessment": assessment,
        "verdict": verdict,
        "active_problems": problems,
    }


def _is_ip(q: str) -> bool:
    """Check if query looks like an IP address or prefix."""
    parts = q.split(".")
    return len(parts) >= 2 and all(p.isdigit() for p in parts if p)


@app.get("/api/search")
async def api_search(request: Request, q: str = ""):
    """Search for BH devices by hostname, tower name, or IP address."""
    if not q or len(q) < 2:
        return {"results": []}
    zabbix: ZabbixClient = request.app.state.zabbix

    all_hosts = []

    if _is_ip(q):
        # IP search
        all_hosts = await zabbix.search_by_ip(q)
    else:
        # Hostname search
        hostname_q = f"BH-{q}" if not q.upper().startswith("BH-") else q
        all_hosts = await zabbix.search_hosts(hostname_q)
        # Also search by tower name
        tower_hosts = await zabbix.search_by_tower(q.upper())
        all_hosts = all_hosts + tower_hosts

    # Merge results, dedup by hostid
    seen = set()
    results = []
    for h in all_hosts:
        hid = h.get("hostid")
        if hid not in seen:
            seen.add(hid)
            tags = {t["tag"]: t["value"] for t in h.get("tags", [])}
            interfaces = h.get("interfaces", [])
            ip = interfaces[0]["ip"] if interfaces else ""
            results.append({
                "hostname": h["host"],
                "name": h.get("name", ""),
                "tower": tags.get("tower", ""),
                "link_type": tags.get("link_type", ""),
                "ip": ip,
            })

    return {"results": results}


@app.get("/api/tower/{tower}")
async def api_tower(request: Request, tower: str):
    """List all BH devices at a tower with basic health info."""
    zabbix: ZabbixClient = request.app.state.zabbix
    hosts = await zabbix.search_by_tower(tower.upper())
    devices = []
    for h in hosts:
        tags = {t["tag"]: t["value"] for t in h.get("tags", [])}
        devices.append({
            "hostname": h["host"],
            "name": h.get("name", ""),
            "tower": tags.get("tower", ""),
            "link_type": tags.get("link_type", ""),
            "max_capacity": tags.get("max_capacity", ""),
            "technology": tags.get("technology", ""),
        })
    return {"tower": tower.upper(), "devices": devices}


@app.get("/api/rf/{hostname}")
async def api_rf_snapshot(request: Request, hostname: str):
    """Get raw RF snapshot for a hostname."""
    zabbix: ZabbixClient = request.app.state.zabbix
    link_info = parse_bh_hostname(hostname)
    rf_prefix = link_info.rf_key_prefix if link_info else "av.wireless"
    snapshot = await zabbix.get_rf_snapshot(hostname, rf_prefix)
    return {"hostname": hostname, "rf_snapshot": snapshot}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "src.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=True,
    )
