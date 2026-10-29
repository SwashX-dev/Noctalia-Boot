"""Privileged-helper logic with fake runners. Never touches EFI variables or reboots."""
import json
import unittest

import boot_helper as boot

BOOTCTL_LIST = [
    {"id": "nixos-generation-12.conf", "title": "NixOS", "showTitle": "NixOS (Generation 12)", "linux": "/k", "isDefault": True, "isSelected": True},
    {"id": "nixos-generation-11.conf", "title": "NixOS", "showTitle": "NixOS (Generation 11)", "linux": "/k"},
    {"id": "windows11.conf", "title": "Windows 11", "efi": "/EFI/Microsoft/Boot/bootmgfw.efi"},
    {"id": "auto-reboot-to-firmware-setup", "title": "Reboot Into Firmware Interface"},
]


def efi(values):
    return lambda name: values.get(name, "")


class Detection(unittest.TestCase):
    def test_systemd_boot_is_supported(self):
        result = boot.detect_bootloader(efi({"LoaderInfo": "systemd-boot 260.5"}), lambda p: p == "/sys/firmware/efi")
        self.assertTrue(result["supported"])

    def test_grub_is_detected_and_refused(self):
        result = boot.detect_bootloader(efi({}), lambda p: p in ("/sys/firmware/efi", "/boot/grub/grub.cfg"))
        self.assertEqual(result["name"], "GRUB")
        self.assertFalse(result["supported"])
        self.assertIn("No boot settings will be changed", result["reason"])

    def test_legacy_bios_is_refused(self):
        result = boot.detect_bootloader(efi({"LoaderInfo": "systemd-boot 260"}), lambda p: False)
        self.assertFalse(result["supported"])

    def test_other_efi_loader_is_refused(self):
        result = boot.detect_bootloader(efi({"LoaderInfo": "rEFInd 0.14"}), lambda p: p == "/sys/firmware/efi")
        self.assertFalse(result["supported"])
        self.assertIn("rEFInd", result["name"])

    def test_unsupported_catalog_runs_no_command(self):
        calls = []
        result = boot.catalog(runner=lambda argv: calls.append(argv),
                              detect=lambda: {"name": "GRUB", "supported": False, "reason": "no"})
        self.assertFalse(result["supported"])
        self.assertEqual(result["entries"], [])
        self.assertEqual(calls, [])


class Catalog(unittest.TestCase):
    def catalog(self, listing, values):
        def runner(argv):
            if argv[-1] == "list":
                return json.dumps(listing)
            raise RuntimeError("no esp in tests")
        return boot.catalog(runner=runner, read_efi=efi(values),
                            detect=lambda: {"name": "systemd-boot 260.5", "supported": True, "reason": ""})

    def test_enumerates_entries_and_marks_default_current_next(self):
        state = self.catalog(BOOTCTL_LIST, {"LoaderEntryDefault": "nixos-generation-12.conf",
                                            "LoaderEntrySelected": "nixos-generation-12.conf"})
        self.assertEqual([e["id"] for e in state["entries"]],
                         ["nixos-generation-12.conf", "nixos-generation-11.conf", "windows11.conf"])
        first = state["entries"][0]
        self.assertTrue(first["default"] and first["current"] and first["next"])
        self.assertTrue(state["windows_found"])
        self.assertEqual(state["entries"][2]["family"], "windows")

    def test_oneshot_is_reported_as_next(self):
        state = self.catalog(BOOTCTL_LIST, {"LoaderEntryDefault": "nixos-generation-12.conf",
                                            "LoaderEntryOneShot": "windows11.conf"})
        self.assertEqual(state["next_entry"], "windows11.conf")
        self.assertEqual(state["default_entry"], "nixos-generation-12.conf")

    def test_missing_windows_entry_is_reported(self):
        state = self.catalog(BOOTCTL_LIST[:2], {})
        self.assertFalse(state["windows_found"])
        self.assertEqual(state["default_entry"], "nixos-generation-12.conf")  # from isDefault

    def test_no_entries_is_an_error(self):
        with self.assertRaises(RuntimeError):
            self.catalog([], {})

    def test_catalog_filters_firmware_remote_and_duplicate_entries(self):
        raw = [{"id": "windows11.conf", "title": "Windows 11", "efi": "/EFI/Microsoft/Boot/bootmgfw.efi"},
               {"id": "windows11.conf", "efi": "/different"},
               {"id": "auto-reboot-to-firmware-setup", "efi": "firmware"},
               {"id": "remote.efi", "ukiUrl": "https://example.invalid/os.efi"},
               {"id": "bad;id", "linux": "/kernel"}]
        self.assertEqual([x["id"] for x in boot.normalize_entries(raw)], ["windows11.conf"])

    def test_default_patterns_resolve_against_actual_entries(self):
        entries = [{"id": "windows11.conf"}]
        self.assertEqual(boot.resolve_default("windows*.conf", entries), "windows11.conf")
        self.assertIsNone(boot.resolve_default("@anything", entries))


