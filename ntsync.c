// SPDX-License-Identifier: GPL-2.0-only
/*
 * ntsync.c - Kernel driver for NT synchronization primitives
 *
 * Copyright (C) 2024 Elizabeth Figura <zfigura@codeweavers.com>
 */

#include <linux/anon_inodes.h>
#include <linux/atomic.h>
#include <linux/file.h>
#include <linux/fs.h>
#include <linux/hrtimer.h>
#include <linux/ktime.h>
#include <linux/miscdevice.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/overflow.h>
#include <linux/sched.h>
#include <linux/sched/signal.h>
#include <linux/slab.h>
#include <linux/spinlock.h>
#include <linux/time_namespace.h>
#include <uapi/linux/ntsync.h>

#define NTSYNC_NAME	"ntsync"
#define NTSYNC_IOCP_MAX_DEPTH	65536

enum ntsync_type {
	NTSYNC_TYPE_SEM,
	NTSYNC_TYPE_MUTEX,
	NTSYNC_TYPE_EVENT,
	NTSYNC_TYPE_IOCP,
};

struct ntsync_iocp_entry {
	struct list_head node;
	struct ntsync_iocp_packet packet;
	__u64 sequence;
};

/*
 * Individual synchronization primitives are represented by
 * struct ntsync_obj, and each primitive is backed by a file.
 *
 * The whole namespace is represented by a struct ntsync_device also
 * backed by a file.
 *
 * Both rely on struct file for reference counting. Individual
 * ntsync_obj objects take a reference to the device when created.
 * Wait operations take a reference to each object being waited on for
 * the duration of the wait.
 */

struct ntsync_obj {
	spinlock_t lock;
	int dev_locked;

	enum ntsync_type type;

	struct file *file;
	struct ntsync_device *dev;

	/* The following fields are protected by the object lock. */
	union {
		struct {
			__u32 count;
			__u32 max;
		} sem;
		struct {
			__u32 count;
			pid_t owner;
			bool ownerdead;
		} mutex;
		struct {
			bool manual;
			bool signaled;
		} event;
		struct {
			struct list_head queue;
			/* Blocking removers are served in LIFO order. */
			struct list_head remove_waiters;
			struct mutex remove_lock;
			__u32 concurrency;
			__u64 generation;
			__u64 sequence;
			__u32 depth;
			atomic_t pending;
			bool shutdown;
		} iocp;
	} u;

	/*
	 * any_waiters is protected by the object lock, but all_waiters is
	 * protected by the device wait_all_lock.
	 */
	struct list_head any_waiters;
	struct list_head all_waiters;

	/*
	 * Hint describing how many tasks are queued on this object in a
	 * wait-all operation.
	 *
	 * Any time we do a wake, we may need to wake "all" waiters as well as
	 * "any" waiters. In order to atomically wake "all" waiters, we must
	 * lock all of the objects, and that means grabbing the wait_all_lock
	 * below (and, due to lock ordering rules, before locking this object).
	 * However, wait-all is a rare operation, and grabbing the wait-all
	 * lock for every wake would create unnecessary contention.
	 * Therefore we first check whether all_hint is zero, and, if it is,
	 * we skip trying to wake "all" waiters.
	 *
	 * Since wait requests must originate from user-space threads, we're
	 * limited here by PID_MAX_LIMIT, so there's no risk of overflow.
	 */
	atomic_t all_hint;
};

struct ntsync_q_entry {
	struct list_head node;
	struct ntsync_q *q;
	struct ntsync_obj *obj;
	__u32 index;
};

struct ntsync_q {
	struct task_struct *task;
	__u32 owner;

	/*
	 * Protected via atomic_try_cmpxchg(). Only the thread that wins the
	 * compare-and-swap may actually change object states and wake this
	 * task.
	 */
	atomic_t signaled;

	bool all;
	bool ownerdead;
	__u32 count;
	struct ntsync_iocp_entry *iocp_entry;
	struct ntsync_q_entry entries[];
};

struct ntsync_device {
	/*
	 * Wait-all operations must atomically grab all objects, and be totally
	 * ordered with respect to each other and wait-any operations.
	 * If one thread is trying to acquire several objects, another thread
	 * cannot touch the object at the same time.
	 *
	 * This device-wide lock is used to serialize wait-for-all
	 * operations, and operations on an object that is involved in a
	 * wait-for-all.
	 */
	struct mutex wait_all_lock;

	struct file *file;
};

/*
 * Single objects are locked using obj->lock.
 *
 * Multiple objects are 'locked' while holding dev->wait_all_lock.
 * In this case however, individual objects are not locked by holding
 * obj->lock, but by setting obj->dev_locked.
 *
 * This means that in order to lock a single object, the sequence is slightly
 * more complicated than usual. Specifically it needs to check obj->dev_locked
 * after acquiring obj->lock, if set, it needs to drop the lock and acquire
 * dev->wait_all_lock in order to serialize against the multi-object operation.
 */

static void dev_lock_obj(struct ntsync_device *dev, struct ntsync_obj *obj)
{
	lockdep_assert_held(&dev->wait_all_lock);
	lockdep_assert(obj->dev == dev);
	spin_lock(&obj->lock);
	/*
	 * By setting obj->dev_locked inside obj->lock, it is ensured that
	 * anyone holding obj->lock must see the value.
	 */
	obj->dev_locked = 1;
	spin_unlock(&obj->lock);
}

static void dev_unlock_obj(struct ntsync_device *dev, struct ntsync_obj *obj)
{
	lockdep_assert_held(&dev->wait_all_lock);
	lockdep_assert(obj->dev == dev);
	spin_lock(&obj->lock);
	obj->dev_locked = 0;
	spin_unlock(&obj->lock);
}

static void obj_lock(struct ntsync_obj *obj)
{
	struct ntsync_device *dev = obj->dev;

	for (;;) {
		spin_lock(&obj->lock);
		if (likely(!obj->dev_locked))
			break;

		spin_unlock(&obj->lock);
		mutex_lock(&dev->wait_all_lock);
		spin_lock(&obj->lock);
		/*
		 * obj->dev_locked should be set and released under the same
		 * wait_all_lock section, since we now own this lock, it should
		 * be clear.
		 */
		lockdep_assert(!obj->dev_locked);
		spin_unlock(&obj->lock);
		mutex_unlock(&dev->wait_all_lock);
	}
}

static void obj_unlock(struct ntsync_obj *obj)
{
	spin_unlock(&obj->lock);
}

static bool ntsync_lock_obj(struct ntsync_device *dev, struct ntsync_obj *obj)
{
	bool all;

	obj_lock(obj);
	all = atomic_read(&obj->all_hint);
	if (unlikely(all)) {
		obj_unlock(obj);
		mutex_lock(&dev->wait_all_lock);
		dev_lock_obj(dev, obj);
	}

	return all;
}

