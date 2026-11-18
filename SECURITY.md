# Security Policy

## Reporting a vulnerability

Please report vulnerabilities privately, not as a public GitHub issue:

- Open a [private security advisory](https://github.com/pcvelz/ha-miele-mobile-control/security/advisories/new)
  on this repository, or
- e-mail the maintainer via the address on the GitHub profile
  ([@pcvelz](https://github.com/pcvelz)).

You should receive a first response within a few days.

## What this integration stores

- Your Miele account **e-mail address and password** are stored in Home
  Assistant's config entry storage (`.storage/core.config_entries`) after you
  enter them in the config flow. Anyone with access to that file can read
  them. Use a dedicated Miele app account if that concerns you.
- The Miele **app token** obtained at login is held **in memory only** and is
  never written to disk. It is renewed automatically.

## What this integration sends, and where

- All traffic goes to the Miele cloud host `rest-eu.domestic.miele-iot.com`
  (login, device list, appliance state, DOP2 read/writes) — the same endpoints
  the official Miele app uses. **No data is sent anywhere else**; there is no
  telemetry and no third-party service involved.
- Credentials are sent to the login endpoint of that host over HTTPS only.
  Secrets and tokens are never written to the log.

## Scope

This integration can change washer settings and, while Mobile Start is armed
at the machine, start it. It cannot bypass the machine's own Mobile Start
arming — a cycle must always be re-armed physically at the machine.
