# Changelog

## Next candidate

- Upgrades and removals keep rollback files beside the installed files, so migration from the private Python bundle fits within Unraid's small `/run` filesystem.
- Shows installer failures in the Unraid Plugins screen.
- Publishes candidates after merges to protected `main` and updates the rolling installer on a dedicated branch.

## 2026.11.27-rc.6

- Uses Unraid's native CSRF validation and administrator session. Sends the page token as form data and reports sign-in, validation, execution and discovery failures separately.
- Runs on native PHP CLI and Bash. The upgrader preserves schema 2 settings and removes verified files from the old private Python installation.
- Shows GPU ownership, role and workloads in Settings. Adds per-VM automatic switching, manual starts, and downloadable diagnostics.
- Detects DRM client handles by kernel device number even when the host has no corresponding `/dev/dri/controlD*` node.
- Displays GPU Switch Manager and its description in the installed plugins list with author `bigsalmon`.
- Publishes candidates and stable releases from explicit Git tags. Stable installations use GitHub's latest stable release endpoint.

Live Unraid and hardware acceptance remain pending. Release packages and detailed notes derive their versions from Git.
