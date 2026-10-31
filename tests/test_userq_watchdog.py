#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Exercise the production UserQ watchdog with deterministic work scheduling."""
import os
import pathlib
import re
import shlex
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(os.environ.get('USERQ_TEST_ROOT', pathlib.Path(__file__).resolve().parents[1]))
AMD = 'amdgpu/drivers/gpu/drm/amd/amdgpu/'


def source(file):
    revision = os.environ.get('USERQ_TEST_REVISION')
    if revision:
        return subprocess.check_output(['git', 'show', revision + ':' + AMD + file],
                                       cwd=ROOT, text=True)
    return (ROOT / AMD / file).read_text()


def extract_function(text, name):
    match = re.search(r'^(?:static )?(?:unsigned long|int|void|bool|u64)\s+' + re.escape(name)
                      + r'\(.*?^\}', text, re.M | re.S)
    if not match:
        raise AssertionError('Production function not found: ' + name)
    return match.group()


class UserqWatchdogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='userq-watchdog-')
        cls.addClassCleanup(cls.temporary.cleanup)
        directory = pathlib.Path(cls.temporary.name)
        cls.binary = directory / 'watchdog'
        actual = '\n\n'.join(extract_function(source('amdgpu_userq_fence.c'), name)
                             for name in ('amdgpu_userq_fence_read', 'amdgpu_userq_fence_has_pending'))
        actual += '\n\n' + '\n\n'.join(
            extract_function(source('amdgpu_userq.c'), name) for name in FUNCTIONS)
        program = directory / 'watchdog.c'
        program.write_text(HARNESS.replace('ACTUAL_FUNCTIONS', actual))
        flags = shlex.split(os.environ.get('CC', 'cc'))
        flags += ['-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror', '-pthread',
                  '-Wno-unused-parameter', '-Wno-sign-compare']
        if os.environ.get('USERQ_TEST_SANITIZERS'):
            flags += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                      '-fno-sanitize-recover=all']
        subprocess.run(flags + [str(program), '-o', str(cls.binary)], check=True, timeout=30)

    def run_case(self, case):
        subprocess.run([str(self.binary), case], check=True, timeout=30)

    def test_timeout_types(self):
        self.run_case('timeout_types')

    def test_no_pending(self):
        self.run_case('no_pending')

    def test_completed_without_irq(self):
        self.run_case('completed_without_irq')

    def test_progress_without_irq(self):
        self.run_case('progress_without_irq')

    def test_repeated_submissions(self):
        self.run_case('repeated_submissions')

    def test_early_callback(self):
        self.run_case('early_callback')

    def test_wrap_deadline(self):
        self.run_case('wrap_deadline')

    def test_pause_unmap(self):
        self.run_case('pause_unmap')

    def test_pause_preempt(self):
        self.run_case('pause_preempt')

    def test_resume_completed(self):
        self.run_case('resume_completed')

    def test_failed_initial_map(self):
        self.run_case('failed_initial_map')

    def test_failed_pending_map(self):
        self.run_case('failed_pending_map')

    def test_progress_irq(self):
        self.run_case('progress_irq')

    def test_completion_irq(self):
        self.run_case('completion_irq')

    def test_unrelated_irq(self):
        self.run_case('unrelated_irq')

    def test_firmware_reset(self):
        self.run_case('firmware_reset')

    def test_reset_fallback(self):
        self.run_case('reset_fallback')

    def test_reset_disabled(self):
        self.run_case('reset_disabled')

    def test_stopped(self):
        self.run_case('stopped')

    def test_stopped_cannot_resume(self):
        self.run_case('stopped_cannot_resume')

    def test_worker_vs_pause(self):
        self.run_case('worker_vs_pause')

    def test_arm_vs_stop(self):
        self.run_case('arm_vs_stop')

    def test_worker_vs_resume(self):
        self.run_case('worker_vs_resume')

    def test_zero_deadline(self):
        self.run_case('zero_deadline')

    def test_progress_64bit(self):
        self.run_case('progress_64bit')

    def test_firmware_unmapped(self):
        self.run_case('firmware_unmapped')

    def test_firmware_preempted(self):
        self.run_case('firmware_preempted')

    def test_already_resetting(self):
        self.run_case('already_resetting')

    def test_failure_requests_recovery(self):
        self.run_case('failure_requests_recovery')

    def test_reset_request_vs_pre_reset(self):
        self.run_case('reset_request_vs_pre_reset')

    def test_restore_invalid_mapping(self):
        self.run_case('restore_invalid_mapping')

    def test_restore_vs_worker(self):
        self.run_case('restore_vs_worker')

    def test_create_failure_stops_before_free(self):
        body = extract_function(source('amdgpu_userq.c'), 'amdgpu_userq_create')
        cleanup = body[body.index('erase_doorbell:'):]
        erase = cleanup.index('xa_erase_irq(')
        stop = cleanup.index('amdgpu_userq_stop_hang_detect_work(')
        self.assertLess(erase, stop)
        for operation in ('uq_funcs->mqd_destroy(', 'amdgpu_userq_fence_driver_free(',
                          'mutex_destroy(', 'kfree('):
            self.assertLess(stop, cleanup.index(operation), operation)


