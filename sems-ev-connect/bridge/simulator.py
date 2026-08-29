"""Fake GoodWe HCA G2 on Modbus TCP. `python -m bridge.simulator [port]`

Behaves like the real unit for the registers the bridge uses:
- write 10060=2 -> starts "charging" if a car is connected; 10060=1 stops
- power ramps to the 10029 cap, session/lifetime energy accumulate
- press ENTER in the terminal to toggle the car plugged/unplugged
"""
from __future__ import annotations

import asyncio
import sys

from pymodbus.server import ModbusTcpServer
from pymodbus.simulator import DataType, SimData, SimDevice

from . import registers as R

UNIT = R.DEFAULT_UNIT_ID


def _str_regs(s: str, n: int) -> list[int]:
    b = s.encode().ljust(n * 2, b"\x00")
    return [(b[i] << 8) | b[i + 1] for i in range(0, n * 2, 2)]


class Sim:
    def __init__(self):
        self.regs = [0] * 11000
        self.device = SimDevice(
            id=UNIT,
            simdata=[SimData(address=0, count=11000, values=0, datatype=DataType.REGISTERS)],
            action=self._action,
        )
        self.car_connected = True
        self.charging = False
        self.lifetime_wh = 1234500
        self.session_wh = 0
        self.set(R.MAX_POWER, 70)
        self.set(R.CHARGE_MODE, 1)
        self.set(R.VOLT_A, 2380)
        self.set(R.COMMS, 0b00101)
        self.set(R.POWER_SPEC, 0)
        self.set(R.PHASE_TYPE, 1)
        self.setm(R.SERIAL, _str_regs("5011KSIM0001", 8))
        self.setm(R.FW_VERSION, _str_regs("1.05", 2))
        self.setm(R.HW_VERSION, _str_regs("V2", 2))
        self._refresh()

    async def _action(self, function_code, start_address, address, count, current_registers, set_values):
        """Serve reads from our own register array; capture writes into it."""
        if set_values is None:
            current_registers[:] = self.regs[start_address:start_address + len(current_registers)]
        else:
            for i, v in enumerate(set_values):
                self.regs[address + i] = int(v) & 0xFFFF
        return None

    def get(self, addr: int) -> int:
        return self.regs[addr]

    def set(self, addr: int, v: int) -> None:
        self.regs[addr] = v & 0xFFFF

    def setm(self, addr: int, vals: list[int]) -> None:
        self.regs[addr:addr + len(vals)] = [v & 0xFFFF for v in vals]

    def _refresh(self) -> None:
        if not self.car_connected:
            status, car = 0, 0
        elif self.charging:
            status, car = 3, 2
        else:
            status, car = 1, 2
        self.set(R.STATUS, status)
        self.set(R.CAR_CONNECTION, car)
        cap_kw = self.get(R.MAX_POWER) / 10
        p = cap_kw if self.charging else 0.0
        self.set(R.POWER, int(p * 10))
        self.set(R.CURR_A, int(p * 1000 / 238 * 10))
        self.set(R.SESSION_KWH, int(self.session_wh / 100))
        lw = int(self.lifetime_wh / 100)
        self.setm(R.LIFETIME_KWH, [(lw >> 16) & 0xFFFF, lw & 0xFFFF])
        self.set(R.POWER_SOURCE, 0b010 if self.charging else 0)

    async def tick(self) -> None:
        while True:
            await asyncio.sleep(1)
            sw = self.get(R.CHARGE_SWITCH)
            if sw == 2 and self.car_connected and not self.charging:
                self.charging = True
                self.session_wh = 0
                print("[sim] charging started")
            elif sw == 1 and self.charging:
                self.charging = False
                print("[sim] charging stopped")
            if sw:
                self.set(R.CHARGE_SWITCH, 0)  # command consumed
            if self.charging:
                wh = self.get(R.MAX_POWER) / 10 * 1000 / 3600
                self.session_wh += wh
                self.lifetime_wh += wh
            self._refresh()

    async def keys(self) -> None:
        loop = asyncio.get_event_loop()
        while True:
            await loop.run_in_executor(None, sys.stdin.readline)
            self.car_connected = not self.car_connected
            if not self.car_connected:
                self.charging = False
            print(f"[sim] car {'plugged in' if self.car_connected else 'unplugged'}")
            self._refresh()


async def run(
    port: int = 5020,
    interactive: bool = True,
    ready: asyncio.Future | None = None,
) -> None:
    sim = Sim()
    server = ModbusTcpServer(sim.device, address=("0.0.0.0", port))
    tasks = [asyncio.create_task(sim.tick())]
    if interactive:
        tasks.append(asyncio.create_task(sim.keys()))
    print(f"[sim] GoodWe HCA G2 simulator on port {port}, unit {UNIT}. ENTER toggles the car.")
    if ready is not None and not ready.done():
        ready.set_result(sim)
    try:
        await server.serve_forever()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await server.shutdown()


if __name__ == "__main__":
    asyncio.run(run(int(sys.argv[1]) if len(sys.argv) > 1 else 5020))
