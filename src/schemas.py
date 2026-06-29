"""Pydantic response models for API documentation (/docs)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ── Link topology (parsed from hostname) ─────────────────────────────

class LinkInfo(BaseModel):
    """Link topology parsed from the BH hostname."""
    model: str | None = Field(None, description="Radio model", examples=["AV4200"])
    radio_family: str | None = Field(None, description="Vendor family (maps to SNMP key prefix)", examples=["aviat"])
    band_ghz: int | None = Field(None, description="Frequency band (GHz)", examples=[18])
    tower_a: str | None = Field(None, description="Near-end tower site")
    tower_z: str | None = Field(None, description="Far-end tower site")
    far_end: str | None = Field(None, description="Far-end hostname (discovered via Zabbix tower tag)")
    far_end_ip: str | None = Field(None, description="Far-end management IP")
    companion: str | None = Field(None, description="Companion polarization device (H/V pairs)")
    companion_ip: str | None = Field(None, description="Companion IP")
    companion_far_end: str | None = Field(None, description="Companion far-end hostname")
    companion_far_end_ip: str | None = Field(None, description="Companion far-end IP")
    technology: str | None = Field(None, description="microwave, eband, mmwave, or unlicensed", examples=["microwave"])
    rain_sensitivity: str | None = Field(None, description="negligible, low, moderate, high, or extreme", examples=["moderate"])


# ── RF telemetry ─────────────────────────────────────────────────────

class RfSnapshot(BaseModel):
    """Live RF telemetry from Zabbix SNMP. Normalized across vendors."""
    model_config = ConfigDict(extra="allow")

    rsl: float | None = Field(None, description="Received Signal Level (dBm)", examples=[-42.5])
    snr: float | None = Field(None, description="Signal-to-Noise Ratio (dB)", examples=[32.0])
    txpower: float | None = Field(None, description="Transmit power (dBm)", examples=[20.0])
    rxmod: int | None = Field(None, description="RX modulation index", examples=[8])
    rxmod_name: str | None = Field(None, description="RX modulation name", examples=["256 QAM"])
    txmod: int | None = Field(None, description="TX modulation index", examples=[8])
    maxmod: int | None = Field(None, description="Max modulation index", examples=[10])
    maxmod_name: str | None = Field(None, description="Max modulation name", examples=["1024 QAM"])
    minmod: int | None = Field(None, description="Min modulation index (link failure floor)", examples=[1])
    minmod_name: str | None = Field(None, description="Min modulation name", examples=["QPSK"])
    ber: int | None = Field(None, description="Bit Error Rate (0 = clean)", examples=[0])
    rxcap: float | None = Field(None, description="RX capacity (Mbps)", examples=[350.0])
    txcap: float | None = Field(None, description="TX capacity (Mbps)", examples=[350.0])
    link_type: str | None = Field(None, description="Zabbix link type tag")
    max_capacity: str | None = Field(None, description="Zabbix max capacity tag")
    radio_count: int | None = Field(None, description="Carrier count (1=1+0, 2+=XPIC/2+0)", examples=[1])
    all_radios: list[dict[str, Any]] | None = Field(None, description="Per-carrier breakdown for multi-radio configs")
    snmp_down: bool | None = Field(None, description="SNMP collection failed — values may be stale")


# ── RSL baseline & history ───────────────────────────────────────────

class Baseline(BaseModel):
    """7-day RSL trend baseline from Zabbix."""
    baseline: float = Field(..., description="Median RSL over 7 days (dBm)", examples=[-41.2])
    stddev: float = Field(..., description="Stddev of hourly RSL averages (dB)", examples=[0.8])
    min: float | None = Field(None, description="Lowest RSL in 7-day window (dBm)", examples=[-45.0])
    max: float | None = Field(None, description="Highest RSL in 7-day window (dBm)", examples=[-39.5])
    sample_hours: int = Field(..., description="Hourly trend samples used", examples=[168])


class RslHistory(BaseModel):
    """Recent RSL history (last 60 min)."""
    points: int = Field(..., description="Data points in window", examples=[60])
    min: float = Field(..., description="Min RSL (dBm)", examples=[-43.2])
    max: float = Field(..., description="Max RSL (dBm)", examples=[-41.0])
    range_db: float = Field(..., description="RSL swing (dB)", examples=[2.2])
    latest: float = Field(..., description="Most recent RSL (dBm)", examples=[-42.5])
    stddev: float = Field(..., description="Stddev (dB)", examples=[0.4])


# ── Weather ──────────────────────────────────────────────────────────

class RecentRain(BaseModel):
    """Rain history from last 6 hours."""
    hours_checked: int = Field(..., description="Hours analyzed", examples=[6])
    total_rain_mm: float = Field(..., description="Total rain (mm)", examples=[3.2])
    max_rain_mm: float = Field(..., description="Peak hourly rate (mm)", examples=[1.8])
    max_rain_time: str | None = Field(None, description="Peak rain timestamp", examples=["2025-07-15T14:00"])
    had_rain: bool = Field(..., description="Any rain in window", examples=[True])
    rain_hours: list[dict[str, Any]] | None = Field(None, description="Per-hour breakdown")


class Weather(BaseModel):
    """Weather at the tower site."""
    model_config = ConfigDict(extra="allow")

    rain_rate_mm_hr: float = Field(..., description="Current rain rate (mm/hr)", examples=[4.2])
    rain_classification: str = Field(..., description="none / light / moderate / heavy / extreme", examples=["light"])
    humidity_pct: float | None = Field(None, description="Humidity (%)", examples=[78])
    wind_speed_mph: float | None = Field(None, description="Wind speed (mph)", examples=[12.5])
    temperature_f: float | None = Field(None, description="Temperature (F)", examples=[82.0])
    cloud_cover_pct: float | None = Field(None, description="Cloud cover (%)", examples=[65])
    description: str = Field("", description="Weather summary", examples=["Moderate Rain"])
    weather_source: str = Field("", description="openweathermap or open-meteo", examples=["openweathermap"])
    estimated_fade_db_per_km: float | None = Field(None, description="ITU-R P.838 rain attenuation (dB/km)")
    rain_fade_likely: bool = Field(..., description="Rain fade conditions active for this band")
    recent_rain: RecentRain | None = Field(None, description="6-hour rain history")
    rain_fade_recovering: bool | None = Field(None, description="Link may be recovering from recent rain")


# ── Link assessment (computed metrics) ───────────────────────────────

class ModulationHeadroom(BaseModel):
    """How far from link failure."""
    current_mod: int | None = Field(None, description="Current mod index", examples=[8])
    max_mod: int | None = Field(None, description="Max mod index", examples=[10])
    min_mod: int | None = Field(None, description="Min mod index (failure floor)", examples=[1])
    steps_below: int | None = Field(None, description="Steps below max", examples=[2])
    margin_pct: float | None = Field(None, description="100% = at max, 0% = failing", examples=[77.8])
    at_max: bool | None = Field(None, description="Running at max modulation", examples=[False])


class Assessment(BaseModel):
    """Computed link health metrics — live RF vs PCN design."""
    model_config = ConfigDict(extra="allow")

    off_target_db: float | None = Field(None, description="dB off PCN target. Positive = degraded. (TX_power - coordPower) + rxmaxPower - RSL", examples=[2.3])
    fade_margin_db: float | None = Field(None, description="Fade margin above RX threshold (dB)", examples=[28.5])
    baseline_delta_db: float | None = Field(None, description="RSL vs 7-day baseline. Positive = below baseline", examples=[1.2])
    adjusted_expected_rsl: float | None = Field(None, description="PCN RSL adjusted for actual TX power (dBm)", examples=[-40.8])
    modulation: ModulationHeadroom | None = Field(None, description="Modulation headroom")
    current_rsl: float | None = Field(None, description="Current RSL (dBm)", examples=[-42.5])
    snr: float | None = Field(None, description="Current SNR (dB)", examples=[32.0])
    ber: int | None = Field(None, description="Bit Error Rate", examples=[0])
    tx_power: float | None = Field(None, description="TX power (dBm)", examples=[20.0])
    rsl_delta_db: float | None = Field(None, description="RSL minus baseline (dB)", examples=[-1.3])
    baseline_rsl: float | None = Field(None, description="7-day baseline RSL (dBm)", examples=[-41.2])
    baseline_stddev: float | None = Field(None, description="Baseline stddev (dB)", examples=[0.8])
    pcn_rx_coord: float | None = Field(None, description="PCN RX coordinated power (dBm)")
    pcn_coord_power: float | None = Field(None, description="PCN TX coordinated power (dBm)")
    pcn_max_power: float | None = Field(None, description="PCN max allowed TX power (dBm)")
    pcn_antenna_model: str | None = Field(None, description="Antenna model from PCN")
    pcn_antenna_size_ft: float | None = Field(None, description="Antenna diameter (ft)")
    pcn_radio_model: str | None = Field(None, description="Radio model from PCN")
    pcn_azimuth: float | None = Field(None, description="Design azimuth (degrees)")
    estimated_rain_fade_db: float | None = Field(None, description="Estimated rain fade for this path (dB)", examples=[3.8])


# ── Diagnosis verdict ────────────────────────────────────────────────

class VerdictAccuracy(BaseModel):
    """Accuracy stats from tech feedback."""
    verdict: str = Field(..., description="Verdict type", examples=["rain_fade"])
    total: int = Field(..., description="Total feedback count", examples=[47])
    accurate: int = Field(..., description="Marked accurate", examples=[41])
    inaccurate: int = Field(..., description="Marked inaccurate", examples=[6])
    accuracy_pct: float | None = Field(None, description="Accuracy %", examples=[87.2])


class Verdict(BaseModel):
    """Diagnosis verdict. Highest-scoring category wins.

    Verdicts: `rain_fade`, `off_target`, `hardware_issue`,
    `alignment_issue`, `interference`, `normal`, `insufficient_data`
    """
    verdict: str = Field(..., description="Diagnosis category", examples=["rain_fade"])
    confidence: int = Field(..., description="Confidence % (0-100), blended with feedback history", examples=[72])
    severity: str = Field(..., description="normal / minor / moderate / severe / critical", examples=["minor"])
    findings: list[str] = Field(..., description="Diagnostic findings")
    recommendations: list[str] = Field(..., description="Recommended next steps")
    scores: dict[str, float] | None = Field(None, description="Raw scores per verdict category")
    raw_confidence: int | None = Field(None, description="Confidence before feedback blending")
    accuracy: VerdictAccuracy | None = Field(None, description="Accuracy stats for this verdict type")


# ── Zabbix problems ──────────────────────────────────────────────────

class ZabbixProblem(BaseModel):
    """Active Zabbix problem."""
    model_config = ConfigDict(extra="allow")

    eventid: str = Field(..., description="Zabbix event ID", examples=["184523"])
    name: str = Field(..., description="Problem name", examples=["SNMP agent is not available"])
    severity: str = Field(..., description="0=not classified ... 5=disaster", examples=["3"])
    clock: str = Field(..., description="Unix timestamp", examples=["1720024800"])


# ═══════════════════════════════════════════════════════════════════════
#  Top-level endpoint response models
# ═══════════════════════════════════════════════════════════════════════

class DiagnoseResponse(BaseModel):
    """Full diagnostic report for a backhaul link."""
    hostname: str = Field(..., description="Queried BH device hostname")
    ip: str | None = Field(None, description="Management IP")
    link_info: LinkInfo = Field(..., description="Parsed link topology")
    rf_snapshot: dict[str, Any] = Field(default_factory=dict, description="Live RF from Zabbix SNMP")
    far_end_rf: dict[str, Any] | None = Field(None, description="Far-end RF telemetry")
    pcn: dict[str, Any] | None = Field(None, description="FCC PCN design values (SMAP or CoMSearch)")
    baseline: Baseline | None = Field(None, description="7-day RSL baseline")
    rsl_history: RslHistory | None = Field(None, description="Last 60 min RSL data")
    weather: dict[str, Any] | None = Field(None, description="Weather + rain fade analysis")
    assessment: dict[str, Any] = Field(default_factory=dict, description="Off-target, fade margin, mod headroom")
    verdict: Verdict = Field(..., description="Diagnosis verdict + recommendations")
    active_problems: list[dict[str, Any]] = Field(default_factory=list, description="Active Zabbix problems")

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "hostname": "BH-AV4200-18-1.SITEA.SITEB",
                    "ip": "10.0.100.15",
                    "link_info": {
                        "model": "AV4200",
                        "radio_family": "aviat",
                        "band_ghz": 18,
                        "tower_a": "SITEA",
                        "tower_z": "SITEB",
                        "far_end": "BH-AV4200-18-1.SITEB.SITEA",
                        "far_end_ip": "10.0.100.16",
                        "companion": None,
                        "companion_ip": None,
                        "companion_far_end": None,
                        "companion_far_end_ip": None,
                        "technology": "microwave",
                        "rain_sensitivity": "moderate",
                    },
                    "rf_snapshot": {
                        "rsl": -42.5,
                        "snr": 32.0,
                        "txpower": 20.0,
                        "rxmod": 8,
                        "rxmod_name": "256 QAM",
                        "txmod": 8,
                        "maxmod": 10,
                        "maxmod_name": "1024 QAM",
                        "minmod": 1,
                        "minmod_name": "QPSK",
                        "ber": 0,
                        "rxcap": 350.0,
                        "txcap": 350.0,
                        "link_type": "PTP",
                        "radio_count": 1,
                    },
                    "far_end_rf": {
                        "rsl": -41.8,
                        "snr": 33.0,
                        "txpower": 20.0,
                        "rxmod": 9,
                        "rxmod_name": "512 QAM",
                        "maxmod": 10,
                        "maxmod_name": "1024 QAM",
                        "radio_count": 1,
                    },
                    "pcn": {
                        "host": "BH-AV4200-18-1.SITEA.SITEB",
                        "site1": "SITEA",
                        "site2": "SITEB",
                        "radiomodel1": "Aviat WTM4200",
                        "mainmodel1": "VHLP2-18",
                        "maindiameter1(ft)": 2.0,
                        "maxPower1(dBm)": 23.0,
                        "coordPower1(dBm)": 20.0,
                        "rxmaxPower1(dBm)": -39.5,
                        "rxcoordPower1(dBm)": -70.0,
                        "azimuth12(deg)": 245.3,
                        "distance(mi)": 8.2,
                        "band": "18 GHz",
                        "_pcn_source": "SMAP bh_report_history",
                    },
                    "baseline": {
                        "baseline": -41.2,
                        "stddev": 0.8,
                        "min": -45.0,
                        "max": -39.5,
                        "sample_hours": 168,
                    },
                    "rsl_history": {
                        "points": 60,
                        "min": -43.2,
                        "max": -41.0,
                        "range_db": 2.2,
                        "latest": -42.5,
                        "stddev": 0.4,
                    },
                    "weather": {
                        "rain_rate_mm_hr": 4.2,
                        "rain_classification": "light",
                        "humidity_pct": 78,
                        "wind_speed_mph": 12.5,
                        "temperature_f": 82.0,
                        "cloud_cover_pct": 65,
                        "description": "Moderate Rain",
                        "weather_source": "openweathermap",
                        "estimated_fade_db_per_km": 0.84,
                        "rain_fade_likely": False,
                        "recent_rain": {
                            "hours_checked": 6,
                            "total_rain_mm": 8.4,
                            "max_rain_mm": 4.2,
                            "max_rain_time": "2025-07-15T14:00",
                            "had_rain": True,
                        },
                    },
                    "assessment": {
                        "off_target_db": 2.5,
                        "fade_margin_db": 28.5,
                        "baseline_delta_db": 1.3,
                        "adjusted_expected_rsl": -39.5,
                        "modulation": {
                            "current_mod": 8,
                            "max_mod": 10,
                            "min_mod": 1,
                            "steps_below": 2,
                            "margin_pct": 77.8,
                            "at_max": False,
                        },
                        "current_rsl": -42.5,
                        "snr": 32.0,
                        "ber": 0,
                        "tx_power": 20.0,
                        "pcn_antenna_model": "VHLP2-18",
                        "pcn_antenna_size_ft": 2.0,
                        "pcn_radio_model": "Aviat WTM4200",
                        "pcn_azimuth": 245.3,
                    },
                    "verdict": {
                        "verdict": "rain_fade",
                        "confidence": 72,
                        "severity": "minor",
                        "findings": [
                            "Rain detected: 4.2 mm/hr, estimated fade 0.8 dB/km at 18 GHz",
                            "On-target: +2.5 dB from PCN design (within +/-3 dB)",
                            "Zabbix baseline stable: +1.3 dB from 7d median",
                            "Both ends show similar RSL — path issue (rain/obstruction)",
                        ],
                        "recommendations": [
                            "Monitor — rain fade is temporary, link should recover when weather clears",
                            "Check if link has adequate fade margin for this rain region",
                        ],
                        "scores": {"rain_fade": 50.0, "normal": 30.0},
                        "raw_confidence": 68,
                        "accuracy": {
                            "verdict": "rain_fade",
                            "total": 47,
                            "accurate": 41,
                            "inaccurate": 6,
                            "accuracy_pct": 87.2,
                        },
                    },
                    "active_problems": [],
                }
            ]
        }
    )


# ── Search ───────────────────────────────────────────────────────────

class SearchResult(BaseModel):
    """Matching BH device."""
    hostname: str = Field(..., description="BH device hostname")
    name: str = Field("", description="Zabbix visible name")
    tower: str = Field("", description="Tower tag")
    link_type: str = Field("", description="Link type tag", examples=["PTP"])
    ip: str = Field("", description="Management IP")


class SearchResponse(BaseModel):
    """Search results (deduped by Zabbix host ID)."""
    results: list[SearchResult] = Field(..., description="Matching BH devices")

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "results": [
                        {
                            "hostname": "BH-AV4200-18-1.SITEA.SITEB",
                            "name": "BH-AV4200-18-1.SITEA.SITEB",
                            "tower": "SITEA",
                            "link_type": "PTP",
                            "ip": "10.0.100.15",
                        },
                        {
                            "hostname": "BH-AV4200-11-1.SITEA.SITEC",
                            "name": "BH-AV4200-11-1.SITEA.SITEC",
                            "tower": "SITEA",
                            "link_type": "PTP",
                            "ip": "10.0.100.20",
                        },
                    ]
                }
            ]
        }
    )


# ── Tower ────────────────────────────────────────────────────────────

class TowerDevice(BaseModel):
    """BH device at a tower."""
    hostname: str = Field(..., description="BH hostname")
    name: str = Field("", description="Zabbix visible name")
    tower: str = Field("", description="Tower tag")
    link_type: str = Field("", description="Link type")
    max_capacity: str = Field("", description="Max capacity")
    technology: str = Field("", description="microwave, eband, etc.")


class TowerResponse(BaseModel):
    """All BH devices at a tower."""
    tower: str = Field(..., description="Tower name queried")
    devices: list[TowerDevice] = Field(..., description="BH devices at this tower")

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "tower": "SITEA",
                    "devices": [
                        {
                            "hostname": "BH-AV4200-18-1.SITEA.SITEB",
                            "name": "BH-AV4200-18-1.SITEA.SITEB",
                            "tower": "SITEA",
                            "link_type": "PTP",
                            "max_capacity": "700 Mbps",
                            "technology": "microwave",
                        },
                        {
                            "hostname": "BH-AV4200-11-1.SITEA.SITEC",
                            "name": "BH-AV4200-11-1.SITEA.SITEC",
                            "tower": "SITEA",
                            "link_type": "PTP",
                            "max_capacity": "500 Mbps",
                            "technology": "microwave",
                        },
                    ],
                }
            ]
        }
    )


# ── RF snapshot (standalone) ─────────────────────────────────────────

class RfSnapshotResponse(BaseModel):
    """Raw RF snapshot for a device."""
    hostname: str = Field(..., description="Queried hostname")
    rf_snapshot: dict[str, Any] = Field(default_factory=dict, description="RF telemetry")


# ── Feedback ─────────────────────────────────────────────────────────

class FeedbackResponse(BaseModel):
    """Feedback result. If already_reported is true, the duplicate was rejected."""
    saved: bool = Field(..., description="Whether new feedback was saved", examples=[True])
    id: int | None = Field(None, description="Row ID (null if duplicate)", examples=[142])
    already_reported: bool | None = Field(None, description="True if feedback already exists for this link + verdict")
    existing_id: int | None = Field(None, description="ID of existing feedback record (if duplicate)")
    message: str | None = Field(None, description="Status message")
    accuracy: VerdictAccuracy = Field(..., description="Accuracy stats for this verdict type")


# ── Health ───────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    """Health check."""
    status: str = Field(..., description="Service status", examples=["ok"])
