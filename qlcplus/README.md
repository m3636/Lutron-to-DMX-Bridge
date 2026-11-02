# QLC+ test show

A [QLC+](https://www.qlcplus.org/) workspace that plays the part of a real lighting desk: it patches the simulator's 14 Lutron zones as dimmers, sends them to the app over **Art-Net universe 0**, and has scenes, chases and faders to drive them.

Built and checked against QLC+ 4.12.7 (the current stable 4.x). Needs no fixture files, only QLC+'s built-in generic dimmers.

## Run it on one computer (QLC+ and the app together)

1. Put the app's test config in place. In the GUI instead, see "Without the config file" below.

   | | |
   |---|---|
   | macOS / Linux | `mkdir -p ~/lutron-test && cp qlcplus/app-home/config.json ~/lutron-test/` |
   | Windows (PowerShell) | `mkdir $HOME\lutron-test; copy qlcplus\app-home\config.json $HOME\lutron-test\` |

2. Start the app against the simulator, pointing it at that folder, so your real config is untouched:

   ```
   # macOS / Linux
   LUTRON_TO_DMX_BRIDGE_HOME=~/lutron-test lutron-to-dmx-bridge --simulator
   # Windows (PowerShell)
   $env:LUTRON_TO_DMX_BRIDGE_HOME = "$HOME\lutron-test"; lutron-to-dmx-bridge --simulator
   ```

3. In QLC+: **File > Open workspace**, pick `qlcplus/Lutron-To-DMX-Bridge-Test.qxw`, then press the **Operate mode** button (or `Ctrl+R`). Open the **Virtual Console** tab.
4. Move a fader or press a scene button. In the app's browser GUI (Devices tab) the same zones move, and the **DMX** tab shows packets, FPS and the sender (`127.0.0.1`).

If nothing moves, check QLC+'s **Inputs/Outputs** tab: Universe 1 should have **ArtNet** with the `127.0.0.1` line ticked in the Output column. The file asks for that line by IP, so it should match on any computer, but a QLC+ that was already running with other settings may have kept its own.

### Without the config file

Start `lutron-to-dmx-bridge --simulator`, then the **DMX** tab: tick Enable DMX input, source Art-Net, universe 0, **Listen on IP** `127.0.0.1`, press **Re-assign all** with params `intensity` and start 1, then **Save & apply**. That is exactly what the config file holds.

## Run it on two computers (the realistic setup)

QLC+ on one computer, the app on another, same LAN. Generate a workspace and config for your addresses:

```
python qlcplus/generate_show.py --interface <QLC+ computer's LAN IP> --target <app computer's IP>
```

Leave out `--target` to broadcast instead of unicast. Files land in `qlcplus/custom/`. Use `custom/app-home/config.json` on the app computer (it listens on all interfaces; allow UDP 6454 through the firewall on first run) and open `custom/Lutron-To-DMX-Bridge-Test.qxw` in QLC+. On the QLC+ computer the `--interface` address must be one of its own, as listed in QLC+'s Inputs/Outputs tab.

## What is in the show

**Patch** (Art-Net universe 0, QLC+ "Universe 1"), in the order the app's **Re-assign all** produces:

| DMX | Zone | Area |
|---|---|---|
| 1-2 | Exterior Uplights, All Step Lights | Exterior |
| 3-6 | Stair Pendants, Stair Accent, Stair Art Wall, Stair Recessed | Main Stairs |
| 7-13 | Living East / Bar / Center / West Recessed, Floor Lamps, Fan Light, Porch Recessed | Living Room |
| 14 | Sitting Room Lights | Sitting Room |

**Virtual Console**: one fader per zone (grouped by area, shown in %), scene buttons named after the real keypad buttons (Entertain, Relax, Bright, Porch, Stairs On/Dim/Accent/Night, Reading, ...), four chases, **Stop All** (fades out over 1.5 s) and **Blackout**.

**Chases** (what a busy desk does to the app):

| Chase | Why |
|---|---|
| Zone Walk | one zone at a time, 0.4 s fades: many small steps across all zones |
| Evening Cycle | slow 4 s mood changes in the living room |
| Ramp 10s | whole house 0-100-0 over 10 s each way: the app's rate limiting (5 commands/s per zone by default) |
| Random Flicker | 100 ms random steps: worst case for the command rate |

## A show for your own system

`generate_show.py --db-xml` builds a QLC+ workspace from your own RadioRA 2 export instead of the simulator's 14 zones. The Main Repeater serves the export at `http://<repeater-ip>/DbXmlInfo.xml`; save it somewhere outside the repo, as it contains room names and installer contact details.

```
python qlcplus/generate_show.py --db-xml ~/Downloads/DbXmlInfo.xml
```

It writes `Lutron-To-DMX-Bridge-Show.qxw` and `patch-list.csv` into `qlcplus/custom/`, which is git-ignored. The same `--interface` / `--target` options apply as above (loopback by default).

- **Patch**: one 1-channel fixture per output, in the order the app's **Re-assign all** uses (rooms in export order, outputs in export order within a room), DMX 1 upwards. The generator reads the export with the app's own parser, so ids and order cannot drift. `patch-list.csv` lists address, integration id, room, name and type for checking the DMX tab against.
- **Virtual Console**: a frame per room with a named fader per dimmer (caption starts with its DMX address). Switched outputs, which are on/off, get a button instead of a fader. Every room with more than one output has an **All On** button. Toggling a button off releases it, and the app then sends 0 for those zones, so that is also the room's "all off".
- **House** buttons: All On, Dimmers 50%, Dimmers 25%, Exterior On. **Test chases**: Zone Walk (one dimmer at a time) and Ramp 10s (all dimmers 0-100-0). Both skip switched outputs. **Stop All** and **Blackout** in Master.

Add `--room Kitchen` (the room name, with or without its number) for a show with just that room, patched from DMX 1 and written as `Lutron-To-DMX-Bridge-Show-Kitchen.qxw`. Map only those zones in the app: any other mapped zone is told 0 by the universe.

To use it, point the app at the real system and, in its DMX tab, enable DMX input (Art-Net, universe 0, **Listen on IP** `127.0.0.1` when QLC+ runs on the same computer), set parameters `intensity`, start 1, press **Re-assign all** and **Save & apply**. Remember that the DMX universe is in charge: once QLC+ is running in Operate mode, every mapped light follows its channel, so lights nothing is driving go to 0.

## Things to know

- **The DMX universe is in charge.** QLC+ sends all 512 channels, so every mapped zone follows DMX, and a zone no scene touches is told 0. This is the app's design.
- QLC+ merges faders and scenes **highest-takes-precedence**, so a scene cannot pull a zone below a fader that is higher. Use **Stop All** or lower the fader to turn things down.
- "All Step Lights" is a switch in the simulator, so it jumps on or off rather than dimming.
- Same computer, nothing arriving? QLC+ and the app both own UDP 6454. The shipped config pins the app to `127.0.0.1` so datagrams to loopback reach it whichever starts first (checked on Linux; Windows and macOS were not available when this was written).

## Regenerating

`python qlcplus/generate_show.py` rebuilds both files from the simulator inventory. A test (`tests/test_qlcplus_show.py`) fails if the committed files drift from it.