FUNCTIONS = (
    'amdgpu_userq_hang_timeout',
    'amdgpu_userq_arm_hang_detect_work',
    'amdgpu_userq_start_hang_detect_work',
    'amdgpu_userq_pause_hang_detect_work',
    'amdgpu_userq_resume_hang_detect_work',
    'amdgpu_userq_stop_hang_detect_work',
    'amdgpu_userq_request_reset',
    'amdgpu_userq_hang_detect_work',
    'amdgpu_userq_process_fence_irq',
    'amdgpu_userq_preempt_helper',
    'amdgpu_userq_restore_helper',
    'amdgpu_userq_unmap_helper',
    'amdgpu_userq_map_helper',
    'amdgpu_userq_process_reset_irq',
    'amdgpu_userq_pre_reset',
    'amdgpu_userq_restore_all',
    'amdgpu_userq_suspend',
)
HARNESS = r'''
#include <assert.h>
#include <errno.h>
#include <limits.h>
#include <pthread.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef uint32_t u32;
typedef uint64_t u64;
#define container_of(ptr, type, member) ((type *)((char *)(ptr) - offsetof(type, member)))
#define unlikely(v) (v)
#define READ_ONCE(v) (v)
#define le64_to_cpu(v) (v)
#define WRITE_ONCE(v, n) ((v) = (n))
#define time_before(a, b) ((long)((a) - (b)) < 0)
#define time_after(a, b) time_before(b, a)
#define time_before_eq(a, b) ((long)((a) - (b)) <= 0)
#define time_after_eq(a, b) time_before_eq(b, a)
#define adev_to_drm(adev) (adev)
#define dev_err(...) ((void)0)
#define dev_warn(...) ((void)0)
#define drm_file_err(...) ((void)0)
#define trace_amdgpu_userq_state_start(q) ((void)0)
#define trace_amdgpu_userq_state_changed(q, s) ((void)0)

struct spinlock { pthread_mutex_t mutex; };
typedef struct spinlock spinlock_t;
static _Thread_local unsigned spin_depth;
static _Thread_local bool block_before_lock;
static _Thread_local unsigned mutex_depth;
static bool userq_mutex_held;
static pthread_barrier_t lock_arrived, lock_release;
static void barrier_wait(pthread_barrier_t *barrier)
{
    int ret = pthread_barrier_wait(barrier);
    assert(!ret || ret == PTHREAD_BARRIER_SERIAL_THREAD);
}
static void spin_lock_init(spinlock_t *lock)
{ assert(!pthread_mutex_init(&lock->mutex, NULL)); }
static void lock_gate(void)
{
    if (block_before_lock) {
        block_before_lock = false;
        barrier_wait(&lock_arrived);
        barrier_wait(&lock_release);
    }
}
static void spin_lock(spinlock_t *lock)
{
    lock_gate();
    assert(!pthread_mutex_lock(&lock->mutex));
    spin_depth++;
}
static void spin_unlock(spinlock_t *lock)
{ assert(spin_depth); spin_depth--; assert(!pthread_mutex_unlock(&lock->mutex)); }
#define spin_lock_irqsave(lock, flags) do { (flags) = 0; spin_lock(lock); } while (0)
#define spin_unlock_irqrestore(lock, flags) do { (void)(flags); spin_unlock(lock); } while (0)
#define lockdep_assert_held(lock) assert(spin_depth)

struct mutex { pthread_mutex_t mutex; bool is_userq; };
static void mutex_init(struct mutex *lock)
{ assert(!pthread_mutex_init(&lock->mutex, NULL)); }
static void mutex_lock(struct mutex *lock)
{
    assert(!spin_depth);
    lock_gate();
    assert(!pthread_mutex_lock(&lock->mutex));
    mutex_depth++;
    if (lock->is_userq) userq_mutex_held = true;
}
static void mutex_unlock(struct mutex *lock)
{
    assert(!spin_depth && mutex_depth);
    mutex_depth--;
    if (lock->is_userq) userq_mutex_held = false;
    assert(!pthread_mutex_unlock(&lock->mutex));
}

static struct mutex *guard_mutex_lock(struct mutex *lock)
{ mutex_lock(lock); return lock; }
static void guard_mutex_cleanup(struct mutex **lock) { mutex_unlock(*lock); }
#define guard(type) struct mutex *guarded_mutex __attribute__((cleanup(guard_mutex_cleanup))) = guard_mutex_lock

struct work_struct { unsigned unused; };
struct delayed_work {
    struct work_struct work;
    bool pending;
    unsigned long expires;
    unsigned scheduled, cancelled;
};
static unsigned long jiffies;
static bool mod_delayed_work(void *wq, struct delayed_work *work, unsigned long delay)
{
    assert(spin_depth);
    bool pending = work->pending;
    work->pending = true;
    work->expires = jiffies + delay;
    work->scheduled++;
    return pending;
}
static bool cancel_delayed_work(struct delayed_work *work)
{
    bool pending = work->pending;
    work->pending = false;
    work->cancelled++;
    return pending;
}

static void cancel_delayed_work_sync(struct delayed_work *work)
{
    assert(!spin_depth && !mutex_depth && !userq_mutex_held);
    work->pending = false;
    work->cancelled++;
}

#define AMDGPU_RING_TYPE_GFX 0
#define AMDGPU_RING_TYPE_COMPUTE 1
#define AMDGPU_RING_TYPE_SDMA 2
#define AMDGPU_HW_IP_COMPUTE AMDGPU_RING_TYPE_COMPUTE
#define AMDGPU_RESET_TYPE_PER_QUEUE 1
#define DRM_WEDGE_RECOVERY_NONE 0

enum amdgpu_userq_state {
    AMDGPU_USERQ_STATE_UNMAPPED,
    AMDGPU_USERQ_STATE_MAPPED,
    AMDGPU_USERQ_STATE_PREEMPTED,
    AMDGPU_USERQ_STATE_HUNG,
    AMDGPU_USERQ_STATE_INVALID_VA,
    AMDGPU_USERQ_STATE_CANCELED,
};
struct list_head { bool empty; void *last; };
#define list_last_entry(head, type, member) ((type *)((head)->last))
static bool list_empty(const struct list_head *head) { return head->empty; }
struct amdgpu_userq_fence_driver {
    spinlock_t fence_list_lock;
    struct list_head fences;
    u64 *cpu_addr;
};
struct dma_fence { bool signaled; u64 seqno; };
struct amdgpu_userq_fence { struct dma_fence base; struct list_head link; };
struct amdgpu_usermode_queue;
struct amdgpu_userq_funcs {
    int (*reset)(struct amdgpu_usermode_queue *queue);
    int (*unmap)(struct amdgpu_usermode_queue *queue);
    int (*map)(struct amdgpu_usermode_queue *queue);
    int (*preempt)(struct amdgpu_usermode_queue *queue);
    int (*restore)(struct amdgpu_usermode_queue *queue);
};
struct xarray {
    spinlock_t lock;
    struct amdgpu_usermode_queue *queues[4];
};
struct reset_domain { void *wq; };
struct amdgpu_device {
    const struct amdgpu_userq_funcs *userq_funcs[3];
    struct reset_domain *reset_domain;
    unsigned long gfx_timeout, compute_timeout, sdma_timeout;
    bool debug_disable_gpu_ring_reset, in_s0ix;
    unsigned gpu_reset_counter;
    struct xarray userq_doorbell_xa;
    void *dev;
};
struct amdgpu_userq_mgr {
    struct amdgpu_device *adev;
    struct work_struct reset_work;
    struct delayed_work resume_work;
    struct mutex userq_mutex;
    struct xarray userq_xa;
    void *file;
};
struct amdgpu_vm { u32 pasid; };
struct amdgpu_usermode_queue {
    struct amdgpu_userq_mgr *userq_mgr;
    struct amdgpu_vm *vm;
    unsigned queue_type, doorbell_offset;
    enum amdgpu_userq_state state;
    struct amdgpu_userq_fence_driver *fence_drv;
    struct dma_fence *last_fence;
    struct delayed_work hang_detect_work;
    spinlock_t hang_detect_lock;
    struct mutex state_mutex;
    unsigned long hang_detect_deadline;
    u64 hang_detect_progress;
    bool hang_detect_stopped, hang_detect_paused, hang_detect_reset_requested, hang_detect_armed;
};
struct drm_wedge_task_info { unsigned unused; };
struct amdgpu_task_info { struct drm_wedge_task_info task; };
static struct amdgpu_device adev;
static struct amdgpu_userq_mgr mgr;
static struct amdgpu_usermode_queue queue;
static struct amdgpu_userq_fence_driver driver;
static struct dma_fence last_fence;
static struct amdgpu_userq_fence pending_fence;
static struct amdgpu_vm vm;
static struct reset_domain domain;
static struct amdgpu_task_info task_info;
static u64 rptr;
static bool amdgpu_gpu_recovery = true, per_queue_reset_supported = true;
static unsigned queue_resets, compute_resets, full_resets, forced, wedges, task_refs;
static unsigned maps, unmaps, preempts, restores, processed;
static int reset_error, map_error, unmap_error, preempt_error, restore_error;
static int process_result = -ENOENT;
static bool device_resetting, mappings_valid = true;
static _Thread_local bool block_after_validation;
static bool amdgpu_in_reset(struct amdgpu_device *dev) { return device_resetting; }
static u32 amdgpu_userq_get_supported_ip_mask(struct amdgpu_device *dev) { return 7; }
static bool amdgpu_userq_buffer_vas_mapped(struct amdgpu_usermode_queue *q)
{
    if (block_after_validation) {
        assert(!mappings_valid);
        block_after_validation = false;
        block_before_lock = true;
    }
    return mappings_valid;
}

static bool amdgpu_userq_is_reset_type_supported(struct amdgpu_device *dev,
                                               int type, int reset_type)
{ return per_queue_reset_supported; }
static struct amdgpu_task_info *amdgpu_vm_get_task_info_pasid(struct amdgpu_device *dev,
                                                           u32 pasid)
{ assert(!spin_depth); task_refs++; return &task_info; }
static void amdgpu_vm_print_task_info(struct amdgpu_device *dev, struct amdgpu_task_info *info) {}
static void amdgpu_vm_put_task_info(struct amdgpu_task_info *info)
{ if (info) { assert(task_refs); task_refs--; } }
static void atomic_inc(unsigned *counter) { ++*counter; }
static int reset_queue(struct amdgpu_usermode_queue *q)
{
    assert(!spin_depth && !mutex_depth);
    assert(q->state == AMDGPU_USERQ_STATE_HUNG);
    queue_resets++;
    return reset_error;
}
static int amdgpu_gfx_reset_mes_compute(struct amdgpu_device *dev, void *a, void *b,
                                      struct amdgpu_usermode_queue *q, void *c, void *d)
{
    assert(!spin_depth && !mutex_depth);
    assert(q->state == AMDGPU_USERQ_STATE_HUNG);
    compute_resets++;
    return reset_error;
}
static void amdgpu_userq_fence_driver_force_completion(struct amdgpu_usermode_queue *q)
{ assert(!spin_depth && (!mutex_depth || device_resetting)); forced++; driver.fences.empty = true; }
static void drm_dev_wedged_event(struct amdgpu_device *dev, int state,
                                struct drm_wedge_task_info *info)
{ assert(!spin_depth); wedges++; }
static void amdgpu_userq_mgr_reset_work(struct work_struct *work)
{ assert(!spin_depth && !mutex_depth); full_resets++; }
static int amdgpu_userq_fence_driver_process(struct amdgpu_userq_fence_driver *drv)
{
    processed++;
    if (!process_result || rptr >= pending_fence.base.seqno) drv->fences.empty = true;
    return process_result;
}
static int hw_map(struct amdgpu_usermode_queue *q)
{ assert(mutex_depth && !spin_depth); maps++; return map_error; }
static int hw_unmap(struct amdgpu_usermode_queue *q)
{ assert(mutex_depth && !spin_depth); unmaps++; return unmap_error; }
static int hw_preempt(struct amdgpu_usermode_queue *q)
{ assert(mutex_depth && !spin_depth); preempts++; return preempt_error; }
static int hw_restore(struct amdgpu_usermode_queue *q)
{ assert(mutex_depth && !spin_depth); restores++; return restore_error; }
#define xa_lock_irqsave(xa, flags) spin_lock_irqsave(&(xa)->lock, flags)
#define xa_unlock_irqrestore(xa, flags) spin_unlock_irqrestore(&(xa)->lock, flags)
#define xa_for_each(xa, idx, q) for ((idx) = 0; (idx) < 4; (idx)++) if (((q) = (xa)->queues[idx]))
static struct amdgpu_usermode_queue *xa_load(struct xarray *xa, u32 id)
{
    for (unsigned i = 0; i < 4; i++)
        if (xa->queues[i] && xa->queues[i]->doorbell_offset == id) return xa->queues[i];
    return NULL;
}

ACTUAL_FUNCTIONS
static const struct amdgpu_userq_funcs funcs = {
    .reset = reset_queue, .map = hw_map, .unmap = hw_unmap,
    .preempt = hw_preempt, .restore = hw_restore,
};
static void fixture(void)
{
    static bool initialized;
    if (initialized) {
        assert(!pthread_mutex_destroy(&queue.hang_detect_lock.mutex));
        assert(!pthread_mutex_destroy(&queue.state_mutex.mutex));
        assert(!pthread_mutex_destroy(&mgr.userq_mutex.mutex));
        assert(!pthread_mutex_destroy(&driver.fence_list_lock.mutex));
        assert(!pthread_mutex_destroy(&adev.userq_doorbell_xa.lock.mutex));
    }
    initialized = true;
    memset(&queue, 0, sizeof(queue));
    memset(&driver, 0, sizeof(driver));
    memset(&adev, 0, sizeof(adev));
    memset(&mgr, 0, sizeof(mgr));
    rptr = 12;
    jiffies = 100;
    driver.cpu_addr = &rptr;
    driver.fences.empty = false;
    driver.fences.last = &pending_fence;
    pending_fence.base.seqno = 20;
    last_fence.signaled = false;
    adev.userq_funcs[0] = adev.userq_funcs[1] = adev.userq_funcs[2] = &funcs;
    adev.gfx_timeout = 10;
    adev.compute_timeout = 20;
    adev.sdma_timeout = 30;
    adev.reset_domain = &domain;
    mgr.adev = &adev;
    vm.pasid = 555;
    queue.userq_mgr = &mgr;
    queue.vm = &vm;
    queue.doorbell_offset = 0x1408;
    queue.fence_drv = &driver;
    queue.last_fence = &last_fence;
    queue.state = AMDGPU_USERQ_STATE_MAPPED;
    spin_lock_init(&queue.hang_detect_lock);
    spin_lock_init(&driver.fence_list_lock);
    spin_lock_init(&adev.userq_doorbell_xa.lock);
    mutex_init(&queue.state_mutex);
    mutex_init(&mgr.userq_mutex);
    mgr.userq_mutex.is_userq = true;
    mgr.userq_xa.queues[0] = &queue;
    adev.userq_doorbell_xa.queues[0] = &queue;
    queue_resets = compute_resets = full_resets = forced = wedges = task_refs = 0;
    maps = unmaps = preempts = restores = processed = 0;
    reset_error = map_error = unmap_error = preempt_error = restore_error = 0;
    process_result = -ENOENT;
    userq_mutex_held = device_resetting = false;
    mappings_valid = true;
    amdgpu_gpu_recovery = per_queue_reset_supported = true;
    assert(!spin_depth && !mutex_depth);
}
static void fire(void)
{
    queue.hang_detect_work.pending = false;
    amdgpu_userq_hang_detect_work(&queue.hang_detect_work.work);
    assert(!spin_depth && !mutex_depth && !task_refs);
}
static void arm(void) { amdgpu_userq_start_hang_detect_work(&queue); }
static void assert_no_reset(void)
{ assert(!queue_resets && !compute_resets && !full_resets && !forced && !wedges); }
static void timeout_types(void)
{
    for (unsigned type = 0; type < 3; type++) {
        fixture();
        queue.queue_type = type;
        arm();
        assert(queue.hang_detect_work.pending);
        assert(queue.hang_detect_work.expires == 100 + 10 * (type + 1));
        jiffies = queue.hang_detect_work.expires;
        /* Eviction can wait on the last fence while holding this mutex. */
        userq_mutex_held = true;
        fire();
        userq_mutex_held = false;
        assert(queue_resets == (type != AMDGPU_RING_TYPE_COMPUTE));
        assert(compute_resets == (type == AMDGPU_RING_TYPE_COMPUTE));
        assert(forced == 1 && wedges == 1 && adev.gpu_reset_counter == 1);
        assert(!queue.hang_detect_work.pending);
    }
}
static void no_pending(void)
{
    fixture();
    driver.fences.empty = true;
    last_fence.signaled = true;
    arm();
    assert(!queue.hang_detect_work.pending);
    jiffies = 200;
    fire();
    assert_no_reset();
}
static void completed_without_irq(void)
{
    fixture();
    arm();
    jiffies = 200;
    rptr = pending_fence.base.seqno;
    last_fence.signaled = true;
    fire();
    assert(processed == 1 && driver.fences.empty);
    assert_no_reset();
    assert(!queue.hang_detect_work.pending);
}
static void progress_without_irq(void)
{
    fixture();
    arm();
    jiffies = 110;
    rptr++;
    fire();
    assert_no_reset();
    assert(queue.hang_detect_work.pending && queue.hang_detect_work.expires == 120);
    jiffies = 120;
    fire();
    assert(queue_resets == 1 && forced == 1);
}
static void repeated_submissions(void)
{
    fixture();
    arm();
    jiffies = 109;
    arm();
    assert(queue.hang_detect_work.expires == 110);
    jiffies = 110;
    fire();
    assert(queue_resets == 1);
}
static void early_callback(void)
{
    fixture();
    arm();
    jiffies = 103;
    fire();
    assert_no_reset();
    assert(queue.hang_detect_work.pending && queue.hang_detect_work.expires == 110);
    jiffies = 110;
    fire();
    assert(queue_resets == 1);
}
static void wrap_deadline(void)
{
    fixture();
    jiffies = ULONG_MAX - 4;
    arm();
    assert(queue.hang_detect_work.expires == 5);
    jiffies = ULONG_MAX;
    fire();
    assert_no_reset();
    assert(queue.hang_detect_work.expires == 5);
    jiffies = 5;
    fire();
    assert(queue_resets == 1);
}
static void pause_unmap(void)
{
    fixture();
    arm();
    assert(!amdgpu_userq_unmap_helper(&queue));
    assert(queue.state == AMDGPU_USERQ_STATE_UNMAPPED && unmaps == 1);
    assert(queue.hang_detect_paused && !queue.hang_detect_work.pending);
    jiffies = 200;
    fire();
    arm();
    assert_no_reset();
    assert(!queue.hang_detect_work.pending);
    assert(!amdgpu_userq_map_helper(&queue));
    assert(queue.state == AMDGPU_USERQ_STATE_MAPPED && maps == 1);
    assert(!queue.hang_detect_paused && queue.hang_detect_work.pending);
    assert(queue.hang_detect_work.expires == 210);
}
static void pause_preempt(void)
{
    fixture();
    arm();
    assert(!amdgpu_userq_preempt_helper(&queue));
    assert(queue.state == AMDGPU_USERQ_STATE_PREEMPTED && preempts == 1);
    assert(queue.hang_detect_paused && !queue.hang_detect_work.pending);
    jiffies = 200;
    fire();
    assert_no_reset();
    assert(!amdgpu_userq_restore_helper(&queue));
    assert(queue.state == AMDGPU_USERQ_STATE_MAPPED && restores == 1);
    assert(!queue.hang_detect_paused && queue.hang_detect_work.pending);
    assert(queue.hang_detect_work.expires == 210);
}
static void resume_completed(void)
{
    fixture();
    arm();
    assert(!amdgpu_userq_unmap_helper(&queue));
    driver.fences.empty = true;
    last_fence.signaled = true;
    jiffies = 200;
    assert(!amdgpu_userq_map_helper(&queue));
    assert(!queue.hang_detect_work.pending);
    fire();
    assert_no_reset();
}
static void failed_initial_map(void)
{
    fixture();
    queue.state = AMDGPU_USERQ_STATE_UNMAPPED;
    queue.hang_detect_paused = true;
    queue.last_fence = NULL;
    driver.fences.empty = true;
    map_error = -EIO;
    assert(amdgpu_userq_map_helper(&queue) == -EIO);
    assert(queue.state == AMDGPU_USERQ_STATE_HUNG && queue.hang_detect_paused);
    assert(!queue.hang_detect_reset_requested && !queue.hang_detect_work.pending);
    fire();
    assert_no_reset();
}
static void failed_pending_map(void)
{
    fixture();
    arm();
    assert(!amdgpu_userq_unmap_helper(&queue));
    map_error = -EIO;
    assert(amdgpu_userq_map_helper(&queue) == -EIO);
    assert(queue.state == AMDGPU_USERQ_STATE_HUNG && queue.hang_detect_paused);
    assert(queue.hang_detect_reset_requested && queue.hang_detect_work.pending);
    fire();
    assert(queue_resets == 1 && forced == 1);
}

static void progress_irq(void)
{
    fixture();
    arm();
    jiffies = 105;
    rptr++;
    process_result = 1;
    amdgpu_userq_process_fence_irq(&adev, queue.doorbell_offset);
    assert(queue.hang_detect_work.pending && queue.hang_detect_work.expires == 115);
    jiffies = 110;
    fire();
    assert_no_reset();
    assert(queue.hang_detect_work.pending && queue.hang_detect_work.expires == 115);
}
static void completion_irq(void)
{
    fixture();
    arm();
    process_result = 0;
    last_fence.signaled = true;
    rptr++;
    amdgpu_userq_process_fence_irq(&adev, queue.doorbell_offset);
    assert(!queue.hang_detect_work.pending);
    jiffies = 200;
    fire();
    assert_no_reset();
}
static void unrelated_irq(void)
{
    fixture();
    arm();
    amdgpu_userq_process_fence_irq(&adev, queue.doorbell_offset + 1);
    amdgpu_userq_process_reset_irq(&adev, vm.pasid + 1, queue.doorbell_offset);
    amdgpu_userq_process_reset_irq(&adev, vm.pasid, queue.doorbell_offset + 1);
    assert(!processed && !queue.hang_detect_reset_requested);
    assert(queue.hang_detect_work.expires == 110);
    assert_no_reset();
}
static void firmware_reset(void)
{
    fixture();
    driver.fences.empty = true;
    last_fence.signaled = true;
    queue.last_fence = NULL;
    amdgpu_userq_process_reset_irq(&adev, vm.pasid, queue.doorbell_offset);
    assert(queue.hang_detect_reset_requested && queue.hang_detect_work.pending);
    jiffies = queue.hang_detect_work.expires;
    fire();
    assert(queue_resets == 1 && forced == 1);
}
static void reset_fallback(void)
{
    for (unsigned supported = 0; supported < 2; supported++) {
        fixture();
        per_queue_reset_supported = supported;
        reset_error = -EIO;
        arm();
        jiffies = 110;
        fire();
        assert(queue_resets == supported && full_resets == 1);
        assert(!forced && !wedges);
    }
}
static void reset_disabled(void)
{
    for (unsigned debug = 0; debug < 2; debug++) {
        fixture();
        adev.debug_disable_gpu_ring_reset = debug;
        amdgpu_gpu_recovery = debug;
        arm();
        jiffies = 110;
        fire();
        assert_no_reset();
    }
}
static void stopped(void)
{
    fixture();
    arm();
    amdgpu_userq_stop_hang_detect_work(&queue);
    assert(queue.hang_detect_stopped && !queue.hang_detect_work.pending);
    jiffies = 200;
    arm();
    amdgpu_userq_resume_hang_detect_work(&queue);
    amdgpu_userq_request_reset(&queue);
    fire();
    assert(!queue.hang_detect_work.pending);
    assert_no_reset();
}

static void stopped_cannot_resume(void)
{
    for (unsigned preempted = 0; preempted < 2; preempted++) {
        fixture();
        queue.state = preempted ? AMDGPU_USERQ_STATE_PREEMPTED : AMDGPU_USERQ_STATE_UNMAPPED;
        queue.hang_detect_paused = true;
        amdgpu_userq_stop_hang_detect_work(&queue);
        assert(!amdgpu_userq_map_helper(&queue));
        assert(!amdgpu_userq_restore_helper(&queue));
        assert(!maps && !restores);
        assert(queue.state == (preempted ? AMDGPU_USERQ_STATE_PREEMPTED : AMDGPU_USERQ_STATE_UNMAPPED));
        assert(queue.hang_detect_stopped && queue.hang_detect_paused);
        assert(!queue.hang_detect_work.pending);
        assert_no_reset();
    }
}

static void *blocked_worker(void *data)
{
    block_before_lock = true;
    amdgpu_userq_hang_detect_work(&queue.hang_detect_work.work);
    assert(!spin_depth && !mutex_depth);
    return NULL;
}
static void *blocked_arm(void *data)
{
    block_before_lock = true;
    arm();
    assert(!spin_depth && !mutex_depth);
    return NULL;
}
static void race_begin(pthread_t *thread, void *(*func)(void *))
{
    assert(!pthread_barrier_init(&lock_arrived, NULL, 2));
    assert(!pthread_barrier_init(&lock_release, NULL, 2));
    assert(!pthread_create(thread, NULL, func, NULL));
    barrier_wait(&lock_arrived);
}
static void race_end(pthread_t thread)
{
    barrier_wait(&lock_release);
    assert(!pthread_join(thread, NULL));
    assert(!pthread_barrier_destroy(&lock_arrived));
    assert(!pthread_barrier_destroy(&lock_release));
}
static void worker_vs_pause(void)
{
    pthread_t thread;
    fixture();
    arm();
    queue.hang_detect_work.pending = false;
    jiffies = 110;
    race_begin(&thread, blocked_worker);
    assert(!amdgpu_userq_unmap_helper(&queue));
    race_end(thread);
    assert_no_reset();
    assert(queue.hang_detect_paused && !queue.hang_detect_work.pending);
}
static void arm_vs_stop(void)
{
    pthread_t thread;
    fixture();
    race_begin(&thread, blocked_arm);
    amdgpu_userq_stop_hang_detect_work(&queue);
    race_end(thread);
    assert_no_reset();
    assert(queue.hang_detect_stopped && !queue.hang_detect_work.pending);
}
static void worker_vs_resume(void)
{
    pthread_t thread;
    fixture();
    arm();
    queue.hang_detect_work.pending = false;
    jiffies = 110;
    race_begin(&thread, blocked_worker);
    assert(!amdgpu_userq_unmap_helper(&queue));
    jiffies = 200;
    assert(!amdgpu_userq_map_helper(&queue));
    race_end(thread);
    assert_no_reset();
    assert(queue.hang_detect_work.pending && queue.hang_detect_work.expires == 210);
}

static void zero_deadline(void)
{
    fixture();
    jiffies = ULONG_MAX - 9;
    arm();
    assert(queue.hang_detect_armed && queue.hang_detect_work.expires == 0);
    jiffies = ULONG_MAX;
    arm();
    fire();
    assert_no_reset();
    assert(queue.hang_detect_work.expires == 0);
    jiffies = 0;
    fire();
    assert(queue_resets == 1);
}
static void progress_64bit(void)
{
    fixture();
    rptr = UINT64_C(0x100000000);
    pending_fence.base.seqno = rptr + 2;
    arm();
    assert(queue.hang_detect_progress == rptr);
    jiffies = 110;
    rptr++;
    fire();
    assert_no_reset();
    assert(queue.hang_detect_progress == rptr && queue.hang_detect_work.expires == 120);
    jiffies = 120;
    rptr++;
    fire();
    assert_no_reset();
    assert(!queue.hang_detect_armed && !queue.hang_detect_work.pending);
    assert(driver.fences.empty && processed == 2);
}
static void firmware_unmapped(void)
{
    fixture();
    arm();
    assert(!amdgpu_userq_unmap_helper(&queue));
    amdgpu_userq_process_reset_irq(&adev, vm.pasid, queue.doorbell_offset);
    assert(queue.hang_detect_reset_requested && queue.hang_detect_work.pending);
    fire();
    assert(queue.state == AMDGPU_USERQ_STATE_CANCELED && queue.hang_detect_stopped);
    assert(!queue_resets && !compute_resets && !full_resets && forced == 1 && !wedges);
    assert(!amdgpu_userq_map_helper(&queue) && !maps);
    amdgpu_userq_request_reset(&queue);
    assert(!queue.hang_detect_work.pending);
}
static void firmware_preempted(void)
{
    fixture();
    assert(!amdgpu_userq_preempt_helper(&queue));
    driver.fences.empty = true;
    amdgpu_userq_process_reset_irq(&adev, vm.pasid, queue.doorbell_offset);
    assert(queue.hang_detect_paused && queue.hang_detect_reset_requested);
    fire();
    assert(queue_resets == 1 && forced == 1);
}
static void already_resetting(void)
{
    fixture();
    device_resetting = true;
    amdgpu_userq_process_reset_irq(&adev, vm.pasid, queue.doorbell_offset);
    assert(!queue.hang_detect_reset_requested && !queue.hang_detect_work.pending);
    arm();
    jiffies = 110;
    fire();
    assert_no_reset();
}
static void failure_requests_recovery(void)
{
    for (unsigned stage = 0; stage < 3; stage++) {
        fixture();
        arm();
        if (stage == 0) {
            unmap_error = -EIO;
            assert(amdgpu_userq_suspend(&adev) == -EIO);
        } else if (stage == 1) {
            preempt_error = -EIO;
            assert(amdgpu_userq_preempt_helper(&queue) == -EIO);
        } else {
            assert(!amdgpu_userq_preempt_helper(&queue));
            restore_error = -EIO;
            assert(amdgpu_userq_restore_helper(&queue) == -EIO);
        }
        assert(queue.state == AMDGPU_USERQ_STATE_HUNG && queue.hang_detect_paused);
        assert(queue.hang_detect_reset_requested && queue.hang_detect_work.pending);
        jiffies = 200;
        fire();
        assert(queue_resets == 1 && forced == 1);
    }
}


static void *blocked_reset_request(void *data)
{
    block_before_lock = true;
    amdgpu_userq_request_reset(&queue);
    assert(!spin_depth && !mutex_depth);
    return NULL;
}
static void reset_request_vs_pre_reset(void)
{
    pthread_t thread;
    fixture();
    arm();
    race_begin(&thread, blocked_reset_request);
    device_resetting = true;
    amdgpu_userq_pre_reset(&adev);
    assert(!queue.hang_detect_reset_requested && !queue.hang_detect_work.pending);
    race_end(thread);
    assert(!queue.hang_detect_reset_requested && !queue.hang_detect_work.pending);
    assert(queue.state == AMDGPU_USERQ_STATE_HUNG && forced == 1);
    device_resetting = false;
    fire();
    assert(!queue_resets && !compute_resets && !full_resets && forced == 1);
}
static void restore_invalid_mapping(void)
{
    const enum amdgpu_userq_state states[] = {
        AMDGPU_USERQ_STATE_UNMAPPED, AMDGPU_USERQ_STATE_PREEMPTED,
        AMDGPU_USERQ_STATE_MAPPED, AMDGPU_USERQ_STATE_HUNG,
        AMDGPU_USERQ_STATE_CANCELED,
    };
    for (unsigned i = 0; i < sizeof(states) / sizeof(*states); i++) {
        fixture();
        queue.state = states[i];
        mappings_valid = false;
        assert(!amdgpu_userq_restore_all(&mgr));
        if (states[i] == AMDGPU_USERQ_STATE_HUNG || states[i] == AMDGPU_USERQ_STATE_CANCELED)
            assert(queue.state == states[i]);
        else
            assert(queue.state == AMDGPU_USERQ_STATE_INVALID_VA && queue.hang_detect_paused);
        assert(!maps && !queue.hang_detect_work.pending);
        assert(!mutex_depth && !userq_mutex_held);
    }
}
static void *blocked_restore_all(void *data)
{
    block_after_validation = true;
    assert(!amdgpu_userq_restore_all(&mgr));
    assert(!spin_depth && !mutex_depth);
    return NULL;
}
static void restore_vs_worker(void)
{
    for (unsigned cancel = 0; cancel < 2; cancel++) {
        pthread_t thread;
        fixture();
        if (cancel) {
            queue.state = AMDGPU_USERQ_STATE_UNMAPPED;
            queue.hang_detect_paused = true;
        } else {
            arm();
            jiffies = 110;
        }
        mappings_valid = false;
        race_begin(&thread, blocked_restore_all);
        if (cancel)
            amdgpu_userq_request_reset(&queue);
        fire();
        enum amdgpu_userq_state expected = cancel ? AMDGPU_USERQ_STATE_CANCELED : AMDGPU_USERQ_STATE_HUNG;
        assert(queue.state == expected && queue.hang_detect_stopped);
        race_end(thread);
        assert(queue.state == expected && !maps);
        assert(!mutex_depth && !userq_mutex_held);
    }
}

struct test { const char *name; void (*run)(void); };
static const struct test tests[] = {
    {"reset_request_vs_pre_reset", reset_request_vs_pre_reset},
    {"restore_invalid_mapping", restore_invalid_mapping},
    {"restore_vs_worker", restore_vs_worker},

    {"zero_deadline", zero_deadline},
    {"progress_64bit", progress_64bit},
    {"firmware_unmapped", firmware_unmapped},
    {"firmware_preempted", firmware_preempted},
    {"already_resetting", already_resetting},
    {"failure_requests_recovery", failure_requests_recovery},

    {"timeout_types", timeout_types},
    {"no_pending", no_pending},
    {"completed_without_irq", completed_without_irq},
    {"progress_without_irq", progress_without_irq},
    {"repeated_submissions", repeated_submissions},
    {"early_callback", early_callback},
    {"wrap_deadline", wrap_deadline},
    {"pause_unmap", pause_unmap},
    {"pause_preempt", pause_preempt},
    {"resume_completed", resume_completed},
    {"failed_initial_map", failed_initial_map},
    {"failed_pending_map", failed_pending_map},
    {"progress_irq", progress_irq},
    {"completion_irq", completion_irq},
    {"unrelated_irq", unrelated_irq},
    {"firmware_reset", firmware_reset},
    {"reset_fallback", reset_fallback},
    {"reset_disabled", reset_disabled},
    {"stopped", stopped},
    {"stopped_cannot_resume", stopped_cannot_resume},
    {"worker_vs_pause", worker_vs_pause},
    {"arm_vs_stop", arm_vs_stop},
    {"worker_vs_resume", worker_vs_resume},
};
int main(int argc, char **argv)
{
    assert(argc == 2);
    for (unsigned i = 0; i < sizeof(tests) / sizeof(*tests); i++) {
        if (!strcmp(argv[1], tests[i].name)) {
            tests[i].run();
            printf("UserQ watchdog: %s passed\n", tests[i].name);
            return 0;
        }
    }
    return 2;
}

'''

if __name__ == '__main__':
    unittest.main()
