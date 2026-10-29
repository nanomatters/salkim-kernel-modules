# SPDX-License-Identifier: GPL-2.0-only

KDIR ?= /lib/modules/$(shell uname -r)/build
KBUILD_ARGS ?= $(shell grep -q '^CONFIG_CC_IS_CLANG=y' $(KDIR)/.config && printf 'LLVM=1')

.PHONY: all module check-kernel clean

all: module

check-kernel:
	@grep -Eq '^7\.3\.' "$(KDIR)/include/config/kernel.release" || \
		{ echo 'AMDGPU Salkim currently supports Linux 7.3.x only.' >&2; exit 1; }
	@grep -q '^CONFIG_DRM_AMDGPU=m' "$(KDIR)/.config" || \
		{ echo 'The target kernel must build AMDGPU as a module.' >&2; exit 1; }

module: check-kernel
	$(MAKE) -C $(KDIR) M=$(CURDIR) $(KBUILD_ARGS) modules

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) $(KBUILD_ARGS) clean
