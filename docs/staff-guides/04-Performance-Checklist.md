# 4. Performance Checklist

Print this page and tick the boxes as you go. It takes you from a closed Mac to a finished show, and back to normal.

| | |
|---|---|
| **Show / date** | |
| **Operator** | |
| **Mac** | |
| **App version** (Status tab > App > Version) | |
| **Art-Net universe** | |

> **Performance mode** is a schedule set (a saved group of Lutron schedules) that you create once. Activating it turns those schedules off for the show. Restoring it afterwards puts them back **exactly as they were**. A schedule that was already off before the show stays off.
>
> **Never leave the show without restoring.** Schedules stay off until you restore them.

---

## A. One-time setup (do once, not every show)

Do this on a quiet day with the person who runs the lighting system.

### A1. Create the Performance mode set

1. [ ] Open the control page and click the **Schedules** tab.
2. [ ] In **Schedule sets**, click **New set**.
3. [ ] In **Name**, type `Performance mode`.
4. [ ] For every schedule in the list, choose what the set does:
   - **Turn off while active**: for timed events that would interrupt a show (for example "lights fade at 9 pm", "dusk scene").
   - **Leave alone**: for everything else.
   - **Turn on while active**: rarely needed. Turns a schedule *on* during the show.
   - **Be careful with security, exterior and safety lighting.** Leave those alone unless you are certain.
5. [ ] Click **Save set**.
6. [ ] Check the new set appears with a line **Turns off: …** listing the right schedules.

> 📷 **Screenshot needed:** the schedule set editor with a few schedules set to "Turn off while active".

### A2. Test it once on a harmless schedule

*This has not been proved on the real equipment yet. Do not skip the test.*

1. [ ] Make a second set called `TEST` that turns off **one** harmless schedule.
2. [ ] Click **Activate** on `TEST`. Click **OK** in the question box.
3. [ ] The **Schedules** list shows that schedule as **disabled**. A top banner says **TEST ACTIVE**.
4. [ ] Click **Restore schedules**. The schedule shows **enabled** again (or whatever state it had before).
5. [ ] If anything is different, or a red card appears, stop and ask for help (Troubleshooting Guide, Section G).
6. [ ] Delete the `TEST` set when you are done (**Delete** button on the set).

### A3. Make a safety copy of the settings

1. [ ] **Settings > Export & import settings > Download settings file** (passwords box unticked).
2. [ ] Copy the file to a USB stick or a private folder.

---

## B. Before the show day (day before, or earlier)

1. [ ] Plug in the Mac's charger. Plan to run the show **on mains power**.
2. [ ] Stop the Mac sleeping. Open **System Settings** and look under **Battery** (or **Energy**) and **Lock Screen**. Set the options so the Mac does **not** sleep while plugged in, and the screen can turn off without putting the Mac to sleep. The names differ a little between macOS versions. *The app does not stop the Mac sleeping by itself, and a sleeping Mac stops the lights following the console.*
3. [ ] Turn on **Do Not Disturb** (or a Focus) so that no pop-ups cover the screen.
4. [ ] Close other programs that are not needed.
5. [ ] If possible, connect the Mac and the console / Art-Net box by **network cable**, not Wi-Fi.
6. [ ] Check for a new version: **Settings > Updates > Check now**. If there is one, update **now**, not on the day. Then re-test (Section A2). Do **not** update on show day unless you must.
7. [ ] Keep a copy of the settings file (A3) with you.

---

## C. Opening the app (show day)

1. [ ] Switch on the Lutron equipment and the console's network box. Check they are powered.
2. [ ] On the Mac, open **Finder > Applications**.
3. [ ] Double-click **Lutron To DMX Bridge**.
   - If the Mac shows a security message, follow guide 1, Part 3. (It should only happen on the very first run, or after an update.)
4. [ ] Wait up to 30 seconds. The **log window** and the **control page** (in your browser) appear.
   - The log window looks like a small window with scrolling text. **Minimise it** (yellow button). **Never close it.**
   - If the browser does not open, click **Open web GUI** in the log window.
5. [ ] Keep the browser tab open for the whole show. Put it in **full screen** if you like (green button).

> 📷 **Screenshot needed:** the Applications folder with the app, and the log window next to the control page.

---

## D. Pre-show checks (aim for 30 minutes before doors)

### D1. Check the chips

Look at the five chips at the top right. All must be green or sensible.