class Switch(unittest.TestCase):
    def setUp(self):
        self.entries = [{"id": "windows11.conf", "title": "Windows 11"},
                        {"id": "nixos-generation-12.conf", "title": "NixOS"}]
        self.calls = []
        self.value = "older.conf"

    def runner(self, argv):
        self.calls.append(argv)
        if argv[1] == "set-oneshot":
            self.value = argv[2]
        return ""

    def switch(self, identity, runner=None):
        return boot.switch_and_reboot(identity,
            get_catalog=lambda: {"supported": True, "entries": self.entries},
            runner=runner or self.runner, read_efi=lambda name: self.value)

    def test_command_construction_is_exact(self):
        result = self.switch("windows11.conf")
        self.assertEqual(result["state"], "restarting")
        self.assertEqual(self.calls, [[boot.BOOTCTL, "set-oneshot", "windows11.conf"],
                                      [boot.SYSTEMCTL, "--check-inhibitors=yes", "reboot"]])

    def test_nixos_can_be_selected_too(self):
        self.switch("nixos-generation-12.conf")
        self.assertEqual(self.calls[0], [boot.BOOTCTL, "set-oneshot", "nixos-generation-12.conf"])

    def test_default_is_never_changed(self):
        self.switch("windows11.conf")
        self.assertFalse(any("set-default" in argv for argv in self.calls))

    def test_labels_are_never_executed(self):
        self.entries[0]["title"] = "Windows; $(touch /tmp/should-not-exist)"
        self.switch("windows11.conf")
        self.assertNotIn(self.entries[0]["title"], sum(self.calls, []))

    def test_invalid_ids_and_selectors_never_mutate(self):
        for identity in ["@default", "*.conf", "--help", "../windows11.conf", "x;id", "x\nreboot", "$(id)", "", None]:
            with self.subTest(identity=identity):
                with self.assertRaises(ValueError):
                    self.switch(identity)
        self.assertEqual(self.calls, [])

    def test_stale_entry_never_mutates(self):
        with self.assertRaises(ValueError):
            self.switch("removed.conf")
        self.assertEqual(self.calls, [])

    def test_selection_failure_never_reboots(self):
        def fail(argv):
            self.calls.append(argv)
            if argv[-1] == "windows11.conf":
                raise RuntimeError("EFI write failed")
            return ""
        with self.assertRaises(RuntimeError):
            self.switch("windows11.conf", fail)
        self.assertFalse(any(x[0] == boot.SYSTEMCTL for x in self.calls))
        self.assertEqual(self.calls[-1], [boot.BOOTCTL, "set-oneshot", "older.conf"])

    def test_restart_failure_restores_previous_oneshot(self):
        def fail(argv):
            if argv[0] == boot.SYSTEMCTL:
                self.calls.append(argv)
                raise RuntimeError("A shutdown inhibitor blocked restart")
            return self.runner(argv)
        with self.assertRaises(RuntimeError):
            self.switch("windows11.conf", fail)
        self.assertEqual(self.value, "older.conf")

    def test_verification_failure_never_reboots(self):
        def no_write(argv):
            self.calls.append(argv)
            return ""
        with self.assertRaises(RuntimeError):
            self.switch("windows11.conf", no_write)
        self.assertFalse(any(x[0] == boot.SYSTEMCTL for x in self.calls))

    def test_empty_previous_selection_is_cleared_on_failure(self):
        self.value = ""
        def fail(argv):
            if argv[0] == boot.SYSTEMCTL:
                raise RuntimeError("blocked")
            return self.runner(argv)
        with self.assertRaises(RuntimeError):
            self.switch("windows11.conf", fail)
        self.assertEqual(self.value, "")

    def test_unsupported_loader_never_mutates(self):
        with self.assertRaises(RuntimeError):
            boot.switch_and_reboot("windows11.conf", get_catalog=lambda: {"supported": False, "error": "GRUB"},
                                   runner=self.runner)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
