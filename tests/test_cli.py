"""User-side front end: confirmation gate, pkexec construction, auth failures."""
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import cli


def completed(code, stdout="", stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class FrontEnd(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.temp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = []
        exists = mock.patch.object(cli.os.path, "exists", side_effect=lambda p: p == cli.PKEXEC or os.path.isfile(p))
        exists.start()
        self.addCleanup(exists.stop)

    def runner(self, result):
        def run(argv, **kwargs):
            self.calls.append(argv)
            return result
        return run

    def test_start_requires_confirmation(self):
        with mock.patch.object(cli.subprocess, "Popen") as popen:
            with self.assertRaises(PermissionError):
                cli.start("windows11.conf", confirmed=False)
            popen.assert_not_called()

    def test_start_rejects_option_like_ids(self):
        with mock.patch.object(cli.subprocess, "Popen") as popen:
            with self.assertRaises(ValueError):
                cli.start("--help", confirmed=True)
            popen.assert_not_called()

    def test_start_launches_worker_with_literal_argv(self):
        with mock.patch.object(cli.subprocess, "Popen") as popen:
            self.assertEqual(cli.start("windows11.conf", confirmed=True), {"started": True})
        argv = popen.call_args[0][0]
        self.assertEqual(argv[-3:], ["worker", "--entry", "windows11.conf"])
        self.assertNotIn("shell", popen.call_args[1])

    def test_status_uses_read_only_action(self):
        data = {"supported": True, "entries": []}
        result = cli.catalog(runner=self.runner(completed(0, json.dumps(data))))
        self.assertEqual(self.calls, [[cli.PKEXEC, cli.BOOT_LIST]])
        self.assertEqual(result["operation"]["state"], "idle")

    def test_unsupported_bootloader_is_not_an_error(self):
        data = {"supported": False, "bootloader": "GRUB", "entries": [], "error": "GRUB detected"}
        result = cli.catalog(runner=self.runner(completed(0, json.dumps(data))))
        self.assertFalse(result["supported"])

    def test_authentication_cancelled(self):
        status = cli.boot_worker("windows11.conf", runner=self.runner(completed(126)), notify=False)
        self.assertEqual(status["state"], "failed")
        self.assertIn("cancelled", status["message"])
        self.assertEqual(self.calls, [[cli.PKEXEC, cli.BOOT_REBOOT, "windows11.conf"]])

    def test_not_authorized(self):
        status = cli.boot_worker("windows11.conf", runner=self.runner(completed(127)), notify=False)
        self.assertIn("not authorized", status["message"])

    def test_helper_error_is_surfaced(self):
        result = completed(1, json.dumps({"error": "The selected entry no longer exists"}))
        status = cli.boot_worker("windows11.conf", runner=self.runner(result), notify=False)
        self.assertEqual(status["message"], "The selected entry no longer exists")

    def test_success_records_restarting(self):
        result = completed(0, json.dumps({"state": "restarting", "message": "Restart requested into Windows 11"}))
        status = cli.boot_worker("windows11.conf", runner=self.runner(result), notify=False)
        self.assertEqual(status["state"], "restarting")

    def test_interrupted_worker_is_detected(self):
        cli.write_json(cli.state_dir() / "boot-operation.json",
                       {"state": "authenticating", "pid": 2 ** 22 + 12345, "boot_id": cli.boot_id()})
        self.assertEqual(cli.operation_status()["state"], "interrupted")

    def test_main_reports_json_errors(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["start", "--entry", "windows11.conf"])
        self.assertEqual(code, 1)
        self.assertIn("not confirmed", json.loads(out.getvalue())["error"])


if __name__ == "__main__":
    unittest.main()
