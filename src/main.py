"""Backhaul Monitor — BH link diagnostic tool for NOC techs.

Type in a BH hostname or tower name, get PCN comparison, weather check,
live RF telemetry, and a diagnosis verdict.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Path, Query, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from pydantic import BaseModel, Field

from src.config import settings
from src.zabbix.client import ZabbixClient
from src.weather.client import WeatherClient
from src.pcn.calculator import CatalogClient, build_link_assessment
from src.radio.client import RadioClient
from src.diagnosis.engine import diagnose_link
from src.topology.hostname_parser import parse_bh_hostname, tower_tag_variants
from src.feedback.store import init_db, save_feedback, check_recent_feedback, get_verdict_accuracy, blend_confidence
from src.auth import (
    init_auth_db, check_login, validate_session, end_session,
    create_api_key, validate_api_key, list_api_keys, revoke_api_key,
)
from src.schemas import (
    DiagnoseResponse,
    FeedbackResponse,
    HealthResponse,
    RfSnapshotResponse,
    SearchResponse,
    TowerResponse,
)


OPENAPI_TAGS = [
    {
        "name": "Diagnostics",
        "description": "Full link diagnosis — RF telemetry, PCN design comparison, weather, far-end check, and verdict.",
    },
    {
        "name": "Discovery",
        "description": "Find BH devices by hostname, tower name, or IP. List all devices at a tower.",
    },
    {
        "name": "Telemetry",
        "description": "Raw RF snapshots from Zabbix SNMP — normalized across Aviat, Cambium, and Siklu.",
    },
    {
        "name": "Feedback",
        "description": "Tech feedback on diagnosis accuracy. Feeds the Bayesian confidence blender.",
    },
    {
        "name": "System",
        "description": "Health checks.",
    },
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    init_db()
    init_auth_db()
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
    summary="BH link diagnostics for NOC technicians",
    description="""
## What this does

Pulls from **4 data sources in parallel** and returns a diagnosis in ~3 seconds:

| Source | What it provides |
|--------|-----------------|
| **Zabbix** | Live RF (RSL, SNR, mod, TX power, BER, capacity), 7-day trend baseline, active alerts |
| **SMAP / CoMSearch** | FCC PCN design values — coordinated power, expected RSL, antenna specs, path distance |
| **Weather** | Rain rate, 6-hour history, humidity, wind — plus ITU-R P.838 rain fade estimation |
| **Hostname Parser** | Radio model, band, tower sites, far-end device, companion polarization |

## Verdict types

| Verdict | Trigger |
|---------|---------|
| `rain_fade` | Active/recent rain with estimated attenuation for this band and path distance |
| `off_target` | RSL more than 3 dB off PCN design (same formula as Grafana BH dashboard) |
| `hardware_issue` | Baseline drop with no rain, asymmetric near/far RSL, SNMP down |
| `alignment_issue` | Running above PCN design, azimuth anomalies |
| `interference` | Degradation inconsistent with weather or hardware |
| `normal` | On-target, mod at max, baseline stable |

Confidence is Bayesian-blended with historical tech feedback — the more feedback, the better the scoring gets.

## Supported radios

