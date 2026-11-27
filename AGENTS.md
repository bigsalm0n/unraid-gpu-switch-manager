# GPU Switch Manager development rules

Keep a single installable source tree. Build with python3 tools/build.py. Run Linux mock tests with python3 -m unittest discover -s tests -v. See docs/TESTING.md for acceptance.

Use Git release tags for versions. Write commit subjects that describe observable changes. The build derives release notes and package metadata from Git. Run tools/check.sh with the original May archive fixture before tagging a candidate.

Use a full PCI address for operations and verify the enrolled hardware identity and every associated function. Enforce protection and ownership in the backend. Libvirt alone manages PCI drivers. Preserve the native QEMU hook. Hooks must finish preparation synchronously, produce no stdout and never call libvirt. Journal intent before stopping a workload and restore only the same container when its recorded policy permits it.

Use synthetic sysfs and command adapters in tests. Live deployment and hardware binding require explicit authorization. Record observed results separately from pending hardware acceptance. Confirm redistribution rights before publishing upstream material.

## Project memory

Search mem-zero at the start of a conversation when available. Keep one self-contained entry per decision, including the reason. Record durable conventions, user corrections, technical debt and failed approaches. Keep transient activity and code signatures in Git.
