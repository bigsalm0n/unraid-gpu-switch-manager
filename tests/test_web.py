import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from php_bridge import PHP

ROOT = Path(__file__).resolve().parents[1]


class NativeWebContract(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        backend = self.root / "gpuctl"
        backend.write_text("""#!/bin/sh
printf '{"action":"%s"}\\n' "$1"
""")
        backend.chmod(0o755)
        code = (ROOT / "ui/api.php").read_text().replace("/usr/local/lib/gpu-switch-manager/gpuctl", str(backend))
        (self.root / "api.php").write_text(code)
        (self.root / "call.php").write_text("""<?php
$req=json_decode(stream_get_contents(STDIN), true);
if ($req['user']) {
  session_id('fixture-session'); session_start();
  $_SESSION=['unraid_user'=>$req['user'],'unraid_login'=>time()];
  $_COOKIE[session_name()]=session_id(); session_write_close();
}
$_SERVER['REQUEST_METHOD']=$req['method'];
$_POST=$req['post'];
// Model the WebGUI prelude contract, including consuming the validated token.
if ($req['native']) {
  function csrf_terminate($reason) {
    http_response_code(403);
    exit(json_encode(['error' => "$reason csrf_token"]));
  }
  if ($req['method'] === 'POST') {
    if (!isset($_POST['csrf_token'])) csrf_terminate('missing');
    if ($_POST['csrf_token'] !== 'test-token') csrf_terminate('wrong');
    unset($_POST['csrf_token']);
  }
}
include __DIR__.'/api.php';
""")

    def call(self, user="root", method="POST", native=True, **post):
        result = subprocess.run([*PHP, "-d", "session.save_path=" + str(self.root), str(self.root / "call.php")],
                                input=json.dumps(dict(user=user, method=method, post=post, native=native)), capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def test_session_csrf_and_confirmation(self):
        valid = dict(action="start", csrf_token="test-token", confirmed="yes", vm="Windows 11")
        for user in ("", "guest"):
            self.assertIn("error", self.call(user=user, **valid))
        self.assertIn("error", self.call(method="GET", **valid))
        self.assertEqual(self.call(native=False, **valid), {"error": "Unraid request validation is unavailable"})
        self.assertEqual(self.call(action="status"), {"error": "missing csrf_token"})
        for key, value in (("csrf_token", ""), ("confirmed", "no"), ("vm", "-invalid"), ("action", "hook")):
            self.assertIn("error", self.call(**(valid | {key: value})))
        self.assertEqual(self.call(**valid), {"action": "start"})
