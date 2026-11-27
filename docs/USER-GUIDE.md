# GPU Switch Manager user guide

GPU Switch Manager lets Docker containers and a virtual machine take turns using one NVIDIA GPU. A typical setup has an inference GPU that stays with Unraid and a second GPU that goes to a gaming VM while it runs. When the VM shuts down, Unraid gets the shared GPU back and the plugin restarts the eligible containers it stopped.

## Before you start

- Start the Unraid array, Docker, and VM Manager. The VM should already exist.
- Keep the guest shut down with VM autostart off during setup. Save a copy of its current XML in VM Manager before changing its PCI devices.
- The shared GPU needs a working NVIDIA host driver and a safely isolated PCI slot. The plugin checks the GPU, its audio functions, and their IOMMU groups. Keep the host's boot display with Unraid.
- Start with one shared GPU and one VM. Use the GPU's PCI address and NVIDIA UUID to identify it. If you have two identical cards, compare those values before saving either identity.

## Install and open the dashboard

In Unraid, open **Plugins → Install Plugin**, paste the rolling candidate manifest URL below, and select **Install**. To upgrade a version that is already installed, open **Plugins → Check for Updates** and update in place.

```text
https://raw.githubusercontent.com/bigsalm0n/unraid-gpu-switch-manager/release-installer/gpu-switch-manager.plg
```

Open **Settings → GPU Switch Manager** and select **Refresh**. The **GPU overview** shows each device's PCI address, UUID where available, current driver, role, and ownership. The candidate is suitable for testing on your hardware. Confirm a complete manual start and shutdown cycle before enabling automatic switching.

## Set the GPU roles

In **GPU configuration**, choose the device and enter a short **Profile key** you will recognise. Select the role and choose **Save device identity**.

| Role | When to use it |
| --- | --- |
| **Protected** | For a GPU that should always stay with Unraid, such as the card running inference. Example key `inference`. |
| **Shared** | For the NVIDIA GPU that will move between Docker and a VM. Example key `gaming`. |
| **Unmanaged** | For a GPU you want the plugin to leave alone. Leave it unenrolled. To clear an enrolled role, remove its VM profiles first and select **Remove enrollment**. |

A profile key is the name you give the GPU in this plugin. It appears in the **Shared GPU** dropdown after you save a shared device.

## Create a VM sharing profile

1. In **VM sharing profiles**, select your VM and the **Shared GPU** you enrolled. Leave **Switch when started from VM Manager** unchecked for the first test.
2. Set a policy for containers that can use the shared GPU and any services that depend on them. Containers pinned to your protected GPU can usually stay unassigned. Prefer the NVIDIA UUID in Docker GPU settings. An all-GPU container may need an explicit policy because it can see the shared card.
3. Select **Save profile**. With the VM shut down, choose **Add managed devices to VM**. This adds the shared GPU and approved functions to the selected VM's persistent definition. If the plugin reports conflicting PCI assignments, review the existing devices in the Unraid VM editor before trying again.
4. Select **Validate and dry run**. Review the drivers and container plan. A container with `will_stop: true` is scheduled to stop during the transition. Resolve any reported blocker before starting the guest.

| Container policy | What happens |
| --- | --- |
| **Unassigned** | The profile has no policy for this container. If a running container may be using the shared GPU, the dry run can ask you to choose a policy. |
| **Stop and restore** | The plugin can stop the container for the VM. It starts the same container again after the GPU returns if the container was running before the switch. |
| **Stop only** | The plugin can stop the container for the VM. Start it yourself once the GPU has returned. |
| **Leave running** | The plugin leaves the container running. If it has the shared GPU open, the switch stops at validation and reports the workload. |

Use **Start after** for container dependencies. For example, if `voice-ui` depends on `voice-engine`, put `voice-engine` in the **Start after** field for `voice-ui`. Keep both containers in the same profile. Separate multiple dependency names with commas. The plugin stops dependent services first and restores their dependencies first.

## Test manual switching

1. With the dry run passing, select **Start VM** and confirm. The GPU overview should move from host ownership to **Switching to VM**, then **Assigned to running VM**. Verify that the protected GPU and its workloads are still available.
2. Verify the guest can use the shared GPU. Shut it down from inside the guest or select **Request guest shutdown**. Wait for the VM to finish shutting down.
3. In GPU Switch Manager, look for **Returning to Unraid**, followed by **Available to Unraid** or **In use by host workloads**. Confirm the host driver is back and the expected containers are running. A container that was already stopped stays stopped.

### Start from the regular VM Manager

Once the manual cycle passes, check **Enable automatic switching** and select **Save switching setting**. For the selected VM, check **Switch when started from VM Manager** and select **Save profile**. Both switches must be enabled for native VM Manager starts. The **Start VM** button in GPU Switch Manager remains available for manual starts.

## Troubleshoot a failed transition

| What you see | What to check |
| --- | --- |
| The **Shared GPU** list is empty | Verify that discovery found the GPU and that you saved it with the **Shared** role. |
| A container needs a policy | Check whether it sees the shared GPU, especially if it is configured for all GPUs. Assign the appropriate policy or pin it to the intended GPU by UUID, then rerun the dry run. |
| A process still has the GPU open | Use **View diagnostics** to identify the client. Close the application or stop the specific workload normally, then rerun the dry run. |
| The IOMMU group or reset cannot be isolated | Leave the card with the host until you can review the PCI topology. |
| **Recovery or review required** after shutdown | Confirm the VM is fully off. Use **View diagnostics** and **Operation log** to check driver bindings and the last error. Once the host drivers are back, select the affected **Shared GPU** and choose **Recover selected GPU**. |
| Upgrade or removal reports a failure | Keep VM Manager running, finish any pending transition, and read the specific message shown in **Plugins**. The old `2026.11.27.0004` removal script can fail from lack of rollback space. Upgrade to the current candidate before using the normal removal button. |

If recovery reports a replaced container or a function still on VFIO, keep the transaction and download diagnostics before making further changes. For sharing a diagnostic report, review it first for VM names, container names, and hardware identifiers.

## Back up or remove the plugin

Use **Configuration import and export → Download configuration** to back up your GPU identities and VM profiles. Importing a file replaces the saved configuration and should happen with no transition pending.

Before removal, shut down the shared VM and verify its GPU has returned. If you also want to remove the VM's managed PCI devices, restore the XML you backed up while the VM is off. Leave VM Manager enabled and remove **GPU Switch Manager** from **Plugins**. The plugin retains `/boot/config/plugins/gpu-switch-manager/sharing.json` for a future reinstall.

For package and hardware acceptance tests, use the [testing procedure](TESTING.md).
