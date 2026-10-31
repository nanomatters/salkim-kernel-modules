#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
import os
import pathlib
import re
import shlex
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
AMD = ROOT / 'amdgpu/drivers/gpu/drm/amd/amdgpu'


class UserqTests(unittest.TestCase):
    def test_wptr_fence_reservation(self):
        source = (AMD / 'mes_userqueue.c').read_text()
        body = re.search(r'^mes_userq_create_wptr_mapping\(.*?^\}', source, re.M | re.S).group()
        prepare = re.search(r'drm_exec_prepare_obj\(&exec,\s*&obj->tbo.base,\s*'
                            r'TTM_NUM_MOVE_FENCES \+ 1\)', body)
        self.assertIsNotNone(prepare)
        self.assertLess(prepare.start(), body.index('amdgpu_evf_mgr_attach_fence('))

    def test_restore_fence_reservation(self):
        source = (AMD / 'amdgpu_userq.c').read_text()
        body = re.search(r'^amdgpu_userq_vm_validate_and_restore_queue\(.*?^\}',
                         source, re.M | re.S).group()
        prepare = re.search(r'amdgpu_vm_lock_pd\(vm,\s*&exec,\s*'
                            r'TTM_NUM_MOVE_FENCES \+ 1\)', body)
        self.assertIsNotNone(prepare)
        self.assertLess(prepare.start(), body.index('amdgpu_vm_validate('))
        self.assertLess(prepare.start(), body.index('amdgpu_evf_mgr_rearm('))

    def test_interface_version(self):
        source = (AMD / 'amdgpu_drv.c').read_text()
        values = [int(re.search(r'^#define KMS_DRIVER_' + name + r'\s+(\d+)$',
                                source, re.M).group(1))
                  for name in ('MAJOR', 'MINOR', 'PATCHLEVEL')]
        self.assertGreaterEqual(tuple(values), (3, 65, 0))

    def test_wait_points_are_64_bit(self):
        source = (AMD / 'amdgpu_userq_fence.c').read_text()
        for name in ('amdgpu_userq_wait_count_fences', 'amdgpu_userq_wait_return_fence_info'):
            signature = re.search(r'^' + name + r'\([^\{]+\{', source, re.M).group()
            self.assertRegex(signature, r'u64 \*timeline_points')
        body = source.split('int amdgpu_userq_wait_ioctl(', 1)[1]
        self.assertRegex(body, r'u64 \*timeline_points;')
        self.assertRegex(body, r'timeline_points = memdup_array_user\(ptr, num_points, sizeof\(u64\)\)')

    def test_signal_and_cleanup(self):
        source = (AMD / 'amdgpu_userq_fence.c').read_text()
        function = re.search(r'^int amdgpu_userq_signal_ioctl\(.*?^\}', source, re.M | re.S)
        self.assertIsNotNone(function)
        flags = shlex.split(os.environ.get('CC', 'cc'))
        flags += ['-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                  '-Wno-unused-parameter', '-Wno-sign-compare',
                  '-I' + str(ROOT / 'amdgpu/include/uapi/drm')]
        flags += shlex.split(subprocess.check_output(
            ['pkg-config', '--cflags', 'libdrm'], text=True))
        if os.environ.get('USERQ_TEST_SANITIZERS'):
            flags += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer']
        with tempfile.TemporaryDirectory() as temporary:
            directory = pathlib.Path(temporary)
            program = directory / 'userq.c'
            binary = directory / 'userq'
            program.write_text(HARNESS.replace('ACTUAL_SIGNAL_FUNCTION', function.group()))
            subprocess.run(flags + [str(program), '-o', str(binary)], check=True, timeout=30)
            subprocess.run([str(binary)], check=True, timeout=30)


HARNESS = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "amdgpu_drm.h"

typedef uint32_t u32;
typedef uint64_t u64;
#define __user
#define ENOTSUPP 524
#define GFP_KERNEL 0
#define DRM_EXEC_INTERRUPTIBLE_WAIT 1
#define DRM_EXEC_IGNORE_DUPLICATES 2
#define DMA_RESV_USAGE_READ 0
#define DMA_RESV_USAGE_WRITE 1
#define AMDGPU_USERQ_MAX_HANDLES (1U << 16)
#define AMDGPU_USERQ_STATE_UNMAPPED 0
#define AMDGPU_USERQ_STATE_HUNG 3
#define AMDGPU_USERQ_STATE_INVALID_VA 4
#define AMDGPU_USERQ_STATE_CANCELED 5
#define READ_ONCE(value) (value)
#define ZERO_SIZE_PTR ((void *)16)
#define ERR_PTR(value) ((void *)(intptr_t)(value))
#define IS_ERR(value) ((uintptr_t)(value) >= (uintptr_t)-4095)
#define PTR_ERR(value) ((intptr_t)(value))
#define u64_to_user_ptr(value) ((void *)(uintptr_t)(value))
#define drm_exec_until_all_locked(exec) for (bool once = true; once; once = false)
#define drm_exec_retry_on_contention(exec) ((void)0)
#define trace_amdgpu_userq_emit_fence(dev, queue, fence) ((void)0)

