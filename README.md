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
