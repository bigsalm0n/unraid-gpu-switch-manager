import os
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from versioning import describe, versions
from release import BEGIN, END, MANIFEST, candidate, promote


class VersionedBuild(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Release test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "runtime").mkdir()
        self.commit("Baseline")
        self.git("tag", "v2026.10.01")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True)

    def commit(self, summary):
        (self.repo / "runtime/example").write_text(summary)
        self.git("add", "runtime")
        self.git("commit", "-qm", summary)

    def test_natural_candidate_order_matches_unraid_string_order(self):
        values = [versions(tag)[1] for tag in ("v2026.11.27-rc.1", "v2026.11.27-rc.2", "v2026.11.27-rc.10", "v2026.11.27")]
        self.assertEqual(values, sorted(values))
        self.assertEqual(len(set(values)), 4)
        for tag in ("main", "v2026.02.31", "v2026.11.27-rc.0", "v2026.11.27-rc.9999", "v2026.11.27-rc.01"):
            with self.assertRaises(ValueError):
                versions(tag)

    def test_tag_must_identify_the_tested_clean_input(self):
        self.commit("First candidate")
        with self.assertRaisesRegex(ValueError, "checked-out commit"):
            describe(self.repo, "v2026.10.01")
        self.git("tag", "v2026.11.27-rc.1")
        self.assertEqual(describe(self.repo).channel, "candidate")
        (self.repo / "runtime/example").write_text("uncommitted")
        self.assertEqual(describe(self.repo).channel, "development")
        with self.assertRaisesRegex(ValueError, "differ"):
            describe(self.repo, "v2026.11.27-rc.1")

    def test_candidate_delta_and_stable_promotion_notes(self):
        self.commit("Preserve the original QEMU hook")
        self.git("tag", "v2026.11.27-rc.1")
        self.commit("Check the audio driver after release")
        self.git("tag", "v2026.11.27-rc.2")
        rc = describe(self.repo)
        self.assertEqual(rc.previous, "v2026.11.27-rc.1")
        self.assertIn("Check the audio driver", rc.notes)
        self.assertNotIn("Preserve the original", rc.notes)
        self.git("tag", "v2026.11.27")
        stable = describe(self.repo)
        self.assertEqual(stable.channel, "stable")
        self.assertEqual(stable.previous, "v2026.10.01")
        self.assertIn("Preserve the original", stable.notes)
        self.assertIn("Check the audio driver", stable.notes)
        self.assertNotIn("Baseline", stable.notes)

    def test_untagged_build_is_a_reproducible_development_snapshot(self):
        self.commit("Pending revision")
        a = describe(self.repo)
        self.assertEqual(a, describe(self.repo))
        self.assertEqual(a.channel, "development")
        self.assertIn(a.commit[:12], a.version)
        self.assertTrue(a.plugin_version.endswith(".0000"))

    def test_two_main_commits_get_ordered_repeatable_tags(self):
        first = candidate(self.repo)
        self.commit("Second tested revision")
        second = candidate(self.repo)
        self.assertLess(versions(first)[1], versions(second)[1])
        self.assertEqual(second, candidate(self.repo))

    def test_main_candidate_advances_past_manual_tag_on_a_merged_branch(self):
        main = self.git("branch", "--show-current").strip()
        self.git("checkout", "-qb", "fix")
        self.commit("Manually tagged fix")
        prefix = candidate(self.repo).rsplit(".", 1)[0]
        manual = prefix + ".90"
        self.git("tag", manual)
        self.assertEqual(candidate(self.repo), manual)
        self.git("checkout", "-q", main)
        self.git("merge", "-q", "--no-ff", "fix", "-m", "Merge fix")
        self.assertEqual(candidate(self.repo), prefix + ".91")
        self.commit("Next main revision")
        self.assertEqual(candidate(self.repo), prefix + ".92")

    def test_rolling_url_and_readme_only_advance_to_verified_releases(self):
        readme = self.repo / "README.md"
        readme.write_text(f"Intro\n{BEGIN}\nold\n{END}\nOther docs\n")
        assets = self.repo / "assets"
        assets.mkdir()
        repo = "example/plugin"
        def bundle(n):
            tag = f"v2026.12.01-rc.{n}"
            value = versions(tag)[1]
            data = f'<PLUGIN version="{value}" />'.encode()
            (assets / MANIFEST).write_bytes(data)
            (assets / "release.json").write_text(json.dumps(dict(tag=tag, plugin_version=value)))
            (assets / "SHA256SUMS").write_text(hashlib.sha256(data).hexdigest() + "  " + MANIFEST)
            return tag
        tag = bundle(2)
        promote(self.repo, assets, repo, tag)
        original = readme.read_bytes()
        self.assertIn(f"/releases/download/{tag}/{MANIFEST}".encode(), original)
        self.assertIn(f"https://raw.githubusercontent.com/{repo}/release-installer/{MANIFEST}".encode(), original)
        self.assertTrue(original.endswith(b"Other docs\n"))
        promote(self.repo, assets, repo, tag)
        promote(self.repo, assets, repo, bundle(1))
        self.assertEqual(original, readme.read_bytes())
        installed = (self.repo / MANIFEST).read_bytes()
        tag = bundle(3)
        with (assets / MANIFEST).open("ab") as file:
            file.write(b" ")
        with self.assertRaisesRegex(ValueError, "checksum"):
            promote(self.repo, assets, repo, tag)
        self.assertEqual((self.repo / MANIFEST).read_bytes(), installed)
