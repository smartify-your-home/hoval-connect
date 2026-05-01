"""Hoval Connect Integration."""
from __future__ import annotations
import logging
import asyncio
from datetime import timedelta
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from .api import HovalAuthError, HovalAPIError, HovalConnectAPI
from .const import CONF_PLANT_ID, DOMAIN, UPDATE_INTERVAL_SECONDS

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.CLIMATE, Platform.SENSOR, Platform.SELECT]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    session = async_get_clientsession(hass)
    api = HovalConnectAPI(
        session=session,
        access_token=entry.data["access_token"],
        refresh_token=entry.data["refresh_token"],
    )
    # Nach Neustart Access Token sofort als abgelaufen markieren
    # → erster Update-Zyklus holt frischen Token via Refresh Token
    api._expires_at = 0.0
    plant_id = entry.data[CONF_PLANT_ID]

    _telemetry_counter = 10  # Trigger immediately on first run

    async def _update():
        nonlocal _telemetry_counter
        try:
            _telemetry_counter += 1
            if _telemetry_counter >= 18:  # 18 x 30s = 9 min
                await api.trigger_high_frequency_mode(plant_id)
                _telemetry_counter = 0
            circuits = await api.get_circuits(plant_id)
            # Fetch live values for each circuit
            live_values = {}
            for c in circuits:
                path = c.get("path", "")
                ctype = c.get("type", "")
                if path and ctype:
                    try:
                        lv = await api.get_live_values(plant_id, path, ctype)
                        if lv:
                            live_values[path] = lv
                            _LOGGER.debug("Live values for %s/%s: %s", path, ctype, lv)
                        else:
                            _LOGGER.debug("No live values for %s/%s", path, ctype)
                    except Exception as lv_err:
                        _LOGGER.warning("Live values error for %s/%s: %s", path, ctype, lv_err)
            return {"circuits": circuits, "live_values": live_values, "plant_id": plant_id}
        except HovalAuthError as err:
            entry.async_start_reauth(hass)
            raise UpdateFailed(f"Auth expired, re-authentication required: {err}") from err
        except (HovalAPIError, Exception) as err:
            raise UpdateFailed(str(err)) from err

    # Save new tokens to config entry whenever they are refreshed
    def _save_tokens(access_token: str, refresh_token: str) -> None:
        new_data = {**entry.data, "access_token": access_token, "refresh_token": refresh_token}
        hass.config_entries.async_update_entry(entry, data=new_data)
        _LOGGER.debug("Tokens refreshed and saved to config entry")

    api.set_token_refresh_callback(_save_tokens)

    # Bootstrap once at startup only
    await api.async_init()
    await api.bootstrap()

    coordinator = DataUpdateCoordinator(
        hass, _LOGGER,
        name=f"hovalconnect_{plant_id}",
        update_method=_update,
        update_interval=timedelta(seconds=UPDATE_INTERVAL_SECONDS),
    )
    # Retry first refresh up to 3 times with delay (Hoval server can be temporarily unreachable)
    for attempt in range(3):
        try:
            await coordinator.async_config_entry_first_refresh()
            break
        except Exception as err:
            if attempt < 2:
                _LOGGER.warning("First refresh attempt %d failed: %s – retrying in 10s", attempt+1, err)
                await asyncio.sleep(10)
            else:
                raise
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator,
        "api": api,
        "plant_id": plant_id,
    }
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
        return True
    return False