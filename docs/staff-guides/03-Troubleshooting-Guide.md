# 3. Troubleshooting Guide

What to do when something is not working. Start with **Step 0**, then jump to the section that matches what you see.

> Items marked **[PENDING]** are not in the released app (version 0.5.0) yet. Ignore them until the app is updated and the feature appears.

---

## Step 0: Look at the five chips

The five coloured chips at the top right of the control page tell you where the problem is. **Green** is fine, **amber** is worth a look, **red** is a problem. Click any chip for details. The **Status** tab shows the same numbers with graphs of the last 2 minutes.

> 📷 **Screenshot needed:** the five chips, all green, with a label on each.

| Chip | What it tells you | If it is not green, go to |
|---|---|---|
| **Lutron** | Is the app connected to the lighting system, and how quickly does it answer. Shows a number in ms when healthy. | Section B |
| **DMX in** | Is the console's signal arriving. Shows frames per second and the universe. | Section C |
| **Output** | How many commands per second the app is sending to the Lutron system. "held" means lights are waiting their turn. | Section D |
| **Alerts** | How many warnings and errors have been logged since the app started. | Section I |
| **Server** | Is this browser still hearing from the app. | Section F |

**Quick rule:** red or amber *Lutron* means the Lutron side. Red or amber *DMX in* means the console side. If both are fine and lights are still wrong, the problem is the mapping (Section C, part 4).

---

## A. The app will not open

| What you see | What to try |
|---|---|
| "Cannot be opened" or "Apple could not verify" | This is the Mac security check. Follow guide 1, Part 3. Click **Done** (never **Move to Trash**), then **System Settings > Privacy & Security > Open Anyway**, then reopen the app. It can take two or three tries. |
| No **Open Anyway** button | Double-click the app once more, then check System Settings again. The button only appears for about an hour after the last blocked attempt. |
| "The app is damaged" | Delete the app, download the zip again from the Releases page, and reinstall (guide 1, Parts 1 to 3). |
| The app opens but the browser does not | Click **Open web GUI** in the log window. Or type `http://127.0.0.1:8080` into your browser's address bar. If the log window says it chose another port (for example 8081), use that number. |
| Safari shows an error page, nothing loads | Make sure the log window is still open (you can minimise it). If you closed it, the app stopped. Open the app again. |
| Another copy is already running | Double-clicking again just opens the control page again. That is normal. |
| The chips look blank or the page looks wrong after an update | Your browser kept an old copy of the page. Hold **Shift + Command** and press **R** to reload fully. |

---

## B. Lutron connection problems

### B1. The Lutron chip is red ("offline")

1. Click the **Lutron** chip. It says *Not connected*, or *Reconnecting (attempt N, next try in X s)*.
   - *Reconnecting* means the app is still trying. Wait a minute.
2. Check your Mac is on the right network (Wi-Fi name, or cable plugged in). It must be the network the Lutron system is on.
3. Check **System Settings > Privacy & Security > Local Network**: the switch next to **Lutron To DMX Bridge** must be **on**. If you change it, quit and reopen the app.
4. Open **Settings > Lutron system**. Check:
   - **System type** is correct,
   - **Host / IP address** is the controller's address,
   - **Integration user name** and **Integration password** are right (RadioRA 2).
5. Click **Save & reconnect**.
6. Still red? Click **Find my controller** (tick *Also scan the whole network* for RadioRA 2). If the controller's address has changed, choose it from the list with **Use this one** and click **Save & reconnect**.

### B2. Find my controller finds nothing

1. Your Mac must be on the same network (and not a "guest" network) as the Lutron system.
2. Tick **Also scan the whole network** and try again. It takes longer.
3. Local Network permission must be on (see B1, step 3).
4. If the building's network blocks device discovery, type the controller's address by hand. Ask whoever runs the lighting system for it.

### B3. The Lutron chip is amber

Amber means the Lutron system is slow to answer. The chip shows the **median round trip in milliseconds**. It turns amber when the slowest 5% of answers take more than 500 ms.

Click the chip to see:

- **Lutron latency**: last, median and 95th percentile (p95).
- **Commands in flight**: commands sent that the Lutron system has not yet confirmed. A number that keeps climbing means the Lutron system is falling behind.

Go to Section D.

### B4. Lights have numbers instead of names (RadioRA 2)

The app needs the Integration Report to show names. Ask the person who set up the system for it, then enter its location in **Settings > Lutron system > Integration Report path**, and click **Save & reconnect**.

### B5. Log says "Could not download … DbXmlInfo.xml: timed out" (RadioRA 2)

The repeater is slow to hand over its list of lights. The app waits longer, retries, and keeps a copy of the last good download. On the very first start there is no copy yet.

