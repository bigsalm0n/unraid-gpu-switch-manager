# Unraid GPU Switch Manager

Share an NVIDIA GPU between Docker and a KVM guest. When the guest starts, the plugin stops its assigned containers and libvirt takes the GPU and audio function. After shutdown, the plugin verifies the host drivers and NVIDIA UUID before restoring eligible containers. Other GPUs can be marked as protected.

**Release candidate.** Automated tests use mocked hardware. Windows passthrough, post-release CUDA and five consecutive VM cycles still need hardware acceptance.

## Install

In the Unraid WebGUI, open **Plugins → Install Plugin**, paste the direct URL ending in `/gpu-switch-manager.plg`, and select **Install**. The manifest embeds its checksummed package. A release candidate uses the download URL for its specific tag. Stable releases use the **latest release** download URL. For a private build, an HTTPS server reachable by Unraid can serve the same file.

If the May plugin is installed, back up its configuration and follow the migration steps first. Its uninstaller deletes saved settings. [May upgrade procedure](docs/AUDIT.md#upgrading-from-the-may-plugin)

The host needs Python 3.9+ at plugin-install time on every boot, PHP 8+ with SimpleXML, Docker, pciutils, libvirt 6.5+ and IOMMU. Shared GPUs need the NVIDIA host driver and `nvidia-smi`. Start the array and VM Manager for first setup. Keep the shared VM's autostart disabled until these checks and its dry run pass.

```sh
ctl=/usr/local/lib/gpu-switch-manager/gpuctl
"$ctl" discover
"$ctl" probe
```

The probe asks libvirt to create a diskless, hardware-free paused domain and verifies that the plugin vetoes it. If the hook is inactive and the probe starts, the CLI destroys that probe UUID and reports failure.

## Configure sharing

1. Open **Settings → GPU Switch Manager**. Discover GPUs, enroll dedicated devices as protected, and enroll the gaming GPU as shared.
2. Choose the VM, GPU and containers. Each container can be left running, stopped only, or stopped and restored. Set dependencies by name and save the profile. A running all-GPU container needs an explicit policy. Use NVIDIA UUIDs where possible.
3. With the VM shut off, back up its XML and select **Add managed devices to VM**. Each profile supports one complete graphics slot and its audio functions. Review conflicting PCI assignments in the Unraid VM editor.
4. Run **Dry run**, then **Enable automatic switching**. Start and shut down the guest through the normal VM Manager. The dashboard shows the ownership journal and any restoration errors.

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

Build from a Git checkout with full tag history. On Windows use `py -3 tools/build.py`. The output includes the installer, package, checksums, generated notes and source revision. To install offline, copy the verified manifest to `/tmp/gpu-switch-manager.plg` and run `plugin install /tmp/gpu-switch-manager.plg`.

Tags use `vYYYY.MM.DD-rc.N` and `vYYYY.MM.DD`. GitHub Actions tests the Linux build, compares the Windows artifacts byte for byte and creates a release draft. Publish the release to make its download URL available. Release notes come from Git history.

The technical references cover [ownership and lifecycle](docs/ARCHITECTURE.md), [the legacy audit and redistribution status](docs/AUDIT.md), and [automated tests and hardware acceptance](docs/TESTING.md).
