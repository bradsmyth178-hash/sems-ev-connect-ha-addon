"""SEMS EV CONNECT cloud link — outbound-only sync with the Wattlane platform.

Why this exists: a hosted web page can never open a raw Modbus TCP socket into a
private LAN, and exposing the charger's unauthenticated port 502 to the internet
is not an option. So the bridge itself phones home instead: every few seconds it
POSTs the charger snapshot to a Supabase RPC and executes whatever commands the customer
queued from his SEMS EV CONNECT page. Outbound HTTPS only — no port forwarding,
no VPN, no inbound listener beyond the LAN-private wizard.

Security shape, mirroring the platform side:
- The bridge holds a per-device secret (from the pairing code). The platform
  stores only its SHA-256; the sync/ack RPCs re-hash and compare server-side.
- Commands expire server-side after 90 seconds and the bridge refuses the same
  age (plus commands stamped in the future — clock-skew guard), so a stale
  "start charging" can never fire minutes later.
- Writes are gated: if the bridge cannot currently see the charger (failed or
  stale snapshot) commands are acked as failed instead of fired blind, charger
  writes are paced at least MIN_WRITE_SPACING_SECONDS apart, and when several
  commands of the same action are queued only the newest runs — the rest are
  acked as superseded.
- The local control PIN is unrelated to and never leaves this machine; cloud
  commands are authorised by the device secret + the customer's link token instead.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import aiohttp

from . import __version__
from . import config as C

log = logging.getLogger("bridge.cloud")

# Matches the server-side command expiry; anything older is dead on arrival.
MAX_CMD_AGE_SECONDS = 90
# A command stamped more than this far in the future means broken clocks —
# its real age is unknowable, so refuse it.
MAX_CMD_FUTURE_SKEW_SECONDS = 30
# A SEMS login + command + verify-after-write chain can take well over 15 s;
# a late cancel after the HTTP write has left the bridge risks duplicate
# writes, so the execution timeout must outlast the whole chain.
CMD_EXEC_TIMEOUT_SECONDS = 60   # floor; the real budget comes from the link
# Stand-down gate: refuse writes when the last charger snapshot failed or is
# older than this.
SNAPSHOT_FRESH_SECONDS = 120
# Pacing between executed charger writes.
MIN_WRITE_SPACING_SECONDS = 5.0

ALLOWED_ACTIONS = ("start", "stop", "mode", "max_power")


def _cmd_age_seconds(created_at: str) -> float:
    try:
        stamp = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - stamp).total_seconds()
    except (TypeError, ValueError):
        return 0.0  # unparseable timestamp: trust the server-side expiry


class CloudLink:
    """Runs beside the Modbus/OCPP loops. Fails quiet, retries forever."""

    def __init__(self, cfg: C.Config, bridge) -> None:
        self.cfg = cfg
        self.bridge = bridge
        self.state = bridge.state
        self._session: aiohttp.ClientSession | None = None
        self.min_write_spacing = MIN_WRITE_SPACING_SECONDS
        self._last_write_at = 0.0  # monotonic stamp of the last executed write

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.cloud_url and self.cfg.cloud_anon_key and self.cfg.cloud_device_key)

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    def _headers(self) -> dict:
        return {
            "apikey": self.cfg.cloud_anon_key,
            "Authorization": f"Bearer {self.cfg.cloud_anon_key}",
            "Content-Type": "application/json",
        }

    async def _rpc(self, fn: str, body: dict):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        url = self.cfg.cloud_url.rstrip("/") + "/rest/v1/rpc/" + fn
        async with self._session.post(url, json=body, headers=self._headers()) as resp:
            if resp.status >= 400:
                raise IOError(f"{fn} -> {resp.status}")
            if resp.content_length == 0:
                return None
            return await resp.json(content_type=None)

    def _state_payload(self) -> dict:
        snap = self.state.snapshot
        payload = {
            "ok": bool(snap and snap.ok),
            "operating_mode": self.cfg.operating_mode,
            "charger_connection": self.cfg.charger_connection,
            "charger_connected": self.state.charger_connected,
            "modbus_connected": self.state.modbus_connected,
            "ocpp_connected": self.state.ocpp_connected,
            "last_action": self.state.last_action,
            "decision": self.state.decision,
            "identity": self.state.identity,
            "charger_kw": self.cfg.charger_kw,
            "first_live_test": {
                "passed": bool(self.cfg.first_live_test_passed),
                "at": self.cfg.first_live_test_at or None,
            },
        }
        if snap:
            payload.update({
                "status": snap.status, "status_name": snap.status_name,
                "car": snap.car, "charging": snap.charging,
                "power_kw": snap.power_kw, "session_kwh": snap.session_kwh,
                "lifetime_kwh": snap.lifetime_kwh, "max_power_kw": snap.max_power_kw,
                "mode": snap.mode, "mode_name": snap.mode_name,
                "volt_a": snap.volt_a, "curr_a": snap.curr_a,
                "faults": snap.faults, "error": snap.error,
            })
        return payload

    def _charger_visible(self) -> bool:
        """Stand-down gate: only write when the last snapshot is good and fresh."""
        snap = self.state.snapshot
        if not (snap and snap.ok):
            return False
        taken_at = getattr(self.state, "snapshot_at", 0.0)
        return bool(taken_at) and (time.time() - taken_at) <= SNAPSHOT_FRESH_SECONDS

    async def _pace(self) -> None:
        """Keep at least ``min_write_spacing`` seconds between charger writes."""
        elapsed = time.monotonic() - self._last_write_at
        wait = self.min_write_spacing - elapsed
        if self._last_write_at and wait > 0:
            await asyncio.sleep(wait)

    async def _execute(self, cmd: dict) -> None:
        cmd_id = cmd.get("id")
        action = str(cmd.get("action", ""))
        value = cmd.get("value")
        if action not in ALLOWED_ACTIONS:
            await self._ack(cmd_id, False, f"unknown action {action!r}")
            return
        age = _cmd_age_seconds(cmd.get("created_at", ""))
        if age > MAX_CMD_AGE_SECONDS:
            await self._ack(cmd_id, False, "command too old, ignored")
            return
        if age < -MAX_CMD_FUTURE_SKEW_SECONDS:
            await self._ack(cmd_id, False,
                            "command timestamped in the future — check platform/bridge clocks; ignored")
            return
        if not self._charger_visible():
            self.state.decision = "Charger not reachable — will keep retrying"
            self.state.trace("Cloud command refused because the charger was not reachable")
            await self._ack(cmd_id, False,
                            "charger not currently reachable from the bridge — command not attempted")
            return
        await self._pace()
        self._last_write_at = time.monotonic()
        self.state.decision = "Command being confirmed on the charger"
        self.state.trace(f"Cloud command received: {action}")
        try:
            # A verified write re-asserts itself, so its worst case is a
            # multiple of the poll window. A fixed 60s cancelled writes that
            # were still in flight and reported them to the customer as
            # failures, which is exactly backwards.
            budget = CMD_EXEC_TIMEOUT_SECONDS
            try:
                budget = max(budget, float(self.bridge.command_budget()))
            except Exception:  # noqa: BLE001
                pass
            await asyncio.wait_for(self.bridge.control(action, value), budget)
            self.state.trace(f"Cloud command completed: {action}")
            if not self.state.command_in_progress:
                self.state.decision = "Watching the charger — no command in progress"
            await self._ack(cmd_id, True, self.state.last_action)
        except Exception as exc:  # noqa: BLE001 — the ack carries the reason
            self.state.trace(f"Cloud command failed: {action}")
            if self.state.decision == "Command being confirmed on the charger":
                self.state.decision = "Last command was not accepted"
            await self._ack(cmd_id, False, str(exc)[:300])

    async def _ack(self, cmd_id, ok: bool, result: str) -> None:
        try:
            await self._rpc("wl_connect_bridge_ack", {
                "p_device_key": self.cfg.cloud_device_key,
                "p_cmd_id": cmd_id, "p_ok": ok, "p_result": result,
            })
        except Exception as exc:  # noqa: BLE001
            log.warning("ack %s failed: %s", cmd_id, exc)

    async def sync_once(self) -> int:
        """One push/pull cycle. Returns the number of commands executed."""
        cmds = await self._rpc("wl_connect_bridge_sync", {
            "p_device_key": self.cfg.cloud_device_key,
            "p_state": self._state_payload(),
            "p_version": __version__,
        })
        if cmds is None:
            raise IOError("device key was not accepted")
        cmds = cmds if isinstance(cmds, list) else []

        # Newest-wins per action: if the queue holds several commands of the
        # same action, only the newest runs; older ones are acked superseded.
        def stamp(cmd: dict) -> str:
            return str(cmd.get("created_at") or "")

        newest: dict[str, dict] = {}
        for cmd in cmds:
            action = str(cmd.get("action", ""))
            current = newest.get(action)
            if current is None or stamp(cmd) >= stamp(current):
                newest[action] = cmd

        ran = 0
        for cmd in cmds:
            action = str(cmd.get("action", ""))
            if action in newest and newest[action] is not cmd:
                await self._ack(cmd.get("id"), False, "superseded by a newer command")
                continue
            await self._execute(cmd)
            ran += 1
        was_ok = self.state.cloud_ok
        self.state.cloud_ok = True
        self.state.cloud_error = ""
        if not was_ok:
            self.state.trace("SEMS EV CONNECT cloud sync connected")
        return ran

    async def run_forever(self) -> None:
        while True:
            if not self.enabled:
                self.state.cloud_ok = False
                await asyncio.sleep(5)
                continue
            try:
                await self.sync_once()
            except asyncio.CancelledError:
                await self.close()
                raise
            except Exception as exc:  # noqa: BLE001 — cloud loss must never stop local control
                if self.state.cloud_ok or not self.state.cloud_error:
                    log.warning("SEMS EV CONNECT sync failed: %s", exc)
                    self.state.trace("SEMS EV CONNECT cloud sync disconnected; retry scheduled")
                self.state.cloud_ok = False
                self.state.cloud_error = str(exc)[:200]
            await asyncio.sleep(max(2, int(self.cfg.cloud_sync_seconds)))
