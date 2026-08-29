"""Modbus TCP link to the GoodWe HCA G2. One connection, serialised access."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from pymodbus.client import AsyncModbusTcpClient

from . import registers as R

log = logging.getLogger("bridge.modbus")


@dataclass
class Snapshot:
    ok: bool = False
    status: int = 0
    status_name: str = "unknown"
    car: int = 0
    power_kw: float = 0.0
    session_kwh: float = 0.0
    lifetime_kwh: float = 0.0
    volt_a: float = 0.0
    curr_a: float = 0.0
    max_power_kw: float = 0.0
    mode: int = 0
    mode_name: str = ""
    comms: int = 0
    faults: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def charging(self) -> bool:
        return self.status == 3

    @property
    def lifetime_wh(self) -> int:
        return int(round(self.lifetime_kwh * 1000))

    @property
    def power_w(self) -> int:
        return int(round(self.power_kw * 1000))


class ModbusLink:
    def __init__(self, host: str, port: int = R.DEFAULT_PORT, unit_id: int = R.DEFAULT_UNIT_ID):
        self.host, self.port, self.unit = host, port, unit_id
        self._client: AsyncModbusTcpClient | None = None
        self._lock = asyncio.Lock()

    # --- connection ---------------------------------------------------------
    async def connect(self) -> bool:
        if self._client and self._client.connected:
            return True
        self._client = AsyncModbusTcpClient(self.host, port=self.port, timeout=5)
        ok = await self._client.connect()
        if not ok:
            log.warning("Modbus connect to %s:%s failed", self.host, self.port)
        return ok

    async def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None

    # --- low level ----------------------------------------------------------
    async def read(self, address: int, count: int = 1) -> list[int]:
        async with self._lock:
            if not await self.connect():
                raise ConnectionError("not connected")
            rr = await self._client.read_holding_registers(address, count=count, device_id=self.unit)
            if rr.isError():
                raise IOError(f"read {address}x{count}: {rr}")
            return list(rr.registers)

    async def write(self, address: int, value: int) -> None:
        async with self._lock:
            if not await self.connect():
                raise ConnectionError("not connected")
            wr = await self._client.write_register(address, int(value) & 0xFFFF, device_id=self.unit)
            if wr.isError():
                raise IOError(f"write {address}={value}: {wr}")
            log.info("wrote %s = %s", address, value)

    # --- high level ---------------------------------------------------------
    async def identity(self) -> dict:
        sn = R.regs_to_str(await self.read(R.SERIAL, 8))
        fw = R.regs_to_str(await self.read(R.FW_VERSION, 2))
        spec, phase = await self.read(R.POWER_SPEC, 2)
        return {
            "serial": sn,
            "firmware": fw,
            "kw": {0: 7, 1: 11, 2: 22}.get(spec, 7),
            "phases": 1 if phase == 1 else 3,
        }

    async def snapshot(self) -> Snapshot:
        s = Snapshot()
        try:
            blk = await self.read(R.BLOCK_START, R.BLOCK_LEN)
            g = lambda a: blk[a - R.BLOCK_START]  # noqa: E731
            s.status = g(R.STATUS)
            s.status_name = R.STATUS_NAMES.get(s.status, f"code {s.status}")
            s.power_kw = g(R.POWER) / 10
            s.session_kwh = g(R.SESSION_KWH) / 10
            s.volt_a = g(R.VOLT_A) / 10
            s.curr_a = g(R.CURR_A) / 10
            s.max_power_kw = g(R.MAX_POWER) / 10
            s.mode = g(R.CHARGE_MODE)
            s.mode_name = R.CHARGE_MODES.get(s.mode, "?")
            s.comms = g(R.COMMS)
            for addr, names in R.FAULT_BITS.items():
                s.faults += R.decode_bits(g(addr), names)
            hi, lo = await self.read(R.LIFETIME_KWH, 2)
            s.lifetime_kwh = R.u32(hi, lo) / 10
            s.car = (await self.read(R.CAR_CONNECTION, 1))[0]
            s.ok = True
        except Exception as e:  # noqa: BLE001
            s.error = str(e)
            log.warning("snapshot failed: %s", e)
            await self.close()
        return s

    async def start_charging(self) -> None:
        await self.write(R.CHARGE_SWITCH, 2)

    async def stop_charging(self) -> None:
        await self.write(R.CHARGE_SWITCH, 1)

    async def set_max_power_kw(self, kw: float, unit_kw: int = 7) -> int:
        minimum_kw = 1.4 if unit_kw == 7 else 4.2
        if kw < minimum_kw:
            raise ValueError(f"the charger cannot charge below {minimum_kw:g} kW")
        reg = R.kw_to_reg(kw, unit_kw)
        await self.write(R.MAX_POWER, reg)
        return reg

    async def set_mode(self, mode: int) -> None:
        await self.write(R.CHARGE_MODE, mode)
