"""Build a self-contained Unraid manifest and deterministic Slackware-layout txz."""
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tarfile
import xml.etree.ElementTree as ET
from versioning import describe, git

ROOT = Path(__file__).resolve().parents[1]
LIB = "usr/local/lib/gpu-switch-manager/"
WEB = "usr/local/emhttp/plugins/gpu-switch-manager/"


def build(out, tag=None, repository="bigsalm0n/unraid-gpu-switch-manager"):
    out.mkdir(parents=True, exist_ok=True)
    release = describe(ROOT, tag or os.environ.get("GSM_RELEASE_TAG"), os.environ.get("GSM_RELEASE_BASE") or None)
    version = release.version
    package = "gpu-switch-manager-" + version + ".txz"
    files = {}
    sources = git(ROOT, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "runtime", "ui").split("\0")
    for folder, dest in (("runtime", LIB), ("ui", WEB)):
        for name in sources:
            if name.startswith(folder + "/"):
                files[dest + name[len(folder)+1:]] = (ROOT / name).read_bytes().replace(b"\r\n", b"\n")
    for source in ("packaging/install.py", "packaging/legacy.json"):
        files[LIB + Path(source).name] = (ROOT / source).read_bytes().replace(b"\r\n", b"\n")
    metadata = (json.dumps(release.metadata(), sort_keys=True, indent=2) + "\n").encode()
    files[LIB + "release.json"] = metadata
    notes = release.notes
    files[LIB + "RELEASE-NOTES.md"] = notes.encode()
    hashes = {k: hashlib.sha256(data).hexdigest() for k, data in files.items()}
    files[LIB + "installed.json"] = json.dumps(hashes, sort_keys=True).encode()
    with tarfile.open(out / package, "w:xz", format=tarfile.USTAR_FORMAT, preset=9) as tar:
        for name, data in sorted(files.items()):
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            entry.mode = 0o755 if name in (LIB + "gpuctl", LIB + "qemu-hook") or name.startswith(WEB + "event/") else 0o644
            entry.uname = entry.gname = "root"
            tar.addfile(entry, io.BytesIO(data))
    binary = (out / package).read_bytes()
    sha = hashlib.sha256(binary).hexdigest()
    plg = ET.Element("PLUGIN", name="gpu-switch-manager", author="GPU Switch Manager contributors",
                     version=release.plugin_version, min="6.12.0", launch="Settings/GPUSwitchManager", icon="icon-hardware")
    if release.tag:
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
            raise ValueError("Invalid release repository")
        base = (f"https://github.com/{repository}/releases/latest/download" if release.channel == "stable"
                else f"https://raw.githubusercontent.com/{repository}/main")
        plg.set("pluginURL", base + "/gpu-switch-manager.plg")
    ET.SubElement(plg, "CHANGES").text = notes
    cache = "/boot/config/plugins/gpu-switch-manager/" + package
    f = ET.SubElement(plg, "FILE", Name=cache, Type="base64")
    ET.SubElement(f, "INLINE").text = base64.b64encode(binary).decode()
    ET.SubElement(f, "SHA256").text = sha
    f = ET.SubElement(plg, "FILE", Run="/bin/bash")
    ET.SubElement(f, "INLINE").text = f"""
set -euo pipefail
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else "Python 3.9 or later required")'
printf '%s  %s\\n' '{sha}' '{cache}' | sha256sum -c -
stage=$(mktemp -d)
trap 'rm -rf -- "$stage"' EXIT
tar -xJf '{cache}' -C "$stage"
python3 -B "$stage/{LIB}install.py" install
"""
    f = ET.SubElement(plg, "FILE", Run="/bin/bash", Method="remove")
    ET.SubElement(f, "INLINE").text = f"set -eu\npython3 -B /{LIB}install.py remove\nrm -f -- '{cache}'\n"
    manifest = out / "gpu-switch-manager.plg"
    manifest.write_bytes(ET.tostring(plg, encoding="utf-8", xml_declaration=True))
    (out / "RELEASE-NOTES.md").write_bytes(notes.encode())
    (out / "release.json").write_bytes(metadata)
    assets = [package, manifest.name, "RELEASE-NOTES.md", "release.json"]
    sums = "".join(f"{hashlib.sha256((out / name).read_bytes()).hexdigest()}  {name}\n" for name in assets)
    (out / "SHA256SUMS").write_bytes(sums.encode())
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--tag", default=os.environ.get("GSM_RELEASE_TAG") or None, help="Require this exact Git release tag")
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", "bigsalm0n/unraid-gpu-switch-manager"))
    args = parser.parse_args()
    print(build(args.output, args.tag, args.repository))