static void ntsync_unlock_obj(struct ntsync_device *dev, struct ntsync_obj *obj, bool all)
{
	if (all) {
		dev_unlock_obj(dev, obj);
		mutex_unlock(&dev->wait_all_lock);
	} else {
		obj_unlock(obj);
	}
}

#define ntsync_assert_held(obj) \
	lockdep_assert((lockdep_is_held(&(obj)->lock) != LOCK_STATE_NOT_HELD) || \
		       ((lockdep_is_held(&(obj)->dev->wait_all_lock) != LOCK_STATE_NOT_HELD) && \
			(obj)->dev_locked))

static bool is_signaled(struct ntsync_obj *obj, __u32 owner)
{
	ntsync_assert_held(obj);

	switch (obj->type) {
	case NTSYNC_TYPE_SEM:
		return !!obj->u.sem.count;
	case NTSYNC_TYPE_MUTEX:
		if (obj->u.mutex.owner && obj->u.mutex.owner != owner)
			return false;
		return obj->u.mutex.count < UINT_MAX;
	case NTSYNC_TYPE_EVENT:
		return obj->u.event.signaled;
	case NTSYNC_TYPE_IOCP:
		return obj->u.iocp.depth || obj->u.iocp.shutdown;
	}

	WARN(1, "bad object type %#x\n", obj->type);
	return false;
}

/*
 * "locked_obj" is an optional pointer to an object which is already locked and
 * should not be locked again. This is necessary so that changing an object's
 * state and waking it can be a single atomic operation.
 */
static void try_wake_all(struct ntsync_device *dev, struct ntsync_q *q,
			 struct ntsync_obj *locked_obj)
{
	__u32 count = q->count;
	bool can_wake = true;
	int signaled = -1;
	__u32 i;

	lockdep_assert_held(&dev->wait_all_lock);
	if (locked_obj)
		lockdep_assert(locked_obj->dev_locked);

	for (i = 0; i < count; i++) {
		if (q->entries[i].obj != locked_obj)
			dev_lock_obj(dev, q->entries[i].obj);
	}

	for (i = 0; i < count; i++) {
		if (!is_signaled(q->entries[i].obj, q->owner)) {
			can_wake = false;
			break;
		}
	}

	if (can_wake && atomic_try_cmpxchg(&q->signaled, &signaled, 0)) {
		for (i = 0; i < count; i++) {
			struct ntsync_obj *obj = q->entries[i].obj;

			switch (obj->type) {
			case NTSYNC_TYPE_SEM:
				obj->u.sem.count--;
				break;
			case NTSYNC_TYPE_MUTEX:
				if (obj->u.mutex.ownerdead)
					q->ownerdead = true;
				obj->u.mutex.ownerdead = false;
				obj->u.mutex.count++;
				obj->u.mutex.owner = q->owner;
				break;
			case NTSYNC_TYPE_EVENT:
				if (!obj->u.event.manual)
					obj->u.event.signaled = false;
				break;
			case NTSYNC_TYPE_IOCP:
				break;
			}
		}
		wake_up_process(q->task);
	}

	for (i = 0; i < count; i++) {
		if (q->entries[i].obj != locked_obj)
			dev_unlock_obj(dev, q->entries[i].obj);
	}
}

static void try_wake_all_obj(struct ntsync_device *dev, struct ntsync_obj *obj)
{
	struct ntsync_q_entry *entry;

	lockdep_assert_held(&dev->wait_all_lock);
	lockdep_assert(obj->dev_locked);

	list_for_each_entry(entry, &obj->all_waiters, node)
		try_wake_all(dev, entry->q, obj);
}

static void try_wake_any_sem(struct ntsync_obj *sem)
{
	struct ntsync_q_entry *entry;

	ntsync_assert_held(sem);
	lockdep_assert(sem->type == NTSYNC_TYPE_SEM);

	list_for_each_entry(entry, &sem->any_waiters, node) {
		struct ntsync_q *q = entry->q;
		int signaled = -1;

		if (!sem->u.sem.count)
			break;

		if (atomic_try_cmpxchg(&q->signaled, &signaled, entry->index)) {
			sem->u.sem.count--;
			wake_up_process(q->task);
		}
	}
}

static void try_wake_any_mutex(struct ntsync_obj *mutex)
{
	struct ntsync_q_entry *entry;

	ntsync_assert_held(mutex);
	lockdep_assert(mutex->type == NTSYNC_TYPE_MUTEX);

	list_for_each_entry(entry, &mutex->any_waiters, node) {
		struct ntsync_q *q = entry->q;
		int signaled = -1;

		if (mutex->u.mutex.count == UINT_MAX)
			break;
		if (mutex->u.mutex.owner && mutex->u.mutex.owner != q->owner)
			continue;

		if (atomic_try_cmpxchg(&q->signaled, &signaled, entry->index)) {
			if (mutex->u.mutex.ownerdead)
				q->ownerdead = true;
			mutex->u.mutex.ownerdead = false;
			mutex->u.mutex.count++;
			mutex->u.mutex.owner = q->owner;
			wake_up_process(q->task);
		}
	}
}

static void try_wake_any_event(struct ntsync_obj *event)
{
	struct ntsync_q_entry *entry;

	ntsync_assert_held(event);
	lockdep_assert(event->type == NTSYNC_TYPE_EVENT);

	list_for_each_entry(entry, &event->any_waiters, node) {
		struct ntsync_q *q = entry->q;
		int signaled = -1;

		if (!event->u.event.signaled)
			break;

		if (atomic_try_cmpxchg(&q->signaled, &signaled, entry->index)) {
			if (!event->u.event.manual)
				event->u.event.signaled = false;
			wake_up_process(q->task);
		}
	}
}

static void try_wake_any_iocp(struct ntsync_obj *iocp)
{
	struct ntsync_q_entry *entry;

	ntsync_assert_held(iocp);
	lockdep_assert(iocp->type == NTSYNC_TYPE_IOCP);

	list_for_each_entry(entry, &iocp->any_waiters, node) {
		struct ntsync_q *q = entry->q;
		int signaled = -1;

		if (!iocp->u.iocp.depth && !iocp->u.iocp.shutdown)
			break;

		if (atomic_try_cmpxchg(&q->signaled, &signaled, entry->index))
			wake_up_process(q->task);
	}
}

static void dispatch_iocp_packets(struct ntsync_device *dev,
				  struct ntsync_obj *iocp, bool all)
{
	struct ntsync_q_entry *entry, *next;

	ntsync_assert_held(iocp);
	lockdep_assert(iocp->type == NTSYNC_TYPE_IOCP);