1. [ ] **Lutron** is green and shows a time in ms (for example `45 ms`). It is not amber (slow) and not red (offline).
2. [ ] **Server** says **live**.
3. [ ] **Alerts** says `none`. If it shows warnings, open **Status** and click **Clear alerts** so that any new alert during the show is a real one.
4. [ ] **Output** shows a small number and nothing "held".
5. [ ] **DMX in** shows `off` for now. It will turn green later (Section F).
   - **If it is already on** (the app remembers this setting between runs), make sure the console is ready, or switch it off now (**DMX** tab, untick **Enable DMX input**, **Save & apply**). While it is on, mapped lights the console is not driving are set to 0.

If a chip is wrong, use the Troubleshooting Guide (Section B for Lutron).

### D2. Check the lights respond

1. [ ] **Devices** tab: switch one harmless light on, then off. It responds in a second or two.
2. [ ] No red message bar at the top of the page.

### D3. Check no other schedule set is active

1. [ ] No coloured banner at the top says "… ACTIVE". If one does, find out who started it and why, before you go on.

### D4. Check the DMX settings

1. [ ] **DMX** tab: **Source** is *Art-Net*, and **Universe** matches the console's universe.
2. [ ] The mapping table lists only the lights the console should control. Mapped lights that the console does not drive will be set to **0**.
3. [ ] The warnings area under the settings is empty.
4. [ ] **Max updates / second, all zones** is at the value agreed for this venue (default 20; 10 if the lights have lagged before).
5. [ ] If you changed anything, click **Save & apply**.

---

## E. Start Performance mode

Do this **before** the audience arrives or before the first cue, in a quiet moment.

1. [ ] Click the **Schedules** tab.
2. [ ] In **Schedule sets**, find **Performance mode**. Read the line **Turns off: …**. Is it the list you expect?
3. [ ] Click **Activate**.
4. [ ] A question box lists the schedules that will be turned **OFF** (and any turned ON). Read it. Click **OK**.
5. [ ] Wait a few seconds. A green card appears: **Performance mode is active**, with a line like "5 schedules changed".
   - A **red** card means something did not go through. **Stop.** Read it. Do not start the show until you have checked each schedule in the list below and turned off the ones that should be off. See Troubleshooting Guide, Section G.
6. [ ] A banner at the top of every tab now says **Performance mode ACTIVE**.
7. [ ] Scroll down the **Schedules** list. Every schedule in the set shows **disabled**.
8. [ ] The **Status** tab, **App** section, shows the schedule set as active.

> 📷 **Screenshot needed:** the green "Performance mode is active" card and the top banner.

---

## F. Switch on DMX input

Do this only when the console is powered, outputting Art-Net, and the right cue (or a blackout you want) is ready. **While DMX input is on, every mapped light follows the console, and a mapped light that the console is not driving is set to 0.**

1. [ ] Console is on and **Art-Net output is enabled**.
2. [ ] Click the **DMX** tab.
3. [ ] Tick **Enable DMX input**.
4. [ ] Click **Save & apply**.
5. [ ] The **DMX in** chip turns green and shows frames per second and the universe, for example `44 fps · U0`.
6. [ ] **Live monitor**: move a console fader and watch the matching box move.
7. [ ] Run a short, gentle check cue. The lights follow the console.
8. [ ] Return the console to its starting state.

If the chip says *waiting* or *no data*, use the Troubleshooting Guide, Section C.

---

## G. Last checks before the show starts

1. [ ] **Lutron** chip green, latency low.
2. [ ] **DMX in** chip green.
3. [ ] **Alerts** still `none`.
4. [ ] **Performance mode ACTIVE** banner showing.
5. [ ] Log window minimised, not closed. Mac plugged in. Do Not Disturb on.
6. [ ] Know who to call (see the contacts sheet in the START-HERE page).

---

## H. During the show

- **Do not** close the log window or quit the app.
- **Do not** start an update, even if the blue bar appears.
- **Do not** change settings.
- Glance at the chips now and then (see the table below). You only need to act if one changes colour.
- If lights start lagging, follow the Troubleshooting Guide, Section D. Short version:

