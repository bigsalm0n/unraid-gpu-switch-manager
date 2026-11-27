"""Synchronous libvirt lifecycle. This module never connects to libvirt."""
import time
from time import monotonic
import uuid
import xml.etree.ElementTree as ET

from hardware import Hardware
from model import Refused, Store, match, ordered, read, require, sync_directory, write


def domain(xml):
    require(len(xml) <= 4 * 1024 * 1024, "Domain XML too large")
    root = ET.fromstring(xml)
    require(root.tag == "domain", "Expected libvirt domain XML")
    devices = {}
    for dev in root.findall("./devices/hostdev[@type='pci']") + root.findall("./devices/interface[@type='hostdev']"):
        address = dev.find("source/address")
        require(address is not None, "Missing source PCI address")
        values = [int(address.get(key, "0"), 16) for key in ("domain", "bus", "slot", "function")]
        pci = "%04x:%02x:%02x.%x" % tuple(values)
        require(pci not in devices, "Duplicate PCI hostdev")
        driver = dev.find("driver")
        devices[pci] = (dev.tag == "hostdev" and dev.get("managed") == "yes"
                        and (driver is None or driver.get("name", "vfio") == "vfio"))
    return root.findtext("name"), root.findtext("uuid"), devices


def running(container):
    return container["State"]["Running"]