1. Wait one minute, then click **Save & reconnect** again.
2. Do not click it many times in a row. The repeater handles one request at a time.
3. Still failing? Ask someone technical to save the file `http://<repeater address>/DbXmlInfo.xml` from a browser, and use it as the **Integration Report path**.

### B6. Lights on the page do not match the real lights

Changes made at a wall keypad show up on the page a moment later. If they never do, check the **Lutron** chip and **Server** chip (Section F).

### B7. RadioRA 3 / Select / Caseta only: pairing fails

1. Click **Pair** in Settings, and press the small button on the back of the processor or bridge **within about 3 minutes**.
2. If the app shows a certificate warning and asks you to confirm trust, only agree if you are sure this is your own controller.
3. Wait for the word *Paired*, then click **Save & reconnect**.

---

## C. Art-Net DMX input problems

Work through this list from the top. It is in the order problems usually happen.

### C1. Is DMX input switched on?

1. Click the **DMX** tab.
2. **Enable DMX input** must be ticked.
3. **Source** must be **Art-Net (UDP 6454)**.
4. After any change, you must click **Save & apply** (top right of the tab). Changes are not used until you do.

### C2. What does the DMX in chip say?

| Chip says | Meaning | What to do |
|---|---|---|
| **off** (grey) | DMX input is switched off. | Tick **Enable DMX input** on the DMX tab, then **Save & apply**. |
| **stopped** (red) | DMX is enabled but not running. The chip tooltip gives the reason. | Click the chip and read the **Error** line. Fix what it says (for example a wrong **Listen on IP**), then **Save & apply**. If you cannot tell what it means, send the log (Section J). |
| **error** (amber) | A problem was reported. | Click the chip and read the **Error** line. |
| **waiting** (amber) | Listening, but no packet has ever arrived. | Go to C3. |
| **no data N s** (amber) | Data was arriving but stopped N seconds ago. | The console, the network cable or the Art-Net box stopped sending. Go to C3. |
| **44 fps · U0** (green) | Receiving Art-Net on universe 0. | Fine. If lights still do not move, go to C4. |

### C3. Nothing is arriving

Check these in order:

1. **Is the console actually sending Art-Net?** Check its output/network settings. Is output enabled? Is the right universe patched?
2. **Is the Art-Net box or console on the same network as this Mac?** Same Wi-Fi name or the same switch.
3. **Is the universe right?** Click the **DMX in** chip. If it lists **Also seeing universe 1 (listening on 0)**, the console is sending a different universe than the app is listening for. Change **Universe** on the DMX tab to match, then **Save & apply**.
   - **Art-Net universes start at 0** in this app. Some consoles number them from 1, so what the console calls "universe 1" may be 0 here. If in doubt, try the number one lower or higher.
   - (sACN numbers start at 1. If you use sACN, set **Source** to match.)
4. **Is Local Network permission on?** (See B1, step 3.)
5. **Is another lighting program running on this Mac** (for example QLC+)? The app can share the Art-Net port with other programs, so this is rarely the cause. But a second program that is itself *sending* Art-Net can confuse things. If you are not sure, quit it and look again.
6. **Is something blocking it?** A strict office or venue network can block Art-Net broadcast traffic. A direct cable or a small private switch between the console and the Mac is the quickest test.
7. **Wi-Fi?** Art-Net over Wi-Fi often drops packets. For a show, use a cable if you can.
8. **Mac firewall.** If a message ever asks whether to allow **incoming network connections** for the app, click **Allow**. If you clicked **Deny** earlier, open **System Settings > Network > Firewall** and allow the app.
9. **Listen on IP.** Leave this as `0.0.0.0`. If someone changed it to a specific address, check it is this Mac's address on the right network.

Then check the chip again. It should show `44 fps` or similar.

### C4. Data arrives (green), but a light does nothing

1. On the **DMX** tab, look at the **Live monitor**. Move the console fader. Does the matching box change?
   - **No:** the console is patched to a different channel than the app expects. Compare the console's patch to the **Channel mapping** table.
   - **Yes:** the signal is arriving. Continue below.
2. In **Channel mapping**, find the light. Is its **On** box ticked? Is its **Address** the channel you expect?
3. Look at the warnings under the settings (for example *channel overlaps*, or *a zone is mapped twice*). Fix them and **Save & apply**.
4. Click the **DMX in** chip. **Mapped lights** should not be 0.
5. Does the **Output** chip show commands per second? If it shows "N held", see Section D.
6. Does the light work from the **Devices** tab? If not, the problem is the Lutron side (Section B).

### C5. A light drops to 0 or goes dark when the show starts

