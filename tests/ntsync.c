// SPDX-License-Identifier: GPL-2.0-or-later
/*
 * Various unit tests for the "ntsync" synchronization primitive driver.
 *
 * Copyright (C) 2021-2022 Elizabeth Figura <zfigura@codeweavers.com>
 */

#define _GNU_SOURCE
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <fcntl.h>
#include <poll.h>
#include <sched.h>
#include <signal.h>
#include <stdatomic.h>
#include <time.h>
#include <pthread.h>
#include <unistd.h>
#include <linux/ntsync.h>
#include <linux/userfaultfd.h>
#include "kselftest_harness.h"

#ifndef CLONE_NEWTIME
#define CLONE_NEWTIME 0x00000080
#endif

static int read_sem_state(int sem, __u32 *count, __u32 *max)
{
	struct ntsync_sem_args args;
	int ret;

	memset(&args, 0xcc, sizeof(args));
	ret = ioctl(sem, NTSYNC_IOC_SEM_READ, &args);
	*count = args.count;
	*max = args.max;
	return ret;
}

#define check_sem_state(sem, count, max) \
	({ \
		__u32 __count, __max; \
		int ret = read_sem_state((sem), &__count, &__max); \
		EXPECT_EQ(0, ret); \
		EXPECT_EQ((count), __count); \
		EXPECT_EQ((max), __max); \
	})

static int release_sem(int sem, __u32 *count)
{
	return ioctl(sem, NTSYNC_IOC_SEM_RELEASE, count);
}

static int read_mutex_state(int mutex, __u32 *count, __u32 *owner)
{
	struct ntsync_mutex_args args;
	int ret;

	memset(&args, 0xcc, sizeof(args));
	ret = ioctl(mutex, NTSYNC_IOC_MUTEX_READ, &args);
	*count = args.count;
	*owner = args.owner;
	return ret;
}

#define check_mutex_state(mutex, count, owner) \
	({ \
		__u32 __count, __owner; \
		int ret = read_mutex_state((mutex), &__count, &__owner); \
		EXPECT_EQ(0, ret); \
		EXPECT_EQ((count), __count); \
		EXPECT_EQ((owner), __owner); \
	})

static int unlock_mutex(int mutex, __u32 owner, __u32 *count)
{
	struct ntsync_mutex_args args;
	int ret;

	args.owner = owner;
	args.count = 0xdeadbeef;
	ret = ioctl(mutex, NTSYNC_IOC_MUTEX_UNLOCK, &args);
	*count = args.count;
	return ret;
}

static int read_event_state(int event, __u32 *signaled, __u32 *manual)
{
	struct ntsync_event_args args;
	int ret;

	memset(&args, 0xcc, sizeof(args));
	ret = ioctl(event, NTSYNC_IOC_EVENT_READ, &args);
	*signaled = args.signaled;
	*manual = args.manual;
	return ret;
}

#define check_event_state(event, signaled, manual) \
	({ \
		__u32 __signaled, __manual; \
		int ret = read_event_state((event), &__signaled, &__manual); \
		EXPECT_EQ(0, ret); \
		EXPECT_EQ((signaled), __signaled); \
		EXPECT_EQ((manual), __manual); \
	})

static int wait_objs(int fd, unsigned long request, __u32 count,
		     const int *objs, __u32 owner, int alert, __u32 *index)
{
	struct ntsync_wait_args args = {0};
	struct timespec timeout;
	int ret;

	clock_gettime(CLOCK_MONOTONIC, &timeout);

	args.timeout = (__u64)timeout.tv_sec * 1000000000 + timeout.tv_nsec;
	args.count = count;
	args.objs = (uintptr_t)objs;
	args.owner = owner;
	args.index = 0xdeadbeef;
	args.alert = alert;
	ret = ioctl(fd, request, &args);
	*index = args.index;
	return ret;
}

static int wait_any(int fd, __u32 count, const int *objs, __u32 owner, __u32 *index)
{
	return wait_objs(fd, NTSYNC_IOC_WAIT_ANY, count, objs, owner, 0, index);
}

static int wait_all(int fd, __u32 count, const int *objs, __u32 owner, __u32 *index)
{
	return wait_objs(fd, NTSYNC_IOC_WAIT_ALL, count, objs, owner, 0, index);
}

static int wait_any_alert(int fd, __u32 count, const int *objs,
			  __u32 owner, int alert, __u32 *index)
{
	return wait_objs(fd, NTSYNC_IOC_WAIT_ANY,
			 count, objs, owner, alert, index);
}

static int wait_all_alert(int fd, __u32 count, const int *objs,
			  __u32 owner, int alert, __u32 *index)
{
	return wait_objs(fd, NTSYNC_IOC_WAIT_ALL,
			 count, objs, owner, alert, index);
}

TEST(semaphore_state)
{
	struct ntsync_sem_args sem_args;
	struct timespec timeout;
	__u32 count, index;
	int fd, ret, sem;

	clock_gettime(CLOCK_MONOTONIC, &timeout);

	fd = open("/dev/ntsync", O_CLOEXEC | O_RDONLY);
	ASSERT_LE(0, fd);

	sem_args.count = 3;
	sem_args.max = 2;
	sem = ioctl(fd, NTSYNC_IOC_CREATE_SEM, &sem_args);
	EXPECT_EQ(-1, sem);
	EXPECT_EQ(EINVAL, errno);

	sem_args.count = 2;
	sem_args.max = 2;
	sem = ioctl(fd, NTSYNC_IOC_CREATE_SEM, &sem_args);
	EXPECT_LE(0, sem);
	check_sem_state(sem, 2, 2);

	count = 0;
	ret = release_sem(sem, &count);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(2, count);
	check_sem_state(sem, 2, 2);

	count = 1;
	ret = release_sem(sem, &count);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(EOVERFLOW, errno);
	check_sem_state(sem, 2, 2);

	ret = wait_any(fd, 1, &sem, 123, &index);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, index);
	check_sem_state(sem, 1, 2);

	ret = wait_any(fd, 1, &sem, 123, &index);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, index);
	check_sem_state(sem, 0, 2);

	ret = wait_any(fd, 1, &sem, 123, &index);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(ETIMEDOUT, errno);

	count = 3;
	ret = release_sem(sem, &count);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(EOVERFLOW, errno);
	check_sem_state(sem, 0, 2);

	count = 2;
	ret = release_sem(sem, &count);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, count);
	check_sem_state(sem, 2, 2);

	ret = wait_any(fd, 1, &sem, 123, &index);
	EXPECT_EQ(0, ret);
	ret = wait_any(fd, 1, &sem, 123, &index);
	EXPECT_EQ(0, ret);

	count = 1;
	ret = release_sem(sem, &count);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, count);
	check_sem_state(sem, 1, 2);

	count = ~0u;
	ret = release_sem(sem, &count);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(EOVERFLOW, errno);
	check_sem_state(sem, 1, 2);

	close(sem);

	close(fd);
}

TEST(mutex_state)
{
	struct ntsync_mutex_args mutex_args;
	__u32 owner, count, index;
	struct timespec timeout;
	int fd, ret, mutex;

	clock_gettime(CLOCK_MONOTONIC, &timeout);

	fd = open("/dev/ntsync", O_CLOEXEC | O_RDONLY);
	ASSERT_LE(0, fd);

	mutex_args.owner = 123;
	mutex_args.count = 0;
	mutex = ioctl(fd, NTSYNC_IOC_CREATE_MUTEX, &mutex_args);
	EXPECT_EQ(-1, mutex);
	EXPECT_EQ(EINVAL, errno);

	mutex_args.owner = 0;
	mutex_args.count = 2;
	mutex = ioctl(fd, NTSYNC_IOC_CREATE_MUTEX, &mutex_args);
	EXPECT_EQ(-1, mutex);
	EXPECT_EQ(EINVAL, errno);

	mutex_args.owner = 123;
	mutex_args.count = 2;
	mutex = ioctl(fd, NTSYNC_IOC_CREATE_MUTEX, &mutex_args);
	EXPECT_LE(0, mutex);
	check_mutex_state(mutex, 2, 123);

	ret = unlock_mutex(mutex, 0, &count);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(EINVAL, errno);

	ret = unlock_mutex(mutex, 456, &count);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(EPERM, errno);
	check_mutex_state(mutex, 2, 123);

	ret = unlock_mutex(mutex, 123, &count);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(2, count);
	check_mutex_state(mutex, 1, 123);

	ret = unlock_mutex(mutex, 123, &count);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(1, count);
	check_mutex_state(mutex, 0, 0);

	ret = unlock_mutex(mutex, 123, &count);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(EPERM, errno);

	ret = wait_any(fd, 1, &mutex, 456, &index);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, index);
	check_mutex_state(mutex, 1, 456);

	ret = wait_any(fd, 1, &mutex, 456, &index);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, index);
	check_mutex_state(mutex, 2, 456);

	ret = unlock_mutex(mutex, 456, &count);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(2, count);
	check_mutex_state(mutex, 1, 456);

	ret = wait_any(fd, 1, &mutex, 123, &index);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(ETIMEDOUT, errno);

	owner = 0;
	ret = ioctl(mutex, NTSYNC_IOC_MUTEX_KILL, &owner);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(EINVAL, errno);

	owner = 123;
	ret = ioctl(mutex, NTSYNC_IOC_MUTEX_KILL, &owner);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(EPERM, errno);
	check_mutex_state(mutex, 1, 456);

	owner = 456;
	ret = ioctl(mutex, NTSYNC_IOC_MUTEX_KILL, &owner);
	EXPECT_EQ(0, ret);

	memset(&mutex_args, 0xcc, sizeof(mutex_args));
	ret = ioctl(mutex, NTSYNC_IOC_MUTEX_READ, &mutex_args);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(EOWNERDEAD, errno);
	EXPECT_EQ(0, mutex_args.count);
	EXPECT_EQ(0, mutex_args.owner);

	memset(&mutex_args, 0xcc, sizeof(mutex_args));
	ret = ioctl(mutex, NTSYNC_IOC_MUTEX_READ, &mutex_args);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(EOWNERDEAD, errno);
	EXPECT_EQ(0, mutex_args.count);
	EXPECT_EQ(0, mutex_args.owner);

	ret = wait_any(fd, 1, &mutex, 123, &index);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(EOWNERDEAD, errno);
	EXPECT_EQ(0, index);
	check_mutex_state(mutex, 1, 123);

	owner = 123;
	ret = ioctl(mutex, NTSYNC_IOC_MUTEX_KILL, &owner);
	EXPECT_EQ(0, ret);

	memset(&mutex_args, 0xcc, sizeof(mutex_args));
	ret = ioctl(mutex, NTSYNC_IOC_MUTEX_READ, &mutex_args);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(EOWNERDEAD, errno);
	EXPECT_EQ(0, mutex_args.count);
	EXPECT_EQ(0, mutex_args.owner);

	ret = wait_any(fd, 1, &mutex, 123, &index);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(EOWNERDEAD, errno);
	EXPECT_EQ(0, index);
	check_mutex_state(mutex, 1, 123);

	close(mutex);

	mutex_args.owner = 0;
	mutex_args.count = 0;
	mutex = ioctl(fd, NTSYNC_IOC_CREATE_MUTEX, &mutex_args);
	EXPECT_LE(0, mutex);
	check_mutex_state(mutex, 0, 0);

	ret = wait_any(fd, 1, &mutex, 123, &index);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, index);
	check_mutex_state(mutex, 1, 123);

	close(mutex);

	mutex_args.owner = 123;
	mutex_args.count = ~0u;
	mutex = ioctl(fd, NTSYNC_IOC_CREATE_MUTEX, &mutex_args);
	EXPECT_LE(0, mutex);
	check_mutex_state(mutex, ~0u, 123);

	ret = wait_any(fd, 1, &mutex, 123, &index);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(ETIMEDOUT, errno);

	close(mutex);

	close(fd);
}

TEST(manual_event_state)
{
	struct ntsync_event_args event_args;
	__u32 index, signaled;
	int fd, event, ret;

	fd = open("/dev/ntsync", O_CLOEXEC | O_RDONLY);
	ASSERT_LE(0, fd);

	event_args.manual = 1;
	event_args.signaled = 0;
	event = ioctl(fd, NTSYNC_IOC_CREATE_EVENT, &event_args);
	EXPECT_LE(0, event);
	check_event_state(event, 0, 1);

	signaled = 0xdeadbeef;
	ret = ioctl(event, NTSYNC_IOC_EVENT_SET, &signaled);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, signaled);
	check_event_state(event, 1, 1);

	ret = ioctl(event, NTSYNC_IOC_EVENT_SET, &signaled);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(1, signaled);
	check_event_state(event, 1, 1);

	ret = wait_any(fd, 1, &event, 123, &index);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, index);
	check_event_state(event, 1, 1);

	signaled = 0xdeadbeef;
	ret = ioctl(event, NTSYNC_IOC_EVENT_RESET, &signaled);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(1, signaled);
	check_event_state(event, 0, 1);

	ret = ioctl(event, NTSYNC_IOC_EVENT_RESET, &signaled);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, signaled);
	check_event_state(event, 0, 1);

	ret = wait_any(fd, 1, &event, 123, &index);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(ETIMEDOUT, errno);

	ret = ioctl(event, NTSYNC_IOC_EVENT_SET, &signaled);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, signaled);

	ret = ioctl(event, NTSYNC_IOC_EVENT_PULSE, &signaled);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(1, signaled);
	check_event_state(event, 0, 1);

	ret = ioctl(event, NTSYNC_IOC_EVENT_PULSE, &signaled);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, signaled);
	check_event_state(event, 0, 1);

	close(event);

	close(fd);
}