	if (iocp->u.iocp.shutdown) {
		list_for_each_entry_safe(entry, next,
					 &iocp->u.iocp.remove_waiters, node) {
			struct ntsync_q *q = entry->q;
			int signaled = -1;

			list_del_init(&entry->node);
			q->iocp_entry = NULL;
			if (atomic_try_cmpxchg(&q->signaled, &signaled, 0))
				wake_up_process(q->task);
		}
	} else {
		while (iocp->u.iocp.depth &&
		       !list_empty(&iocp->u.iocp.remove_waiters)) {
			struct ntsync_iocp_entry *packet;
			bool assigned = false;

			packet = list_first_entry(&iocp->u.iocp.queue,
						  struct ntsync_iocp_entry, node);
			list_for_each_entry_safe(entry, next,
						 &iocp->u.iocp.remove_waiters, node) {
				struct ntsync_q *q = entry->q;
				int signaled = -1;

				/*
				 * Keep failed claims off the list; another wake source
				 * already owns that waiter.
				 */
				list_del_init(&entry->node);
				q->iocp_entry = packet;
				if (atomic_try_cmpxchg(&q->signaled, &signaled, 0)) {
					list_del_init(&packet->node);
					iocp->u.iocp.depth--;
					wake_up_process(q->task);
					assigned = true;
					break;
				}
				q->iocp_entry = NULL;
			}
			if (!assigned)
				break;
		}
	}

	if (!is_signaled(iocp, 0))
		return;

	if (all)
		try_wake_all_obj(dev, iocp);
	try_wake_any_iocp(iocp);
}

static void requeue_iocp_packet(struct ntsync_obj *iocp,
				struct ntsync_iocp_entry *entry)
{
	struct list_head *pos;

	ntsync_assert_held(iocp);

	/* Concurrent reservations can be returned in any order. */
	list_for_each(pos, &iocp->u.iocp.queue) {
		struct ntsync_iocp_entry *queued;

		queued = list_entry(pos, struct ntsync_iocp_entry, node);
		if ((__s64)(entry->sequence - queued->sequence) < 0)
			break;
	}
	list_move_tail(&entry->node, pos);
	iocp->u.iocp.depth++;
}

static void return_iocp_packet(struct ntsync_obj *iocp,
			       struct ntsync_iocp_entry *entry)
{
	struct ntsync_device *dev = iocp->dev;
	bool discard, all;

	if (!entry)
		return;

	all = ntsync_lock_obj(dev, iocp);
	discard = iocp->u.iocp.shutdown;
	if (!discard) {
		requeue_iocp_packet(iocp, entry);
		dispatch_iocp_packets(dev, iocp, all);
	} else {
		atomic_dec(&iocp->u.iocp.pending);
	}
	ntsync_unlock_obj(dev, iocp, all);

	if (discard)
		kfree(entry);
}

/*
 * Actually change the semaphore state, returning -EOVERFLOW if it is made
 * invalid.
 */
static int release_sem_state(struct ntsync_obj *sem, __u32 count)
{
	__u32 sum;

	ntsync_assert_held(sem);

	if (check_add_overflow(sem->u.sem.count, count, &sum) ||
	    sum > sem->u.sem.max)
		return -EOVERFLOW;

	sem->u.sem.count = sum;
	return 0;
}

static int ntsync_sem_release(struct ntsync_obj *sem, void __user *argp)
{
	struct ntsync_device *dev = sem->dev;
	__u32 __user *user_args = argp;
	__u32 prev_count;
	__u32 args;
	bool all;
	int ret;

	if (copy_from_user(&args, argp, sizeof(args)))
		return -EFAULT;

	if (sem->type != NTSYNC_TYPE_SEM)
		return -EINVAL;

	all = ntsync_lock_obj(dev, sem);

	prev_count = sem->u.sem.count;
	ret = release_sem_state(sem, args);
	if (!ret) {
		if (all)
			try_wake_all_obj(dev, sem);
		try_wake_any_sem(sem);
	}

	ntsync_unlock_obj(dev, sem, all);

	if (!ret && put_user(prev_count, user_args))
		ret = -EFAULT;

	return ret;
}

/*
 * Actually change the mutex state, returning -EPERM if not the owner.
 */
static int unlock_mutex_state(struct ntsync_obj *mutex,
			      const struct ntsync_mutex_args *args)
{
	ntsync_assert_held(mutex);

	if (mutex->u.mutex.owner != args->owner)
		return -EPERM;

	if (!--mutex->u.mutex.count)
		mutex->u.mutex.owner = 0;
	return 0;
}

static int ntsync_mutex_unlock(struct ntsync_obj *mutex, void __user *argp)
{
	struct ntsync_mutex_args __user *user_args = argp;
	struct ntsync_device *dev = mutex->dev;
	struct ntsync_mutex_args args;
	__u32 prev_count;
	bool all;
	int ret;

	if (copy_from_user(&args, argp, sizeof(args)))
		return -EFAULT;
	if (!args.owner)
		return -EINVAL;

	if (mutex->type != NTSYNC_TYPE_MUTEX)
		return -EINVAL;

	all = ntsync_lock_obj(dev, mutex);

	prev_count = mutex->u.mutex.count;
	ret = unlock_mutex_state(mutex, &args);
	if (!ret) {
		if (all)
			try_wake_all_obj(dev, mutex);
		try_wake_any_mutex(mutex);
	}

	ntsync_unlock_obj(dev, mutex, all);

	if (!ret && put_user(prev_count, &user_args->count))
		ret = -EFAULT;

	return ret;
}

/*
 * Actually change the mutex state to mark its owner as dead,
 * returning -EPERM if not the owner.
 */
static int kill_mutex_state(struct ntsync_obj *mutex, __u32 owner)
{
	ntsync_assert_held(mutex);

	if (mutex->u.mutex.owner != owner)
		return -EPERM;

	mutex->u.mutex.ownerdead = true;
	mutex->u.mutex.owner = 0;
	mutex->u.mutex.count = 0;
	return 0;
}

static int ntsync_mutex_kill(struct ntsync_obj *mutex, void __user *argp)
{
	struct ntsync_device *dev = mutex->dev;
	__u32 owner;
	bool all;
	int ret;

	if (get_user(owner, (__u32 __user *)argp))
		return -EFAULT;
	if (!owner)
		return -EINVAL;

	if (mutex->type != NTSYNC_TYPE_MUTEX)
		return -EINVAL;

	all = ntsync_lock_obj(dev, mutex);

	ret = kill_mutex_state(mutex, owner);
	if (!ret) {
		if (all)
			try_wake_all_obj(dev, mutex);
		try_wake_any_mutex(mutex);
	}

	ntsync_unlock_obj(dev, mutex, all);

	return ret;
}

