"""Zabbix JSON-RPC client for RF metric retrieval."""

from __future__ import annotations

import time
from typing import Any, Optional

import httpx

from src.config import settings
from src.topology.hostname_parser import tower_tag_variants


# Aviat item key suffix → normalized name
AVIAT_KEY_MAP = {
    "rsl": "rsl",
    "snr": "snr",
    "txmod": "txmod",
    "rxmod": "rxmod",
    "maxmod": "maxmod",
    "minmod": "minmod",
    "txpower": "txpower",
    "ber": "ber",
    "rxcap": "rxcap",
    "txcap": "txcap",
    "licensedcap": "licensedcap",
    "txfreq": "txfreq",
    "rxfreq": "rxfreq",
}

# Cambium CN820 item key suffix → normalized name
CN820_KEY_MAP = {
    "rxlvl": "rsl",
    "txmod": "txmod",
    "rxmod": "rxmod",
    "txcap": "txcap",
    "rxcap": "rxcap",
    "txfreq": "txfreq",
    "rxfreq": "rxfreq",
    "txpower": "txpower_max",
    "genEquipRfuStatusTxLevel": "txpower",
    "txprof": "txprof",
    "rxprof": "rxprof",
}

# Siklu item key suffix → normalized name
SIKLU_KEY_MAP = {
    "AverageRssi": "rsl",
    "AverageCinr": "snr",
    "TxPower": "txpower",
    "ModulationName": "rxmod",
    "ChannelWidth": "channel_width",
}

# Aviat modulation index → QAM name
AVIAT_MOD_NAMES = {
    0: "BPSK", 1: "QPSK", 2: "QPSK",
    3: "8 QAM", 4: "16 QAM", 5: "32 QAM",
    6: "64 QAM", 7: "128 QAM", 8: "256 QAM",
    9: "512 QAM", 10: "1024 QAM", 11: "2048 QAM",
    12: "4096 QAM", 13: "4096 QAM", 14: "4096 QAM",
}

# Radio family → Zabbix item key prefix
RF_PREFIX_MAP = {
    "aviat": "av.wireless",
    "cambium_cn820": "820.wireless",
    "siklu": "system.rf",
}