This is by design. **While DMX input is on, every mapped light follows the console. A mapped light that no console channel is driving is held at 0.** Starting a lighting program with nothing patched can turn lights off.

- Map only the lights you want the console to own (untick **On** for the rest, or remove the row).
- Switch DMX input on only when the console is ready and outputting.
- The app **remembers** that DMX input was on. When you reopen the app, it starts listening again by itself. If the console is not ready, switch it off first.
- To get normal control back, untick **Enable DMX input** and click **Save & apply**.

### C6. Values look wrong (brightness off by a step)

- **intensity** channels use 0 to 255 for 0 to 100%.
- **on_off** channels are on at 128 or above and off below.
- Check the **Params** column of the row is what the console expects.

---

## D. Lights are slow, lag, or keep moving after the console has stopped

The Lutron system can only accept so many commands per second. The console sends 44 frames a second, so the app does not pass on every one. If it is sent more than the repeater can handle, the extra commands queue up *inside the repeater*, and the lights lag behind the console.

### D1. What you will see

- Lights follow the console a few seconds late, or in a staggered way.
- Lights keep moving after you stop the fader.
- The **Output** chip shows "N held".
- The **Lutron** chip is amber, showing high latency.
- **Commands in flight** (click the Lutron chip) is high.

### D2. What to do

Do these in order. Stop at the first one that fixes it.

1. **Wait ten seconds and see.** A short burst is normal and clears itself.
2. **Slow the console down.** Stop long, fast fades across many lights. Prefer shorter ramps or fewer lights moving at once.
3. **Lower the app's speed limit.** On the **DMX** tab, set **Max updates / second, all zones (0 = no limit)** to **10** (the default is 20). Click **Save & apply**. If your system copes easily, you can raise it again later.
4. **[PENDING, not in 0.5.0] Level step, %.** A new setting on the DMX tab called **Level step, %**. A bigger step means fewer commands (and slightly rougher fades). The default (0.5) is the same as today. It is for when a console fader flutters or when you want fewer commands.
5. **[PENDING, not in 0.5.0] Clear Lutron queue.** A new button on the **Status** tab (next to *Clear alerts*). It drops commands the app is still holding back. It **cannot** recall commands already sent to the repeater, and it cannot stop a fade the repeater has already started. It will say what it could not undo.

### D3. After a "DMX storm" (the lights are badly behind or frozen)

A storm is when the repeater has been sent far more than it can take. It then keeps working through its backlog after the console has stopped.

**Do these in order:**

1. **Stop the source.** Stop any running cue or ramp on the console.
2. **Stop the app sending.** On the **DMX** tab, untick **Enable DMX input** and click **Save & apply**. This stops all new commands.
3. **Wait and watch.** Click the **Lutron** chip, or open the **Status** tab. Watch **Commands in flight** fall to 0 and the **Lutron latency** graph drop back to a low number (the amber line is at 500 ms). This often takes a minute or two.
4. **Test from the Devices tab.** Switch one harmless light on and off. If it responds at once, the repeater has recovered.
5. **Re-enable DMX input** with the speed limits lowered (D2, step 3), and run a gentle cue first.
6. **If the lights are still stuck or the latency stays high after about five minutes, power-cycle the repeater.**
   - **Ask the person who runs the lighting system first.** A power cycle takes the repeater offline for a few minutes. During that time the keypads, schedules and automation connected to it do not work.
   - If the building has more than one repeater, only cycle the one that is not responding.
   - Unplug its power (or switch it off at its supply), wait about 10 seconds, plug it back in.
   - Wait a few minutes. The **Lutron** chip will go red, then reconnect by itself.
   - Afterwards, check that the **Schedules** tab still shows what you expect (see Section G if a schedule set was active).
   - This is general advice for this project. It is not a procedure published by Lutron, so confirm it with whoever installed the system.
7. **Click Clear alerts** on the Status tab to reset the warnings counter.

> 📷 **Screenshot needed:** the Status tab, with the Lutron latency graph showing a spike and recovery.

---

## E. Reading the Status tab

Open **Status**. It has the same sections as the chips, with more detail.

| Section | Useful lines |
|---|---|
| **Lutron link** | State (Connected / Reconnecting), System and address, Connected for, **Reconnects since start** (should be 0 or small), **Lutron latency** (last, median, p95), **Commands in flight** |
| **DMX input** | Source and universe, Sender, **Frames/s**, **Last packet**, **Mapped lights**, **Patch warnings**, **Also seeing universe**, Error |
| **Output to Lutron** | **Commands/s** (against the limit), **Held back by the limit**, Sent since start |
| **App** | **Schedule set** (shows "None active" or the active set), Warnings / errors, Version, Uptime, This browser |
| **Graphs** | Frames per second, commands per second, **Lutron latency**. They show the last 2 minutes and start empty after a page reload |
| **Recent warnings and errors** | The latest messages. Handy to read out to the person helping you |

