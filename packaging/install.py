#!/usr/bin/env python3
"""Install only files in our manifest under a maintenance lock. Keep existing settings."""
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from model import DEFAULT, Refused, Store, read, require, validate, write

LIB = "usr/local/lib/gpu-switch-manager"
WEB = "usr/local/emhttp/plugins/gpu-switch-manager"
HOOK = "etc/libvirt/hooks/qemu.d/60-gpu-switch-manager"
TARGET = "/" + LIB + "/qemu-hook"
MANIFEST = LIB + "/installed.json"
PERSIST = "boot/config/plugins/gpu-switch-manager"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Installer:
    def __init__(self, root, source=None, mounted=None):
        self.root, self.source = root, source
        self.s = Store(root / PERSIST, root / "run/gpu-switch-manager")
        self.mounted = (root / "etc/libvirt").is_mount() if mounted is None else mounted

    def register(self):
        hook = self.root / HOOK
        if self.mounted:
            self.check_hooks()
            hook.parent.mkdir(parents=True, exist_ok=True)
            source = self.root / LIB / "qemu-hook"
            expected = digest(source)
            if hook.is_symlink() or not hook.is_file() or digest(hook) != expected or not os.access(hook, os.X_OK):
                with tempfile.NamedTemporaryFile(dir=hook.parent, delete=False) as temp:
                    stage = Path(temp.name)
                try:
                    shutil.copy2(source, stage)
                    stage.replace(hook)
                finally:
                    stage.unlink(missing_ok=True)
            marker = self.s.config / "hook-registered"
            if not marker.exists() or read(marker) != expected:
                write(marker, expected)
            return True
        return False

    def check_hooks(self):
        hook = self.root / HOOK
        if os.path.lexists(hook):
            if hook.is_symlink():
                owned = os.readlink(hook) == TARGET
            else:
                marker = self.s.config / "hook-registered"
                owned = hook.is_file() and marker.is_file() and digest(hook) == read(marker)
            require(owned, "Foreign or modified hook occupies " + HOOK)
        native = self.root / "etc/libvirt/hooks/qemu"
        if native.is_file():
            require(b"gpu-switch-manager/scripts/vm_" not in native.read_bytes()
                    and b"libvirt qemu hook for GPU switching" not in native.read_bytes(),
                    "Legacy main QEMU hook detected; restore the administrator's original hook first")

    def previous(self):
        installed = self.root / MANIFEST
        if installed.is_file():
            previous = read(installed)
            for name in previous:
                require(name.startswith((LIB + "/", WEB + "/")) and ".." not in Path(name).parts, "Invalid installation manifest")
            return {k: [v] for k, v in previous.items()} | {MANIFEST: [digest(installed)]}
        return read(self.source / LIB / "legacy.json") if self.source else {}

    def apply(self, remove=False):
        old = self.previous()
        current = {} if remove else read(self.source / MANIFEST)
        if not remove:
            runtime = self.source / LIB / "python"
            if runtime.is_dir():
                for path in sorted(runtime.rglob("*")):
                    if path.is_file():
                        current[path.relative_to(self.source).as_posix()] = digest(path)
                write(self.source / MANIFEST, current)
            current[MANIFEST] = digest(self.source / MANIFEST)
            for name, expected in current.items():
                require(digest(self.source / name) == expected, "Package integrity failure: " + name)
        self.check_hooks()
        legacy = not (self.root / MANIFEST).exists() and any((self.root / k).exists() for k in old)
        if legacy:
            pidfile = self.root / "var/run/gpu-switch-manager/docker-event-monitor.pid"
            if pidfile.exists():
                pid = pidfile.read_text().strip()
                require(pid.isdigit() and not (self.root / "proc" / pid).exists(), "Inspect and stop the old Docker monitor before migration")
            for p in (self.root / "sys/bus/pci/devices").glob("*"):
                if (p / "class").read_text().startswith("0x03"):
                    require((p / "driver").resolve().name != "vfio-pci", "Complete the existing GPU assignment before legacy migration")
        if self.s.settings.exists():
            cfg = validate(read(self.s.settings))
            if remove or (self.root / MANIFEST).exists():
                require(not any(self.s.leases.glob("*.json")), "Outstanding GPU transaction; finish release or recovery first")
                for g in cfg["gpus"].values():
                    if not g["protected"]:
                        slot = self.root / "sys/bus/pci/devices" / g["pci"]
                        require((slot / "driver").resolve().name != "vfio-pci", "An enrolled GPU still uses VFIO")
        elif any(self.s.leases.glob("*.json")):
            raise Refused("Missing configuration for outstanding transactions")
        if remove:
            require(self.mounted or not (self.s.config / "hook-registered").exists(),
                    "Start the array with VM Manager enabled so its hook can be removed from the mounted image")
        # Resolve ownership before altering any file. Unknown files are preserved;
        # unknown legacy web endpoints would remain executable, so refuse migration.
        for folder in (LIB, WEB):
            base = self.root / folder
            require(not base.is_symlink(), "Foreign symlink at " + folder)
            for p in base.rglob("*"):
                name = p.relative_to(self.root).as_posix()
                require(not p.is_symlink(), "Unexpected symlink: " + name)
                if legacy and p.is_file():
                    require(name in old, "Review unrecognized legacy file: " + name)
        touched = set(current)
        for name, hashes in old.items():
            p = self.root / name
            if p.exists():
                require(not p.is_symlink() and p.is_file() and digest(p) in hashes, "Review locally modified file: " + name)
                touched.add(name)
        for name in current:
            require(name in old or not os.path.lexists(self.root / name), "Unowned destination: " + name)

        hook = self.root / HOOK
        old_link = os.readlink(hook) if hook.is_symlink() else None
        old_hook = hook.read_bytes() if hook.is_file() and old_link is None else None
        with tempfile.TemporaryDirectory(dir=self.s.run) as tmp:
            backups = Path(tmp)
            changed = []
            try:
                for name in sorted(touched):
                    dest = self.root / name
                    if name in current and dest.exists() and digest(dest) == current[name]:
                        continue
                    backup = backups / name
                    if dest.exists():
                        backup.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(dest, backup)
                    changed.append(name)
                    if name in current:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as new:
                            stage = Path(new.name)
                        try:
                            shutil.copy2(self.source / name, stage)
                            stage.replace(dest)
                        finally:
                            stage.unlink(missing_ok=True)
                    else:
                        dest.unlink()
                if remove:
                    hook.unlink(missing_ok=True)
                    (self.s.config / "hook-registered").unlink(missing_ok=True)
                else:
                    if not self.s.settings.exists():
                        self.s.change(DEFAULT.copy())
                    self.register()
            except Exception:
                if old_hook is not None:
                    if not hook.is_file() or hook.read_bytes() != old_hook:
                        hook.write_bytes(old_hook)
                        hook.chmod(0o755)
                elif old_link is not None:
                    if not hook.is_symlink():
                        hook.unlink(missing_ok=True)
                        hook.symlink_to(old_link)
                else:
                    hook.unlink(missing_ok=True)
                for name in reversed(changed):
                    dest, backup = self.root / name, backups / name
                    if backup.exists():
                        shutil.copy2(backup, dest)
                    else:
                        dest.unlink(missing_ok=True)
                raise
        for name in sorted(touched, key=lambda n: len(Path(n).parts), reverse=True):
            parent = (self.root / name).parent
            while parent.is_relative_to(self.root / WEB) or parent.is_relative_to(self.root / LIB):
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
        if legacy:
            for name in ("javascript", "styles", "templates"):
                directory = self.root / WEB / name
                if directory.is_dir() and not any(directory.iterdir()):
                    directory.rmdir()
        if remove:
            for p in self.s.run.iterdir():
                if p.name in ("history.json", "history.lock", "probe.lock", "array-stopping") or re.fullmatch(
                        r"(?:gpu-[A-Za-z0-9_.-]+|definition-[a-f0-9-]{36})\.lock", p.name):
                    p.unlink(missing_ok=True)
            (self.s.run / "maintenance.lock").unlink(missing_ok=True)
            if not any(self.s.run.iterdir()):
                self.s.run.rmdir()


