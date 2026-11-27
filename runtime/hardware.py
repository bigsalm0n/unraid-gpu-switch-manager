"""Read-only Linux device and workload adapters. All commands use argument vectors."""
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
import xml.etree.ElementTree as ET

from model import Refused, PCI, UUID, match, require


def command(*args, timeout=12):
    try:
        result = subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout,
                                env={**os.environ, "LC_ALL": "C"})
    except subprocess.CalledProcessError as e:
        raise Refused(f"{args[0]} {args[1]}: {e.stderr.strip()[:700] or e.returncode}") from e
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Refused(str(e)) from e
    return result.stdout


def field(path):
    return path.read_text().strip()


class Hardware:
    def __init__(self, sys=Path("/sys"), proc=Path("/proc"), dev=Path("/dev"), run=command):
        self.sys, self.proc, self.dev, self.run = sys, proc, dev, run
        self.pci = sys / "bus/pci/devices"

    def info(self, pci):
        require(match(pci, PCI), "Invalid PCI address")
        slot = self.pci / pci
        require(field(slot / "class").startswith("0x03"), "Expected graphics controller at " + pci)
        functions, drivers, groups, neighbors, reset_peers = {}, {}, {}, {}, {}
        for p in sorted(self.pci.glob(pci[:-1] + "?")):
            bdf = p.name
            functions[bdf[-1]] = ":".join(field(p / f).removeprefix("0x") for f in
                                          ("class", "vendor", "device", "subsystem_vendor", "subsystem_device"))
            drivers[bdf] = (p / "driver").resolve().name if (p / "driver").exists() else None
            group = p / "iommu_group"
            groups[bdf] = group.resolve().name if group.exists() else None
            neighbors[bdf] = sorted(n.name for n in (group / "devices").glob("*"))
            bridge = p.resolve().parent
            reset_peers[bdf] = sorted(n.name for n in bridge.iterdir() if match(n.name, PCI)) if match(bridge.name, PCI) else []
        data = dict(pci=pci, functions=functions, drivers=drivers, groups=groups, neighbors=neighbors, reset_peers=reset_peers,
                    uuid=None, model=None, temperature=None, memory_used=None, memory_total=None,
                    utilization=None, processes=[], minor=None, error=None)
        if drivers[pci] == "nvidia":
            try:
                xml = ET.fromstring(self.run("nvidia-smi", "-i", pci, "-q", "-x"))
                devices = xml.findall("gpu")
                require(len(devices) == 1, "Expected one NVIDIA device")
                gpu = devices[0]
                address = gpu.findtext("pci/pci_bus_id", "").lower()
                domain, location = address.split(":", 1)
                require(f"{int(domain,16):04x}:{location}" == pci, "NVIDIA bus ID mismatch")
                for key, tag in (("uuid","uuid"), ("model","product_name"), ("minor","minor_number"),
                                 ("temperature","temperature/gpu_temp"), ("memory_used","fb_memory_usage/used"),
                                 ("memory_total","fb_memory_usage/total"), ("utilization","utilization/gpu_util")):
                    data[key] = gpu.findtext(tag)
                data["processes"] = [dict(pid=p.findtext("pid"), kind=p.findtext("type"), name=p.findtext("process_name"))
                                     for p in gpu.findall("processes/process_info")]
            except (Refused, ET.ParseError, ValueError) as e:
                data["error"] = str(e)
        data["boot_display"] = (slot / "boot_vga").exists() and field(slot / "boot_vga") == "1"
        return data

    def discover(self):
        devices = []
        for p in sorted(self.pci.iterdir()):
            if match(p.name, PCI) and field(p / "class").startswith("0x03"):
                info = self.info(p.name)
                labels = {}
                for line in self.run("lspci", "-Dvmmnn", "-s", p.name).splitlines():
                    key, sep, value = line.partition(":\t")
                    if sep:
                        labels[key] = value.strip()
                info["description"] = " ".join(labels.get(k, "") for k in ("Vendor", "Device")).strip()
                info["board_vendor"] = labels.get("SVendor")
                devices.append(info)
        return devices

    def snapshot(self, pci, protected):
        data = self.info(pci)
        require(protected or (data["uuid"] and not data["error"]), "Shared GPU requires a working NVIDIA UUID")
        return {k: data[k] for k in ("pci", "uuid", "functions")} | {"protected": protected}

    def verify(self, gpu, host=True):
        require(not gpu["protected"], "GPU is protected")
        data = self.info(gpu["pci"])
        require(data["functions"] == gpu["functions"], "PCI fingerprint changed; rediscover and remap")
        require(not data["boot_display"], "Boot display is reserved for the host")
        for addr, members in data["neighbors"].items():
            require(addr in members and set(members) <= data["drivers"].keys(), "Unsafe IOMMU group at " + addr)
            peers = data["reset_peers"][addr]
            require(addr in peers and set(peers) <= data["drivers"].keys(), "Cannot isolate an upstream bridge reset at " + addr)
            signature = data["functions"][addr[-1]]
            require(addr == gpu["pci"] or signature.startswith("0403"), "Unapproved sibling function at " + addr)
        if host:
            require(data["uuid"] == gpu["uuid"] and data["uuid"] and not data["error"], "Host GPU UUID unavailable or changed")
            require(data["drivers"][gpu["pci"]] == "nvidia" and all(d and d != "vfio-pci" for d in data["drivers"].values()),
                    "Expected a host driver on every function")
            for addr in data["drivers"]:
                override = self.pci / addr / "driver_override"
                require(not override.exists() or field(override) in ("", "(null)"), "Review existing PCI driver override at " + addr)
        return data

    def clients(self, data):
        # Match device numbers, including FDs opened via container-specific paths.
        nodes = []
        if data["drivers"][data["pci"]] == "nvidia":
            require(str(data["minor"]).isdigit(), "NVIDIA minor number unavailable")
            nodes.append(self.dev / ("nvidia" + data["minor"]))
        for addr in data["drivers"]:
            slot = self.pci / addr
            nodes.extend(self.dev / "dri" / p.name for p in (slot / "drm").glob("*") if (p / "dev").exists())
            for card in (slot / "sound").glob("card[0-9]*"):
                nodes.extend((self.dev / "snd").glob("*C" + card.name[4:] + "[!0-9]*"))
                nodes.append(self.dev / "snd" / ("controlC" + card.name[4:]))
        numbers = set()
        for node in nodes:
            st = node.stat()
            require(stat.S_ISCHR(st.st_mode), "Invalid GPU device node: " + str(node))
            numbers.add(st.st_rdev)
        require(numbers, "Target device nodes unavailable")
        pids = {int(p["pid"]) for p in data["processes"] if str(p["pid"]).isdigit()}
        deadline = time.monotonic() + 10
        for proc in self.proc.glob("[0-9]*"):
            require(time.monotonic() < deadline, "Timed out checking target device handles")
            try:
                for fd in (proc / "fd").iterdir():
                    try:
                        st = fd.stat()
                    except (FileNotFoundError, ProcessLookupError):
                        continue
                    if stat.S_ISCHR(st.st_mode) and st.st_rdev in numbers:
                        pids.add(int(proc.name))
                        break
            except (FileNotFoundError, ProcessLookupError):
                continue
        return sorted(pids)

    def containers(self):
        ids = self.run("docker", "ps", "-aq", "--no-trunc").split()
        rows = json.loads(self.run("docker", "inspect", *ids)) if ids else []
        return {c["Name"].lstrip("/"): c for c in rows}

    def selectors(self, container):
        """Capture the physical meaning of selectors that can change at reattach."""
        host = container["HostConfig"]
        tokens = set()
        for env in container["Config"].get("Env") or []:
            if env.startswith("NVIDIA_VISIBLE_DEVICES="):
                tokens.update(env.partition("=")[2].split(","))
        for request in host.get("DeviceRequests") or []:
            if request.get("Driver") in ("nvidia", "cdi") or any("gpu" in c for c in request.get("Capabilities") or []):
                tokens.update(request.get("DeviceIDs") or [])
        references = {}
        for token in tokens:
            index = token.strip().removeprefix("nvidia.com/gpu=")
            if index.isdecimal():
                uid = self.run("nvidia-smi", "-i", index, "--query-gpu=uuid", "--format=csv,noheader").strip()
                require(match(uid, UUID), "Cannot resolve NVIDIA index " + index)
                references["index:" + index] = uid
        for path in device_paths(container):
            if re.fullmatch(r"/dev/nvidia[0-9]+", path):
                ids = [d["uuid"] for d in self.discover() if d["minor"] == path.removeprefix("/dev/nvidia")]
                require(len(ids) == 1 and match(ids[0], UUID), "Cannot resolve Docker device " + path)
                references[path] = ids[0]
            elif path.startswith("/dev/dri/"):
                node = (self.dev / path.removeprefix("/dev/")).resolve().name
                slot = (self.sys / "class/drm" / node / "device").resolve().name
                require(match(slot, PCI), "Cannot resolve DRM mapping " + path)
                references[path] = slot
        return references

    def owner(self, pid, containers):
        try:
            group = field(self.proc / str(pid) / "cgroup")
        except FileNotFoundError:
            return None
        return next((name for name, c in containers.items() if c["Id"] in group), None)

    def process_ids(self, container):
        result = self.run("docker", "top", container["Id"], "-eo", "pid")
        lines = result.splitlines()
        require(lines and lines[0].strip() == "PID" and all(line.strip().isdecimal() for line in lines[1:]),
                "Cannot identify processes for " + container["Name"])
        return {int(line) for line in lines[1:]}

    def assignments(self, data, containers, clients):
        owners = {self.owner(pid, containers) for pid in clients}
        found = {}
        for name, c in containers.items():
            config, host = c["Config"], c["HostConfig"]
            nvidia = data["drivers"][data["pci"]] == "nvidia"
            visible = [v.partition("=")[2] for v in config.get("Env") or [] if v.startswith("NVIDIA_VISIBLE_DEVICES=")]
            tokens = {n.strip() for v in visible for n in v.split(",")}
            reasons = []
            if nvidia and host.get("Runtime") == "nvidia" and not visible:
                reasons.append("NVIDIA runtime with unspecified visibility")
            if host.get("Privileged"):
                reasons.append("privileged device access")
            for req in host.get("DeviceRequests") or []:
                if req.get("Driver") in ("nvidia", "cdi") or any("gpu" in caps for caps in req.get("Capabilities") or []):
                    tokens.update(req.get("DeviceIDs") or ["all"])
            for path in device_paths(c):
                if path == "/dev" or path == "/dev/dri" or path == "/dev/nvidia" + str(data["minor"]):
                    reasons.append(path)
                elif path.startswith("/dev/dri/"):
                    node = (self.sys / "class/drm" / Path(path).resolve().name / "device").resolve().name
                    if node == data["pci"] or not match(node, PCI):
                        reasons.append(path)
            for t in (tokens if nvidia else set()) - {"", "none", "void"}:
                if t == data["uuid"] or not re.fullmatch(r"GPU-[a-fA-F0-9-]{36}", t):
                    reasons.append("visible " + t)
            if name in owners:
                reasons.append("active client")
            if reasons:
                found[name] = reasons
        return found


def device_paths(container):
    return [d["PathOnHost"] for d in container["HostConfig"].get("Devices") or []] + [
        d["Source"] for d in container.get("Mounts") or [] if d.get("Type") == "bind"]