TEST(auto_event_state)
{
	struct ntsync_event_args event_args;
	__u32 index, signaled;
	int fd, event, ret;

	fd = open("/dev/ntsync", O_CLOEXEC | O_RDONLY);
	ASSERT_LE(0, fd);

	event_args.manual = 0;
	event_args.signaled = 1;
	event = ioctl(fd, NTSYNC_IOC_CREATE_EVENT, &event_args);
	EXPECT_LE(0, event);

	check_event_state(event, 1, 0);

	signaled = 0xdeadbeef;
	ret = ioctl(event, NTSYNC_IOC_EVENT_SET, &signaled);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(1, signaled);
	check_event_state(event, 1, 0);

	ret = wait_any(fd, 1, &event, 123, &index);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, index);
	check_event_state(event, 0, 0);

	signaled = 0xdeadbeef;
	ret = ioctl(event, NTSYNC_IOC_EVENT_RESET, &signaled);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, signaled);
	check_event_state(event, 0, 0);

	ret = wait_any(fd, 1, &event, 123, &index);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(ETIMEDOUT, errno);

	ret = ioctl(event, NTSYNC_IOC_EVENT_SET, &signaled);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, signaled);

	ret = ioctl(event, NTSYNC_IOC_EVENT_PULSE, &signaled);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(1, signaled);
	check_event_state(event, 0, 0);

	ret = ioctl(event, NTSYNC_IOC_EVENT_PULSE, &signaled);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, signaled);
	check_event_state(event, 0, 0);

	close(event);

	close(fd);
}

TEST(iocp_state)
{
	struct ntsync_iocp_packet packet, received;
	struct ntsync_iocp_args args = {0};
	struct ntsync_iocp_info info;
	__u32 index;
	int fd, iocp, ret;

	fd = open("/dev/ntsync", O_CLOEXEC | O_RDONLY);
	ASSERT_LE(0, fd);

	args.concurrency = 4;
	args.version = 0;
	iocp = ioctl(fd, NTSYNC_IOC_CREATE_IOCP, &args);
	EXPECT_EQ(-1, iocp);
	EXPECT_EQ(EINVAL, errno);
	args.version = NTSYNC_IOCP_VERSION + 1;
	iocp = ioctl(fd, NTSYNC_IOC_CREATE_IOCP, &args);
	EXPECT_EQ(-1, iocp);
	EXPECT_EQ(EINVAL, errno);
	args.version = NTSYNC_IOCP_VERSION;
	iocp = ioctl(fd, NTSYNC_IOC_CREATE_IOCP, &args);
	ASSERT_LE(0, iocp);

	memset(&info, 0xcc, sizeof(info));
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_READ, &info);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(4, info.concurrency);
	EXPECT_EQ(0, info.depth);

	ret = ioctl(iocp, NTSYNC_IOC_IOCP_REMOVE, &received);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(EAGAIN, errno);

	packet.key = 0x1122334455667788ULL;
	packet.value = 0x8877665544332211ULL;
	packet.information = 0x123456789abcdef0ULL;
	packet.status = -123;
	packet.pad = 1;
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_POST, &packet);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(EINVAL, errno);

	packet.pad = 0;
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_POST, &packet);
	EXPECT_EQ(0, ret);

	packet.key++;
	packet.value++;
	packet.information++;
	packet.status--;
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_POST, &packet);
	EXPECT_EQ(0, ret);

	ret = wait_any(fd, 1, &iocp, 123, &index);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, index);

	ret = ioctl(iocp, NTSYNC_IOC_IOCP_READ, &info);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(2, info.depth);

	ret = ioctl(iocp, NTSYNC_IOC_IOCP_REMOVE, &received);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0x1122334455667788ULL, received.key);
	EXPECT_EQ(0x8877665544332211ULL, received.value);
	EXPECT_EQ(0x123456789abcdef0ULL, received.information);
	EXPECT_EQ(-123, received.status);

	ret = wait_all(fd, 1, &iocp, 123, &index);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, index);

	ret = ioctl(iocp, NTSYNC_IOC_IOCP_REMOVE, &received);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0x1122334455667789ULL, received.key);
	EXPECT_EQ(0x8877665544332212ULL, received.value);
	EXPECT_EQ(0x123456789abcdef1ULL, received.information);
	EXPECT_EQ(-124, received.status);

	ret = wait_any(fd, 1, &iocp, 123, &index);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(ETIMEDOUT, errno);

	close(iocp);
	close(fd);
}

TEST(test_wait_any)
{
	int objs[NTSYNC_MAX_WAIT_COUNT + 1], fd, ret;
	struct ntsync_mutex_args mutex_args = {0};
	struct ntsync_sem_args sem_args = {0};
	__u32 owner, index, count, i;
	struct timespec timeout;

	clock_gettime(CLOCK_MONOTONIC, &timeout);

	fd = open("/dev/ntsync", O_CLOEXEC | O_RDONLY);
	ASSERT_LE(0, fd);

	sem_args.count = 2;
	sem_args.max = 3;
	objs[0] = ioctl(fd, NTSYNC_IOC_CREATE_SEM, &sem_args);
	EXPECT_LE(0, objs[0]);

	mutex_args.owner = 0;
	mutex_args.count = 0;
	objs[1] = ioctl(fd, NTSYNC_IOC_CREATE_MUTEX, &mutex_args);
	EXPECT_LE(0, objs[1]);

	ret = wait_any(fd, 2, objs, 123, &index);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, index);
	check_sem_state(objs[0], 1, 3);
	check_mutex_state(objs[1], 0, 0);

	ret = wait_any(fd, 2, objs, 123, &index);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, index);
	check_sem_state(objs[0], 0, 3);
	check_mutex_state(objs[1], 0, 0);

	ret = wait_any(fd, 2, objs, 123, &index);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(1, index);
	check_sem_state(objs[0], 0, 3);
	check_mutex_state(objs[1], 1, 123);

	count = 1;
	ret = release_sem(objs[0], &count);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, count);

	ret = wait_any(fd, 2, objs, 123, &index);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, index);
	check_sem_state(objs[0], 0, 3);
	check_mutex_state(objs[1], 1, 123);

	ret = wait_any(fd, 2, objs, 123, &index);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(1, index);
	check_sem_state(objs[0], 0, 3);
	check_mutex_state(objs[1], 2, 123);

	ret = wait_any(fd, 2, objs, 456, &index);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(ETIMEDOUT, errno);

	owner = 123;
	ret = ioctl(objs[1], NTSYNC_IOC_MUTEX_KILL, &owner);
	EXPECT_EQ(0, ret);

	ret = wait_any(fd, 2, objs, 456, &index);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(EOWNERDEAD, errno);
	EXPECT_EQ(1, index);

	ret = wait_any(fd, 2, objs, 456, &index);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(1, index);

	close(objs[1]);

	/* test waiting on the same object twice */

	count = 2;
	ret = release_sem(objs[0], &count);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, count);

	objs[1] = objs[0];
	ret = wait_any(fd, 2, objs, 456, &index);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, index);
	check_sem_state(objs[0], 1, 3);

	ret = wait_any(fd, 0, NULL, 456, &index);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(ETIMEDOUT, errno);

	for (i = 1; i < NTSYNC_MAX_WAIT_COUNT + 1; ++i)
		objs[i] = objs[0];

	ret = wait_any(fd, NTSYNC_MAX_WAIT_COUNT, objs, 123, &index);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, index);

	ret = wait_any(fd, NTSYNC_MAX_WAIT_COUNT + 1, objs, 123, &index);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(EINVAL, errno);

	ret = wait_any(fd, -1, objs, 123, &index);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(EINVAL, errno);

	close(objs[0]);

	close(fd);
}

TEST(test_wait_all)
{
	struct ntsync_event_args event_args = {0};
	struct ntsync_mutex_args mutex_args = {0};
	struct ntsync_sem_args sem_args = {0};
	__u32 owner, index, count;
	int objs[2], fd, ret;

	fd = open("/dev/ntsync", O_CLOEXEC | O_RDONLY);
	ASSERT_LE(0, fd);

	sem_args.count = 2;
	sem_args.max = 3;
	objs[0] = ioctl(fd, NTSYNC_IOC_CREATE_SEM, &sem_args);
	EXPECT_LE(0, objs[0]);

	mutex_args.owner = 0;
	mutex_args.count = 0;
	objs[1] = ioctl(fd, NTSYNC_IOC_CREATE_MUTEX, &mutex_args);
	EXPECT_LE(0, objs[1]);

	ret = wait_all(fd, 2, objs, 123, &index);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, index);
	check_sem_state(objs[0], 1, 3);
	check_mutex_state(objs[1], 1, 123);

	ret = wait_all(fd, 2, objs, 456, &index);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(ETIMEDOUT, errno);
	check_sem_state(objs[0], 1, 3);
	check_mutex_state(objs[1], 1, 123);

	ret = wait_all(fd, 2, objs, 123, &index);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, index);
	check_sem_state(objs[0], 0, 3);
	check_mutex_state(objs[1], 2, 123);

	ret = wait_all(fd, 2, objs, 123, &index);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(ETIMEDOUT, errno);
	check_sem_state(objs[0], 0, 3);
	check_mutex_state(objs[1], 2, 123);

	count = 3;
	ret = release_sem(objs[0], &count);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, count);

	ret = wait_all(fd, 2, objs, 123, &index);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, index);
	check_sem_state(objs[0], 2, 3);
	check_mutex_state(objs[1], 3, 123);

	owner = 123;
	ret = ioctl(objs[1], NTSYNC_IOC_MUTEX_KILL, &owner);
	EXPECT_EQ(0, ret);

	ret = wait_all(fd, 2, objs, 123, &index);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(EOWNERDEAD, errno);
	check_sem_state(objs[0], 1, 3);
	check_mutex_state(objs[1], 1, 123);

	close(objs[1]);

	event_args.manual = true;
	event_args.signaled = true;
	objs[1] = ioctl(fd, NTSYNC_IOC_CREATE_EVENT, &event_args);
	EXPECT_LE(0, objs[1]);

	ret = wait_all(fd, 2, objs, 123, &index);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, index);
	check_sem_state(objs[0], 0, 3);
	check_event_state(objs[1], 1, 1);

	close(objs[1]);

	/* test waiting on the same object twice */
	objs[1] = objs[0];
	ret = wait_all(fd, 2, objs, 123, &index);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(EINVAL, errno);

	close(objs[0]);

	close(fd);
}

struct wake_args {
	int fd;
	int obj;
};

struct wait_args {
	int fd;
	unsigned long request;
	struct ntsync_wait_args *args;
	int ret;
	int err;
};

struct iocp_wait_args {
	int fd;
	struct ntsync_iocp_remove_args *args;
	int ret;
	int err;
};

static void *wait_thread(void *arg)
{
	struct wait_args *args = arg;

	args->ret = ioctl(args->fd, args->request, args->args);
	args->err = errno;
	return NULL;
}

static void *iocp_wait_thread(void *arg)
{
	struct iocp_wait_args *args = arg;

	args->ret = ioctl(args->fd, NTSYNC_IOC_IOCP_REMOVE_BATCH, args->args);
	args->err = errno;
	return NULL;
}

static __u64 get_abs_timeout(unsigned int ms)
{
	struct timespec timeout;
	clock_gettime(CLOCK_MONOTONIC, &timeout);
	return (__u64)timeout.tv_sec * 1000000000 + timeout.tv_nsec +
	       (__u64)ms * 1000000;
}

static int wait_for_thread(pthread_t thread, unsigned int ms)
{
	struct timespec timeout;

	clock_gettime(CLOCK_REALTIME, &timeout);
	timeout.tv_sec += ms / 1000;
	timeout.tv_nsec += (ms % 1000) * 1000000;
	timeout.tv_sec += (timeout.tv_nsec / 1000000000);
	timeout.tv_nsec %= 1000000000;
	return pthread_timedjoin_np(thread, NULL, &timeout);
}