struct drm_device { void *dev; };
struct amdgpu_device { int unused; };
struct amdgpu_userq_mgr { int userq_mutex; };
struct amdgpu_fpriv { struct amdgpu_userq_mgr userq_mgr; int evf_mgr; };
struct drm_file { struct amdgpu_fpriv *driver_priv; };
struct drm_gem_object { void *resv; };
struct drm_exec { int unused; };
struct dma_fence { unsigned refs; };
struct dma_fence_chain { int unused; };
struct amdgpu_userq_fence { struct dma_fence base; };
struct drm_syncobj { unsigned refs; };
struct amdgpu_usermode_queue {
    unsigned refs, state;
    int state_mutex;
    bool hang_detect_stopped;
};

enum failure {
    NONE, DISABLED, READ_LOOKUP, WRITE_LOOKUP, QUEUE_LOOKUP,
    WPTR_READ, FENCE_ALLOC, READ_PREPARE, WRITE_PREPARE,
    QUEUE_HUNG, QUEUE_CANCELED, QUEUE_INVALID_VA, QUEUE_STOPPED,
};
static enum failure failure;
static unsigned alloc_count, fail_alloc, live_allocs, published, chains_owned;
static unsigned failed_handle, lookup_count, prepare_count, gem_refs;
static unsigned signal_count, binary_count, cases;
static const void *bad_pointer;
static struct drm_syncobj syncobjs[4];
static struct drm_gem_object objects[4];
static struct dma_fence_chain *owned_chains[4];
static u64 signalled_points[4];
static struct amdgpu_usermode_queue queue;
static struct amdgpu_userq_fence fence;
static struct amdgpu_device device;

