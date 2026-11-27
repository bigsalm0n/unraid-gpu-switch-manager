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
for d in ("tools",):
    sys.path.insert(0, str(ROOT / d))
from build import build
from php_bridge import Refused, bridge, read, write
from test_lifecycle import DEFAULT

LIB = "usr/local/lib/gpu-switch-manager"
WEB = "usr/local/emhttp/plugins/gpu-switch-manager"
HOOK = "etc/libvirt/hooks/qemu.d/60-gpu-switch-manager"
MANIFEST = LIB + "/installed.json"
PERSIST = "boot/config/plugins/gpu-switch-manager"


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
        self.mounted = True
        self.config = self.root / PERSIST / "sharing.json"
        self.journal = self.root / PERSIST / "transactions/game.json"
        self.run = self.root / "run/gpu-switch-manager"

    def apply(self, remove=False, stage=None):
        bridge(self.root, "installer", mounted=self.mounted, remove=remove,
               source=None if remove else str(stage or self.stage))

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
            subprocess.run(["shellcheck", "-s", "bash", "-"], input=f.findtext("INLINE"), text=True, check=True)
        self.assertTrue(os.access(self.stage / LIB / "gpuctl", os.X_OK))
        self.assertEqual(root.getroot().get("name"), "gpu-switch-manager")
        self.assertEqual(root.getroot().get("author"), "bigsalmon")
        self.assertEqual(root.getroot().get("launch"), "Settings/GPUSwitchManager")
        page = self.stage / WEB / "GPUSwitchManager.page"
        self.assertIn('Menu="Utilities"', page.read_text().split("---", 1)[0])
        self.assertIn("**GPU Switch Manager**", (self.stage / WEB / "README.md").read_text())
        self.assertFalse(list(self.stage.rglob("*.py")))
        self.assertNotIn("python", root.find("FILE[@Run]").findtext("INLINE").lower())

    def test_manifest_rejects_a_corrupt_cached_package_before_install(self):
        manifest = ET.parse(self.manifest)
        data = manifest.find("FILE")
        cache = self.root / "cache.txz"
        cache.write_bytes(base64.b64decode(data.findtext("INLINE")))
        script = manifest.find("FILE[@Run]").findtext("INLINE").replace(data.get("Name"), str(cache))
        marker = self.root / "install-called"
        shim = self.root / "php"
        shim.write_text('#!/bin/bash\nif [[ ${3-} == -r ]]; then exit 0; fi\ntest -r "$3" && test "$4" = install && touch "$MARKER"\n')
        shim.chmod(0o755)
        script = script.replace("/usr/bin/php", str(shim))
        env = dict(os.environ, PATH="/usr/bin:/bin", MARKER=str(marker))
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
        write(self.config, cfg)
        foreign = self.root / WEB / "third-party.txt"
        foreign.write_text("retain")
        self.apply()
        self.assertEqual(read(self.config), cfg)
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
        self.assertEqual(read(self.config), cfg)
        self.assertFalse(self.run.exists())

    def test_unmounted_image_and_active_transaction(self):
        self.mounted = False
        self.apply()
        self.assertFalse(os.path.lexists(self.root / HOOK))
        self.mounted = True
        self.apply()
        self.mounted = False
        with self.assertRaisesRegex(Refused, "mounted"):
            self.apply(remove=True)
        self.mounted = True
        write(self.journal, {"state": "VM_ACTIVE"})
        for remove in (False, True):
            with self.assertRaisesRegex(Refused, "Outstanding"):
                self.apply(remove)
        self.assertTrue((self.root / MANIFEST).exists())

    def test_boot_reinstall_preserves_pending_transaction(self):
        write(self.config, DEFAULT)
        write(self.journal, {"state": "ERROR"})
        self.apply()
        self.assertTrue(self.journal.exists())

    def test_image_hook_vetoes_pci_assignment_when_runtime_missing(self):
        self.apply()
        cfg = self.root / "missing-runtime.json"
        write(cfg, {"profiles": {"Windows": {"gpu": "game"}}})
        wrapper = (self.root / HOOK).read_text().replace("/usr/local/lib/gpu-switch-manager/gpuctl", str(self.root / "missing-ctl"))
        wrapper = wrapper.replace("/boot/config/plugins/gpu-switch-manager/sharing.json", str(cfg))
        (self.root / HOOK).write_text(wrapper)
        pci = "<hostdev type='pci'><source><address domain='0x0000' bus='0x05' slot='0x00' function='0x0'/></source></hostdev>"
        for name, devices, expected in (("Windows", pci, 1), ("Windows", "", 0), ("Control guest", "", 0)):
            xml = f"<domain><name>{name}</name><devices>{devices}</devices></domain>"
            p = subprocess.run(["bash", str(self.root / HOOK), name, "prepare", "begin"], input=xml, text=True, capture_output=True)
            self.assertEqual(p.returncode, expected, p.stderr)
            self.assertEqual(p.stdout, "")

    def test_upgrade_removes_only_manifest_owned_private_python_files(self):
        self.apply()
        path = self.root / LIB / "python/bin/python3"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"private interpreter fixture")
        launcher = self.root / LIB / "gpuctl"
        launcher.write_text("#!/bin/bash\nexit 123\n")
        manifest_path = self.root / MANIFEST
        manifest = read(manifest_path)
        for owned in (path, launcher):
            manifest[owned.relative_to(self.root).as_posix()] = hashlib.sha256(owned.read_bytes()).hexdigest()
        write(manifest_path, manifest)
        cfg = DEFAULT | {"enabled": True}
        write(self.config, cfg)
        self.apply()
        self.assertFalse((self.root / LIB / "python").exists())
        self.assertEqual(launcher.read_bytes(), (self.stage / LIB / "gpuctl").read_bytes())
        self.assertEqual(read(self.config), cfg)

    def test_upgrade_refuses_vfio_on_associated_audio_function(self):
        self.apply()
        cfg = DEFAULT | {"gpus": {"game": {"pci": "0000:62:00.0", "protected": False,
               "uuid": "GPU-00000000-0000-0000-0000-000000000001",
               "functions": {"0": "030000:10de:0000:10de:0000", "1": "040300:10de:0001:10de:0001"}}}}
        write(self.config, cfg)
        pci = self.root / "sys/bus/pci/devices/0000:62:00.1"
        pci.mkdir(parents=True)
        (pci / "driver").symlink_to("/sys/bus/pci/drivers/vfio-pci")
        with self.assertRaisesRegex(Refused, "still uses VFIO"):
            self.apply()

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
        (self.root / HOOK).parent.write_text("not a directory")
        with self.assertRaisesRegex(Refused, "safe directory"):
            self.apply()
        self.assertFalse((self.root / MANIFEST).exists())
        self.assertFalse((self.root / WEB / "api.php").exists())
        self.assertTrue(self.native.exists())

    def test_migrate_legacy_shape_preserve_flash(self):
        legacy = self.root / PERSIST / "profiles.json"
        write(legacy, {"version": "1.0", "profiles": {"Windows": {"gpu": "1"}}})
        self.apply()
        self.assertEqual(read(legacy)["version"], "1.0")
        self.assertEqual(read(self.config), DEFAULT)

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
