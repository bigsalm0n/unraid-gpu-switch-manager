#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
for file in runtime/qemu-hook runtime/python-env ui/event/* packaging/gpuctl packaging/{install,remove}.sh tools/check{,-python-bootstrap}.sh; do
    bash -n "$file"
    shellcheck -x "$file"
done
bash tools/check-python-bootstrap.sh
for file in ui/api.php ui/dashboard.inc ui/GPUSwitchManager.page; do php -l "$file"; done
node --check ui/dashboard.js
python3 -B -m unittest discover -s tests -v
python3 -B - <<'PY'
import json, sys
from pathlib import Path
from jsonschema import Draft202012Validator
sys.path[:0] = ["runtime", "tests"]
from model import DEFAULT
from test_lifecycle import Rig
schema = json.loads(Path("schema/sharing.schema.json").read_text())
Draft202012Validator.check_schema(schema)
v = Draft202012Validator(schema)
v.validate(DEFAULT)
rig = Rig()
try:
    rig.setUp()
    v.validate(rig.cfg)
    assert not v.is_valid(dict(DEFAULT, enabled="true"))
finally:
    rig.doCleanups()
PY
python3 tools/build.py
(cd dist && sha256sum -c SHA256SUMS)
