# Architecture-specific VM builds

These scripts build the accelerated **guest kernel**, not a complete pre-populated
company appliance. Docker and the company services run inside the Ubuntu guest.
Keep credentials, company data and deployment-specific hostnames outside Git.

| Target | Guest architecture | Kernel entry point | Status |
| --- | --- | --- | --- |
| Intel/AMD Linux with QEMU/KVM | x86_64 / amd64 | `build-x86_64.sh` | Existing runtime architecture; new entry point not rebuilt in this PR |
| Apple Silicon with UTM QEMU | aarch64 / arm64 | `build-arm64.sh` | Experimental native build path; compilation, boot and accelerated runtime unverified |

ARM64 is not yet a supported full-company deployment. This PR provides its
candidate build recipe and records the work required to establish support.
An x86 Ubuntu guest can also be emulated in UTM on Apple Silicon, using the x86
script inside that guest; this is slower than native ARM virtualisation and does
not establish accelerated-clock compatibility with UTM.

## Shared build prerequisites

Run the build inside an empty Ubuntu 24.04 guest of the matching architecture,
not on macOS or the physical Linux host. The scripts install modules and boot
files into that guest. Keep its stock kernel and persistent data disk intact.

Inside the guest, install the native compiler and kernel build dependencies:

```sh
sudo apt-get update
sudo apt-get install --no-install-recommends build-essential bc bison flex \
  libssl-dev libelf-dev dwarves cpio kmod initramfs-tools curl xz-utils patch python3 git
```

Check out this repository in the guest. Both entry points call the existing
shared builder, use the same pinned Linux archive and clock patches, and merge
common Docker requirements with an architecture-specific config fragment.
The artifact digest includes the architecture, patches and both fragments, so
an ARM build cannot reuse an x86 source/build identity. Builds are native;
these scripts do not cross-install modules into another operating system.

## x86_64: Ubuntu on a Linux KVM host

On a Linux host with KVM and the packages listed in the
[VM deployment guide](../../deploy/virtual-time-world/README.md):

```sh
sudo bash deploy/virtual-time-world/provision.sh
```

This provisions the existing amd64 Ubuntu guest and its separate data disk.
Then, inside that guest, from the repository root:

```sh
sudo bash deploy/virtual-time-world/kernel/build-x86_64.sh
```

The build produces `arch/x86/boot/bzImage`, installs it as
`/boot/vmlinuz-<kernel-release>`, installs its modules and initramfs, and writes
`/opt/agentic-world-build/candidate.json`. It does not reboot the guest or
select the kernel automatically. The original `kernel/build.sh` entry point
continues to default to x86 for existing operators.

Continue with the existing host controller's `prepare` and initialisation
procedure in the VM guide. For an existing company, use the
[migration runbook](virtual-time-migration.md); never substitute host UTC for
its saved company epoch. Application Compose configuration and data transfer
are separate from kernel compilation.

## ARM64: Ubuntu on Apple Silicon through UTM

Create a **new ARM64 Ubuntu 24.04 guest** in UTM using its QEMU backend and
native virtualisation. The current Linux/libvirt provisioner does not create
UTM bundles. Install Ubuntu from its ARM64 installer, retain the stock boot
entry, and add a separate guest-owned data disk for the company. Format only a
new empty disk; never format a copied company data disk. Mount it persistently
at `/data` and configure Docker/containerd storage there as described in the
VM guide. Do not share a host directory for company storage: its filesystem
timestamps would come from macOS.

Inside that ARM64 Ubuntu guest, install the shared prerequisites, then run:

```sh
sudo bash deploy/virtual-time-world/kernel/build-arm64.sh
```

This selects Linux's `ARCH=arm64`, builds `arch/arm64/boot/Image`, and installs
the candidate kernel/modules/initramfs alongside Ubuntu's stock kernel. Its
ARM fragment includes the architectural timer, QEMU virt PCI/virtio devices,
EFI and PL011 serial console. Build jobs can be configured explicitly:

```sh
sudo env WORLD_BUILD_JOBS=2 bash deploy/virtual-time-world/kernel/build-arm64.sh
```

This is a candidate build recipe, not instructions to activate a completed
ARM company deployment. Do not use the existing libvirt clock controller to
activate this UTM guest: it currently assumes the Linux host's libvirt domain,
SSH/guest-agent transport and x86 serial console.

### Remaining ARM64/UTM implementation

Before publishing a runnable ARM appliance:

1. Compile and boot the ARM candidate alongside the stock kernel. Verify the
   actual ARM clocksource, vDSO, architectural timer and clockevent paths,
   continuity during rate changes, file timestamps, and reboot persistence.
   Current observations cover the x86 KVM guest only. Devices using direct
   KTIME clockevents and suspend/resume remain documented coverage gaps.
2. Add a UTM host-control path for kernel selection, the PL011 console,
   durable paired clock anchors, rate control and clock-ready application
   startup. Preserve the same time semantics; do not add a second app clock.
3. Audit every pinned application, desktop and task image for `linux/arm64`.
   Rebuild missing native images from their original sources where possible.
   An image that is amd64-only remains an explicit coverage gap; do not silently
   substitute a different application, benchmark or scoring implementation.
4. Adapt the host model-authentication transport and real-time issue scheduler
   for macOS. The existing host systemd/libvirt units cannot be installed on
   macOS unchanged. Keep the confirmed handoff deadline on real time.
5. Establish integration health without launching benchmark episodes or
   interacting with Viv. Building a kernel alone does not establish that the
   complete company or accelerated clock is compatible with UTM.

For the image audit, use native Docker tooling inside the Linux guest:

```sh
docker compose -f YOUR_PRIVATE_COMPOSE_FILE config --images
# Repeat for each resolved, pinned image reference:
docker buildx imagetools inspect IMAGE_REFERENCE
```

Do not commit rendered Compose secrets or private deployment mappings. Keep
architecture differences in build/config inputs, while sharing the application
code, company identity contracts and benchmark criteria.

UTM references: [QEMU architecture settings](https://docs.getutm.app/settings-qemu/system/),
[hypervisor settings](https://docs.getutm.app/settings-qemu/qemu/).