static void *allocate(size_t size)
{
    if (!size)
        return ZERO_SIZE_PTR;
    if (++alloc_count == fail_alloc)
        return NULL;
    void *ptr = malloc(size);
    assert(ptr);
    ++live_allocs;
    return ptr;
}
static void kfree(void *ptr)
{
    if (ptr && ptr != ZERO_SIZE_PTR) {
        assert(!IS_ERR(ptr) && live_allocs);
        --live_allocs;
        free(ptr);
    }
}
#define kvfree kfree
static void *memdup_array_user(const void *ptr, size_t count, size_t size)
{
    if (count && (!ptr || ptr == bad_pointer))
        return ERR_PTR(-EFAULT);
    void *copy = allocate(count * size);
    if (!copy)
        return ERR_PTR(-ENOMEM);
    if (count)
        memcpy(copy, ptr, count * size);
    return copy;
}
static void *kmalloc_array(size_t count, size_t size, int flags)
{
    return allocate(count * size);
}
static struct dma_fence_chain *dma_fence_chain_alloc(void)
{
    return allocate(sizeof(struct dma_fence_chain));
}
#define dma_fence_chain_free kfree
static struct amdgpu_device *drm_to_adev(struct drm_device *dev) { return &device; }
static bool amdgpu_userq_enabled(struct drm_device *dev) { return failure != DISABLED; }
static struct drm_syncobj *drm_syncobj_find(struct drm_file *file, u32 handle)
{
    assert(handle > 0 && handle < 4);
    if (handle == failed_handle)
        return NULL;
    ++syncobjs[handle].refs;
    return &syncobjs[handle];
}
static void drm_syncobj_put(struct drm_syncobj *obj)
{
    assert(obj->refs);
    --obj->refs;
}
static void drm_syncobj_replace_fence(struct drm_syncobj *obj, struct dma_fence *f)
{
    ++binary_count;
    ++f->refs;
    ++published;
}
static void drm_syncobj_add_point(struct drm_syncobj *obj, struct dma_fence_chain *chain,
                                  struct dma_fence *f, u64 point)
{
    assert(chain && point && chains_owned < 4 && signal_count < 4);
    owned_chains[chains_owned++] = chain;
    signalled_points[signal_count++] = point;
    ++f->refs;
    ++published;
}
static int drm_gem_objects_lookup(struct drm_file *file, const void *ptr,
                                   unsigned count, struct drm_gem_object ***out)
{
    ++lookup_count;
    if ((failure == READ_LOOKUP && lookup_count == 1) ||
        (failure == WRITE_LOOKUP && lookup_count == 2))
        return -EFAULT;
    *out = allocate(count * sizeof(**out));
    if (!*out)
        return -ENOMEM;
    for (unsigned i = 0; i < count; ++i) {
        assert(i < 4);
        (*out)[i] = &objects[i];
        ++gem_refs;
    }
    return 0;
}
static void drm_gem_object_put(struct drm_gem_object *obj)
{
    assert(gem_refs);
    --gem_refs;
}
static struct amdgpu_usermode_queue *amdgpu_userq_get(struct amdgpu_userq_mgr *mgr, u32 id)
{
    if (failure == QUEUE_LOOKUP)
        return NULL;
    ++queue.refs;
    return &queue;
}
static void amdgpu_userq_put(struct amdgpu_usermode_queue *q)
{
    assert(q->refs);
    --q->refs;
}
static int amdgpu_userq_fence_read_wptr(struct amdgpu_device *dev,
                                       struct amdgpu_usermode_queue *q, u64 *wptr)
{
    *wptr = (1ULL << 40) + 7;
    return failure == WPTR_READ ? -EIO : 0;
}
static int amdgpu_userq_fence_alloc(struct amdgpu_usermode_queue *q,
                                    struct amdgpu_userq_fence **out)
{
    assert(q->state_mutex && !q->hang_detect_stopped);
    assert(q->state != AMDGPU_USERQ_STATE_HUNG &&
           q->state != AMDGPU_USERQ_STATE_CANCELED &&
           q->state != AMDGPU_USERQ_STATE_INVALID_VA);
    if (failure == FENCE_ALLOC)
        return -ENOMEM;
    fence.base.refs = 1;
    *out = &fence;
    return 0;
}
static void amdgpu_userq_ensure_ev_fence(struct amdgpu_userq_mgr *mgr, int *ev)
{ assert(!mgr->userq_mutex); mgr->userq_mutex = 1; }
static void amdgpu_userq_fence_init(struct amdgpu_usermode_queue *q,
                                    struct amdgpu_userq_fence *f, u64 wptr)
{
    assert(wptr == (1ULL << 40) + 7 && q->state_mutex);
}
static void dma_fence_put(struct dma_fence *f)
{
    assert(f->refs);
    --f->refs;
}
static void mutex_lock(int *mutex) { assert(!*mutex); *mutex = 1; }
static void mutex_unlock(int *mutex) { assert(*mutex); *mutex = 0; }
static void drm_exec_init(struct drm_exec *exec, unsigned flags, unsigned count) {}
static void drm_exec_fini(struct drm_exec *exec) {}
static int drm_exec_prepare_array(struct drm_exec *exec, struct drm_gem_object **objs,
                                   unsigned count, unsigned slots)
{
    ++prepare_count;
    return ((failure == READ_PREPARE && prepare_count == 1) ||
            (failure == WRITE_PREPARE && prepare_count == 2)) ? -EINTR : 0;
}
static void dma_resv_add_fence(void *resv, struct dma_fence *f, unsigned usage)
{
    ++f->refs;
    ++published;
}

ACTUAL_SIGNAL_FUNCTION

struct legacy_signal {
    __u32 queue_id, pad;
    __u64 syncobj_handles;
    __u16 num_syncobj_handles, pad0;
    __u32 pad1;
    __u64 bo_read_handles, bo_write_handles;
    __u32 num_bo_read_handles, num_bo_write_handles;
};
_Static_assert(sizeof(struct legacy_signal) == 48, "legacy signal size");
_Static_assert(offsetof(struct drm_amdgpu_userq_signal, syncobj_points) == 48, "appended field");
_Static_assert(sizeof(struct drm_amdgpu_userq_signal) == 56, "extended signal size");
_Static_assert(_IOC_SIZE(DRM_IOCTL_AMDGPU_USERQ_SIGNAL) == 56, "ioctl size");