TEST(iocp_remove_batch)
{
	struct ntsync_iocp_remove_args remove_args = {0};
	struct ntsync_iocp_packet packet = {0}, received[3];
	struct ntsync_iocp_packet max_packets[NTSYNC_IOCP_MAX_REMOVE];
	struct ntsync_iocp_packet *partial_packets;
	struct ntsync_event_args event_args = {0};
	struct ntsync_iocp_args create_args = {.version = NTSYNC_IOCP_VERSION};
	struct ntsync_iocp_info info;
	struct iocp_wait_args thread_args;
	void *fault_page;
	long page_size;
	pthread_t thread;
	__u32 index, signaled;
	unsigned int i;
	int event, fd, iocp, purge_iocp, ret, shutdown_iocp;

	fd = open("/dev/ntsync", O_CLOEXEC | O_RDONLY);
	ASSERT_LE(0, fd);
	iocp = ioctl(fd, NTSYNC_IOC_CREATE_IOCP, &create_args);
	ASSERT_LE(0, iocp);

	remove_args.packets = (uintptr_t)received;
	remove_args.timeout = get_abs_timeout(0);
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_REMOVE_BATCH, &remove_args);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(EINVAL, errno);

	remove_args.count = NTSYNC_IOCP_MAX_REMOVE + 1;
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_REMOVE_BATCH, &remove_args);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(EINVAL, errno);

	remove_args.count = 1;
	remove_args.flags = ~NTSYNC_WAIT_REALTIME;
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_REMOVE_BATCH, &remove_args);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(EINVAL, errno);
	remove_args.flags = 0;
	remove_args.written = 0xdeadbeef;
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_REMOVE_BATCH, &remove_args);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(ETIMEDOUT, errno);
	EXPECT_EQ(0, remove_args.written);

	packet.key = 0x100;
	packet.value = 0x200;
	packet.information = 0x300;
	packet.status = -1;
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_POST, &packet);
	EXPECT_EQ(0, ret);

	remove_args.alert = iocp;
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_REMOVE_BATCH, &remove_args);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(EINVAL, errno);
	remove_args.alert = 0;

	packet.key = 0x101;
	packet.value = 0x201;
	packet.information = 0x301;
	packet.status = -2;
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_POST, &packet);
	EXPECT_EQ(0, ret);

	page_size = sysconf(_SC_PAGESIZE);
	ASSERT_LT(0, page_size);
	fault_page = mmap(NULL, page_size, PROT_NONE,
			  MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	ASSERT_TRUE(fault_page != MAP_FAILED);
	remove_args.packets = (uintptr_t)fault_page;
	remove_args.count = 3;
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_REMOVE_BATCH, &remove_args);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(EFAULT, errno);
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_READ, &info);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(2, info.depth);
	munmap(fault_page, page_size);

	packet.key = 0x102;
	packet.value = 0x202;
	packet.information = 0x302;
	packet.status = -3;
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_POST, &packet);
	EXPECT_EQ(0, ret);
	remove_args.packets = (uintptr_t)received;
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_REMOVE_BATCH, &remove_args);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(3, remove_args.written);
	EXPECT_EQ(0x100, received[0].key);
	EXPECT_EQ(0x200, received[0].value);
	EXPECT_EQ(0x300, received[0].information);
	EXPECT_EQ(-1, received[0].status);
	EXPECT_EQ(0x101, received[1].key);
	EXPECT_EQ(0x201, received[1].value);
	EXPECT_EQ(0x301, received[1].information);
	EXPECT_EQ(-2, received[1].status);
	EXPECT_EQ(0x102, received[2].key);
	EXPECT_EQ(0x202, received[2].value);
	EXPECT_EQ(0x302, received[2].information);
	EXPECT_EQ(-3, received[2].status);

	for (packet.key = 0x200; packet.key < 0x202; packet.key++) {
		packet.value = packet.key + 0x100;
		packet.information = packet.key + 0x200;
		packet.status = -packet.key;
		ret = ioctl(iocp, NTSYNC_IOC_IOCP_POST, &packet);
		EXPECT_EQ(0, ret);
	}
	fault_page = mmap(NULL, page_size * 2, PROT_READ | PROT_WRITE,
			  MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	ASSERT_TRUE(fault_page != MAP_FAILED);
	ret = mprotect((char *)fault_page + page_size, page_size, PROT_NONE);
	ASSERT_EQ(0, ret);
	partial_packets = (void *)((char *)fault_page + page_size -
				   sizeof(*partial_packets));
	remove_args.packets = (uintptr_t)partial_packets;
	remove_args.count = 2;
	remove_args.written = 0xdeadbeef;
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_REMOVE_BATCH, &remove_args);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(EFAULT, errno);
	EXPECT_EQ(0, remove_args.written);
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_READ, &info);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(2, info.depth);
	ret = mprotect((char *)fault_page + page_size, page_size,
		       PROT_READ | PROT_WRITE);
	ASSERT_EQ(0, ret);
	remove_args.packets = (uintptr_t)received;
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_REMOVE_BATCH, &remove_args);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(2, remove_args.written);
	EXPECT_EQ(0x200, received[0].key);
	EXPECT_EQ(0x201, received[1].key);
	munmap(fault_page, page_size * 2);

	for (packet.key = 0; packet.key < 3; packet.key++) {
		packet.value = packet.key + 10;
		packet.information = packet.key + 20;
		packet.status = -packet.key;
		ret = ioctl(iocp, NTSYNC_IOC_IOCP_POST, &packet);
		EXPECT_EQ(0, ret);
	}
	remove_args.count = 2;
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_REMOVE_BATCH, &remove_args);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(2, remove_args.written);
	EXPECT_EQ(0, received[0].key);
	EXPECT_EQ(1, received[1].key);
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_REMOVE_BATCH, &remove_args);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(1, remove_args.written);
	EXPECT_EQ(2, received[0].key);

	for (i = 0; i < NTSYNC_IOCP_MAX_REMOVE; i++) {
		packet.key = 0x1000 + i;
		packet.value = 0x2000 + i;
		packet.information = 0x3000 + i;
		packet.status = -(__s32)i;
		ret = ioctl(iocp, NTSYNC_IOC_IOCP_POST, &packet);
		EXPECT_EQ(0, ret);
	}
	remove_args.packets = (uintptr_t)max_packets;
	remove_args.timeout = get_abs_timeout(1000);
	remove_args.count = NTSYNC_IOCP_MAX_REMOVE;
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_REMOVE_BATCH, &remove_args);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(NTSYNC_IOCP_MAX_REMOVE, remove_args.written);
	for (i = 0; i < NTSYNC_IOCP_MAX_REMOVE; i++) {
		EXPECT_EQ(0x1000 + i, max_packets[i].key);
		EXPECT_EQ(0x2000 + i, max_packets[i].value);
		EXPECT_EQ(0x3000 + i, max_packets[i].information);
		EXPECT_EQ(-(__s32)i, max_packets[i].status);
		EXPECT_EQ(0, max_packets[i].pad);
	}
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_READ, &info);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, info.depth);
	remove_args.packets = (uintptr_t)received;

	event_args.manual = false;
	event_args.signaled = true;
	event = ioctl(fd, NTSYNC_IOC_CREATE_EVENT, &event_args);
	ASSERT_LE(0, event);
	remove_args.alert = event;
	remove_args.timeout = get_abs_timeout(1000);
	remove_args.written = 0xdeadbeef;
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_REMOVE_BATCH, &remove_args);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, remove_args.written);
	check_event_state(event, 0, 0);

	remove_args.alert = 0;
	remove_args.count = 1;
	remove_args.timeout = get_abs_timeout(1000);
	thread_args.fd = iocp;
	thread_args.args = &remove_args;
	ret = pthread_create(&thread, NULL, iocp_wait_thread, &thread_args);
	EXPECT_EQ(0, ret);
	ret = wait_for_thread(thread, 100);
	EXPECT_EQ(ETIMEDOUT, ret);

	packet.key = 0xabcdef;
	packet.value = 0x123456;
	packet.information = 0x789abc;
	packet.status = -123;
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_POST, &packet);
	EXPECT_EQ(0, ret);
	ret = wait_for_thread(thread, 100);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, thread_args.ret);
	EXPECT_EQ(1, remove_args.written);
	EXPECT_EQ(0xabcdef, received[0].key);

	remove_args.alert = event;
	remove_args.timeout = get_abs_timeout(1000);
	ret = pthread_create(&thread, NULL, iocp_wait_thread, &thread_args);
	EXPECT_EQ(0, ret);
	ret = wait_for_thread(thread, 100);
	EXPECT_EQ(ETIMEDOUT, ret);
	ret = ioctl(event, NTSYNC_IOC_EVENT_SET, &signaled);
	EXPECT_EQ(0, ret);
	ret = wait_for_thread(thread, 100);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, thread_args.ret);
	EXPECT_EQ(0, remove_args.written);
	check_event_state(event, 0, 0);

	ret = ioctl(event, NTSYNC_IOC_EVENT_SET, &signaled);
	EXPECT_EQ(0, ret);
	packet.key = 0xfeed;
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_POST, &packet);
	EXPECT_EQ(0, ret);
	remove_args.alert = event;
	remove_args.timeout = get_abs_timeout(1000);
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_REMOVE_BATCH, &remove_args);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(1, remove_args.written);
	EXPECT_EQ(0xfeed, received[0].key);
	check_event_state(event, 1, 0);
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_REMOVE_BATCH, &remove_args);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, remove_args.written);
	check_event_state(event, 0, 0);

	/* A manual-reset alert must remain pending after repeated removals. */
	close(event);
	event_args.manual = true;
	event_args.signaled = true;
	event = ioctl(fd, NTSYNC_IOC_CREATE_EVENT, &event_args);
	ASSERT_LE(0, event);
	remove_args.alert = event;
	remove_args.timeout = 0;
	for (i = 0; i < 2; i++) {
		ret = ioctl(iocp, NTSYNC_IOC_IOCP_REMOVE_BATCH, &remove_args);
		EXPECT_EQ(0, ret);
		EXPECT_EQ(0, remove_args.written);
		check_event_state(event, 1, 1);
	}
	ret = ioctl(event, NTSYNC_IOC_EVENT_RESET, &signaled);
	EXPECT_EQ(0, ret);
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_REMOVE_BATCH, &remove_args);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(ETIMEDOUT, errno);
	EXPECT_EQ(0, remove_args.written);
	check_event_state(event, 0, 1);

	shutdown_iocp = ioctl(fd, NTSYNC_IOC_CREATE_IOCP, &create_args);
	ASSERT_LE(0, shutdown_iocp);
	remove_args.alert = 0;
	remove_args.timeout = get_abs_timeout(1000);
	remove_args.written = 0xdeadbeef;
	thread_args.fd = shutdown_iocp;
	ret = pthread_create(&thread, NULL, iocp_wait_thread, &thread_args);
	EXPECT_EQ(0, ret);
	ret = wait_for_thread(thread, 100);
	EXPECT_EQ(ETIMEDOUT, ret);
	ret = ioctl(shutdown_iocp, NTSYNC_IOC_IOCP_SHUTDOWN);
	EXPECT_EQ(0, ret);
	ret = wait_for_thread(thread, 100);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(-1, thread_args.ret);
	EXPECT_EQ(ECANCELED, thread_args.err);
	EXPECT_EQ(0, remove_args.written);

	ret = ioctl(shutdown_iocp, NTSYNC_IOC_IOCP_POST, &packet);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(ESHUTDOWN, errno);
	ret = ioctl(shutdown_iocp, NTSYNC_IOC_IOCP_REMOVE, &received[0]);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(ECANCELED, errno);
	ret = ioctl(shutdown_iocp, NTSYNC_IOC_IOCP_REMOVE_BATCH,
		    &remove_args);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(ECANCELED, errno);
	ret = wait_any(fd, 1, &shutdown_iocp, 123, &index);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, index);
	ret = ioctl(shutdown_iocp, NTSYNC_IOC_IOCP_SHUTDOWN);
	EXPECT_EQ(0, ret);
	close(shutdown_iocp);

	purge_iocp = ioctl(fd, NTSYNC_IOC_CREATE_IOCP, &create_args);
	ASSERT_LE(0, purge_iocp);
	for (packet.key = 0; packet.key < 3; packet.key++) {
		ret = ioctl(purge_iocp, NTSYNC_IOC_IOCP_POST, &packet);
		EXPECT_EQ(0, ret);
	}
	ret = ioctl(purge_iocp, NTSYNC_IOC_IOCP_READ, &info);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(3, info.depth);
	ret = ioctl(purge_iocp, NTSYNC_IOC_IOCP_SHUTDOWN);
	EXPECT_EQ(0, ret);
	ret = ioctl(purge_iocp, NTSYNC_IOC_IOCP_READ, &info);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, info.depth);
	ret = ioctl(purge_iocp, NTSYNC_IOC_IOCP_REMOVE, &received[0]);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(ECANCELED, errno);
	close(purge_iocp);

	close(event);
	close(iocp);
	close(fd);
}

#define IOCP_MAX_PENDING 65536

