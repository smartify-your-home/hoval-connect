"""Hoval Connect API client."""
from __future__ import annotations
import logging, time, asyncio
import aiohttp
from .const import (AUTH_TOKEN_URL, CLIENT_ID, API_MY_PLANTS, API_PLANT_SETTINGS,
                    API_CIRCUITS, API_TEMP_CHANGE, API_SET_PROGRAM, API_LIVE_VALUES,
                    API_TELEMETRY_HF, API_BOOTSTRAP)

_LOGGER = logging.getLogger(__name__)

_APP_VERSION = "3.2.0"

APP_HEADERS = {
    "User-Agent": "HovalConnect/6022 CFNetwork/3860.400.51 Darwin/25.3.0",
    "Accept": "application/json",
    "x-requested-with": "XMLHttpRequest",
    "hovalconnect-frontend-app-version": _APP_VERSION,
}

API_SET_CONSTANT = "https://azure-iot-prod.hoval.com/core/v3/plants/{plant_id}/circuits/{path}/programs"
ITUNES_LOOKUP_URL = "https://itunes.apple.com/lookup?bundleId=com.hoval.connect2"


async def fetch_app_version(session: aiohttp.ClientSession) -> str:
    """Fetch current HovalConnect app version from iTunes API."""
    try:
        async with session.get(
            ITUNES_LOOKUP_URL,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                results = data.get("results", [])
                if results:
                    version = results[0].get("version", "")
                    if version:
                        _LOGGER.info("HovalConnect app version from App Store: %s", version)
                        return version
    except Exception as err:
        _LOGGER.debug("App Store version lookup failed: %s", err)
    _LOGGER.debug("Using fallback app version: %s", _APP_VERSION)
    return _APP_VERSION


class HovalAuthError(Exception):
    pass

class HovalAPIError(Exception):
    pass


class HovalConnectAPI:
    def __init__(self, session: aiohttp.ClientSession, access_token: str, refresh_token: str) -> None:
        self._session = session
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._expires_at: float = time.monotonic() + 1800
        self._plant_token: str | None = None
        self._plant_token_expires: float = 0.0
        self._on_token_refresh = None
        self._proactive_refresh_interval: float = 20 * 60

    def set_token_refresh_callback(self, callback) -> None:
        self._on_token_refresh = callback

    async def async_init(self) -> None:
        """Fetch current app version from App Store (called once on setup)."""
        version = await fetch_app_version(self._session)
        APP_HEADERS["hovalconnect-frontend-app-version"] = version

    async def _ensure_access_token(self) -> None:
        proactive_threshold = self._expires_at - self._proactive_refresh_interval
        if time.monotonic() < proactive_threshold:
            return
        async with self._session.post(
            AUTH_TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": self._refresh_token, "client_id": CLIENT_ID},
            headers={**APP_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
        ) as resp:
            if resp.status in (400, 401):
                raise HovalAuthError("Refresh token expired")
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        self._access_token = data["access_token"]
        self._refresh_token = data.get("refresh_token", self._refresh_token)
        self._expires_at = time.monotonic() + data.get("expires_in", 1800) - 60
        self._plant_token = None
        if self._on_token_refresh:
            self._on_token_refresh(self._access_token, self._refresh_token)

    async def _ensure_plant_token(self, plant_id: str) -> str:
        if self._plant_token and time.monotonic() < self._plant_token_expires:
            return self._plant_token
        await self._ensure_access_token()
        async with self._session.get(
            API_PLANT_SETTINGS.format(plant_id=plant_id),
            headers={**APP_HEADERS, "Authorization": f"Bearer {self._access_token}"},
        ) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        self._plant_token = data["token"]
        self._plant_token_expires = time.monotonic() + 800
        return self._plant_token

    async def _h(self, plant_id: str) -> dict:
        pt = await self._ensure_plant_token(plant_id)
        return {**APP_HEADERS, "Authorization": f"Bearer {self._access_token}", "x-plant-access-token": pt}

    async def bootstrap(self) -> None:
        """Initialize server-side session – required before API calls work."""
        await self._ensure_access_token()
        h = {**APP_HEADERS, "Authorization": f"Bearer {self._access_token}",
             "Content-Type": "application/x-www-form-urlencoded"}
        try:
            async with self._session.post(
                API_BOOTSTRAP, data="", headers=h,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                _LOGGER.debug("Bootstrap: status=%d", resp.status)
        except Exception as err:
            _LOGGER.warning("Bootstrap error: %s", err)

    async def trigger_high_frequency_mode(self, plant_id: str) -> None:
        """Trigger high-frequency telemetry mode – keeps data fresh."""
        h = await self._h(plant_id)
        try:
            async with self._session.post(
                API_TELEMETRY_HF,
                json={"plantExternalId": plant_id},
                headers={**h, "Content-Type": "application/json"},
            ) as resp:
                data = await resp.json(content_type=None)
                _LOGGER.debug("Telemetry HF mode: status=%d end=%s", resp.status, data.get("end"))
        except Exception as err:
            _LOGGER.warning("Telemetry HF mode error: %s", err)

    async def get_plants(self) -> list[dict]:
        await self._ensure_access_token()
        async with self._session.get(API_MY_PLANTS,
            headers={**APP_HEADERS, "Authorization": f"Bearer {self._access_token}"}) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def get_circuits(self, plant_id: str) -> list[dict]:
        async with self._session.get(API_CIRCUITS.format(plant_id=plant_id),
            headers=await self._h(plant_id)) as resp:
            if resp.status == 401:
                _LOGGER.debug("401 on circuits, forcing token refresh")
                self._expires_at = 0.0
                self._plant_token = None
                async with self._session.get(API_CIRCUITS.format(plant_id=plant_id),
                    headers=await self._h(plant_id)) as resp2:
                    resp2.raise_for_status()
                    return await resp2.json(content_type=None)
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def get_live_values(self, plant_id: str, circuit_path: str, circuit_type: str) -> dict:
        h = await self._h(plant_id)
        url = f"{API_LIVE_VALUES.format(plant_id=plant_id)}?circuitPath={circuit_path}&circuitType={circuit_type}"
        async with self._session.get(url, headers=h) as resp:
            if resp.status in (404, 502):
                return {}
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        return {item["key"]: item["value"] for item in data if "key" in item}

    async def set_temporary_change(self, plant_id: str, path: str, value: float, duration: str = "fourHours") -> None:
        """Temporary temperature change – retries on timeout/gateway errors."""
        for attempt in range(3):
            try:
                h = await self._h(plant_id)
                async with self._session.post(
                    API_TEMP_CHANGE.format(plant_id=plant_id, path=path),
                    json={"duration": duration, "value": value},
                    headers={**h, "Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status in (502, 503, 504):
                        _LOGGER.warning("set_temporary_change attempt %d failed: %d", attempt+1, resp.status)
                        await asyncio.sleep(3)
                        continue
                    resp.raise_for_status()
                    return
            except (aiohttp.ServerTimeoutError, aiohttp.ClientError, asyncio.TimeoutError, TimeoutError) as err:
                _LOGGER.warning("set_temporary_change attempt %d error: %s", attempt+1, err)
                if attempt < 2:
                    await asyncio.sleep(3)
        raise HovalAPIError("set_temporary_change failed after 3 attempts")

    async def set_constant_temp(self, plant_id: str, path: str, value: float) -> None:
        """Set permanent temperature – retries on timeout/gateway errors."""
        for attempt in range(3):
            try:
                h = await self._h(plant_id)
                async with self._session.patch(
                    API_SET_CONSTANT.format(plant_id=plant_id, path=path),
                    json={"constant": {"value": value}},
                    headers={**h, "Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status in (502, 503, 504):
                        _LOGGER.warning("set_constant_temp attempt %d failed: %d", attempt+1, resp.status)
                        await asyncio.sleep(3)
                        continue
                    resp.raise_for_status()
                    return
            except (aiohttp.ServerTimeoutError, aiohttp.ClientError, asyncio.TimeoutError, TimeoutError) as err:
                _LOGGER.warning("set_constant_temp attempt %d error: %s", attempt+1, err)
                if attempt < 2:
                    await asyncio.sleep(3)
        raise HovalAPIError("set_constant_temp failed after 3 attempts")

    async def set_program(self, plant_id: str, path: str, program: str) -> None:
        """Switch program: week1, week2, constant."""
        h = await self._h(plant_id)
        async with self._session.post(
            API_SET_PROGRAM.format(plant_id=plant_id, path=path, program=program),
            headers={**h, "Content-Type": "application/x-www-form-urlencoded"},
            data="",
        ) as resp:
            resp.raise_for_status()
