"""Parse BH radio hostnames into structured link information.

Ported from ACS — same hostname conventions.

Hostname examples:
  BH-AV4200-18-1.BULLOCK.WEATHERFORDNORTH
  BH-AV4100-11-1.TX-EUSTACE-SO-1.TX-CANEYCITY-CN-1
  BH-CN820S-18-1.125091013.SOUTHCLEBURNE
  BH-CN820C-11-1.WALNUTSPRINGS.MORGANWT
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


# Radio model -> family
FAMILY_MAP: dict[str, str] = {
    "AV4200": "aviat",
    "AV4100": "aviat",
    "AV4800": "aviat",
    "CN820S": "cambium_cn820",
    "CN820C": "cambium_cn820",
    "CN850C": "cambium_cn820",
    "CNPTP450": "cambium_ptp",
    "CNF16": "cambium_force",
    "CNF30025": "cambium_force",
    "CNEP3K": "cambium_epmp",
    "UBAF11FX": "ubiquiti_af",
    "UBAF5XHD": "ubiquiti_af",
    "UBWPRO": "ubiquiti_wave",
    "UPBAC": "ubiquiti_pb",
    "UBPB5AC": "ubiquiti_pb",
    "SK2500": "siklu",
    "SK1200": "siklu",
    "SK8010": "siklu",
    "RFE30": "cambium_force",
    "WTM4200": "aviat",
    "WTM4800": "aviat",
    "WTM4100": "aviat",
}

# Radio family -> Zabbix item key prefix
RF_PREFIX: dict[str, str] = {
    "aviat": "av.wireless",
    "cambium_cn820": "820.wireless",
    "siklu": "system.rf",
}

KNOWN_BANDS = {5, 6, 11, 18, 24, 60, 70, 80}

BAND_TECHNOLOGY: dict[int, str] = {
    5: "unlicensed", 6: "microwave", 11: "microwave", 18: "microwave",
    24: "microwave", 60: "mmwave", 70: "eband", 80: "eband",
}

BAND_RAIN_SENSITIVITY: dict[int, str] = {
    5: "negligible", 6: "negligible", 11: "low", 18: "moderate",
    24: "high", 60: "extreme", 70: "extreme", 80: "extreme",
}


@dataclass(frozen=True)
class BhLinkInfo:
    hostname: str
    model: str
    radio_family: str
    band_ghz: Optional[int]
    tower_a: str
    tower_z: str
    rf_key_prefix: Optional[str]

    @property
    def technology(self) -> str:
        if self.band_ghz is None:
            return "unknown"
        return BAND_TECHNOLOGY.get(self.band_ghz, "unknown")

    @property
    def rain_sensitivity(self) -> str:
        if self.band_ghz is None:
            return "unknown"
        return BAND_RAIN_SENSITIVITY.get(self.band_ghz, "unknown")

    @property
    def far_end_hostname(self) -> Optional[str]:
        dot_idx = self.hostname.find(".")
        if dot_idx < 0:
            return None
        prefix = self.hostname[:dot_idx]
        return f"{prefix}.{self.tower_z}.{self.tower_a}"


_BH_RE = re.compile(
    r"^(?:z)?BH-"
    r"(?P<model>[A-Z][A-Z0-9]+)"
    r"(?:-(?P<band>\d{1,2}))?"
    r"(?:-(?P<suffix>[^.]+))?"
    r"(?:\.(?P<rest>.+))?$",
    re.IGNORECASE,
)


def parse_bh_hostname(hostname: str) -> Optional[BhLinkInfo]:
    """Parse a BH hostname into structured link information."""
    m = _BH_RE.match(hostname)
    if not m:
        return None

    model = m.group("model").upper()
    band_str = m.group("band")
    suffix = m.group("suffix") or ""
    rest = m.group("rest")

    family = FAMILY_MAP.get(model, "unknown")

    band_ghz: Optional[int] = None
    if band_str:
        candidate = int(band_str)
        if candidate in KNOWN_BANDS:
            band_ghz = candidate

    tower_a, tower_z = _extract_towers(rest or "", suffix)

    if not tower_a or not tower_z:
        if rest and not tower_a:
            embedded = _extract_tower_from_suffix(suffix) if suffix else None
            if embedded:
                tower_a = embedded
                tower_z = rest
        if not tower_a or not tower_z:
            return None

    rf_prefix = RF_PREFIX.get(family)

    return BhLinkInfo(
        hostname=hostname,
        model=model,
        radio_family=family,
        band_ghz=band_ghz,
        tower_a=tower_a,
        tower_z=tower_z,
        rf_key_prefix=rf_prefix,
    )


def _extract_towers(rest: str, suffix: str) -> tuple[Optional[str], Optional[str]]:
    parts = rest.split(".")
    if len(parts) >= 2:
        return parts[0], ".".join(parts[1:])
    if suffix and parts:
        embedded = _extract_tower_from_suffix(suffix)
        if embedded:
            return embedded, parts[0]
    return None, None


def _extract_tower_from_suffix(suffix: str) -> Optional[str]:
    cleaned = re.sub(r"^(\d+-)+", "", suffix)
    if not cleaned or cleaned.isdigit():
        return None
    # Strip leading "BH-" — sometimes tower names get prefixed with it
    if cleaned.upper().startswith("BH-"):
        cleaned = cleaned[3:]
    if not cleaned:
        return None
    if re.match(r"^[A-Z]", cleaned, re.IGNORECASE):
        return cleaned
    return None
