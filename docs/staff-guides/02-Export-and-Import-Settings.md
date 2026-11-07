# 2. Export and Import Settings

How to save the app's settings to a file, and how to load them back. Use this to:

- keep a safety copy before a big show or an update,
- move the app to a different Mac,
- recover if the settings were lost or broken.

**Available in:** version 0.5.0 and later. **Not yet tried on a real Mac with the real Lutron system.** Try it once on a quiet day before you rely on it.

---

## What gets saved (and what does not)

**Saved in the settings file:**

- the Lutron connection (system type, address, port),
- the DMX input settings and the light-to-channel mapping,
- your own scenes,
- your schedule sets, such as Performance mode (and the remembered schedule states, if a set was active when you exported),
- the update preference, and the control page's web address and port.

**Not saved:**

- the lights, rooms and schedules inside the Lutron system itself (those live in the Lutron equipment; use **Settings > Backup** for a reference copy),
- the app itself,
- log files.

> **Two different things.**
> **Export & import settings** saves *this app's* setup.
> **Backup** (just below it on the Settings tab) saves what the *Lutron system* told the app, as a reference. It contains the installer's name and contact details. Keep it private.

---

## Exporting your settings

1. Open the control page and click **Settings**.
2. Scroll to the card called **Export & import settings**.
3. Decide about the passwords box (see "The credentials box" below). **If unsure, leave it unticked.**
4. Click **Download settings file**.
5. Your browser saves a file named like:
   `lutron-to-dmx-bridge-config-20261107-183000.json`
   It goes to your **Downloads** folder, unless your browser asks where to save.
6. Move the file somewhere safe (a USB stick or a private folder). Do not leave it only in Downloads.

> 📷 **Screenshot needed:** the "Export & import settings" card.

> **Where do the files go?** The *Download settings file* button saves only to the place your **browser** downloads files. The app does not keep its own copy of an export. Its own `backups` folder is described below.

### The credentials box

The box says **"Include the Lutron integration login, update token and pairing certificates"**.

| Box | What is in the file | When to use it |
|---|---|---|
| **Unticked** (default) | No passwords or keys. | Normal safety copies. If you import this file later, the app keeps the login it already has. |
| **Ticked** | Includes the Lutron integration user name and password, and any pairing certificates, **in plain text**. | Only when setting up a brand-new Mac so it can connect straight away without asking for the login again. |

If you tick the box, a yellow warning appears. It means:

- **Anyone who has the file can control the lights over the network.**
- Do **not** email it, post it in chat, or attach it to a report.
- Move it with AirDrop or a USB stick, and **delete it when you are done**.

---

## Importing settings

Use this on a new Mac, after a reinstall, or to go back to a saved copy.

1. Make sure the app is installed and running (guide 1). The Lutron connection does not need to work yet.
2. Click **Settings**, and scroll to **Export & import settings**.
3. Click **Import from file…**
4. Choose your settings file (a `.json` file).
5. A panel opens titled **Import <your file name>**. It tells you:
   - which version of the app made the file, and when,
   - whether the login is **included in the file** or **not in the file** (the ones the app has now are kept),
   - a list of **sections**, each with a tick box: the Lutron connection, DMX, scenes, schedule sets and so on. Each section lists what would change, or says *no changes*.
6. Read any yellow warnings. Common ones:
   - a scene uses a light the connected Lutron system does not have,
   - a file path (for example the Integration Report) does not exist on this Mac.
7. Leave the default ticks unless you have a reason. Untick any section you do not want.
8. Click **Import selected**.
9. A green message says **Settings imported**. It lists what was imported and gives the path of an automatic backup of your *previous* settings.
10. Click **Close**. The app reconnects to the Lutron system by itself. Check that the **Lutron** chip turns green.

> 📷 **Screenshot needed:** the import panel with sections and the "Import selected" button.

### Rules to know before you import

