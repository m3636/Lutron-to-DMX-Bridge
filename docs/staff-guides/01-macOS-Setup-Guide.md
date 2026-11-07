# 1. macOS Setup Guide

How to download, install and open Lutron To DMX Bridge on a Mac, and connect it to the Lutron system and to your lighting console.

**Time needed:** about 15 minutes the first time. After that you only double-click the app.

---

## Before you start

You need:

1. **A Mac with an Apple chip (M1 or newer).** To check: click the Apple menu (top-left), choose **About This Mac**, and look for "Apple" next to **Chip**. The app does not run on older Intel Macs.
2. **The Mac on the same network as the Lutron system.** Wi-Fi works, but a network cable is more dependable for a show.
3. **For a RadioRA 2 system: the integration user name and password.** The person who runs the lighting system has them.
4. **A lighting console that can send Art-Net** (or a DMX-to-Art-Net box). You need to know which Art-Net **universe** it sends.

---

## Part 1: Download the app

1. Open your web browser and go to:
   **https://github.com/m3636/Lutron-to-DMX-Bridge/releases**
2. Find the newest release at the top of the page. The latest at the time of writing is **0.5.0**.
3. Scroll to **Assets** and click the file ending in **-macOS.zip**, for example `LutronToDMXBridge-0.5.0-macOS.zip`.
   - Do **not** pick the `Windows` file or `SHA256SUMS.txt`.
   - If you see "404" or "Page not found", ask the person who shared this app to give you access.
4. The file goes to your **Downloads** folder.

> 📷 **Screenshot needed:** the Releases page with the "-macOS.zip" file circled.

## Part 2: Install the app

1. Open the **Downloads** folder (click **Downloads** in the Finder sidebar).
2. Double-click the `.zip` file. An item called **Lutron To DMX Bridge** appears next to it. Safari may already have done this for you.
3. Open a second Finder window and click **Applications** in the sidebar.
4. **Drag** *Lutron To DMX Bridge* from Downloads into the Applications window.
   - This step matters. If you run the app from Downloads, it cannot update itself later.
5. You can now delete the `.zip` file from Downloads.

> 📷 **Screenshot needed:** the app being dragged into Applications.

## Part 3: Open it for the first time (Mac security steps)

Your Mac will block the app the first time. The app is safe, but it is not signed with an Apple developer account, so macOS (Gatekeeper) is cautious. You only do this once. It sometimes takes two or three tries. That is normal.

### Steps

1. In **Applications**, double-click **Lutron To DMX Bridge**.
2. A message says the app **cannot be opened**, or that **Apple could not verify** it.
   - Click **Done**.
   - **Do not click "Move to Trash".**
3. Click the Apple menu (top-left) and choose **System Settings**.
4. In the left sidebar, click **Privacy & Security**.
5. Scroll down to the **Security** section. You will see a line like: **"Lutron To DMX Bridge" was blocked to protect your Mac.** Click **Open Anyway**.
6. Type your Mac password (or use Touch ID) when asked.
7. Go back to **Applications** and double-click **Lutron To DMX Bridge** again.
8. A new message asks if you are sure. Click **Open Anyway** (on some versions the button says **Open**).
9. Wait a few seconds. Two things appear:
   - a small **log window** with scrolling text, and
   - your web browser, showing the **control page**.

> 📷 **Screenshot needed:** (a) the "cannot be opened" message, (b) System Settings > Privacy & Security with the "Open Anyway" button, (c) the final "Open Anyway" prompt.

### If it does not work

| What you see | What to do |
|---|---|
| The same "cannot be opened" message keeps coming back | Normal. Double-click the app again, and go through steps 3 to 8 once more. |
| There is no **Open Anyway** button in System Settings | Double-click the app once more, then look in **Privacy & Security** again. The button only shows for about an hour after the last blocked attempt. |
| Your Mac says the app is **damaged** | Delete the app from Applications, download the zip again and repeat Parts 1 to 3. |
| Nothing at all happens | Wait 30 seconds. The first start is slow. Then try again. |

### Other ways, only if the steps above fail

**Right-click method.** Hold **Control** and click the app (or right-click it). Choose **Open**, then click **Open** in the message. On macOS 15 (Sequoia) and later this often does not work. Use the System Settings steps instead.

**Terminal method (last resort).** Only do this if someone technical agrees. It removes the "downloaded from the internet" mark from the app:

1. Open **Terminal** (press Command + Space, type *Terminal*, press Return).
2. Paste this line and press Return:

   ```
   xattr -dr com.apple.quarantine "/Applications/Lutron To DMX Bridge.app"
   ```

3. Close Terminal and double-click the app again.

> **Note:** The exact wording of these messages changes between macOS versions. If yours is not word for word the same, look for the nearest match. The idea is always: tell the Mac you trust this app.

### The "local network" question

The first time the app runs, your Mac may ask whether it can **find and connect to devices on your local network**. Click **Allow**.

If you click **Don't Allow**, the app cannot find the Lutron system or receive Art-Net. To fix it later: **System Settings > Privacy & Security > Local Network**, then switch on **Lutron To DMX Bridge**. Quit and reopen the app afterwards.

## Part 4: Connect to the Lutron system (first run only)

The first time, the control page opens on the **Settings** tab. If your browser did not open, click **Open web GUI** in the log window.

> 📷 **Screenshot needed:** the Settings tab, "Lutron system" card.

1. Under **Lutron system**, open **System type** and choose:
   - **RadioRA 2 (Main Repeater, telnet integration)**: the classic system with a "Main Repeater" box.
   - **RadioRA 3 / RA2 Select / Caseta (LEAP)**: newer systems with a "processor" or "Smart Bridge".
   - Not sure? Ask the person who runs the lighting system.
   - **Simulator (demo, no hardware)** is a pretend house for practice. It never touches real lights.
