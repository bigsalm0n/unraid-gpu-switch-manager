import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid

from php_bridge import Refused, bridge, read, write


DEFAULT = {"version": 2, "enabled": False, "gpus": {}, "profiles": {}}


class Adapter:
    def __init__(self, root, target, command=None):
        self.root, self.target, self.command = root, target, command

    def __getattr__(self, method):
        return lambda *args: bridge(self.root, self.target, *args, method=method, command=self.command)


class Store(Adapter):
    def __init__(self, root):
        super().__init__(root, "store")
        self.run, self.config = root / "ram", root / "flash"
        self.settings = self.config / "sharing.json"

    def journal(self, key):
        return self.config / "transactions" / (key + ".json")


def validate(cfg):
    return bridge("/", "validate", cfg)


def domain(xml):
    return bridge("/", "domain", xml)


class Rig(unittest.TestCase):
    ai = "0000:31:00.0"
    gaming = "0000:62:00.0"
    domain_id = "00000000-0000-4000-8000-000000000003"

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.hw = Adapter(self.root, "hardware", lambda *a, **kw: self.command(*a, **kw))
        self.hw.sys, self.hw.proc, self.hw.dev = (self.root / name for name in ("sys", "proc", "dev"))
        self.hw.pci = self.hw.sys / "bus/pci/devices"
        self.store = Store(self.root)
        self.m = Adapter(self.root, "manager", lambda *a, **kw: self.command(*a, **kw))
        self.virt_commands = []
        self.virt = None
        self.ops = []
        self.uuids = {}
        self.procs = {}
        self.broken = None
        self.stop_fails = False
        self.cs = {}
        for i, (pci, group) in enumerate(((self.ai, 31), (self.gaming, 45))):
            self.uuids[pci] = "GPU-" + str(uuid.UUID(int=i + 1))
            for f in (0, 1):
                addr = pci[:-1] + str(f)
                p = self.hw.sys / "devices" / f"0000:00:0{i+1}.0" / addr
                p.mkdir(parents=True)
                self.hw.pci.mkdir(parents=True, exist_ok=True)
                (self.hw.pci / addr).symlink_to(p)
                for k, v in dict(vendor="10de", device=("1234", "5678")[f], subsystem_vendor="abcd",
                                 subsystem_device=("a001", "a002")[i], **{"class": ("030000", "040300")[f]}).items():
                    (p / k).write_text("0x" + v)
                g = self.hw.sys / "kernel/iommu_groups" / str(group + f)
                (g / "devices").mkdir(parents=True)
                (g / "devices" / addr).symlink_to(p)
                (p / "iommu_group").symlink_to(g)
                self.driver(addr, ("nvidia", "snd_hda_intel")[f])
        self.hw.dev.mkdir(parents=True)
        (self.hw.dev / "nvidia0").symlink_to("/dev/null")
        (self.hw.dev / "nvidia1").symlink_to("/dev/zero")
        for i, (n, pci, active) in enumerate((("infer", self.ai, True), ("voice", self.gaming, True),
                                               ("dependent", None, True), ("stopped", None, False))):
            self.cs[n] = {"Id": ("%064x" % (i + 1)), "Name": "/" + n, "State": {"Running": active},
                          "Config": {"Env": ["NVIDIA_VISIBLE_DEVICES=" + self.uuids[pci]] if pci else []}, "HostConfig": {}}
        self.client(101, "infer", self.ai)
        cfg = copy.deepcopy(DEFAULT)
        cfg["enabled"] = True
        cfg["gpus"] = {"ai": self.hw.snapshot(self.ai, True), "game": self.hw.snapshot(self.gaming, False)}
        items = [dict(name="dependent", policy="restore", after=["voice"]),
                 dict(name="voice", policy="restore", after=[]), dict(name="stopped", policy="restore", after=[])]
        cfg["profiles"] = {n: dict(gpu="game", containers=copy.deepcopy(items)) for n in ("Windows", "Second")}
        self.store.change(cfg)
        self.cfg = cfg

    def driver(self, addr, driver):
        p = self.hw.pci / addr / "driver"
        p.unlink(missing_ok=True)
        dst = self.hw.sys / "drivers" / driver
        dst.mkdir(parents=True, exist_ok=True)
        p.symlink_to(dst)

    def client(self, pid, name, addr):
        p = self.hw.proc / str(pid)
        (p / "fd").mkdir(parents=True, exist_ok=True)
        (p / "cgroup").write_text("0::/docker/" + self.cs[name]["Id"])
        (p / "fd/3").symlink_to(self.hw.dev / ("nvidia0" if addr == self.ai else "nvidia1"))
        self.procs[pid] = addr

    def command(self, *args, timeout=0):
        if args[0] == "virsh":
            self.assertIsNotNone(self.virt, "Hook attempted to call libvirt")
            self.virt_commands.append(args[3:])
            return self.virt(*args[3:])
        if args[0] == "lspci":
            return "Vendor:\tTest vendor\nDevice:\tTest GPU"
        if args[0] == "nvidia-smi":
            self.assertEqual(args[1:], ("-i", args[2], "-q", "-x"))
            pci = args[2]
            if self.broken == pci:
                raise Refused("NVIDIA unavailable")
            minor = 0 if pci == self.ai else 1
            processes = "".join(f"<process_info><pid>{pid}</pid><type>G</type></process_info>" for pid, a in self.procs.items() if a == pci)
            return (f"<nvidia_smi_log><gpu><pci><pci_bus_id>0000{pci}</pci_bus_id></pci><uuid>{self.uuids[pci]}</uuid>"
                    f"<minor_number>{minor}</minor_number><processes>{processes}</processes></gpu></nvidia_smi_log>")
        self.assertEqual(args[0], "docker")
        if args[1] == "ps":
            return "\n".join(c["Id"] for c in self.cs.values())
        if args[1] == "inspect":
            return json.dumps(list(self.cs.values()))
        if args[1] == "top":
            return "PID\n"
        name = next(n for n, c in self.cs.items() if c["Id"] == args[-1])
        self.ops.append((args[1], name))
        if args[1] == "stop" and self.stop_fails and name == "voice":
            raise Refused("Container stop failed")
        self.cs[name]["State"]["Running"] = args[1] == "start"
        return ""

    def xml(self, vm="Windows", pci=None):
        pci = pci or self.gaming
        devices = "".join(f"<hostdev type='pci' managed='yes'><source><address domain='0x0000' bus='0x{pci[5:7]}' slot='0x00' function='0x{f}'/></source></hostdev>" for f in (0, 1))
        return f"<domain><name>{vm}</name><uuid>{self.domain_id}</uuid><devices>{devices}</devices></domain>"

    def dispatch(self, *args, data=""):
        return bridge(self.root, "dispatch", args, data, command=lambda *a, **kw: self.command(*a, **kw))

    def hook(self, event, vm="Windows"):
        self.dispatch("hook", vm, event, "end" if event == "release" else "begin", data=self.xml(vm))

    def bound(self):
        for f in ("0", "1"):
            self.driver(self.gaming[:-1] + f, "vfio-pci")