- **A ticked section is replaced as a whole.** Anything in that section that is not in the file is removed. For example, importing "Scenes" replaces all your scenes with the scenes in the file.
- **The control page's web address and port are not ticked by default.** They decide who can reach the app. Leave them alone unless a technical person asks you to import them.
- **Schedule sets can be blocked.** If a schedule set (for example Performance mode) is **active right now**, and it is not in the file, the *Schedule sets* section cannot be imported. Go to the **Schedules** tab, click **Restore schedules**, and try again. Or untick that section.
- **Bringing back a remembered schedule state.** If the file was made while a set was active, a tick box offers to bring back its saved schedule states. Only tick it if you know why. It is only available when no set is active now.
- **Files from other versions.**
  - A file from a *newer, incompatible* format is refused. The message tells you to update the app.
  - A file from an *older* format is upgraded automatically.
  - A file from a newer app version is accepted with a warning. Settings this version does not understand are ignored.
- **Old Lutron backup files** (from *Download backup*) can also be imported, but only the app's own settings are used, not the password.

---

## The automatic safety copy

Every time you import, the app first saves your *current* settings to a file named like:

`config-before-import-20261107-183500.json`

Where: `~/Library/Application Support/LutronToDMXBridge/backups/`

**This file includes your passwords** (it is a full copy). It can only be read by your Mac user.

**To undo an import:** import that `config-before-import-…` file using the steps above.

**How to open that folder on a Mac**

1. In the Finder, click **Go** in the menu bar, then **Go to Folder…** (or press **Shift + Command + G**).
2. Paste: `~/Library/Application Support/LutronToDMXBridge/backups`
3. Press Return.

Other things in the `LutronToDMXBridge` folder: `config.json` (the live settings), `logs/` (log files), and Lutron backups made with the *Download backup* button (`lutron-backup-…json`).

---

## Recipes

### A. Safety copy before a show or an update

1. **Settings > Export & import settings**, leave the passwords box unticked, click **Download settings file**.
2. Copy the file to a USB stick or a private cloud folder you trust.
3. You are done. Nothing changes in the app.

### B. Move the app to a different Mac

**On the old Mac**

1. Open **Settings > Export & import settings**.
2. Tick **Include the Lutron integration login, update token and pairing certificates** (if you want the new Mac to connect without asking again).
3. Click **Download settings file**.
4. Copy the file to the new Mac by AirDrop or USB stick. **Do not email it.**
5. Delete the file from the old Mac's Downloads folder and from any USB stick afterwards.

**On the new Mac**

1. Do guide 1, Parts 1 to 3 (download, install, open the first time).
2. You can skip Part 4 (connecting to Lutron). The import fills in the connection.
3. Click **Settings > Export & import settings > Import from file…**, pick the file and click **Import selected**.
4. Read the warnings. Fix anything marked as a missing path (for example, point **Integration Report path** at a file that exists on this Mac).
5. Check the **Lutron** chip is green. Open **Devices** and **Schedules** to confirm.
6. **Delete the settings file** from the new Mac's Downloads folder.

### C. Recover after the settings were lost

1. Install the app (guide 1, Parts 1 to 3) if needed.
2. Import your last settings file (recipe B, new Mac steps).
3. If you have no file, set the app up again from guide 1, Part 4 onwards.

---

## If something goes wrong

| What you see | What to do |
|---|---|
| "Cannot read <file>. It is not valid JSON." | The file is damaged or is not a settings file. Use another copy. |
| "That file is too big to be a settings export." | You picked the wrong file. A settings file is small. |
| "Cannot import <file>" with a message about a newer format | Update the app (blue bar > **Update now**), then try again. |
| The *Schedule sets* section is greyed out with a reason | A schedule set is active. Restore it first (Schedules tab), or leave that section out. |
| After import, the **Lutron** chip is red | The login may not have been in the file. Go to **Settings > Lutron system**, type the user name and password, and click **Save & reconnect**. For RadioRA 3 / Select / Caseta, you may need to **Pair** again. |
| A warning says a path does not exist | Normal on another Mac. Fix the path in **Settings** (for example the Integration Report). |
| You imported by mistake | Import the `config-before-import-…` file from the `backups` folder (see above). |

---

## Staying safe with these files

- A settings file made **without** the passwords box is fairly harmless, but still keep it private. It contains your network addresses.
- A settings file made **with** the passwords box, and every `config-before-import-…` file, contain passwords. Treat them like a password list.
- **Never attach any of these files, or `config.json`, to a bug report.**
- If you must share an example, share a screenshot of the Status tab instead.
