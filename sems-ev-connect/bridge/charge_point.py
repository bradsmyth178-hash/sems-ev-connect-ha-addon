"""OCPP 1.6J charge point that fronts a GoodWe HCA charger over either link:
local Modbus for the HCA G2, or the SEMS cloud for the first-generation HCA.

Register mapping summary — G2/Modbus path only (the SEMS link maps the same
logical operations onto SEMS-Plus cloud endpoints; see SEMS-API-REFERENCE.md):
  RemoteStartTransaction  -> write 10060 = 2  (optionally 10032 = 0 Fast)
  RemoteStopTransaction   -> write 10060 = 1
  SetChargingProfile      -> write 10029 = limit (tenths of kW); limit below minimum pauses charging
  ClearChargingProfile    -> write 10029 = unit maximum
  StatusNotification      <- 10017 status code (on change)
  StartTransaction        <- status transitions into 3 (Charging)
  StopTransaction         <- status transitions out of 3
  MeterValues             <- 10015 power, 10065 lifetime energy, 10009/10012 V/A (periodic while charging)
"""
from __future__ import annotations

import asyncio
from collections import deque
import logging
import time
from datetime import datetime, timezone

from ocpp.routing import on
from ocpp.v16 import ChargePoint as OcppChargePoint
from ocpp.v16 import call, call_result
from ocpp.v16.enums import (
    Action,
    AvailabilityStatus,
    ChargePointErrorCode,
    ChargePointStatus,
    ChargingProfileStatus,
    ClearChargingProfileStatus,
    ConfigurationStatus,
    DataTransferStatus,
    Reason,
    RegistrationStatus,
    RemoteStartStopStatus,
    ResetStatus,
    TriggerMessageStatus,
    UnlockStatus,
)

from . import registers as R
from .config import Config
from .modbus_link import ModbusLink, Snapshot
from .sems_link import SemsLink

