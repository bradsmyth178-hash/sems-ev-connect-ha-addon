# SEMS EV CONNECT — Home Assistant add-on

Connects a **GoodWe HCA EV charger** to Home Assistant through the GoodWe cloud,
with a guided setup wizard and a first live test.

The first-generation HCA charger has no local network port — no Modbus, no LAN,
no OCPP of its own — so the only way to reach it is the GoodWe cloud. This add-on
signs in with the owner's own GoodWe account, keeps charger status in Home
Assistant, and exposes start, stop, charge mode and a power limit.

## Install

1. **Settings → Add-ons → Add-on Store**
2. Three-dot menu → **Repositories** → paste this repository's URL → **Add**
3. **SEMS EV CONNECT** appears in the store → **Install**
4. **Start**, then switch on **Start on boot** and **Watchdog**
5. **Open Web UI** and follow the guided setup

Full setup notes are in [`sems-ev-connect/README.md`](sems-ev-connect/README.md),
which is also what the add-on's Documentation tab shows.

## What it needs

The owner's GoodWe account email and password (it signs in as them; nothing is
shared onward), and a control PIN they choose, which protects the charging
controls on their home network.

The charger's serial number is **not** something they have to go and find — the
setup page has a **Find my charger** button that lists the chargers on the
account and fills it in.

## Why an app runs at the house

A web page on the internet cannot reach devices inside a home network. This add-on
runs at the house and calls outward — nothing is opened up, no ports forwarded, no
VPN. Stop the add-on and the charger carries on exactly as it did before.

## Architectures

`aarch64` and `amd64`. Those are the architectures whose Python dependencies all
publish musl wheels for CPython 3.12; on anything else pip would try to compile
from source on an Alpine image with no compiler and the install would fail
part-way through. Between them they cover every current Home Assistant Green,
Yellow, Raspberry Pi 4/5 and Intel install.

## Development

This repository is **generated**. The source lives in the Wattlane platform repo
under `integrations/sunlands-ev-bridge/addon`, and `scripts/build-addon-repo.py`
copies it here. Edits belong upstream; a change made directly in this repository
will be overwritten by the next release.
