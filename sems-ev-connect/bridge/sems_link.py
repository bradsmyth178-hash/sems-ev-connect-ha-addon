"""GoodWe SEMS cloud link for first-generation HCA chargers (e.g. the GW7K-HCA).

Mirrors the request shapes of the prezervos/goodwe-wallbox-sems-home-assistant
reference integration (custom_components/sems_wallbox/sems_api.py) pinned at
commit 9ce5772195f13cc9c4082a93e09b87213a1e2a6a — the SEMS-Plus web flow, NOT
the legacy semsportal.com v3 API (which the reference abandoned because it
left the wallbox busy with ~30 s timeouts). The full vendored contract lives
in bridge/SEMS-API-REFERENCE.md; never depend on the live GitHub repo.

The link presents the same small interface as ``ModbusLink`` so the OCPP
engine and local control console can use either charger connection. On top of
the reference shapes it adds verify-after-write: SEMS mode changes are known
to silently revert (prezervos issue #13), so every write is re-read, compared
against the requested value, re-asserted a bounded number of times, and
finally raised as a clear error so the platform can alert and stand down.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import time
from typing import Any, Awaitable, Callable

import aiohttp

from .modbus_link import Snapshot
from . import registers as R

log = logging.getLogger("bridge.sems")

# --- Endpoints (reference: sems_api.py @ 9ce5772) --------------------------------
DEFAULT_LOGIN_URL = "https://semsplus.goodwe.com/web/sems/sems-user/api/v1/auth/cross-login"
LOGIN_PATH = "sems-user/api/v1/auth/cross-login"  # appended to api_base overrides
# Regional gateway base. Overridden at runtime from the cross-login response's
# data.api field (that is how the reference lands on the right region).
# PENDING REAL-HARDWARE CONFIRMATION: which gateway an Australian SEMS account
# is routed to — the reference default is the EU gateway; we follow data.api.
DEFAULT_GATEWAY_BASE = "https://eu-gateway.semsportal.com/web/sems"

PATH_SET_MODE = "sems-remote/api/ev-charger/set-mode"
PATH_START_CHARGE = "sems-remote/api/ev-charger/startCharge"
PATH_STOP_CHARGE = "sems-remote/api/ev-charger/stopCharge"
PATH_DETAIL = "sems-remote/api/ev-charger/detail"
PATH_LAST_CHARGE = "sems-plant/api/v1/chargePile/getLastCharge"
PATH_STATIONS_PAGE = "sems-plant/api/portal/stations/page"
PATH_DEVICE_PAGE = "sems-plant/api/web/device/centralized/page"

SUCCESS_CODES = ("0", "00000")
CODE_SESSION_EXPIRED = "C0602"      # renew token once, retry once
CODE_REMOTE_CONTROL_FAIL = "R0305"  # transient set-mode failure, bounded retry

READ_TIMEOUT = 30      # seconds — status reads (reference _RequestTimeout)
COMMAND_TIMEOUT = 90   # seconds — set-mode/start/stop (reference _SetModeTimeout)

# getLastCharge chargeLog.workStu values (reference coordinator/sensor):
WORKSTU_CHARGING = 6   # actively charging — the authoritative charging signal
WORKSTU_FINISHED = 8   # session finished, vehicle still connected

_CHARGING_STATUS_TEXTS = ("EVDetail_Status_Title_Charging", "charging")
_OFFLINE_STATUS_TEXTS = ("EVDetail_Status_Title_Offline", "offline", "unavailable")
_WORKSTATE_UNPLUGGED = (
    "EVDetail_Status_Waiting_Stat00",
    "available_gun_no_insered",
    "available_gun_no_inserted",
)
_WORKSTATE_PLUGGED = (
    "EVDetail_Status_Waiting_Stat01",
    "available_gun_insered",
    "available_gun_inserted",
    "prepare",
)
_WORKSTATE_FINISHED = (
    "EVDetail_Status_Waiting_Stat02",
    "finishing",
    "finish",
    "suspended_evse",
    "suspended_ev",
)


def _float(data: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return default


def _int(data: dict[str, Any], *keys: str, default: int = 0) -> int:
    return int(round(_float(data, *keys, default=float(default))))


def _pick(raw: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = raw.get(key)
        if value is not None:
            return value
    return default


def encode_password(password: str) -> str:
    """SEMS-Plus login password encoding: base64(md5-hexdigest(password))."""
    return base64.b64encode(hashlib.md5(password.encode()).hexdigest().encode()).decode()


class SemsLink:
    """Async client for a first-generation GoodWe HCA through the SEMS-Plus API."""

    def __init__(
        self,
        username: str,
        password: str,
        wallbox_serial: str,
        *,
        charger_kw: int = 7,
        phases: int = 1,
        api_base: str = "",
        login_url: str = "",
        plant_id: str = "",
        trace_cb: Callable[[str], None] | None = None,
    ) -> None:
        self.username = username.strip()
        self.password = password
        self.wallbox_serial = wallbox_serial.strip()
        self.charger_kw = charger_kw
        self.phases = phases
        self._api_base_override = api_base.rstrip("/")
        self._login_url = login_url or (
            f"{self._api_base_override}/{LOGIN_PATH}"
            if self._api_base_override
            else DEFAULT_LOGIN_URL
        )
        self._fallback_base = DEFAULT_GATEWAY_BASE
        self._token: dict[str, Any] | None = None
        self._regional_api_base = ""
        self._plant_id: str = plant_id.strip()
        self._trace_cb = trace_cb
        self._product_model: str = ""
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()
        self._last_data: dict[str, Any] = {}
        self._last_power_limit: float = 0.0
        # Lifetime energy: SEMS only exposes charge-session-scoped energy
        # (chargeEnergy / currentChargeQuantity). We keep a monotonic local
        # accumulator so OCPP meter values can never go backwards.
        self._energy_base_kwh: float = 0.0
        self._last_session_kwh: float = 0.0

        # Verify-after-write knobs (tests shrink these; defaults sized for the
        # real SEMS cloud, where a device can take 60-90 s to act).
        self.verify_reasserts = 2       # re-send the command up to 2 more times
        self.verify_wait = 2.0          # settle time before the first re-read
        self.verify_poll = 3.0          # spacing between verification re-reads
        self.verify_window_mode = 8.0   # per-attempt poll window: mode/power
        self.verify_window_switch = 20.0  # per-attempt poll window: start/stop
        self.verify_confirm_reads = 2   # consecutive matching reads for mode/power
        self.r0305_retries = 3          # reference _SetModeR0305Retries
        self.r0305_delay = 2.0          # reference _SetModeR0305Delay

    def _trace(self, line: str) -> None:
        if self._trace_cb:
            self._trace_cb(line)

    # ------------------------------------------------------------------ session
    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def connect(self) -> bool:
        try:
            await self._ensure_token()
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("SEMS connection failed: %s", exc)
            return False

    async def _client(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": "SEMS-EV-CONNECT/0.4"},
            )
        return self._session

    # ------------------------------------------------------------------ auth
    @staticmethod
    def _signature(ts_ms: str, uid: str, token: str) -> str:
        """x-signature = base64(sha256(ts@uid@token).hexdigest() + '@' + ts)."""
        digest = hashlib.sha256(f"{ts_ms}@{uid}@{token}".encode()).hexdigest()
        return base64.b64encode(f"{digest}@{ts_ms}".encode()).decode()

    def _login_headers(self) -> dict[str, str]:
        empty_token = json.dumps(
            {"uid": "", "timestamp": 0, "token": "",
             "client": "semsPlusWeb", "version": "", "language": "en"}
        )
        ts = str(int(time.time() * 1000))
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "token": empty_token,
            "client": "semsPlusWeb",
            "neutral": "0",
            "currentlang": "en",
            "x-signature": self._signature(ts, "", ""),
        }

    def _signed_headers(self) -> dict[str, str]:
        if self._token is None:
            raise RuntimeError("SEMS session is not ready")
        ts = str(int(time.time() * 1000))
        uid = str(self._token.get("uid") or "")
        tok = str(self._token.get("token") or "")
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "token": json.dumps(self._token),
            "client": "semsPlusWeb",
            "neutral": "0",
            "currentlang": "en",
            "x-signature": self._signature(ts, uid, tok),
        }

    async def _login(self) -> None:
        try:
            if not self.username or not self.password:
                raise ValueError("SEMS account email and password are required")
            session = await self._client()
            body = {
                "account": self.username,
                "pwd": encode_password(self.password),
                "agreement": 1,
                "isLocal": False,
                "isChinese": False,
            }
            async with session.post(
                self._login_url,
                headers=self._login_headers(),
                json=body,
                timeout=aiohttp.ClientTimeout(total=READ_TIMEOUT),
            ) as response:
                if response.status >= 400:
                    raise ConnectionError(f"SEMS sign-in returned HTTP {response.status}")
                payload = await response.json(content_type=None)
            code = payload.get("code")
            if payload.get("hasError") or code not in (0, "0", "00000", None):
                raise PermissionError(payload.get("msg") or "SEMS sign-in was not accepted")
            token = payload.get("data")
            if not isinstance(token, dict) or not token.get("token"):
                raise PermissionError("SEMS sign-in did not return an access token")
            self._token = token
            api = str(token.get("api") or "").rstrip("/")
            if api:
                self._regional_api_base = api
            self._trace("SEMS signed in")
        except Exception:
            self._trace("SEMS sign-in failed")
            raise

    async def _ensure_token(self, renew: bool = False) -> None:
        if self._token is None or renew:
            await self._login()

    # ------------------------------------------------------------------ transport
    def _candidate_urls(self, path: str) -> list[str]:
        path = path.lstrip("/")
        if self._api_base_override:
            return [f"{self._api_base_override}/{path}"]
        urls: list[str] = []
        for base in (self._regional_api_base, self._fallback_base):
            base = base.rstrip("/")
            if base and f"{base}/{path}" not in urls:
                urls.append(f"{base}/{path}")
        return urls

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        timeout: float = READ_TIMEOUT,
        relogin: bool = True,
    ) -> dict[str, Any]:
        await self._ensure_token()
        session = await self._client()
        last_error = f"SEMS request to {path} failed"
        for url in self._candidate_urls(path):
            try:
                async with session.request(
                    method,
                    url,
                    headers=self._signed_headers(),
                    json=body,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as response:
                    if response.status == 404 or response.status >= 500:
                        # This base does not serve the path (404) or is broken
                        # (5xx) — fall through to the next candidate gateway.
                        last_error = f"{url} returned HTTP {response.status}"
                        continue
                    if response.status >= 400:
                        raise ConnectionError(f"SEMS returned HTTP {response.status}")
                    payload = await response.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                last_error = f"{url}: {exc}"
                continue
            if not isinstance(payload, dict):
                last_error = f"{url}: unexpected response shape"
                continue
            if str(payload.get("code") or "") == CODE_SESSION_EXPIRED and relogin:
                # Token expiry code (reference: C0602). Clear the token,
                # re-login once, retry the request once.
                log.info("SEMS session expired (C0602), renewing token")
                self._token = None
                await self._ensure_token(renew=True)
                return await self._request(
                    method, path, body=body, params=params, timeout=timeout, relogin=False
                )
            return payload
        raise ConnectionError(last_error)

    async def _post(self, path: str, body: dict[str, Any], *, timeout: float = READ_TIMEOUT) -> dict[str, Any]:
        return await self._request("POST", path, body=body, timeout=timeout)

    async def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        return await self._request("GET", path, params=params)

    # ------------------------------------------------------------------ discovery
    async def _ensure_plant_id(self, required: bool = False) -> str:
        """Resolve the SEMS plantId (single-plant accounts only, per reference)."""
        if self._plant_id:
            return self._plant_id
        try:
            payload = await self._post(PATH_STATIONS_PAGE, {"current": 1, "size": 50})
            data = payload.get("data") or {}
            if isinstance(data, list):
                records = data
            else:
                records = (
                    data.get("dataList") or data.get("records")
                    or data.get("list") or data.get("data") or []
                )
            ids: list[str] = []
            for rec in records if isinstance(records, list) else []:
                sid = _pick(rec, "stationId", "id", "plantId", "powerStationId")
                if sid:
                    ids.append(str(sid))
            if len(ids) == 1:
                self._plant_id = ids[0]
            elif len(ids) > 1:
                log.warning("SEMS account has %d plants; cannot auto-detect plantId", len(ids))
        except Exception as exc:  # noqa: BLE001
            log.debug("SEMS plantId auto-detect failed: %s", exc)
        if required and not self._plant_id:
            raise RuntimeError(
                "could not determine the SEMS plant id for this account — "
                "charger commands need it (single-plant accounts auto-detect)"
            )
        return self._plant_id

    async def list_chargers(self) -> list[dict[str, str]]:
        """Every EV charger on this account, so nobody has to read a label.

        Finding a serial number on a unit mounted in a garage is the fiddliest
        part of the setup, and mistyping it fails in a way that looks like a
        broken connection. Best effort: if the endpoint is unavailable the
        caller falls back to a typed serial, so this can only ever help.
        """
        found: list[dict[str, str]] = []
        try:
            payload = await self._post(
                PATH_DEVICE_PAGE,
                {"deviceTypeList": ["EV_CHARGER"], "current": 1, "size": 50},
            )
            data = payload.get("data") or {}
            groups = data.get("dataList") if isinstance(data, dict) else data
            for group in groups if isinstance(groups, list) else []:
                if not isinstance(group, dict):
                    continue
                children = group.get("children")
                rows = children if isinstance(children, list) else [group]
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    sn = _pick(row, "sn", "deviceSn", "serialNumber", "serial")
                    if not sn:
                        continue
                    model = _pick(row, "productModel", "deviceModel", "model", "deviceType")
                    name = _pick(row, "deviceName", "name", "alias", "stationName")
                    found.append({
                        "serial": str(sn),
                        "model": str(model or ""),
                        "name": str(name or ""),
                    })
        except Exception as exc:  # noqa: BLE001
            log.debug("charger enumeration unavailable: %s", exc)
        seen: set[str] = set()
        unique: list[dict[str, str]] = []
        for row in found:
            if row["serial"] in seen:
                continue
            seen.add(row["serial"])
            unique.append(row)
        return unique

    async def account_probe(self) -> dict[str, Any]:
        """Sign in and report what this GoodWe account actually contains.

        "Your password is wrong" and "that serial is not on this account" are
        different problems with different fixes, and a failed charger lookup
        told you neither. This needs no charger, so it also works on an account
        that only has an inverter on it.
        """
        out: dict[str, Any] = {"signed_in": False, "plants": [], "plant_count": 0}
        await self._ensure_token(renew=True)   # the sign-in itself is the test
        out["signed_in"] = True
        try:
            payload = await self._post(PATH_STATIONS_PAGE, {"current": 1, "size": 50})
            data = payload.get("data") or {}
            records = data if isinstance(data, list) else (
                data.get("dataList") or data.get("records")
                or data.get("list") or data.get("data") or [])
            names: list[str] = []
            for rec in records if isinstance(records, list) else []:
                if not isinstance(rec, dict):
                    continue
                name = _pick(rec, "stationName", "name", "plantName", "powerStationName")
                names.append(str(name) if name else "(unnamed site)")
            out["plants"] = names[:10]
            out["plant_count"] = len(names)
            out["chargers"] = await self.list_chargers()
        except Exception as exc:  # noqa: BLE001
            log.debug("account probe could not list plants: %s", exc)
        return out

    async def _command_payload(self) -> dict[str, Any]:
        if not self.wallbox_serial:
            raise ValueError("charger serial number is required")
        payload: dict[str, Any] = {
            "sn": self.wallbox_serial,
            "plantId": await self._ensure_plant_id(required=True),
        }
        if self._product_model:
            payload["productModel"] = self._product_model
        return payload

    # ------------------------------------------------------------------ reads
    async def _detail(self) -> dict[str, Any]:
        if not self.wallbox_serial:
            raise ValueError("charger serial number is required")
        payload: dict[str, Any] = {"sn": self.wallbox_serial}
        plant = await self._ensure_plant_id()
        if plant:
            payload["plantId"] = plant
        if self._product_model:
            payload["productModel"] = self._product_model
        response = await self._post(PATH_DETAIL, payload)
        code = str(response.get("code") or "")
        raw = response.get("data")
        if code not in SUCCESS_CODES or not isinstance(raw, dict):
            message = response.get("msg") or "charger was not found in this SEMS account"
            raise LookupError(message)
        # Normalise field-name variants the same way the reference does, so
        # both Gen1-shaped and G2-shaped payloads parse.
        data: dict[str, Any] = {
            "sn": str(raw.get("sn") or self.wallbox_serial),
            "name": _pick(raw, "name", "deviceName", default="EV Charger"),
            "status": _pick(raw, "status", "statusCode", "chargeStatus", default=""),
            "workstate": _pick(raw, "workstate", "workState", "carState", default=""),
            "model": _pick(raw, "model", "deviceModel", "productModel", default=""),
            "fireware": _pick(raw, "fireware", "firmware", "softwareVersion", default=""),
            "chargeEnergy": _pick(raw, "chargeEnergy", "chargedEnergy", "totalEnergy", default="0"),
            "power": _pick(raw, "power", "chargePower", "activePower", default="0"),
            "current": _pick(raw, "current", "chargeCurrent", default="0"),
            "chargeMode": _pick(raw, "chargeMode", "mode", "workMode", default=0),
            "set_charge_power": _pick(raw, "chargePowerSetted", "chargeMaxPower", default=None),
            "rated_max_charge_power": _pick(raw, "ratedMaxiChargePower", "ratedMaxChargePower", default=None),
        }
        # Fault text fields: carried over from the legacy payload shape.
        # PENDING REAL-HARDWARE CONFIRMATION — not observed in the reference's
        # detail mapping; harmless if absent.
        for key in ("faultMsg", "errorMsg", "warningMsg", "fault"):
            if raw.get(key):
                data[key] = raw[key]
        model = str(data.get("model") or "")
        if model:
            self._product_model = model
        return data

    async def _last_charge(self) -> dict[str, Any]:
        """GET getLastCharge — workStu is the authoritative charging signal."""
        plant = await self._ensure_plant_id()
        if not plant:
            return {}
        try:
            response = await self._get(
                PATH_LAST_CHARGE, {"chargeSn": self.wallbox_serial, "pwId": plant}
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("SEMS getLastCharge failed: %s", exc)
            return {}
        if str(response.get("code") or "") not in SUCCESS_CODES:
            return {}
        charge_log = (response.get("data") or {}).get("chargeLog") or {}
        return {
            "last_charge_work_status": charge_log.get("workStu"),
            "last_charge_power": charge_log.get("pevChar"),
            "last_charge_energy": charge_log.get("currentChargeQuantity"),
        }

    async def _read_state(self) -> dict[str, Any]:
        data = await self._detail()
        data.update(await self._last_charge())
        self._last_data = data
        limit = _float(data, "set_charge_power", default=0.0)
        if limit > 0:
            self._last_power_limit = limit
        return data

    async def data(self) -> dict[str, Any]:
        async with self._lock:
            return await self._read_state()

    async def identity(self) -> dict[str, Any]:
        data = await self.data()
        return {
            "serial": str(data.get("sn") or self.wallbox_serial),
            "firmware": str(data.get("fireware") or ""),
            "model": str(data.get("model") or "GW7K-HCA"),
            "kw": self.charger_kw,
            "phases": self.phases,
        }

    # ------------------------------------------------------------------ mapping
    @staticmethod
    def _is_charging(data: dict[str, Any]) -> bool:
        if data.get("last_charge_work_status") == WORKSTU_CHARGING:
            return True
        return str(data.get("status") or "") in _CHARGING_STATUS_TEXTS

    @classmethod
    def _map_status(cls, data: dict[str, Any]) -> tuple[int, int, str]:
        """Return (status_code, car, status_name) without contradictory pairs."""
        status_text = str(data.get("status") or "")
        workstate = str(data.get("workstate") or "")
        work_stu = data.get("last_charge_work_status")
        if status_text in _OFFLINE_STATUS_TEXTS:
            status, car = 7, 0
        elif "fault" in status_text.lower() or data.get("fault"):
            status, car = 5, 0
        elif cls._is_charging(data):
            status, car = 3, 2
        elif work_stu == WORKSTU_FINISHED or workstate in _WORKSTATE_FINISHED:
            status, car = 4, 2
        elif workstate in _WORKSTATE_PLUGGED:
            status, car = 1, 2
        elif workstate in _WORKSTATE_UNPLUGGED:
            status, car = 0, 0
        else:
            # Unknown strings must not collapse into "unplugged but connected".
            name = status_text or workstate or "Unknown"
            return 0, 0, f"Unknown ({name})" if name != "Unknown" else "Unknown"
        return status, car, R.SEMS_STATUS_NAMES.get(status, status_text or "Ready")

    def _lifetime_kwh(self, session_kwh: float) -> float:
        """Monotonic lifetime energy from session-scoped SEMS readings.

        SEMS exposes only per-session energy (chargeEnergy in the detail
        payload, currentChargeQuantity in getLastCharge). When a new session
        starts the reading resets, so we roll the finished session into a base
        counter. The counter restarts at bridge restart — acceptable for OCPP
        (meter deltas within a transaction are what matter), but never
        decreasing across snapshots.
        """
        if session_kwh < self._last_session_kwh - 0.001:
            self._energy_base_kwh += self._last_session_kwh
        self._last_session_kwh = max(session_kwh, 0.0)
        return self._energy_base_kwh + self._last_session_kwh

    async def snapshot(self) -> Snapshot:
        snap = Snapshot()
        try:
            data = await self.data()
            status, car, status_name = self._map_status(data)
            charging = status == 3

            if data.get("last_charge_work_status") is not None:
                power = _float(data, "last_charge_power") if charging else 0.0
            else:
                power = max(0.0, _float(data, "power")) if charging else 0.0

            # Session-scoped energy (see SEMS-API-REFERENCE.md): prefer the
            # getLastCharge session counter, fall back to detail chargeEnergy.
            session = max(0.0, _float(data, "last_charge_energy", "chargeEnergy"))
            mode = _int(data, "chargeMode", default=0)
            max_power = _float(
                data,
                "set_charge_power",
                "rated_max_charge_power",
                default=self._last_power_limit or float(self.charger_kw),
            )
            current = max(0.0, _float(data, "current"))
            voltage = 230.0  # SEMS does not report voltage; assumed nominal

            faults: list[str] = []
            for key in ("faultMsg", "errorMsg", "warningMsg"):
                value = data.get(key)
                if value:
                    faults.append(str(value))

            snap.ok = True
            snap.status = status
            snap.status_name = status_name
            snap.car = car
            snap.power_kw = power
            snap.session_kwh = session
            snap.lifetime_kwh = self._lifetime_kwh(session)
            snap.volt_a = voltage
            snap.curr_a = current or (power * 1000 / voltage if voltage else 0.0)
            snap.max_power_kw = max_power
            snap.mode = mode
            snap.mode_name = R.CHARGE_MODES.get(mode, "Fast")
            snap.comms = 1
            snap.faults = faults
        except Exception as exc:  # noqa: BLE001
            snap.error = str(exc)
            log.warning("SEMS charger update failed: %s", exc)
        return snap

    # ------------------------------------------------------------------ commands
    async def _command(self, path: str, body: dict[str, Any], *, retry_transient: bool = False) -> None:
        attempt = 0
        while True:
            payload = await self._post(path, body, timeout=COMMAND_TIMEOUT)
            code = str(payload.get("code") or "")
            accepted = not payload.get("hasError") and (
                code in SUCCESS_CODES or payload.get("data") is True
            )
            if accepted:
                return
            if retry_transient and code == CODE_REMOTE_CONTROL_FAIL and attempt < self.r0305_retries:
                # Reference: R0305 "remote_control_fail" is transient on
                # set-mode; retry after a short delay.
                attempt += 1
                log.info("SEMS R0305 (transient), retry %d/%d", attempt, self.r0305_retries)
                await asyncio.sleep(self.r0305_delay)
                continue
            raise RuntimeError(
                payload.get("msg") or f"charger command was not accepted (SEMS code {code or 'unknown'})"
            )

    async def _write_and_verify(
        self,
        send: Callable[[], Awaitable[None]],
        verify: Callable[[dict[str, Any]], bool],
        label: str,
        *,
        window: float,
        confirm_reads: int = 1,
    ) -> None:
        """Send a command, then prove the charger holds the requested state.

        SEMS mode changes are known to silently revert (prezervos issue #13),
        so a write only counts once re-reads confirm it — ``confirm_reads``
        consecutive matching reads for mode/power so a quick revert inside the
        window is caught. On mismatch the command is re-asserted (idempotent)
        up to ``verify_reasserts`` more times; a persistent mismatch raises so
        the failure surfaces as a failed ack (alert-and-stand-down), never as
        a silent "it probably worked".
        """
        attempts = 1 + self.verify_reasserts
        for attempt in range(1, attempts + 1):
            await send()
            deadline = time.monotonic() + window
            matched = 0
            while True:
                await asyncio.sleep(self.verify_wait if matched == 0 else self.verify_poll)
                try:
                    data = await self._read_state()
                except Exception as exc:  # noqa: BLE001
                    log.debug("verification re-read failed (%s), still polling", exc)
                    data = None
                if data is not None and verify(data):
                    matched += 1
                    if matched >= confirm_reads:
                        if attempt > 1:
                            log.info("%s held after re-assert %d", label, attempt - 1)
                        return
                else:
                    matched = 0
                if time.monotonic() >= deadline:
                    break
            log.warning("charger state does not show %s yet (attempt %d/%d)", label, attempt, attempts)
        raise RuntimeError(
            f"charger did not hold the requested {label} after {attempts} attempts — "
            "SEMS reverted or ignored the change"
        )

    async def _switch_charging(self, on: bool) -> None:
        label = "start charging" if on else "stop charging"
        path = PATH_START_CHARGE if on else PATH_STOP_CHARGE
        body = await self._command_payload()

        async def send() -> None:
            await self._command(path, body)

        # State transitions after start/stop are slow on SEMS; poll the
        # re-read across the window before declaring a mismatch.
        await self._write_and_verify(
            send,
            lambda data: self._is_charging(data) == on,
            label,
            window=self.verify_window_switch,
            confirm_reads=1,
        )

    async def start_charging(self) -> None:
        async with self._lock:
            await self._switch_charging(True)

    async def stop_charging(self) -> None:
        async with self._lock:
            await self._switch_charging(False)

    def _minimum_kw(self, unit_kw: int | None = None) -> float:
        unit = unit_kw or self.charger_kw
        return 1.4 if unit == 7 else 4.2

    async def _current_limit_kw(self) -> float:
        """Best knowledge of the configured Fast-mode power limit.

        Never silently resets the limit to the full unit rating: prefer the
        freshest charger-reported value, then the last limit we saw or wrote,
        and only then the hardware minimum (matching the reference's fallback).
        """
        kw = _float(self._last_data, "set_charge_power", default=0.0)
        if kw <= 0:
            try:
                kw = _float(await self._read_state(), "set_charge_power", default=0.0)
            except Exception as exc:  # noqa: BLE001
                log.debug("could not read current power limit: %s", exc)
        if kw <= 0:
            kw = self._last_power_limit
        if kw <= 0:
            kw = self._minimum_kw()
        return kw

    async def _set_mode_verified(self, mode: int, kw: float | None) -> None:
        body = await self._command_payload()
        body["mode"] = int(mode)
        if kw is not None:
            # Reference: send BOTH field names — Gen1 reads chargePowerSetted,
            # G2 reads chargeMaxPower — always as floats.
            body["chargePowerSetted"] = float(kw)
            body["chargeMaxPower"] = float(kw)

        async def send() -> None:
            await self._command(PATH_SET_MODE, body, retry_transient=True)

        def verify(data: dict[str, Any]) -> bool:
            if _int(data, "chargeMode", default=-1) != int(mode):
                return False
            if kw is None:
                return True
            reported = _float(data, "set_charge_power", default=-1.0)
            return abs(reported - float(kw)) <= 0.051

        label = f"charge mode {R.CHARGE_MODES.get(int(mode), mode)}"
        if kw is not None:
            label += f" at {kw:g} kW"
        await self._write_and_verify(
            send, verify, label,
            window=self.verify_window_mode,
            confirm_reads=self.verify_confirm_reads,
        )
        if kw is not None:
            self._last_power_limit = float(kw)

    async def set_mode(self, mode: int) -> None:
        async with self._lock:
            kw: float | None = None
            if int(mode) == 0:
                # Fast mode requires the power fields or SEMS silently ignores
                # the command; PV modes must NOT send power (it forces Fast).
                kw = await self._current_limit_kw()
            await self._set_mode_verified(int(mode), kw)

    async def set_max_power_kw(self, kw: float, unit_kw: int = 7) -> int:
        minimum_kw = self._minimum_kw(unit_kw)
        if kw < minimum_kw:
            raise ValueError(f"the charger cannot charge below {minimum_kw:g} kW")
        reg = R.kw_to_reg(kw, unit_kw)
        actual_kw = reg / 10.0
        async with self._lock:
            await self._set_mode_verified(0, actual_kw)
        return reg
