# Changelog

All notable changes to **BeamNG.Earth - Traffic and AutoDrive fixes** are documented in this file.

## [0.1.4] - 2026-11-27

### Fixed
- **Camera-Cone In-View & Car-Following Despawn Protection:** Runs the sight shield every frame using the camera's horizontal FOV cone, an `85 m` surround proximity bubble, and a `2.5 s` camera-glance grace window so traffic you are looking at or following stays loaded instead of fading out on curves, crests, camera turns, or streaming hitches; verifies road-connected forward runway before cut-recycling so adjacent clipped side streets no longer yank vehicles ahead on continuous roads.
- **Town-Speed Respawns, Ghost-Cloak Prevention & EV Launch Drive Sync:** Applies the `360 m` ahead-uncloak floor only at highway speeds instead of recycling town-speed spawns behind the camera, avoids cloaking initial spawns or leaving conflict-recycled vehicles stranded invisible in `preRoll` (with automatic orphan uncloak recovery), and restores Drive mode on `electricMotor` vehicles alongside DCT dual-clutch and automatic gearboxes after rolling launches.
- **Undivided Two-Lane Highway Horizon & Oncoming Spawns:** Walks curving two-lane highways by local road tangent continuity, requires verified forward highway runway (`>= 200 m` ahead, `>= 220 m` oncoming) clear of dead-end graph cuts, and places oncoming traffic cleanly on undivided two-lane highways without falling back to short behind-camera teleports that triggered mid-highway U-turns.
- **Pooled Respawn AI & Post-Crash Role Reset Before Cloaking:** Runs `veh:onRefresh()` and clears stale `follow`/`chase` post-crash role states and `respawnSpeed` before `cloakPreRoll()` sets `state = 'preRoll'`, preventing recycled traffic vehicles from retaining stale routes, reversing on spawn, or chasing the player across respawns.
- **DCT Dual-Clutch & Automatic ShiftLogic Drive Sync + Cloaked Reverse Guard:** Synchronizes `dctGearbox` odd/even shaft gear indices and clutch ratios plus `automaticGearbox` `grb_mde = 'D'` / `'N'` state in `spawnMotion.lua` (preserving arcade behavior), and immediately recycles cloaked vehicles that reverse or turn across fast roads instead of uncloaking them on timer expiry.

## [0.1.3] - 2026-11-26

### Fixed
- **Two-Way Road AutoDrive Corner Continuity & Hilly Grade Snapping:** Prevents reverse-node bounce loops (`a -> b -> a`) at turns on two-way roads so AutoDrive drives smoothly through bends and T-junctions without sudden U-turns or stalls, and widens surface vertical-curve grade tolerance (`3.5 m`) while keeping overpasses and stacked decks separated.
- **Arterial Dead-End U-Turn Prevention:** Distinguishes extended fast-halo corridors from true dead-end graph cuts where an arterial transitions into a slower zone near the block boundary, recycling AI traffic before cars brake and make three-point U-turns across two-lane roads.
- **Linux Case-Sensitive VFS Screenshot & Notification Path Resolution (`dev.607` / `dev.617`+):** Resolves lowercase `beamngearth/` folders created by **BeamNG.drive**'s virtual filesystem on case-sensitive Linux drives so `Ctrl+Shift+T` screenshots, receipts, and launch fix/update notices attach reliably alongside full `dev.607` / `dev.617` settings compatibility.

## [0.1.2] - 2026-11-25

### Fixed
- **Latest Dev Build Compatibility (`dev.594` / `dev.595`+):** Decouples client Lua/Vue mod patching from server `fast.MIN_VERSION` bumps so all 8 Traffic and AutoDrive fixes stay active across newer **BeamNG.Earth** dev builds, and syncs `<user_dir>/mods/unpacked/beamng_earth/` live on startup so fixes take effect immediately on the first launch of a new build.
- **Arterial & Highway Cut U-Turn Prevention:** Lowers `FAST_HALO_MS` to `13.4 m/s` (`30 mph`) and widens `CUT_M` (`95 m`) and `CUT_FRESH_M` (`140 m`) so traffic on 30-54 mph arterials and highways keeps ahead route runway instead of executing sudden U-turns near tile boundaries.
- **Resident Add-ons Status & Seamless Side-by-Side Stacking:** Keeps the companion add-on resident while **BeamNG.Earth** runs so the Add-ons status badge stays `running`, eliminates duplicate restart popups on dev updates, self-heals upgrades without receipt errors, and coordinates single-close atomic swaps when stacked alongside **Bridge, Rail & Tunnel Fixes**.

## [0.1.1] - 2026-11-25

### Added
- **Full Native Linux Support (`x86_64`):** Supports **BeamNG.Earth** Linux Type 2 `.AppImage` builds (both repacked via `mksquashfs` and zero-dependency extracted AppDir + POSIX launcher fallback), standalone PyInstaller ELF64 installations, `BeamNG.Earth-Host` server client-mod archives, and source checkouts, with detached pure-Python POSIX background swap and automatic native Linux **BeamNG.drive** user-folder discovery.
- **Cache-Key Isolation & `dev.591` Compatibility:** Deterministically salts bundled `CODE_ID` and `REVISIT_ID` digests during binary/AppImage patching (restoring exact SHA-256 bytes on uninstall) and adds full compatibility through **BeamNG.Earth v1.10.1-beta.dev.591**.

### Fixed
- **1-Click Installer Self-Exe Guard:** Prevents `--self-exe` fallback mode (when system Python is absent) from terminating its own PyInstaller bootloader parent process during setup.

## [0.1.0] - 2026-11-24

### Added
- **Highway Traffic Spawning:** Spawns oncoming and ahead highway traffic farther down the road already matching road speed so cars enter smoothly instead of popping in stopped right ahead.
- **Cleaner Merges & Road Navigation:** Keeps AI traffic centered through highway merges and cleans up broken micro-stubs that caused swerving or sudden braking while preserving signals, seams, and speed limits.
- **Exit-Ramp & Overpass Protection:** Keeps highway AutoDrive and GPS routing locked onto the main carriageway past narrow exit ramps and crossing overpasses.
- **Long-Distance AutoDrive Continuity:** Prevents AutoDrive from dropping routes or braking repeatedly on long highway stretches and recovers smoothly after pausing or crossing tile borders.
- **Live Driving Controls, Race Mode, Passing & Wreck Avoidance:** Applies live Tune slider changes without restarting routes, enables safer passing on clear stretches, and steers around stopped or crashed vehicles.
- **1-Hotkey Issue Reporter (`Ctrl+Shift+T`) & Launch Notices:** Sends a quiet screenshot, location, and anonymous nearby-traffic snapshot without pausing, with optional corner notices at startup for fixed reports and new add-on updates.
- **Side-by-Side Coexistence:** Installs, runs, and uninstalls cleanly alongside **BeamNG.Earth - Bridge, Rail & Tunnel Fixes** (`Ctrl+Shift+B`) in any order.
