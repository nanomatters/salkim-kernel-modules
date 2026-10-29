# Salkim kernel modules

One DKMS package provides ntsync with IOCP support and a modified AMDGPU driver
for Linux 7.3.x.

The AMDGPU source starts from upstream Linux 7.3 and carries the driver contained
Salkim VM fixes and sparse mapping optimisations.

AMDGPU is experimental and currently restricted to 7.3.x kernels with
`CONFIG_DRM_AMDGPU=m`. Keep an unmodified kernel as a boot fallback.

## ntsync

This repository carries the upstream Linux `ntsync` driver as a single
out-of-tree kernel module. It is the development tree for adding kernel-backed
NT I/O completion ports while preserving the existing synchronization API.

## Upstream baseline

- Repository: https://github.com/torvalds/linux.git
- Commit: `1b78070aaef63512688aebfbc82365ef9d6660f1`
- Driver: `drivers/misc/ntsync.c`
- UAPI: `include/uapi/linux/ntsync.h`
- Self-tests: `tools/testing/selftests/drivers/ntsync/ntsync.c`

The imported source, UAPI, documentation, and tests are kept with their
upstream licenses.

## IOCP development

The current development interface provides creation, FIFO posting,
nonblocking and blocking batched removal, absolute timeouts, alert events,
generation based removal abandonment, terminal cancellation, depth queries,
and generic ntsync wait integration. See `Documentation/iocp.rst` for its
semantics and remaining work.

## Arch Linux and CachyOS package

Install the headers for every kernel that should build the module. For the
standard CachyOS kernel:

```sh
sudo pacman -S --needed base-devel linux-cachyos-headers
```

Other kernel variants need their corresponding headers package. Build the
local package from the repository root with:

```sh
cd package/arch
makepkg -s
```

Install the resulting package with:

```sh
sudo pacman -U ./salkim-kernel-modules-dkms-*.pkg.tar.zst
```

DKMS builds ntsync for installed kernels with matching headers and AMDGPU for
supported 7.3.x kernels. Modules are installed under `updates/dkms`.

After DKMS installation, existing mkinitcpio presets are rebuilt if their
images already contain AMDGPU for a 7.3.x kernel. Removal rebuilds those
images again so they use the distribution driver. Other initramfs generators
need their normal manual regeneration procedure.

Confirm the selected modules with:

```sh
modinfo -n ntsync
modinfo -n amdgpu
```

Installation does not replace modules that are already loaded. Reboot after
installing AMDGPU. For ntsync alone, stop all processes using it before reloading:

```sh
sudo modprobe -r ntsync
sudo modprobe ntsync
```

## Build

The running kernel's build tree is used by default:

```sh
make
```

The compiler is selected from the target kernel configuration. Clang-built
kernels use LLVM and GCC-built kernels use GCC. To override the extra Kbuild
arguments explicitly:

```sh
make KBUILD_ARGS=
```

To build against another kernel:

```sh
make KDIR=/path/to/kernel/build
```

The resulting module is `ntsync.ko`. It has the same name as the in-tree
module, so the installed module must be unloaded before loading this one.

Build AMDGPU separately without installing it with:

```sh
make -C amdgpu KDIR=/path/to/kernel/build
```

## Tests

Build the userspace self-tests with:

```sh
make tests
```

Run them against the currently loaded module with:

```sh
make test
```
