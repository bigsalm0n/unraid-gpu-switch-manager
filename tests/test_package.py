import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
for d in ("runtime", "tools", "packaging"):
    sys.path.insert(0, str(ROOT / d))
from build import build
from install import Installer, LIB, WEB, HOOK, MANIFEST, PERSIST, TARGET
from model import DEFAULT, Refused, read, write


class Package(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.package_dir = Path(cls.tmp.name) / "dist"
        cls.manifest = build(cls.package_dir)
        cls.stage = Path(cls.tmp.name) / "stage"
        with tarfile.open(next(cls.package_dir.glob("*.txz"))) as archive:
            archive.extractall(cls.stage, filter="data")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.native = self.root / "etc/libvirt/hooks/qemu"
        self.native.parent.mkdir(parents=True)
        self.native.write_text("#!/bin/sh\nexit 0\n")
        self.tool = Installer(self.root, self.stage, mounted=True)

    def apply(self, remove=False):
        with self.tool.s.lock("maintenance"):
            self.tool.apply(remove=remove)

    def test_build_reproducible_manifest_and_payload(self):
        other = self.root / "dist"
        build(other)
        for p in other.iterdir():
            self.assertEqual(p.read_bytes(), (self.package_dir / p.name).read_bytes())
        root = ET.parse(self.manifest)
        release = read(self.package_dir / "release.json")
        self.assertEqual(release, read(self.stage / LIB / "release.json"))
        self.assertEqual(root.getroot().get("version"), release["plugin_version"])
        self.assertEqual(root.findtext("CHANGES"), (self.package_dir / "RELEASE-NOTES.md").read_text())
        self.assertIn(release["commit"], root.findtext("CHANGES"))
        payload = root.find("FILE")
        raw = base64.b64decode(payload.findtext("INLINE"), validate=True)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), payload.findtext("SHA256"))
        self.assertEqual(raw, next(other.glob("*.txz")).read_bytes())
        for f in root.findall("FILE[@Run]"):
            subprocess.run(["bash", "-n"], input=f.findtext("INLINE"), text=True, check=True)
            subprocess.run(["shellcheck", "-x", "-s", "bash", "-"], input=f.findtext("INLINE"), text=True, check=True)
        self.assertTrue(os.access(self.stage / LIB / "gpuctl", os.X_OK))

    def test_manifest_rejects_a_corrupt_cached_package_before_install(self):
        manifest = ET.parse(self.manifest)
        data = manifest.find("FILE")
        cache = self.root / "cache.txz"
        cache.write_bytes(base64.b64decode(data.findtext("INLINE")))
        script = manifest.find("FILE[@Run]").findtext("INLINE").replace(data.get("Name"), str(cache))
        marker = self.root / "install-called"
        shim = self.root / "python3"
        shim.write_text('#!/bin/bash\nif [[ $1 == -EB ]]; then test -r "$2" && test "$3" = install && touch "$MARKER"; fi\n')
        shim.chmod(0o755)
        env = dict(os.environ, PATH=str(self.root) + ":" + os.environ["PATH"], MARKER=str(marker))
        for intact in (True, False):
            if not intact:
                marker.unlink()
                cache.write_bytes(cache.read_bytes() + b"corrupt")
            result = subprocess.run(["bash", "-s"], input=script, env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode == 0, intact, result.stderr)
            self.assertEqual(marker.exists(), intact)

    def test_install_upgrade_remove_ownership(self):
        self.apply()
        self.assertEqual((self.root / HOOK).read_bytes(), (self.stage / LIB / "qemu-hook").read_bytes())
        cfg = DEFAULT | {"enabled": True}
        self.tool.s.change(cfg)
        foreign = self.root / WEB / "third-party.txt"
        foreign.write_text("retain")
        self.apply()
        self.assertEqual(self.tool.s.cfg(), cfg)
        other = self.root / HOOK
        other.unlink()
        other.symlink_to("/third-party/hook")
        with self.assertRaises(Refused):
            self.apply(remove=True)
        other.unlink()
        shutil.copy2(self.stage / LIB / "qemu-hook", other)
        self.apply(remove=True)
        self.assertFalse(os.path.lexists(other))
        self.assertFalse((self.root / LIB).exists())
        self.assertEqual(self.native.read_text(), "#!/bin/sh\nexit 0\n")
        self.assertEqual(foreign.read_text(), "retain")
        self.assertEqual(self.tool.s.cfg(), cfg)
        self.assertFalse(self.tool.s.run.exists())

    def test_unmounted_image_and_active_transaction(self):
        self.tool.mounted = False
        self.apply()
        self.assertFalse(os.path.lexists(self.root / HOOK))
        self.tool.mounted = True
        self.apply()
        self.tool.mounted = False
        with self.assertRaisesRegex(Refused, "mounted"):
            self.apply(remove=True)
        self.tool.mounted = True
        write(self.tool.s.journal("game"), {"state": "VM_ACTIVE"})
        for remove in (False, True):
            with self.assertRaisesRegex(Refused, "Outstanding"):
                self.apply(remove)
        self.assertTrue((self.root / MANIFEST).exists())

    def test_boot_reinstall_preserves_pending_transaction(self):
        self.tool.s.change(DEFAULT.copy())
        write(self.tool.s.journal("game"), {"state": "ERROR"})
        self.apply()
        self.assertTrue(self.tool.s.journal("game").exists())

    def test_image_hook_vetoes_profile_when_runtime_missing(self):
        self.apply()
        cfg = self.root / "missing-runtime.json"
        write(cfg, {"profiles": {"Windows": {"gpu": "game"}}})
        wrapper = (self.root / HOOK).read_text().replace("/usr/local/lib/gpu-switch-manager", str(self.root / "missing-runtime"))
        wrapper = wrapper.replace("/boot/config/plugins/gpu-switch-manager/sharing.json", str(cfg))
        (self.root / HOOK).write_text(wrapper)
        for name, expected in (("Windows", 1), ("Control guest", 0)):
            xml = f"<domain><name>{name}</name><devices/></domain>"
            p = subprocess.run(["bash", str(self.root / HOOK), name, "prepare", "begin"], input=xml, text=True, capture_output=True)
            self.assertEqual(p.returncode, expected, p.stderr)
            self.assertEqual(p.stdout, "")

    def test_unknown_legacy_main_hook_refused_before_install(self):
        self.native.write_text("# libvirt qemu hook for GPU switching\n")
        with self.assertRaisesRegex(Refused, "Legacy main"):
            self.apply()
        self.assertFalse((self.root / MANIFEST).exists())

    def test_modified_runtime_is_preserved(self):
        self.apply()
        f = self.root / WEB / "api.php"
        f.write_text("administrator edit")
        with self.assertRaisesRegex(Refused, "modified"):
            self.apply(remove=True)
        self.assertEqual(f.read_text(), "administrator edit")
        self.assertTrue((self.root / HOOK).is_file())

    def test_files_roll_back_when_registration_fails(self):
        from unittest.mock import patch
        with patch.object(self.tool, "register", side_effect=OSError("simulated write error")), self.assertRaises(OSError):
            self.apply()
        self.assertFalse((self.root / MANIFEST).exists())
        self.assertFalse((self.root / WEB / "api.php").exists())
        self.assertTrue(self.native.exists())

    def test_migrate_legacy_shape_preserve_flash(self):
        legacy = self.root / PERSIST / "profiles.json"
        write(legacy, {"version": "1.0", "profiles": {"Windows": {"gpu": "1"}}})
        self.apply()
        self.assertEqual(read(legacy)["version"], "1.0")
        self.assertEqual(self.tool.s.cfg(), DEFAULT)

    def test_real_legacy_package_when_supplied(self):
        path = os.environ.get("GSM_LEGACY_TEST_TXZ")
        if not path:
            self.skipTest("Supply the May 13 archive from Git history to test the installed legacy layout")
        with tarfile.open(path) as archive:
            archive.extractall(self.root, filter="data")
        self.assertTrue((self.root / WEB / "scripts/apply_profile.php").exists())
        self.apply()
        self.assertFalse((self.root / WEB / "scripts/apply_profile.php").exists())
        self.assertEqual(self.native.read_text(), "#!/bin/sh\nexit 0\n")
        self.apply(remove=True)
        self.assertFalse((self.root / WEB).exists())