log = logging.getLogger("bridge.ocpp")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class BridgeChargePoint(OcppChargePoint):
    def __init__(self, cp_id: str, ws, link: ModbusLink | SemsLink, cfg: Config, state: "BridgeState",
                 gate=None):
        super().__init__(cp_id, ws)
        self.link = link
        self.cfg = cfg
        self.state = state
        self._gate = gate
        self.transaction_id: int | None = None
        self._last_status: str | None = None
        self._last_meter = 0.0
        self._pending_remote_start = False
        self._transaction_id_tag = cfg.ocpp_id_tag
        self.configuration = {
            "HeartbeatInterval": str(cfg.heartbeat_seconds),
            "MeterValueSampleInterval": str(cfg.meter_seconds),
            "MeterValuesSampledData": "Energy.Active.Import.Register,Power.Active.Import,Current.Import,Voltage",
            "NumberOfConnectors": "1",
            "ChargeProfileMaxStackLevel": "1",
            "ChargingScheduleAllowedChargingRateUnit": "Current,Power",
            "ChargingScheduleMaxPeriods": "1",
            "MaxChargingProfilesInstalled": "1",
            "SupportedFeatureProfiles": "Core,SmartCharging,RemoteTrigger",
            "AuthorizeRemoteTxRequests": "false",
            "LocalAuthorizeOffline": "true",
            "ConnectorPhaseRotation": "NotApplicable",
        }

    # ------------------------------------------------------------------ outbound
    async def boot(self) -> int:
        ident = self.state.identity or {}
        req = call.BootNotification(
            charge_point_model=ident.get("model") or f"GoodWe-HCA-{ident.get('kw', self.cfg.charger_kw)}kW",
            charge_point_vendor="SEMS EV CONNECT",
            charge_point_serial_number=ident.get("serial") or None,
            firmware_version=ident.get("firmware") or None,
        )
        resp = await self.call(req)
        if resp.status != RegistrationStatus.accepted:
            log.warning("BootNotification not accepted: %s", resp.status)
        self.state.ocpp_connected = True
        self.state.trace("OCPP connected and boot accepted")
        return int(resp.interval or self.cfg.heartbeat_seconds)

    async def heartbeat_loop(self, interval: int) -> None:
        while True:
            # Honour the central system's accepted BootNotification interval.
            # A one-second floor protects against a broken zero/negative value
            # without silently replacing a valid short HA test interval.
            await asyncio.sleep(max(1, int(interval)))
            try:
                await self.call(call.Heartbeat())
            except Exception as e:  # noqa: BLE001
                log.warning("heartbeat failed: %s", e)
                return

    async def send_status(self, snap: Snapshot, force: bool = False) -> None:
        status = R.OCPP_STATUS.get(snap.status, "Unavailable")
        if not snap.ok:
            status = "Unavailable"
        if not force and status == self._last_status:
            return
        self._last_status = status
        err = ChargePointErrorCode.no_error
        info = snap.status_name
        if snap.faults:
            err = ChargePointErrorCode.other_error
            info = "; ".join(snap.faults)[:50]
        await self.call(call.StatusNotification(
            connector_id=1, error_code=err, status=ChargePointStatus(status),
            timestamp=now_iso(), info=info,
        ))

    async def send_meter(self, snap: Snapshot) -> None:
        sampled = [
            {"value": str(snap.lifetime_wh), "measurand": "Energy.Active.Import.Register", "unit": "Wh"},
            {"value": str(snap.power_w), "measurand": "Power.Active.Import", "unit": "W"},
            {"value": f"{snap.curr_a:.1f}", "measurand": "Current.Import", "unit": "A", "phase": "L1"},
            {"value": f"{snap.volt_a:.1f}", "measurand": "Voltage", "unit": "V", "phase": "L1-N"},
        ]
        await self.call(call.MeterValues(
            connector_id=1,
            meter_value=[{"timestamp": now_iso(), "sampled_value": sampled}],
            transaction_id=self.transaction_id,
        ))

    async def start_transaction(self, snap: Snapshot) -> None:
        resp = await self.call(call.StartTransaction(
            connector_id=1, id_tag=self._transaction_id_tag,
            meter_start=snap.lifetime_wh, timestamp=now_iso(),
        ))
        self.transaction_id = resp.transaction_id
        log.info("transaction %s started", self.transaction_id)

    async def stop_transaction(self, snap: Snapshot, reason: Reason = Reason.local) -> None:
        if self.transaction_id is None:
            return
        await self.call(call.StopTransaction(
            meter_stop=snap.lifetime_wh, timestamp=now_iso(),
            transaction_id=self.transaction_id, reason=reason,
        ))
        log.info("transaction %s stopped", self.transaction_id)
        self.transaction_id = None
        self._transaction_id_tag = self.cfg.ocpp_id_tag

    # ------------------------------------------------------------------ main loop
    async def poll_loop(self) -> None:
        """Poll the charger, translate changes into OCPP events."""
        prev: Snapshot | None = None
        meter_due = 0.0
        loop = asyncio.get_event_loop()
        while True:
            snap = await self.link.snapshot()
            self.state.snapshot = snap
            if not self.state.command_in_progress:
                if snap.ok:
                    self.state.decision = "Watching the charger — no command in progress"
                else:
                    self.state.decision = "Charger not reachable — will keep retrying"
            try:
                await self.send_status(snap, force=prev is None)
                if prev is not None and snap.ok:
                    if snap.charging and not prev.charging:
                        await self.start_transaction(snap)
                        meter_due = 0.0
                    elif prev.charging and not snap.charging:
                        reason = Reason.remote if self.state.last_remote_stop else Reason.ev_disconnected \
                            if snap.car == 0 else Reason.local
                        await self.stop_transaction(snap, reason)
                        self.state.last_remote_stop = False
                if snap.ok and snap.charging and loop.time() >= meter_due:
                    await self.send_meter(snap)
                    meter_due = loop.time() + self.cfg.meter_seconds
            except Exception as e:  # noqa: BLE001
                log.warning("poll cycle error: %s", e)
                return  # websocket gone; caller reconnects
            prev = snap if snap.ok else prev
            minimum = 30 if self.cfg.charger_connection == "sems" else 1
            await asyncio.sleep(max(minimum, self.cfg.poll_seconds))

    # ------------------------------------------------------------------ inbound
    async def _guard(self) -> None:
        """The same write gate the console and cloud link use.

        Without it an OCPP central system could tell a charger the bridge has
        not seen for hours to start charging, and be told it worked.
        """
        if self._gate is not None:
            await self._gate()

    @on(Action.remote_start_transaction)
    async def on_remote_start(self, id_tag: str, connector_id: int | None = None, charging_profile: dict | None = None):
        self.state.command_in_progress = True
        self.state.decision = "Command being confirmed on the charger"
        self.state.trace("OCPP Remote Start command sent")
        try:
            await self._guard()
            self._transaction_id_tag = id_tag or self.cfg.ocpp_id_tag
            if self.cfg.remote_start_sets_fast_mode:
                await self.link.set_mode(0)
            if charging_profile:
                await self._apply_profile(charging_profile, allow_start=False)
            await self.link.start_charging()
            self.state.last_action = f"RemoteStart ({id_tag})"
            self.state.trace("OCPP Remote Start command verified")
            self.state.decision = "Watching the charger — no command in progress"
            return call_result.RemoteStartTransaction(status=RemoteStartStopStatus.accepted)
        except Exception as e:  # noqa: BLE001
            log.error("remote start failed: %s", e)
            self.state.trace("OCPP Remote Start command failed")
            self.state.decision = "Last command was not accepted"
            return call_result.RemoteStartTransaction(status=RemoteStartStopStatus.rejected)
        finally:
            self.state.command_in_progress = False

    @on(Action.remote_stop_transaction)
    async def on_remote_stop(self, transaction_id: int):
        self.state.command_in_progress = True
        self.state.decision = "Command being confirmed on the charger"
        self.state.trace("OCPP Remote Stop command sent")
        try:
            await self._guard()
            await self.link.stop_charging()
            self.state.last_remote_stop = True
            self.state.last_action = "RemoteStop"
            self.state.trace("OCPP Remote Stop command verified")
            self.state.decision = "Watching the charger — no command in progress"
            return call_result.RemoteStopTransaction(status=RemoteStartStopStatus.accepted)
        except Exception as e:  # noqa: BLE001
            log.error("remote stop failed: %s", e)
            self.state.trace("OCPP Remote Stop command failed")
            self.state.decision = "Last command was not accepted"
            return call_result.RemoteStopTransaction(status=RemoteStartStopStatus.rejected)
        finally:
            self.state.command_in_progress = False

    async def _apply_profile(self, profile: dict, allow_start: bool = False) -> None:
        sched = profile.get("charging_schedule") or profile.get("chargingSchedule") or {}
        periods = sched.get("charging_schedule_period") or sched.get("chargingSchedulePeriod") or []
        unit = (sched.get("charging_rate_unit") or sched.get("chargingRateUnit") or "A")
        if not periods:
            return
        await self._guard()
        limit = float(periods[0].get("limit", 0))
        kw = limit / 1000.0 if unit == "W" else R.amps_to_kw(limit, self.cfg.phases)
        unit_kw = self.cfg.charger_kw
        min_kw = 1.4 if unit_kw == 7 else 4.2
        if kw < min_kw:
            # Below the hardware minimum: treat as a pause.
            self.state.command_in_progress = True
            self.state.decision = "Command being confirmed on the charger"
            self.state.trace("OCPP profile below the charger floor; pause command sent")
            await self.link.stop_charging()
            self.state.last_action = f"Profile limit {kw:.1f} kW < min, paused"
            self.state.trace("OCPP profile pause verified")
            self.state.command_in_progress = False
            self.state.decision = "Watching the charger — no command in progress"
            return
        self.state.command_in_progress = True
        self.state.decision = "Command being confirmed on the charger"
        self.state.trace("OCPP charging-profile limit sent")
        reg = await self.link.set_max_power_kw(kw, unit_kw)
        # An Accepted OCPP response must mean the charger is actually holding
        # the requested limit, not merely that a write call returned. SEMSLink
        # already verifies internally; this explicit cross-link read-back also
        # closes that acknowledgement gap for the G2 Modbus path.
        confirmed = await self.link.snapshot()
        self.state.snapshot = confirmed
        expected_kw = reg / 10.0
        if not confirmed.ok or abs(confirmed.max_power_kw - expected_kw) > 0.051:
            raise RuntimeError("charger did not report the requested charging-profile limit")
        self.state.last_action = f"Profile limit -> {reg/10:.1f} kW"
        snap = confirmed
        # Only resume charging when the profile arrived as part of a RemoteStart.
        # A bare SetChargingProfile is "charge no faster than this", not "start
        # now" - starting a car charging because its limit was adjusted is not
        # the operator's instruction.
        if allow_start and snap and snap.ok and snap.car == 2 and not snap.charging and snap.status in (1, 4, 10):
            await self.link.start_charging()
        self.state.trace("OCPP charging-profile limit verified")
        self.state.command_in_progress = False
        self.state.decision = "Watching the charger — no command in progress"

    @on(Action.set_charging_profile)
    async def on_set_profile(self, connector_id: int, cs_charging_profiles: dict):
        try:
            await self._apply_profile(cs_charging_profiles)
            return call_result.SetChargingProfile(status=ChargingProfileStatus.accepted)
        except Exception as e:  # noqa: BLE001
            log.error("set profile failed: %s", e)
            self.state.trace("OCPP charging-profile command failed")
            self.state.decision = "Last command was not accepted"
            return call_result.SetChargingProfile(status=ChargingProfileStatus.rejected)
        finally:
            self.state.command_in_progress = False

    @on(Action.clear_charging_profile)
    async def on_clear_profile(self, **kwargs):
        try:
            await self._guard()
            await self.link.set_max_power_kw(self.cfg.charger_kw, self.cfg.charger_kw)
            self.state.last_action = "Profile cleared"
            return call_result.ClearChargingProfile(status=ClearChargingProfileStatus.accepted)
        except Exception:  # noqa: BLE001
            return call_result.ClearChargingProfile(status=ClearChargingProfileStatus.unknown)

    @on(Action.get_configuration)
    async def on_get_configuration(self, key: list[str] | None = None):
        keys = key or list(self.configuration)
        known = [{"key": k, "readonly": False, "value": self.configuration[k]} for k in keys if k in self.configuration]
        unknown = [k for k in keys if k not in self.configuration]
        return call_result.GetConfiguration(configuration_key=known, unknown_key=unknown or None)

    @on(Action.change_configuration)
    async def on_change_configuration(self, key: str, value: str):
        if key not in self.configuration:
            return call_result.ChangeConfiguration(status=ConfigurationStatus.not_supported)
        self.configuration[key] = value
        if key == "MeterValueSampleInterval" and value.isdigit():
            self.cfg.meter_seconds = int(value)
        return call_result.ChangeConfiguration(status=ConfigurationStatus.accepted)

    @on(Action.trigger_message)
    async def on_trigger(self, requested_message: str, connector_id: int | None = None):
        snap = self.state.snapshot or Snapshot()
        try:
            if requested_message == "StatusNotification":
                asyncio.create_task(self.send_status(snap, force=True))
            elif requested_message == "MeterValues":
                asyncio.create_task(self.send_meter(snap))
            elif requested_message == "Heartbeat":
                asyncio.create_task(self.call(call.Heartbeat()))
            elif requested_message == "BootNotification":
                asyncio.create_task(self.boot())
            else:
                return call_result.TriggerMessage(status=TriggerMessageStatus.not_implemented)
            return call_result.TriggerMessage(status=TriggerMessageStatus.accepted)
        except Exception:  # noqa: BLE001
            return call_result.TriggerMessage(status=TriggerMessageStatus.rejected)

    @on(Action.reset)
    async def on_reset(self, type: str):  # noqa: A002
        # We can't reboot the charger; a "reset" just re-boots the bridge session.
        self.state.reset_requested = True
        return call_result.Reset(status=ResetStatus.accepted)

    @on(Action.change_availability)
    async def on_change_availability(self, connector_id: int, type: str):  # noqa: A002
        return call_result.ChangeAvailability(status=AvailabilityStatus.accepted)

    @on(Action.unlock_connector)
    async def on_unlock(self, connector_id: int):
        return call_result.UnlockConnector(status=UnlockStatus.not_supported)

    @on(Action.data_transfer)
    async def on_data_transfer(self, vendor_id: str, message_id: str | None = None, data: str | None = None):
        return call_result.DataTransfer(status=DataTransferStatus.unknown_vendor_id)