static void run_case(unsigned count, unsigned mask, bool points_present, bool legacy,
                     enum failure fail, unsigned oom, unsigned invalid_handle,
                     unsigned invalid_pointer, bool excessive_bos, int expected)
{
    u32 handles[] = {1, 2, 3}, bo_handles[] = {1, 2};
    u64 points[3];
    for (unsigned i = 0; i < 3; ++i)
        points[i] = mask & (1U << i) ? (1ULL << (40 + i)) + 7 + i : 0;
    struct drm_amdgpu_userq_signal args = {
        .queue_id = 1, .syncobj_handles = (uintptr_t)handles,
        .num_syncobj_handles = count, .syncobj_points = points_present ? (uintptr_t)points : 0,
        .bo_read_handles = (uintptr_t)bo_handles, .bo_write_handles = (uintptr_t)bo_handles,
        .num_bo_read_handles = excessive_bos ? AMDGPU_USERQ_MAX_HANDLES + 1 : 1,
        .num_bo_write_handles = 1,
    };
    if (legacy) {
        struct legacy_signal old;
        memcpy(&old, &args, sizeof(old));
        memset(&args, 0, sizeof(args));
        memcpy(&args, &old, sizeof(old));
        assert(!args.syncobj_points);
        points_present = false;
    }
    failure = fail;
    fail_alloc = oom;
    failed_handle = invalid_handle;
    bad_pointer = invalid_pointer == 1 ? (void *)handles :
                  invalid_pointer == 2 ? (void *)points : NULL;
    alloc_count = lookup_count = prepare_count = published = 0;
    signal_count = binary_count = chains_owned = gem_refs = live_allocs = 0;
    memset(syncobjs, 0, sizeof(syncobjs));
    memset(&fence, 0, sizeof(fence));
    memset(&queue, 0, sizeof(queue));
    queue.state = fail == QUEUE_HUNG ? AMDGPU_USERQ_STATE_HUNG :
                  fail == QUEUE_CANCELED ? AMDGPU_USERQ_STATE_CANCELED :
                  fail == QUEUE_INVALID_VA ? AMDGPU_USERQ_STATE_INVALID_VA :
                  fail == QUEUE_STOPPED ? AMDGPU_USERQ_STATE_UNMAPPED : 1;
    queue.hang_detect_stopped = fail == QUEUE_STOPPED;
    struct amdgpu_fpriv fpriv = {0};
    struct drm_file file = { .driver_priv = &fpriv };
    struct drm_device dev = {0};
    assert(amdgpu_userq_signal_ioctl(&dev, &args, &file) == expected);
    assert(!queue.refs && !gem_refs && !queue.state_mutex && !fpriv.userq_mgr.userq_mutex);
    assert(fence.base.refs == published);
    for (unsigned i = 1; i < 4; ++i)
        assert(!syncobjs[i].refs);
    if (!expected) {
        unsigned timeline = 0;
        for (unsigned i = 0; i < count; ++i) {
            if (points_present && points[i])
                assert(signalled_points[timeline++] == points[i]);
        }
        assert(signal_count == timeline && binary_count == count - timeline);
    } else {
        assert(!signal_count && !binary_count && !published);
    }
    assert(live_allocs == chains_owned);
    for (unsigned i = 0; i < chains_owned; ++i)
        dma_fence_chain_free(owned_chains[i]);
    while (published) {
        --published;
        dma_fence_put(&fence.base);
    }
    assert(!live_allocs && !fence.base.refs);
    ++cases;
}

int main(void)
{
    for (unsigned count = 0; count <= 3; ++count) {
        run_case(count, 0, false, false, NONE, 0, 0, 0, false, 0);
        run_case(count, 7, true, true, NONE, 0, 0, 0, false, 0);
        for (unsigned mask = 0; mask < 8; ++mask)
            run_case(count, mask, true, false, NONE, 0, 0, 0, false, 0);
    }
    run_case(3, 7, true, false, DISABLED, 0, 0, 0, false, -ENOTSUPP);
    run_case(3, 7, true, false, NONE, 0, 0, 0, true, -EINVAL);
    for (unsigned oom = 1; oom <= 8; ++oom)
        run_case(3, 7, true, false, NONE, oom, 0, 0, false, -ENOMEM);
    for (unsigned handle = 1; handle <= 3; ++handle)
        run_case(3, 7, true, false, NONE, 0, handle, 0, false, -ENOENT);
    for (unsigned pointer = 1; pointer <= 2; ++pointer)
        run_case(3, 7, true, false, NONE, 0, 0, pointer, false, -EFAULT);
    const int errors[] = {0, 0, -EFAULT, -EFAULT, -ENOENT, -EIO, -ENOMEM, -EINTR, -EINTR};
    for (enum failure fail = READ_LOOKUP; fail <= WRITE_PREPARE; ++fail)
        run_case(3, 7, true, false, fail, 0, 0, 0, false, errors[fail]);
    run_case(3, 7, true, false, QUEUE_HUNG, 0, 0, 0, false, -ECANCELED);
    run_case(3, 7, true, false, QUEUE_CANCELED, 0, 0, 0, false, -ECANCELED);
    run_case(3, 7, true, false, QUEUE_INVALID_VA, 0, 0, 0, false, -ECANCELED);
    run_case(3, 7, true, false, QUEUE_STOPPED, 0, 0, 0, false, -ECANCELED);
    printf("USERQ_SIGNAL: %u cases passed\n", cases);
    return 0;
}
'''


if __name__ == '__main__':
    unittest.main()
