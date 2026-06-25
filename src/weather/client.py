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

    async def check_rain_fade(
        self,
        lat: float,
        lon: float,
        band_ghz: Optional[int] = None,
    ) -> dict[str, Any]:
        """Check if rain fade conditions exist at a location."""
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

        if band_ghz and rain_rate > 0:
            from src.pcn.calculator import estimate_rain_attenuation
            result["estimated_fade_db_per_km"] = estimate_rain_attenuation(
                band_ghz, rain_rate
            )
            result["rain_fade_likely"] = classification in ("moderate", "heavy", "extreme")
        else:
            result["rain_fade_likely"] = False

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