2. Click **Find my controller**.
   - For RadioRA 2, first tick **Also scan the whole network (slower; needed for RadioRA 2)**. Then click **Find my controller**. Be patient. It can take a minute.
3. When your controller appears in the list, click **Use this one**. The address fills in.
   - If nothing is found, check the Mac is on the same network, then try again.
   - If it still fails, type the controller's address (it looks like `192.168.1.50`) into **Host / IP address**.
4. Enter the login:
   - **RadioRA 2:** type the **Integration user name** and **Integration password**.
   - **RadioRA 3 / RA2 Select / Caseta:** click **Pair**. Within about 3 minutes, press the small button on the back of the Lutron processor or bridge. Come back and wait for the word *Paired*.
5. Click **Save & reconnect**. A message at the bottom says *Connected* when it works.
6. Click the **Devices** tab. Your rooms and lights should appear.

You do not need to do this again. The app remembers.

> **Light names show as numbers (RadioRA 2)?** The app needs the system's "Integration Report" to show names. In **Settings**, the field **Integration Report path (optional)** takes the location of that file. Ask the person who set up the system for it.

> **Slow first start (RadioRA 2)?** On RadioRA 2 the app downloads a file of light names from the repeater. This can be slow. Version 0.5.0 waits longer, retries, and keeps a copy for next time. If the first start fails, wait a minute and click **Save & reconnect** again.

## Part 5: Connect the lighting console (DMX / Art-Net input)

Skip this part if you do not use a lighting console. **Ask whoever patched the console before changing anything here.**

> **Read this first.** While DMX input is on, every light you have *mapped* follows the console. A mapped light that the console is not controlling is set to **0** (off). Map only the lights you want the console to own, and switch DMX input on only when the console is ready.

1. Click the **DMX** tab.
2. Under **DMX input**, set:
   - **Source:** *Art-Net (UDP 6454)*.
   - **Universe:** the Art-Net universe your console sends. **Art-Net universes start at 0.** (sACN universes start at 1. Some consoles show Art-Net universes starting at 1, so if nothing arrives, try one lower.)
   - **Listen on IP:** leave as `0.0.0.0` (all network connections).
   - **Max updates / second / zone:** leave the default (5) unless told otherwise.
   - **Max updates / second, all zones:** leave the default (20). Lower it to 10 if the lights lag (see the Troubleshooting Guide).
3. Under **Channel mapping**, decide which light listens to which DMX channel:
   - Click **Auto-assign unmapped zones** to give every unmapped light the next free channels, starting at the **Start** number. Choose **Params** first (for example *intensity (1 ch)*).
   - Or click **Add row** to add one light by hand, and set its **Address**.
   - Untick the **On** box on a row to ignore that light.
4. Read any warnings shown under the settings (for example "channel overlaps"). Fix them.
5. Click **Save & apply** (top right of the DMX tab).
6. To switch the input on, tick **Enable DMX input**, then click **Save & apply**.
7. Start the console sending Art-Net. Look at the **DMX in** chip at the top right. It should turn green and show something like `44 fps · U0`.
8. Watch the **Live monitor** at the bottom of the DMX tab. The little boxes should move when you move a fader on the console.

> 📷 **Screenshot needed:** the DMX tab with the DMX in chip green and the Live monitor moving.

> **The app remembers this setting.** If DMX input was on when you closed the app, it switches itself on again next time the app opens.

> **Which Mac port is used?** The app listens on UDP port **6454** (Art-Net). Nothing else on the Mac should need that port.

## Part 6: Everyday use

| Tab | What it is for |
|---|---|
| **Devices** | Every room and light. Use the switch to turn on or off and the slider to dim. **All on** / **All off** do a whole room. |
| **Scenes** | Your own saved looks. Click **Activate** to set many lights at once. These belong to this app, not to Lutron. |
| **Schedules** | **Schedule sets** (including Performance mode) at the top, and the list of Lutron schedules below. |
| **DMX** | Console input, channel mapping and the live monitor. |
| **Status** | Health of the whole system, graphs, and the **Clear alerts** button. |
| **Settings** | Lutron connection, updates, **Export & import settings**, Lutron backup. |

**Starting the app:** double-click **Lutron To DMX Bridge** in Applications. If it is already running, double-clicking just reopens the control page.

**Stopping the app:** click **Quit** in the log window (or close that window) and confirm. Quitting stops DMX control. **The lights stay as they are.**

**Keeping it running:** *minimise* the log window (the yellow button). Do not close it.

**Updating:** a blue bar at the top of the control page says when a newer version is out. Click **Update now** at a time that suits you (never in the middle of a show). Your settings and scenes are kept. If a schedule set is active or DMX input is live, the app asks you to confirm first.

## Part 7: Your first-day checks

Do these once, with the person who runs the lighting system, **before the first show**:

1. [ ] **Devices** tab: toggle one harmless light on and off. It follows.
2. [ ] **Schedules** tab: the schedule list matches what the lighting system has.
3. [ ] Make a **Performance mode** schedule set (see the Performance Checklist, section A).
4. [ ] Test Performance mode on **one harmless schedule**: activate it, check that the schedule shows *disabled*, restore it, and check it shows *enabled* again. *This has not yet been tried on the real equipment, so do not skip it.*
5. [ ] **DMX** tab: move a console fader and see a light follow.
6. [ ] **Settings > Export & import settings > Download settings file**, and keep the file safe (see guide 2).

---

*Problems? See guide 3, the Troubleshooting Guide.*