TEST(iocp_pending_limit)
{
	struct ntsync_iocp_remove_args remove_args = {0};
	struct ntsync_iocp_packet packet = {0};
	struct ntsync_iocp_args create_args = {.version = NTSYNC_IOCP_VERSION};
	struct ntsync_iocp_info info;
	struct iocp_wait_args thread_args;
	struct uffdio_register uffd_register = {0};
	struct uffdio_copy uffd_copy = {0};
	struct uffdio_api uffd_api = { .api = UFFD_API };
	struct uffd_msg msg;
	struct pollfd pollfd;
	void *fault_page, *source_page;
	long page_size;
	pthread_t thread;
	unsigned int i;
	int fd, iocp, ret, uffd;

	uffd = syscall(SYS_userfaultfd, O_CLOEXEC | O_NONBLOCK);
	if (uffd < 0)
		SKIP(return, "userfaultfd kernel faults are unavailable");
	ret = ioctl(uffd, UFFDIO_API, &uffd_api);
	ASSERT_EQ(0, ret);

	page_size = sysconf(_SC_PAGESIZE);
	ASSERT_LT(0, page_size);
	fault_page = mmap(NULL, page_size, PROT_READ | PROT_WRITE,
			  MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	ASSERT_TRUE(fault_page != MAP_FAILED);
	source_page = mmap(NULL, page_size, PROT_READ | PROT_WRITE,
			   MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	ASSERT_TRUE(source_page != MAP_FAILED);
	memset(source_page, 0, page_size);

	uffd_register.range.start = (uintptr_t)fault_page;
	uffd_register.range.len = page_size;
	uffd_register.mode = UFFDIO_REGISTER_MODE_MISSING;
	ret = ioctl(uffd, UFFDIO_REGISTER, &uffd_register);
	ASSERT_EQ(0, ret);

	fd = open("/dev/ntsync", O_CLOEXEC | O_RDONLY);
	ASSERT_LE(0, fd);
	iocp = ioctl(fd, NTSYNC_IOC_CREATE_IOCP, &create_args);
	ASSERT_LE(0, iocp);

	packet.key = 0xdeadbeef;
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_POST, &packet);
	ASSERT_EQ(0, ret);

	remove_args.packets = (uintptr_t)fault_page;
	remove_args.timeout = ~(__u64)0;
	remove_args.count = 1;
	thread_args.fd = iocp;
	thread_args.args = &remove_args;
	ret = pthread_create(&thread, NULL, iocp_wait_thread, &thread_args);
	ASSERT_EQ(0, ret);

	pollfd.fd = uffd;
	pollfd.events = POLLIN;
	ret = poll(&pollfd, 1, 1000);
	ASSERT_EQ(1, ret);
	ASSERT_TRUE(pollfd.revents & POLLIN);
	ret = read(uffd, &msg, sizeof(msg));
	ASSERT_EQ((int)sizeof(msg), ret);
	ASSERT_EQ(UFFD_EVENT_PAGEFAULT, msg.event);

	for (i = 1; i < IOCP_MAX_PENDING; i++) {
		packet.key = i;
		ret = ioctl(iocp, NTSYNC_IOC_IOCP_POST, &packet);
		ASSERT_EQ(0, ret);
	}
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_POST, &packet);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(ENOSPC, errno);
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_READ, &info);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(IOCP_MAX_PENDING - 1, info.depth);

	uffd_copy.dst = (uintptr_t)fault_page;
	uffd_copy.src = (uintptr_t)source_page;
	uffd_copy.len = page_size;
	ret = ioctl(uffd, UFFDIO_COPY, &uffd_copy);
	ASSERT_EQ(0, ret);
	ASSERT_EQ(page_size, uffd_copy.copy);
	ret = wait_for_thread(thread, 1000);
	ASSERT_EQ(0, ret);
	EXPECT_EQ(0, thread_args.ret);
	EXPECT_EQ(1, remove_args.written);
	EXPECT_EQ(0xdeadbeef,
		  ((struct ntsync_iocp_packet *)fault_page)->key);

	packet.key = IOCP_MAX_PENDING;
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_POST, &packet);
	EXPECT_EQ(0, ret);
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_POST, &packet);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(ENOSPC, errno);
	packet.key++;
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_POST_INTERNAL, &packet);
	EXPECT_EQ(0, ret);
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_READ, &info);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(IOCP_MAX_PENDING + 1, info.depth);
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_POST, &packet);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(ENOSPC, errno);

	close(iocp);
	close(fd);
	munmap(source_page, page_size);
	munmap(fault_page, page_size);
	close(uffd);
}

TEST(iocp_wait_all)
{
	struct ntsync_iocp_packet packet = {0}, received;
	struct ntsync_event_args event_args = {0};
	struct ntsync_iocp_args create_args = {.version = NTSYNC_IOCP_VERSION};
	struct ntsync_iocp_info info;
	__u32 index, signaled;
	int objs[2], fd, ret;

	fd = open("/dev/ntsync", O_CLOEXEC | O_RDONLY);
	ASSERT_LE(0, fd);
	objs[0] = ioctl(fd, NTSYNC_IOC_CREATE_IOCP, &create_args);
	ASSERT_LE(0, objs[0]);
	objs[1] = ioctl(fd, NTSYNC_IOC_CREATE_EVENT, &event_args);
	ASSERT_LE(0, objs[1]);

	packet.key = 0x1234;
	ret = ioctl(objs[0], NTSYNC_IOC_IOCP_POST, &packet);
	EXPECT_EQ(0, ret);
	ret = wait_all(fd, 2, objs, 123, &index);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(ETIMEDOUT, errno);

	ret = ioctl(objs[1], NTSYNC_IOC_EVENT_SET, &signaled);
	EXPECT_EQ(0, ret);
	ret = wait_all(fd, 2, objs, 123, &index);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, index);
	check_event_state(objs[1], 0, 0);
	ret = ioctl(objs[0], NTSYNC_IOC_IOCP_READ, &info);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(1, info.depth);
	ret = ioctl(objs[0], NTSYNC_IOC_IOCP_REMOVE, &received);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(packet.key, received.key);

	ret = ioctl(objs[0], NTSYNC_IOC_IOCP_SHUTDOWN);
	EXPECT_EQ(0, ret);
	ret = ioctl(objs[1], NTSYNC_IOC_EVENT_SET, &signaled);
	EXPECT_EQ(0, ret);
	ret = wait_all(fd, 2, objs, 123, &index);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, index);
	check_event_state(objs[1], 0, 0);
	ret = ioctl(objs[0], NTSYNC_IOC_IOCP_READ, &info);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, info.depth);

	close(objs[1]);
	close(objs[0]);
	close(fd);
}

TEST(iocp_alert_validation)
{
	struct ntsync_iocp_remove_args remove_args = {0};
	struct ntsync_iocp_packet received;
	struct ntsync_event_args event_args = {0};
	struct ntsync_iocp_args create_args = {.version = NTSYNC_IOCP_VERSION};
	struct ntsync_sem_args sem_args = {.max = 1};
	int event, fd, iocp, other_fd, pipefd[2], ret, sem;

	fd = open("/dev/ntsync", O_CLOEXEC | O_RDONLY);
	ASSERT_LE(0, fd);
	other_fd = open("/dev/ntsync", O_CLOEXEC | O_RDONLY);
	ASSERT_LE(0, other_fd);
	iocp = ioctl(fd, NTSYNC_IOC_CREATE_IOCP, &create_args);
	ASSERT_LE(0, iocp);
	event = ioctl(other_fd, NTSYNC_IOC_CREATE_EVENT, &event_args);
	ASSERT_LE(0, event);
	sem = ioctl(fd, NTSYNC_IOC_CREATE_SEM, &sem_args);
	ASSERT_LE(0, sem);
	ret = pipe(pipefd);
	ASSERT_EQ(0, ret);

	remove_args.packets = (uintptr_t)&received;
	remove_args.timeout = get_abs_timeout(0);
	remove_args.count = 1;
	remove_args.alert = event;
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_REMOVE_BATCH, &remove_args);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(EINVAL, errno);
	remove_args.alert = sem;
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_REMOVE_BATCH, &remove_args);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(EINVAL, errno);
	remove_args.alert = pipefd[0];
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_REMOVE_BATCH, &remove_args);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(EINVAL, errno);

	close(pipefd[1]);
	close(pipefd[0]);
	close(sem);
	close(event);
	close(iocp);
	close(other_fd);
	close(fd);
}

static void iocp_signal_handler(int signal)
{
	(void)signal;
}

TEST(iocp_signal_restart)
{
	struct ntsync_iocp_remove_args remove_args = {0};
	struct ntsync_iocp_packet packet = {0}, received;
	struct ntsync_iocp_args create_args = {.version = NTSYNC_IOCP_VERSION};
	struct iocp_wait_args thread_args;
	struct sigaction action = {0}, old_action;
	pthread_t thread;
	int fd, iocp, ret;

	fd = open("/dev/ntsync", O_CLOEXEC | O_RDONLY);
	ASSERT_LE(0, fd);
	iocp = ioctl(fd, NTSYNC_IOC_CREATE_IOCP, &create_args);
	ASSERT_LE(0, iocp);
	remove_args.packets = (uintptr_t)&received;
	remove_args.timeout = get_abs_timeout(2000);
	remove_args.count = 1;
	thread_args.fd = iocp;
	thread_args.args = &remove_args;
	action.sa_handler = iocp_signal_handler;
	sigemptyset(&action.sa_mask);
	ret = sigaction(SIGUSR1, &action, &old_action);
	ASSERT_EQ(0, ret);

	ret = pthread_create(&thread, NULL, iocp_wait_thread, &thread_args);
	ASSERT_EQ(0, ret);
	ret = wait_for_thread(thread, 100);
	EXPECT_EQ(ETIMEDOUT, ret);
	ret = pthread_kill(thread, SIGUSR1);
	EXPECT_EQ(0, ret);
	ret = wait_for_thread(thread, 1000);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(-1, thread_args.ret);
	EXPECT_EQ(EINTR, thread_args.err);
	EXPECT_EQ(0, remove_args.written);

	action.sa_flags = SA_RESTART;
	ret = sigaction(SIGUSR1, &action, NULL);
	ASSERT_EQ(0, ret);
	remove_args.timeout = get_abs_timeout(2000);
	remove_args.written = 0xdeadbeef;
	ret = pthread_create(&thread, NULL, iocp_wait_thread, &thread_args);
	ASSERT_EQ(0, ret);
	ret = wait_for_thread(thread, 100);
	EXPECT_EQ(ETIMEDOUT, ret);
	ret = pthread_kill(thread, SIGUSR1);
	EXPECT_EQ(0, ret);
	ret = wait_for_thread(thread, 100);
	EXPECT_EQ(ETIMEDOUT, ret);
	packet.key = 0xfeedface;
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_POST, &packet);
	EXPECT_EQ(0, ret);
	ret = wait_for_thread(thread, 1000);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, thread_args.ret);
	EXPECT_EQ(1, remove_args.written);
	EXPECT_EQ(packet.key, received.key);

	ret = sigaction(SIGUSR1, &old_action, NULL);
	EXPECT_EQ(0, ret);
	close(iocp);
	close(fd);
}

TEST(iocp_killed_waiter)
{
	struct ntsync_iocp_remove_args remove_args = {0};
	struct ntsync_iocp_packet packet = {0}, received;
	struct ntsync_iocp_args create_args = {.version = NTSYNC_IOCP_VERSION};
	struct iocp_wait_args thread_args;
	pthread_t thread;
	char ready;
	pid_t child;
	int fd, iocp, pipefd[2], ret, status;

	fd = open("/dev/ntsync", O_CLOEXEC | O_RDONLY);
	ASSERT_LE(0, fd);
	iocp = ioctl(fd, NTSYNC_IOC_CREATE_IOCP, &create_args);
	ASSERT_LE(0, iocp);
	ret = pipe(pipefd);
	ASSERT_EQ(0, ret);
	child = fork();
	ASSERT_LE(0, child);
	if (!child) {
		struct ntsync_iocp_remove_args child_args = {0};
		struct ntsync_iocp_packet child_packet;

		close(pipefd[0]);
		child_args.packets = (uintptr_t)&child_packet;
		child_args.timeout = ~(__u64)0;
		child_args.count = 1;
		if (write(pipefd[1], "x", 1) != 1)
			_exit(2);
		ioctl(iocp, NTSYNC_IOC_IOCP_REMOVE_BATCH, &child_args);
		_exit(1);
	}

	close(pipefd[1]);
	ret = read(pipefd[0], &ready, 1);
	ASSERT_EQ(1, ret);
	close(pipefd[0]);
	usleep(100000);
	ret = kill(child, SIGKILL);
	EXPECT_EQ(0, ret);
	ret = waitpid(child, &status, 0);
	EXPECT_EQ(child, ret);
	EXPECT_TRUE(WIFSIGNALED(status));
	if (WIFSIGNALED(status))
		EXPECT_EQ(SIGKILL, WTERMSIG(status));

	packet.key = 0x1111;
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_POST, &packet);
	EXPECT_EQ(0, ret);
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_REMOVE, &received);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(packet.key, received.key);

	remove_args.packets = (uintptr_t)&received;
	remove_args.timeout = get_abs_timeout(2000);
	remove_args.count = 1;
	thread_args.fd = iocp;
	thread_args.args = &remove_args;
	ret = pthread_create(&thread, NULL, iocp_wait_thread, &thread_args);
	ASSERT_EQ(0, ret);
	ret = wait_for_thread(thread, 100);
	EXPECT_EQ(ETIMEDOUT, ret);
	packet.key = 0x2222;
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_POST, &packet);
	EXPECT_EQ(0, ret);
	ret = wait_for_thread(thread, 1000);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, thread_args.ret);
	EXPECT_EQ(1, remove_args.written);
	EXPECT_EQ(packet.key, received.key);

	close(iocp);
	close(fd);
}

