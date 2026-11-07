# Lutron To DMX Bridge: Staff Guides

These guides are for people who use the app but do not build it. You do not need any technical knowledge.

**Written for:** version 0.5.0 (released 7 Nov 2026), on a Mac with an Apple chip (M1 or newer).

## Which guide do I need?

| I want to... | Open this guide |
|---|---|
| Install the app on a Mac for the first time | **1. macOS Setup Guide** |
| Save my settings, or move them to another Mac | **2. Export and Import Settings** |
| Fix something that is not working | **3. Troubleshooting Guide** |
| Run a show tonight | **4. Performance Checklist** (print this one) |

## What the app does, in one minute

The app sits between your lighting console and the building's Lutron lighting system.

- Your console sends DMX (over Art-Net on the network).
- The app listens, and passes the levels on to the Lutron lights.
- You control it from a web page that opens in your browser. The page is served by the app on your own Mac. Nothing goes to the internet except an optional check for new versions.

**Performance mode** is a button that turns off the Lutron timed events (schedules) while a show runs, and puts them back exactly as they were afterwards.

## Words used in these guides

| Word | What it means |
|---|---|
| **The app** | Lutron To DMX Bridge, installed on your Mac |
| **The control page** | The web page that the app opens in your browser |
| **The log window** | The small window with scrolling text that opens with the app. Do not close it while the app is needed |
| **Repeater / controller** | The Lutron box that controls the lights |
| **Schedule** | A timed event inside the Lutron system (for example "lights on at dusk") |
| **Schedule set** | A group of schedules that the app can turn off together. **Performance mode** is a schedule set |
| **Chips** | The five small coloured labels at the top right of the control page (Lutron, DMX in, Output, Alerts, Server) |
| **Green / amber / red** | Green is fine. Amber is worth a look. Red is a problem |

## Screenshots

These guides contain boxes that look like this:

> 📷 **Screenshot needed:** description of what to capture

Each box marks a place where a screenshot would help. Take them on your own Mac once the app is installed. Press **Shift + Command + 4**, then **Space**, then click the window.

## What is finished and what is still pending

Checked against the app's code on 7 Nov 2026. If a feature is marked **pending**, it is not in the released app yet. Do not look for it.

| Feature | Status |
|---|---|
| Install, Gatekeeper steps, Lutron connection, DMX / Art-Net input | In version 0.5.0 |
| Status chips, Status tab, **Clear alerts** button | In version 0.5.0 |
| **Lutron latency** graph and chip (how fast the repeater answers) | In version 0.5.0 |
| Schedule sets (Performance mode) | In version 0.5.0 |
| Export and import settings | In version 0.5.0 |
| **Clear Lutron queue** button (Status tab) | **Pending.** Open change #13, not merged, not released |
| **Level step, %** setting (DMX tab) | **Pending.** Open change #12, not merged, not released |

Not yet tried on real equipment (so wording and behaviour may differ a little):

- Turning Lutron schedules off and on from the app (this is what Performance mode does). **Test it with one harmless schedule before you rely on it for a show.**
- Export and import of settings, on a Mac.
- The macOS security (Gatekeeper) screens. The wording is written from how macOS normally behaves and may differ on your version.

## Contacts (fill in)

| | |
|---|---|
| Person who runs the lighting system | |
| Person who set up this app | |
| Lutron controller address | |
| Lutron integration user name (do not write the password here) | |
| Art-Net universe used by the console | |
| Where the saved settings file is kept | |
