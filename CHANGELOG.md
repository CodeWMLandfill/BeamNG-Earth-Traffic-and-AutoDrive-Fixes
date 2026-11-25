# Changelog

All notable changes to **BeamNG.Earth - Traffic and AutoDrive fixes** are documented in this file.

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