class ZabbixClient:
    """Async Zabbix JSON-RPC client."""

    def __init__(self) -> None:
        self._url = settings.zabbix_url
        self._token = settings.zabbix_api_token
        self._client = httpx.AsyncClient(timeout=15.0, follow_redirects=True)
        self._req_id = 0

    async def jsonrpc(self, method: str, params: dict) -> Any:
        self._req_id += 1
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "auth": self._token,
            "id": self._req_id,
        }
        resp = await self._client.post(self._url, json=payload)
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            raise RuntimeError(f"Zabbix API error: {body['error']}")
        return body.get("result")

    # ── Host resolution ───────────────────────────────────────────────

    async def resolve_host(self, hostname: str) -> Optional[dict]:
        """Resolve a hostname to hostid + tags + IP."""
        result = await self.jsonrpc("host.get", {
            "filter": {"host": [hostname]},
            "output": ["hostid", "host", "name"],
            "selectTags": "extend",
            "selectInterfaces": ["ip", "type"],
            "limit": 1,
        })
        if not result:
            return None
        host = result[0]
        host["tags_dict"] = {t["tag"]: t["value"] for t in host.get("tags", [])}
        interfaces = host.get("interfaces", [])
        host["ip"] = interfaces[0]["ip"] if interfaces else None
        return host

    async def search_hosts(self, pattern: str) -> list[dict]:
        """Search hosts by name pattern (for BH- prefix devices)."""
        return await self.jsonrpc("host.get", {
            "search": {"host": pattern},
            "output": ["hostid", "host", "name"],
            "selectTags": "extend",
            "selectInterfaces": ["ip"],
            "limit": 50,
        }) or []

    async def search_by_ip(self, ip: str) -> list[dict]:
        """Search for BH hosts by IP address."""
        hosts = await self.jsonrpc("hostinterface.get", {
            "filter": {"ip": [ip]},
            "output": ["hostid", "ip"],
            "limit": 10,
        }) or []
        if not hosts:
            return []
        hostids = list({h["hostid"] for h in hosts})
        result = await self.jsonrpc("host.get", {
            "hostids": hostids,
            "output": ["hostid", "host", "name"],
            "selectTags": "extend",
            "selectInterfaces": ["ip"],
            "limit": 10,
        }) or []
        return [h for h in result if h.get("host", "").upper().startswith("BH-")]

    # ── RF snapshot ───────────────────────────────────────────────────

    async def get_rf_snapshot(
        self, hostname: str, rf_key_prefix: str
    ) -> dict[str, Any]:
        """Fetch current RF metrics (RSL, mod, SNR, TX power, etc.)."""
        host = await self.resolve_host(hostname)
        if not host:
            return {}

        items = await self.jsonrpc("item.get", {
            "hostids": [host["hostid"]],
            "search": {"key_": rf_key_prefix},
            "output": ["key_", "lastvalue", "lastclock", "name", "units"],
            "limit": 50,
        })

        # For Siklu, also grab siklu.if items for throughput data
        if "system.rf" in rf_key_prefix:
            siklu_if_items = await self.jsonrpc("item.get", {
                "hostids": [host["hostid"]],
                "search": {"key_": "siklu.if"},
                "output": ["key_", "lastvalue", "lastclock", "name", "units"],
                "limit": 20,
            })
            if siklu_if_items:
                items = (items or []) + siklu_if_items

        if not items:
            return {}

        return self._normalize_rf_items(items, rf_key_prefix, host.get("tags_dict", {}))

    # ── RSL trend baseline ────────────────────────────────────────────

    async def get_rsl_trend(
        self, hostname: str, rf_key_prefix: str, days: int = 7
    ) -> Optional[dict]:
        """Compute RSL baseline and volatility from Zabbix trend data."""
        host = await self.resolve_host(hostname)
        if not host:
            return None

        if "system.rf" in rf_key_prefix:
            rsl_key = "system.rfAverageRssi"
        elif "av.wireless" in rf_key_prefix:
            rsl_key = f"{rf_key_prefix}.rsl"
        else:
            rsl_key = f"{rf_key_prefix}.rxlvl"
        items = await self.jsonrpc("item.get", {
            "hostids": [host["hostid"]],
            "search": {"key_": rsl_key},
            "output": ["itemid", "key_"],
            "limit": 4,
        })
        if not items:
            return None

        itemids = [i["itemid"] for i in items]
        time_from = int(time.time()) - (days * 86400)
        trends = await self.jsonrpc("trend.get", {
            "itemids": itemids,
            "time_from": time_from,
            "output": ["itemid", "clock", "value_avg", "value_min", "value_max"],
            "limit": days * 24 * len(itemids),
        })
        if not trends:
            return None

        averages, all_mins, all_maxs = [], [], []
        for t in trends:
            avg = float(t.get("value_avg", 0))
            if avg != 0:
                averages.append(avg)
            vmin = float(t.get("value_min", 0))
            vmax = float(t.get("value_max", 0))
            if vmin != 0 and vmax != 0:
                all_mins.append(vmin)
                all_maxs.append(vmax)

        if not averages:
            return None

        import statistics
        baseline = statistics.median(averages)
        stddev = statistics.stdev(averages) if len(averages) > 1 else 0.0

        return {
            "baseline": round(baseline, 1),
            "stddev": round(stddev, 2),
            "min": round(min(all_mins), 1) if all_mins else None,
            "max": round(max(all_maxs), 1) if all_maxs else None,
            "sample_hours": len(averages),
        }

    # ── RSL history (recent) ──────────────────────────────────────────

    async def get_rsl_history(
        self, hostname: str, rf_key_prefix: str, window_minutes: int = 60
    ) -> Optional[dict]:
        """Fetch recent RSL history for stability analysis."""
        host = await self.resolve_host(hostname)
        if not host:
            return None

        if "system.rf" in rf_key_prefix:
            rsl_key = "system.rfAverageRssi"
        elif "av.wireless" in rf_key_prefix:
            rsl_key = f"{rf_key_prefix}.rsl"
        else:
            rsl_key = f"{rf_key_prefix}.rxlvl"
        items = await self.jsonrpc("item.get", {
            "hostids": [host["hostid"]],
            "search": {"key_": rsl_key},
            "output": ["itemid"],
            "limit": 1,
        })
        if not items:
            return None

        time_from = int(time.time()) - (window_minutes * 60)
        history = await self.jsonrpc("history.get", {
            "itemids": [items[0]["itemid"]],
            "time_from": time_from,
            "output": "extend",
            "sortfield": "clock",
            "sortorder": "ASC",
            "limit": 500,
        })
        if not history:
            return None

        values = [float(h["value"]) for h in history if h.get("value")]
        if not values:
            return None

        return {
            "points": len(values),
            "min": round(min(values), 1),
            "max": round(max(values), 1),
            "range_db": round(max(values) - min(values), 1),
            "latest": round(values[-1], 1),
            "stddev": round(__import__("statistics").stdev(values), 2) if len(values) > 1 else 0.0,
        }

    # ── Active problems ───────────────────────────────────────────────

    async def get_active_problems(self, hostname: str) -> list[dict]:
        """Get active Zabbix problems for a host."""
        host = await self.resolve_host(hostname)
        if not host:
            return []

        return await self.jsonrpc("problem.get", {
            "hostids": [host["hostid"]],
            "output": ["eventid", "name", "severity", "clock"],
            "recent": True,
            "sortfield": "eventid",
            "sortorder": "DESC",
            "limit": 20,
        }) or []

    # ── Normalization ─────────────────────────────────────────────────

    async def find_far_end(
        self, hostname: str, tower_a: str, tower_z: str,
        exclude: Optional[list[str]] = None,
    ) -> Optional[str]:
        """Find the far-end hostname by searching for BH devices at far tower.

        Handles -H/-V polarization suffixes: if tower_z is "TX-HOHQ-H" but
        the Zabbix tag is "TX-HOHQ", the stripped variant is tried as well.

        ``exclude`` skips specific hostnames (e.g. the companion polarization
        device which shares both tower names but is NOT the far end).
        """
        excluded = {hostname.upper()}
        if exclude:
            excluded.update(h.upper() for h in exclude if h)

        # Tower name variants (original, stripped pol, stripped state prefix)
        tz_variants = tower_tag_variants(tower_z)
        ta_upper = tower_a.upper()

        # Search for BH devices at tower_z that reference tower_a
        for tz in tz_variants:
            far_hosts = await self.search_by_tower(tz)
            for h in far_hosts:
                h_name = h.get("host", "")
                if h_name.upper() not in excluded and ta_upper in h_name.upper():
                    return h_name

        # Also try: BH devices matching tower_a in hostname at tower_z
        hosts = await self.jsonrpc("host.get", {
            "search": {"host": tower_a},
            "output": ["hostid", "host"],
            "selectTags": "extend",
            "limit": 30,
        }) or []
        for h in hosts:
            h_name = h.get("host", "")
            if h_name.upper() in excluded or not h_name.upper().startswith("BH-"):
                continue
            h_upper = h_name.upper()
            if any(tz.upper() in h_upper for tz in tz_variants):
                return h_name

        return None

    async def search_by_tower(self, tower: str) -> list[dict]:
        """Search for BH hosts at a tower by tag."""
        hosts = await self.jsonrpc("host.get", {
            "tags": [{"tag": "tower", "value": tower, "operator": 1}],
            "output": ["hostid", "host", "name"],
            "selectTags": "extend",
            "limit": 100,
        }) or []
        # Filter to BH- devices only
        return [h for h in hosts if h.get("host", "").upper().startswith("BH-")]

    def _normalize_rf_items(
        self, items: list, rf_key_prefix: str, tags: dict
    ) -> dict[str, Any]:
        if "system.rf" in rf_key_prefix:
            key_map = SIKLU_KEY_MAP
        elif "av.wireless" in rf_key_prefix:
            key_map = AVIAT_KEY_MAP
        else:
            key_map = CN820_KEY_MAP
        prefix = rf_key_prefix + "." if "system.rf" not in rf_key_prefix else rf_key_prefix

        radio_items: dict[str, dict] = {}
        for item in items:
            key = item.get("key_", "")
            if not key.startswith(prefix):
                continue

            remainder = key[len(prefix):]
            bracket_idx = remainder.find("[")
            if bracket_idx >= 0:
                suffix = remainder[:bracket_idx]
                radio_id = remainder[bracket_idx + 1 : remainder.find("]")]
            else:
                suffix = remainder
                radio_id = "0"

            if radio_id not in radio_items:
                radio_items[radio_id] = {}

            normalized_name = key_map.get(suffix)
            if normalized_name:
                try:
                    val = item.get("lastvalue", "")
                    if normalized_name in (
                        "ber", "txmod", "rxmod", "maxmod", "minmod",
                        "txfreq", "rxfreq", "txprof", "rxprof",
                    ):
                        radio_items[radio_id][normalized_name] = int(float(val)) if val else None
                    else:
                        radio_items[radio_id][normalized_name] = float(val) if val else None
                except (ValueError, TypeError):
                    radio_items[radio_id][normalized_name] = None

        if not radio_items:
            return {}

        # Add modulation names to all radios
        is_aviat = "av.wireless" in rf_key_prefix
        for rid, r in radio_items.items():
            if is_aviat:
                rxmod = r.get("rxmod")
                maxmod = r.get("maxmod")
                minmod = r.get("minmod")
                if rxmod is not None:
                    r["rxmod_name"] = AVIAT_MOD_NAMES.get(rxmod, f"Mod {rxmod}")
                if maxmod is not None:
                    r["maxmod_name"] = AVIAT_MOD_NAMES.get(maxmod, f"Mod {maxmod}")
                if minmod is not None:
                    r["minmod_name"] = AVIAT_MOD_NAMES.get(minmod, f"Mod {minmod}")

        sorted_ids = sorted(radio_items.keys())
        result = radio_items[sorted_ids[0]]
        result["link_type"] = tags.get("link_type")
        result["max_capacity"] = tags.get("max_capacity")
        result["radio_count"] = len(sorted_ids)

        if len(sorted_ids) > 1:
            all_radios = []
            for rid in sorted_ids:
                r = radio_items[rid]
                all_radios.append({
                    "radio_id": rid,
                    "rsl": r.get("rsl"),
                    "snr": r.get("snr"),
                    "rxmod": r.get("rxmod"),
                    "rxmod_name": r.get("rxmod_name"),
                    "txmod": r.get("txmod"),
                    "maxmod": r.get("maxmod"),
                    "maxmod_name": r.get("maxmod_name"),
                    "minmod": r.get("minmod"),
                    "ber": r.get("ber"),
                    "txpower": r.get("txpower"),
                    "rxcap": r.get("rxcap"),
                    "txcap": r.get("txcap"),
                })
            result["all_radios"] = all_radios

        return result

    async def close(self) -> None:
        await self._client.aclose()