static int ntsync_event_set(struct ntsync_obj *event, void __user *argp, bool pulse)
{
	struct ntsync_device *dev = event->dev;
	__u32 prev_state;
	bool all;

	if (event->type != NTSYNC_TYPE_EVENT)
		return -EINVAL;

	all = ntsync_lock_obj(dev, event);

	prev_state = event->u.event.signaled;
	event->u.event.signaled = true;
	if (all)
		try_wake_all_obj(dev, event);
	try_wake_any_event(event);
	if (pulse)
		event->u.event.signaled = false;

	ntsync_unlock_obj(dev, event, all);

	if (put_user(prev_state, (__u32 __user *)argp))
		return -EFAULT;

	return 0;
}

static int ntsync_event_reset(struct ntsync_obj *event, void __user *argp)
{
	struct ntsync_device *dev = event->dev;
	__u32 prev_state;
	bool all;

	if (event->type != NTSYNC_TYPE_EVENT)
		return -EINVAL;

	all = ntsync_lock_obj(dev, event);

	prev_state = event->u.event.signaled;
	event->u.event.signaled = false;

	ntsync_unlock_obj(dev, event, all);

	if (put_user(prev_state, (__u32 __user *)argp))
		return -EFAULT;

	return 0;
}

static int ntsync_sem_read(struct ntsync_obj *sem, void __user *argp)
{
	struct ntsync_sem_args __user *user_args = argp;
	struct ntsync_device *dev = sem->dev;
	struct ntsync_sem_args args;
	bool all;

	if (sem->type != NTSYNC_TYPE_SEM)
		return -EINVAL;

	all = ntsync_lock_obj(dev, sem);

	args.count = sem->u.sem.count;
	args.max = sem->u.sem.max;

	ntsync_unlock_obj(dev, sem, all);

	if (copy_to_user(user_args, &args, sizeof(args)))
		return -EFAULT;
	return 0;
}

static int ntsync_mutex_read(struct ntsync_obj *mutex, void __user *argp)
{
	struct ntsync_mutex_args __user *user_args = argp;
	struct ntsync_device *dev = mutex->dev;
	struct ntsync_mutex_args args;
	bool all;
	int ret;

	if (mutex->type != NTSYNC_TYPE_MUTEX)
		return -EINVAL;

	all = ntsync_lock_obj(dev, mutex);

	args.count = mutex->u.mutex.count;
	args.owner = mutex->u.mutex.owner;
	ret = mutex->u.mutex.ownerdead ? -EOWNERDEAD : 0;

	ntsync_unlock_obj(dev, mutex, all);

	if (copy_to_user(user_args, &args, sizeof(args)))
		return -EFAULT;
	return ret;
}

static int ntsync_event_read(struct ntsync_obj *event, void __user *argp)
{
	struct ntsync_event_args __user *user_args = argp;
	struct ntsync_device *dev = event->dev;
	struct ntsync_event_args args;
	bool all;

	if (event->type != NTSYNC_TYPE_EVENT)
		return -EINVAL;

	all = ntsync_lock_obj(dev, event);

	args.manual = event->u.event.manual;
	args.signaled = event->u.event.signaled;

	ntsync_unlock_obj(dev, event, all);

	if (copy_to_user(user_args, &args, sizeof(args)))
		return -EFAULT;
	return 0;
}

static int ntsync_iocp_post(struct ntsync_obj *iocp, void __user *argp,
			    bool internal)
{
	struct ntsync_iocp_entry *entry;
	struct ntsync_device *dev = iocp->dev;
	bool all;
	int ret = 0;

	if (iocp->type != NTSYNC_TYPE_IOCP)
		return -EINVAL;

	entry = kmalloc_obj(*entry, GFP_KERNEL_ACCOUNT);
	if (!entry)
		return -ENOMEM;
	if (copy_from_user(&entry->packet, argp, sizeof(entry->packet))) {
		kfree(entry);
		return -EFAULT;
	}
	if (entry->packet.pad) {
		kfree(entry);
		return -EINVAL;
	}

	all = ntsync_lock_obj(dev, iocp);
	if (iocp->u.iocp.shutdown) {
		ret = -ESHUTDOWN;
	} else if (!internal && atomic_read(&iocp->u.iocp.pending) >=
		   NTSYNC_IOCP_MAX_DEPTH) {
		ret = -ENOSPC;
	} else {
		entry->sequence = iocp->u.iocp.sequence++;
		list_add_tail(&entry->node, &iocp->u.iocp.queue);
		iocp->u.iocp.depth++;
		atomic_inc(&iocp->u.iocp.pending);
		dispatch_iocp_packets(dev, iocp, all);
	}
	ntsync_unlock_obj(dev, iocp, all);

	if (ret)
		kfree(entry);
	return ret;
}

static int ntsync_iocp_remove_packets(struct ntsync_obj *iocp,
				      struct ntsync_iocp_packet __user *user_packets,
				      __u32 count, __u32 __user *user_written,
				      struct ntsync_iocp_entry **assigned,
				      __u64 generation)
{
	struct ntsync_iocp_packet stack_packet;
	struct ntsync_iocp_packet *packets = &stack_packet;
	struct ntsync_iocp_entry *assigned_entry = assigned ? *assigned : NULL;
	struct ntsync_iocp_entry *entry, *next;
	struct ntsync_device *dev = iocp->dev;
	LIST_HEAD(reserved);
	__u32 i, written = 0;
	bool all;
	int ret = 0;

	if (iocp->type != NTSYNC_TYPE_IOCP)
		return -EINVAL;
	if (!count || count > NTSYNC_IOCP_MAX_REMOVE)
		return -EINVAL;

	if (count > 1) {
		packets = kcalloc(count, sizeof(*packets), GFP_KERNEL_ACCOUNT);
		if (!packets) {
			return_iocp_packet(iocp, assigned_entry);
			if (assigned)
				*assigned = NULL;
			return -ENOMEM;
		}
	}

	ret = mutex_lock_killable(&iocp->u.iocp.remove_lock);
	if (ret) {
		return_iocp_packet(iocp, assigned_entry);
		if (assigned)
			*assigned = NULL;
		goto out;
	}

	all = ntsync_lock_obj(dev, iocp);
	if (generation && generation != iocp->u.iocp.generation) {
		ret = -ECANCELED;
	} else if (assigned_entry) {
		packets[written++] = assigned_entry->packet;
		list_add_tail(&assigned_entry->node, &reserved);
		if (assigned)
			*assigned = NULL;
	}
	if (!ret && !written && iocp->u.iocp.shutdown) {
		ret = -ECANCELED;
	} else if (!ret && !written && !iocp->u.iocp.depth) {
		ret = -EAGAIN;
	} else if (!ret && !iocp->u.iocp.shutdown && written < count) {
		__u32 queued = min(count - written, iocp->u.iocp.depth);

		i = written;
		list_for_each_entry_safe(entry, next, &iocp->u.iocp.queue, node) {
			packets[i++] = entry->packet;
			list_move_tail(&entry->node, &reserved);
			if (i == written + queued)
				break;
		}
		iocp->u.iocp.depth -= queued;
		written += queued;
	}
	ntsync_unlock_obj(dev, iocp, all);

	if (!ret && copy_to_user(user_packets, packets,
				 array_size(written, sizeof(*packets))))
		ret = -EFAULT;
	if (!ret && user_written && put_user(written, user_written))
		ret = -EFAULT;

	if (ret && written) {
		all = ntsync_lock_obj(dev, iocp);
		if (!iocp->u.iocp.shutdown) {
			list_for_each_entry_safe(entry, next, &reserved, node)
				requeue_iocp_packet(iocp, entry);
			dispatch_iocp_packets(dev, iocp, all);
		} else {
			atomic_sub(written, &iocp->u.iocp.pending);
		}
		ntsync_unlock_obj(dev, iocp, all);
	} else if (written)
		atomic_sub(written, &iocp->u.iocp.pending);

	mutex_unlock(&iocp->u.iocp.remove_lock);

out:
	list_for_each_entry_safe(entry, next, &reserved, node)
		kfree(entry);
	if (packets != &stack_packet)
		kfree(packets);
	return ret;
}

