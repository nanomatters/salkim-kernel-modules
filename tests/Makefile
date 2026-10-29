# SPDX-License-Identifier: GPL-2.0-only
CC ?= cc
CPPFLAGS += -I../include/uapi
CFLAGS ?= -O2 -g
LDLIBS += -lpthread

.PHONY: all run clean

all: ntsync

ntsync: ntsync.c kselftest_harness.h kselftest.h
	$(CC) $(CPPFLAGS) $(CFLAGS) -o $@ ntsync.c $(LDLIBS)

run: ntsync
	./ntsync

clean:
	$(RM) ntsync
