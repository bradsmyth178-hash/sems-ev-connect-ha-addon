# SEMS EV CONNECT

Connects a GoodWe HCA EV charger to Home Assistant, and keeps your setup page
up to date so support can see what the charger is doing without asking you to
read numbers down the phone.

## Install

1. **Settings → Add-ons → Add-on Store**, open the three-dot menu, choose
   **Repositories**, and paste this repository's URL.
2. **SEMS EV CONNECT** appears in the store. Click it and press **Install**.
3. Press **Start**, then turn on **Start on boot** and **Watchdog** so it looks
   after itself.
4. Press **Open Web UI** and follow the guided setup.

## What you will need

- Your **GoodWe account** email and password — the same ones you use in the
  SEMS app. The add-on signs in as you; nothing is shared with anyone else.
- Your **charger's serial number**. You do not have to go and find it: the
  setup page has a **Find my charger** button that lists the chargers on your
  account and fills the serial in for you.
- The **pairing code** you were given, so your setup page can show live status.
- A **control PIN** of your choosing, which protects the charging controls on
  your home network. Pick it yourself and keep it.

## Why an app runs at your house

A web page on the internet cannot reach devices inside your home network. This
add-on runs at your place and calls out to us — nothing is opened up, no ports
forwarded, no VPN. Stop the add-on and your charger carries on exactly as it
does today.

## Support

Everything, including step-by-step guides, is on your own setup page. If you
get stuck there is an **I'm stuck** button on every section that tells us where
you are, so nobody has to describe the problem from scratch.
