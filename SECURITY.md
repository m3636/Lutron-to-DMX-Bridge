# Security

Please report vulnerabilities privately through **Security → Report a vulnerability** on this repository's
GitHub page, not in a public issue.

## What the app is designed to do

* It controls lighting on your own network. It has no cloud service and no account.
* The web GUI has **no login**. By default it listens on `127.0.0.1` only, answers only loopback `Host`
  names, and refuses cross-site state-changing requests. If you change `web_host` to a LAN address,
  anyone who can reach that port can switch your lights and disable your schedules, so do that only on a
  network you trust (or behind your own authenticating reverse proxy).
* The only internet access is the optional update check against this repository's GitHub releases.
* Settings, including the RadioRA 2 integration login, are stored in `config.json` in the app's data
  folder. Protect that folder like any other file holding a password.
* A settings export (Settings → Export & import settings, or `lutron-to-dmx-bridge export-config`) leaves out the
  integration login, the update token and the LEAP pairing keys unless you explicitly include them. A file made with
  them holds those secrets in plain text, so treat it like `config.json`. The automatic backup written before an
  import always includes them and is readable by your user only.
* Backups made with `lutron-to-dmx-bridge backup` contain the controller's full project export, which includes the
  installer's name and contact details. Treat them as private.