TEST(iocp_two_waiters)
{
	struct ntsync_iocp_remove_args remove_args[2] = {{0}};
	struct ntsync_iocp_packet packets[3] = {{0}}, received[2], leftover;
	struct ntsync_iocp_args create_args = {.version = NTSYNC_IOCP_VERSION};
	struct iocp_wait_args thread_args[2];
	pthread_t threads[2];
	int fd, i, iocp, ret;

	fd = open("/dev/ntsync", O_CLOEXEC | O_RDONLY);
	ASSERT_LE(0, fd);
	iocp = ioctl(fd, NTSYNC_IOC_CREATE_IOCP, &create_args);
	ASSERT_LE(0, iocp);
	for (i = 0; i < 2; i++) {
		remove_args[i].packets = (uintptr_t)&received[i];
		remove_args[i].timeout = get_abs_timeout(3000);
		remove_args[i].count = 1;
		thread_args[i].fd = iocp;
		thread_args[i].args = &remove_args[i];
		ret = pthread_create(&threads[i], NULL, iocp_wait_thread,
				     &thread_args[i]);
		ASSERT_EQ(0, ret);
		ret = wait_for_thread(threads[i], 100);
		EXPECT_EQ(ETIMEDOUT, ret);
	}

	packets[0].key = 0xaaaa;
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_POST, &packets[0]);
	EXPECT_EQ(0, ret);
	packets[1].key = 0xbbbb;
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_POST, &packets[1]);
	EXPECT_EQ(0, ret);
	packets[2].key = 0xcccc;
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_POST, &packets[2]);
	EXPECT_EQ(0, ret);

	ret = ioctl(iocp, NTSYNC_IOC_IOCP_REMOVE, &leftover);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(packets[2].key, leftover.key);

	ret = wait_for_thread(threads[1], 1000);
	EXPECT_EQ(0, ret);
	ret = wait_for_thread(threads[0], 1000);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, thread_args[1].ret);
	EXPECT_EQ(1, remove_args[1].written);
	EXPECT_EQ(packets[0].key, received[1].key);
	EXPECT_EQ(0, thread_args[0].ret);
	EXPECT_EQ(1, remove_args[0].written);
	EXPECT_EQ(packets[1].key, received[0].key);

	close(iocp);
	close(fd);
}

TEST(iocp_remove_priority)
{
	struct ntsync_iocp_remove_args remove_args = {0};
	struct ntsync_iocp_packet packets[2] = {{0}}, received;
	struct ntsync_iocp_args create_args = {.version = NTSYNC_IOCP_VERSION};
	struct ntsync_wait_args wait_args = {0};
	struct iocp_wait_args remove_thread_args;
	struct wait_args generic_thread_args;
	pthread_t generic_thread, remove_thread;
	int fd, iocp, objs[1], ret;

	fd = open("/dev/ntsync", O_CLOEXEC | O_RDONLY);
	ASSERT_LE(0, fd);
	iocp = ioctl(fd, NTSYNC_IOC_CREATE_IOCP, &create_args);
	ASSERT_LE(0, iocp);
	objs[0] = iocp;

	wait_args.timeout = get_abs_timeout(5000);
	wait_args.count = 1;
	wait_args.objs = (uintptr_t)objs;
	wait_args.owner = 123;
	wait_args.index = 0xdeadbeef;
	generic_thread_args.fd = fd;
	generic_thread_args.request = NTSYNC_IOC_WAIT_ANY;
	generic_thread_args.args = &wait_args;
	ret = pthread_create(&generic_thread, NULL, wait_thread,
			     &generic_thread_args);
	ASSERT_EQ(0, ret);
	ret = wait_for_thread(generic_thread, 100);
	EXPECT_EQ(ETIMEDOUT, ret);

	remove_args.packets = (uintptr_t)&received;
	remove_args.timeout = get_abs_timeout(5000);
	remove_args.count = 1;
	remove_thread_args.fd = iocp;
	remove_thread_args.args = &remove_args;
	ret = pthread_create(&remove_thread, NULL, iocp_wait_thread,
			     &remove_thread_args);
	ASSERT_EQ(0, ret);
	ret = wait_for_thread(remove_thread, 100);
	EXPECT_EQ(ETIMEDOUT, ret);

	packets[0].key = 0xaaaa;
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_POST, &packets[0]);
	EXPECT_EQ(0, ret);
	ret = wait_for_thread(remove_thread, 1000);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, remove_thread_args.ret);
	EXPECT_EQ(1, remove_args.written);
	EXPECT_EQ(packets[0].key, received.key);
	ret = wait_for_thread(generic_thread, 100);
	EXPECT_EQ(ETIMEDOUT, ret);

	packets[1].key = 0xbbbb;
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_POST, &packets[1]);
	EXPECT_EQ(0, ret);
	ret = wait_for_thread(generic_thread, 1000);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, generic_thread_args.ret);
	EXPECT_EQ(0, wait_args.index);
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_REMOVE, &received);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(packets[1].key, received.key);

	close(iocp);
	close(fd);
}

TEST(iocp_close_does_not_shutdown)
{
	struct ntsync_iocp_remove_args remove_args = {0};
	struct ntsync_iocp_packet received;
	struct ntsync_iocp_args create_args = {.version = NTSYNC_IOCP_VERSION};
	struct iocp_wait_args thread_args;
	pthread_t thread;
	int fd, iocp, ret, shutdown_iocp;

	fd = open("/dev/ntsync", O_CLOEXEC | O_RDONLY);
	ASSERT_LE(0, fd);
	iocp = ioctl(fd, NTSYNC_IOC_CREATE_IOCP, &create_args);
	ASSERT_LE(0, iocp);
	shutdown_iocp = dup(iocp);
	ASSERT_LE(0, shutdown_iocp);
	remove_args.packets = (uintptr_t)&received;
	remove_args.timeout = get_abs_timeout(2000);
	remove_args.count = 1;
	thread_args.fd = iocp;
	thread_args.args = &remove_args;
	ret = pthread_create(&thread, NULL, iocp_wait_thread, &thread_args);
	ASSERT_EQ(0, ret);
	ret = wait_for_thread(thread, 100);
	EXPECT_EQ(ETIMEDOUT, ret);
	close(iocp);
	ret = wait_for_thread(thread, 100);
	EXPECT_EQ(ETIMEDOUT, ret);
	ret = ioctl(shutdown_iocp, NTSYNC_IOC_IOCP_SHUTDOWN);
	EXPECT_EQ(0, ret);
	ret = wait_for_thread(thread, 1000);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(-1, thread_args.ret);
	EXPECT_EQ(ECANCELED, thread_args.err);
	EXPECT_EQ(0, remove_args.written);

	close(shutdown_iocp);
	close(fd);
}

TEST(iocp_abandon_generation)
{
	struct ntsync_iocp_remove_args remove_args = {0};
	struct ntsync_iocp_abandon_args abandon_args = {0};
	struct ntsync_iocp_packet packet = {0}, received;
	struct ntsync_iocp_args create_args = {.version = NTSYNC_IOCP_VERSION};
	struct iocp_wait_args thread_args;
	pthread_t thread;
	int fd, iocp, ret;

	fd = open("/dev/ntsync", O_CLOEXEC | O_RDONLY);
	ASSERT_LE(0, fd);
	iocp = ioctl(fd, NTSYNC_IOC_CREATE_IOCP, &create_args);
	ASSERT_LE(0, iocp);

	remove_args.packets = (uintptr_t)&received;
	remove_args.timeout = get_abs_timeout(2000);
	remove_args.count = 1;
	remove_args.generation = NTSYNC_IOCP_INITIAL_GENERATION;
	thread_args.fd = iocp;
	thread_args.args = &remove_args;
	ret = pthread_create(&thread, NULL, iocp_wait_thread, &thread_args);
	ASSERT_EQ(0, ret);
	ret = wait_for_thread(thread, 100);
	EXPECT_EQ(ETIMEDOUT, ret);

	abandon_args.generation = NTSYNC_IOCP_INITIAL_GENERATION;
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_ABANDON, &abandon_args);
	EXPECT_EQ(0, ret);
	ret = wait_for_thread(thread, 1000);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(-1, thread_args.ret);
	EXPECT_EQ(ECANCELED, thread_args.err);
	EXPECT_EQ(0, remove_args.written);

	packet.key = 0x1234;
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_POST, &packet);
	EXPECT_EQ(0, ret);
	abandon_args.generation++;
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_ABANDON, &abandon_args);
	EXPECT_EQ(0, ret);

	remove_args.timeout = get_abs_timeout(0);
	remove_args.written = 0xdeadbeef;
	remove_args.generation = abandon_args.generation;
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_REMOVE_BATCH, &remove_args);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(ECANCELED, errno);
	EXPECT_EQ(0, remove_args.written);

	remove_args.generation++;
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_REMOVE_BATCH, &remove_args);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(1, remove_args.written);
	EXPECT_EQ(packet.key, received.key);

	abandon_args.generation = NTSYNC_IOCP_INITIAL_GENERATION;
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_ABANDON, &abandon_args);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(ECANCELED, errno);
	abandon_args.generation = 0;
	ret = ioctl(iocp, NTSYNC_IOC_IOCP_ABANDON, &abandon_args);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(EINVAL, errno);

	close(iocp);
	close(fd);
}

#define IOCP_STRESS_PRODUCERS 8
#define IOCP_STRESS_CONSUMERS 8
#define IOCP_STRESS_PACKETS_PER_PRODUCER 25000

struct iocp_stress_context {
	int iocp;
	unsigned int total;
	atomic_uint consumed;
	atomic_uint errors;
	atomic_uint full_retries;
	atomic_bool stop;
	atomic_uchar *seen;
};

struct iocp_stress_producer_args {
	struct iocp_stress_context *context;
	unsigned int first;
};

static void *iocp_stress_producer(void *arg)
{
	struct iocp_stress_producer_args *args = arg;
	struct iocp_stress_context *context = args->context;
	struct ntsync_iocp_packet packet = {0};
	__u64 deadline = get_abs_timeout(30000);
	unsigned int id;
	int ret;

	for (id = args->first;
	     id < args->first + IOCP_STRESS_PACKETS_PER_PRODUCER; id++) {
		packet.key = id;
		packet.value = id ^ 0x123456789abcdef0ULL;
		packet.information = id * 17ULL + 3;
		packet.status = -(__s32)id;

		for (;;) {
			if (atomic_load_explicit(&context->stop,
						 memory_order_relaxed))
				return NULL;
			ret = ioctl(context->iocp, NTSYNC_IOC_IOCP_POST, &packet);
			if (!ret)
				break;
			if (errno == ENOSPC)
				atomic_fetch_add_explicit(&context->full_retries, 1,
							  memory_order_relaxed);
			if (errno != ENOSPC || get_abs_timeout(0) >= deadline) {
				atomic_fetch_add_explicit(&context->errors, 1,
							  memory_order_relaxed);
				atomic_store_explicit(&context->stop, true,
						      memory_order_relaxed);
				return NULL;
			}
			sched_yield();
		}
	}

	return NULL;
}

