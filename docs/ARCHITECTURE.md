# Ownership and lifecycle

Libvirt owns all PCI detachment, reattachment and guest assignment. Each shared GPU uses managed PCI hostdev entries covering the complete approved slot, including audio. A profile authorizes one GPU for one VM name. Several VMs may reference the same GPU. Other GPUs can operate concurrently. The initial backend shares NVIDIA devices whose userspace identity can be verified and can protect any discovered display controller. Manual start and shutdown use the normal libvirt domain lifecycle. [Managed PCI devices](https://libvirt.org/formatdomain.html#host-device-assignment)

The qemu.d filter consumes domain XML, emits empty stdout on success and returns preparation failures synchronously. Calls into libvirt run only in the external CLI or dashboard process. The hook exits immediately on reconnect, which preserves existing guest processes. [Libvirt hook lifecycle and filter contract](https://libvirt.org/hooks.html)

| Event | Backend verification | Result |
| --- | --- | --- |
| prepare/begin | Match enrolled UUID, PCI and subsystem fingerprints, complete slot, IOMMU members, exclusive upstream port, host drivers and managed XML. Snapshot container IDs and stop in reverse dependency order. Scan the target again. | PREPARING_VM, or refuse startup and attempt restoration. |
| start/begin | Match domain and transaction. Verify VFIO on every approved function before QEMU launches. | VFIO_BOUND |
| started/begin | Verify the started domain hook and actual VFIO drivers. | VM_ACTIVE |
| release/end | After libvirt releases resources, wait for original drivers and UUID, check container IDs, verify renumberable mappings and dependencies, then start eligible workloads. | RESTORING_HOST, followed by cleared ownership or ERROR. |

The adapter reads graphics/compute PIDs from device-scoped NVIDIA XML and checks open character device numbers for NVIDIA, DRM and HDMI sound nodes. Cgroup membership and Docker's process listing associate PIDs with selected containers. Other GPUs' active clients constrain which containers can stop. A conservative topology check rejects a shared upstream bridge, even where the IOMMU groups differ.

Configuration and outstanding restoration journals live on flash. Journals capture original container IDs, prior running state, restore policy, original drivers, domain identity and hardware identity before disruptive actions. Locks and detailed history live in RAM. The plugin saves a bounded history checkpoint on completed restoration or transition errors. Configuration edits and installation take an exclusive maintenance lock. Hooks share that lock and take an exclusive per-GPU lock. A journal extends ownership across separate hook processes. Normal release clears ownership only after verification. Recovery requires inactive-domain evidence and driver/UUID checks. A corrupt or ambiguous journal blocks further switching.

The plugin never writes PCI sysfs. Ambiguous rediscovery, unexpected IOMMU neighbors, incomplete XML, unknown assignments, driver mismatches and failed userspace checks require operator attention. Global module reloads, bus resets and persistent VFIO changes are outside the implementation.

The installer stages files by checksum under a maintenance lock. It owns one executable in the mounted libvirt image and records its digest on flash. This wrapper can deny GPU starts even if the runtime fails to install on reboot. Unraid's libvirt_started event registers it on a newly mounted image. The stopping event defers restoration while the array goes offline. The started event reconciles outstanding transactions after services return. If libvirt, Docker or the GPU is unavailable, the journal remains available for operator recovery. [Unraid array events](https://github.com/unraid/webgui/blob/master/sbin/emhttp_event)

## Runtime and WebGUI

PHP CLI implements the controller, configuration and journals, hardware reads, the lifecycle state machine, and package installation. Bash entry points connect the controller to CLI users and Unraid events. The CLI passes argument arrays to bounded processes and relies on libvirt managed hostdevs for driver changes. An explicit start from the plugin writes a short-lived, one-use authorization for the selected domain UUID. A regular VM Manager start uses the global and per-profile automatic settings.

The settings page gets its token from WebGUI's page context. JavaScript posts `csrf_token` in a same-origin URL-encoded body. WebGUI's PHP prelude validates and consumes the token before `api.php` runs. The endpoint requires the native prelude and the native administrator session, accepts only POST, and forwards allowlisted actions to the CLI. The browser distinguishes a sign-in redirect, a native token refusal when the response identifies it, an unknown HTTP refusal, runtime failure, and partial discovery errors. [WebGUI request validation](https://github.com/unraid/webgui/blob/master/emhttp/plugins/dynamix/include/local_prepend.php) · [Native Docker form](https://github.com/unraid/webgui/blob/master/emhttp/plugins/dynamix.docker.manager/include/CreateDocker.php)

The installed plugins table reads its display text from the plugin-local `README.md`. The manifest retains the machine identifier and supplies the author, icon, version, launch path and update URL. The boot manifest embeds the verified archive so the RAM files can be restored offline. Existing schema 2 settings and journals carry forward byte for byte. Profiles without an `automatic` field retain their previous behavior. [Unraid installed plugins table](https://github.com/unraid/webgui/blob/master/emhttp/plugins/dynamix.plugin.manager/include/ShowPlugins.php) · [Maintained Tailscale plugin layout](https://github.com/unraid/unraid-tailscale/tree/trunk/src/usr/local/emhttp/plugins/tailscale)

Configuration edits use the same backend validator for CLI, dashboard and imported JSON. The dashboard submits its last-read revision to detect stale edits.

## Implementation sequence

1. Inventory existing package and endpoints. Add validated configuration, read-only discovery and identity checks.
2. Add synchronous hooks, ownership leases, Docker ordering, selective restoration and recovery. Exercise the paths with mock devices.
3. Connect the Unraid Settings page to the same CLI operations. Check native session and CSRF data and present verified backend status.
4. Build deterministic release candidates, test installation in a temporary root, and complete the documented hardware acceptance before public release.
