# SEMS-Plus API contract (vendored)

The `SemsLink` in `sems_link.py` drives a GoodWe GW7K-HCA (Gen 1, SEMS
cloud only) through GoodWe's **SEMS-Plus web API**. This file vendors the
contract so the repo never depends on the live reference repository again.

**Primary reference (authoritative):**
`prezervos/goodwe-wallbox-sems-home-assistant`,
file `custom_components/sems_wallbox/sems_api.py` (API_VERSION "1.4.0"),
**pinned commit `9ce5772195f13cc9c4082a93e09b87213a1e2a6a`** (2026-07-31).

**Compared fork:** `WolfrageTV/goodwe-wallbox-sems-gen2-fixed`, pinned commit
`7cd4ba87bb15c0c8b00ca6aecdd26515d4547751` (v1.1.3, 2026-04-02) — see
"Fork comparison" at the end. It is the *legacy* v3 lineage; prezervos 1.4.0
supersedes it.

Everything below marked **[verified]** was read directly from the pinned
reference source. Anything marked **[PENDING REAL-HARDWARE CONFIRMATION]** is
an inference and must be confirmed against real charger hardware.

---

## Why not the legacy semsportal.com v3 API

The legacy flow (`https://www.semsportal.com/api/v3/Common/CrossLogin` with a
**plaintext** `pwd`, commands via `v3/EvCharger/SetChargeMode` and
`v3/EvCharger/Charging`) was explicitly abandoned by the reference because the
old SetChargeMode call left the wallbox **busy**, causing ~30 s timeouts when
the newer gateway set-mode arrived ([verified] docstring in
`set_charge_mode_gen2`: "Skips the legacy semsportal.com SetChargeMode call
entirely — this avoids the wallbox being 'busy'"). Do not reintroduce those
endpoints.

## Authentication [verified]

### Login

```
POST https://semsplus.goodwe.com/web/sems/sems-user/api/v1/auth/cross-login
```

Headers:

| header        | value |
|---------------|-------|
| Content-Type  | application/json |
| Accept        | application/json |
| token         | `{"uid":"","timestamp":0,"token":"","client":"semsPlusWeb","version":"","language":"en"}` (JSON string) |
| client        | `semsPlusWeb` |
| neutral       | `0` |
| currentlang   | `en` |
| x-signature   | empty-credential signature, see below |

Body:

```json
{
  "account": "<email>",
  "pwd": "<base64(md5_hexdigest(password))>",
  "agreement": 1,
  "isLocal": false,
  "isChinese": false
}
```

`pwd` is **base64 of the ASCII MD5 hex digest** of the plaintext password
(observed from browser traffic capture, per the reference). Plaintext
passwords are never sent.

Success: `code` in `(0, "0", "00000")` (and `hasError` falsy). `data` is the
token dict — it must contain at least `uid`, `token`, `timestamp`, and may
contain `api`: the **regional gateway base URL** to use for all further calls.

### x-signature [verified]

For every request (from the semsplus.goodwe.com JS bundle):

```
ts     = current unix time in milliseconds, as string
digest = sha256(f"{ts}@{uid}@{token}").hexdigest()
x-signature = base64(f"{digest}@{ts}")
```

For the login request itself `uid` and `token` are empty strings
(`sha256(f"{ts}@@")`).

### Signed request headers [verified]

Every post-login request carries: `token` = full JSON dump of the login token
dict, `client: semsPlusWeb`, `neutral: 0`, `currentlang: en`, and a fresh
`x-signature` computed from the token's `uid` + `token`.

## Regional gateway resolution

- Default base: `https://eu-gateway.semsportal.com/web/sems` [verified —
  reference default `_EuGatewayBase`].
- Overridden at runtime from the login response's `data.api` field [verified].
- **[PENDING REAL-HARDWARE CONFIRMATION]** Which gateway an **Australian**
  account is routed to. Research notes name `au.semsportal.com` as a regional
  host of the *legacy* v2/v3 API (`/api/v2/common/crosslogin`); the SEMS-Plus
  flow's AU routing is expected to arrive via `data.api`, which our client
  follows. Our client tries the login-provided base first, then the EU default
  (`SemsLink._fallback_base`, overridable), falling through candidates on
  HTTP 404/5xx and connection errors.

## Endpoints (relative to the gateway base)

All are JSON POST unless noted. Success is `code` in `("0","00000")` **or**
`data === true` [verified].

### `sems-remote/api/ev-charger/detail` — charger status [verified]

Body: `{"sn": "<serial>"}` plus `plantId` and `productModel` when known.
Response `data` field names vary between payload generations; the reference
reads each value from a candidate list (first non-null wins):

| normalised key | candidate response fields |
|---|---|
| status | `status`, `statusCode`, `chargeStatus` |
| workstate | `workstate`, `workState`, `carState` |
| model | `model`, `deviceModel`, `productModel` |
| fireware | `fireware`, `firmware`, `softwareVersion` |
| chargeEnergy | `chargeEnergy`, `chargedEnergy`, `totalEnergy` |
| power | `power`, `chargePower`, `activePower` |
| current | `current`, `chargeCurrent` |
| chargeMode | `chargeMode`, `mode`, `workMode` |
| set_charge_power | `chargePowerSetted`, `chargeMaxPower` |
| rated max power | `ratedMaxiChargePower`, `ratedMaxChargePower` |

Caveats [verified in reference comments]:
- `startStatus`/`isCharging` from detail is **unreliable** (always false in PV
  mode) — do not use it for charging state.
- detail `power`/`chargePower` is the **inverter allocation limit**, not the
  actual EV draw.
- `status` string values seen: `EVDetail_Status_Title_Charging`,
  `EVDetail_Status_Title_Waiting`, `EVDetail_Status_Title_Offline`, and Gen2
  lowercase forms `charging`, `available`, `standby`, `offline`, `unavailable`.
- `workstate` values seen: legacy `EVDetail_Status_Waiting_Stat00` (unplugged),
  `Stat01` (plugged), `Stat02` (finished); Gen2 `available_gun_no_insered`/
  `available_gun_no_inserted`, `available_gun_insered`/`available_gun_inserted`,
  `prepare`, `finishing`, `finish`, `suspended_evse`, `suspended_ev`.

### `sems-plant/api/v1/chargePile/getLastCharge` — live session (GET) [verified]

`GET <base>/sems-plant/api/v1/chargePile/getLastCharge?chargeSn=<sn>&pwId=<plantId>`

Success code is `"00000"`. `data.chargeLog`:

| field | meaning |
|---|---|
| `workStu` | **6 = actively charging** (authoritative signal), 8 = session finished, vehicle still connected |
| `pevChar` | actual EV power draw, kW |
| `chargeTimeLength` | session duration, minutes |
| `currentChargeQuantity` | **session** energy, kWh |

### Energy semantics [verified]

The cloud API exposes **charge-session-scoped** energy only:
`currentChargeQuantity` (getLastCharge) and `chargeEnergy` (detail). The
reference's cloud energy sensor is fed by `currentChargeQuantity`; its only
lifetime-total sensor is fed by **Modbus register 10065**, which a Gen 1
charger does not offer. There is **no verified cumulative lifetime field in
the cloud API** — `sems_link.py` therefore synthesises a monotonic lifetime
counter (base + current session, rolled over on session reset) so OCPP meter
values can never decrease. The counter restarts at bridge restart.

### `sems-remote/api/ev-charger/set-mode` — mode / power [verified]

Body:

```json
{
  "sn": "<serial>",
  "plantId": "<plantId>",        // REQUIRED
  "mode": 0 | 1 | 2,             // 0 Fast, 1 PV priority, 2 PV + battery
  "productModel": "<model>",     // when known
  "chargePowerSetted": 3.5,      // float kW — Gen1 field
  "chargeMaxPower": 3.5          // float kW — G2 field; ALWAYS send both
}
```

Rules [verified in reference select.py/number.py comments]:
- Switching **to Fast (0)** REQUIRES the power fields, otherwise SEMS
  **silently ignores** the command.
- Switching to PV modes (1, 2) must **NOT** send power fields — doing so makes
  the API revert to Fast mode.
- Both `chargePowerSetted` and `chargeMaxPower` are sent as **floats**.
- Timeout 90 s (`_SetModeTimeout`) — the device can take 60-90 s to respond.
- Optional per-mode params: `ensureMinimumChargingPower` (0 or 170),
  `maxEnergy`, `minEnergy`, `soc` (%), `finishTime` ("0" = ASAP, "1".."6" hours).

### `sems-remote/api/ev-charger/startCharge` / `stopCharge` [verified]

Body: `{"sn", "plantId", "productModel"?}` — same as set-mode without mode.
Timeout 90 s. Same success/error codes.

### `sems-plant/api/portal/stations/page` — plantId auto-detect [verified]

Body `{"current": 1, "size": 50}`. Response `data` may be a list, or a dict
with `dataList`/`records`/`list`/`data`. Station id is the first of
`stationId`, `id`, `plantId`, `powerStationId`. Auto-detection works only for
**single-plant accounts**; multiple plants require explicit configuration.

### Other reference endpoints (not used by the bridge)

- `sems-remote/api/ev-charger/set-config` — single-property toggles
  (`chargedNow`, `dynamicLoad`, `phaseSwitch`, `currentLimit`; magic value
  **170 (0xAA)** = feature enabled for boolean-style fields).
- `sems-remote/api/ev-charger/control-item-content-list/{sn}` (GET) — device
  metadata (`productModel`, rated power).
- `sems-plant/api/web/device/centralized/page` — EV charger enumeration
  (`{"deviceTypeList":["EV_CHARGER"],"current":1,"size":50}`;
  chargers in `data.dataList[].children[]`).
- `sems-remote/api/v2/address/remote/get-work-mode`.

## Error codes [verified]

| code | meaning | handling (mirrors reference) |
|---|---|---|
| `0`, `00000` | success | — |
| `C0602` | session/token expired | clear token, re-login **once**, retry the request **once** |
| `R0305` | `remote_control_fail` — transient | retry set-mode up to **3** times with **2 s** delay |
| anything else | command rejected | fail loudly (bridge raises `RuntimeError`) |

Do **not** match the English message "authorization has expired" — that is
the legacy v3 API's failure shape, not this flow's.

## Known behavioural hazards

- **Silent mode revert (prezervos issue #13, open):** on Gen 1, a charge-mode
  change can initially appear applied and then revert to the SEMS-stored
  value. This is why `sems_link.py` verifies after every write (re-read →
  require consecutive matching reads → bounded re-assert → raise). Load-bearing,
  not defensive theatre.
- SEMS is slow: set-mode/start/stop can take 60-90 s to act; state reads lag
  the device. Verification polls with tolerance instead of failing on the
  first stale read.
- Tokens expire frequently (community reports); C0602 handling above is hit in
  normal operation, not just edge cases.
- GoodWe is migrating SEMS → SEMS Plus; endpoint shapes may drift. This doc is
  pinned to the commit above — re-verify against the reference repo before
  changing shapes.
- No documented hard rate limits, but the bridge paces writes (cloud_link
  spacing + 30 s minimum poll interval) to stay polite.

## Fork comparison: WolfrageTV/goodwe-wallbox-sems-gen2-fixed @ 7cd4ba8

Examined because it claims "working power control, stop/set/start sequence,
correct API fields". Findings from its `sems_api.py` (v0.4.2) and `number.py`:

- It is the **legacy lineage**: `www.semsportal.com/api/v3/Common/CrossLogin`
  with **plaintext** `pwd`, status via `v3/EvCharger/GetCurrentChargeinfo`
  (optional v4 `GetEvChargerMoreView` toggle, default off), commands via
  `v3/EvCharger/SetChargeMode` (`{"sn","type","charge_power"}`) and
  `v3/EvCharger/Charging` (`{"sn","status":"1"|"2"}`), token expiry detected
  by the English substring "authorization has expired".
- **No stop→set→start sequencing exists in the fork's code** — its power
  change is a single SetChargeMode(type=0, charge_power) call, same as
  prezervos pre-1.4. The claimed sequencing could not be substantiated at the
  pinned commit.
- Conclusion: prezervos @ 9ce5772 (SEMS-Plus flow) is strictly newer and is
  the shape this bridge follows. The fork documents the legacy field names
  (`type`, `charge_power`, `status: "1"/"2"`) should a legacy fallback ever be
  needed — it is deliberately **not** implemented here.
