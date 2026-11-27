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

Configuration and outstanding restoration journals live on flash. Journals capture original container IDs, prior running state, restore policy, original drivers, domain identity and hardware identity before disruptive actions. Completed operation history and locks live in RAM. Configuration edits and installation take an exclusive maintenance lock. Hooks share that lock and take an exclusive per-GPU lock. A journal extends ownership across separate hook processes. Normal release clears ownership only after verification. Recovery requires inactive-domain evidence and driver/UUID checks. A corrupt or ambiguous journal blocks further switching.

The plugin never writes PCI sysfs. Ambiguous rediscovery, unexpected IOMMU neighbors, incomplete XML, unknown assignments, driver mismatches and failed userspace checks require operator attention. Global module reloads, bus resets and persistent VFIO changes are outside the implementation.

The installer stages files by checksum under a maintenance lock. It owns one executable in the mounted libvirt image and records its digest on flash. This wrapper can deny GPU starts even if the runtime fails to install on reboot. Unraid's libvirt_started event registers it on a newly mounted image. The stopping event defers restoration while the array goes offline. The started event reconciles outstanding transactions after services return. If libvirt, Docker or the GPU is unavailable, the journal remains available for operator recovery. [Unraid array events](https://github.com/unraid/webgui/blob/master/sbin/emhttp_event)

Configuration edits use the same backend validator for CLI, dashboard and imported JSON. The dashboard submits its last-read revision to detect stale edits. The web handler checks native root login, POST, the Unraid CSRF token and explicit confirmation for changes. External start requests use ordinary libvirt entry points, so future streaming or scheduling clients do not need a separate privileged GPU API.

## Implementation sequence

1. Inventory existing package and endpoints. Add validated configuration, read-only discovery and identity checks.
2. Add synchronous hooks, ownership leases, Docker ordering, selective restoration and recovery. Exercise the paths with mock devices.
3. Connect the Unraid Settings page to the same CLI operations. Check native session and CSRF data and present verified backend status.
4. Build deterministic release candidates, test installation in a temporary root, and complete the documented hardware acceptance before public release.