static int ntsync_iocp_remove(struct ntsync_obj *iocp, void __user *argp)
{
	return ntsync_iocp_remove_packets(iocp, argp, 1, NULL, NULL, 0);
}

static int ntsync_iocp_read(struct ntsync_obj *iocp, void __user *argp)
{
	struct ntsync_iocp_info info;
	bool all;

	if (iocp->type != NTSYNC_TYPE_IOCP)
		return -EINVAL;

	all = ntsync_lock_obj(iocp->dev, iocp);
	info.concurrency = iocp->u.iocp.concurrency;
	info.depth = iocp->u.iocp.depth;
	ntsync_unlock_obj(iocp->dev, iocp, all);

	if (copy_to_user(argp, &info, sizeof(info)))
		return -EFAULT;
	return 0;
}

static int ntsync_iocp_shutdown(struct ntsync_obj *iocp)
{
	struct ntsync_iocp_entry *entry, *next;
	struct ntsync_device *dev = iocp->dev;
	LIST_HEAD(removed);
	bool all;

	if (iocp->type != NTSYNC_TYPE_IOCP)
		return -EINVAL;

	all = ntsync_lock_obj(dev, iocp);
	iocp->u.iocp.shutdown = true;
	list_splice_init(&iocp->u.iocp.queue, &removed);
	atomic_sub(iocp->u.iocp.depth, &iocp->u.iocp.pending);
	iocp->u.iocp.depth = 0;
	dispatch_iocp_packets(dev, iocp, all);
	ntsync_unlock_obj(dev, iocp, all);

	list_for_each_entry_safe(entry, next, &removed, node)
		kfree(entry);
	return 0;
}

static int ntsync_iocp_abandon(struct ntsync_obj *iocp, void __user *argp)
{
	struct ntsync_iocp_abandon_args args;
	struct ntsync_q_entry *entry, *next;
	struct ntsync_device *dev = iocp->dev;
	bool all;
	int ret = 0;

	if (iocp->type != NTSYNC_TYPE_IOCP)
		return -EINVAL;
	if (copy_from_user(&args, argp, sizeof(args)))
		return -EFAULT;
	if (!args.generation)
		return -EINVAL;

	all = ntsync_lock_obj(dev, iocp);
	if (iocp->u.iocp.shutdown ||
	    args.generation != iocp->u.iocp.generation) {
		ret = -ECANCELED;
	} else {
		if (!++iocp->u.iocp.generation)
			++iocp->u.iocp.generation;
		list_for_each_entry_safe(entry, next,
					 &iocp->u.iocp.remove_waiters, node) {
			struct ntsync_q *q = entry->q;
			int signaled = -1;

			list_del_init(&entry->node);
			q->iocp_entry = NULL;
			if (atomic_try_cmpxchg(&q->signaled, &signaled, 0))
				wake_up_process(q->task);
		}
	}
	ntsync_unlock_obj(dev, iocp, all);

	return ret;
}

static void ntsync_free_obj(struct ntsync_obj *obj)
{
	if (obj->type == NTSYNC_TYPE_IOCP) {
		struct ntsync_iocp_entry *entry, *next;

		list_for_each_entry_safe(entry, next, &obj->u.iocp.queue, node) {
			list_del(&entry->node);
			kfree(entry);
		}
	}
	fput(obj->dev->file);
	kfree(obj);
}

static int ntsync_obj_release(struct inode *inode, struct file *file)
{
	ntsync_free_obj(file->private_data);
	return 0;
}

static int ntsync_iocp_remove_batch(struct ntsync_obj *iocp,
				     void __user *argp);

static long ntsync_obj_ioctl(struct file *file, unsigned int cmd,
			     unsigned long parm)
{
	struct ntsync_obj *obj = file->private_data;
	void __user *argp = (void __user *)parm;

	switch (cmd) {
	case NTSYNC_IOC_SEM_RELEASE:
		return ntsync_sem_release(obj, argp);
	case NTSYNC_IOC_SEM_READ:
		return ntsync_sem_read(obj, argp);
	case NTSYNC_IOC_MUTEX_UNLOCK:
		return ntsync_mutex_unlock(obj, argp);
	case NTSYNC_IOC_MUTEX_KILL:
		return ntsync_mutex_kill(obj, argp);
	case NTSYNC_IOC_MUTEX_READ:
		return ntsync_mutex_read(obj, argp);
	case NTSYNC_IOC_EVENT_SET:
		return ntsync_event_set(obj, argp, false);
	case NTSYNC_IOC_EVENT_RESET:
		return ntsync_event_reset(obj, argp);
	case NTSYNC_IOC_EVENT_PULSE:
		return ntsync_event_set(obj, argp, true);
	case NTSYNC_IOC_EVENT_READ:
		return ntsync_event_read(obj, argp);
	case NTSYNC_IOC_IOCP_POST:
		return ntsync_iocp_post(obj, argp, false);
	case NTSYNC_IOC_IOCP_POST_INTERNAL:
		return ntsync_iocp_post(obj, argp, true);
	case NTSYNC_IOC_IOCP_REMOVE:
		return ntsync_iocp_remove(obj, argp);
	case NTSYNC_IOC_IOCP_READ:
		return ntsync_iocp_read(obj, argp);
	case NTSYNC_IOC_IOCP_REMOVE_BATCH:
		return ntsync_iocp_remove_batch(obj, argp);
	case NTSYNC_IOC_IOCP_SHUTDOWN:
		return ntsync_iocp_shutdown(obj);
	case NTSYNC_IOC_IOCP_ABANDON:
		return ntsync_iocp_abandon(obj, argp);
	default:
		return -ENOIOCTLCMD;
	}
}

