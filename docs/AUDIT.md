# Audit of ff1e0ce and the May 2026 packages

The baseline contains 234 tracked files present in the checkout, with 151 distinct byte contents. The two committed txz archives each contain 96 files. The May 13 archive's corresponding source files match the package tree after newline normalization. Top-level scripts, PHP classes, settings pages and the hand-maintained package tree form competing source trees.

| Area | Finding and consequence |
| --- | --- |
| Binding scripts | bind_gpu_to_vfio.sh writes shared graphics and audio IDs to vfio-pci/new_id. Its inverse uses remove_id. Both paths edit modprobe configuration. Matching cards can be claimed outside the selected slot. |
| Discovery | list_gpus.sh, get_gpu_state.sh and bind_gpu_to_vfio.sh query all compute PIDs. The settings statistics and binding endpoints use different enumerations. Indices and GPU model names cannot distinguish identical cards across detachments. |
| Execution | apply_gpu_binding.php and apply_profile.php persist intent and log success without performing the operation. Profile create/update/delete/toggle use unlocked read-modify-write and unchecked writes. The old validation and history paths contain placeholders. |
| Web access | Standalone package endpoints bypass the separate top-level authentication and CSRF classes. Dashboard code assumes systemd. The Intel stats command can run continuously. No reason to expose a separate credential store, WebSocket server or password setup. |
| Lifecycle | vm_lifecycle_hooks.sh writes the main qemu script, generates invalid top-level local statements, backgrounds preparation and uses stopped. Removal unlinks the main hook by path. Executability tests cannot demonstrate invocation. |
| Events | Six event scripts source an absent adjacent helper. Registration writes into a shared webGui directory. The Docker monitor daemonizes independently of transaction ownership. Profile-wide service orchestration can affect unrelated workloads. |
| Installation | The canonical manifest advertises May 10 and a different URL pattern from May 13. Install chmods a websettings path outside the actual package layout. Removal invokes broken deregistration and recursively deletes persistent configuration. No consistent dependency, upgrade or active-assignment check. |
| Validation | Phase scripts and historical completion reports exercise the old structure. There is no tracked CI workflow, package recipe, reproducibility check, or recorded hardware acceptance. |

## Provenance and release status

The legacy checkout contains no licence granting redistribution of its code or artwork. The replacement package contains newly written runtime, UI and installation files. Migration recognizes old files by SHA-256 digest. CI reads the May package from the existing fork history for a temporary migration fixture. New releases exclude the legacy package, scripts, artwork and third-party dependencies. Preserve this boundary when adding release assets.

## Upgrading from the May plugin

Back up the flash configuration, VM XML and native QEMU hook. Finish any current GPU assignment. Inspect legacy VFIO boot settings and previous hook edits before enabling the candidate. Save version-named legacy manifests such as `gpu-switch-manager-2026.05.13.plg` outside the boot plugins directory so Unraid cannot reinstall them at reboot. For an installed version-named manifest, archive its matching tracking symlink in `/var/log/plugins` as well. Leave the legacy runtime in place for the new installer's ownership checks. The May uninstaller recursively deletes persistent settings.

After backing up, use the [installation procedure](../README.md#install). The installer recognizes exact old package files and known generated event scripts, preserves independent integrations, and rejects an occupied hook path or modified legacy endpoint. If the legacy main hook replaced your own, restore it from your known-good Unraid backup. Inspect any reported old monitor before stopping its specific verified process. The installer never guesses the prior contents of a native hook or modprobe configuration.

Legacy `profiles.json`, `state.json` and cfg files are retained. Migration creates a disabled version 2 configuration. Review and recreate the profiles from the preserved files because the old GPU indices and saved state lack reliable device identity. Later candidate upgrades preserve version 2 and reject unknown schema versions. Finish outstanding transactions before upgrading an installed runtime.

## Version 2026.11.27.0004 audit

The installed WebGUI configured `local_prepend.php` for PHP requests. It validated the URL-encoded `csrf_token` field and removed it from `$_POST` before the plugin endpoint ran. The plugin then claimed the same token was invalid, resulting in the reported 403 and preventing inventory initialization. A loopback HTTP fixture used the installed prelude with synthetic state and a stub CLI. A valid authenticated request returned the reported 403 against the old handler and 200 against the replacement. The replacement still received native 403 responses for missing and invalid tokens and returned 401 without a session. This fixture exercised the form encoding and response bodies. Live browser and HTTPS checks remain in the acceptance procedure. [WebGUI request validation](https://github.com/unraid/webgui/blob/master/emhttp/plugins/dynamix/include/local_prepend.php)

The installed version tracked 4,582 files, including a private Python distribution. All tracked checksums matched. An upgrade and removal against a copy of that installation removed the owned interpreter files and the verified flash cache, and preserved the synthetic version 2 configuration and native hook byte for byte. This exercised migration on a temporary root.

The PHP port retains libvirt-managed binding, PCI and GPU identity checks, workload ownership checks, and journals from the previous managed-PCI refactor. The operating system supplies the PHP CLI, so installation no longer downloads or packs an interpreter. Stable release distribution, real driver reattachment, and protected workload continuity remain hardware and release acceptance tasks.

On Unraid 7.4.0, read-only inventory initially found DRM control minors in sysfs with no corresponding host device nodes. The scanner now uses the sysfs device numbers to match character-device handles, including a handle opened through a container-visible path. After the fix, the inventory returned three GPUs, two NVIDIA UUIDs, three VMs and 43 Docker containers with zero discovery or device-query errors. The installed plugin and driver bindings stayed in place during these checks.

The live plugin manager reported a generic Bash failure for the rc.6 upgrade and the old rc.4 removal. Running both original scripts on copies of the installed files in a private mount namespace exposed the underlying problem. Their rollback copies filled Unraid's 128 MiB `/run` filesystem. The updated installer links each rollback file beside its original on the same filesystem, and the manifest routes command errors to the plugin manager output. The complete fixed upgrade and removal scripts passed on the copied installation with a 4 MiB `/run` mount. The live plugin, native QEMU hook and workloads stayed in place.
