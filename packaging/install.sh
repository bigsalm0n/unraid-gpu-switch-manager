set -euo pipefail
package='@PACKAGE@'
printf '%s  %s\n' '@PACKAGE_SHA@' "$package" | sha256sum -c -
stage=$(mktemp -d)
download=''
cleanup() {
    rm -rf -- "$stage"
    if [[ -n $download ]]; then rm -f -- "$download"; fi
}
trap cleanup EXIT
tar -xJf "$package" -C "$stage"
lib="$stage/usr/local/lib/gpu-switch-manager"
# shellcheck source=runtime/python-env
source "$lib/python-env"

if ! select_python "$lib"; then
    if [[ $(uname -m) != x86_64 ]]; then
        echo "GPU Switch Manager needs Python 3.9+ on this architecture." >&2
        exit 1
    fi
    asset='cpython-3.13.18+20261124-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz'
    checksum='b56ceb4fc1d8f0c49b7bd4dc91cd176bad7b86a88b9fc509bae6b10b3ee36692'
    cache="$(dirname "$package")/python-runtime.tar.gz"
    if ! printf '%s  %s\n' "$checksum" "$cache" | sha256sum -c --status 2>/dev/null; then
        echo "Downloading GPU Switch Manager's private Python runtime..."
        download=$(mktemp "$cache.XXXXXX")
        curl --fail --location --silent --show-error --retry 3 --connect-timeout 15 --max-time 180 \
            "https://github.com/astral-sh/python-build-standalone/releases/download/20261124/$asset" -o "$download"
        printf '%s  %s\n' "$checksum" "$download" | sha256sum -c -
        mv -f -- "$download" "$cache"
        download=''
    fi
    # Verify a staged copy so extraction uses the exact bytes just checked.
    cp -- "$cache" "$stage/python.tar.gz"
    printf '%s  %s\n' "$checksum" "$stage/python.tar.gz" | sha256sum -c -
    tar -xzf "$stage/python.tar.gz" --no-same-owner -C "$stage"
    # The installer tracks regular files. Materialize the archive's internal aliases.
    cp -aL -- "$stage/python" "$lib/python"
    if ! select_python "$lib"; then
        echo "GPU Switch Manager could not start its private Python runtime." >&2
        exit 1
    fi
fi
"$python" -EB "$lib/install.py" install
