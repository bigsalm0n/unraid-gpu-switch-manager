"""Derive the package identity and changelog from the Git revision being tested."""
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
import re
import subprocess

TAG = re.compile(r"v(\d{4})\.(\d{2})\.(\d{2})(?:-rc\.([1-9]\d{0,3}))?")
# Last upstream checkout before the managed-PCI rewrite. It had no release tags.
LEGACY_BASE = "ff1e0ce220d1b285801227b067331ffc592f64d8"
INPUTS = ("tools", "runtime", "ui", "packaging", "schema", "tests", ".github")


def git(root, *args):
    trusted = Path(root).resolve().as_posix()
    return subprocess.check_output(["git", "-c", "safe.directory=" + trusted, "-C", str(root), *args],
                                   encoding="utf-8").strip()


def versions(tag):
    m = TAG.fullmatch(tag)
    if not m:
        raise ValueError("Expected vYYYY.MM.DD or vYYYY.MM.DD-rc.N")
    year, month, day, candidate = m.groups()
    date(int(year), int(month), int(day))
    number = int(candidate) if candidate else 9999
    if candidate and number >= 9999:
        raise ValueError("Candidate number must be between 1 and 9998")
    return tag[1:], f"{year}.{month}.{day}.{number:04}", "candidate" if candidate else "stable"


@dataclass(frozen=True)
class Release:
    version: str
    plugin_version: str
    channel: str
    commit: str
    tag: str
    previous: str
    notes: str

    def metadata(self):
        return dict(version=self.version, plugin_version=self.plugin_version, channel=self.channel,
                    commit=self.commit, tag=self.tag or None, previous=self.previous or None)


def describe(root, requested=None):
    commit = git(root, "rev-parse", "HEAD")
    tags = [t for t in git(root, "tag", "--points-at", "HEAD").splitlines() if TAG.fullmatch(t)]
    dirty = git(root, "status", "--porcelain", "--", *INPUTS)
    tag = requested or (max(tags, key=lambda t: versions(t)[1]) if tags and not dirty else "")
    if tag:
        version, plugin_version, channel = versions(tag)
        if tag not in tags:
            raise ValueError(f"Release tag {tag} does not identify the checked-out commit")
        if dirty:
            raise ValueError("Release inputs differ from the tagged commit")
    else:
        timestamp = int(git(root, "show", "-s", "--format=%ct", "HEAD"))
        stamp = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y.%m.%d")
        version, plugin_version, channel = f"{stamp}-dev.{commit[:12]}", stamp + ".0000", "development"
    # Walk first-parent history to select the nearest earlier release. Stable
    # notes cover the whole series, including changes introduced in earlier RCs.
    history = git(root, "log", "--decorate=short", "--first-parent", "--format=%H%x09%D", "HEAD").splitlines()
    previous = ""
    for line in history[1:]:
        sha, _, decoration = line.partition("\t")
        candidates = [s[5:] for s in decoration.split(", ") if s.startswith("tag: ") and TAG.fullmatch(s[5:])]
        if channel == "stable":
            candidates = [t for t in candidates if versions(t)[2] == "stable"]
        if candidates:
            previous = max(candidates, key=lambda t: versions(t)[1])
            break
        if sha == LEGACY_BASE:
            previous = sha
            break
    span = f"{previous}..HEAD" if previous else "HEAD"
    changes = git(root, "log", "--reverse", "--no-merges", "--format=%s%x09%H", span).splitlines()
    heading = {"candidate": "Release candidate", "stable": "Release", "development": "Development build"}[channel]
    lines = [f"# {heading} {version}", "", f"Source commit \x60{commit}\x60. Unraid package version \x60{plugin_version}\x60.", ""]
    if channel == "candidate":
        lines.extend(["Hardware acceptance is pending. See docs/TESTING.md for the hardware procedure.", ""])
    lines += ["## Changes", ""]
    for change in changes:
        subject, _, sha = change.rpartition("\t")
        lines.append(f"- {subject} (\x60{sha[:12]}\x60)")
    if not changes:
        lines.append("This tag promotes the previously tested source revision.")
    return Release(version, plugin_version, channel, commit, tag, previous, "\n".join(lines) + "\n")
