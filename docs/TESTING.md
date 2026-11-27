# Verification and the test host acceptance

## Automated verification

Run in Linux using Python 3.12+, python3-jsonschema, Bash, ShellCheck, PHP CLI with SimpleXML and Node. Runtime Python supports 3.9+. The test fixture uses the safe tar extraction API available in newer Python versions.

```bash
fixture=$(mktemp)
git show ff1e0ce:gpu-switch-manager-2026.05.13.txz > "$fixture"
GSM_LEGACY_TEST_TXZ="$fixture" bash tools/check.sh
rm -f -- "$fixture"
```

The check runs Bash, PHP and JavaScript syntax checks, ShellCheck, JSON Schema and backend validation, the unit suite, two independent package builds and checksum verification. CI uses the same entry point and checks Windows output against the Linux bundle. The release workflow publishes the tagged package after both jobs pass and verifies its public install URL.

| Concern | Automated evidence |
| --- | --- |
| Identical GPUs and protected inference | Two synthetic PCI slots with the same device IDs, distinct UUIDs/subsystems, graphics and audio in separate groups. FDs and graphics PIDs on the protected GPU remain present. |
| Target usage | PCI-scoped NVIDIA XML, device-number FD checks, unresolved running workload, all-GPU requests, numeric selection, explicit mappings, protected client in a selected container. |
| Safe assignment | Protected, unrelated and competing VMs, extra group member, shared upstream bridge, changed UUID/BDF, incomplete or unmanaged XML, persistent definition update preserving metadata. |
| Failure paths | Stop failure, client surviving stop, failed detachment/start, absent host driver or NVIDIA userspace, replaced container, array shutdown and journal corruption. |
| Restoration and exclusivity | Dependency order, previously stopped container, stop-only and leave policies, simultaneous competing prepares, duplicate release, active-domain recovery refusal and idempotent recovery. |
| Packaging and access | Real May package as migration fixture, temporary-root install/upgrade/remove, existing native hook and foreign files, installation failure rollback, boot with an outstanding journal, native-session/CSRF/confirmation contract with a stub CLI. |
| Release integrity | Exercise the manifest's checksum gate with a damaged cache. Check matching metadata in the package and installer, RC upgrade ordering, dirty and mismatched tags, development reproducibility and complete stable-promotion notes using real temporary Git repositories. |

## First authorized the test host session

**Hardware acceptance is pending.** Run these commands as root on the test host only after authorizing the test. Use a quiet window for the shared GPU and keep the protected workload and the control VM running. Record model, kernel, plugin version, timestamps, outputs and observations for each cycle. Store evidence under a private array share after testing. `/tmp` is temporary.