static const struct file_operations ntsync_obj_fops = {
	.owner		= THIS_MODULE,
	.release	= ntsync_obj_release,
	.unlocked_ioctl	= ntsync_obj_ioctl,
	.compat_ioctl	= compat_ptr_ioctl,
};

static struct ntsync_obj *ntsync_alloc_obj(struct ntsync_device *dev,
					   enum ntsync_type type)
{
	struct ntsync_obj *obj;

	obj = kzalloc_obj(*obj);
	if (!obj)
		return NULL;
	obj->type = type;
	obj->dev = dev;
	get_file(dev->file);
	spin_lock_init(&obj->lock);
	INIT_LIST_HEAD(&obj->any_waiters);
	INIT_LIST_HEAD(&obj->all_waiters);
	atomic_set(&obj->all_hint, 0);

	return obj;
}

static int ntsync_obj_get_fd(struct ntsync_obj *obj)
{
	FD_PREPARE(fdf, O_CLOEXEC,
		   anon_inode_getfile("ntsync", &ntsync_obj_fops, obj, O_RDWR));
	if (fdf.err)
		return fdf.err;
	obj->file = fd_prepare_file(fdf);
	return fd_publish(fdf);
}

static int ntsync_create_sem(struct ntsync_device *dev, void __user *argp)
{
	struct ntsync_sem_args args;
	struct ntsync_obj *sem;
	int fd;

	if (copy_from_user(&args, argp, sizeof(args)))
		return -EFAULT;

	if (args.count > args.max)
		return -EINVAL;

	sem = ntsync_alloc_obj(dev, NTSYNC_TYPE_SEM);
	if (!sem)
		return -ENOMEM;
	sem->u.sem.count = args.count;
	sem->u.sem.max = args.max;
	fd = ntsync_obj_get_fd(sem);
	if (fd < 0)
		ntsync_free_obj(sem);

	return fd;
}

static int ntsync_create_mutex(struct ntsync_device *dev, void __user *argp)
{
	struct ntsync_mutex_args args;
	struct ntsync_obj *mutex;
	int fd;

	if (copy_from_user(&args, argp, sizeof(args)))
		return -EFAULT;

	if (!args.owner != !args.count)
		return -EINVAL;

	mutex = ntsync_alloc_obj(dev, NTSYNC_TYPE_MUTEX);
	if (!mutex)
		return -ENOMEM;
	mutex->u.mutex.count = args.count;
	mutex->u.mutex.owner = args.owner;
	fd = ntsync_obj_get_fd(mutex);
	if (fd < 0)
		ntsync_free_obj(mutex);

	return fd;
}

static int ntsync_create_event(struct ntsync_device *dev, void __user *argp)
{
	struct ntsync_event_args args;
	struct ntsync_obj *event;
	int fd;

	if (copy_from_user(&args, argp, sizeof(args)))
		return -EFAULT;

	event = ntsync_alloc_obj(dev, NTSYNC_TYPE_EVENT);
	if (!event)
		return -ENOMEM;
	event->u.event.manual = args.manual;
	event->u.event.signaled = args.signaled;
	fd = ntsync_obj_get_fd(event);
	if (fd < 0)
		ntsync_free_obj(event);

	return fd;
}

static int ntsync_create_iocp(struct ntsync_device *dev, void __user *argp)
{
	struct ntsync_iocp_args args;
	struct ntsync_obj *iocp;
	int fd;

	if (copy_from_user(&args, argp, sizeof(args)))
		return -EFAULT;
	if (args.version != NTSYNC_IOCP_VERSION)
		return -EINVAL;

	iocp = ntsync_alloc_obj(dev, NTSYNC_TYPE_IOCP);
	if (!iocp)
		return -ENOMEM;
	INIT_LIST_HEAD(&iocp->u.iocp.queue);
	INIT_LIST_HEAD(&iocp->u.iocp.remove_waiters);
	mutex_init(&iocp->u.iocp.remove_lock);
	iocp->u.iocp.concurrency = args.concurrency;
	iocp->u.iocp.generation = NTSYNC_IOCP_INITIAL_GENERATION;
	atomic_set(&iocp->u.iocp.pending, 0);
	iocp->u.iocp.shutdown = false;
	fd = ntsync_obj_get_fd(iocp);
	if (fd < 0)
		ntsync_free_obj(iocp);

	return fd;
}

static struct ntsync_obj *get_obj(struct ntsync_device *dev, int fd)
{
	struct file *file = fget(fd);
	struct ntsync_obj *obj;

	if (!file)
		return NULL;

	if (file->f_op != &ntsync_obj_fops) {
		fput(file);
		return NULL;
	}

	obj = file->private_data;
	if (obj->dev != dev) {
		fput(file);
		return NULL;
	}

	return obj;
}

static void put_obj(struct ntsync_obj *obj)
{
	fput(obj->file);
}

static int ntsync_schedule(const struct ntsync_q *q, const struct ntsync_wait_args *args)
{
	ktime_t timeout = ns_to_ktime(args->timeout);
	clockid_t clock = CLOCK_MONOTONIC;
	ktime_t *timeout_ptr;
	int ret = 0;

	timeout_ptr = (args->timeout == U64_MAX ? NULL : &timeout);

	if (args->flags & NTSYNC_WAIT_REALTIME)
		clock = CLOCK_REALTIME;
	else
		timeout = timens_ktime_to_host(clock, timeout);

	do {
		if (signal_pending(current)) {
			ret = -ERESTARTSYS;
			break;
		}

		set_current_state(TASK_INTERRUPTIBLE);
		if (atomic_read(&q->signaled) != -1) {
			ret = 0;
			break;
		}
		ret = schedule_hrtimeout_range_clock(timeout_ptr, 0, HRTIMER_MODE_ABS, clock);
	} while (ret < 0);
	__set_current_state(TASK_RUNNING);

	return ret;
}

static int ntsync_iocp_wait(struct ntsync_obj *iocp,
			    struct ntsync_obj *alert,
			    const struct ntsync_iocp_remove_args *args,
			    struct ntsync_iocp_entry **packet)
{
	struct ntsync_wait_args wait_args = {
		.timeout = args->timeout,
		.flags = args->flags,
	};
	struct ntsync_device *dev = iocp->dev;
	struct ntsync_q *q;
	__u32 count = alert ? 2 : 1;
	int signaled, ret;
	bool all;

	q = kmalloc_flex(*q, entries, count);
	if (!q)
		return -ENOMEM;

	q->task = current;
	q->owner = 0;
	atomic_set(&q->signaled, -1);
	q->all = false;
	q->ownerdead = false;
	q->count = count;
	q->iocp_entry = NULL;
	q->entries[0].q = q;
	q->entries[0].obj = iocp;
	q->entries[0].index = 0;
	if (alert) {
		q->entries[1].q = q;
		q->entries[1].obj = alert;
		q->entries[1].index = 1;
	}

