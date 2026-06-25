"""Weather client for rain fade detection using Open-Meteo API."""

from __future__ import annotations

from typing import Any, Optional

import httpx

from src.config import settings


# Rain rate thresholds (mm/hr) for fade classification
RAIN_THRESHOLDS = {
    "none": 0.0,
    "light": 2.5,
    "moderate": 7.5,
    "heavy": 25.0,
    "extreme": 50.0,
}


class WeatherClient:
    """Fetch weather conditions for tower locations via Open-Meteo."""

    def __init__(self) -> None:
        self._base_url = settings.weather_api_url
        self._client = httpx.AsyncClient(timeout=10.0)

    async def get_conditions(
        self, lat: float, lon: float
    ) -> Optional[dict[str, Any]]:
        """Fetch current weather conditions for a lat/lon."""
        try:
            resp = await self._client.get(
                f"{self._base_url}/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": ",".join([
                        "temperature_2m",
                        "relative_humidity_2m",
                        "precipitation",
                        "rain",
                        "wind_speed_10m",
                        "wind_direction_10m",
                        "cloud_cover",
                        "weather_code",
                    ]),
                    "temperature_unit": "fahrenheit",
                    "wind_speed_unit": "mph",
                    "precipitation_unit": "mm",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return self._normalize(data)
        except Exception:
            return None

    async def get_recent_rain(self, lat: float, lon: float, hours: int = 6) -> Optional[dict]:
        """Fetch hourly rain history for the last N hours."""
        try:
            resp = await self._client.get(
                f"{self._base_url}/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "hourly": "precipitation,rain,weather_code",
                    "precipitation_unit": "mm",
                    "past_hours": hours,
                    "forecast_hours": 0,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            hourly = data.get("hourly", {})
            times = hourly.get("time", [])
            precip = hourly.get("precipitation", [])
            rain = hourly.get("rain", [])
            codes = hourly.get("weather_code", [])

            if not times:
                return None

            # Find max rain and total
            max_rain = 0.0
            max_rain_time = None
            total_rain = 0.0
            rain_hours = []
            for i, t in enumerate(times):
                r = rain[i] if i < len(rain) else 0
                p = precip[i] if i < len(precip) else 0
                val = max(r, p)
                total_rain += val
                if val > 0:
                    code = codes[i] if i < len(codes) else 0
                    rain_hours.append({
                        "time": t,
                        "rain_mm": round(val, 1),
                        "description": WMO_CODES.get(code, ""),
                    })
                if val > max_rain:
                    max_rain = val
                    max_rain_time = t

            return {
                "hours_checked": hours,
                "total_rain_mm": round(total_rain, 1),
                "max_rain_mm": round(max_rain, 1),
                "max_rain_time": max_rain_time,
                "had_rain": total_rain > 0,
                "rain_hours": rain_hours,
            }
        except Exception:
            return None

    async def check_rain_fade(
        self,
        lat: float,
        lon: float,
        band_ghz: Optional[int] = None,
    ) -> dict[str, Any]:
        """Check if rain fade conditions exist at a location.

        Fetches current conditions AND 6-hour rain history.
        """
        conditions = await self.get_conditions(lat, lon)
        if not conditions:
            return {"rain_fade_likely": False, "reason": "weather data unavailable"}

        rain_rate = conditions.get("rain_rate_mm_hr", 0.0)
        classification = self._classify_rain(rain_rate)

        result: dict[str, Any] = {
            "rain_rate_mm_hr": rain_rate,
            "rain_classification": classification,
            "humidity_pct": conditions.get("humidity"),
            "wind_speed_mph": conditions.get("wind_speed"),
            "temperature_f": conditions.get("temperature_f"),
            "cloud_cover_pct": conditions.get("cloud_cover_pct"),
            "description": conditions.get("description", ""),
        }

        # Current rain fade
        if band_ghz and rain_rate > 0:
            from src.pcn.calculator import estimate_rain_attenuation
            result["estimated_fade_db_per_km"] = estimate_rain_attenuation(
                band_ghz, rain_rate
            )
            result["rain_fade_likely"] = classification in ("moderate", "heavy", "extreme")
        else:
            result["rain_fade_likely"] = False

        # 6-hour rain history
        recent = await self.get_recent_rain(lat, lon, hours=6)
        if recent:
            result["recent_rain"] = recent
            # If no current rain but recent rain, flag as recovering
            if not result["rain_fade_likely"] and recent["had_rain"]:
                result["rain_fade_recovering"] = True

        return result

    def _normalize(self, data: dict) -> dict[str, Any]:
        """Normalize Open-Meteo response to a consistent format."""
        current = data.get("current", {})

        # WMO weather codes → descriptions
        wmo_code = current.get("weather_code", 0)
        description = WMO_CODES.get(wmo_code, "Unknown")

        # Open-Meteo gives precipitation in mm for the current interval;
        # estimate hourly rate from current reading
        precip_mm = current.get("precipitation", 0) or 0
        rain_mm = current.get("rain", 0) or 0
        # Use the larger of precipitation/rain as the rate indicator
        rain_rate = max(precip_mm, rain_mm)

        return {
            "temperature_f": current.get("temperature_2m"),
            "humidity": current.get("relative_humidity_2m"),
            "wind_speed": current.get("wind_speed_10m"),
            "wind_direction": current.get("wind_direction_10m"),
            "rain_rate_mm_hr": rain_rate,
            "cloud_cover_pct": current.get("cloud_cover"),
            "description": description,
            "weather_code": wmo_code,
        }

    @staticmethod
    def _classify_rain(rain_rate: float) -> str:
        if rain_rate >= RAIN_THRESHOLDS["extreme"]:
            return "extreme"
        if rain_rate >= RAIN_THRESHOLDS["heavy"]:
            return "heavy"
        if rain_rate >= RAIN_THRESHOLDS["moderate"]:
            return "moderate"
        if rain_rate >= RAIN_THRESHOLDS["light"]:
            return "light"
        return "none"

    async def close(self) -> None:
        await self._client.aclose()


# WMO Weather interpretation codes
WMO_CODES = {
    0: "Clear sky",
    1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Foggy", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    56: "Light freezing drizzle", 57: "Dense freezing drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    66: "Light freezing rain", 67: "Heavy freezing rain",
    71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow",
    77: "Snow grains",
    80: "Slight rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
    85: "Slight snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail",
}