static void *iocp_stress_consumer(void *arg)
{
	struct iocp_stress_context *context = arg;
	struct ntsync_iocp_packet packets[NTSYNC_IOCP_MAX_REMOVE];
	struct ntsync_iocp_remove_args args = {
		.packets = (uintptr_t)packets,
		.timeout = ~(__u64)0,
		.count = NTSYNC_IOCP_MAX_REMOVE,
	};
	unsigned int i;
	int ret;

	for (;;) {
		ret = ioctl(context->iocp, NTSYNC_IOC_IOCP_REMOVE_BATCH, &args);
		if (ret < 0) {
			if (errno == ECANCELED)
				return NULL;
			atomic_fetch_add_explicit(&context->errors, 1,
						  memory_order_relaxed);
			atomic_store_explicit(&context->stop, true,
					      memory_order_relaxed);
			return NULL;
		}
		if (!args.written) {
			atomic_fetch_add_explicit(&context->errors, 1,
						  memory_order_relaxed);
			atomic_store_explicit(&context->stop, true,
					      memory_order_relaxed);
			return NULL;
		}

		for (i = 0; i < args.written; i++) {
			unsigned int id = packets[i].key;

			if (id >= context->total ||
			    packets[i].value != (id ^ 0x123456789abcdef0ULL) ||
			    packets[i].information != id * 17ULL + 3 ||
			    packets[i].status != -(__s32)id ||
			    packets[i].pad ||
			    atomic_exchange_explicit(&context->seen[id], 1,
						     memory_order_relaxed)) {
				atomic_fetch_add_explicit(&context->errors, 1,
							  memory_order_relaxed);
				atomic_store_explicit(&context->stop, true,
						      memory_order_relaxed);
				return NULL;
			}
			atomic_fetch_add_explicit(&context->consumed, 1,
						  memory_order_relaxed);
		}
	}
}

TEST(iocp_stress)
{
	struct ntsync_iocp_args create_args = {
		.concurrency = IOCP_STRESS_CONSUMERS,
		.version = NTSYNC_IOCP_VERSION,
	};
	struct iocp_stress_producer_args producer_args[IOCP_STRESS_PRODUCERS];
	struct iocp_stress_context context = {0};
	pthread_t producers[IOCP_STRESS_PRODUCERS];
	pthread_t consumers[IOCP_STRESS_CONSUMERS];
	__u64 deadline;
	unsigned int i, missing = 0;
	int fd, ret;

	fd = open("/dev/ntsync", O_CLOEXEC | O_RDONLY);
	ASSERT_LE(0, fd);
	context.iocp = ioctl(fd, NTSYNC_IOC_CREATE_IOCP, &create_args);
	ASSERT_LE(0, context.iocp);
	context.total = IOCP_STRESS_PRODUCERS *
			IOCP_STRESS_PACKETS_PER_PRODUCER;
	atomic_init(&context.consumed, 0);
	atomic_init(&context.errors, 0);
	atomic_init(&context.full_retries, 0);
	atomic_init(&context.stop, false);
	context.seen = calloc(context.total, sizeof(*context.seen));
	ASSERT_TRUE(context.seen != NULL);
	for (i = 0; i < context.total; i++)
		atomic_init(&context.seen[i], 0);

	for (i = 0; i < IOCP_STRESS_PRODUCERS; i++) {
		producer_args[i].context = &context;
		producer_args[i].first =
			i * IOCP_STRESS_PACKETS_PER_PRODUCER;
		ret = pthread_create(&producers[i], NULL,
				     iocp_stress_producer, &producer_args[i]);
		ASSERT_EQ(0, ret);
	}

	deadline = get_abs_timeout(10000);
	while (!atomic_load_explicit(&context.full_retries,
				     memory_order_relaxed) &&
	       !atomic_load_explicit(&context.errors, memory_order_relaxed) &&
	       get_abs_timeout(0) < deadline)
		usleep(1000);
	EXPECT_LT(0, atomic_load_explicit(&context.full_retries,
					 memory_order_relaxed));

	for (i = 0; i < IOCP_STRESS_CONSUMERS; i++) {
		ret = pthread_create(&consumers[i], NULL,
				     iocp_stress_consumer, &context);
		ASSERT_EQ(0, ret);
	}

	for (i = 0; i < IOCP_STRESS_PRODUCERS; i++) {
		ret = pthread_join(producers[i], NULL);
		EXPECT_EQ(0, ret);
	}

	deadline = get_abs_timeout(30000);
	while (atomic_load_explicit(&context.consumed, memory_order_relaxed) <
	       context.total &&
	       !atomic_load_explicit(&context.errors, memory_order_relaxed) &&
	       get_abs_timeout(0) < deadline)
		usleep(1000);

	ret = ioctl(context.iocp, NTSYNC_IOC_IOCP_SHUTDOWN);
	EXPECT_EQ(0, ret);
	for (i = 0; i < IOCP_STRESS_CONSUMERS; i++) {
		ret = wait_for_thread(consumers[i], 1000);
		EXPECT_EQ(0, ret);
	}

	for (i = 0; i < context.total; i++)
		missing += !atomic_load_explicit(&context.seen[i],
						 memory_order_relaxed);
	EXPECT_EQ(0, atomic_load_explicit(&context.errors,
					 memory_order_relaxed));
	EXPECT_EQ(context.total, atomic_load_explicit(&context.consumed,
						     memory_order_relaxed));
	EXPECT_EQ(0, missing);

	free(context.seen);
	close(context.iocp);
	close(fd);
}

#define IOCP_PROCESS_PACKETS 100000

TEST(iocp_process_shared)
{
	struct ntsync_iocp_remove_args remove_args = {0};
	struct ntsync_iocp_packet packets[NTSYNC_IOCP_MAX_REMOVE];
	struct ntsync_iocp_args create_args = {.version = NTSYNC_IOCP_VERSION};
	unsigned int received = 0, i;
	int fd, iocp, ret, status;
	pid_t child;

	fd = open("/dev/ntsync", O_CLOEXEC | O_RDONLY);
	ASSERT_LE(0, fd);
	iocp = ioctl(fd, NTSYNC_IOC_CREATE_IOCP, &create_args);
	ASSERT_LE(0, iocp);

	child = fork();
	ASSERT_LE(0, child);
	if (!child) {
		struct ntsync_iocp_packet packet = {0};
		__u64 deadline = get_abs_timeout(30000);
		unsigned int id;

		for (id = 0; id < IOCP_PROCESS_PACKETS; id++) {
			packet.key = id;
			packet.value = id ^ 0xfedcba9876543210ULL;
			packet.information = id * 31ULL + 7;
			packet.status = -(__s32)id;

			for (;;) {
				ret = ioctl(iocp, NTSYNC_IOC_IOCP_POST, &packet);
				if (!ret)
					break;
				if (errno != ENOSPC || get_abs_timeout(0) >= deadline)
					_exit(1);
				sched_yield();
			}
		}
		_exit(0);
	}

	remove_args.packets = (uintptr_t)packets;
	remove_args.timeout = get_abs_timeout(30000);
	remove_args.count = NTSYNC_IOCP_MAX_REMOVE;
	while (received < IOCP_PROCESS_PACKETS) {
		ret = ioctl(iocp, NTSYNC_IOC_IOCP_REMOVE_BATCH, &remove_args);
		if (ret) {
			EXPECT_EQ(0, ret);
			break;
		}
		for (i = 0; i < remove_args.written; i++, received++) {
			EXPECT_EQ(received, packets[i].key);
			EXPECT_EQ(received ^ 0xfedcba9876543210ULL,
				  packets[i].value);
			EXPECT_EQ(received * 31ULL + 7,
				  packets[i].information);
			EXPECT_EQ(-(__s32)received, packets[i].status);
			EXPECT_EQ(0, packets[i].pad);
		}
	}

	if (received != IOCP_PROCESS_PACKETS)
		ioctl(iocp, NTSYNC_IOC_IOCP_SHUTDOWN);
	ret = waitpid(child, &status, 0);
	EXPECT_EQ(child, ret);
	EXPECT_TRUE(WIFEXITED(status));
	if (WIFEXITED(status))
		EXPECT_EQ(0, WEXITSTATUS(status));
	EXPECT_EQ(IOCP_PROCESS_PACKETS, received);

	close(iocp);
	close(fd);
}

#define IOCP_RACE_ITERATIONS 256

struct iocp_race_wait_args {
	pthread_barrier_t *barrier;
	struct ntsync_iocp_remove_args *args;
	int fd;
	int ret;
	int err;
};

struct iocp_race_post_args {
	pthread_barrier_t *barrier;
	struct ntsync_iocp_packet *packet;
	int fd;
	int ret;
	int err;
};

static void *iocp_race_wait(void *arg)
{
	struct iocp_race_wait_args *args = arg;

	pthread_barrier_wait(args->barrier);
	args->ret = ioctl(args->fd, NTSYNC_IOC_IOCP_REMOVE_BATCH, args->args);
	args->err = errno;
	return NULL;
}

static void *iocp_race_post(void *arg)
{
	struct iocp_race_post_args *args = arg;

	pthread_barrier_wait(args->barrier);
	args->ret = ioctl(args->fd, NTSYNC_IOC_IOCP_POST, args->packet);
	args->err = errno;
	return NULL;
}

#define IOCP_ALERT_RACE_ITERATIONS 100000

struct iocp_alert_race_args {
	pthread_barrier_t barrier;
	int event;
	int ret;
};

static void *iocp_race_alert(void *arg)
{
	struct iocp_alert_race_args *args = arg;
	unsigned int i, j;
	__u32 signaled;

	for (i = 0; i < IOCP_ALERT_RACE_ITERATIONS; i++) {
		pthread_barrier_wait(&args->barrier);
		/* Vary the overlap with the remover's timeout cleanup. */
		for (j = 0; j < (i & 255); j++)
			atomic_signal_fence(memory_order_seq_cst);
		args->ret = ioctl(args->event, NTSYNC_IOC_EVENT_SET, &signaled);
		pthread_barrier_wait(&args->barrier);
	}
	return NULL;
}

TEST(iocp_alert_timeout_race)
{
	struct ntsync_iocp_args create_args = {.version = NTSYNC_IOCP_VERSION};
	struct ntsync_iocp_remove_args remove_args = {0};
	struct ntsync_event_args event_args = {0};
	struct ntsync_iocp_packet received;
	struct iocp_alert_race_args thread_args;
	unsigned int i, lost = 0;
	__u32 signaled, manual;
	pthread_t thread;
	int fd, iocp, ret, err;

	fd = open("/dev/ntsync", O_CLOEXEC | O_RDONLY);
	ASSERT_LE(0, fd);
	iocp = ioctl(fd, NTSYNC_IOC_CREATE_IOCP, &create_args);
	ASSERT_LE(0, iocp);
	thread_args.event = ioctl(fd, NTSYNC_IOC_CREATE_EVENT, &event_args);
	ASSERT_LE(0, thread_args.event);
	remove_args.packets = (uintptr_t)&received;
	remove_args.count = 1;
	remove_args.alert = thread_args.event;

	ret = pthread_barrier_init(&thread_args.barrier, NULL, 2);
	ASSERT_EQ(0, ret);
	ret = pthread_create(&thread, NULL, iocp_race_alert, &thread_args);
	ASSERT_EQ(0, ret);

	for (i = 0; i < IOCP_ALERT_RACE_ITERATIONS; i++) {
		ret = ioctl(thread_args.event, NTSYNC_IOC_EVENT_RESET, &signaled);
		ASSERT_EQ(0, ret);
		remove_args.written = 0xdeadbeef;
		pthread_barrier_wait(&thread_args.barrier);
		/* The empty port and expired timeout race only against the alert. */
		ret = ioctl(iocp, NTSYNC_IOC_IOCP_REMOVE_BATCH, &remove_args);
		err = errno;
		pthread_barrier_wait(&thread_args.barrier);
		ASSERT_EQ(0, thread_args.ret);
		ASSERT_TRUE(!ret || (ret == -1 && err == ETIMEDOUT));
		ASSERT_EQ(0, remove_args.written);
		ASSERT_EQ(0, read_event_state(thread_args.event, &signaled, &manual));
		ASSERT_EQ(0, manual);
		if (!ret) {
			ASSERT_EQ(0, signaled);
		} else if (!signaled) {
			lost++;
		}
	}

	ret = wait_for_thread(thread, 1000);
	ASSERT_EQ(0, ret);
	ret = pthread_barrier_destroy(&thread_args.barrier);
	EXPECT_EQ(0, ret);
	/* A timeout must leave an unconsumed alert for the next wait. */
	EXPECT_EQ(0, lost);
	close(thread_args.event);
	close(iocp);
	close(fd);
}

