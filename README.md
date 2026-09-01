# Standalone ntsync

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
local `ntsync-iocp-dkms` package from the repository root with:

```sh
cd package/arch
makepkg -s
```

Install the resulting package with:

```sh
sudo pacman -U ./ntsync-iocp-dkms-*.pkg.tar.zst
```

DKMS builds the module for each installed kernel that has matching headers.
On Arch Linux and CachyOS, the module is installed under `updates/dkms`, which
takes precedence over the in-tree `ntsync` module. Confirm the selected module
for the running kernel with:

```sh
modinfo -n ntsync
```

Installation does not replace a module that is already loaded. Reboot, or stop
all processes using ntsync before reloading it:

```sh
sudo modprobe -r ntsync
sudo modprobe ntsync
```

## Build

The running kernel's build tree is used by default:

```sh
make
```

The default uses LLVM because the CachyOS kernel is built with Clang. For a
kernel built with GCC, clear the extra Kbuild arguments:

```sh
make KBUILD_ARGS=
```

To build against another kernel:

```sh
make KDIR=/path/to/kernel/build
```

The resulting module is `ntsync.ko`. It has the same name as the in-tree
module, so the installed module must be unloaded before loading this one.

## Tests

Build the userspace self-tests with:

```sh
make tests
```

Run them against the currently loaded module with:

```sh
make test
```
