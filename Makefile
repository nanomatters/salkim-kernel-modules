ifneq ($(KERNELRELEASE),)

obj-m += ntsync.o
ccflags-y += -include $(src)/include/uapi/linux/ntsync.h

else

KDIR ?= /lib/modules/$(shell uname -r)/build
KBUILD_ARGS ?= LLVM=1

.PHONY: all module tests test install clean

all: module tests

module:
	$(MAKE) -C $(KDIR) M=$(CURDIR) $(KBUILD_ARGS) modules

tests:
	$(MAKE) -C tests

test: tests
	$(MAKE) -C tests run

install: module
	$(MAKE) -C $(KDIR) M=$(CURDIR) $(KBUILD_ARGS) modules_install

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) $(KBUILD_ARGS) clean
	$(MAKE) -C tests clean

endif
