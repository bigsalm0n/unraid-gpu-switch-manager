# Unraid GPU Switch Manager

Share an NVIDIA GPU between Docker and a KVM guest. When the guest starts, the plugin stops its assigned containers and libvirt takes the GPU and audio function. After shutdown, the plugin verifies the host drivers and NVIDIA UUID before restoring eligible containers. Other GPUs can be marked as protected.

**Release candidate.** Automated tests use mocked hardware. Live WebGUI, Windows passthrough, post-release CUDA and five consecutive VM cycles still need acceptance.

## Install

For the current repository build, install the verified local manifest using the commands under [Build and release](#build-and-release). For published candidates, open **Plugins → Install Plugin** and use the [rolling candidate manifest](https://raw.githubusercontent.com/bigsalm0n/unraid-gpu-switch-manager/release-installer/gpu-switch-manager.plg). The original `main` URL is retained for existing installations.

<!-- latest-install:begin -->
```text
https://raw.githubusercontent.com/bigsalm0n/unraid-gpu-switch-manager/release-installer/gpu-switch-manager.plg
```

Current build **2026.11.27-rc.8** · [Release notes](https://github.com/bigsalm0n/unraid-gpu-switch-manager/releases/tag/v2026.11.27-rc.8) · [Version-pinned installer](https://github.com/bigsalm0n/unraid-gpu-switch-manager/releases/download/v2026.11.27-rc.8/gpu-switch-manager.plg)
<!-- latest-install:end -->

Merges to protected `main` that change plugin or build files publish a candidate after CI verifies the build. CI advances the rolling installer on `release-installer`. The stable channel is `https://github.com/bigsalm0n/unraid-gpu-switch-manager/releases/latest/download/gpu-switch-manager.plg` once a stable release is published. Installed plugins use their manifest's channel when you select **Plugins → Check for Updates**. Stable releases have the highest version suffix for their date. The manifest embeds the package for offline installation and verifies its SHA-256 before extraction.

If the May plugin is installed, back up its configuration and follow the migration steps first. Its uninstaller deletes saved settings. [May upgrade procedure](docs/AUDIT.md#upgrading-from-the-may-plugin)

The runtime uses Unraid's PHP 8.1+ CLI with SimpleXML, DOM and POSIX, Bash, Docker, pciutils and libvirt 6.5+. It installs without Python. Shared GPUs need IOMMU, the NVIDIA host driver and `nvidia-smi`. Start the array and VM Manager for first setup. Keep the shared VM's autostart disabled until its dry run and the hook probe pass.

To upgrade version `2026.11.27.0004`, finish any shared-GPU transaction, leave VM Manager enabled, and back up `/boot/config/plugins/gpu-switch-manager/sharing.json`. Install over the existing version. The installer keeps GPU identities and VM profiles, replaces the plugin-owned hook, and removes checksum-verified files belonging to the old bundled Python. A transition, outstanding journal, modified plugin file or shared PCI function still bound to VFIO aborts the upgrade.

```sh
ctl=/usr/local/lib/gpu-switch-manager/gpuctl
"$ctl" discover
"$ctl" probe
```

The probe asks libvirt to create a diskless, hardware-free paused domain and verifies that the plugin vetoes it. If the hook is inactive and the probe starts, the CLI destroys that probe UUID and reports failure.

## Configure sharing

1. Open **Settings → GPU Switch Manager**. Enroll dedicated devices as protected and the gaming GPU as shared. A GPU without an enrollment is unmanaged.
2. Choose the VM, GPU and containers. Detected workloads for the selected GPU appear first. A container can be left running, stopped only, stopped and restored, or started with the VM if it has no host GPU access. Containers started with the VM stay running after guest shutdown. Set dependencies by name and save the profile. The dry run shows which running containers the plan would stop. A running container with an open device handle needs to release the shared GPU or have an eligible stop policy. A possible GPU mapping is shown for review. Use NVIDIA UUIDs where possible.
3. With the VM shut off, back up its XML and select **Add managed devices to VM**. Each profile supports one complete graphics slot and its audio functions. Review conflicting PCI assignments in the Unraid VM editor.
4. Run **Validate and dry run**. Use **Start VM** for a manual transition. For standard Unraid VM Manager starts, enable automatic switching globally and select **Switch when started from VM Manager** in the profile. The dashboard shows ownership, transition state and restoration errors.

After a motherboard change, rediscover the GPU by UUID, compare subsystem IDs and sibling functions, then explicitly re-enroll its BDF and update the powered-off VM definition.

## Recovery and removal

Run `gpuctl diagnostics` and `gpuctl logs` using the CLI path above. Retain the transaction under `/boot/config/plugins/gpu-switch-manager/transactions/`. Check the owner with `virsh list --all`, both functions with `lspci -Dnnk -s ADDRESS`, and the shared UUID with `nvidia-smi -i UUID`. When the guest is off and the host drivers and userspace are healthy, run `gpuctl recover KEY` with the enrolled GPU key. A replaced container or corrupt journal requires operator review.

If a function remains on VFIO, preserve the journal and inspect kernel and libvirt logs before scheduling driver recovery. Global NVIDIA module reloads and bus resets can disrupt other workloads.

To roll back, finish outstanding transactions, disable automatic switching, and shut down the affected VM. Restore its saved definition with `virsh define --validate /path/to/saved.xml`. Remove the plugin with `plugin remove gpu-switch-manager.plg` while the libvirt image is mounted. Removal retains the flash configuration. Archive and verify that directory before deleting it if retiring the plugin.

## Build and release

```sh
python3 tools/build.py
(cd dist && sha256sum -c SHA256SUMS)
```

Build from a Git checkout with full tag history. On Windows use `py -3 tools/build.py`. For a candidate, check out its tag before building. An untagged build uses a development version and is unsuitable for testing native upgrade ordering.

Copy the verified `dist/gpu-switch-manager.plg` to `/tmp/gpu-switch-manager.plg` on the test server and run as root.

```bash
plugin install /tmp/gpu-switch-manager.plg
```

Merges to `main` publish a candidate after the Linux test suite and reproducible package checks. An explicit `vYYYY.MM.DD-rc.N` tag can also trigger a candidate build. A pushed `vYYYY.MM.DD` tag publishes an accepted revision on the stable channel. Both use Git-generated release notes. Verify hardware acceptance and redistribution rights before pushing a stable release tag.

The technical references cover [ownership and lifecycle](docs/ARCHITECTURE.md), [the legacy audit and redistribution status](docs/AUDIT.md), and [automated tests and hardware acceptance](docs/TESTING.md).
