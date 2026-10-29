"""Setup helper tests: conflict detection, backup, undo. Uses temporary configs only."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import setup

BINDS = Path(__file__).resolve().parent.parent / "niri/binds.kdl"
LEGACY = """binds {
    // BEGIN DESK TOOLS
    Mod+M hotkey-overlay-title="Minimize" { spawn "noctalia" "msg" "plugin" "local/desk-tools:actions" "all" "minimize"; }
    Mod+Shift+M { spawn "noctalia" "msg" "plugin" "local/desk-tools:actions" "all" "windows"; }
    Mod+Shift+P { spawn "noctalia" "msg" "plugin" "local/desk-tools:actions" "all" "boot"; }
    // END DESK TOOLS
    Mod+T { spawn "ghostty"; }
}
"""


def run(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = setup.main(argv)
    return code, json.loads(out.getvalue())


class Parsing(unittest.TestCase):
    def test_normalize_is_order_and_case_insensitive(self):
        self.assertEqual(setup.normalize("Shift+Mod+m"), setup.normalize("Mod+Shift+M"))
        self.assertEqual(setup.normalize("Super+M"), setup.normalize("Mod+M"))
        self.assertNotEqual(setup.normalize("Mod+M"), setup.normalize("Mod+Shift+M"))

    def test_binds_ignore_comments_strings_and_other_sections(self):
        text = 'input {\n    Mod+M\n}\nbinds {\n    // Mod+X { }\n    Mod+Y { spawn "a{"; }\n    Mod+Z {\n        spawn "b";\n    }\n    /-Mod+Q { }\n}\n'
        self.assertEqual([c for c, _ in setup.binds_in(text)], ["Mod+Y", "Mod+Z"])

    def test_shipped_binds_file_declares_expected_keys(self):
        self.assertEqual([c for c, _ in setup.binds_in(BINDS.read_text())], ['Mod+Shift+P'])


class Apply(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = root / "niri/config.kdl"
        self.config.parent.mkdir()
        env = {"XDG_STATE_HOME": str(root / "state"), "XDG_CONFIG_HOME": str(root), "PATH": str(root / "empty")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def args(self, *extra):
        return ["--niri-config", str(self.config), "--bindings-file", str(BINDS), *extra]

    def test_conflict_aborts_without_changes(self):
        self.config.write_text(LEGACY)
        code, result = run(["apply", *self.args(), "--no-noctalia"])
        self.assertEqual(code, 1)
        self.assertIn("conflict", result["error"])
        self.assertIn("--replace-desk-tools", result["error"])
        self.assertEqual(self.config.read_text(), LEGACY)

    def test_conflict_inside_include_is_detected(self):
        (self.config.parent / "extra.kdl").write_text('binds {\n    Super+Shift+P { spawn "x"; }\n}\n')
        self.config.write_text('include "./extra.kdl"\n')
        code, result = run(["check", *self.args()])
        self.assertEqual(code, 3)
        self.assertEqual(result["conflicts"][0]["key"], "Super+Shift+P")

    def test_replace_desk_tools_then_undo_restores_exactly(self):
        self.config.write_text(LEGACY)
        code, result = run(["apply", *self.args(), "--no-noctalia", "--replace-desk-tools"])
        self.assertEqual(code, 0, result)
        text = self.config.read_text()
        self.assertIn(setup.MARKER, text)
        self.assertIn(f'include optional=true "{BINDS}"', text)
        self.assertIn("Mod+M", text)  # unrelated Desk Tools bindings kept
        self.assertNotIn("local/desk-tools:actions\" \"all\" \"boot", text)
        code, _ = run(["apply", *self.args(), "--no-noctalia"])
        self.assertEqual(code, 0)
        self.assertEqual(self.config.read_text(), text)  # idempotent
        code, _ = run(["undo", "--keep-plugin"])
        self.assertEqual(code, 0)
        self.assertEqual(self.config.read_text(), LEGACY)

    def test_replace_comments_out_existing_binding_and_undo_restores(self):
        original = 'binds {\n    Mod+Shift+P { spawn "other-tool"; }\n    Mod+T { spawn "ghostty"; }\n}\n'
        self.config.write_text(original)
        code, result = run(["apply", *self.args(), "--no-noctalia", "--replace", "Mod+Shift+P"])
        self.assertEqual(code, 0, result)
        text = self.config.read_text()
        self.assertIn('// noctalia-switch-os-setup replaced: Mod+Shift+P { spawn "other-tool"; }', text)
        self.assertEqual([c for c, _ in setup.binds_in(text.split(setup.MARKER)[0])], ["Mod+T"])
        run(["undo", "--keep-plugin"])
        self.assertEqual(self.config.read_text(), original)

    def test_undo_refuses_when_user_edited_after_setup(self):
        self.config.write_text("binds {\n}\n")
        run(["apply", *self.args(), "--no-noctalia"])
        self.config.write_text(self.config.read_text() + "// my edit\n")
        code, result = run(["undo", "--keep-plugin"])
        self.assertEqual(code, 1)
        self.assertIn("changed after setup", result["error"])

    def test_remove_drops_only_managed_include(self):
        self.config.write_text("binds {\n    Mod+T { spawn \"ghostty\"; }\n}\n")
        run(["apply", *self.args(), "--no-noctalia"])
        code, _ = run(["remove"])
        self.assertEqual(code, 0)
        self.assertEqual(self.config.read_text().rstrip(), "binds {\n    Mod+T { spawn \"ghostty\"; }\n}")

    def test_store_paths_are_copied_not_included(self):
        self.config.write_text("binds {\n}\n")
        with mock.patch.object(setup, "stable_binds", wraps=setup.stable_binds) as wrapped:
            run(["apply", *self.args(), "--no-noctalia"])
        self.assertTrue(wrapped.called)
        fake = Path("/nix/store/aaaa-binds.kdl")
        with mock.patch.object(Path, "read_text", return_value="binds {\n}\n"):
            copied = setup.stable_binds(fake, self.config)
        self.assertEqual(copied, self.config.parent / "noctalia-switch-os-binds.kdl")


if __name__ == "__main__":
    unittest.main()
