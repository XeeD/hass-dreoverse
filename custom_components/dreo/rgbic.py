"""RGBIC mobile API support for the newer DR-HCF002S hardware.

The official Open API remains the owner of fan and main-light commands.
RGBIC effect protocol is based on JeffSteinbok/hass-dreo PR927 (MIT).
"""

import asyncio
from copy import deepcopy
import re
import time


class RGBICError(Exception):
    """A bounded RGBIC operation could not be verified."""


def effect_payload(serial, effect, color=None):
    """Keep the catalog's identifiers and segmented layouts intact."""
    if not all(effect.get(k) is not None for k in ("id", "name", "referencedId")):
        raise RGBICError("RGBIC catalog is incomplete")
    layouts = deepcopy(effect.get("layouts", []))
    if color is not None:
        if effect["name"] != "Constant" or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise RGBICError("RGBIC color requires the Constant effect")
        whole = [layout for layout in layouts if "wholeColorList" in layout]
        if not whole:
            raise RGBICError("RGBIC whole-ring color is unavailable")
        for layout in whole:
            layout["currentSelected"] = "whole"
            layout["wholeColorList"] = [color.upper()]
    item = {"applyToDevice": True, "devicesn": serial,
            "effectType": effect.get("effectType", 0), "id": effect["id"],
            "name": effect["name"],
            "subEffects": [{"effectId": effect["referencedId"], "layouts": layouts}]}
    if effect.get("subRemoteEffect") is not None:
        item["subRemoteEffect"] = deepcopy(effect["subRemoteEffect"])
    return [item]


def mixed_value(mixed, key):
    """Read only actual reported values, never caller intent."""
    item = mixed.get(key)
    return item.get("state") if isinstance(item, dict) else None


def observed_effect(catalog, effect_id):
    """Correlate the independently reported effect ID with its catalog record."""
    matches = [e for section in ("basicEffects", "remoteEffects", "sceneEffects")
               for e in catalog.get(section, []) if str(e.get("id")) == str(effect_id)]
    if len(matches) != 1:
        return None, None
    effect = matches[0]
    remote = effect.get("subRemoteEffect") or {}
    name = effect.get("referenceName") or remote.get("effectName") or effect.get("name")
    layouts = remote.get("layouts") or effect.get("layouts", [])
    colors = [layout.get("wholeColorList") for layout in layouts
              if layout.get("currentSelected") == "whole" and "wholeColorList" in layout]
    color = None
    if name == "Constant" and colors and all(c == colors[0] for c in colors):
        if len(colors[0]) == 1 and re.fullmatch(r"#[0-9a-fA-F]{6}", colors[0][0]):
            color = int(colors[0][0][1:], 16)
    return name, color