	INIT_LIST_HEAD(&q->entries[0].node);
	all = ntsync_lock_obj(dev, iocp);
	if (iocp->u.iocp.shutdown ||
	    args->generation != iocp->u.iocp.generation) {
		ntsync_unlock_obj(dev, iocp, all);
		kfree(q);
		return -ECANCELED;
	}
	/* Wine completion ports wake the most recently associated remover. */
	list_add(&q->entries[0].node, &iocp->u.iocp.remove_waiters);
	dispatch_iocp_packets(dev, iocp, all);
	ntsync_unlock_obj(dev, iocp, all);

	if (alert) {
		INIT_LIST_HEAD(&q->entries[1].node);
		all = ntsync_lock_obj(dev, alert);
		list_add_tail(&q->entries[1].node, &alert->any_waiters);
		if (atomic_read(&q->signaled) == -1)
			try_wake_any_event(alert);
		ntsync_unlock_obj(dev, alert, all);
	}

	ret = ntsync_schedule(q, &wait_args);

	all = ntsync_lock_obj(dev, iocp);
	if (!list_empty(&q->entries[0].node))
		list_del_init(&q->entries[0].node);
	*packet = q->iocp_entry;
	ntsync_unlock_obj(dev, iocp, all);

	if (alert) {
		all = ntsync_lock_obj(dev, alert);
		if (!list_empty(&q->entries[1].node))
			list_del_init(&q->entries[1].node);
		ntsync_unlock_obj(dev, alert, all);
	}

	/* An alert can still win until both waiters have been unqueued. */
	signaled = atomic_read(&q->signaled);
	if (signaled != -1)
		ret = signaled;
	else if (!ret)
		ret = -ETIMEDOUT;

	kfree(q);
	return ret;
}

static int ntsync_iocp_remove_batch(struct ntsync_obj *iocp,
				     void __user *argp)
{
	struct ntsync_iocp_remove_args __user *user_args = argp;
	struct ntsync_iocp_remove_args args;
	struct ntsync_iocp_entry *assigned = NULL;
	struct ntsync_obj *alert = NULL;
	int ret;

	if (iocp->type != NTSYNC_TYPE_IOCP)
		return -EINVAL;
	if (copy_from_user(&args, argp, sizeof(args)))
		return -EFAULT;
	if (!args.count || args.count > NTSYNC_IOCP_MAX_REMOVE ||
	    (args.flags & ~NTSYNC_WAIT_REALTIME))
		return -EINVAL;
	if (!args.generation) {
		bool all = ntsync_lock_obj(iocp->dev, iocp);

		args.generation = iocp->u.iocp.generation;
		ntsync_unlock_obj(iocp->dev, iocp, all);
	}

	if (args.alert) {
		alert = get_obj(iocp->dev, args.alert);
		if (!alert || alert->type != NTSYNC_TYPE_EVENT) {
			if (alert)
				put_obj(alert);
			return -EINVAL;
		}
	}

	if (put_user(0, &user_args->written)) {
		ret = -EFAULT;
		goto out;
	}

	for (;;) {
		ret = ntsync_iocp_remove_packets(iocp,
				u64_to_user_ptr(args.packets), args.count,
				&user_args->written, &assigned,
				args.generation);
		if (ret != -EAGAIN)
			break;

		ret = ntsync_iocp_wait(iocp, alert, &args, &assigned);
		if (ret < 0)
			break;
		if (ret == 1) {
			ret = 0;
			break;
		}
	}

out:
	return_iocp_packet(iocp, assigned);
	if (alert)
		put_obj(alert);
	return ret;
}

/*
 * Allocate and initialize the ntsync_q structure, but do not queue us yet.
 */
static int setup_wait(struct ntsync_device *dev,
		      const struct ntsync_wait_args *args, bool all,
		      struct ntsync_q **ret_q)
{
	int fds[NTSYNC_MAX_WAIT_COUNT + 1];
	const __u32 count = args->count;
	size_t size = array_size(count, sizeof(fds[0]));
	struct ntsync_q *q;
	__u32 total_count;
	__u32 i, j;

	if (args->pad || (args->flags & ~NTSYNC_WAIT_REALTIME))
		return -EINVAL;

	if (!args->owner)
		return -EINVAL;

	if (size >= sizeof(fds))
		return -EINVAL;

	total_count = count;
	if (args->alert)
		total_count++;

	if (copy_from_user(fds, u64_to_user_ptr(args->objs), size))
		return -EFAULT;
	if (args->alert)
		fds[count] = args->alert;

	q = kmalloc_flex(*q, entries, total_count);
	if (!q)
		return -ENOMEM;
	q->task = current;
	q->owner = args->owner;
	atomic_set(&q->signaled, -1);
	q->all = all;
	q->ownerdead = false;
	q->count = count;
	q->iocp_entry = NULL;

	for (i = 0; i < total_count; i++) {
		struct ntsync_q_entry *entry = &q->entries[i];
		struct ntsync_obj *obj = get_obj(dev, fds[i]);

		if (!obj)
			goto err;

		if (all) {
			/* Check that the objects are all distinct. */
			for (j = 0; j < i; j++) {
				if (obj == q->entries[j].obj) {
					put_obj(obj);
					goto err;
				}
			}
		}

		entry->obj = obj;
		entry->q = q;
		entry->index = i;
	}

	*ret_q = q;
	return 0;

err:
	for (j = 0; j < i; j++)
		put_obj(q->entries[j].obj);
	kfree(q);
	return -EINVAL;
}

static void try_wake_any_obj(struct ntsync_obj *obj)
{
	switch (obj->type) {
	case NTSYNC_TYPE_SEM:
		try_wake_any_sem(obj);
		break;
	case NTSYNC_TYPE_MUTEX:
		try_wake_any_mutex(obj);
		break;
	case NTSYNC_TYPE_EVENT:
		try_wake_any_event(obj);
		break;
	case NTSYNC_TYPE_IOCP:
		try_wake_any_iocp(obj);
		break;
	}
}

