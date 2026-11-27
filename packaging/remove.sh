# shellcheck shell=bash
set -eu
lib=/usr/local/lib/gpu-switch-manager
# shellcheck source=runtime/python-env
source "$lib/python-env"
if ! select_python "$lib"; then
    echo "GPU Switch Manager requires Python 3.9+ to remove its files and hook." >&2
    exit 1
fi
if [[ $python == "$lib/python/bin/python3" ]]; then
    stage=$(mktemp -d)
    trap 'rm -rf -- "$stage"' EXIT
    # Keep the interpreter available while the installer deletes the live copy.
    cp -a -- "$lib/python" "$stage/"
    python="$stage/python/bin/python3"
fi
"$python" -EB "$lib/install.py" remove
package='@PACKAGE@'
rm -f -- "$package" "$(dirname "$package")/python-runtime.tar.gz"