class RGBICClient:
    """Use Home Assistant's maintained HTTP session; no background transport."""

    def __init__(self, session, username, password_md5, serial):
        self.session = session
        self.username = username
        # ConfigFlow already persists the API's MD5 credential. Do not hash twice.
        self.password_md5 = password_md5
        self.serial = serial
        self.origin = "https://app-api-us.dreo-tech.com"
        self.region = "us"
        self.token = None
        self.enabled = False
        self.effects = []
        self.lock = asyncio.Lock()

    async def _request(self, method, path, *, params=None, body=None, auth=True):
        headers = {"ua": "dreo/2.8.2", "lang": "en",
                   "content-type": "application/json; charset=UTF-8",
                   "user-agent": "okhttp/4.9.1"}
        if auth:
            headers["authorization"] = f"Bearer {self.token}"
        query = dict(params or {})
        query["timestamp"] = str(int(time.time() * 1000))
        try:
            async with asyncio.timeout(30):
                async with self.session.request(method, self.origin + path, headers=headers,
                                                params=query, json=body, allow_redirects=False) as response:
                    if response.status != 200:
                        raise RGBICError("RGBIC HTTP request rejected")
                    payload = await response.json()
            if not isinstance(payload, dict) or payload.get("code") != 0:
                raise RGBICError("RGBIC request rejected")
            return payload.get("data", {})
        except RGBICError:
            raise
        except Exception:
            # Transport exceptions can contain the private authentication URL.
            raise RGBICError("RGBIC communication failed") from None

    async def initialize(self):
        """Enable only the exact model with independently observed RGBIC capability."""
        body = {"acceptLanguage": "en", "client_id": "7de37c362ee54dcf9c4561812309347a",
                "client_secret": "32dfa0764f25451d99f94e1693498791",
                "email": self.username, "password": self.password_md5,
                "encrypt": "ciphertext", "grant_type": "email-password",
                "himei": "faede31549d649f58864093158787ec9", "scope": "all"}
        login = await self._request("POST", "/api/oauth/login", body=body, auth=False)
        if login.get("region") not in ("NA", "EU") or not login.get("access_token"):
            raise RGBICError("RGBIC login rejected")
        self.region = "eu" if login["region"] == "EU" else "us"
        self.origin = f"https://app-api-{self.region}.dreo-tech.com"
        self.token = login["access_token"].split(":")[0]
        if self.region == "eu":
            login = await self._request("POST", "/api/oauth/login", body=body, auth=False)
            if login.get("region") != "EU" or not login.get("access_token"):
                raise RGBICError("RGBIC region login rejected")
            self.token = login["access_token"].split(":")[0]
        devices = await self._request("GET", "/api/v2/user-device/device/list",
                                      params={"acceptLanguage": "en", "method": "devices",
                                              "pageNo": "1", "pageSize": "100"})
        exact = [d for d in devices.get("list", []) if d.get("sn") == self.serial]
        if len(exact) != 1 or exact[0].get("model") != "DR-HCF002S":
            return False
        state, catalog = await self._read_raw()
        if not isinstance(mixed_value(state, "atmon"), bool) or not mixed_value(state, "rgbeffectid"):
            return False
        self.effects = [e["name"] for e in catalog.get("basicEffects", [])
                        if all(e.get(k) is not None for k in ("id", "name", "referencedId"))]
        self.enabled = "Constant" in self.effects
        return self.enabled

    async def _read_raw(self):
        state = await self._request("GET", "/api/user-device/device/state",
                                    params={"deviceSn": self.serial})
        catalog = await self._request("GET", "/api/device/rgb-effect/all/list",
                                      params={"devicesn": self.serial})
        return state.get("mixed", {}), catalog

    async def read(self):
        if not self.enabled:
            raise RGBICError("RGBIC capability unavailable")
        state, catalog = await self._read_raw()
        effect, color = observed_effect(catalog, mixed_value(state, "rgbeffectid"))
        return {"on": mixed_value(state, "atmon"), "brightness": mixed_value(state, "atmbri"),
                "effect": effect, "color": color}

    async def _power_brightness(self, params):
        # RGB parameters only. No fan, main-light, relay or whole-device gate writes.
        if not params or any(k not in ("atmon", "atmbri") for k in params):
            raise RGBICError("RGBIC command scope invalid")
        message = {"devicesn": self.serial, "method": "control", "params": params,
                   "timestamp": str(int(time.time() * 1000))}
        try:
            async with asyncio.timeout(10):
                async with self.session.ws_connect(
                    f"wss://wsb-{self.region}.dreo-tech.com/websocket",
                    params={"accessToken": self.token, "timestamp": message["timestamp"]}) as socket:
                    await socket.send_json(message)
                    # A server reply is acceptance only. A fresh GET proves reported state.
                    while True:
                        response = await socket.receive_json()
                        if response.get("devicesn") == self.serial and response.get("method") in (
                            "control-reply", "control-report"):
                            return
        except Exception:
            raise RGBICError("RGBIC command acknowledgement unavailable") from None

    async def command(self, *, on=True, brightness=None, effect=None, color=None):
        """Serialize one command and accept only independent native readback."""
        async with self.lock:
            if not self.enabled:
                raise RGBICError("RGBIC capability unavailable")
            if brightness is not None and (type(brightness) is not int or not 1 <= brightness <= 100):
                raise RGBICError("RGBIC brightness invalid")
            if color is not None:
                effect = "Constant"
            before, catalog = await self._read_raw()
            matches = [e for e in catalog.get("basicEffects", []) if e.get("name") == effect]
            if effect is not None and len(matches) != 1:
                raise RGBICError("RGBIC effect unavailable")
            payload = effect_payload(self.serial, matches[0], color) if effect else None
            params = {"atmon": bool(on)}
            if brightness is not None:
                params["atmbri"] = brightness
            await self._power_brightness(params)
            if on and mixed_value(before, "atmon") is not True:
                await asyncio.sleep(0.6)
            if payload is not None:
                await self._request("POST", "/api/device/rgb-effect/edit/batch", body=payload)
            expected_color = int(color[1:], 16) if color else None
            for attempt in range(10):
                observed = await self.read()
                if (observed["on"] is bool(on)
                    and (brightness is None or observed["brightness"] == brightness)
                    and (effect is None or observed["effect"] == effect)
                    and (color is None or observed["color"] == expected_color)):
                    return observed
                if attempt < 9:
                    await asyncio.sleep(1)
            raise RGBICError("RGBIC reported state did not match the command")
