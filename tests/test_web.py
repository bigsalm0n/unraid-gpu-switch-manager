import json
from pathlib import Path
import subprocess
import tempfile
import unittest

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
        ini = self.root / "var.ini"
        ini.write_text('csrf_token="test-token"\n')
        code = (ROOT / "ui/api.php").read_text().replace("/usr/local/lib/gpu-switch-manager/gpuctl", str(backend)).replace("/var/local/emhttp/var.ini", str(ini))
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
include __DIR__.'/api.php';
""")

    def call(self, user="root", method="POST", **post):
        result = subprocess.run(["php", "-d", "session.save_path=" + str(self.root), str(self.root / "call.php")],
                                input=json.dumps(dict(user=user, method=method, post=post)), capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def test_session_csrf_and_confirmation(self):
        valid = dict(action="start", csrf_token="test-token", confirmed="yes", vm="Windows 11")
        for user in ("", "guest"):
            self.assertIn("error", self.call(user=user, **valid))
        self.assertIn("error", self.call(method="GET", **valid))
        for key, value in (("csrf_token", ""), ("confirmed", "no"), ("vm", "-invalid"), ("action", "hook")):
            self.assertIn("error", self.call(**(valid | {key: value})))
        self.assertEqual(self.call(**valid), {"action": "start"})