Aviat WTM4200/4100/4800, Cambium CN820S/C/850C, Siklu 2500/1200/8010,
Ubiquiti AirFiber/Wave/PowerBeam, Cambium Force/PTP/ePMP.
""",
    version="1.0.0",
    lifespan=lifespan,
    openapi_tags=OPENAPI_TAGS,
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


@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["System"],
    summary="Health check",
    description="Returns service status. Use for load balancer health probes or uptime monitoring.",
)
async def health():
    return {"status": "ok"}


# ── API key validation ────────────────────────────────────────────────

def verify_api_key(request: Request) -> None:
    """Dependency: validate X-API-Key header if present.

    - Browser requests from the UI (no header) pass through.
    - External requests with X-API-Key must provide a valid key.
    """
    key = request.headers.get("x-api-key")
    if key is None:
        # No key header — allow (UI / internal use)
        return
    if not validate_api_key(key):
        raise HTTPException(status_code=401, detail="Invalid API key")


# ── Admin routes ─────────────────────────────────────────────────────

def _get_session(request: Request) -> str | None:
    return request.cookies.get("bhm_session")


@app.get("/admin/login", response_class=HTMLResponse, include_in_schema=False)
async def admin_login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/admin/login", response_class=HTMLResponse, include_in_schema=False)
async def admin_login(request: Request):
    form = await request.form()
    username = form.get("username", "")
    password = form.get("password", "")

    token = check_login(username, password)
    if not token:
        return templates.TemplateResponse(request, "login.html", {
            "error": "Invalid credentials",
        })

    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie("bhm_session", token, httponly=True, samesite="lax", max_age=86400)
    return response


@app.get("/admin/logout", include_in_schema=False)
async def admin_logout(request: Request):
    token = _get_session(request)
    if token:
        end_session(token)
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie("bhm_session")
    return response


@app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
async def admin_page(request: Request):
    token = _get_session(request)
    if not token or not validate_session(token):
        return RedirectResponse("/admin/login", status_code=303)
    return templates.TemplateResponse(request, "admin.html", {})


@app.get("/admin/api/keys", include_in_schema=False)
async def admin_list_keys(request: Request):
    token = _get_session(request)
    if not token or not validate_session(token):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"keys": list_api_keys()}


@app.post("/admin/api/keys", include_in_schema=False)
async def admin_create_key(request: Request):
    token = _get_session(request)
    if not token or not validate_session(token):
        raise HTTPException(status_code=401, detail="Not authenticated")
    body = await request.json()
    label = body.get("label", "").strip()
    if not label:
        raise HTTPException(status_code=400, detail="Label is required")
    return create_api_key(label)


@app.delete("/admin/api/keys/{key_id}", include_in_schema=False)
async def admin_revoke_key(request: Request, key_id: int):
    token = _get_session(request)
    if not token or not validate_session(token):
        raise HTTPException(status_code=401, detail="Not authenticated")
    revoke_api_key(key_id)
    return {"revoked": True}


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


@app.get(
    "/api/diagnose/{hostname}",
    response_model=DiagnoseResponse,
    tags=["Diagnostics"],
    dependencies=[Depends(verify_api_key)],
    summary="Full link diagnosis",
    description="""Run a complete diagnostic on a backhaul link.

1. Parse hostname → radio model, band, tower sites, vendor family
2. Phase 1 (parallel): Zabbix RF + baseline + problems, PCN design values, tower coords, far-end discovery, companion device
3. Phase 2: Far-end RF and weather (needs Phase 1 results)
4. Compute off-target dB, fade margin, modulation headroom, rain attenuation
5. Score verdict categories, pick highest, classify severity, generate recommendations
6. Blend confidence with historical tech feedback

Hostname format: `BH-{MODEL}-{BAND}-{NUM}.{TOWER_A}.{TOWER_Z}`
""",
)
async def api_diagnose(
    request: Request,
    hostname: str = Path(
        ...,
        description="BH device hostname",
    ),
):
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
            return await zabbix.find_far_end(
                hostname, tower_a, tower_z,
                exclude=[companion_hostname] if companion_hostname else None,
            )
        return None

    async def _get_tower_coords():
        # Try tower name variants (original, stripped pol, stripped state prefix)
        candidates = []
        seen = set()
        for t in [tower_a, tower_z]:
            for v in tower_tag_variants(t):
                if v not in seen:
                    candidates.append(v)
                    seen.add(v)
        for t in candidates:
            coords = await catalog.get_tower_coords(t)
            if coords:
                return coords
        return None

    async def _get_companion_rf():
        if not companion_hostname or not rf_prefix:
            return None
        return await zabbix.get_rf_snapshot(companion_hostname, rf_prefix)

    async def _resolve_companion():
        if not companion_hostname:
            return None
        return await zabbix.resolve_host(companion_hostname)

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
        _resolve_companion(),                              # 9
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
    comp_host = phase1[9] if not isinstance(phase1[9], Exception) else None

    host_ip = host_data.get("ip") if host_data else None
    comp_ip = comp_host.get("ip") if comp_host else None
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

    # Drop computed far-end hostname if it couldn't be verified in Zabbix
    # (the parser's guess is often wrong for non-standard hostname formats)
    if not zabbix_far_end and not far_end_ip:
        far_end = None
        comp_far = None

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

    # Blend confidence with historical feedback accuracy
    raw_confidence = verdict["confidence"]
    verdict["confidence"] = blend_confidence(raw_confidence, verdict["verdict"])
    accuracy_stats = get_verdict_accuracy(verdict["verdict"])
    verdict["raw_confidence"] = raw_confidence
    verdict["accuracy"] = accuracy_stats

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
            "companion_ip": comp_ip,
            "companion_far_end": comp_far,
            "companion_far_end_ip": comp_far_ip,
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


@app.get(
    "/api/search",
    response_model=SearchResponse,
    tags=["Discovery"],
    dependencies=[Depends(verify_api_key)],
    summary="Search BH devices",
    description="""Search by hostname, tower name, or IP address.

