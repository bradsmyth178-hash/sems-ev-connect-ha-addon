"""GoodWe HCA G2 (GW7K/11K/22K-HCA-20) Modbus register map.

Source: GoodWe "AC charger Gen 2 Modbus protocol" V1.0.15 (2025-09-12).
All registers are 16-bit holding registers unless noted U32 (two registers, big-endian).
"""

DEFAULT_UNIT_ID = 247  # 0xF7
DEFAULT_PORT = 502

# --- Read block (contiguous, 10000..10039) -------------------------------------
BLOCK_START = 10000
BLOCK_LEN = 40

EMS_DISPATCH = 10000
FAULT_1 = 10001
FAULT_2 = 10002
FAULT_3 = 10003
WARN_5 = 10005
WARN_6 = 10006
HW_FAULT_7 = 10007
VOLT_A = 10009      # /10 V
VOLT_B = 10010
VOLT_C = 10011
CURR_A = 10012      # /10 A
CURR_B = 10013
CURR_C = 10014
POWER = 10015       # /10 kW
SESSION_KWH = 10016  # /10 kWh
STATUS = 10017
COMMS = 10018
PLUG_AND_CHARGE = 10019
RESERVATION = 10020
ENSURE_MIN_POWER = 10024
DLM_ENABLE = 10025
MAX_CHARGE_KWH = 10027   # /10
MIN_CHARGE_KWH = 10028   # /10
MAX_POWER = 10029        # /10 kW, 7 kW unit range 14..70
BATTERY_SOC_FLOOR = 10030
CHARGE_MODE = 10032      # 0 fast, 1 pv, 2 pv+battery
GRID_IMPORT_CAP = 10039  # /10 kW

# --- Identity block -------------------------------------------------------------
SERIAL = 10040       # STR, 8 regs
FW_VERSION = 10048   # STR, 2 regs
HW_VERSION = 10056   # STR, 2 regs
POWER_SPEC = 10058   # 0=7kW 1=11kW 2=22kW
PHASE_TYPE = 10059   # 0=three-phase 1=single-phase

# --- Control / misc -------------------------------------------------------------
CHARGE_SWITCH = 10060    # write 2 = on, 1 = off
SESSION_SECONDS = 10063  # U32
LIFETIME_KWH = 10065     # U32, /10 kWh
CAR_CONNECTION = 10075   # 0 disconnected, 1 half, 2 connected
START_MODE = 10076
POWER_SOURCE = 10108     # bit0 grid, bit1 pv, bit2 battery

STATUS_NAMES = {
    0: "Idle (unplugged)",
    1: "Idle (plugged)",
    2: "Handshaking",
    3: "Charging",
    4: "Charge complete",
    5: "Fault",
    6: "Scheduled start",
    7: "Maintenance",
    8: "Start failed",
    9: "Updating",
    10: "Paused (insufficient PV/battery)",
}

# SEMS-cloud display names. The SEMS link reuses the numeric status codes above
# so the OCPP mapping stays shared, but code 7 means "the cloud cannot see the
# charger" there — showing the G2 Modbus label "Maintenance" for that would be
# wrong, so the cloud path gets its own name table.
SEMS_STATUS_NAMES = {
    **STATUS_NAMES,
    7: "Offline (not reachable from SEMS cloud)",
}

# Charger status -> OCPP 1.6 ChargePointStatus
OCPP_STATUS = {
    0: "Available",
    1: "Preparing",
    2: "Preparing",
    3: "Charging",
    4: "Finishing",
    5: "Faulted",
    6: "Preparing",
    7: "Unavailable",
    8: "Faulted",
    9: "Unavailable",
    10: "SuspendedEVSE",
}

CHARGE_MODES = {0: "Fast", 1: "Solar only", 2: "Solar + battery"}

FAULT_BITS = {
    FAULT_1: ["Emergency stop", "Overvoltage", "Overcurrent", "Undervoltage",
              "Connector fault", "S2 disconnected", "Ambient over-temperature", "Plug over-temperature"],
    FAULT_2: ["Door access", "Grounding fault", "Handshake timeout", "RFID comms",
              "Display comms", "Meter IC comms", "Output relay", "Plug lock"],
    FAULT_3: ["Output short circuit", "Leakage current", "Paused >10 min", "Meter reading abnormal",
              "Offline at PV/battery start", "Insufficient power at PV/battery start"],
    HW_FAULT_7: ["External flash", "EEPROM", "Leak detector", "Input power abnormal",
                 "SN not registered", "Factory params abnormal", "Unauthorised firmware"],
}


def decode_bits(value: int, names: list[str]) -> list[str]:
    return [n for i, n in enumerate(names) if value & (1 << i)]


def regs_to_str(regs: list[int]) -> str:
    out = bytearray()
    for r in regs:
        out += bytes([(r >> 8) & 0xFF, r & 0xFF])
    return out.split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()


def u32(hi: int, lo: int) -> int:
    return ((hi & 0xFFFF) << 16) | (lo & 0xFFFF)


def kw_to_reg(kw: float, unit_kw: int = 7) -> int:
    """Clamp a kW request into the charger's writable range (tenths of kW)."""
    lo = 14 if unit_kw == 7 else 42
    hi = unit_kw * 10
    return max(lo, min(hi, int(round(kw * 10))))


def amps_to_kw(amps: float, phases: int = 1, volts: float = 230.0) -> float:
    return amps * volts * phases / 1000.0
