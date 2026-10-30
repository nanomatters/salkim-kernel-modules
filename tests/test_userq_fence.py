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


class UserqFenceTests(unittest.TestCase):
    def test_implicit_ownership_and_explicit_waits(self):
        source = (AMD / 'amdgpu_userq_fence.c').read_text()
        names = ('to_amdgpu_userq_fence', 'amdgpu_userq_fence_driver_alloc',
                 'amdgpu_userq_fence_is_same_client', 'amdgpu_userq_wait_count_fences',
                 'amdgpu_userq_wait_add_fence', 'amdgpu_userq_wait_return_fence_info')
        functions = []
        for name in names:
            match = re.search(r'^(?:static (?:inline )?)?(?:int|bool|struct \w+ \*)\s*'
                              + name + r'\(.*?^\}', source, re.M | re.S)
            self.assertIsNotNone(match, name)
            functions.append(match.group())
        header = (AMD / 'amdgpu_userq_fence.h').read_text()
        structures = '\n'.join(re.findall(
            r'^struct amdgpu_userq_fence(?:_driver)? \{.*?^\};', header, re.M | re.S))
        flags = shlex.split(os.environ.get('CC', 'cc'))
        flags += ['-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                  '-Wno-unused-parameter', '-Wno-sign-compare',
                  '-I' + str(ROOT / 'amdgpu/include/uapi/drm')]
        flags += shlex.split(subprocess.check_output(
            ['pkg-config', '--cflags', 'libdrm'], text=True))
        if os.environ.get('USERQ_TEST_SANITIZERS'):
            flags += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                      '-fno-sanitize-recover=all']
        with tempfile.TemporaryDirectory(prefix='userq-fence-test-') as temporary:
            directory = pathlib.Path(temporary)
            program, binary = directory / 'test.c', directory / 'test'
            program.write_text(HARNESS.replace('ACTUAL_STRUCTURES', structures)
                               .replace('ACTUAL_FUNCTIONS', '\n'.join(functions)))
            subprocess.run(flags + [str(program), '-o', str(binary)], check=True, timeout=30)
            subprocess.run([str(binary)], check=True, timeout=30)