- **Hostname:** auto-prepends `BH-` if missing
- **Tower:** searches Zabbix tower tags (case-insensitive)
- **IP:** detects IP format and searches Zabbix interfaces

Results are deduped by Zabbix host ID.
""",
)
async def api_search(
    request: Request,
    q: str = Query(
        "",
        description="Hostname, tower name, or IP address (min 2 characters)",
    ),
):
    if not q or len(q) < 2:
        return {"results": []}
    zabbix: ZabbixClient = request.app.state.zabbix

    all_hosts = []

    if _is_ip(q):
        # IP search
        all_hosts = await zabbix.search_by_ip(q)
    else:
        # Search hostname (with BH- prefix) and tower tag in parallel
        hostname_q = f"BH-{q}" if not q.upper().startswith("BH-") else q
        host_results, tower_results = await asyncio.gather(
            zabbix.search_hosts(hostname_q),
            zabbix.search_by_tower(q.upper()),
        )
        all_hosts = host_results + tower_results

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


@app.get(
    "/api/tower/{tower}",
    response_model=TowerResponse,
    tags=["Discovery"],
    dependencies=[Depends(verify_api_key)],
    summary="List devices at a tower",
    description="""List all BH devices at a tower site.

Searches Zabbix by tower tag (exact match, case-insensitive). Returns hostname,
link type, max capacity, and technology for each device.
""",
)
async def api_tower(
    request: Request,
    tower: str = Path(
        ...,
        description="Tower site name",
    ),
):
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


class FeedbackIn(BaseModel):
    """Feedback on a diagnosis verdict."""
    hostname: str = Field(..., description="BH device hostname that was diagnosed")
    verdict: str = Field(..., description="Verdict type (rain_fade, off_target, hardware_issue, etc.)", examples=["rain_fade"])
    confidence: float = Field(..., description="Confidence score shown (0-100)", examples=[72.0])
    severity: str = Field(..., description="Severity level shown", examples=["minor"])
    accurate: bool = Field(..., description="true = diagnosis was right, false = it missed", examples=[True])
    band_ghz: int | None = Field(None, description="Frequency band (GHz)", examples=[18])
    off_target_db: float | None = Field(None, description="Off-target dB at time of diagnosis", examples=[2.5])
    baseline_delta: float | None = Field(None, description="Baseline delta dB at time of diagnosis", examples=[1.3])
    rain_rate: float | None = Field(None, description="Rain rate (mm/hr) at time of diagnosis", examples=[4.2])
    comment: str | None = Field(None, description="Free-text comment")


@app.post(
    "/api/feedback",
    response_model=FeedbackResponse,
    tags=["Feedback"],
    dependencies=[Depends(verify_api_key)],
    summary="Submit diagnosis feedback",
    description="""Record whether a diagnosis was accurate or not.

Updates historical accuracy stats for that verdict type. The confidence blender
uses this to adjust future scores — accurate verdicts get boosted, wrong ones get pulled down.

`accurate: true` = diagnosis was right, `false` = it missed.
""",
)
async def api_feedback(body: FeedbackIn):
    """Record user feedback on a diagnosis verdict."""
    # Check if this link + verdict was already reported recently
    existing = check_recent_feedback(body.hostname, body.verdict)
    if existing:
        return {
            "saved": False,
            "already_reported": True,
            "existing_id": existing["id"],
            "message": "Feedback already submitted for this link and verdict",
            "accuracy": get_verdict_accuracy(body.verdict),
        }

    row_id = save_feedback(
        hostname=body.hostname,
        verdict=body.verdict,
        confidence=body.confidence,
        severity=body.severity,
        accurate=body.accurate,
        band_ghz=body.band_ghz,
        off_target_db=body.off_target_db,
        baseline_delta=body.baseline_delta,
        rain_rate=body.rain_rate,
        comment=body.comment,
    )
    stats = get_verdict_accuracy(body.verdict)
    return {"saved": True, "id": row_id, "accuracy": stats}


@app.get(
    "/api/rf/{hostname}",
    response_model=RfSnapshotResponse,
    tags=["Telemetry"],
    dependencies=[Depends(verify_api_key)],
    summary="Raw RF snapshot",
    description="""Current RF telemetry for a BH device, pulled from Zabbix SNMP.

Normalized across vendors — Aviat (`av.wireless.*`), Cambium CN820 (`820.wireless.*`),
Siklu (`system.rf*`). Multi-carrier configs (2+0, XPIC) include per-carrier data in `all_radios`.
""",
)
async def api_rf_snapshot(
    request: Request,
    hostname: str = Path(
        ...,
        description="BH device hostname",
    ),
):
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
