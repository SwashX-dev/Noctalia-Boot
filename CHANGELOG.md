# Changelog

## 1.0.1

- Match README, plugin and interface branding to Noctalia Boot.
- Update repository URLs while preserving existing commands, IDs and settings.

## 1.0.0 — 2026-10-29

First standalone release, extracted from the local "Desk Tools" plugin
(`local/desk-tools` 0.1.0), whose operating-system half it replaces.

### Kept from Desk Tools
- Real `bootctl --json=short list` catalog; firmware, remote and malformed entries filtered out.
- Selection changes nothing; an explicit confirmation (button or Ctrl+Enter, never plain Enter) is required.
- Separate PolicyKit actions: read-only listing for the active session, `auth_admin` for the restart.
- Root helper re-validates the entry against a fresh catalog, sets a one-time entry
  (`bootctl set-oneshot`), verifies `LoaderEntryOneShot`, and restarts honoring inhibitors.
- On failure the previous one-time entry is restored; the default entry is never changed.

### New
- Standalone plugin `local/switch-os` and CLI `noctalia-switch-os`.
- Bootloader detection reports Legacy BIOS, GRUB and other EFI loaders explicitly (switching refused).
- `--confirmed` is required by the backend as well as the UI.
- Clear messages for cancelled authentication (pkexec 126) and missing authorization (127).
- "No Windows entry found" hint; older NixOS generations hidden by default (setting).
- Nix flake: package with PolicyKit policy, overlay, NixOS module, checks and a UEFI
  systemd-boot VM test that performs a real one-time switch and reboot inside the VM.
- `noctalia-switch-os-setup` for conflict-checked Niri binding and Noctalia registration.