class BridgeState:
    """Shared state for the web UI, the cloud link and the write gates."""
    def __init__(self):
        self.identity: dict | None = None
        self._snapshot: Snapshot | None = None
        self.snapshot_at: float = 0.0  # wall-clock stamp of the last stored snapshot
        self.ocpp_connected = False
        self.modbus_connected = False
        self.charger_connected = False
        self.cloud_ok = False
        self.cloud_error = ""
        self.last_action = ""
        self.last_error = ""
        self.decision = "Watching the charger — no command in progress"
        self.command_in_progress = False
        self._trace = deque(maxlen=50)
        self.last_remote_stop = False
        self.reset_requested = False

    def trace(self, line: str) -> None:
        """Store one deliberately redacted diagnostic event.

        Callers pass fixed, human-readable event descriptions rather than raw
        request bodies or exception text, keeping passwords, access tokens and
        cloud device keys out of the diagnostic surface by construction.
        """
        safe = " ".join(str(line).split())[:300]
        if not safe:
            return
        self._trace.append({
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "line": safe,
        })

    def trace_entries(self) -> list[dict[str, str]]:
        return list(self._trace)

    @property
    def snapshot(self) -> Snapshot | None:
        return self._snapshot

    @snapshot.setter
    def snapshot(self, snap: Snapshot | None) -> None:
        # Every store stamps the time so freshness gates can refuse writes
        # against a charger the bridge has not seen recently.
        self._snapshot = snap
        self.snapshot_at = time.time() if snap is not None else 0.0

    def snapshot_fresh(self, max_age_seconds: float = 120.0) -> bool:
        """True when the last snapshot succeeded and is recent enough to trust."""
        snap = self._snapshot
        if not (snap and snap.ok):
            return False
        return bool(self.snapshot_at) and (time.time() - self.snapshot_at) <= max_age_seconds
