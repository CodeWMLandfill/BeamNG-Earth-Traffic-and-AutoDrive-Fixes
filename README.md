# BeamNG.Earth - Traffic and AutoDrive fixes

Fixes highway traffic spawning, lane keeping past exit ramps and merges, long-distance AutoDrive stops, passing, stopped-car avoidance, and live driving controls in **BeamNG.Earth**, and includes an optional **`Ctrl+Shift+T`** hotkey to report bad traffic or AutoDrive behavior while driving without pausing.

> **Work in Progress - Accelerated by Community Reports:** This add-on **does not fix every traffic or AutoDrive issue in the game**. Because **BeamNG.Earth** streams roads from all over the world, edge cases are fixed continuously as players report them. When AutoDrive or traffic acts up, press **`Ctrl+Shift+T`** while driving to send a quick report so that pattern can be fixed in a future update.

## What This Fixes

- **Smoother Highway Traffic Spawns:** Spawns oncoming and ahead highway traffic farther down the road already rolling at road speed instead of popping in stopped or skidding right in front of you.
- **Cleaner Highway Merges & Lane Lines:** Keeps cars centered in their lanes through highway merges and removes tiny broken road stubs that made AI cars swerve or hesitate.
- **Stays on the Highway Past Exits & Overpasses:** Stops AutoDrive and GPS routing from accidentally jumping onto narrow exit ramps or overpasses crossing above the highway.
- **Uninterrupted Long-Distance AutoDrive:** Fixes AutoDrive dropping its route or braking every few hundred meters on long drives, and recovers cleanly after pausing or crossing map tile borders.
- **Live Driving Controls & Race Mode:** Speed, speed-limit mode, passing, aggression, and awareness sliders take effect right away while driving without resetting your route.
- **Safer Passing & Stopped-Car Avoidance:** Lets AutoDrive pass slower traffic when clear and steer around crashed or stopped cars blocking a lane.

## Works Alongside Bridge, Rail & Tunnel Fixes

You can use **Traffic and AutoDrive fixes** on its own or together with [BeamNG.Earth - Bridge, Rail & Tunnel Fixes](https://github.com/CodeWMLandfill/BeamNG-Earth-Bridge-Rail-Fixes):

- Both add-ons can be installed, updated, or uninstalled in any order without overwriting each other.
- **`Ctrl+Shift+T`** reports a traffic or AutoDrive issue; **`Ctrl+Shift+B`** reports a broken bridge, railway, or tunnel.

## Install - Option 1 (1-Click ZIP)

1. Click the green **Code** button above -> **Download ZIP** (or [download the ZIP directly](https://github.com/CodeWMLandfill/BeamNG-Earth-Traffic-and-AutoDrive-Fixes/archive/refs/heads/main.zip)).
2. Right-click the `.zip` file, click **Extract All...**, and open the extracted folder.
3. Double-click **`1_Click_Install.bat`**.
   - It automatically finds **BeamNG.Earth** on your PC and offers to close and restart it for you if it is running.

## Install - Option 2 (Inside BeamNG.Earth)

1. Open **BeamNG.Earth** -> **Add-ons** -> **Add from GitHub**.
2. Enter `CodeWMLandfill/BeamNG-Earth-Traffic-and-AutoDrive-Fixes` and click **Install**.
3. Click **Start** on the add-on card, then close and restart **BeamNG.Earth**.

*(Tip: You can also download `beamng_earth_autodrive_fixes.py` from the [Releases](https://github.com/CodeWMLandfill/BeamNG-Earth-Traffic-and-AutoDrive-Fixes/releases) tab and double-click it in File Explorer to launch the 1-click setup.)*

## Uninstall

- **From the ZIP folder:** Double-click **`1_Click_Uninstall.bat`** to restore the original **BeamNG.Earth** in one click.
- **Inside BeamNG.Earth:** Open **Add-ons** -> **Options**, choose **`Restore original app`**, click **Save**, click **Start**, and restart **BeamNG.Earth**.

## Report a Traffic or AutoDrive Issue (`Ctrl+Shift+T`)

Press **`Ctrl+Shift+T`** anytime while driving to silently send a quick screenshot, map location, and anonymous nearby-traffic snapshot without pausing the game. When a spot you reported is fixed in a later update, a quiet one-time corner notice lets you know at launch. You can change the default category (`autodrive`, `traffic`, `overtake`, `routing`, `other`), turn fix or update notices off, or disable the hotkey in **Add-ons -> Options**.

## Privacy

- No login, name, account, or personal info is collected.
- No PC usernames, folder paths, or IP addresses are stored.
- Hidden photo details (EXIF/metadata) are stripped before upload.

## Requirements

- **Windows 10 or 11** (64-bit).
- **BeamNG.Earth v1.9.1-beta.dev.424** through **v1.10.1-beta** and **v1.10.1-beta.dev.574** (older versions are skipped safely with a clear notice).
