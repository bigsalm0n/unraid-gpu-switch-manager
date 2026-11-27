from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from threading import Barrier

from test_lifecycle import Rig, domain
from php_bridge import Refused


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

    def test_shared_reset_bridge_is_refused(self):
        neighbor = (self.hw.pci / self.gaming).resolve().parent / "0000:62:01.0"
        neighbor.mkdir()
        with self.assertRaisesRegex(Refused, "bridge reset"):
            self.hook("prepare")
        self.assertFalse(self.ops)

    def test_assignment_preserves_other_xml_and_is_idempotent(self):
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
            if action == "create":
                return self.observe_probe(args[0])
            self.assertEqual(action, "define")
            self.assertEqual(args[0], "--validate")
            xml = Path(args[-1]).read_text()
            writes.append(xml)
            return ""

        self.virt = virsh
        for _ in range(2):
            self.dispatch("assign", "Windows")
        self.assertEqual(len(writes), 1)
        self.assertIn('passwd="fixture"', xml)
        self.assertIn("<ex:data>original</ex:data>", xml)
        self.assertEqual(set(domain(xml)[2]), {self.gaming, self.gaming[:-1] + "1"})
        self.assertFalse(self.ops)

    def observe_probe(self, path):
        xml = Path(path).read_text()
        return self.dispatch("hook", domain(xml)[0], "prepare", "begin", data=xml)

    def mock_virsh(self, *responses):
        queue = iter(responses)

        def next_response(*args):
            response = next(queue)
            if isinstance(response, Exception):
                raise response
            return response

        self.virt = next_response

    def test_reboot_recovery_requires_inactive_owner_and_matching_hardware(self):
        self.hook("prepare")
        self.mock_virsh(self.domain_id, "paused")
        with self.assertRaisesRegex(Refused, "shut off"):
            self.dispatch("recover", "game")
        self.mock_virsh("", self.domain_id, self.xml())
        with self.assertRaisesRegex(Refused, "running domain"):
            self.dispatch("recover", "game")
        self.mock_virsh(self.domain_id, "shut off", Refused("domain is not running"), "")
        self.assertEqual(self.dispatch("array-start"), {"game": {"restored": "game"}})
        self.assertEqual(self.dispatch("recover", "game"), {"pending": False})
        (self.store.run / "history.json").unlink()
        self.assertEqual(self.dispatch("logs")[-1]["state"], "HOST_AVAILABLE")

    def test_prelaunch_integrity(self):
        self.hook("prepare")
        self.bound()
        self.hook("start")
        self.assertEqual(self.m.lease("game", self.cfg)["state"], "VFIO_BOUND")

    def test_changed_numeric_selector_blocks_container_restore(self):
        self.cs["voice"]["Config"]["Env"] = ["NVIDIA_VISIBLE_DEVICES=1"]
        run = self.command
        mapping = [self.uuids[self.gaming]]

        def selector(*args, **kwargs):
            return mapping[0] if args[:3] == ("nvidia-smi", "-i", "1") else run(*args, **kwargs)

        self.command = selector
        self.hook("prepare")
        mapping[0] = self.uuids[self.ai]
        with self.assertRaisesRegex(Refused, "mapping changed"):
            self.hook("release")
        self.assertFalse(any(op == "start" for op, name in self.ops))

    def test_unresolved_cgroup_cross_checked_with_docker_top(self):
        self.client(303, "voice", self.ai)
        (self.hw.proc / "303/cgroup").write_text("0::/anonymous-scope")
        run = self.command

        def top(*args, **kwargs):
            if args[:2] == ("docker", "top"):
                return "PID\n303\n" if args[2] == self.cs["voice"]["Id"] else "PID\n"
            return run(*args, **kwargs)

        self.command = top
        with self.assertRaisesRegex(Refused, "another GPU"):
            self.hook("prepare")
        self.assertFalse(self.ops)

    def test_probe_requires_hook_execution(self):
        self.virt = lambda action, path, option: self.observe_probe(path)
        self.assertTrue(self.dispatch("probe")["verified"])
        self.assertEqual([call[0] for call in self.virt_commands], ["create"])
        self.virt_commands.clear()
        self.virt = lambda *args: ""
        with self.assertRaisesRegex(Refused, "inactive"):
            self.dispatch("probe")
        self.assertEqual([call[0] for call in self.virt_commands], ["create", "destroy"])

    def test_configuration_revision_and_dashboard_diagnostics(self):
        self.mock_virsh("Windows\nControl guest\n", "Windows shut off", "")
        status = self.dispatch("diagnostics")
        self.assertEqual(status["vms"], ["Windows", "Control guest"])
        self.assertEqual(len(status["devices"]), 2)
        self.assertEqual(status["config"], self.cfg)
        self.cfg["profiles"]["Windows"]["automatic"] = False
        self.dispatch("import", "--revision", status["revision"], data=json.dumps(self.cfg))
        self.assertEqual(self.dispatch("export"), self.cfg)
        with self.assertRaisesRegex(Refused, "edited"):
            self.dispatch("import", "--revision", status["revision"], data=json.dumps(self.cfg))
        with self.assertRaisesRegex(Refused, "Automatic sharing is disabled"):
            self.hook("prepare")
        self.assertEqual(self.dispatch("progress"), {})

    def test_manual_start_with_automatic_disabled_and_graceful_shutdown(self):
        self.cfg["enabled"] = False
        self.store.change(self.cfg)
        state = "shut off"

        def virt(action, *args):
            nonlocal state
            if action == "dumpxml":
                return self.xml()
            if action == "domstate":
                return state
            if action == "create":
                return self.observe_probe(args[0])
            if action == "start":
                self.hook("prepare")
                self.bound()
                self.hook("started")
                state = "running"
            else:
                self.assertEqual(action, "shutdown")
                state = "shut off"
                self.driver(self.gaming, "nvidia")
                self.driver(self.gaming[:-1] + "1", "snd_hda_intel")
                self.hook("release")
            return ""

        self.virt = virt
        self.assertEqual(self.dispatch("start", "Windows")["result"], "VM_ACTIVE")
        self.assertEqual(self.dispatch("progress")["game"]["vm"], "Windows")
        self.assertIn("shutdown requested", self.dispatch("shutdown", "Windows")["result"])
        self.assertFalse(self.dispatch("progress"))

    def test_manual_start_failure_recovers_prepared_workloads(self):
        def virt(action, *args):
            if action == "dumpxml":
                return self.xml()
            if action == "domstate":
                return "shut off"
            if action == "create":
                return self.observe_probe(args[0])
            if action == "start":
                self.hook("prepare")
                raise Refused("synthetic VM startup failure")
            self.assertEqual(action, "list")
            return ""

        self.virt = virt
        with self.assertRaisesRegex(Refused, "startup failure"):
            self.dispatch("start", "Windows")
        self.assertFalse(self.dispatch("progress"))
        self.assertEqual(self.ops[-2:], [("start", "voice"), ("start", "dependent")])
