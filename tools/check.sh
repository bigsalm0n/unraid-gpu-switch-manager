#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
for file in runtime/gpuctl runtime/qemu-hook ui/event/* tools/check.sh; do
    bash -n "$file"
    shellcheck "$file"
done
for file in runtime/*.php ui/api.php ui/dashboard.inc ui/GPUSwitchManager.page tests/mock_runtime.php; do php -l "$file"; done
node --check ui/dashboard.js
node --test tests/test_dashboard.js
python3 -B -m unittest discover -s tests -v
python3 -B - <<'PY'
import json, sys
from pathlib import Path
from jsonschema import Draft202012Validator
sys.path.insert(0, "tests")
from test_lifecycle import DEFAULT, Rig
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