| Sign | First action |
|---|---|
| **Output** shows "N held" for more than a few seconds, or lights are staggered | Ease off the cue speed. Fewer lights moving at once. |
| **Lutron** chip turns amber (slow) | Same. Click the chip. If *Commands in flight* keeps rising, stop the cue source. |
| **DMX in** chip turns amber ("no data") | Check the console output and the network cable. |
| **Lutron** chip turns red | Do not panic. Lights keep their last level. Check the network cable or Wi-Fi. Wait for it to reconnect by itself. |
| Page shows **Server: offline** | Check the log window is open. If the app has stopped, reopen it. Lights keep their last level. |

- **[PENDING, not in 0.5.0]** After the next update, the Status tab will have a **Clear Lutron queue** button for a backed-up system.
- If the lights are badly behind or stuck, follow *Troubleshooting Guide, Section D3 (after a DMX storm)*. Do not power-cycle the repeater without asking the person who runs the lighting system.

---

## I. After the show

Do these in order. **Do not skip step 3.**

### I1. Stop the console driving the lights

1. [ ] Put the console into the state you want the room left in (for example a blackout, or house lights).
2. [ ] Click the **DMX** tab.
3. [ ] **Untick Enable DMX input** and click **Save & apply**.
4. [ ] The **DMX in** chip turns grey and says `off`.
5. [ ] Lights now stay as they are. You can set them from the **Devices** tab, or from the wall keypads.

### I2. End Performance mode

1. [ ] Click the **Schedules** tab, or the **Restore schedules** button in the banner at the top.
2. [ ] Click **Restore schedules** on **Performance mode**.
3. [ ] Wait a few seconds. A green card says **Performance mode restored**, and "N schedules put back to how they were".
   - A red card says **Some schedules are still not restored**: click **Restore schedules** again. Keep going until it works.
4. [ ] The top banner has gone.
5. [ ] In the **Schedules** list, schedules that were running before the show show **enabled** again. Schedules that were already off before the show are still **disabled**.
6. [ ] **Status** tab, **App** section, shows the schedule set as `None active`.

> 📷 **Screenshot needed:** the green "restored" card.

### I3. Leave the building how you found it

1. [ ] On the **Devices** tab, check key rooms are in the state you want (use **All off** per room if needed).
2. [ ] If the lighting system should run its normal timed events tonight (security, exterior lights), now that schedules are back, check the next scheduled event is one you expect.

### I4. Save a note for next time

1. [ ] Look at the **Status** tab. Note anything unusual: **Alerts** count, highest **Lutron latency** (in the graph), **Reconnects since start**.
2. [ ] Write it on this sheet or tell the person who runs the app.
3. [ ] Click **Clear alerts** if you like, so next time starts fresh.

### I5. Shut down

1. [ ] Only quit the app if nobody else needs it. If the Mac is staying on, leaving the app running is fine.
2. [ ] To quit: open the log window and click **Quit**. Confirm **OK**. (Quitting stops DMX control. The lights stay as they are.)
3. [ ] Close the browser tab.
4. [ ] Turn the Mac's sleep and Do Not Disturb settings back, if you changed them.

---

## J. If the app or the Mac stops during the show

1. **The lights stay as they are.** Nothing turns off by itself.
2. Reopen the app (Section C).
3. If the banner says **Performance mode ACTIVE**, the schedules are still off. Leave it that way until the show finishes, then restore as in I2.
4. The app remembers whether DMX input was on, so it may start receiving by itself when it reopens. Check the **DMX in** chip. If the console is not ready, untick **Enable DMX input** and **Save & apply** (mapped lights the console is not driving are set to 0). Switch it on again (Section F) when the console is ready.
5. If you lose the app completely, schedules can also be switched back on by hand in the **Schedules** list, or in the Lutron system itself. Ask the person who runs the lighting system.

---

## K. One-page summary (cut out and keep by the Mac)

**Open:** Applications > Lutron To DMX Bridge. Minimise (never close) the log window.

**Check:** Lutron green · Server live · Alerts none · DMX in off.

**Start:** Schedules > Performance mode > **Activate** > OK > banner shows ACTIVE.

**Console on:** DMX > tick **Enable DMX input** > **Save & apply** > DMX in green.

**Show runs:** watch the chips, do not touch anything.

**Finish:** DMX > untick **Enable DMX input** > **Save & apply**.

**Restore:** Schedules > **Restore schedules** > banner gone.

**Shut down:** log window > **Quit**.

**Lag?** Ease the cue. Lower **Max updates / second, all zones** to 10. See Troubleshooting Guide, Section D.