static int ntsync_wait_any(struct ntsync_device *dev, void __user *argp)
{
	struct ntsync_wait_args args;
	__u32 i, total_count;
	struct ntsync_q *q;
	int signaled;
	bool all;
	int ret;

	if (copy_from_user(&args, argp, sizeof(args)))
		return -EFAULT;

	ret = setup_wait(dev, &args, false, &q);
	if (ret < 0)
		return ret;

	total_count = args.count;
	if (args.alert)
		total_count++;

	/* queue ourselves */

	for (i = 0; i < total_count; i++) {
		struct ntsync_q_entry *entry = &q->entries[i];
		struct ntsync_obj *obj = entry->obj;

		all = ntsync_lock_obj(dev, obj);
		list_add_tail(&entry->node, &obj->any_waiters);
		ntsync_unlock_obj(dev, obj, all);
	}

	/*
	 * Check if we are already signaled.
	 *
	 * Note that the API requires that normal objects are checked before
	 * the alert event. Hence we queue the alert event last, and check
	 * objects in order.
	 */

	for (i = 0; i < total_count; i++) {
		struct ntsync_obj *obj = q->entries[i].obj;

		if (atomic_read(&q->signaled) != -1)
			break;

		all = ntsync_lock_obj(dev, obj);
		try_wake_any_obj(obj);
		ntsync_unlock_obj(dev, obj, all);
	}

	/* sleep */

	ret = ntsync_schedule(q, &args);

	/* and finally, unqueue */

	for (i = 0; i < total_count; i++) {
		struct ntsync_q_entry *entry = &q->entries[i];
		struct ntsync_obj *obj = entry->obj;

		all = ntsync_lock_obj(dev, obj);
		list_del(&entry->node);
		ntsync_unlock_obj(dev, obj, all);

		put_obj(obj);
	}

	signaled = atomic_read(&q->signaled);
	if (signaled != -1) {
		struct ntsync_wait_args __user *user_args = argp;

		/* even if we caught a signal, we need to communicate success */
		ret = q->ownerdead ? -EOWNERDEAD : 0;

		if (put_user(signaled, &user_args->index))
			ret = -EFAULT;
	} else if (!ret) {
		ret = -ETIMEDOUT;
	}

	kfree(q);
	return ret;
}

static int ntsync_wait_all(struct ntsync_device *dev, void __user *argp)
{
	struct ntsync_wait_args args;
	struct ntsync_q *q;
	int signaled;
	__u32 i;
	int ret;

	if (copy_from_user(&args, argp, sizeof(args)))
		return -EFAULT;

	ret = setup_wait(dev, &args, true, &q);
	if (ret < 0)
		return ret;

	/* queue ourselves */

	mutex_lock(&dev->wait_all_lock);

	for (i = 0; i < args.count; i++) {
		struct ntsync_q_entry *entry = &q->entries[i];
		struct ntsync_obj *obj = entry->obj;

		atomic_inc(&obj->all_hint);

		/*
		 * obj->all_waiters is protected by dev->wait_all_lock rather
		 * than obj->lock, so there is no need to acquire obj->lock
		 * here.
		 */
		list_add_tail(&entry->node, &obj->all_waiters);
	}
	if (args.alert) {
		struct ntsync_q_entry *entry = &q->entries[args.count];
		struct ntsync_obj *obj = entry->obj;

		dev_lock_obj(dev, obj);
		list_add_tail(&entry->node, &obj->any_waiters);
		dev_unlock_obj(dev, obj);
	}

	/* check if we are already signaled */

	try_wake_all(dev, q, NULL);

	mutex_unlock(&dev->wait_all_lock);

	/*
	 * Check if the alert event is signaled, making sure to do so only
	 * after checking if the other objects are signaled.
	 */

	if (args.alert) {
		struct ntsync_obj *obj = q->entries[args.count].obj;

		if (atomic_read(&q->signaled) == -1) {
			bool all = ntsync_lock_obj(dev, obj);
			try_wake_any_obj(obj);
			ntsync_unlock_obj(dev, obj, all);
		}
	}

	/* sleep */

	ret = ntsync_schedule(q, &args);

	/* and finally, unqueue */

	mutex_lock(&dev->wait_all_lock);

	for (i = 0; i < args.count; i++) {
		struct ntsync_q_entry *entry = &q->entries[i];
		struct ntsync_obj *obj = entry->obj;

		/*
		 * obj->all_waiters is protected by dev->wait_all_lock rather
		 * than obj->lock, so there is no need to acquire it here.
		 */
		list_del(&entry->node);

		atomic_dec(&obj->all_hint);

		put_obj(obj);
	}

	mutex_unlock(&dev->wait_all_lock);

	if (args.alert) {
		struct ntsync_q_entry *entry = &q->entries[args.count];
		struct ntsync_obj *obj = entry->obj;
		bool all;

		all = ntsync_lock_obj(dev, obj);
		list_del(&entry->node);
		ntsync_unlock_obj(dev, obj, all);

		put_obj(obj);
	}

	signaled = atomic_read(&q->signaled);
	if (signaled != -1) {
		struct ntsync_wait_args __user *user_args = argp;

		/* even if we caught a signal, we need to communicate success */
		ret = q->ownerdead ? -EOWNERDEAD : 0;

		if (put_user(signaled, &user_args->index))
			ret = -EFAULT;
	} else if (!ret) {
		ret = -ETIMEDOUT;
	}

	kfree(q);
	return ret;
}

static int ntsync_char_open(struct inode *inode, struct file *file)
{
	struct ntsync_device *dev;

	dev = kzalloc_obj(*dev);
	if (!dev)
		return -ENOMEM;

	mutex_init(&dev->wait_all_lock);

	file->private_data = dev;
	dev->file = file;
	return nonseekable_open(inode, file);
}

static int ntsync_char_release(struct inode *inode, struct file *file)
{
	struct ntsync_device *dev = file->private_data;

	kfree(dev);

	return 0;
}

static long ntsync_char_ioctl(struct file *file, unsigned int cmd,
			      unsigned long parm)
{
	struct ntsync_device *dev = file->private_data;
	void __user *argp = (void __user *)parm;

	switch (cmd) {
	case NTSYNC_IOC_CREATE_EVENT:
		return ntsync_create_event(dev, argp);
	case NTSYNC_IOC_CREATE_MUTEX:
		return ntsync_create_mutex(dev, argp);
	case NTSYNC_IOC_CREATE_SEM:
		return ntsync_create_sem(dev, argp);
	case NTSYNC_IOC_CREATE_IOCP:
		return ntsync_create_iocp(dev, argp);
	case NTSYNC_IOC_WAIT_ALL:
		return ntsync_wait_all(dev, argp);
	case NTSYNC_IOC_WAIT_ANY:
		return ntsync_wait_any(dev, argp);
	default:
		return -ENOIOCTLCMD;
	}
}

static const struct file_operations ntsync_fops = {
	.owner		= THIS_MODULE,
	.open		= ntsync_char_open,
	.release	= ntsync_char_release,
	.unlocked_ioctl	= ntsync_char_ioctl,
	.compat_ioctl	= compat_ptr_ioctl,
};

static struct miscdevice ntsync_misc = {
	.minor		= MISC_DYNAMIC_MINOR,
	.name		= NTSYNC_NAME,
	.fops		= &ntsync_fops,
	.mode		= 0666,
};

module_misc_device(ntsync_misc);

MODULE_AUTHOR("Elizabeth Figura <zfigura@codeweavers.com>");
MODULE_DESCRIPTION("Kernel driver for NT synchronization primitives");
MODULE_LICENSE("GPL");