Follow the [install procedure](../README.md#install). Open Bash on the test host. Supply the exact Windows and the control VM domain names displayed by virsh.

```bash
set -euo pipefail
ctl=/usr/local/lib/gpu-switch-manager/gpuctl
nvidia-smi --query-gpu=name,uuid,pci.bus_id --format=csv
read -r -p "Protected GPU UUID: " AI
read -r -p "Shared GPU UUID: " GAME
read -r -p "Protected display BDF: " AP
read -r -p "Shared display BDF: " GP
read -r -p "Shared audio BDF: " GA
test "$AI" != "$GAME"
T=$(mktemp -d /tmp/gsm-accept.XXXXXX)
chmod 700 "$T"
virsh list --all
read -r -p "Exact gaming VM name: " VM
read -r -p "Exact running the control VM VM name: " HA
test "$VM" != "$HA"
test "$(virsh domstate "$VM")" = "shut off"
test "$(virsh domstate "$HA")" = running
HA_ID=$(virsh domid "$HA")
virsh dumpxml --inactive --security-info "$VM" > "$T/before.xml"
sha256sum /etc/libvirt/hooks/qemu > "$T/native.sha256"
uname -a
cat /etc/unraid-version
virsh --version
nvidia-smi --query-gpu=name,driver_version,uuid,pci.bus_id --format=csv
nvidia-smi -i "$AI" --query-gpu=uuid --format=csv,noheader | grep -Fx "$AI"
nvidia-smi -i "$GAME" --query-gpu=uuid --format=csv,noheader | grep -Fx "$GAME"
"$ctl" discover | tee "$T/discovery.json"
"$ctl" diagnostics > "$T/before.json"
"$ctl" probe
```

Compare discovered PCI addresses, UUIDs, subsystem IDs, groups and upstream bridge peers to the physical inventory. Confirm the expected host drivers on both shared functions. If addresses differ, rediscover and explicitly remap. The following enrollment commands are for the confirmed addresses.

```bash
"$ctl" enroll ai "$AP" protected
"$ctl" enroll game "$GP" shared
"$ctl" export > "$T/enrolled.json"
docker ps -a --format '{{.Names}}'
read -r -a WORKLOADS -p "Containers to stop and restore, in startup order: "
python3 - "$T/enrolled.json" "$VM" "$AI" "$GAME" "${WORKLOADS[@]}" <<'PY'
import json, sys
from pathlib import Path
p, vm, ai, game, *names = sys.argv[1:]
cfg = json.loads(Path(p).read_text())
assert cfg["gpus"]["ai"]["uuid"] == ai and cfg["gpus"]["game"]["uuid"] == game
cfg["profiles"][vm] = {"gpu": "game", "containers": [
    {"name": name, "policy": "restore", "after": names[max(i-1,0):i]}
    for i, name in enumerate(names)]}
Path(p).write_text(json.dumps(cfg, indent=2))
PY
docker ps -a --format '{{.Names}}  {{.Status}}'
cat "$T/enrolled.json"
```

Review the example workload list and correct it for the actual host before import. Select an explicit leave policy for any reported all-GPU workload that must remain untouched. Active target clients in a leave container still block the operation. Enroll another display controller as protected using the BDF shown by discovery if desired. To retain this additional enrollment, make it before exporting the JSON above.

```bash
"$ctl" import < "$T/enrolled.json"
"$ctl" assign "$VM"
"$ctl" dry-run "$VM" | tee "$T/plan.json"
python3 -c 'import json,sys; c=json.load(open(sys.argv[1])); c["enabled"]=True; print(json.dumps(c))' "$T/enrolled.json" > "$T/enabled.json"
"$ctl" import < "$T/enabled.json"
```

If dry-run reports conflicting hostdevs or unsafe isolation, stop here. Use the VM editor to review stale definitions and remove only obsolete assignments from the powered-off gaming VM. Verify the backup before editing.

### Five start and shutdown cycles

For post-release CUDA, supply an existing local Docker image with a working CUDA-enabled PyTorch installation. It will run a disposable, network-isolated test container pinned to the shared UUID. Confirm the continuous application service is producing inference responses in its existing client during each cycle. For the example protected containers, compare Docker IDs, running state, restart counts and start times. Enter only the names of protected containers that actually exist.

```bash
read -r -p "Local CUDA PyTorch image: " CUDA_IMAGE
docker image inspect "$CUDA_IMAGE" >/dev/null
read -r -a PROTECTED -p "Protected running container names, space-separated: "
test "${#PROTECTED[@]}" -gt 0
protected_state() {
  docker inspect --format '{{.Id}} {{.State.Running}} {{.State.StartedAt}} {{.RestartCount}}' "${PROTECTED[@]}"
}
protected_state > "$T/protected.before"
grep -q ' true ' "$T/protected.before"
mapfile -t ASSIGNED < <(python3 -c 'import json,sys; print("\n".join(i["name"] for i in json.load(open(sys.argv[1]))["containers"]))' "$T/plan.json")
docker inspect --format '{{.Name}} {{.Id}} {{.State.Running}}' "${ASSIGNED[@]}" > "$T/assigned.before"
check_cuda() {
  docker run --pull=never --rm --network none --gpus "device=$GAME" "$CUDA_IMAGE" python -c 'import torch; assert torch.cuda.device_count()==1; x=torch.arange(1024,device="cuda"); assert x.sum().item()==523776; print(torch.cuda.get_device_name(0),"CUDA kernel and transfer passed")'
}
for cycle in 1 2 3 4 5; do
  "$ctl" start "$VM" | tee "$T/start-$cycle.json"
  test "$(virsh domstate "$VM")" = running
  test "$(basename "$(readlink "/sys/bus/pci/devices/$GP/driver")")" = vfio-pci
  test "$(basename "$(readlink "/sys/bus/pci/devices/$GA/driver")")" = vfio-pci
  nvidia-smi -i "$AI" --query-gpu=uuid,utilization.gpu --format=csv
  test "$(virsh domid "$HA")" = "$HA_ID"
  diff "$T/protected.before" <(protected_state)
  "$ctl" diagnostics > "$T/active-$cycle.json"
  read -r -p "Confirm Windows GPU/driver, live inference and HA service all passed (type PASS): " result
  test "$result" = PASS
  "$ctl" shutdown "$VM"
  for attempt in {1..120}; do
    if test "$(virsh domstate "$VM")" = "shut off" && test ! -e /boot/config/plugins/gpu-switch-manager/transactions/game.json; then break; fi
    sleep 2
  done
  test "$(virsh domstate "$VM")" = "shut off"
  test ! -e /boot/config/plugins/gpu-switch-manager/transactions/game.json
  test "$(basename "$(readlink "/sys/bus/pci/devices/$GP/driver")")" = nvidia
  test "$(basename "$(readlink "/sys/bus/pci/devices/$GA/driver")")" = snd_hda_intel
  nvidia-smi -i "$GAME" --query-gpu=uuid --format=csv,noheader | grep -Fx "$GAME"
  check_cuda | tee "$T/cuda-$cycle.txt"
  diff "$T/protected.before" <(protected_state)
  diff "$T/assigned.before" <(docker inspect --format '{{.Name}} {{.Id}} {{.State.Running}}' "${ASSIGNED[@]}")
  test "$(virsh domid "$HA")" = "$HA_ID"
  "$ctl" diagnostics > "$T/release-$cycle.json"
done
sha256sum -c "$T/native.sha256"
"$ctl" logs > "$T/operations.json"
```

The assigned-state comparison assumes the example restore/leave policy. For deliberately stop-only containers, verify the expected stopped state separately. In Windows, run `nvidia-smi.exe -L` and `Get-PnpDevice -Class Display | Format-Table Status,FriendlyName` in PowerShell, then exercise a GPU workload and HDMI audio. Check the control VM through its normal client and retain continuous application request/response evidence. A single host utilization sample cannot establish continuity.

### Forced exit, failed start and recovery

Use a disposable, diskless test domain with a different name and UUID for fault injection. Never force off Windows with live data or the control VM. Copy the shared GPU profile to that disposable domain in Settings and use the same enrolled GPU. With its minimal KVM domain shut off, use **Add managed devices to VM**. Keep a copy of its XML. Start it, verify VFIO ownership, then `virsh destroy "$TEST_VM"` using the disposable domain name. Verify release, both host drivers, both UUIDs and CUDA as above.

To exercise a failure after prepare without modifying a guest disk, install this temporary test-only hook on the mounted image during an authorized maintenance test. The hook applies only to the uniquely named disposable domain `gsm-accept-failure`. The atomic no-clobber creation protects an existing hook. Keep that name for this test profile. Verify that libvirt saw both prepare and the injected failure in its logs.

```bash
TEST_VM=gsm-accept-failure
test "$(virsh domstate "$TEST_VM")" = "shut off"
fault=/etc/libvirt/hooks/qemu.d/99-gsm-accept-failure
( set -C; cat > "$fault" <<'SH'
#!/bin/bash
if [[ "$1/$2/$3" == "gsm-accept-failure/start/begin" ]]; then
  echo "gsm acceptance injected prelaunch failure" >&2
  exit 1
fi
SH
)
chmod 755 "$fault"
trap 'rm -f -- "$fault"' EXIT
if "$ctl" start "$TEST_VM" > "$T/failure.txt" 2>&1; then
  echo "Unexpected successful start; shut down the disposable guest and inspect hook registration" >&2
  exit 1
fi
rm -f -- "$fault"
trap - EXIT
grep -F 'gsm acceptance injected prelaunch failure' "$T/failure.txt"
test "$(virsh domstate "$TEST_VM")" = "shut off"
"$ctl" logs
"$ctl" diagnostics
test ! -e /boot/config/plugins/gpu-switch-manager/transactions/game.json
check_cuda
```

If any release fails, preserve the journal and follow the [recovery procedure](../README.md#recovery-and-removal). Validate a failed start through the ordinary VM Manager as well, confirm the dashboard reports the actual result, and test a reboot/array stop and plugin upgrade in a separate authorized maintenance window. Record the native hook checksum before and after uninstall. Stable release requires the completed hardware evidence, native UI and packaging checks, and documented compatible versions.