class Manager:
    def __init__(self, store=None, hardware=None):
        self.s = store or Store()
        self.hw = hardware or Hardware()

    def record(self, key, j, state, message):
        j.update(state=state, message=message)
        write(self.s.journal(key), j)
        self.s.log(key, j["vm"], state, message)

    def lease(self, key, cfg):
        path = self.s.journal(key)
        if not path.exists():
            return None
        j = read(path)
        require(isinstance(j, dict) and j.get("gpu") == cfg["gpus"][key] and j.get("key") == key,
                "Journal identity mismatch; inspect transaction")
        profile = cfg["profiles"].get(j.get("vm"))
        require(profile and profile["gpu"] == key, "Journal profile mismatch")
        require(str(uuid.UUID(j["domain"])) == j["domain"] and str(uuid.UUID(j["transaction"])) == j["transaction"],
                "Invalid transaction identity")
        expected = {j["gpu"]["pci"][:-1] + f for f in j["gpu"]["functions"]}
        require(set(j["drivers"]) == expected and all(isinstance(d, str) and d != "vfio-pci" for d in j["drivers"].values()),
                "Invalid original drivers")
        require(j["state"] in {"PREPARING_VM", "VFIO_BOUND", "VM_ACTIVE", "RESTORING_HOST", "ERROR"}, "Invalid journal state")
        items = ordered(profile["containers"])
        require(len(j["containers"]) == len(items), "Incomplete container snapshot")
        for item, row in zip(items, j["containers"]):
            require(row["name"] == item["name"] and row["policy"] == item["policy"] and match(row["id"], "[0-9a-f]{64}")
                    and type(row["running"]) is bool and type(row["attempted"]) is bool
                    and isinstance(row.get("selectors"), dict), "Invalid container snapshot")
        return j

    def target(self, cfg, key):
        gpu = cfg["gpus"][key]
        for other in cfg["gpus"].values():
            if other["protected"] and other["functions"] == gpu["functions"]:
                require(other["uuid"] and other["uuid"] != gpu["uuid"], "Indistinguishable protected GPU; confirm its persistent identity")
        return self.hw.verify(gpu)

    def plan(self, cfg, key, profile, devices):
        gpu = cfg["gpus"][key]
        data = self.target(cfg, key)
        require(set(devices) == set(data["drivers"]) and all(devices.values()),
                "VM must have managed VFIO hostdev entries for exactly the approved PCI functions")
        require(not (self.s.run / "array-stopping").exists(), "Array is stopping")
        containers = self.hw.containers()
        items = ordered(profile["containers"])
        require(all(i["name"] in containers for i in items), "Configured container is missing")
        clients = self.hw.clients(data)
        found = self.hw.assignments(data, containers, clients)
        configured = {item["name"] for item in items}
        unknown = {n for n in found if running(containers[n])} - configured
        require(not unknown, "Set an explicit policy for potential GPU containers: " + ", ".join(sorted(unknown)))
        stopping = {i["name"] for i in items if i["policy"] != "leave" and running(containers[i["name"]])}
        stop_pids = None

        def belongs_to_selected(pid):
            nonlocal stop_pids
            owner = self.hw.owner(pid, containers)
            if owner is not None:
                return owner in stopping
            if stop_pids is None:
                stop_pids = set()
                for name in stopping:
                    stop_pids.update(self.hw.process_ids(containers[name]))
            return pid in stop_pids

        for pid in clients:
            require(belongs_to_selected(pid), f"Target has an unquiesced client PID {pid}")
        if stopping:
            for other in self.hw.discover():
                if other["pci"] == gpu["pci"] or other["drivers"][other["pci"]] in (None, "vfio-pci"):
                    continue
                require(not other["error"], "Cannot check clients on another GPU")
                for pid in self.hw.clients(other):
                    require(not belongs_to_selected(pid),
                            f"A selected container is active on another GPU at {other['pci']}")
        rows = []
        for i in items:
            c = containers[i["name"]]
            require(match(c["Id"], "[0-9a-f]{64}"), "Invalid Docker container identity")
            if i["policy"] != "leave":
                require(not c["State"].get("Paused") and not c["State"].get("Restarting") and not c["HostConfig"].get("AutoRemove"),
                        "Paused, restarting or auto-remove container: " + i["name"])
            references = self.hw.selectors(c) if i["policy"] == "restore" and running(c) else {}
            rows.append(dict(name=i["name"], policy=i["policy"], id=c["Id"], running=running(c), attempted=False,
                             selectors=references))
        return data, rows

    def prepare(self, cfg, key, vm, uid, devices, dry=False):
        require(cfg["enabled"] or dry, "Automatic sharing is disabled for this enrolled GPU")
        require(str(uuid.UUID(uid)) == uid, "Invalid domain UUID")
        existing = self.lease(key, cfg)
        require(not existing, "GPU reserved by " + (existing or {}).get("vm", "") + "; inspect outstanding transaction")
        data, rows = self.plan(cfg, key, cfg["profiles"][vm], devices)
        if dry:
            return dict(gpu=key, vm=vm, drivers=data["drivers"], containers=rows)
        j = dict(transaction=str(uuid.uuid4()), gpu=cfg["gpus"][key], key=key, vm=vm, domain=uid,
                 drivers=data["drivers"], containers=rows)
        self.record(key, j, "PREPARING_VM", "Validated host and captured workload state")
        try:
            for row in reversed(rows):
                if row["running"] and row["policy"] != "leave":
                    row["attempted"] = True
                    write(self.s.journal(key), j)
                    self.hw.run("docker", "stop", "--time", "15", row["id"], timeout=22)
                    self.check_container(row, False)
            data = self.hw.verify(j["gpu"])
            require(not self.hw.clients(data), "Target device handles remain open")
            require(not (self.s.run / "array-stopping").exists(), "Array is stopping")
            self.s.log(key, vm, "PREPARING_VM", "Target idle; allowing managed PCI assignment")
        except Exception as e:
            self.record(key, j, "ERROR", str(e))
            self.restore(key, j)
            raise

    def check_container(self, row, expected=None):
        c = self.hw.containers().get(row["name"])
        require(c and c["Id"] == row["id"], "Container removed or replaced: " + row["name"])
        if expected is not None:
            require(running(c) == expected, "Unexpected running state for " + row["name"])
        return c

    def restore(self, key, j):
        self.record(key, j, "RESTORING_HOST", "Checking original drivers and userspace")
        try:
            end = monotonic() + 12
            while True:
                try:
                    data = self.hw.verify(j["gpu"])
                    require(data["drivers"] == j["drivers"], "Original drivers have not returned")
                    break
                except Refused:
                    if monotonic() >= end:
                        raise
                    time.sleep(0.4)
            require(not (self.s.run / "array-stopping").exists(), "Array stopping; restoration deferred until recovery")
            profile = self.s.cfg()["profiles"][j["vm"]]
            policies = {i["name"]: i for i in profile["containers"]}
            rows = {r["name"]: r for r in j["containers"]}
            for row in j["containers"]:
                if row["running"] and row["attempted"] and row["policy"] == "restore":
                    container = self.check_container(row)
                    require(self.hw.selectors(container) == row["selectors"], "Docker GPU mapping changed for " + row["name"])
                    if not running(container):
                        for dependency in policies[row["name"]]["after"]:
                            self.check_container(rows[dependency], True)
                        self.hw.run("docker", "start", row["id"], timeout=30)
                        self.check_container(row, True)
            self.s.log(key, j["vm"], "HOST_AVAILABLE", "Verified original drivers, UUID and configured restoration")
            self.s.journal(key).unlink()
            sync_directory(self.s.leases)
        except Exception as e:
            self.record(key, j, "ERROR", str(e) + ". Inspect drivers, domain and container IDs before recovery.")
            raise

    def guard(self, cfg, vm, devices):
        # Unprofiled domains can use unrelated devices. Enrolled slots, their IOMMU
        # neighbors and relocated protected display functions still need protection.
        if not devices:
            return
        for key, gpu in cfg["gpus"].items():
            addresses = {gpu["pci"][:-1] + f for f in gpu["functions"]}
            for addr in devices:
                group = self.hw.pci / addr / "iommu_group/devices"
                intersects = addr in addresses or any(m.name in addresses for m in group.glob("*"))
                if intersects:
                    require(not gpu["protected"] and cfg["profiles"].get(vm, {}).get("gpu") == key,
                            f"PCI device {addr} overlaps reserved GPU {key}")
        if vm not in cfg["profiles"]:
            for addr in devices:
                slot = self.hw.pci / addr
                for p in (slot / "iommu_group/devices").glob("*"):
                    candidates = self.hw.pci.glob(p.name[:-1] + "?")
                    for card in candidates:
                        if (card / "class").read_text().strip().startswith("0x03"):
                            data = self.hw.info(card.name)
                            for key, gpu in cfg["gpus"].items():
                                if gpu["uuid"] and data["uuid"]:
                                    resembles = gpu["uuid"] == data["uuid"]
                                else:
                                    resembles = gpu["functions"] == data["functions"]
                                require(not resembles,
                                        "GPU resembles enrolled hardware; confirm profile or remap: " + key)

    def hook(self, vm, event, phase, xml):
        if (event, phase) not in (("prepare", "begin"), ("start", "begin"), ("started", "begin"), ("release", "end")):
            return
        name, uid, devices = domain(xml)
        require(name == vm, "Hook VM name differs from XML")
        if not devices:
            try:
                if vm not in self.s.cfg()["profiles"]:
                    return
            except (Refused, OSError, ValueError, TypeError):
                # No PCI assignment can occur. Keep unrelated services available
                # while an administrator repairs settings.
                return
        with self.s.lock("maintenance", shared=True):
            cfg = self.s.cfg()
            if event == "prepare":
                self.guard(cfg, vm, devices)
            profile = cfg["profiles"].get(vm)
            if not profile:
                return
            key = profile["gpu"]
            with self.s.lock("gpu-" + key):
                if event == "prepare":
                    try:
                        return self.prepare(cfg, key, vm, uid, devices)
                    except Exception as e:
                        self.s.log(key, vm, "REFUSED", str(e))
                        raise
                j = self.lease(key, cfg)
                if not j or (j["vm"], j["domain"]) != (vm, uid):
                    require(event == "release", "Startup has no matching prepared transaction")
                    return
                if event == "release":
                    return self.restore(key, j)
                try:
                    data = self.hw.verify(j["gpu"], host=False)
                    require(devices.keys() == data["drivers"].keys() and all(devices.values()), "Hostdev configuration changed during startup")
                    require(all(d == "vfio-pci" for d in data["drivers"].values()), "Guest started without expected VFIO drivers")
                    if event == "start":
                        self.record(key, j, "VFIO_BOUND", "All approved functions use VFIO before guest launch")
                    else:
                        self.record(key, j, "VM_ACTIVE", "Libvirt started guest and all functions use VFIO")
                except Exception as e:
                    self.record(key, j, "ERROR", str(e))
                    raise
