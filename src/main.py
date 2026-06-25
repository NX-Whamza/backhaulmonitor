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
    return templates.TemplateResponse(request, "index.html")


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

    if not rf_prefix:
        rf_prefix = "av.wireless"

    # Always try Zabbix search for far-end — parser guesses are often wrong
    if tower_a and tower_z:
        zabbix_far_end = await zabbix.find_far_end(hostname, tower_a, tower_z)
        if zabbix_far_end:
            far_end = zabbix_far_end

    # Run all enrichment in parallel
    rf_task = zabbix.get_rf_snapshot(hostname, rf_prefix)
    pcn_task = catalog.get_pcn(hostname, tower_a, tower_z)
    baseline_task = zabbix.get_rsl_trend(hostname, rf_prefix, days=7)
    history_task = zabbix.get_rsl_history(hostname, rf_prefix)
    problems_task = zabbix.get_active_problems(hostname)

    tasks = [rf_task, pcn_task, baseline_task, history_task, problems_task]

    # Far-end RF if we know the partner
    if far_end and rf_prefix:
        tasks.append(zabbix.get_rf_snapshot(far_end, rf_prefix))
    else:
        async def _noop():
            return None
        tasks.append(_noop())

    results = await asyncio.gather(*tasks, return_exceptions=True)

    rf_snapshot = results[0] if not isinstance(results[0], Exception) else {}
    pcn = results[1] if not isinstance(results[1], Exception) else None
    baseline = results[2] if not isinstance(results[2], Exception) else None
    rsl_history = results[3] if not isinstance(results[3], Exception) else None
    problems = results[4] if not isinstance(results[4], Exception) else []
    far_end_rf = results[5] if not isinstance(results[5], Exception) else None

    # Resolve host IP
    host_data = await zabbix.resolve_host(hostname)
    host_ip = host_data.get("ip") if host_data else None
    far_end_ip = None
    if far_end:
        far_host = await zabbix.resolve_host(far_end)
        far_end_ip = far_host.get("ip") if far_host else None

    # Sanitize zero RF values when SNMP is down
    snmp_down = _snmp_is_down(problems)
    rf_snapshot = _sanitize_rf(rf_snapshot, snmp_down)

    # Fill in band from PCN if parser didn't extract it
    if not band_ghz and pcn and pcn.get("band"):
        import re
        band_match = re.match(r"(\d+)\s*GHz", pcn["band"])
        if band_match:
            band_ghz = int(band_match.group(1))

    # Weather check — resolve tower lat/lon then fetch conditions
    weather_data = None
    tower_name = tower_a or tower_z
    if tower_name:
        coords = await catalog.get_tower_coords(tower_name)
        if not coords and tower_z and tower_z != tower_name:
            coords = await catalog.get_tower_coords(tower_z)
        if coords:
            weather_data = await weather_client.check_rain_fade(
                coords["lat"], coords["lon"], band_ghz
            )

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