HARNESS = r'''
#include <assert.h>
#include <errno.h>
#include <limits.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "amdgpu_drm.h"
typedef uint32_t u32;
typedef uint64_t u64;
typedef int spinlock_t;
struct kref { unsigned refs; };
struct list_head { void *next, *prev; };
struct dma_fence_ops { int unused; };
struct dma_fence {
    const struct dma_fence_ops *ops;
    u64 seqno;
    unsigned refs, waited;
    int error;
};
#define TASK_COMM_LEN 16
ACTUAL_STRUCTURES
struct amdgpu_device { int unused; };
struct amdgpu_userq_mgr { int unused; };
struct amdgpu_fpriv { struct amdgpu_userq_mgr userq_mgr; };
struct drm_file { u64 client_id; struct amdgpu_fpriv *driver_priv; };
struct drm_device { void *dev; };
struct dma_resv { struct dma_fence *fences[8]; unsigned count; };
struct dma_resv_iter { unsigned index; struct dma_resv *resv; };
struct dma_fence_unwrap { struct dma_fence *next; };
struct drm_gem_object { struct dma_resv *resv; };
struct drm_exec { int unused; };
struct xarray { struct amdgpu_userq_fence_driver *entries[8]; unsigned count; };
struct amdgpu_usermode_queue { int fence_drv_lock; struct xarray fence_drv_xa; unsigned refs; };
static struct amdgpu_usermode_queue queue;
static const struct dma_fence_ops amdgpu_userq_fence_ops, kernel_ops, wrapper_ops;
static struct dma_fence *syncobjs[2];
static bool locked;
static unsigned cases;
static u64 seq64_word;
#define GFP_KERNEL 0
#define DRM_EXEC_INTERRUPTIBLE_WAIT 1
#define DRM_EXEC_IGNORE_DUPLICATES 2
#define DMA_RESV_USAGE_READ 2
#define DMA_RESV_USAGE_WRITE 1
#define container_of(p, t, m) ((t *)((char *)(p) - offsetof(t, m)))
#define min(a, b) ((a) < (b) ? (a) : (b))
#define kzalloc_obj(obj) calloc(1, sizeof(obj))
#define kfree free
#define INIT_LIST_HEAD(p) ((void)(p))
#define spin_lock_init(p) (*(p) = 0)
#define get_task_comm(dst, task) strcpy(dst, "test")
#define drm_exec_until_all_locked(e) for (bool once = true; once; once = false)
#define drm_exec_retry_on_contention(e) ((void)0)
#define dma_resv_for_each_fence(it, r, usage, f) \
    for ((it)->resv = (r), (it)->index = 0; \
         (it)->index < (it)->resv->count && ((f) = (it)->resv->fences[(it)->index]); (it)->index++)
#define dma_fence_unwrap_for_each(f, it, root) \
    for ((it)->next = (root); ((f) = (it)->next); (it)->next = NULL)
#define trace_amdgpu_userq_wait_deps(dev, q, f) ((void)0)
#define xa_limit_32b 0
#define u64_to_user_ptr(p) ((void *)(uintptr_t)(p))
static void kref_init(struct kref *ref) { ref->refs = 1; }
static u64 dma_fence_context_alloc(unsigned count) { static u64 context; return context += count; }
static int amdgpu_seq64_alloc(struct amdgpu_device *dev, u64 *va, u64 *gpu, u64 **cpu)
{ *va = 0x1000; *gpu = 0x2000; *cpu = &seq64_word; return 0; }
static void *kmalloc_array(size_t count, size_t size, int flags)
{ void *p = calloc(count ? count : 1, size); assert(p); return p; }
static struct dma_fence *dma_fence_get(struct dma_fence *f) { assert(f->refs); f->refs++; return f; }
static void dma_fence_put(struct dma_fence *f) { assert(f->refs > 1); f->refs--; }
static int dma_fence_wait(struct dma_fence *f, bool interruptible)
{ assert(interruptible); f->waited++; return f->error; }
static int drm_syncobj_find_fence(struct drm_file *file, u32 handle, u64 point,
                                 u64 flags, struct dma_fence **f)
{
    assert(!locked && handle < 2 && flags == DRM_SYNCOBJ_WAIT_FLAGS_WAIT_FOR_SUBMIT);
    assert(point == (handle ? UINT64_C(0x100000001) : 0));
    *f = dma_fence_get(syncobjs[handle]);
    return 0;
}
static void drm_exec_init(struct drm_exec *exec, unsigned flags, unsigned count)
{ assert(!locked); locked = true; }
static int drm_exec_prepare_array(struct drm_exec *exec, struct drm_gem_object **objs,
                                  unsigned count, unsigned fences)
{ assert(locked && fences == 1); return 0; }
static void drm_exec_fini(struct drm_exec *exec) { assert(locked); locked = false; }
static unsigned dma_fence_dedup_array(struct dma_fence **fences, unsigned count)
{
    unsigned n = 0;
    for (unsigned i = 0; i < count; i++) {
        unsigned j;
        for (j = 0; j < n && fences[j] != fences[i]; j++);
        if (j == n) fences[n++] = fences[i];
        else dma_fence_put(fences[i]);
    }
    return n;
}
static struct amdgpu_usermode_queue *amdgpu_userq_get(struct amdgpu_userq_mgr *mgr, u32 id)
{ assert(!locked && id == 7); queue.refs++; return &queue; }
static void amdgpu_userq_put(struct amdgpu_usermode_queue *q) { assert(q->refs); q->refs--; }
static void mutex_lock(int *m) { assert(!*m); *m = 1; }
static void mutex_unlock(int *m) { assert(*m); *m = 0; }
static int xa_alloc(struct xarray *xa, u32 *index, struct amdgpu_userq_fence_driver *drv,
                    unsigned limit, int flags)
{
    assert(queue.fence_drv_lock && xa->count < 8);
    *index = xa->count;
    xa->entries[xa->count++] = drv;
    return 0;
}
static void amdgpu_userq_fence_driver_get(struct amdgpu_userq_fence_driver *drv)
{ drv->refcount.refs++; }
static int copy_to_user(void *dst, const void *src, size_t size)
{ assert(!locked); memcpy(dst, src, size); return 0; }
ACTUAL_FUNCTIONS

static void check_wait(struct drm_file *file, struct dma_fence *f, unsigned transport,
                       bool write_list, unsigned capacity, unsigned expected_count,
                       unsigned expected_gpu, unsigned expected_cpu, int expected_error)
{
    struct dma_resv resv = {.fences = {f}, .count = 1};
    struct drm_gem_object bo = {.resv = &resv}, *bos[] = {&bo};
    struct drm_device dev = {0};
    struct drm_amdgpu_userq_fence_info out[8] = {{0}};
    u32 handle = transport == 2;
    u64 point = UINT64_C(0x100000001);
    syncobjs[handle] = f;
    struct drm_amdgpu_userq_wait wait = {
        .waitq_id = 7, .num_bo_read_handles = !write_list, .num_bo_write_handles = write_list,
        .num_syncobj_handles = transport == 1, .num_syncobj_timeline_handles = transport == 2,
        .out_fences = (uintptr_t)out,
    };
    assert(amdgpu_userq_wait_count_fences(file, &wait, &handle, &point, &handle, bos, bos) == 0);
    assert(wait.num_fences == expected_count && !locked && f->refs == 1);
    wait.num_fences = capacity;
    f->waited = 0;
    assert(amdgpu_userq_wait_return_fence_info(&dev, file, &wait, &handle, &point,
                                               &handle, bos, bos) == expected_error);
    assert(f->waited == expected_cpu && f->refs == 1 && !locked && !queue.refs);
    if (!expected_error) {
        assert(wait.num_fences == expected_gpu);
        if (expected_gpu) {
            struct amdgpu_userq_fence *uq = to_amdgpu_userq_fence(f);
            assert(expected_gpu == 1 && uq);
            assert(out[0].va == uq->fence_drv->va && out[0].value == f->seqno);
        }
    }
    for (unsigned i = 0; i < queue.fence_drv_xa.count; i++) {
        struct amdgpu_userq_fence_driver *drv = queue.fence_drv_xa.entries[i];
        assert(drv->refcount.refs > 1);
        drv->refcount.refs--;
    }
    queue.fence_drv_xa.count = 0;
    cases++;
}

int main(void)
{
    struct amdgpu_device dev = {0};
    struct amdgpu_fpriv fpriv = {0};
    struct drm_file file = {.client_id = UINT64_C(0x100000007), .driver_priv = &fpriv};
    struct amdgpu_userq_fence_driver *own, *other;
    assert(amdgpu_userq_fence_driver_alloc(&dev, file.client_id, &own) == 0);
    assert(amdgpu_userq_fence_driver_alloc(&dev, UINT64_C(0x200000007), &other) == 0);
    assert(own->client_id == file.client_id && other->client_id != file.client_id);
    assert(own->adev == &dev && own->context != other->context);
    struct amdgpu_userq_fence local = {
        .base = {.ops = &amdgpu_userq_fence_ops, .seqno = 11, .refs = 1}, .fence_drv = own,
    };
    struct amdgpu_userq_fence foreign = {
        .base = {.ops = &amdgpu_userq_fence_ops, .seqno = 22, .refs = 1}, .fence_drv = other,
    };
    struct dma_fence kernel = {.ops = &kernel_ops, .refs = 1};
    struct dma_fence wrapped = {.ops = &wrapper_ops, .refs = 1};

    for (unsigned write = 0; write < 2; write++) {
        /* Both BO lists skip local UserQ fences, including when there is no output space. */
        check_wait(&file, &local.base, 0, write, 8, 0, 0, 0, 0);
        local.base.error = -EINTR;
        check_wait(&file, &local.base, 0, write, 0, 0, 0, 0, 0);
        local.base.error = 0;
        /* Explicit binary and timeline waits survive even with the same fence on a BO. */
        for (unsigned transport = 1; transport <= 2; transport++) {
            check_wait(&file, &local.base, transport, write, 8, 1, 1, 0, 0);
            check_wait(&file, &local.base, transport, write, 0, 1, 0, 1, 0);
            local.base.error = -EINTR;
            check_wait(&file, &local.base, transport, write, 0, 1, 0, 1, -EINTR);
            local.base.error = 0;
        }
        /* Other DRM clients still synchronize, even when the low 32 bits of the ID match. */
        check_wait(&file, &foreign.base, 0, write, 8, 1, 1, 0, 0);
        check_wait(&file, &foreign.base, 0, write, 0, 1, 0, 1, 0);
        foreign.base.error = -EINTR;
        check_wait(&file, &foreign.base, 0, write, 0, 1, 0, 1, -EINTR);
        foreign.base.error = 0;
        /* Kernel maintenance and unrecognized compound fences are retained conservatively. */
        check_wait(&file, &kernel, 0, write, 8, 1, 0, 1, 0);
        check_wait(&file, &wrapped, 0, write, 8, 1, 0, 1, 0);
    }
    /* A skipped local fence must not consume the only slot needed by a foreign fence. */
    for (unsigned write = 0; write < 2; write++) {
        struct dma_resv resv = {.fences = {&local.base, &foreign.base}, .count = 2};
        struct drm_gem_object bo = {.resv = &resv}, *bos[] = {&bo};
        struct drm_device drm = {0};
        struct drm_amdgpu_userq_fence_info out = {0};
        struct drm_amdgpu_userq_wait wait = {
            .waitq_id = 7, .num_bo_read_handles = !write, .num_bo_write_handles = write,
            .num_fences = 1, .out_fences = (uintptr_t)&out,
        };
        local.base.waited = foreign.base.waited = 0;
        assert(amdgpu_userq_wait_count_fences(&file, &wait, NULL, NULL, NULL, bos, bos) == 0);
        assert(wait.num_fences == 1);
        assert(amdgpu_userq_wait_return_fence_info(&drm, &file, &wait, NULL, NULL,
                                                   NULL, bos, bos) == 0);
        assert(wait.num_fences == 1 && out.va == other->va && out.value == foreign.base.seqno);
        assert(!local.base.waited && !foreign.base.waited && !locked && !queue.refs);
        assert(local.base.refs == 1 && foreign.base.refs == 1);
        assert(queue.fence_drv_xa.count == 1 && queue.fence_drv_xa.entries[0] == other);
        assert(other->refcount.refs == 2);
        other->refcount.refs--;
        queue.fence_drv_xa.count = 0;
        cases++;
    }
    /* An exported fence keeps its original identity after the DRM file is replaced. */
    file.client_id = UINT64_C(0x300000007);
    check_wait(&file, &local.base, 0, false, 8, 1, 1, 0, 0);
    assert(own->refcount.refs == 1 && other->refcount.refs == 1);
    free(own);
    free(other);
    printf("UserQ fence ownership: %u cases passed\n", cases);
    return 0;
}
'''


if __name__ == '__main__':
    unittest.main()
