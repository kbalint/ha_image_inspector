"""Admin-only Home Assistant image inspection panel."""

import asyncio
from functools import partial
import logging
from pathlib import Path

try:
    import probatio as vol
except ImportError:
    import voluptuous as vol

from homeassistant.components import frontend, panel_custom, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.helpers.storage import Store

from .analyzer import LOCAL_ARCHIVE, REFERENCE, analyze, analyze_archive, host_architecture

DOMAIN = "ha_image_inspector"
TITLE = "HA Image Inspector"
PANEL = "ha-image-inspector"
STATIC = f"/{DOMAIN}_static"
_LOGGER = logging.getLogger(__name__)


class Inspection:
    def __init__(self, hass):
        self.hass = hass
        self.store = Store(hass, 1, DOMAIN)
        self.report = None
        self.task = None
        self.progress = None
        self.error = None

    def status(self, known):
        report = self.report if self.report and self.report["generated"] != known else None
        return {"report": report, "progress": self.progress, "error": self.error,
                "default_reference": HA_VERSION, "default_architecture": host_architecture()}

    async def run(self, source, reference, architecture):
        def report_progress(index, total, downloaded=None, download_total=None):
            self.hass.loop.call_soon_threadsafe(update_progress, index, total, downloaded, download_total)

        def update_progress(index, total, downloaded, download_total):
            self.progress = {"done": index, "total": total, "source": source,
                             "downloaded": downloaded, "download_total": download_total}

        try:
            if source == "local":
                work = partial(analyze_archive, self.hass.config.path(LOCAL_ARCHIVE), report_progress)
            else:
                work = partial(analyze, reference, architecture, report_progress)
            result = await self.hass.async_add_executor_job(work)
            await self.store.async_save(result)
            self.report = result
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Registry redirects can carry signed URLs. Return the type and HTTP code only.
            code = getattr(error, "code", None)
            self.error = f"HTTP {code}" if code else type(error).__name__
            if source == "local" and isinstance(error, FileNotFoundError):
                self.error = "archive_missing"
            elif isinstance(error, ValueError):
                self.error = str(error)
            _LOGGER.warning("Image inspection failed (%s): %s", source, self.error)
        finally:
            self.progress = None


@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/inspect",
    vol.Optional("reference"): str,
    vol.Optional("architecture"): str,
    vol.Optional("source"): vol.In(["local", "registry"]),
    vol.Optional("known"): str,
})
@websocket_api.async_response
async def inspect(hass, connection, msg):
    if not connection.user or not connection.user.is_admin:
        connection.send_error(msg["id"], "unauthorized", "An administrator is required")
        return
    shared = hass.data.get(DOMAIN, {})
    if not shared.get("loaded"):
        connection.send_error(msg["id"], "not_loaded", "The integration is unloaded")
        return
    state = shared["inspection"]
    source = msg.get("source", "registry")
    if source == "local" or "reference" in msg or "source" in msg:
        reference = msg.get("reference", HA_VERSION)
        architecture = msg.get("architecture", host_architecture())
        if source == "registry" and (not REFERENCE.fullmatch(reference) or architecture not in {"amd64", "arm64"}):
            connection.send_error(msg["id"], "invalid_format", "Use an official image tag or sha256 digest")
            return
        if state.task is None or state.task.done():
            state.progress = {"done": 0, "total": 0, "source": source}
            state.error = None
            state.task = hass.async_create_background_task(state.run(source, reference, architecture), f"{DOMAIN} scan")
    connection.send_result(msg["id"], state.status(msg.get("known")))


async def async_setup_entry(hass, entry):
    shared = hass.data.setdefault(DOMAIN, {})
    if "registered" not in shared:
        await hass.http.async_register_static_paths([
            StaticPathConfig(STATIC, str(Path(__file__).parent / "frontend"), False),
        ])
        websocket_api.async_register_command(hass, inspect)
        shared["registered"] = True

    state = shared.get("inspection") or Inspection(hass)
    if "inspection" not in shared:
        state.report = await state.store.async_load()
        shared["inspection"] = state
    await panel_custom.async_register_panel(
        hass, frontend_url_path=PANEL, webcomponent_name=PANEL,
        sidebar_title=TITLE, sidebar_icon="mdi:layers-search", module_url=f"{STATIC}/panel.js",
        require_admin=True, config_panel_domain=DOMAIN,
    )
    shared["loaded"] = True
    return True


async def async_unload_entry(hass, entry):
    hass.data[DOMAIN]["loaded"] = False
    frontend.async_remove_panel(hass, PANEL)
    return True