def main():
    require(os.geteuid() == 0 and len(sys.argv) == 2, "Root and an installer operation are required")
    action = sys.argv[1]
    require(action in ("install", "remove", "register"), "Invalid installer operation")
    if action == "install":
        for program in ("docker", "virsh", "lspci", "php"):
            require(shutil.which(program), "Missing required command: " + program)
        version = subprocess.check_output(["virsh", "--version"], text=True, timeout=5).strip()
        require(tuple(map(int, version.split(".")[:2])) >= (6, 5), "libvirt 6.5 or later required")
        require(int(subprocess.check_output(["php", "-r", "echo PHP_VERSION_ID;"], timeout=5)) >= 80000, "PHP 8 or later required")
        require(subprocess.run(["php", "-r", "exit(extension_loaded('simplexml') ? 0 : 1);"], timeout=5).returncode == 0,
                "PHP SimpleXML is required")
        manifests = Path("/boot/config/plugins").glob("gpu-switch-manager-*.plg")
        require(not list(manifests), "Archive version-named legacy .plg files outside /boot/config/plugins before installation")
    source = Path(__file__).resolve().parents[4] if action == "install" else None
    deploy = Installer(Path("/"), source)
    with deploy.s.lock("maintenance"):
        if action == "register":
            require(deploy.register(), "Libvirt image is not mounted")
        else:
            deploy.apply(remove=action == "remove")
            if action == "install":
                version = read(deploy.root / LIB / "release.json")["version"]
                registration = "hook registered" if deploy.mounted else "hook registration deferred until the libvirt image mounts"
                print(f"GPU Switch Manager {version} installed; {registration}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, Refused) as exc:
        sys.exit(str(exc))