TEST(iocp_shutdown_race)
{
	struct ntsync_iocp_args create_args = {.version = NTSYNC_IOCP_VERSION};
	unsigned int iteration;
	int fd;

	fd = open("/dev/ntsync", O_CLOEXEC | O_RDONLY);
	ASSERT_LE(0, fd);

	for (iteration = 0; iteration < IOCP_RACE_ITERATIONS; iteration++) {
		struct ntsync_iocp_remove_args remove_args = {0};
		struct ntsync_iocp_packet packet = {0}, received;
		struct iocp_race_wait_args wait_args;
		struct iocp_race_post_args post_args;
		struct ntsync_iocp_info info;
		pthread_barrier_t barrier;
		pthread_t waiter, poster;
		int iocp, ret;

		iocp = ioctl(fd, NTSYNC_IOC_CREATE_IOCP, &create_args);
		ASSERT_LE(0, iocp);
		packet.key = iteration;
		packet.value = iteration ^ 0x123456789abcdef0ULL;
		packet.information = iteration * 13ULL + 5;
		packet.status = -(__s32)iteration;
		remove_args.packets = (uintptr_t)&received;
		remove_args.timeout = get_abs_timeout(1000);
		remove_args.count = 1;

		ret = pthread_barrier_init(&barrier, NULL, 3);
		ASSERT_EQ(0, ret);
		wait_args.barrier = &barrier;
		wait_args.args = &remove_args;
		wait_args.fd = iocp;
		post_args.barrier = &barrier;
		post_args.packet = &packet;
		post_args.fd = iocp;
		ret = pthread_create(&waiter, NULL, iocp_race_wait, &wait_args);
		ASSERT_EQ(0, ret);
		ret = pthread_create(&poster, NULL, iocp_race_post, &post_args);
		ASSERT_EQ(0, ret);

		pthread_barrier_wait(&barrier);
		ret = ioctl(iocp, NTSYNC_IOC_IOCP_SHUTDOWN);
		EXPECT_EQ(0, ret);
		ret = wait_for_thread(waiter, 2000);
		EXPECT_EQ(0, ret);
		ret = wait_for_thread(poster, 2000);
		EXPECT_EQ(0, ret);

		EXPECT_TRUE(!post_args.ret ||
			    (post_args.ret == -1 && post_args.err == ESHUTDOWN));
		EXPECT_TRUE(!wait_args.ret ||
			    (wait_args.ret == -1 && wait_args.err == ECANCELED));
		if (!wait_args.ret) {
			EXPECT_EQ(0, post_args.ret);
			EXPECT_EQ(1, remove_args.written);
			EXPECT_EQ(packet.key, received.key);
			EXPECT_EQ(packet.value, received.value);
			EXPECT_EQ(packet.information, received.information);
			EXPECT_EQ(packet.status, received.status);
			EXPECT_EQ(0, received.pad);
		}

		ret = ioctl(iocp, NTSYNC_IOC_IOCP_READ, &info);
		EXPECT_EQ(0, ret);
		EXPECT_EQ(0, info.depth);
		ret = pthread_barrier_destroy(&barrier);
		EXPECT_EQ(0, ret);
		close(iocp);
	}

	close(fd);
}

TEST(wake_any)
{
	struct ntsync_event_args event_args = {0};
	struct ntsync_mutex_args mutex_args = {0};
	struct ntsync_wait_args wait_args = {0};
	struct ntsync_sem_args sem_args = {0};
	struct wait_args thread_args;
	__u32 count, index, signaled;
	int objs[2], fd, ret;
	pthread_t thread;

	fd = open("/dev/ntsync", O_CLOEXEC | O_RDONLY);
	ASSERT_LE(0, fd);

	sem_args.count = 0;
	sem_args.max = 3;
	objs[0] = ioctl(fd, NTSYNC_IOC_CREATE_SEM, &sem_args);
	EXPECT_LE(0, objs[0]);

	mutex_args.owner = 123;
	mutex_args.count = 1;
	objs[1] = ioctl(fd, NTSYNC_IOC_CREATE_MUTEX, &mutex_args);
	EXPECT_LE(0, objs[1]);

	/* test waking the semaphore */

	wait_args.timeout = get_abs_timeout(1000);
	wait_args.objs = (uintptr_t)objs;
	wait_args.count = 2;
	wait_args.owner = 456;
	wait_args.index = 0xdeadbeef;
	thread_args.fd = fd;
	thread_args.args = &wait_args;
	thread_args.request = NTSYNC_IOC_WAIT_ANY;
	ret = pthread_create(&thread, NULL, wait_thread, &thread_args);
	EXPECT_EQ(0, ret);

	ret = wait_for_thread(thread, 100);
	EXPECT_EQ(ETIMEDOUT, ret);

	count = 1;
	ret = release_sem(objs[0], &count);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, count);
	check_sem_state(objs[0], 0, 3);

	ret = wait_for_thread(thread, 100);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, thread_args.ret);
	EXPECT_EQ(0, wait_args.index);

	/* test waking the mutex */

	/* first grab it again for owner 123 */
	ret = wait_any(fd, 1, &objs[1], 123, &index);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, index);

	wait_args.timeout = get_abs_timeout(1000);
	wait_args.owner = 456;
	ret = pthread_create(&thread, NULL, wait_thread, &thread_args);
	EXPECT_EQ(0, ret);

	ret = wait_for_thread(thread, 100);
	EXPECT_EQ(ETIMEDOUT, ret);

	ret = unlock_mutex(objs[1], 123, &count);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(2, count);

	ret = pthread_tryjoin_np(thread, NULL);
	EXPECT_EQ(EBUSY, ret);

	ret = unlock_mutex(objs[1], 123, &count);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(1, mutex_args.count);
	check_mutex_state(objs[1], 1, 456);

	ret = wait_for_thread(thread, 100);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, thread_args.ret);
	EXPECT_EQ(1, wait_args.index);

	close(objs[1]);

	/* test waking events */

	event_args.manual = false;
	event_args.signaled = false;
	objs[1] = ioctl(fd, NTSYNC_IOC_CREATE_EVENT, &event_args);
	EXPECT_LE(0, objs[1]);

	wait_args.timeout = get_abs_timeout(1000);
	ret = pthread_create(&thread, NULL, wait_thread, &thread_args);
	EXPECT_EQ(0, ret);

	ret = wait_for_thread(thread, 100);
	EXPECT_EQ(ETIMEDOUT, ret);

	ret = ioctl(objs[1], NTSYNC_IOC_EVENT_SET, &signaled);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, signaled);
	check_event_state(objs[1], 0, 0);

	ret = wait_for_thread(thread, 100);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, thread_args.ret);
	EXPECT_EQ(1, wait_args.index);

	wait_args.timeout = get_abs_timeout(1000);
	ret = pthread_create(&thread, NULL, wait_thread, &thread_args);
	EXPECT_EQ(0, ret);

	ret = wait_for_thread(thread, 100);
	EXPECT_EQ(ETIMEDOUT, ret);

	ret = ioctl(objs[1], NTSYNC_IOC_EVENT_PULSE, &signaled);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, signaled);
	check_event_state(objs[1], 0, 0);

	ret = wait_for_thread(thread, 100);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, thread_args.ret);
	EXPECT_EQ(1, wait_args.index);

	close(objs[1]);

	event_args.manual = true;
	event_args.signaled = false;
	objs[1] = ioctl(fd, NTSYNC_IOC_CREATE_EVENT, &event_args);
	EXPECT_LE(0, objs[1]);

	wait_args.timeout = get_abs_timeout(1000);
	ret = pthread_create(&thread, NULL, wait_thread, &thread_args);
	EXPECT_EQ(0, ret);

	ret = wait_for_thread(thread, 100);
	EXPECT_EQ(ETIMEDOUT, ret);

	ret = ioctl(objs[1], NTSYNC_IOC_EVENT_SET, &signaled);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, signaled);
	check_event_state(objs[1], 1, 1);

	ret = wait_for_thread(thread, 100);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, thread_args.ret);
	EXPECT_EQ(1, wait_args.index);

	ret = ioctl(objs[1], NTSYNC_IOC_EVENT_RESET, &signaled);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(1, signaled);

	wait_args.timeout = get_abs_timeout(1000);
	ret = pthread_create(&thread, NULL, wait_thread, &thread_args);
	EXPECT_EQ(0, ret);

	ret = wait_for_thread(thread, 100);
	EXPECT_EQ(ETIMEDOUT, ret);

	ret = ioctl(objs[1], NTSYNC_IOC_EVENT_PULSE, &signaled);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, signaled);
	check_event_state(objs[1], 0, 1);

	ret = wait_for_thread(thread, 100);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, thread_args.ret);
	EXPECT_EQ(1, wait_args.index);

	/* delete an object while it's being waited on */

	wait_args.timeout = get_abs_timeout(200);
	wait_args.owner = 123;
	ret = pthread_create(&thread, NULL, wait_thread, &thread_args);
	EXPECT_EQ(0, ret);

	ret = wait_for_thread(thread, 100);
	EXPECT_EQ(ETIMEDOUT, ret);

	close(objs[0]);
	close(objs[1]);

	ret = wait_for_thread(thread, 200);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(-1, thread_args.ret);
	EXPECT_EQ(ETIMEDOUT, thread_args.err);

	close(fd);
}

TEST(wake_all)
{
	struct ntsync_event_args manual_event_args = {0};
	struct ntsync_event_args auto_event_args = {0};
	struct ntsync_mutex_args mutex_args = {0};
	struct ntsync_wait_args wait_args = {0};
	struct ntsync_sem_args sem_args = {0};
	struct wait_args thread_args;
	__u32 count, index, signaled;
	int objs[4], fd, ret;
	pthread_t thread;

	fd = open("/dev/ntsync", O_CLOEXEC | O_RDONLY);
	ASSERT_LE(0, fd);

	sem_args.count = 0;
	sem_args.max = 3;
	objs[0] = ioctl(fd, NTSYNC_IOC_CREATE_SEM, &sem_args);
	EXPECT_LE(0, objs[0]);

	mutex_args.owner = 123;
	mutex_args.count = 1;
	objs[1] = ioctl(fd, NTSYNC_IOC_CREATE_MUTEX, &mutex_args);
	EXPECT_LE(0, objs[1]);

	manual_event_args.manual = true;
	manual_event_args.signaled = true;
	objs[2] = ioctl(fd, NTSYNC_IOC_CREATE_EVENT, &manual_event_args);
	EXPECT_LE(0, objs[2]);

	auto_event_args.manual = false;
	auto_event_args.signaled = true;
	objs[3] = ioctl(fd, NTSYNC_IOC_CREATE_EVENT, &auto_event_args);
	EXPECT_LE(0, objs[3]);

	wait_args.timeout = get_abs_timeout(1000);
	wait_args.objs = (uintptr_t)objs;
	wait_args.count = 4;
	wait_args.owner = 456;
	thread_args.fd = fd;
	thread_args.args = &wait_args;
	thread_args.request = NTSYNC_IOC_WAIT_ALL;
	ret = pthread_create(&thread, NULL, wait_thread, &thread_args);
	EXPECT_EQ(0, ret);

	ret = wait_for_thread(thread, 100);
	EXPECT_EQ(ETIMEDOUT, ret);

	count = 1;
	ret = release_sem(objs[0], &count);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, count);

	ret = pthread_tryjoin_np(thread, NULL);
	EXPECT_EQ(EBUSY, ret);

	check_sem_state(objs[0], 1, 3);

	ret = wait_any(fd, 1, &objs[0], 123, &index);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, index);

	ret = unlock_mutex(objs[1], 123, &count);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(1, count);

	ret = pthread_tryjoin_np(thread, NULL);
	EXPECT_EQ(EBUSY, ret);

	check_mutex_state(objs[1], 0, 0);

	ret = ioctl(objs[2], NTSYNC_IOC_EVENT_RESET, &signaled);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(1, signaled);

	count = 2;
	ret = release_sem(objs[0], &count);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, count);
	check_sem_state(objs[0], 2, 3);

	ret = ioctl(objs[3], NTSYNC_IOC_EVENT_RESET, &signaled);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(1, signaled);

	ret = ioctl(objs[2], NTSYNC_IOC_EVENT_SET, &signaled);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, signaled);

	ret = ioctl(objs[3], NTSYNC_IOC_EVENT_SET, &signaled);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, signaled);

	check_sem_state(objs[0], 1, 3);
	check_mutex_state(objs[1], 1, 456);
	check_event_state(objs[2], 1, 1);
	check_event_state(objs[3], 0, 0);

	ret = wait_for_thread(thread, 100);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, thread_args.ret);

	/* delete an object while it's being waited on */

	wait_args.timeout = get_abs_timeout(200);
	wait_args.owner = 123;
	ret = pthread_create(&thread, NULL, wait_thread, &thread_args);
	EXPECT_EQ(0, ret);

	ret = wait_for_thread(thread, 100);
	EXPECT_EQ(ETIMEDOUT, ret);

	close(objs[0]);
	close(objs[1]);
	close(objs[2]);
	close(objs[3]);

	ret = wait_for_thread(thread, 200);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(-1, thread_args.ret);
	EXPECT_EQ(ETIMEDOUT, thread_args.err);

	close(fd);
}

