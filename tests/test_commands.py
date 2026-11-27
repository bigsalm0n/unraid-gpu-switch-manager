from concurrent.futures import ThreadPoolExecutor
import contextlib
import importlib.machinery
import importlib.util
import io
import json
from pathlib import Path
import sys
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import patch

from test_lifecycle import Rig, ROOT
from lifecycle import domain
from model import Refused

loader = importlib.machinery.SourceFileLoader("gpuctl", str(ROOT / "runtime/gpuctl"))
spec = importlib.util.spec_from_loader(loader.name, loader)
ctl = importlib.util.module_from_spec(spec)
loader.exec_module(ctl)


class Commands(Rig):
    def test_concurrent_start_requests_have_one_owner(self):
        barrier = Barrier(2)
        def start(vm):
            barrier.wait(timeout=3)
            try:
                self.hook("prepare", vm)
                return vm
            except Refused:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            winners = [v for v in pool.map(start, ["Windows", "Second"]) if v]
        self.assertEqual(len(winners), 1)
        self.assertEqual(self.m.lease("game", self.cfg)["vm"], winners[0])
        self.assertEqual(len(self.ops), 2)

    def test_independent_group_with_shared_reset_bridge_is_refused(self):
        neighbor = (self.hw.pci / self.gaming).resolve().parent / "0000:62:01.0"
        neighbor.mkdir()
        with self.assertRaisesRegex(Refused, "bridge reset"):
            self.hook("prepare")
        self.assertFalse(self.ops)

    def test_config_only_assignment_preserves_other_xml_and_is_idempotent(self):
        xml = (f"<domain xmlns:ex='urn:test'><name>Windows</name><uuid>{self.domain_id}</uuid>"
               "<metadata><ex:data>original</ex:data></metadata><devices><graphics type='vnc' passwd='fixture'/>"
               "</devices></domain>")
        writes = []
        def virsh(action, *args):
            nonlocal xml
            if action == "domstate":
                return "shut off"
            if action == "dumpxml":
                return xml
            self.assertEqual(action, "define")
            self.assertEqual(args[0], "--validate")
            xml = Path(args[-1]).read_text()
            writes.append(xml)
        with patch.object(ctl, "virsh", side_effect=virsh):
            for _ in range(2):
                ctl.assign(self.m, self.cfg, "game", "Windows", self.domain_id)
        self.assertEqual(len(writes), 1)
        self.assertIn('passwd="fixture"', xml)
        self.assertIn("<ex:data>original</ex:data>", xml)
        self.assertEqual(set(domain(xml)[2]), {self.gaming, self.gaming[:-1] + "1"})
        self.assertFalse(self.ops)

    def test_recovery_requires_inactive_owner_and_matching_hardware(self):
        self.hook("prepare")
        with patch.object(ctl, "virsh", side_effect=[self.domain_id, "paused"]), self.assertRaisesRegex(Refused, "shut off"):
            ctl.recovery(self.m, "game")
        with patch.object(ctl, "virsh", side_effect=["", self.domain_id, self.xml()]), self.assertRaisesRegex(Refused, "running domain"):
            ctl.recovery(self.m, "game")
        with patch.object(ctl, "virsh", side_effect=[self.domain_id, "shut off", Refused("domain is not running"), ""]):
            self.assertEqual(ctl.recovery(self.m, "game"), {"restored": "game"})
        self.assertEqual(ctl.recovery(self.m, "game"), {"pending": False})

    def test_hook_filter_silent_and_prelaunch_integrity(self):
        self.hook("prepare")
        self.bound()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.hook("start")
        self.assertEqual(buf.getvalue(), "")
        self.assertEqual(self.m.lease("game", self.cfg)["state"], "VFIO_BOUND")

    def test_changed_numeric_selector_blocks_container_restore(self):
        self.cs["voice"]["Config"]["Env"] = ["NVIDIA_VISIBLE_DEVICES=1"]
        run = self.hw.run
        mapping = [self.uuids[self.gaming]]
        def selector(*args, **kwargs):
            if args[:3] == ("nvidia-smi", "-i", "1"):
                return mapping[0]
            return run(*args, **kwargs)
        with patch.object(self.hw, "run", side_effect=selector):
            self.hook("prepare")
            mapping[0] = self.uuids[self.ai]
            with self.assertRaisesRegex(Refused, "mapping changed"):
                self.hook("release")
        self.assertFalse(any(op == "start" for op, name in self.ops))

    def test_unresolved_cgroup_cross_checked_with_docker_top(self):
        self.client(303, "voice", self.ai)
        (self.hw.proc / "303/cgroup").write_text("0::/anonymous-scope")
        with patch.object(self.hw, "process_ids", side_effect=lambda c: {303} if c["Name"] == "/voice" else set()):
            with self.assertRaisesRegex(Refused, "another GPU"):
                self.hook("prepare")
        self.assertFalse(self.ops)

    def test_real_probe_requires_hook_execution(self):
        self.store.run.mkdir(exist_ok=True)
        calls = []
        def create(*args):
            calls.append(args[0])
            if args[0] == "create":
                xml = Path(args[1]).read_bytes()
                name, _, _ = domain(xml)
                req = SimpleNamespace(action="hook", vm=name, event="prepare", phase="begin")
                with patch.object(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(xml))):
                    ctl.dispatch(self.m, req)
        with patch.object(ctl, "virsh", side_effect=create):
            self.assertTrue(ctl.proof(self.m)["verified"])
        self.assertEqual(calls, ["create"])
        with patch.object(ctl, "virsh", return_value="") as run, self.assertRaisesRegex(Refused, "inactive"):
            ctl.proof(self.m)
        self.assertEqual([c.args[0] for c in run.call_args_list], ["create", "destroy"])

    def test_diagnostics_and_progress_expose_real_owner(self):
        self.hook("prepare")
        with patch.object(ctl, "virsh", side_effect=["Windows\nControl guest\n", self.domain_id, self.xml()]):
            result = ctl.dispatch(self.m, SimpleNamespace(action="diagnostics"))
        gpu = next(d for d in result["devices"] if d["pci"] == self.gaming)
        self.assertEqual(gpu["owner"], "Windows")
        self.assertEqual(gpu["state"], "PREPARING_VM")
        self.assertEqual(ctl.dispatch(self.m, SimpleNamespace(action="progress"))["game"]["vm"], "Windows")
