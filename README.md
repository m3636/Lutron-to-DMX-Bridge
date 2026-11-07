# Lutron To DMX Bridge

Local-only control of a Lutron **RadioRA 2** (Main Repeater) or **RA3 / RA2 Select** system from a web GUI and from DMX512 / Art-Net / sACN. No cloud, no Lutron account. Runs on Windows, macOS, and Linux.

> **Independent project.** Not affiliated with, authorized by or endorsed by Lutron Electronics Co., Inc., Artistic Licence, the QLC+ project, Enttec or PKnight. Product names are used only to say what this works with and belong to their owners. **No warranty** (see [LICENSE](LICENSE)): you are switching real lighting, so read [Safety](#safety) before connecting it to a house.

## Features
- Web GUI: every area and light, on/off and dimmer sliders, live updates.
- Your own scenes (stored by this app, not in Lutron) that set many lights at once.
- View schedules (timeclock events) and enable/disable them. Nothing is ever deleted.
- Schedule sets (e.g. "Performance mode"): one button turns off a chosen group of schedules for a show, one button puts them back exactly as they were (schedules that were already off stay off). The prior state is saved in `config.json` first, so it survives a restart or crash.
- DMX input: Art-Net, sACN, or a USB DMX adapter that can *receive*. Each light is mapped to a start address and its parameters (intensity, on/off, fade) take sequential channels.
- Status area: five chips in the header (Lutron, DMX in, Output, Alerts, Server) show health at a glance, click one for the details, or open the **Status** tab for the same numbers plus 2-minute graphs. See [Reading the status area](#reading-the-status-area).
- JSON backup of everything the system lets us read.
- Export and import of this app's own settings (Lutron connection, DMX mapping, scenes, schedule sets) as one versioned file, to move to another computer or recover after the settings were lost. See [Export and import settings](#export-and-import-settings).

## Setting up on a Mac (step-by-step guide)

This guide is for anyone who just wants to use the app. You do not need to know anything technical. It takes about 10 minutes the first time. After that, you only double-click the app.

**What you need**
- A Mac with an Apple chip (M1 or newer). To check: click the Apple menu in the top-left corner, choose **About This Mac**, and look for the word "Apple" next to **Chip**. The app is not built for older Intel Macs.
- Your Mac connected to the same network (Wi-Fi or cable) as the Lutron system.
- The user name and password for the Lutron system, if it is a RadioRA 2 system. Your installer or the person who runs the lighting system can give you these.

### Part 1: Download and install

1. Open the **Releases** page in your web browser: <https://github.com/m3636/Lutron-to-DMX-Bridge/releases>. If it says "404" or "Page not found", ask the person who shared this app to give you access first.
2. Find the newest release at the top. Under **Assets**, click the file that ends in `-macOS.zip`. It is named like `LutronToDMXBridge-0.4.0-macOS.zip`. The file goes to your **Downloads** folder.
3. Open the **Downloads** folder (in the Finder, click **Downloads** in the sidebar).
4. Double-click the `.zip` file. A new item called **Lutron To DMX Bridge** (with a round icon) appears next to it. Your Mac may already have done this for you.
5. Open a new Finder window and click **Applications** in the sidebar.
6. Drag **Lutron To DMX Bridge** from Downloads into the **Applications** window. This matters: if you run the app straight from Downloads, it cannot update itself later.
7. You can now delete the `.zip` file from Downloads.

### Part 2: Open it for the first time (Mac security steps)

macOS is cautious about apps that do not come from the App Store. This app is safe, but it is not signed with an Apple developer account, so your Mac will block it the first time. You only have to do this once. It can take a few tries, and that is normal.

1. In the **Applications** folder, double-click **Lutron To DMX Bridge**.
2. A message appears saying the app "cannot be opened" or "Apple could not verify" it. Click **Done**. Do **not** click **Move to Trash**.
3. Open the Apple menu (top-left corner) and choose **System Settings**.
4. In the left sidebar, click **Privacy & Security**.
5. Scroll down to the **Security** section. You will see a line saying **"Lutron To DMX Bridge" was blocked to protect your Mac**. Click **Open Anyway** next to it.
6. Type your Mac password (or use Touch ID) and confirm.
7. Go back to the **Applications** folder and double-click **Lutron To DMX Bridge** again.
8. A new message asks whether you are sure. Click **Open Anyway** (on some versions the button says **Open**).
9. Wait a few seconds. A small window with scrolling text appears (this is the "log window") and your web browser opens with the control page.

**If nothing seems to happen, or the same message comes back:** that is normal. Double-click the app again. Some Macs need two or three tries before they accept it. If **Open Anyway** is missing in System Settings, double-click the app once more, then look in System Settings again. The button only shows for about an hour after the last blocked attempt.

**Another way:** in the **Applications** folder, hold down the **Control** key and click the app (or right-click it), then choose **Open**, then click **Open** in the message. On newer Macs (macOS 15 "Sequoia" and later) this often does not work, so use the System Settings steps above.

The exact wording of these messages and buttons is a little different on each macOS version. If yours does not match word for word, look for the nearest match. The idea is always the same: tell the Mac that you trust this app.

**Network question.** The first time it runs, your Mac may ask whether **Lutron To DMX Bridge** can **find and connect to devices on your local network**. Click **Allow**. If you click "Don't Allow", the app cannot find the Lutron system. You can fix this later in **System Settings → Privacy & Security → Local Network**: turn on the switch next to Lutron To DMX Bridge.

### Part 3: Connect to the Lutron system (first run only)

The first time, the control page opens on the **Settings** tab. If your browser did not open, click **Open web GUI** in the log window.

1. Under **Lutron system**, click **System type** and pick the kind of system you have:
   - **RadioRA 2 (Main Repeater, telnet integration)**: the classic system with a "Main Repeater" box.
   - **RadioRA 3 / RA2 Select / Caseta (LEAP)**: newer systems with a "processor" or "Smart Bridge" box.
   - **Simulator (demo, no hardware)**: a pretend house, handy for trying things out without touching real lights.

   Not sure which one? Ask the person who set up the lights, or try **RadioRA 2** first.
2. Click **Find my controller**. Wait a few seconds. If you have a RadioRA 2 system, tick **Also scan the whole network** first, then click **Find my controller**. This takes longer, so be patient.
3. When the controller shows up in the list, click **Use this one**. The address fills in by itself. If nothing is found, check that your Mac is on the same network as the Lutron system and try again. If it still fails, ask for the controller's address (it looks like `192.168.1.50`) and type it into **Host / IP address**.
4. **For RadioRA 2:** type the **Integration user name** and **Integration password** you were given.
   **For RadioRA 3 / RA2 Select / Caseta:** click **Pair**, then walk to the Lutron processor or bridge and press the small button on the back within about 3 minutes. Come back and wait for "Paired".
5. Click **Save & reconnect**.
6. Click the **Devices** tab. Your rooms and lights appear. Now you are connected.

You will not need to do Part 3 again. The app remembers your settings, even after a restart.

### Part 4: Everyday use

- **Start the app:** double-click **Lutron To DMX Bridge** in Applications. Your browser opens with the control page. (If the app is already running, double-clicking just opens the page again.)
- **Devices:** click a room's name to open it and see its lights. Each light has a switch to turn it on or off and a slider to dim it. **All on** and **All off** do a whole room at once. Changes made at a wall keypad show up here a moment later.
- **Scenes:** make your own looks (for example "Dinner"). Set the lights how you like them and click **Snapshot all lights**, or click **New scene** to pick lights and levels yourself. Later, click **Activate** to set every light in the scene at once. These scenes belong to this app. They are not Lutron's own scenes, and they do not change anything inside the Lutron system.
- **Schedules:** see the timed events in the Lutron system and turn them off or back on. Nothing is ever deleted.
- **DMX:** connects your lighting console. Ask whoever set up the DMX side before changing anything here.
- **Settings → Export & import settings:** saves everything the app needs to be rebuilt into one file, and restores it later. See [Export and import settings](#export-and-import-settings).
- **Status:** shows whether everything is healthy. The small coloured chips at the top right turn amber or red when something needs attention.
- **Quit the app:** close the small log window, or click **Quit** in it. The control page in your browser stops working when the app is closed. To keep it running, **minimise** the log window (the yellow button) instead of closing it.
- **Updates:** a blue bar appears at the top of the page when a newer version is out. Click **Update now** when it suits you. Your settings and scenes are kept.

> **Be careful with schedules and DMX.** Turning off a schedule means it will not run until you turn it back on. While DMX input is switched on, lights follow the lighting console, so a light the console is not using can be set to 0. If you are unsure, ask before changing these.

### If something goes wrong

| What you see | What to try |
|---|---|
| The Mac says the app is damaged or cannot be opened, and there is no **Open Anyway** button | Double-click the app once more, wait a moment, then look in **System Settings → Privacy & Security** again. Still not there? Delete the app from Applications, download the zip again, and repeat Part 1 and Part 2. |
| The browser does not open | Click **Open web GUI** in the log window. Or type `http://127.0.0.1:8080` into your browser's address bar. If the log window says it picked a different port (for example 8081), use that number instead. |
| **Find my controller** finds nothing | Make sure your Mac is on the same network as the Lutron system. Check **System Settings → Privacy & Security → Local Network** (the switch for this app must be on). Tick **Also scan the whole network** and try again, or type the controller's address yourself. |
| Log says "Could not download ... DbXmlInfo.xml: timed out" (RadioRA 2) | The repeater's built-in web server is slow and handles one request at a time. The app waits longer, retries, and keeps a copy of the last good download in its settings folder (`cache/`); if the download fails or is slow it starts from that copy. The very first start has no copy yet, so if it keeps failing, save `http://<repeater IP>/DbXmlInfo.xml` from a browser and set it as the **Integration Report path** in Settings. |
| Lights show up with numbers instead of names (RadioRA 2) | The app needs the system's "Integration Report" file for the names. Ask the person who set up the system for it, then put its location in **Integration Report path** in Settings and click **Save & reconnect**. |
| The page says it cannot connect, or the **Lutron** chip at the top is red | Check the Mac's network, then check the address, user name and password in Settings. |

**Getting help.** Ask the person who set this up for you first. They can read the log. In the log window, click **Copy log** and paste it into a message. To report a problem on GitHub, open a new issue at <https://github.com/m3636/Lutron-to-DMX-Bridge/issues>. **Never attach a backup file, your `config.json`, or an export from the Lutron system.** These contain private details such as your installer's contact information, your network addresses and your Lutron login. If the log has anything private in it, remove that before you share it.

## Start it with a double-click
Running the app opens a small **log window** (live log, Copy / Save log, Open web GUI, Quit) and opens the **web GUI** in your browser. The first time, before a controller is set up, it opens on the Settings tab. Closing the log window stops the app (minimise it to keep it running). If it is already running, double-clicking again just opens the web GUI. Logs are also kept in a rotating file (`lutron-to-dmx-bridge info` prints where).

| How | Steps |
|---|---|
| **Built app** (nothing to install) | Download `LutronToDMXBridge-<version>-macOS.zip` or `LutronToDMXBridge-<version>-Windows.zip` from the repository's **Releases** page, unzip, double-click **Lutron To DMX Bridge.app** (macOS) or **Lutron To DMX Bridge\Lutron To DMX Bridge.exe** (Windows). Builds of unreleased branches are in the **Artifacts** list at the bottom of a CI run's summary page (not inside a job). |
| **From a checkout, macOS** | Double-click **Launch Lutron To DMX Bridge.command**. The first run creates `.venv` and installs the requirements. |
| **From a checkout, Windows** | Double-click **Launch Lutron To DMX Bridge.bat** (same idea). |
| **Developer** | `python -m pip install -e . && lutron-to-dmx-bridge-app` (or `python -m lutron_to_dmx_bridge.launcher`; add `--simulator`, `--no-window` for terminal-only, `-v` for debug detail). |

**macOS "unidentified developer" warning.** (Non-technical step-by-step version: [Setting up on a Mac](#setting-up-on-a-mac-step-by-step-guide).) The app is not signed or notarised, so Gatekeeper blocks a copy downloaded from GitHub. Either right-click the app and choose **Open**, then **Open** again (once), or run `xattr -dr com.apple.quarantine "Lutron To DMX Bridge.app"` in Terminal. On macOS 15 (Sequoia) right-click is no longer enough: try to open it, then go to **System Settings → Privacy & Security** and click **Open Anyway**. An app you build yourself is not blocked. On macOS 15+ it also asks to allow **Local Network** access: say yes, or the controller cannot be found.

**tkinter.** The log window needs tkinter. python.org installers include it; Homebrew needs `brew install python-tk@3.12` (and a fresh `.venv`). Without it the launcher still works but logs to the terminal instead of a window, and the build script refuses to build an app without it.

**Port in use.** If another program holds port 8080 the app uses the next free port from 8081 and says so in the log. If it is another copy of Lutron To DMX Bridge, it reuses that one.

The built app is the log window and web GUI only; for `pair`, `discover` and `backup` use the pip-installed `lutron-to-dmx-bridge` command (or the same arguments on the app's executable).

## Quick start (no Lutron hardware needed)
```
python -m pip install -e .
lutron-to-dmx-bridge --simulator
```
The simulator is a made-up house: 14 lights in a few rooms, three keypads and three schedules, so you can try everything without a controller.

## Connecting to a real system
| System | How it connects |
|---|---|
| **RadioRA 2** (Lutron Connect app, Main Repeater) | Settings → RadioRA 2: repeater IP, integration login (set in the RA2 software; defaults are often `lutron` / `integration`). Fixtures are read from the repeater's `DbXmlInfo.xml`, or point at an exported copy. |
| **RA3 / RA2 Select** | `lutron-to-dmx-bridge pair` (finds the processor by itself, or pass `<bridge-ip>`), then press the small button on the back of the processor. Local certificates only. |

Pairing checks the controller's certificate against Lutron's published CAs. If your processor presents one that matches neither (`CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`), pairing stops and shows the certificate it saw. If that is your controller, pair again with `lutron-to-dmx-bridge pair --trust-controller-cert` (the web GUI asks you to confirm): the pairing socket then accepts that certificate once (trust on first use; the button press proves physical access), and the connection that actually controls lights always verifies against the CA the processor issued. `lutron-to-dmx-bridge -v pair` shows the details.

### Finding the controller's IP address
Don't know the IP? `lutron-to-dmx-bridge discover` (or **Settings → Find my controller** in the GUI) looks for it:

1. **Bonjour/mDNS** (`_lutron._tcp`, the same thing the Lutron phone app uses). Finds RadioRA 3, RA2 Select, Caseta and HomeWorks QS processors in a few seconds.
2. **Network scan** if mDNS finds nothing (or tick "Also scan", or `lutron-to-dmx-bridge discover --scan`): tries TCP 8081 (LEAP, checks the certificate is Lutron's) and TCP 23 (a RadioRA 2 Main Repeater answers with a `login:` prompt) on this computer's local /24. This is how a classic RadioRA 2 Main Repeater is found, as it is not known to advertise over mDNS.
3. A Lutron MAC address (`00:0F:E7`) in the ARP table raises the match score; it is only a hint.

Once a LEAP processor is paired its Bonjour name is remembered, so if the router hands it a new IP the app finds it again on start-up.

Things that stop discovery: the computer is on a different network/VLAN than the controller, the router filters multicast/mDNS (Lutron asks for Bonjour/mDNS to be allowed and IGMP snooping off), the Windows Firewall prompt for Python was denied, or on macOS 15+ Local Network access is off for the app (System Settings → Privacy & Security → Local Network). Running from Terminal is exempt on macOS. You can always type the IP in by hand; give the controller a DHCP reservation so it stays put.

Backup: `lutron-to-dmx-bridge backup` or the Settings tab. On RadioRA 2 this is a reference snapshot; the RA2 programming software project file is the real recovery source. **A backup contains the controller's full project export, including the installer's name and contact details. Keep it private and never attach it to an issue.**

## Export and import settings

The Lutron backup above saves what the *Lutron system* knows. This is different: it saves **this app's own settings** so you can rebuild the app on a fresh install, move it to another computer, or recover after `config.json` was lost. One JSON file holds:

- the Lutron connection (system type, address, port, mDNS name, integration report path, certificate folder path),
- the DMX input settings and the address mapping,
- your custom scenes,
- the schedule sets (such as "Performance mode"), and the saved prior schedule states of a set that was active at the time,
- the update-check preference, and the web GUI address and port.

**In the web GUI:** Settings → *Export & import settings*. **From the command line:** `lutron-to-dmx-bridge export-config [FILE]` and `lutron-to-dmx-bridge import-config FILE`. **Over the API:** `GET /api/config/export`, `POST /api/config/import/preview` and `POST /api/config/import`.

**Credentials are opt-in.** By default the file does **not** contain the RadioRA 2 integration login, the GitHub update token or the LEAP pairing keys, and importing such a file keeps whatever the app already has. Tick *Include the Lutron integration login, update token and pairing certificates* (CLI: `--include-credentials`) to put them in, so a brand-new install can reconnect without pairing again. A file made that way holds passwords and private keys in plain text: keep it private, never post or attach it anywhere, and delete it when you are done. The app writes the files it creates itself (CLI export, the automatic pre-import backup) readable by your user only.

**Importing** shows what would change first, section by section, with warnings (for example scenes that use lights the connected Lutron system does not have). You choose which sections to import. A ticked section is replaced as a whole, so lights, scenes or mappings that are not in the file are removed from that section. Before anything is changed the current settings are saved, credentials included, to `backups/config-before-import-<time>.json` in the app's data folder, and importing that file undoes the import. The web GUI and the Lutron connection are reconnected automatically; with the command line, stop the app first (it refuses to import while the app seems to be running, because the running app would overwrite the result).

- The web address and port are **not** selected by default, because they decide who on the network can reach the app. Tick them yourself, or pass `--sections` on the command line.
- A schedule set cannot be replaced while a set that the file does not contain is active: restore the schedules first (Schedules tab), or leave that section out. If the file was made while a set was active you can also bring back its saved schedule states, but only when no set is active now.
- Paths such as the integration report and the certificate folder are specific to a computer. Import warns when one does not exist on this computer.
- **Versions:** the file records a format number (`schema_version`) and the app version that wrote it. A file from a *newer* format is refused with a message to update the app; an older format is upgraded automatically; a file from a newer app version with the same format is accepted with a warning, and settings the running version does not know are ignored. The older Lutron system backup file (`Download backup`) can also be imported; only its app settings are used, never its (masked) password.

## DMX hardware
- **Network (recommended):** a node such as the PKnight CR021R turns a physical DMX input into Art-Net/sACN. Point the app at that universe.
- **USB input:** needs an adapter that can receive, such as the Enttec DMX USB Pro.
- The **Enttec Open DMX USB is output-only** and cannot be used as an input.

## How fast can DMX drive the lights?
A RadioRA 2 repeater works through its telnet commands one at a time and passes each on over radio, so it can only take so many per second (the real number depends on your system). DMX sends 44 frames a second, so the app does not forward every frame:

- **Max updates / second / zone** (default 5) and **Max updates / second, all zones** (default 20) cap what is sent. If more zones need an update than the budget allows, the ones that have waited longest go first.
- When a console fades or ramps a light, the app sends each update with a fade time and aims it at where the ramp will be when the next update lands, so the dimmer glides on its own between commands and many lights stay together. A cue that jumps to a level is sent immediately, with no fade. A `fade` channel on a fixture always wins.
- If lights still update one after another, or keep moving after the console has stopped, the repeater is being sent more than it can take: lower **Max updates / second, all zones** (try 10). If your system keeps up easily, raise it. `lutron-to-dmx-bridge -v` logs how many updates are held back and how long the repeater takes to echo each one.
- Art-Net universes start at 0; sACN universes start at 1.
- **Testing without hardware:** [`qlcplus/`](qlcplus/README.md) has a QLC+ workspace that acts as a lighting desk and drives the simulator over Art-Net.

### Protecting the repeater from a DMX storm
Closing this app does not recall commands the repeater has already accepted. If something sends it more than it can carry out (a console chase gone wrong, a feedback loop, a limit set higher than the repeater can take), the backlog sits inside the repeater and the lights keep dimming for as long as it takes to work through it, minutes if it was bad. A fixed limit cannot prevent that, because nobody knows the right number for your system, so with **Protect the repeater** on (the default, DMX tab) the limit above is only a ceiling and the app watches the repeater:

- **It slows down while the repeater is behind.** While commands are flowing the app times a round trip to the repeater twice a second. Above the **latency limit** (500 ms) the allowed commands per second are halved, at most once per queue-drain time; while it is quick again they climb back by 4 per second. The rate saw-tooths around what your repeater can really do. An unanswered probe counts for as long as it has waited, so a stalled repeater cannot hide by going quiet.
- **It never has much in flight.** The number of commands written but not echoed yet is capped at rate x latency limit (and at **Max commands in flight**, 40). A whole-house cue is therefore spread over a fraction of a second instead of going out as one burst; raise the commands-per-second limit if you want bigger bursts.
- **It holds back when the repeater is seconds behind.** Nothing is sent until the round trip drops again. The app keeps only the latest DMX value per light, so nothing piles up here either, and the lights catch up afterwards.
- **It pauses on a storm and raises an alert** (Alerts chip, red banner with a **Resume DMX** button): more than 200 DMX frames per second for 2 s (DMX512 tops out at 44), lights told to reverse direction by 10% or more over 300 times per second for 3 s (noise, a strobe or a loop that dimmers cannot follow), or a repeater that has not recovered after 20 s at the minimum rate. An automatic pause stays until you resume it; a resume starts slowly and ramps up.
- **Pause DMX** in the header does the same on demand: the input is still tracked, nothing is written to the Lutron system. When the repeater is behind, pausing also resets the network connection (TCP reset), which discards commands still sitting in network buffers instead of delivering them after the pause. Commands the repeater has already accepted cannot be recalled.

`python tools/simulate_storm.py` replays a storm against the simulator's modeled repeater (a queue that takes a fixed time per command) with and without protection. To try the app against such a repeater, set `LUTRON_DMX_SIM_SERVICE_MS=100` and use the simulator driver. Untested against a real repeater: the thresholds are guesses until the latency graph shows what yours does.

## Reading the status area
The chips in the top right are always visible. Green is fine, amber is worth a look, red is a problem.

- **Lutron** shows the median round-trip latency to the repeater over the last minute: every couple of seconds, busy or idle, the app asks the repeater for its clock and times the answer, so commands queued ahead of it show up as higher latency. The Status tab graphs it for the last 2 minutes. Amber if the slowest 5% take over 500 ms; red when not connected. "In flight" in the details is commands sent that have not been echoed yet. The repeater's own internal queue cannot be seen, so a growing in-flight count with rising latency is the sign that it is falling behind.
- **DMX in** shows frames per second and the universe. Amber with "no data" when no packet has arrived for 2 seconds, and the details list any other universe it is seeing (a common cause of a silent input).
- **Output** shows commands per second sent to the Lutron system against the limit from the DMX tab (the limit shown drops while the app is slowing down to protect the repeater), plus how many lights are waiting because of it. Red "paused" when DMX output is paused.
- **Alerts** counts warnings and errors logged since the app started, or since you pressed **Clear alerts** on the Status tab, which also lists the latest ones.
- **Server** is the connection between this browser and the app (normally on this computer): "live", "polling" when live updates dropped, or "offline".

The Status tab graphs keep the last two minutes in the browser, so they start empty after a refresh.

## Packaging
`python -m pip install -e . pyinstaller && python packaging/build.py` builds the double-clickable app on the current OS (`dist/LutronToDMXBridge-<version>-macOS.zip` holding `Lutron To DMX Bridge.app`, or `dist/LutronToDMXBridge-<version>-Windows.zip`), starts it once as a check (`--self-check`: simulator up, GUI and API fetched, clean stop), and zips it. The build is described by `packaging/LutronToDMXBridge.spec`; `packaging/make_icons.py` regenerates the icons. CI builds and uploads the Windows and macOS zips.

## Updates
When a newer release exists on GitHub, a blue banner appears at the top of the web GUI (and a line in the log window): **Version X is available · Release notes · Update now · Dismiss**. Dismissing hides that version only; the banner returns for the next one. **Settings → Updates** shows the installed and latest versions and has a **Check now** button.

**Privacy.** This is the one thing in the app that talks to the internet. It asks GitHub's public "latest release" endpoint for this repository, on start-up and every 12 hours, and sends nothing about you, your lights or your network. Untick **Check for updates automatically** (or set `LUTRON_TO_DMX_BRIDGE_NO_UPDATE_CHECK=1`) and the app only contacts GitHub when you press **Check now**. Offline, rate-limited or "not found" simply mean no banner. Only if you run a private fork, GitHub answers "not found" to anonymous checks; paste a read-only fine-grained token (Contents: read, that repository only) into **Settings → Updates**. It is stored in `config.json` like the other settings and is only ever sent to GitHub.

**What "Update now" does**, by how the app was installed:

| Installed as | Update |
|---|---|
| **Lutron To DMX Bridge.app** (macOS) or **Lutron To DMX Bridge\Lutron To DMX Bridge.exe** (Windows) | Downloads `LutronToDMXBridge-<version>-<OS>.zip` from the release, checks its SHA-256 against the release's `SHA256SUMS.txt`, unpacks it into the app's data folder, starts the new copy's `--self-check`, and only then closes the running app. The new copy then swaps itself in, reopens, and the browser tab reloads by itself. If anything goes wrong at any step, the old version stays or is put back and the banner says why. |
| A git checkout (`python -m lutron_to_dmx_bridge`) | Fast-forwards `main` to the release tag and runs `pip install -e .`, then asks you to restart. Refuses (and tells you) if the checkout has uncommitted changes, is not on `main`, `origin` is not this repository, or `main` has diverged. |
| Anything else, or a macOS app still running from Downloads, or a folder the app can't write to | Shows what to do by hand (the macOS case: drag the app into Applications first). |

**Safety.** The download must come from this repository's own release downloads over HTTPS (redirects may only go to GitHub, and the token is never forwarded to another host). A file that does not match its published checksum is deleted and nothing is installed. Nothing happens without a click. If a schedule set (such as Performance mode) is active or DMX input is live, you are asked to confirm first. Settings, scenes, certificates and logs live in the app's data folder and are never touched by an update. The previous version is kept in `<data folder>/updates/previous/<version>/` (one older version, replaced by the next update). To roll back, quit the app and put that copy back in place of the installed one. A note about how the last update went is in `update-result.json` until the next start, and the installer step logs to `logs/update.log`.

Limits: the integrity check proves the download is the file published with the release, not that the release itself is trustworthy (anyone who can publish a release on the repository could publish an update), and the macOS app is not code-signed. Only the arm64 macOS build and the 64-bit Windows build are published.

## Versions and releases
Versions follow [semantic versioning](https://semver.org) (`MAJOR.MINOR.PATCH`; while below 1.0, a new feature bumps MINOR and a fix bumps PATCH). The one place the version lives is `__version__` in `lutron_to_dmx_bridge/__init__.py`; the package metadata, the app's log window, and the macOS bundle all read it.

### How to release
On GitHub: **Actions → New release → Run workflow**, then pick **patch**, **minor** or **major** (or type an exact version such as `1.0.0`) and click the green button. Optionally add a sentence for the top of the release notes. Tick **Dry run** first if you want to see what it would do.

The workflow (`.github/workflows/new-release.yml`) then:
1. checks that CI already passed on the latest `main` commit;
2. runs `tools/bump_version.py`, refusing a version that is not newer than the current one or whose tag/release already exists;
3. runs the test suite on the bumped code;
4. commits `Release vX.Y.Z` to `main` and creates the tag `vX.Y.Z` in one atomic push (nothing is pushed if `main` moved meanwhile, or if the tests fail);
5. starts the CI workflow on the tag, which builds the macOS and Windows apps and publishes the GitHub Release with both zips, `SHA256SUMS.txt`, your optional note, and the list of PRs merged since the previous release.

Why step 5 is explicit: a tag pushed with the built-in `GITHUB_TOKEN` does not trigger other workflows (GitHub's guard against loops), but `workflow_dispatch` is exempt, so the workflow starts CI on the tag itself. No personal access token is needed.

Settings: none for the default setup. If `main` is protected and rejects the version commit, either allow pushes from the workflow, or add a personal access token with `repo` scope as an Actions secret named `RELEASE_TOKEN` and the workflow will push with it (the tag then also triggers a CI run of its own, which the explicit one supersedes).

If the build fails after the tag was pushed: no release is published. Fix the problem on `main` and delete the tag (`git push origin --delete vX.Y.Z`), then run **New release** with the exact same version typed in (it tags the current code without a new version commit), or just release the next patch. If you only need to re-run the same build, use **Actions → CI → Run workflow → Use workflow from: Tag vX.Y.Z**.

By hand still works: `python tools/bump_version.py minor`, merge that change, then `git tag v0.3.0 && git push origin v0.3.0`. CI refuses a tag that doesn't match `__version__`.

## Safety
- **Do not use this for emergency, egress or life-safety lighting**, or anything where a light not responding could hurt someone. It is hobby software with no warranty.
- **DMX input is authoritative.** While DMX input is on, every mapped light follows the console. A mapped light that no console channel is driving is held at 0, so starting a lighting program with nothing patched can turn lights off. Map only the lights you want a console to own.
- **Schedules.** Disabling a schedule (or activating a schedule set) stops it from running until you restore it. Check the banner before you leave a set active, especially for security or exterior lighting.
- **No login on the web GUI.** It listens on `127.0.0.1` only by default. Do not point it at a LAN or the internet unless you trust everyone who can reach the port; see [SECURITY.md](SECURITY.md).
- **Your settings are private.** `config.json` in the app's data folder holds your controller address and integration login (and an update token if you set one).

## Upgrading from "Lutron DMX"
This project was called **Lutron DMX** before it became **Lutron To DMX Bridge**. Nothing about your setup has to be redone: the first start of this version moves your settings folder (`LutronDMX` in `%APPDATA%` / `~/Library/Application Support`, or `~/.config/lutron-dmx`) to the new name (`LutronToDMXBridge` / `lutron-to-dmx-bridge`), including scenes, schedule sets, pairing certificates and backups. If you started the app from a checkout, the launcher reinstalls the package by itself the first time. What changed for scripts: the command is now `lutron-to-dmx-bridge` (and `lutron-to-dmx-bridge-app`), the Python package is `lutron_to_dmx_bridge`, the environment variable `LUTRON_DMX_HOME` is now `LUTRON_TO_DMX_BRIDGE_HOME` (the old name still works), and release files are named `LutronToDMXBridge-<version>-<OS>.zip`.

## Status
Works against the built-in simulator and, in the author's testing, a RadioRA 2 system with two Main Repeaters (connect, read areas, lights, keypads and schedules). Not yet confirmed on real hardware: RA2 timeclock enable/disable commands, LEAP schedule access (RA3 / RA2 Select), and USB / Art-Net behaviour with every kind of adapter. Reports from other systems are welcome; please read the note about private data in the issue template first.

## License
[Apache-2.0](LICENSE). Third-party components are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). "Art-Net(TM) Designed by and Copyright Artistic Licence Engineering Ltd."

## Trademarks
Lutron To DMX Bridge is an independent, community project. It is **not affiliated with, sponsored by or endorsed by Lutron Electronics Co., Inc.** "Lutron", "RadioRA", "Caseta", "Connect" and "HomeWorks" are trademarks or registered trademarks of Lutron Electronics Co., Inc.; they appear here only to describe which products this software works with. The app's name and its logo are not Lutron's and do not use any Lutron artwork: the logo is original (see `packaging/make_icons.py`). All other product names, including Art-Net, QLC+, Enttec and PKnight, belong to their respective owners.
