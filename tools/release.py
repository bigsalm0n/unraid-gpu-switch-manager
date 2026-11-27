"""Select a candidate tag and promote its verified installer into the README."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from versioning import git, versions

BEGIN = "<!-- latest-install:begin -->"
END = "<!-- latest-install:end -->"
MANIFEST = "gpu-switch-manager.plg"


def candidate(root):
    stamp = datetime.fromtimestamp(int(git(root, "show", "-s", "--format=%ct", "HEAD")), timezone.utc)
    start = stamp.strftime("%Y-%m-%dT00:00:00Z")
    # First-parent position gives concurrent pushes different tags. Re-running
    # a build of the same revision selects the same immutable release.
    sequence = int(git(root, "rev-list", "--count", "--first-parent", "--since-as-filter=" + start, "HEAD"))
    tag = f"v{stamp:%Y.%m.%d}-rc.{sequence}"
    versions(tag)
    return tag


def promote(root, assets, repo, tag):
    version, encoded, _ = versions(tag)
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise ValueError("Invalid GitHub repository")
    metadata = json.loads((assets / "release.json").read_bytes())
    payload = (assets / MANIFEST).read_bytes()
    if metadata["tag"] != tag or metadata["plugin_version"] != encoded or ET.fromstring(payload).get("version") != encoded:
        raise ValueError("Release metadata and installer version disagree")
    sums = {name: sha for sha, name in (line.split("  ", 1) for line in (assets / "SHA256SUMS").read_text().splitlines())}
    if hashlib.sha256(payload).hexdigest() != sums[MANIFEST]:
        raise ValueError("Installer checksum mismatch")
    current = root / MANIFEST
    if current.exists():
        previous = ET.fromstring(current.read_bytes()).get("version", "")
        if previous > encoded:
            return
        if previous == encoded and current.read_bytes() != payload:
            raise ValueError("Published installer differs at the same version")
    readme = root / "README.md"
    doc = readme.read_text(encoding="utf8")
    if doc.count(BEGIN) != 1 or doc.count(END) != 1 or doc.index(BEGIN) >= doc.index(END):
        raise ValueError("Missing or ambiguous README installation markers")
    release = f"https://github.com/{repo}/releases"
    channel = f"https://raw.githubusercontent.com/{repo}/main/{MANIFEST}"
    entry = (f"{BEGIN}\n```text\n{channel}\n```\n\nCurrent build **{version}** · [Release notes]({release}/tag/{tag})"
             f" · [Version-pinned installer]({release}/download/{tag}/{MANIFEST})\n{END}")
    updated = doc[:doc.index(BEGIN)] + entry + doc[doc.index(END) + len(END):]
    current.write_bytes(payload)
    readme.write_bytes(updated.encode())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("tag")
    p = commands.add_parser("promote")
    p.add_argument("assets", type=Path)
    p.add_argument("repository")
    p.add_argument("tag")
    args = parser.parse_args()
    if args.command == "tag":
        print(candidate(Path.cwd()))
    else:
        promote(Path.cwd(), args.assets, args.repository, args.tag)