class LifecycleTests(Rig):
    def test_identical_ids_target_specific_clients_and_audio(self):
        cards = self.hw.discover()
        self.assertEqual(len(cards), 2)
        self.assertEqual(self.hw.clients(cards[0]), [101])
        self.assertEqual(self.hw.clients(cards[1]), [])
        self.assertEqual(set(cards[1]["groups"].values()), {"45", "46"})
        self.hook("prepare")
        self.assertEqual(self.ops, [("stop", "dependent"), ("stop", "voice")])
        self.bound()
        self.hook("started")
        self.assertEqual(self.m.lease("game", self.cfg)["state"], "VM_ACTIVE")
        self.driver(self.gaming, "nvidia")
        self.driver(self.gaming[:-1] + "1", "snd_hda_intel")
        self.hook("release")
        self.hook("release")
        self.assertEqual(self.ops[2:], [("start", "voice"), ("start", "dependent")])
        self.assertTrue(self.cs["infer"]["State"]["Running"])
        self.assertFalse(self.store.journal("game").exists())

    def test_protected_profile_and_unprofiled_domain(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["profiles"]["Windows"]["gpu"] = "ai"
        with self.assertRaises(Refused):
            validate(cfg)
        for pci in (self.ai, self.gaming):
            with self.assertRaisesRegex(Refused, "reserved"):
                self.m.hook("Other", "prepare", "begin", self.xml("Other", pci))
        self.assertFalse(self.ops)

    def test_all_gpu_container_needs_policy(self):
        self.cs["infer"]["Config"]["Env"] = ["NVIDIA_VISIBLE_DEVICES=all"]
        with self.assertRaisesRegex(Refused, "infer"):
            self.hook("prepare")
        self.assertFalse(self.ops)

    def test_protected_gpu_work_cannot_be_stopped(self):
        self.client(102, "voice", self.ai)
        with self.assertRaisesRegex(Refused, "another GPU"):
            self.hook("prepare")
        self.assertFalse(self.ops)

    def test_unowned_graphics_and_open_handles_block(self):
        self.client(202, "infer", self.gaming)
        del self.procs[202]  # Open device handle only, absent from NVML.
        self.assertIn(202, self.hw.clients(self.hw.info(self.gaming)))
        with self.assertRaises(Refused):
            self.hook("prepare")
        self.assertFalse(self.ops)

    def test_sysfs_drm_minor_detects_client_without_host_device_node(self):
        drm = self.hw.pci / self.gaming / "drm/controlD66"
        drm.mkdir(parents=True)
        number = os.stat("/dev/full").st_rdev
        (drm / "dev").write_text(f"{os.major(number)}:{os.minor(number)}")
        proc = self.hw.proc / "212"
        (proc / "fd").mkdir(parents=True)
        (proc / "fd/3").symlink_to("/dev/full")
        self.assertIn(212, self.hw.clients(self.hw.info(self.gaming)))
        with self.assertRaisesRegex(Refused, "unquiesced client"):
            self.hook("prepare")
        self.assertFalse(self.ops)

    def test_still_open_after_stop_rolls_back(self):
        self.client(203, "voice", self.gaming)
        with self.assertRaisesRegex(Refused, "remain open"):
            self.hook("prepare")
        self.assertEqual(self.ops[-2:], [("start", "voice"), ("start", "dependent")])

    def test_failed_container_stop_rolls_back(self):
        self.stop_fails = True
        with self.assertRaisesRegex(Refused, "stop failed"):
            self.hook("prepare")
        self.assertEqual(self.ops[-1], ("start", "dependent"))
        self.assertFalse(self.store.journal("game").exists())

    def test_failed_detach_and_failed_vm_start(self):
        self.hook("prepare")
        with self.assertRaisesRegex(Refused, "VFIO"):
            self.hook("started")
        self.assertEqual(self.m.lease("game", self.cfg)["state"], "ERROR")
        self.hook("release")
        self.assertEqual(self.ops[-1], ("start", "dependent"))

    def test_failed_reattach_blocks_restoration(self):
        self.hook("prepare")
        self.bound()
        with self.assertRaises(Refused):
            self.hook("release")
        self.assertEqual(len(self.ops), 2)
        self.assertEqual(self.m.lease("game", self.cfg)["state"], "ERROR")

    def test_missing_userspace_even_with_host_driver(self):
        self.hook("prepare")
        self.broken = self.gaming
        with self.assertRaises(Refused):
            self.hook("release")
        self.assertEqual(len(self.ops), 2)

    def test_competing_vm_and_its_release(self):
        self.hook("prepare")
        with self.assertRaisesRegex(Refused, "reserved by Windows"):
            self.hook("prepare", "Second")
        self.hook("release", "Second")
        self.assertEqual(self.m.lease("game", self.cfg)["vm"], "Windows")
        self.assertEqual(len(self.ops), 2)

    def test_wrong_fingerprint_and_relocated_uuid(self):
        self.uuids[self.gaming], self.uuids[self.ai] = self.uuids[self.ai], self.uuids[self.gaming]
        with self.assertRaisesRegex(Refused, "UUID"):
            self.hook("prepare")
        card = self.hw.pci / self.gaming
        card.rename(self.hw.pci / "0000:08:00.0")
        with self.assertRaises(Refused):
            self.hook("prepare")
        self.assertFalse(self.ops)

    def test_iommu_neighbor_and_incomplete_xml(self):
        g = self.hw.pci / self.gaming / "iommu_group/devices"
        (g / self.ai).symlink_to(self.hw.pci / self.ai)
        with self.assertRaises(Refused):
            self.hook("prepare")
        (g / self.ai).unlink()
        with self.assertRaisesRegex(Refused, "managed"):
            self.m.hook("Windows", "prepare", "begin", self.xml().replace("managed='yes'", "managed='no'", 1))
        self.assertFalse(self.ops)

    def test_changed_container_id_and_deferred_array_stop(self):
        self.hook("prepare")
        (self.store.run / "array-stopping").touch()
        with self.assertRaisesRegex(Refused, "Array is stopping"):
            self.hook("release")
        (self.store.run / "array-stopping").unlink()
        self.cs["voice"]["Id"] = "f" * 64
        with self.assertRaisesRegex(Refused, "replaced"):
            self.hook("release")
        self.assertEqual(len(self.ops), 2)

    def test_non_restored_stopped_and_leave_policies(self):
        items = self.cfg["profiles"]["Windows"]["containers"]
        next(i for i in items if i["name"] == "voice")["policy"] = "stop"
        next(i for i in items if i["name"] == "dependent")["after"] = []
        self.cs["dependent"]["Config"]["Env"] = ["NVIDIA_VISIBLE_DEVICES=" + self.uuids[self.gaming]]
        items.append(dict(name="infer", policy="leave", after=[]))
        self.store.change(self.cfg)
        self.hook("prepare")
        self.hook("release")
        self.assertEqual(self.ops[-1], ("start", "dependent"))
        self.assertNotIn(("start", "voice"), self.ops)
        self.assertNotIn(("stop", "infer"), self.ops)

    def test_configured_unrelated_container_is_left_running(self):
        self.cfg["profiles"]["Windows"]["containers"].append(dict(name="infer", policy="stop", after=[]))
        self.store.change(self.cfg)
        self.hook("prepare")
        self.hook("release")
        self.assertTrue(self.cs["infer"]["State"]["Running"])
        self.assertFalse(any(name == "infer" for _, name in self.ops))

    def test_dry_run_corrupt_journal_and_configuration_edit(self):
        plan = self.m.prepare(self.cfg, "game", "Windows", self.domain_id, domain(self.xml())[2], True)
        self.assertEqual(plan["gpu"], "game")
        self.assertFalse(self.ops)
        self.hook("prepare")
        with self.assertRaises(Refused):
            self.store.change(self.cfg)
        j = read(self.store.journal("game"))
        j["containers"][0]["name"] = "infer"
        write(self.store.journal("game"), j)
        with self.assertRaisesRegex(Refused, "snapshot"):
            self.hook("release")
        self.assertEqual(len(self.ops), 2)

    def test_reconnect_and_unrelated_vm_no_commands(self):
        self.m.hook("Control guest", "reconnect", "begin", "")
        self.m.hook("Control guest", "prepare", "begin", "<domain><name>Control guest</name><devices/></domain>")
        self.store.settings.write_text("{")
        self.m.hook("Control guest", "prepare", "begin", "<domain><name>Control guest</name><devices/></domain>")
        self.assertFalse(self.ops)

    def test_docker_device_requests_indices_and_explicit_mapping(self):
        self.cs["infer"]["Config"]["Env"] = []
        self.cs["infer"]["HostConfig"] = {"DeviceRequests": [{"Driver": "nvidia", "Count": -1}]}
        data = self.hw.info(self.gaming)
        self.assertIn("infer", self.hw.assignments(data, self.cs, []))
        self.cs["infer"]["HostConfig"] = {"Devices": [{"PathOnHost": "/dev/nvidia1"}]}
        self.assertIn("infer", self.hw.assignments(data, self.cs, []))
        self.cs["infer"]["HostConfig"] = {}
        self.cs["infer"]["Config"]["Env"] = ["NVIDIA_VISIBLE_DEVICES=0"]
        self.assertIn("infer", self.hw.assignments(data, self.cs, []))

    def test_dependency_cycle_rejected(self):
        items = self.cfg["profiles"]["Windows"]["containers"]
        next(i for i in items if i["name"] == "voice")["after"] = ["dependent"]
        with self.assertRaisesRegex(Refused, "cycle"):
            validate(self.cfg)
