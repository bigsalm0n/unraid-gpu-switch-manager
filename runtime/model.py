"""Versioned configuration and durable ownership records."""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
import time

CONFIG = Path("/boot/config/plugins/gpu-switch-manager")
RUN = Path("/run/gpu-switch-manager")
PCI = r"[0-9a-f]{4}:[0-9a-f]{2}:[01][0-9a-f]\.[0-7]"
UUID = r"GPU-[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}"
NAME = r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}"
VM = r"[a-zA-Z0-9][a-zA-Z0-9 _.()-]{0,126}"
DEFAULT = {"version": 2, "enabled": False, "gpus": {}, "profiles": {}}


class Refused(Exception):
    pass


def require(condition, message):
    if not condition:
        raise Refused(message)


def match(value, pattern):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def keys(obj, names):
    require(isinstance(obj, dict) and obj.keys() == set(names.split()), "Unexpected or missing fields: " + names)


def ordered(items):
    todo = {i["name"]: i for i in items}
    result = []
    while todo:
        ready = [n for n, item in todo.items() if not set(item["after"]).intersection(todo)]
        require(ready, "Container dependencies form a cycle")
        result.extend(todo.pop(n) for n in ready)
    return result


def validate(cfg):
    keys(cfg, "version enabled gpus profiles")
    require(type(cfg["version"]) is int and cfg["version"] == 2 and type(cfg["enabled"]) is bool, "Expected configuration version 2 and boolean enabled")
    gpus, profiles = cfg["gpus"], cfg["profiles"]
    require(isinstance(gpus, dict) and isinstance(profiles, dict), "Invalid configuration maps")
    addresses, uuids, assignments, stoppable = set(), set(), {}, set()
    for name, gpu in gpus.items():
        keys(gpu, "pci uuid functions protected")
        require(match(name, NAME) and match(gpu["pci"], PCI) and type(gpu["protected"]) is bool, "Invalid GPU record")
        require(gpu["pci"][:-1] not in addresses, "Duplicate physical slot")
        addresses.add(gpu["pci"][:-1])
        uid = gpu["uuid"]
        require((gpu["protected"] and uid is None) or (match(uid, UUID) and uid not in uuids), "Missing or duplicate NVIDIA identity")
        uuids.add(uid)
        funcs = gpu["functions"]
        require(isinstance(funcs, dict) and gpu["pci"][-1] in funcs, "Missing graphics fingerprint")
        for number, signature in funcs.items():
            require(match(number, "[0-7]") and match(signature, r"[0-9a-f]{6}(?::[0-9a-f]{4}){4}"), "Invalid PCI fingerprint")
        require(funcs[gpu["pci"][-1]].startswith("03"), "Expected display controller")
    for vm, profile in profiles.items():
        keys(profile, "gpu containers")
        gpu, items = profile["gpu"], profile["containers"]
        require(match(vm, VM) and match(gpu, NAME) and gpu in gpus and not gpus[gpu]["protected"], "Invalid VM or protected/missing GPU")
        require(isinstance(items, list) and len(items) <= 64, "Invalid container list")
        names = set()
        for item in items:
            keys(item, "name policy after")
            n = item["name"]
            require(match(n, NAME) and n not in names and item["policy"] in ("restore", "stop", "leave"), "Invalid or duplicate container")
            assignments.setdefault(n, set()).add(gpu)
            if item["policy"] != "leave":
                stoppable.add(n)
            names.add(n)
        for item in items:
            deps = item["after"]
            require(isinstance(deps, list) and all(match(d, NAME) and d in names for d in deps), "Unknown dependency")
        ordered(items)
    for name in stoppable:
        require(len(assignments[name]) == 1, "Container with a stop policy belongs to multiple GPUs: " + name)
    return cfg


def read(path):
    return json.loads(path.read_text(encoding="utf8"))


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, mode="w", delete=False, encoding="utf8") as f:
        try:
            json.dump(value, f, sort_keys=True, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
            f.close()
            os.replace(f.name, path)
            sync_directory(path.parent)
        finally:
            Path(f.name).unlink(missing_ok=True)


class Store:
    def __init__(self, config=CONFIG, run=RUN):
        self.config, self.run = config, run
        self.settings = config / "sharing.json"
        self.leases = config / "transactions"

    def cfg(self):
        return validate(read(self.settings))

    def journal(self, gpu):
        require(match(gpu, NAME), "Invalid GPU key")
        return self.leases / (gpu + ".json")

    @contextlib.contextmanager
    def lock(self, name, shared=False):
        self.run.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.run / (name + ".lock")
        with path.open("a") as lock:
            end = time.monotonic() + 4
            mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
            while True:
                try:
                    fcntl.flock(lock, mode | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    require(time.monotonic() < end, "Another operation holds " + name)
                    time.sleep(0.05)
            require(path.exists() and os.path.samestat(os.fstat(lock.fileno()), path.stat()),
                    "Runtime was removed during this operation")
            yield

    def log(self, gpu, vm, state, message):
        record = dict(time=int(time.time()), gpu=gpu, vm=vm, state=state, message=message)
        with self.lock("history"):
            path = self.run / "history.json"
            history = read(path) if path.exists() else []
            write(path, (history + [record])[-200:])

    def change(self, cfg):
        require(not any(self.leases.glob("*.json")), "Resolve outstanding transactions before editing configuration")
        validate(cfg)
        write(self.settings, cfg)
