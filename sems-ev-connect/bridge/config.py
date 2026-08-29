"""Bridge configuration. Lives in a single YAML file; the web wizard writes it."""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field

import yaml

CONFIG_PATH = os.environ.get("BRIDGE_CONFIG", "/data/bridge.yaml")


# The published GoodWe SEMS endpoints. The API base is configurable because
# GoodWe serves different regions from different hosts, not so it can be
# pointed anywhere: the account password is POSTed to whatever this names.
ALLOWED_SEMS_BASES = (
    "https://eu.semsportal.com",
    "https://au.semsportal.com",
    "https://us.semsportal.com",
    "https://www.semsportal.com",
    "https://semsportal.com",
)


def sems_base_allowed(base: str) -> bool:
    """Blank means GoodWe's own regional routing, which is the normal case.

    Loopback is permitted so the test suites can stand up a fake SEMS in
    process; reaching it already requires code execution on this machine, so
    it adds no exposure. Anything else must be a published GoodWe host.
    """
    base = (base or "").strip().rstrip("/")
    if not base:
        return True
    if base in ALLOWED_SEMS_BASES:
        return True
    host = base.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
    return host in ("127.0.0.1", "localhost", "::1", "[::1]")

@dataclass
class Config:
    # Charger connection: SEMS cloud for GW7K-HCA, or local Modbus for HCA G2.
    charger_connection: str = "sems"

    # Runtime path: direct Modbus control, or Modbus-to-OCPP bridge.
    # Matches the wizard default and every customer document; OCPP is opt-in.
    operating_mode: str = "modbus"

    # GoodWe SEMS cloud (first-generation HCA)
    sems_username: str = ""
    sems_password: str = ""
    wallbox_serial: str = ""
    sems_api_base: str = ""       # deployment/test override; blank uses GoodWe regional routing

    # Charger (Modbus TCP)
    charger_host: str = ""
    charger_port: int = 502
    charger_unit_id: int = 247
    charger_kw: int = 7            # 7, 11 or 22
    phases: int = 1                # 1 or 3

    # OCPP central system
    ocpp_url: str = "ws://homeassistant.local:9000"   # HA OCPP integration default
    charge_point_id: str = "goodwe-hca"
    ocpp_id_tag: str = "SUNLANDS"  # idTag reported on StartTransaction (plug & charge)
    ocpp_basic_auth_user: str = ""
    ocpp_basic_auth_pass: str = ""

    # Timing
    poll_seconds: int = 5           # Modbus poll
    meter_seconds: int = 30         # MeterValues interval while charging
    heartbeat_seconds: int = 300

    # Behaviour
    remote_start_sets_fast_mode: bool = False   # when RemoteStart arrives, also force Fast mode
    control_pin: str = ""          # required for write/control operations in the local web UI
    web_port: int = 8099

    # SEMS EV CONNECT cloud link (outbound-only; filled from a pairing code)
    cloud_url: str = ""
    cloud_anon_key: str = ""
    cloud_device_key: str = ""
    cloud_sync_seconds: int = 3

    # Guided first live test: proof that remote control really reaches THIS
    # charger. Only set by the wizard's stepper after a change was verified,
    # a human confirmed it at the charger, and the original mode was restored.
    first_live_test_passed: bool = False
    first_live_test_at: str = ""
    # Mode the charger was on when a live test started, persisted so an
    # abandoned run can always be put back — a browser that dies mid-test must
    # never leave the customer's charger silently on the wrong setting.
    live_test_pending_mode: int = -1

    configured: bool = False

    @property
    def ready(self) -> bool:
        if not self.configured:
            return False
        if self.charger_connection == "sems":
            charger_ready = bool(self.sems_username and self.sems_password and self.wallbox_serial)
        else:
            charger_ready = bool(self.charger_host)
        if not charger_ready:
            return False
        return self.operating_mode == "modbus" or (
            self.operating_mode == "ocpp" and bool(self.ocpp_url) and bool(self.charge_point_id)
        )

    def to_dict(self) -> dict:
        return asdict(self)


def load(path: str = CONFIG_PATH) -> Config:
    cfg = Config()
    data: dict = {}
    if os.path.exists(path):
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        for k, v in data.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
        # Existing installations created before cloud support are local G2
        # configurations. Preserve their connection automatically.
        if "charger_connection" not in data and data.get("charger_host"):
            cfg.charger_connection = "modbus"
    # Env overrides (handy for docker/add-on)
    for key in cfg.to_dict():
        env = os.environ.get("BRIDGE_" + key.upper())
        if env is not None:
            cur = getattr(cfg, key)
            if isinstance(cur, bool):
                setattr(cfg, key, env.lower() in ("1", "true", "yes"))
            elif isinstance(cur, int):
                try:
                    setattr(cfg, key, int(env))
                except ValueError:
                    # A typo'd env var must not stop the bridge from booting.
                    pass
            else:
                setattr(cfg, key, env)
    if (
        "charger_connection" not in data
        and not os.environ.get("BRIDGE_CHARGER_CONNECTION")
        and cfg.charger_host
        and not cfg.sems_username
    ):
        cfg.charger_connection = "modbus"
    return cfg


def apply_pairing_code(cfg: Config, code: str) -> bool:
    """Fill the cloud fields from a SEMS EV CONNECT pairing code.

    The code is base64(JSON {u: platform URL, k: publishable key, d: device secret})
    generated once in the Wattlane staff view. Returns False on garbage input
    without touching the config."""
    import base64
    import json
    try:
        data = json.loads(base64.b64decode(code.strip().encode("ascii"), validate=True))
        url, key, dev = str(data["u"]), str(data["k"]), str(data["d"])
    except Exception:  # noqa: BLE001 — any malformed code is just "not a pairing code"
        return False
    if not url.startswith("https://") or len(dev) < 16:
        return False
    cfg.cloud_url, cfg.cloud_anon_key, cfg.cloud_device_key = url, key, dev
    return True


def save(cfg: Config, path: str = CONFIG_PATH) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump(cfg.to_dict(), f, sort_keys=False)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