TEST(alert_any)
{
	struct ntsync_event_args event_args = {0};
	struct ntsync_wait_args wait_args = {0};
	struct ntsync_sem_args sem_args = {0};
	__u32 index, count, signaled;
	struct wait_args thread_args;
	int objs[2], event, fd, ret;
	pthread_t thread;

	fd = open("/dev/ntsync", O_CLOEXEC | O_RDONLY);
	ASSERT_LE(0, fd);

	sem_args.count = 0;
	sem_args.max = 2;
	objs[0] = ioctl(fd, NTSYNC_IOC_CREATE_SEM, &sem_args);
	EXPECT_LE(0, objs[0]);

	sem_args.count = 1;
	sem_args.max = 2;
	objs[1] = ioctl(fd, NTSYNC_IOC_CREATE_SEM, &sem_args);
	EXPECT_LE(0, objs[1]);

	event_args.manual = true;
	event_args.signaled = true;
	event = ioctl(fd, NTSYNC_IOC_CREATE_EVENT, &event_args);
	EXPECT_LE(0, event);

	ret = wait_any_alert(fd, 0, NULL, 123, event, &index);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, index);

	ret = ioctl(event, NTSYNC_IOC_EVENT_RESET, &signaled);
	EXPECT_EQ(0, ret);

	ret = wait_any_alert(fd, 0, NULL, 123, event, &index);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(ETIMEDOUT, errno);

	ret = ioctl(event, NTSYNC_IOC_EVENT_SET, &signaled);
	EXPECT_EQ(0, ret);

	ret = wait_any_alert(fd, 2, objs, 123, event, &index);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(1, index);

	ret = wait_any_alert(fd, 2, objs, 123, event, &index);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(2, index);

	/* test wakeup via alert */

	ret = ioctl(event, NTSYNC_IOC_EVENT_RESET, &signaled);
	EXPECT_EQ(0, ret);

	wait_args.timeout = get_abs_timeout(1000);
	wait_args.objs = (uintptr_t)objs;
	wait_args.count = 2;
	wait_args.owner = 123;
	wait_args.index = 0xdeadbeef;
	wait_args.alert = event;
	thread_args.fd = fd;
	thread_args.args = &wait_args;
	thread_args.request = NTSYNC_IOC_WAIT_ANY;
	ret = pthread_create(&thread, NULL, wait_thread, &thread_args);
	EXPECT_EQ(0, ret);

	ret = wait_for_thread(thread, 100);
	EXPECT_EQ(ETIMEDOUT, ret);

	ret = ioctl(event, NTSYNC_IOC_EVENT_SET, &signaled);
	EXPECT_EQ(0, ret);

	ret = wait_for_thread(thread, 100);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, thread_args.ret);
	EXPECT_EQ(2, wait_args.index);

	close(event);

	/* test with an auto-reset event */

	event_args.manual = false;
	event_args.signaled = true;
	event = ioctl(fd, NTSYNC_IOC_CREATE_EVENT, &event_args);
	EXPECT_LE(0, event);

	count = 1;
	ret = release_sem(objs[0], &count);
	EXPECT_EQ(0, ret);

	ret = wait_any_alert(fd, 2, objs, 123, event, &index);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, index);

	ret = wait_any_alert(fd, 2, objs, 123, event, &index);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(2, index);

	ret = wait_any_alert(fd, 2, objs, 123, event, &index);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(ETIMEDOUT, errno);

	close(event);

	close(objs[0]);
	close(objs[1]);

	close(fd);
}

TEST(alert_all)
{
	struct ntsync_event_args event_args = {0};
	struct ntsync_wait_args wait_args = {0};
	struct ntsync_sem_args sem_args = {0};
	struct wait_args thread_args;
	__u32 index, count, signaled;
	int objs[2], event, fd, ret;
	pthread_t thread;

	fd = open("/dev/ntsync", O_CLOEXEC | O_RDONLY);
	ASSERT_LE(0, fd);

	sem_args.count = 2;
	sem_args.max = 2;
	objs[0] = ioctl(fd, NTSYNC_IOC_CREATE_SEM, &sem_args);
	EXPECT_LE(0, objs[0]);

	sem_args.count = 1;
	sem_args.max = 2;
	objs[1] = ioctl(fd, NTSYNC_IOC_CREATE_SEM, &sem_args);
	EXPECT_LE(0, objs[1]);

	event_args.manual = true;
	event_args.signaled = true;
	event = ioctl(fd, NTSYNC_IOC_CREATE_EVENT, &event_args);
	EXPECT_LE(0, event);

	ret = wait_all_alert(fd, 2, objs, 123, event, &index);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, index);

	ret = wait_all_alert(fd, 2, objs, 123, event, &index);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(2, index);

	/* test wakeup via alert */

	ret = ioctl(event, NTSYNC_IOC_EVENT_RESET, &signaled);
	EXPECT_EQ(0, ret);

	wait_args.timeout = get_abs_timeout(1000);
	wait_args.objs = (uintptr_t)objs;
	wait_args.count = 2;
	wait_args.owner = 123;
	wait_args.index = 0xdeadbeef;
	wait_args.alert = event;
	thread_args.fd = fd;
	thread_args.args = &wait_args;
	thread_args.request = NTSYNC_IOC_WAIT_ALL;
	ret = pthread_create(&thread, NULL, wait_thread, &thread_args);
	EXPECT_EQ(0, ret);

	ret = wait_for_thread(thread, 100);
	EXPECT_EQ(ETIMEDOUT, ret);

	ret = ioctl(event, NTSYNC_IOC_EVENT_SET, &signaled);
	EXPECT_EQ(0, ret);

	ret = wait_for_thread(thread, 100);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, thread_args.ret);
	EXPECT_EQ(2, wait_args.index);

	close(event);

	/* test with an auto-reset event */

	event_args.manual = false;
	event_args.signaled = true;
	event = ioctl(fd, NTSYNC_IOC_CREATE_EVENT, &event_args);
	EXPECT_LE(0, event);

	count = 2;
	ret = release_sem(objs[1], &count);
	EXPECT_EQ(0, ret);

	ret = wait_all_alert(fd, 2, objs, 123, event, &index);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(0, index);

	ret = wait_all_alert(fd, 2, objs, 123, event, &index);
	EXPECT_EQ(0, ret);
	EXPECT_EQ(2, index);

	ret = wait_all_alert(fd, 2, objs, 123, event, &index);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(ETIMEDOUT, errno);

	close(event);

	close(objs[0]);
	close(objs[1]);

	close(fd);
}

#define STRESS_LOOPS 10000
#define STRESS_THREADS 4

static atomic_uint stress_counter;
static atomic_uint stress_inside;
static atomic_uint stress_overlap;
static int stress_device, stress_start_event, stress_mutex;

static void *stress_thread(void *arg)
{
	struct ntsync_wait_args wait_args = {0};
	__u32 index, count, i;
	int ret;

	wait_args.timeout = UINT64_MAX;
	wait_args.count = 1;
	wait_args.objs = (uintptr_t)&stress_start_event;
	wait_args.owner = gettid();
	wait_args.index = 0xdeadbeef;

	ioctl(stress_device, NTSYNC_IOC_WAIT_ANY, &wait_args);

	wait_args.objs = (uintptr_t)&stress_mutex;

	for (i = 0; i < STRESS_LOOPS; ++i) {
		ioctl(stress_device, NTSYNC_IOC_WAIT_ANY, &wait_args);

		if (atomic_fetch_add(&stress_inside, 1))
			atomic_store(&stress_overlap, 1);
		atomic_fetch_add(&stress_counter, 1);
		if (atomic_fetch_sub(&stress_inside, 1) != 1)
			atomic_store(&stress_overlap, 1);

		unlock_mutex(stress_mutex, wait_args.owner, &count);
	}

	return NULL;
}

TEST(stress_wait)
{
	struct ntsync_event_args event_args;
	struct ntsync_mutex_args mutex_args;
	pthread_t threads[STRESS_THREADS];
	__u32 signaled, i;
	int ret;

	stress_device = open("/dev/ntsync", O_CLOEXEC | O_RDONLY);
	ASSERT_LE(0, stress_device);

	mutex_args.owner = 0;
	mutex_args.count = 0;
	stress_mutex = ioctl(stress_device, NTSYNC_IOC_CREATE_MUTEX, &mutex_args);
	EXPECT_LE(0, stress_mutex);

	event_args.manual = 1;
	event_args.signaled = 0;
	stress_start_event = ioctl(stress_device, NTSYNC_IOC_CREATE_EVENT, &event_args);
	EXPECT_LE(0, stress_start_event);

	for (i = 0; i < STRESS_THREADS; ++i)
		pthread_create(&threads[i], NULL, stress_thread, NULL);

	ret = ioctl(stress_start_event, NTSYNC_IOC_EVENT_SET, &signaled);
	EXPECT_EQ(0, ret);

	for (i = 0; i < STRESS_THREADS; ++i) {
		ret = pthread_join(threads[i], NULL);
		EXPECT_EQ(0, ret);
	}

	EXPECT_EQ(STRESS_LOOPS * STRESS_THREADS, atomic_load(&stress_counter));
	EXPECT_EQ(0, atomic_load(&stress_overlap));

	close(stress_start_event);
	close(stress_mutex);
	close(stress_device);
}

TEST(wait_args_validation)
{
	struct ntsync_sem_args sem_args = { .count = 1, .max = 1 };
	struct ntsync_wait_args wait_args = {0};
	struct timespec timeout;
	int fd, fd2, sem, ret;
	__u32 index;

	fd = open("/dev/ntsync", O_CLOEXEC | O_RDONLY);
	ASSERT_GE(fd, 0);

	fd2 = open("/dev/ntsync", O_CLOEXEC | O_RDONLY);
	ASSERT_GE(fd2, 0);

	sem = ioctl(fd, NTSYNC_IOC_CREATE_SEM, &sem_args);
	EXPECT_GE(sem, 0);

	ret = wait_any(fd, 1, &sem, 0, &index);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(EINVAL, errno);

	ret = wait_all(fd, 1, &sem, 0, &index);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(EINVAL, errno);

	clock_gettime(CLOCK_MONOTONIC, &timeout);
	wait_args.timeout = timeout.tv_sec * 1000000000ULL + timeout.tv_nsec;
	wait_args.count = 0;
	wait_args.objs = 0;
	wait_args.owner = 123;
	wait_args.pad = 1;
	ret = ioctl(fd, NTSYNC_IOC_WAIT_ANY, &wait_args);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(EINVAL, errno);

	ret = wait_any(fd2, 1, &sem, 123, &index);
	EXPECT_EQ(-1, ret);
	EXPECT_EQ(EINVAL, errno);

	close(sem);
	close(fd2);
	close(fd);
}

/*
 * Absolute MONOTONIC timeouts must honour the caller's time namespace.
 * With a negative monotonic offset, a 100 ms wait must still take ~100 ms
 * of namespace time (not return immediately against the host clock).
 */
TEST(wait_any_monotonic_timens)
{
	struct ntsync_sem_args sem_args = {0};
	struct ntsync_wait_args wait_args = {0};
	struct timespec start, end;
	char buf[64];
	__u64 elapsed_ns;
	int fd, offset_fd, sem, ret, status, len;
	pid_t pid;

	if (access("/proc/self/ns/time", F_OK))
		SKIP(return, "Time namespaces are not supported");

	fd = open("/dev/ntsync", O_CLOEXEC | O_RDONLY);
	if (fd < 0)
		SKIP(return, "/dev/ntsync is not available");

	ret = unshare(CLONE_NEWTIME);
	if (ret) {
		close(fd);
		if (errno == EPERM)
			SKIP(return, "need CAP_SYS_ADMIN for CLONE_NEWTIME");
		ASSERT_EQ(0, ret);
	}

	len = snprintf(buf, sizeof(buf), "%d %d 0", CLOCK_MONOTONIC, -10);
	offset_fd = open("/proc/self/timens_offsets", O_WRONLY);
	ASSERT_LE(0, offset_fd);
	ASSERT_EQ(len, write(offset_fd, buf, len));
	close(offset_fd);

	pid = fork();
	ASSERT_LE(0, pid);
	if (!pid) {
		int obj;

		sem_args.count = 0;
		sem_args.max = 1;
		sem = ioctl(fd, NTSYNC_IOC_CREATE_SEM, &sem_args);
		if (sem < 0)
			_exit(1);

		obj = sem;
		wait_args.timeout = get_abs_timeout(100);
		wait_args.objs = (uintptr_t)&obj;
		wait_args.count = 1;
		wait_args.owner = 123;
		wait_args.index = 0xdeadbeef;

		if (clock_gettime(CLOCK_MONOTONIC, &start))
			_exit(2);
		ret = ioctl(fd, NTSYNC_IOC_WAIT_ANY, &wait_args);
		if (clock_gettime(CLOCK_MONOTONIC, &end))
			_exit(2);

		if (ret != -1 || errno != ETIMEDOUT)
			_exit(3);

		elapsed_ns = (end.tv_sec - start.tv_sec) * 1000000000ULL +
			     (end.tv_nsec - start.tv_nsec);
		/* Without timens conversion this returns in ~0 ms. */
		if (elapsed_ns < 50 * 1000000ULL)
			_exit(4);
		if (elapsed_ns > 1000 * 1000000ULL)
			_exit(5);

		_exit(0);
	}

	ASSERT_EQ(pid, waitpid(pid, &status, 0));
	EXPECT_TRUE(WIFEXITED(status));
	EXPECT_EQ(0, WEXITSTATUS(status));

	close(fd);
}

TEST_HARNESS_MAIN
