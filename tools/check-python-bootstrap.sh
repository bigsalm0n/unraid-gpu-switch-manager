#!/bin/bash
set -euo pipefail
scratch=$(mktemp -d)
trap 'rm -rf -- "$scratch"' EXIT

lib=usr/local/lib/gpu-switch-manager
mkdir -p "$scratch/package/$lib" "$scratch/bin"
cp runtime/python-env "$scratch/package/$lib/"
cat > "$scratch/package/$lib/install.py" <<'PY'
import fcntl
from pathlib import Path
import sys
import xml.etree.ElementTree

assert sys.argv[1:] == ["install"]
private = Path(__file__).parent / "python" / "bin" / "python3"
if private.exists():
    assert Path(sys.executable) == private
    assert sys.version_info[:3] == (3, 13, 18)
    print("private Python")
else:
    print("host Python")
PY
package="$scratch/plugin.txz"
tar -cJf "$package" -C "$scratch/package" .
sha=$(sha256sum "$package")
sed -e "s|@PACKAGE@|$package|g" -e "s|@PACKAGE_SHA@|${sha%% *}|g" packaging/install.sh > "$scratch/install.sh"
bash "$scratch/install.sh" | grep -Fx 'host Python'
test ! -e "$scratch/python-runtime.tar.gz"

# Simulate an incompatible or unavailable host Python without masking base OS tools.
printf '#!/bin/bash\nexit 127\n' > "$scratch/bin/python3"
chmod +x "$scratch/bin/python3"
export PATH="$scratch/bin:$PATH"
bash "$scratch/install.sh" | grep -Fx 'private Python'
test -s "$scratch/python-runtime.tar.gz"
printf '#!/bin/bash\nexit 1\n' > "$scratch/bin/curl"
chmod +x "$scratch/bin/curl"
bash "$scratch/install.sh" | grep -Fx 'private Python'
