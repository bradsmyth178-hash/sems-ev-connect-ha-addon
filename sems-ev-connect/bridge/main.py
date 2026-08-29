"""Entrypoint. `python -m bridge.main`"""
from __future__ import annotations

import asyncio
import base64
import logging
import os

import websockets
from aiohttp import web

from . import __version__
from . import config as C
from . import registers as R
from .charge_point import BridgeChargePoint, BridgeState
from .cloud_link import CloudLink
from .modbus_link import ModbusLink
from .sems_link import SemsLink
from .web import build_app

log = logging.getLogger("bridge")


class Bridge:
    def __init__(self, cfg: C.Config):
        self.cfg = cfg
        self.state = BridgeState()
        self._task: asyncio.Task | None = None
        self._cloud_task: asyncio.Task | None = None
        self.cloud = CloudLink(cfg, self)
        self.link: ModbusLink | SemsLink | None = None

    def new_link(self) -> ModbusLink | SemsLink:
        if self.cfg.charger_connection == "sems":
            return SemsLink(
                self.cfg.sems_username,
                self.cfg.sems_password,
                self.cfg.wallbox_serial,
                charger_kw=self.cfg.charger_kw,
                phases=self.cfg.phases,
                api_base=self.cfg.sems_api_base,
                trace_cb=self.state.trace,
            )
        return ModbusLink(self.cfg.charger_host, self.cfg.charger_port, self.cfg.charger_unit_id)

    def poll_interval(self) -> int:
        minimum = 30 if self.cfg.charger_connection == "sems" else 1
        return max(minimum, int(self.cfg.poll_seconds))

    def _note_snapshot(self, snap) -> None:
        """Store a snapshot and derive the connection flags honestly.

        ``charger_connected`` reports whether the charger answered, whatever
        the transport. ``modbus_connected`` is kept for compatibility but only
        ever true for an actual Modbus connection — a healthy SEMS cloud link
        is not a Modbus link.
        """
        self.state.snapshot = snap
        ok = bool(snap and snap.ok)
        self.state.charger_connected = ok
        self.state.modbus_connected = ok and self.cfg.charger_connection == "modbus"
        if not self.state.command_in_progress:
            self.state.decision = (
                "Watching the charger — no command in progress"
                if ok else "Charger not reachable — will keep retrying"
            )

    async def run_forever(self) -> None:
        backoff = 5
        while True:
            if not self.cfg.ready:
                self.state.decision = "Waiting for setup to be completed"
                await asyncio.sleep(2)
                continue
            self.state.decision = "Connecting to the charger"
            link = self.new_link()
            self.link = link
            try:
                connection_timeout = 40 if self.cfg.charger_connection == "sems" else 10
                self.state.identity = await asyncio.wait_for(link.identity(), connection_timeout)
                self.cfg.charger_kw = self.state.identity.get("kw", self.cfg.charger_kw)
                self.cfg.phases = self.state.identity.get("phases", self.cfg.phases)
                log.info("charger %s", self.state.identity)
                self._note_snapshot(await link.snapshot())
                self.state.trace("Charger connection established")
            except Exception as e:  # noqa: BLE001
                log.warning("charger not reachable yet: %s", e)
                self.state.snapshot = await link.snapshot()
                self.state.modbus_connected = False
                self.state.charger_connected = False
                self.state.decision = "Charger not reachable — will keep retrying"
                self.state.trace("Charger connection failed; retry scheduled")
                await link.close()
                self.link = None
                # Back off. A flat retry here meant a charger that never answers
                # drove ~720 SEMS logins an hour against the customer's own
                # GoodWe account, which is how accounts get locked.
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)
                continue

            if self.cfg.operating_mode == "modbus":
                log.info("direct charger control active over %s", self.cfg.charger_connection)
                backoff = 5
                try:
                    while self.cfg.ready and self.cfg.operating_mode == "modbus":
                        self._note_snapshot(await link.snapshot())
                        if not self.state.charger_connected:
                            self.state.last_error = self.state.snapshot.error if self.state.snapshot else "charger unreachable"
                        await asyncio.sleep(self.poll_interval())
                except asyncio.CancelledError:
                    raise
                finally:
                    self.state.modbus_connected = False
                    self.state.charger_connected = False
                    await link.close()
                    self.link = None
                continue

            url = self.cfg.ocpp_url.rstrip("/") + "/" + self.cfg.charge_point_id
            headers = {}
            if self.cfg.ocpp_basic_auth_user:
                tok = base64.b64encode(f"{self.cfg.ocpp_basic_auth_user}:{self.cfg.ocpp_basic_auth_pass}".encode()).decode()
                headers["Authorization"] = f"Basic {tok}"
            try:
                async with websockets.connect(url, subprotocols=["ocpp1.6"], additional_headers=headers,
                                              open_timeout=10, ping_interval=30) as ws:
                    log.info("connected to %s", url)
                    self.state.trace("OCPP WebSocket connected")
                    cp = BridgeChargePoint(self.cfg.charge_point_id, ws, link, self.cfg, self.state,
                                           gate=self.assert_writable)
                    start_task = asyncio.create_task(cp.start())
                    try:
                        interval = await cp.boot()
                    except BaseException:
                        # A failed BootNotification must not leak the reader task.
                        start_task.cancel()
                        await asyncio.gather(start_task, return_exceptions=True)
                        raise
                    hb = asyncio.create_task(cp.heartbeat_loop(interval))
                    poll = asyncio.create_task(cp.poll_loop())
                    backoff = 5
                    session_tasks = {start_task, hb, poll}
                    try:
                        done, _ = await asyncio.wait(
                            session_tasks, return_when=asyncio.FIRST_COMPLETED
                        )
                        for task in done:
                            if not task.cancelled() and task.exception():
                                log.warning("bridge task ended: %s", task.exception())
                    finally:
                        # This finally also runs when the whole bridge is
                        # cancelled during shutdown, not only when one session
                        # task ends naturally.
                        for task in session_tasks:
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(*session_tasks, return_exceptions=True)
            except Exception as e:  # noqa: BLE001
                log.warning("OCPP session error: %s", e)
                self.state.last_error = str(e)
            finally:
                if self.state.ocpp_connected:
                    self.state.trace("OCPP disconnected; reconnect scheduled")
                self.state.ocpp_connected = False
                self.state.modbus_connected = False
                self.state.charger_connected = False
                await link.close()
                self.link = None
            if self.state.reset_requested:
                self.state.reset_requested = False
                backoff = 2
            log.info("reconnecting in %ss", backoff)
            self.state.trace(f"OCPP reconnect scheduled in {backoff} seconds")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    def start(self) -> None:
        self._task = asyncio.create_task(self.run_forever())
        # The Connect link outlives charger reconnects: it reports "charger
        # unreachable" states to the platform rather than disappearing with them.
        if self._cloud_task is None or self._cloud_task.done():
            self._cloud_task = asyncio.create_task(self.cloud.run_forever())

    async def restart(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        # Drop the old link and its HTTP session. Keeping it meant a restart
        # after a serial change could still write to the previous charger.
        if self.link is not None:
            try:
                await self.link.close()
            except Exception:  # noqa: BLE001
                pass
            self.link = None
        prior_trace = self.state.trace_entries()
        self.state.__init__()
        self.state._trace.extend(prior_trace)
        self.state.trace("Bridge restarted")
        self.start()

    async def assert_writable(self, link=None) -> None:
        """Refuse to write to a charger the bridge cannot currently see.

        Every write path goes through this: the local console, the cloud link
        and the OCPP handlers. OCPP used to call the link directly, so it wrote
        blind to a charger that might have been offline for hours.
        """
        if not self.cfg.ready:
            raise RuntimeError("complete the setup wizard first")
        link = link or self.link
        if link is None:
            raise RuntimeError("charger link is not up — no write attempted")
        if self.state.identity is None:
            try:
                self.state.identity = await link.identity()
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"charger has not been identified yet ({exc}) — no write attempted"
                ) from exc
        if not self.state.snapshot_fresh(120):
            self._note_snapshot(await link.snapshot())
            if not self.state.charger_connected:
                err = self.state.snapshot.error if self.state.snapshot else ""
                raise RuntimeError(
                    "charger not currently reachable"
                    + (f" ({err})" if err else "")
                    + " — no write attempted"
                )

    @staticmethod
    def _write_confirmed(action: str, value, snap):
        """True if the charger reports the requested change, False if it still
        reports the old value, None if it did not report back at all."""
        if snap is None or not getattr(snap, "ok", False):
            return None
        try:
            if action == "start":
                return bool(snap.charging)
            if action == "stop":
                return not bool(snap.charging)
            if action == "mode":
                return int(snap.mode) == int(value)
            if action == "max_power":
                return abs(float(snap.max_power_kw) - float(value)) <= 0.11
        except (TypeError, ValueError, AttributeError):
            return None
        return None

    def command_budget(self) -> float:
        """How long a single verified write can legitimately take.

        The verify chain re-asserts a rejected write, so its worst case is a
        multiple of the poll window. A caller timeout shorter than this cancels
        a write that is still in flight and reports it as failed — the one
        outcome the whole verify design exists to avoid.
        """
        link = self.link
        attempts = 1 + int(getattr(link, "verify_reasserts", 0) or 0)
        window = float(getattr(link, "verify_window_switch", 0) or 0)
        settle = float(getattr(link, "verify_wait", 0) or 0) + float(getattr(link, "verify_poll", 0) or 0)
        per_attempt = window + settle + 5.0   # + HTTP round trips
        return max(60.0, attempts * per_attempt + 10.0)

    async def control(self, action: str, value=None) -> None:
        """Serialised charger writes used by the local console and cloud link."""
        if not self.cfg.ready:
            raise RuntimeError("complete the setup wizard first")
        link = self.link
        temporary = link is None
        if temporary:
            link = self.new_link()
        action_name = {
            "start": "start charging",
            "stop": "stop charging",
            "mode": "change charge mode",
            "max_power": "change the power limit",
        }.get(action, "unknown command")
        self.state.command_in_progress = True
        self.state.decision = "Command being confirmed on the charger"
        self.state.trace(f"Command sent: {action_name}")
        try:
            await self.assert_writable(link)
            if action == "start":
                await link.start_charging()
                label = "Charging started"
            elif action == "stop":
                await link.stop_charging()
                label = "Charging stopped"
            elif action == "mode":
                mode = int(value)
                if mode not in (0, 1, 2):
                    raise ValueError("mode must be Fast, Solar only or Solar + battery")
                await link.set_mode(mode)
                label = f"Charge mode set to {R.CHARGE_MODES[mode]}"
            elif action == "max_power":
                kw = float(value)
                if kw <= 0:
                    raise ValueError("power limit must be greater than zero")
                minimum_kw = 1.4 if self.cfg.charger_kw == 7 else 4.2
                if kw < minimum_kw:
                    raise ValueError(f"the charger cannot charge below {minimum_kw:g} kW")
                await link.set_max_power_kw(kw, self.cfg.charger_kw)
                actual_kw = min(kw, float(self.cfg.charger_kw))
                label = f"Power limit set to {actual_kw:g} kW"
            else:
                raise ValueError("unknown control action")
            self.state.last_action = label
            self.state.decision = "Waiting for the charger to report back"
            snap = await link.snapshot()
            self._note_snapshot(snap)
            # The SEMS link verifies internally before returning; the Modbus
            # link reads nothing back. Reporting "verified" for a write nobody
            # confirmed is the kind of small lie that costs a callout, so the
            # trace says which of the two actually happened.
            confirmed = self._write_confirmed(action, value, snap)
            if confirmed is True:
                self.state.trace(f"Command verified: {action_name}")
            elif confirmed is False:
                self.state.trace(f"Command sent but the charger still reports the old setting: {action_name}")
            else:
                self.state.trace(f"Command sent: {action_name} (charger did not report back in time)")
            self.state.decision = "Watching the charger — no command in progress"
        except Exception as exc:
            self.state.trace(f"Command failed: {action_name}")
            if "did not hold" in str(exc) or "reverted or ignored" in str(exc):
                self.state.decision = "Last command did not stick — automatic retries have stopped"
            elif "not currently reachable" in str(exc) or "not been identified" in str(exc):
                self.state.decision = "Charger not reachable — will keep retrying"
            else:
                self.state.decision = "Last command was not accepted"
            raise
        finally:
            self.state.command_in_progress = False
            if temporary:
                await link.close()


async def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = C.load()
    bridge = Bridge(cfg)
    bridge.start()
    app = build_app(cfg, bridge.state, bridge.restart, bridge.control)
    # keep the web app pointing at the live state after restarts
    app["bridge"] = bridge
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", cfg.web_port)
    await site.start()
    log.info("SEMS EV CONNECT v%s — web UI on http://0.0.0.0:%s", __version__, cfg.web_port)
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