**Clear alerts** resets the Alerts counter and the list. Do this before a show, so any new warning is a real one.

---

## F. The page itself (Server chip)

| Server chip | Meaning | What to do |
|---|---|---|
| **live** (green) | Everything fine. | Nothing. |
| **connecting** | Starting up. | Wait. |
| **no data N s** (amber) | Connected but the app has been silent. It may be busy. | Wait 10 seconds. If it stays, reload the page. |
| **polling** (amber) | Live updates dropped. The page asks for updates every 5 seconds. | Reload the page. |
| **offline** (red) | The page cannot reach the app. | Is the log window still open? If not, the app has stopped: open it again. |

---

## G. Schedule sets and Performance mode problems

| What you see | What to do |
|---|---|
| **Activate** is greyed out | Another set is active. Restore it first (**Restore schedules**). Only one set can be active at a time. |
| A red card: "Could not change …", or "is active, but not everything went through" | Some schedules did not turn off. Do not assume they are off. Check each one in the **Schedules** list below. Turn off the rest by hand with their switches. Ask for help if it keeps failing. |
| "Skipped: …" | A schedule in the set was renamed or removed in the Lutron system. Edit the set to fix it. |
| "No confirmation from the system for …" | The Lutron system did not answer in time. Check the schedule list, then try again. |
| **Restore incomplete** ("Some schedules are still not restored") | Press **Restore schedules** again. It keeps what it remembered until every schedule is back. |
| "Could not restore" | The Lutron system could not be reached. The app keeps the remembered states. Fix the connection (Section B), then press **Restore schedules** again. |
| **Delete** is greyed out | The set is active. Restore it first. |
| The Mac or app crashed during a show | The remembered states are saved to disk *before* anything changes. Open the app again. A banner at the top says *Performance mode ACTIVE*. Click **Restore schedules** there. |
| Quit the app without restoring | Schedules stay turned off. The Lutron system does not know about this app. Open the app again and click **Restore schedules**. |

> **Not yet proven on real equipment.** Turning schedules off and on from the app has been written and tested against a simulator, but has not yet been confirmed on the real Lutron repeaters. After any test, check the **Schedules** list shows the right *enabled* / *disabled* labels. If you are unsure, check in the Lutron app too.

---

## H. Updates

| What you see | What to do |
|---|---|
| Blue bar: "Version X is available" | Optional. Click **Update now** when no show is running. |
| "Update now" says to move the app first | Drag the app into the **Applications** folder (guide 1, Part 2) and open it from there. |
| Update asks to confirm | A schedule set is active, or DMX input is live. Finish the show, restore schedules, switch DMX off, then update. |
| "Could not reach GitHub" or "offline" | The Mac has no internet, or GitHub is down. The app works fine without it. Try later. |
| After an update, **Gatekeeper** appears again | Repeat guide 1, Part 3 once. |

Settings, scenes and logs are never touched by an update. The old version is kept in `~/Library/Application Support/LutronToDMXBridge/updates/previous/`.

---

## I. Alerts

The **Alerts** chip counts warnings and errors since the app started (or since **Clear alerts**). It shows e.g. `2 warn` or `1 err`.

1. Open the **Status** tab and read **Recent warnings and errors**.
2. If they look old and the system is working, click **Clear alerts**.
3. If a message repeats, copy it into your message when asking for help.

---

## J. Getting help

Before asking, collect these three things:

1. **The log text.** In the log window, click **Copy log**, and paste it into your message. (**Save log…** saves it as a file. **Open log folder** shows where logs are kept.)
2. **A screenshot of the Status tab.** Include the chips at the top.
3. **What you did, and what you expected.** For example: "At 7:10 I switched DMX input on. The kitchen lights stayed off."

**Do not send:** a Lutron backup file, a settings export, `config.json`, or anything from the `backups` folder. They contain passwords and the installer's contact details. If the log shows addresses or passwords, remove them first.

To report a problem publicly: https://github.com/m3636/Lutron-to-DMX-Bridge/issues (never attach private files).

---

## Quick reference: problem to section

| Problem | Section |
|---|---|
| App will not open, Mac security message | A |
| Lutron chip red or amber | B |
| Find my controller finds nothing | B2 |
| DMX in chip grey, red, or "waiting" | C |
| Light ignores the console | C4 |
| Lights go dark when DMX starts | C5 |
| Slow, laggy lights, "held" number | D |
| Lights stuck after a heavy cue | D3 |
| Page shows offline / polling | F |
| Performance mode did not work or will not restore | G |
