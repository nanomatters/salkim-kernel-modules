#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
import os
import pathlib
import re
import shlex
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(os.environ.get('VA_BATCH_TEST_ROOT', pathlib.Path(__file__).resolve().parents[1]))
AMD = ROOT / 'amdgpu/drivers/gpu/drm/amd/amdgpu'


class VaBatchTests(unittest.TestCase):
    def test_request_layout(self):
        flags = shlex.split(os.environ.get('CC', 'cc'))
        flags += ['-std=gnu11', '-Werror', '-I' + str(ROOT / 'amdgpu/include/uapi/drm')]
        flags += shlex.split(subprocess.check_output(['pkg-config', '--cflags', 'libdrm'], text=True))
        with tempfile.TemporaryDirectory(prefix='va-batch-abi-') as temporary:
            directory = pathlib.Path(temporary)
            source = directory / 'abi.c'
            source.write_text(ABI)
            for bits in (32, 64):
                with self.subTest(bits=bits):
                    subprocess.run(flags + [f'-m{bits}', '-c', str(source), '-o',
                                             str(directory / f'abi{bits}.o')], check=True, timeout=30)

    def test_batch_contract(self):
        source = (AMD / 'amdgpu_gem.c').read_text()
        timeline = re.search(r'^static int\namdgpu_gem_update_timeline_node\(.*?^\}', source, re.M | re.S)
        self.assertIsNotNone(timeline)
        start = source.index('bool amdgpu_gem_va_batch_supported(')
        end = source.index('\nint amdgpu_gem_op_ioctl(', start)
        boundary = re.search(r'^static struct amdgpu_bo_va \*\n'
                             r'amdgpu_gem_va_batch_boundary\(.*?^\}', source, re.M | re.S)
        self.assertIsNotNone(boundary)
        body = source[start:end].replace(boundary.group(), '')
        vm = (AMD / 'amdgpu_vm.c').read_text()
        header = (AMD / 'amdgpu_vm.h').read_text()
        entry = re.search(r'^struct amdgpu_vm_batch_entry \{.*?^\};', header, re.M | re.S)
        self.assertIsNotNone(entry)
        begin = vm.index('struct amdgpu_vm_batch_state {')
        final = re.search(r'^static void amdgpu_vm_batch_fini\(.*?^\}', vm[begin:], re.M | re.S)
        self.assertIsNotNone(final)
        state = entry.group() + '\n' + vm[begin:begin + final.end()]
        functions = state + '\n' + timeline.group() + '\n' + boundary.group() + '\n' + body
        kms = (AMD / 'amdgpu_kms.c').read_text()
        probe = re.search(r'\tcase AMDGPU_INFO_GEM_VA_BATCH: \{.*?\n\t\}', kms, re.S)
        self.assertIsNotNone(probe)
        functions += ('\nstatic int probe_batch(struct drm_amdgpu_info *info, void *out, u32 size)\n{\n'
                      'struct amdgpu_device *adev = &device; struct drm_file *filp = &file;\n'
                      'struct amdgpu_fpriv *fpriv; switch (info->query) {\n'
                      + probe.group() + '\ndefault: return -EINVAL; }\n}\n')
        flags = shlex.split(os.environ.get('CC', 'cc'))
        flags += ['-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                  '-Wno-unused-parameter', '-Wno-unused-function', '-Wno-sign-compare', '-pthread',
                  '-I' + str(ROOT / 'amdgpu/include/uapi/drm')]
        flags += shlex.split(subprocess.check_output(['pkg-config', '--cflags', 'libdrm'], text=True))
        if os.environ.get('USERQ_TEST_SANITIZERS'):
            flags += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                      '-fno-sanitize-recover=all']
        with tempfile.TemporaryDirectory(prefix='va-batch-test-') as temporary:
            directory = pathlib.Path(temporary)
            program, binary = directory / 'test.c', directory / 'test'
            program.write_text(HARNESS.replace('ACTUAL_FUNCTIONS', functions))
            if os.environ.get('VA_BATCH_TEST_SAVE'):
                pathlib.Path(os.environ['VA_BATCH_TEST_SAVE']).write_text(program.read_text())
            subprocess.run(flags + [str(program), '-o', str(binary)], check=True, timeout=60)
            subprocess.run([str(binary)], check=True, timeout=60)


ABI = r'''
#include <stddef.h>
#include "amdgpu_drm.h"
_Static_assert(sizeof(struct drm_amdgpu_gem_va_batch) == 40, "batch ABI size");
_Static_assert(sizeof(struct drm_amdgpu_info) == 32, "INFO ABI size");
_Static_assert(offsetof(struct drm_amdgpu_info, gem_va_batch.flags) == 16, "INFO selector offset");
_Static_assert(offsetof(struct drm_amdgpu_gem_va_batch, operations) == 8, "operation pointer offset");
_Static_assert(offsetof(struct drm_amdgpu_gem_va_batch, vm_timeline_syncobj_out) == 24, "syncobj offset");
_Static_assert(offsetof(struct drm_amdgpu_gem_va_batch, vm_timeline_point) == 32, "timeline point offset");
_Static_assert(sizeof(struct drm_amdgpu_gem_va_batch_op) == 32, "operation ABI size");
'''


HARNESS = r'''
#include <assert.h>
#include <errno.h>
#include <limits.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdatomic.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "amdgpu_drm.h"

_Static_assert(sizeof(struct drm_amdgpu_gem_va_batch) == 40, "batch ABI size");
_Static_assert(sizeof(struct drm_amdgpu_info) == 32, "INFO ABI size");
_Static_assert(offsetof(struct drm_amdgpu_info, gem_va_batch.flags) == 16, "INFO selector offset");
_Static_assert(offsetof(struct drm_amdgpu_gem_va_batch, operations) == 8, "operation pointer offset");
_Static_assert(offsetof(struct drm_amdgpu_gem_va_batch, vm_timeline_syncobj_out) == 24, "syncobj offset");
_Static_assert(offsetof(struct drm_amdgpu_gem_va_batch, vm_timeline_point) == 32, "timeline point offset");
_Static_assert(sizeof(struct drm_amdgpu_gem_va_batch_op) == 32, "operation ABI size");

typedef uint32_t u32;
typedef uint64_t u64;
#define GFP_KERNEL 0
#define DRM_EXEC_INTERRUPTIBLE_WAIT 1
#define DRM_EXEC_IGNORE_DUPLICATES 2
#define DMA_RESV_USAGE_BOOKKEEP 4
#define AMDGPU_SYNC_EXPLICIT 0
#define AMDGPU_GPU_PAGE_SIZE 4096u
#define AMDGPU_VA_RESERVED_BOTTOM 4096u
#define AMDGPU_GMC_HOLE_START UINT64_C(0x0000800000000000)
#define AMDGPU_GMC_HOLE_END UINT64_C(0xffff800000000000)
#define AMDGPU_GMC_HOLE_MASK UINT64_C(0x0000ffffffffffff)
#define AMDGPU_VA_RESERVED_TOP 4096u
#define TTM_PL_SYSTEM 0
#define TTM_PL_TT 1
#define TTM_PL_VRAM 2
#define ARRAY_SIZE(x) (sizeof(x) / sizeof((x)[0]))
#define ERR_PTR(v) ((void *)(intptr_t)(v))
#define IS_ERR(v) ((uintptr_t)(v) >= (uintptr_t)-4095)
#define PTR_ERR(v) ((intptr_t)(v))
#define u64_to_user_ptr(v) ((void *)(uintptr_t)(v))
#define array_size(a, b) ((size_t)(a) * (b))
#define check_add_overflow(a, b, out) __builtin_add_overflow((a), (b), (out))
#define smp_load_acquire(p) __atomic_load_n(p, __ATOMIC_ACQUIRE)
#define smp_store_release(p, value) __atomic_store_n(p, value, __ATOMIC_RELEASE)
#define atomic64_read(p) (*(p))
#define container_of(p, t, member) ((t *)((char *)(p) - offsetof(t, member)))
#define INIT_LIST_HEAD(head) ((head)->next = (head)->prev = (head))
#define list_for_each_entry_safe(entry, tmp, head, member) \
    for (struct list_head *node = (head)->next, *next; \
         node != (head) && ((next = node->next), \
         (entry = container_of(node, typeof(*entry), member)), \
         (tmp = next != (head) ? container_of(next, typeof(*tmp), member) : NULL), true); node = next)
#define dma_resv_assert_held(resv) assert(locked)
#define min_t(t, a, b) ((t)(a) < (t)(b) ? (t)(a) : (t)(b))
#define min(a, b) ((a) < (b) ? (a) : (b))
#define dev_dbg_ratelimited(...) ((void)0)
#define drm_exec_until_all_locked(e) for (bool once = true; once; once = false)
#define drm_exec_retry_on_contention(e) ((void)0)

struct dma_fence { _Atomic unsigned refs; unsigned kind; bool signaled; int error; struct dma_fence *dependency; };
struct dma_fence_array { struct dma_fence base; unsigned num_fences, capacity; struct dma_fence **fences; };
struct dma_fence_chain { unsigned unused; };
struct dma_resv { struct dma_fence *fence; };
struct ttm_resource { unsigned mem_type; };
struct amdgpu_bo_va;
struct drm_gem_object { struct dma_resv *resv; struct amdgpu_bo_va *mapping; _Atomic unsigned refs; unsigned handle; };
struct amdgpu_bo { struct { struct drm_gem_object base; struct ttm_resource *resource; } tbo; bool always_valid; };
struct amdgpu_bo_va { struct { struct amdgpu_bo *bo; } base; struct dma_fence *last_pt_update; };
struct amdgpu_bo_va_mapping { u64 start, last; struct amdgpu_bo_va *bo_va; };
struct list_head { unsigned count; struct list_head *next, *prev; };
struct mutex { pthread_mutex_t value; };
struct amdgpu_vm_batch_state;
struct amdgpu_vm {
    bool use_cpu_for_update, is_compute_context, need_tlb_fence, ready;
    struct { struct amdgpu_bo *bo; } root;
    struct dma_fence *last_update, *last_tlb_inv;
    struct list_head freed;
    struct amdgpu_vm_batch_state *batch_state;
    u64 tlb_seq;
};
struct amdgpu_device { bool debug_vm, userq; void *dev; struct { u64 max_pfn; } vm_manager; };
struct drm_device { struct amdgpu_device *adev; };
struct amdgpu_fpriv { struct amdgpu_vm vm; struct amdgpu_bo_va *prt_va; };
struct drm_file { struct amdgpu_fpriv *driver_priv; };
struct drm_syncobj { _Atomic unsigned refs; u64 point; struct dma_fence *fence; struct dma_fence_chain *chain; };
struct drm_exec { bool locked; };
struct xarray { unsigned count; struct amdgpu_bo_va *entries[AMDGPU_GEM_VA_BATCH_MAX_OPS * 3]; };
#define DEFINE_XARRAY(name) struct xarray name = {0}
#define LIST_HEAD(name) struct list_head name = {0}
#define xa_for_each(xa, index, entry) \
    for ((index) = 0; (index) < (xa)->count && ((entry) = (xa)->entries[index]); (index)++)

static _Atomic unsigned allocation_count, live_allocations;
static unsigned fail_allocation;
static unsigned mutations, published, update_stage, cases, lookup_fail, lock_fail, syncobj_fail;
static unsigned preparation_allocations, bind_bos, locked_handles, updated_handles;
static struct amdgpu_bo_va_mapping old_mappings[4];
static bool become_unready;
static int stage_error;
static bool missing_mapping, mode_changed;
static _Thread_local bool locked;
static unsigned failure_stage, tlb_failure, dependency_stage;
static unsigned flush_trigger;
static const void *bad_pointer;
static struct amdgpu_device device;
static struct drm_device drm;
static struct amdgpu_fpriv priv;
static struct drm_file file;
static struct amdgpu_bo objects[4], root_bo;
static struct amdgpu_bo_va mappings[4], prt;
static struct ttm_resource resources[4];
static struct dma_resv reservations[5];
static struct dma_fence leaves[8];
static struct drm_syncobj syncobjs[2];
#define first_syncobj syncobjs[0]
static struct drm_amdgpu_gem_va_batch_op operations[AMDGPU_GEM_VA_BATCH_MAX_OPS];
static struct drm_amdgpu_gem_va_batch_op applied[AMDGPU_GEM_VA_BATCH_MAX_OPS * 2];
static pthread_mutex_t vm_mutex = PTHREAD_MUTEX_INITIALIZER;

static void mutex_init(struct mutex *m) { assert(!pthread_mutex_init(&m->value, NULL)); }
static void mutex_lock(struct mutex *m) { assert(!pthread_mutex_lock(&m->value)); }
static void mutex_unlock(struct mutex *m) { assert(!pthread_mutex_unlock(&m->value)); }
static void mutex_destroy(struct mutex *m) { assert(!pthread_mutex_destroy(&m->value)); }
static void list_add_tail(struct list_head *node, struct list_head *head)
{ node->next = head; node->prev = head->prev; head->prev->next = node; head->prev = node; }
static void list_del(struct list_head *node)
{ node->next->prev = node->prev; node->prev->next = node->next; }
static int copy_to_user(void *dst, const void *src, size_t size)
{ if (dst == bad_pointer) return -EFAULT; memcpy(dst, src, size); return 0; }
static void *allocate(size_t size)
{
    if (++allocation_count == fail_allocation) return NULL;
    void *p = calloc(1, size ? size : 1);
    assert(p);
    live_allocations++;
    return p;
}
static void release(void *p) { if (p) { assert(live_allocations); live_allocations--; free(p); } }
#define kfree release
#define kvfree release
#define kzalloc_obj(o) allocate(sizeof(o))
#define kvcalloc(n, s, flags) allocate((n) * (s))
#define kcalloc(n, s, flags) allocate((n) * (s))
#define kmalloc_array(n, s, flags) allocate((n) * (s))
static void *vmemdup_user(const void *src, size_t size)
{
    if (src == bad_pointer || !src) return ERR_PTR(-EFAULT);
    void *p = allocate(size);
    if (!p) return ERR_PTR(-ENOMEM);
    memcpy(p, src, size);
    return p;
}
static struct amdgpu_device *drm_to_adev(struct drm_device *dev) { return dev->adev; }
static struct drm_device *adev_to_drm(struct amdgpu_device *adev) { assert(adev == &device); return &drm; }
static bool amdgpu_userq_enabled(struct drm_device *dev) { return dev->adev->userq; }
static struct amdgpu_bo *gem_to_amdgpu_bo(struct drm_gem_object *obj) { return (struct amdgpu_bo *)obj; }
static struct drm_gem_object *drm_gem_object_lookup(struct drm_file *filp, u32 handle)
{
    if (!handle || handle >= 4 || handle == lookup_fail) return NULL;
    objects[handle].tbo.base.refs++;
    return &objects[handle].tbo.base;
}
static void drm_gem_object_put(struct drm_gem_object *obj) { if (obj) { assert(obj->refs > 1); obj->refs--; } }
static struct amdgpu_bo_va *amdgpu_vm_bo_find(struct amdgpu_vm *vm, struct amdgpu_bo *bo)
{ assert(locked); return missing_mapping ? NULL : bo->tbo.base.mapping; }
static struct amdgpu_bo_va_mapping *amdgpu_vm_bo_lookup_mapping(struct amdgpu_vm *vm, u64 addr)
{
    assert(locked);
    u64 page = addr / AMDGPU_GPU_PAGE_SIZE;
    for (unsigned i = 0; i < ARRAY_SIZE(old_mappings); i++)
        if (old_mappings[i].bo_va && page >= old_mappings[i].start && page <= old_mappings[i].last)
            return &old_mappings[i];
    return NULL;
}
static int amdgpu_vm_verify_parameters(struct amdgpu_device *adev, struct amdgpu_bo *bo,
                                      u64 addr, u64 offset, u64 size)
{ return !size || ((addr | offset | size) & 4095) ? -EINVAL : 0; }
static void drm_exec_init(struct drm_exec *exec, unsigned flags, unsigned count)
{ assert(!locked); exec->locked = false; }
static int drm_exec_lock_obj(struct drm_exec *exec, struct drm_gem_object *obj)
{
    if (obj->handle == lock_fail) return -EINTR;
    if (!exec->locked) { pthread_mutex_lock(&vm_mutex); exec->locked = locked = true; }
    locked_handles |= 1u << obj->handle;
    return 0;
}
static int amdgpu_vm_lock_pd(struct amdgpu_vm *vm, struct drm_exec *exec, unsigned extra)
{
    if (!exec->locked) { pthread_mutex_lock(&vm_mutex); exec->locked = locked = true; }
    if (mode_changed) vm->is_compute_context = true;
    return 0;
}
static void drm_exec_fini(struct drm_exec *exec)
{ if (exec->locked) { assert(locked); exec->locked = locked = false; pthread_mutex_unlock(&vm_mutex); } }
static void *xa_store(struct xarray *xa, unsigned long key, struct amdgpu_bo_va *entry, unsigned flags)
{
    for (unsigned i = 0; i < xa->count; i++) if (xa->entries[i] == entry) return NULL;
    void *token = allocate(1);
    if (!token) return ERR_PTR(-ENOMEM);
    release(token);
    assert(xa->count < ARRAY_SIZE(xa->entries));
    xa->entries[xa->count++] = entry;
    return NULL;
}
static int xa_err(const void *p) { return IS_ERR(p) ? PTR_ERR(p) : 0; }
static void xa_destroy(struct xarray *xa) { xa->count = 0; }
static void list_splice_init(struct list_head *from, struct list_head *to)
{ to->count += from->count; from->count = 0; }
static bool amdgpu_vm_ready(struct amdgpu_vm *vm) { assert(locked); return vm->ready; }
static bool amdgpu_vm_is_bo_always_valid(struct amdgpu_vm *vm, struct amdgpu_bo *bo)
{ return bo && bo->always_valid; }

static struct dma_fence *dma_fence_get(struct dma_fence *f) { if (f) { assert(f->refs); f->refs++; } return f; }
static void dma_fence_put(struct dma_fence *f)
{
    if (!f) return;
    assert(f->refs);
    if (--f->refs) return;
    if (f->kind == 1) {
        struct dma_fence_array *a = (void *)f;
        for (unsigned i = 0; i < a->num_fences; i++) dma_fence_put(a->fences[i]);
        release(a->fences);
    } else if (f->kind == 2) dma_fence_put(f->dependency);
    else assert(!"leaf lost its fixture reference");
    release(f);
}
static bool dma_fence_is_signaled(struct dma_fence *f)
{
    if (!f) return true;
    if (f->kind == 1) {
        struct dma_fence_array *a = (void *)f;
        for (unsigned i = 0; i < a->num_fences; i++) if (!dma_fence_is_signaled(a->fences[i])) return false;
        return true;
    }
    if (f->kind == 2) return f->signaled && dma_fence_is_signaled(f->dependency);
    return f->signaled;
}
static int dma_fence_get_status(struct dma_fence *f)
{ return !dma_fence_is_signaled(f) ? 0 : f->error ? f->error : 1; }
struct dma_fence_unwrap { unsigned i; };
#define dma_fence_unwrap_for_each(f, it, root) \
    for ((it)->i = 0; \
         (it)->i < ((root)->kind == 1 ? ((struct dma_fence_array *)(root))->num_fences : 1) && \
         ((f) = (root)->kind == 1 ? ((struct dma_fence_array *)(root))->fences[(it)->i] : (root)); ++(it)->i)
static struct dma_fence_array *dma_fence_array_alloc(unsigned count)
{
    struct dma_fence_array *a = allocate(sizeof(*a));
    if (a) a->capacity = count;
    return a;
}
static void dma_fence_array_init(struct dma_fence_array *a, unsigned count, struct dma_fence **fences,
                                  u64 context, unsigned seqno)
{ assert(count && count <= a->capacity); a->base.refs = 1; a->base.kind = 1; a->num_fences = count; a->fences = fences; }
static u64 dma_fence_context_alloc(unsigned count) { static u64 next; return ++next; }
static struct dma_fence_chain *dma_fence_chain_alloc(void) { return allocate(sizeof(struct dma_fence_chain)); }
static void dma_fence_chain_free(struct dma_fence_chain *c) { release(c); }
static struct drm_syncobj *drm_syncobj_find(struct drm_file *filp, u32 handle)
{ if (handle < 7 || handle > 8 || syncobj_fail) return NULL; syncobjs[handle - 7].refs++; return &syncobjs[handle - 7]; }
static void drm_syncobj_put(struct drm_syncobj *s) { assert(s->refs > 1); s->refs--; }
static void drm_syncobj_add_point(struct drm_syncobj *s, struct dma_fence_chain *chain,
                                  struct dma_fence *fence, u64 point)
{
    assert(locked && update_stage >= 3 && point > UINT32_MAX);
    dma_fence_put(s->fence); dma_fence_chain_free(s->chain);
    s->fence = dma_fence_get(fence); s->chain = chain; s->point = point; published++;
}
static void drm_syncobj_replace_fence(struct drm_syncobj *s, struct dma_fence *f)
{ dma_fence_put(s->fence); s->fence = dma_fence_get(f); published++; }
static void dma_resv_add_fence(struct dma_resv *resv, struct dma_fence *fence, unsigned usage)
{ assert(locked && usage == DMA_RESV_USAGE_BOOKKEEP); dma_fence_put(resv->fence); resv->fence = dma_fence_get(fence); }
static void amdgpu_vm_bo_replace_map_prealloc(struct amdgpu_device *adev, struct amdgpu_bo_va *bo_va,
                                              u64 va, u64 offset, u64 size, u32 flags,
                                              struct amdgpu_bo_va_mapping **maps, struct list_head *freed,
                                              bool *explicit_sync)
{
    assert(locked && mutations < ARRAY_SIZE(applied));
    if (!mutations) preparation_allocations = allocation_count;
    if (become_unready) priv.vm.ready = false;
    applied[mutations++] = (struct drm_amdgpu_gem_va_batch_op) {
        .handle = bo_va->base.bo ? bo_va->base.bo->tbo.base.handle : 0,
        .flags = flags, .va_address = va, .offset_in_bo = offset, .map_size = size,
    };
    freed->count++;
    for (unsigned i = 0; i < 3; i++) { release(maps[i]); maps[i] = NULL; }
}
static int amdgpu_vm_clear_freed(struct amdgpu_device *adev, struct amdgpu_vm *vm, struct dma_fence **out)
{ assert(locked); update_stage = 1; if (failure_stage == 1) return stage_error; vm->freed.count = 0; return 0; }
static int amdgpu_vm_clear_freed_list(struct amdgpu_device *adev, struct amdgpu_vm *vm, struct list_head *freed,
                                      unsigned sync, struct dma_fence **out)
{ assert(locked); if (failure_stage == 4) return stage_error; freed->count = 0; return 0; }
static int amdgpu_vm_bo_update_batch(struct amdgpu_device *adev, struct amdgpu_vm *vm, struct xarray *bos)
{
    assert(locked && update_stage == 1); update_stage = 2; bind_bos = bos->count;
    if (flush_trigger == 2) {
        dma_fence_put(vm->last_tlb_inv);
        vm->last_tlb_inv = dma_fence_get(&leaves[7]);
    }
    for (unsigned i = 0; i < bos->count; i++)
        if (bos->entries[i]->base.bo) updated_handles |= 1u << bos->entries[i]->base.bo->tbo.base.handle;
    return failure_stage == 2 ? stage_error : 0;
}
static int amdgpu_vm_update_pdes(struct amdgpu_device *adev, struct amdgpu_vm *vm, bool immediate)
{
    assert(locked && update_stage == 2); update_stage = 3;
    if (flush_trigger == 1) vm->tlb_seq++;
    return failure_stage == 3 ? stage_error : 0;
}
static int amdgpu_vm_tlb_fence_create(struct amdgpu_device *adev, struct amdgpu_vm *vm,
                                      struct dma_fence *dep, struct dma_fence **out)
{
    assert(locked && update_stage == 3 && dep);
    dependency_stage = update_stage;
    if (tlb_failure) return -EIO;
    struct dma_fence *f = allocate(sizeof(*f));
    if (!f) return -ENOMEM;
    f->kind = 2; f->refs = 1; f->dependency = dma_fence_get(dep);
    dma_fence_put(*out); *out = f;
    return 0;
}

ACTUAL_FUNCTIONS

static void setup(bool userq)
{
    assert(!live_allocations && !locked);
    allocation_count = fail_allocation = mutations = published = update_stage = 0;
    lookup_fail = lock_fail = syncobj_fail = failure_stage = tlb_failure = dependency_stage = 0;
    preparation_allocations = bind_bos = locked_handles = updated_handles = 0;
    memset(old_mappings, 0, sizeof(old_mappings)); stage_error = -ENOMEM;
    flush_trigger = 1;
    missing_mapping = mode_changed = become_unready = false; bad_pointer = (void *)1;
    memset(&priv, 0, sizeof(priv)); memset(objects, 0, sizeof(objects));
    memset(mappings, 0, sizeof(mappings)); memset(leaves, 0, sizeof(leaves));
    memset(reservations, 0, sizeof(reservations)); memset(&root_bo, 0, sizeof(root_bo));
    memset(syncobjs, 0, sizeof(syncobjs)); memset(&prt, 0, sizeof(prt));
    memset(operations, 0, sizeof(operations)); memset(applied, 0, sizeof(applied));
    device = (struct amdgpu_device){.userq = userq, .vm_manager.max_pfn = UINT64_C(1) << 24};
    drm.adev = &device; file.driver_priv = &priv;
    for (unsigned i = 0; i < ARRAY_SIZE(leaves); i++) leaves[i].refs = 1;
    for (unsigned i = 0; i < ARRAY_SIZE(objects); i++) {
        resources[i].mem_type = TTM_PL_VRAM;
        objects[i].tbo.resource = &resources[i];
        objects[i].tbo.base = (struct drm_gem_object){.resv = &reservations[i], .mapping = &mappings[i], .refs = 1, .handle = i};
        mappings[i].base.bo = &objects[i]; mappings[i].last_pt_update = dma_fence_get(&leaves[i]);
    }
    root_bo.tbo.base.resv = &reservations[4];
    priv.vm.ready = true; priv.vm.need_tlb_fence = userq; priv.vm.root.bo = &root_bo;
    priv.vm.last_update = dma_fence_get(&leaves[4]);
    priv.vm.last_tlb_inv = dma_fence_get(&leaves[5]);
    prt.last_pt_update = dma_fence_get(&leaves[6]); priv.prt_va = &prt;
    first_syncobj.refs = syncobjs[1].refs = 1;
    operations[0] = (struct drm_amdgpu_gem_va_batch_op){.handle = 1, .flags = AMDGPU_VM_PAGE_READABLE,
                                                     .va_address = 0x10000, .map_size = 4096};
    operations[1] = (struct drm_amdgpu_gem_va_batch_op){.handle = 2, .flags = AMDGPU_VM_PAGE_WRITEABLE,
                                                     .va_address = 0x20000, .map_size = 4096};
    operations[2] = (struct drm_amdgpu_gem_va_batch_op){.handle = 0, .flags = AMDGPU_VM_PAGE_PRT,
                                                     .va_address = 0x10000, .map_size = 4096};
}
static void cleanup(void)
{
    assert(!locked && first_syncobj.refs == 1 && syncobjs[1].refs == 1);
    for (unsigned i = 0; i < 2; i++) { dma_fence_put(syncobjs[i].fence); dma_fence_chain_free(syncobjs[i].chain); }
    for (unsigned i = 0; i < ARRAY_SIZE(reservations); i++) dma_fence_put(reservations[i].fence);
    amdgpu_vm_batch_fini(&priv.vm);
    dma_fence_put(priv.vm.last_update); dma_fence_put(priv.vm.last_tlb_inv); dma_fence_put(prt.last_pt_update);
    for (unsigned i = 0; i < ARRAY_SIZE(objects); i++) {
        assert(objects[i].tbo.base.refs == 1); dma_fence_put(mappings[i].last_pt_update);
    }
    for (unsigned i = 0; i < ARRAY_SIZE(leaves); i++) assert(leaves[i].refs == 1);
    assert(!live_allocations); cases++;
}
static struct drm_amdgpu_gem_va_batch request(bool timeline)
{
    struct drm_amdgpu_gem_va_batch a = {.cookie = AMDGPU_GEM_VA_BATCH_COOKIE,
                                      .operations = (uintptr_t)operations, .count = 3};
#ifdef AMDGPU_GEM_VA_BATCH_VM_TIMELINE
    if (timeline) {
        a.flags = AMDGPU_GEM_VA_BATCH_VM_TIMELINE;
        a.vm_timeline_syncobj_out = 7; a.vm_timeline_point = UINT64_C(0x100000002);
    }
#else
    assert(!timeline);
#endif
    return a;
}
static int run(struct drm_amdgpu_gem_va_batch *a) { return amdgpu_gem_va_batch_ioctl(&drm, a, &file); }
static void legacy_tests(void)
{
    setup(false); struct drm_amdgpu_gem_va_batch a = request(false);
    assert(run(&a) == 0 && mutations == 3 && !published);
    assert(applied[0].handle == 1 && applied[1].handle == 2 && applied[2].handle == 0);
    unsigned allocations = allocation_count; cleanup();
    for (unsigned fail = 1; fail <= allocations; fail++) {
        setup(false); a = request(false); fail_allocation = fail;
        assert(run(&a) == -ENOMEM && !mutations && !published); cleanup();
    }
    setup(false); a = request(false); operations[2].map_size = 3;
    assert(run(&a) == -EINVAL && !mutations); cleanup();
    setup(false); a = request(false); priv.vm.ready = false;
    assert(run(&a) == 0 && mutations == 3 && !update_stage && priv.vm.freed.count == 3); cleanup();
    for (unsigned stage = 1; stage <= 3; stage++) {
        setup(false); a = request(false); failure_stage = stage;
        assert(run(&a) == 0 && mutations == 3 && !published); cleanup();
    }
    setup(true); a = request(false); assert(run(&a) == -EOPNOTSUPP && !mutations); cleanup();
    setup(false); a = request(false); a.reserved[0] = 1;
    assert(run(&a) == -EINVAL && !mutations); cleanup();
}
static void timeline_tests(void)
{
    struct drm_amdgpu_gem_va_batch a;
    setup(true); a = request(true);
    assert(sizeof(a) == 40 && offsetof(struct drm_amdgpu_gem_va_batch, vm_timeline_syncobj_out) == 24);
    assert(run(&a) == 0 && mutations == 3 && published == 1 && dependency_stage == 3);
    assert(first_syncobj.point == a.vm_timeline_point && first_syncobj.fence == priv.vm.last_tlb_inv);
    assert(first_syncobj.fence->kind == 2 && first_syncobj.fence->dependency->kind == 1);
    struct dma_fence_array *array = (void *)first_syncobj.fence->dependency;
    assert(array->num_fences == 5 && array->capacity == 5 && bind_bos == 3);
    const unsigned dependencies[] = {4, 5, 1, 2, 6};
    for (unsigned i = 0; i < ARRAY_SIZE(dependencies); i++) {
        bool found = false;
        for (unsigned j = 0; j < array->num_fences; j++) found |= array->fences[j] == &leaves[dependencies[i]];
        assert(found);
    }
    /* Every private BO, PRT, PDE and previous invalidation remains a dependency. */
    first_syncobj.fence->signaled = true;
    for (unsigned i = 0; i < ARRAY_SIZE(dependencies); i++) {
        for (unsigned j = 0; j < ARRAY_SIZE(dependencies); j++) leaves[dependencies[j]].signaled = i != j;
        assert(!dma_fence_is_signaled(first_syncobj.fence));
    }
    for (unsigned i = 0; i < ARRAY_SIZE(dependencies); i++) leaves[dependencies[i]].signaled = true;
    first_syncobj.fence->signaled = false;
    assert(!dma_fence_is_signaled(first_syncobj.fence));
    first_syncobj.fence->signaled = true;
    assert(dma_fence_is_signaled(first_syncobj.fence));
    unsigned allocations = preparation_allocations; cleanup();
    for (unsigned fail = 1; fail <= allocations; fail++) {
        setup(true); a = request(true); fail_allocation = fail;
        assert(run(&a) == -ENOMEM && !mutations && !published); cleanup();
    }
    /* Error leaves are retained rather than folded into a successful stub. */
    setup(true); a = request(true); leaves[1].signaled = true; leaves[1].error = -EIO;
    assert(run(&a) == 0 && first_syncobj.fence->kind == 2);
    array = (void *)first_syncobj.fence->dependency;
    bool found = false;
    for (unsigned i = 0; i < array->num_fences; i++) found |= array->fences[i] == &leaves[1];
    assert(found); cleanup();
    /* A shared BO appears only once even with several replacements. */
    setup(true); a = request(true); operations[1].handle = 1;
    assert(run(&a) == 0 && mutations == 3 && bind_bos == 2);
    array = (void *)first_syncobj.fence->dependency;
    assert(array->num_fences == 4 && array->capacity == 4 && applied[1].va_address == 0x20000); cleanup();
    /* An always-valid BO may not have its own update fence. */
    setup(true); a = request(true); objects[1].always_valid = true;
    dma_fence_put(mappings[1].last_pt_update); mappings[1].last_pt_update = NULL;
    assert(run(&a) == 0 && published == 1);
    array = (void *)first_syncobj.fence->dependency;
    assert(array->num_fences == 4); cleanup();
    /* Kernel queues can request a completion array without a PASID flush. */
    setup(false); a = request(true);
    assert(run(&a) == 0 && published == 1 && !dependency_stage && first_syncobj.fence->kind == 1); cleanup();

    for (unsigned n = 0; n < 4; n++) {
        setup(true); a = request(true);
        if (n == 0) priv.vm.ready = false;
        if (n == 1) resources[2].mem_type = TTM_PL_SYSTEM;
        if (n == 2) objects[2].tbo.resource = NULL;
        if (n == 3) { resources[1].mem_type = TTM_PL_TT; resources[2].mem_type = TTM_PL_SYSTEM; }
        assert(run(&a) == -EBUSY && !mutations && !published && !update_stage); cleanup();
    }
    setup(true); a = request(true); resources[1].mem_type = TTM_PL_TT;
    assert(run(&a) == 0 && mutations == 3 && published == 1); cleanup();
    for (unsigned n = 0; n < 4; n++) {
        setup(true); a = request(true);
        if (n == 0) device.debug_vm = true;
        if (n == 1) priv.vm.use_cpu_for_update = true;
        if (n == 2) priv.vm.is_compute_context = true;
        if (n == 3) mode_changed = true;
        assert(run(&a) == -EOPNOTSUPP && !mutations && !published); cleanup();
    }
    /* No post-mutation error may masquerade as rejection or request a restart. */
    const int errors[] = {-ENOMEM, -EINTR, -512, -EAGAIN};
    for (unsigned i = 0; i < ARRAY_SIZE(errors); i++) {
        for (unsigned stage = 1; stage <= 4; stage++) {
            setup(true); a = request(true); a.flags |= AMDGPU_GEM_VA_BATCH_WAIT_COMPLETE;
            failure_stage = stage; stage_error = errors[i];
            first_syncobj.fence = dma_fence_get(&leaves[7]); first_syncobj.point = UINT64_C(0x100000001);
            assert(run(&a) == -EIO && mutations == 3 && !published);
            assert(first_syncobj.fence == &leaves[7] && first_syncobj.point == UINT64_C(0x100000001)); cleanup();
        }
    }
    setup(true); a = request(true); tlb_failure = 1;
    assert(run(&a) == -EIO && mutations == 3 && !published && priv.vm.last_tlb_inv == &leaves[5]); cleanup();
    setup(true); a = request(true); become_unready = true;
    assert(run(&a) == -EIO && mutations == 3 && !published && !update_stage); cleanup();
}

static void validation_tests(void)
{
    for (unsigned timeline = 0; timeline <= 1; timeline++) {
        for (unsigned bad = 0; bad < 15; bad++) {
            setup(timeline); struct drm_amdgpu_gem_va_batch a = request(timeline);
            int expected = -EINVAL;
            switch (bad) {
            case 0: a.cookie++; break;
            case 1: a.flags |= 1u << 31; break;
            case 2: a.count = 0; break;
            case 3: a.count = AMDGPU_GEM_VA_BATCH_MAX_OPS + 1; break;
            case 4: a.operations = (uintptr_t)bad_pointer; expected = -EFAULT; break;
            case 5: operations[2].handle = 1; break;
            case 6: operations[2].offset_in_bo = 4096; break;
            case 7: operations[2].va_address = 0; break;
            case 8: operations[2].va_address = AMDGPU_GMC_HOLE_START; break;
            case 9: operations[2].map_size = UINT64_MAX; break;
            case 10: operations[2].flags |= AMDGPU_VM_DELAY_UPDATE; break;
            case 11: lookup_fail = 2; expected = -ENOENT; break;
            case 12: missing_mapping = true; expected = -ENOENT; break;
            case 13: lock_fail = 2; expected = -EINTR; break;
            case 14: operations[2].map_size = 0; break;
            }
            assert(run(&a) == expected && !mutations && !published); cleanup();
        }
    }
    for (unsigned bad = 0; bad < 5; bad++) {
        setup(true); struct drm_amdgpu_gem_va_batch a = request(true); int expected = -EINVAL;
        switch (bad) {
        case 0: a.vm_timeline_syncobj_out = 0; break;
        case 1: a.vm_timeline_point = 0; break;
        case 2: a.pad = 1; break;
        case 3: a.vm_timeline_syncobj_out = 9; expected = -ENOENT; break;
        case 4: syncobj_fail = 1; expected = -ENOENT; break;
        }
        assert(run(&a) == expected && !mutations && !published); cleanup();
    }
}

static void boundary_tests(void)
{
    for (unsigned sides = 1; sides <= 3; sides++) {
        setup(true); struct drm_amdgpu_gem_va_batch a = request(true); a.count = 1;
        old_mappings[0] = (struct amdgpu_bo_va_mapping) {
            .start = 0x10 - !!(sides & 1), .last = 0x10 + !!(sides & 2), .bo_va = &mappings[3],
        };
        assert(run(&a) == 0 && mutations == 1 && published == 1 && bind_bos == 2);
        assert((locked_handles & 8) && (updated_handles & 8));
        struct dma_fence_array *array = (void *)first_syncobj.fence->dependency;
        bool found = false;
        for (unsigned i = 0; i < array->num_fences; i++) found |= array->fences[i] == &leaves[3];
        assert(found);
        for (unsigned i = 0; i < ARRAY_SIZE(leaves); i++) leaves[i].signaled = i != 3;
        first_syncobj.fence->signaled = true;
        assert(!dma_fence_is_signaled(first_syncobj.fence));
        leaves[3].signaled = true;
        assert(dma_fence_is_signaled(first_syncobj.fence));
        cleanup();
    }
    /* A fully replaced old mapping has no surviving range to rewrite. */
    setup(true); struct drm_amdgpu_gem_va_batch a = request(true); a.count = 1;
    old_mappings[0] = (struct amdgpu_bo_va_mapping){.start = 0x10, .last = 0x10, .bo_va = &mappings[3]};
    assert(run(&a) == 0 && bind_bos == 1 && !(locked_handles & 8) && !(updated_handles & 8)); cleanup();
    /* The two partial boundaries can belong to different old buffers. */
    setup(true); a = request(true); a.count = 1; operations[0].map_size = 4096 * 4;
    old_mappings[0] = (struct amdgpu_bo_va_mapping){.start = 0xf, .last = 0x11, .bo_va = &mappings[2]};
    old_mappings[1] = (struct amdgpu_bo_va_mapping){.start = 0x12, .last = 0x14, .bo_va = &mappings[3]};
    assert(run(&a) == 0 && bind_bos == 3 && (locked_handles & 12) == 12 && (updated_handles & 12) == 12); cleanup();
    /* Missing residency or lock failure in a survivor rejects before replacing. */
    for (unsigned failure = 0; failure < 3; failure++) {
        setup(true); a = request(true); a.count = 1;
        old_mappings[0] = (struct amdgpu_bo_va_mapping){.start = 0xf, .last = 0x11, .bo_va = &mappings[3]};
        if (failure == 0) resources[3].mem_type = TTM_PL_SYSTEM;
        if (failure == 1) objects[3].tbo.resource = NULL;
        if (failure == 2) lock_fail = 3;
        assert(run(&a) == (failure == 2 ? -EINTR : -EBUSY) && !mutations && !published); cleanup();
    }
    /* Sparse survivors have no GEM object, but their PTE fence is still needed. */
    setup(true); a = request(true); a.count = 1;
    old_mappings[0] = (struct amdgpu_bo_va_mapping){.start = 0xf, .last = 0x11, .bo_va = &prt};
    assert(run(&a) == 0 && bind_bos == 2);
    struct dma_fence_array *array = (void *)first_syncobj.fence->dependency;
    bool found = false;
    for (unsigned i = 0; i < array->num_fences; i++) found |= array->fences[i] == &leaves[6];
    assert(found); cleanup();
}

static void capability_and_limit_tests(void)
{
    for (unsigned userq = 0; userq <= 1; userq++) {
        setup(userq);
        assert(amdgpu_gem_va_batch_supported(&device, &priv.vm, true));
        assert(amdgpu_gem_va_batch_supported(&device, &priv.vm, false) == !userq);
        priv.vm.use_cpu_for_update = true;
        assert(!amdgpu_gem_va_batch_supported(&device, &priv.vm, true));
        assert(!amdgpu_gem_va_batch_supported(&device, &priv.vm, false));
        cleanup();
    }
    for (unsigned timeline = 0; timeline <= 1; timeline++) {
        setup(timeline); struct drm_amdgpu_gem_va_batch a = request(timeline);
        for (unsigned i = 0; i < AMDGPU_GEM_VA_BATCH_MAX_OPS; i++) {
            operations[i] = (struct drm_amdgpu_gem_va_batch_op) {
                .handle = 1 + (i % 2), .flags = AMDGPU_VM_PAGE_READABLE,
                .va_address = UINT64_C(0x10000) + i * UINT64_C(4096), .map_size = 4096,
            };
        }
        a.count = AMDGPU_GEM_VA_BATCH_MAX_OPS;
        assert(run(&a) == 0 && mutations == AMDGPU_GEM_VA_BATCH_MAX_OPS);
        assert(published == timeline && bind_bos == 2);
        if (timeline) {
            struct dma_fence_array *array = (void *)first_syncobj.fence->dependency;
            assert(array->capacity == 4);
        }
        assert(applied[AMDGPU_GEM_VA_BATCH_MAX_OPS - 1].va_address == operations[AMDGPU_GEM_VA_BATCH_MAX_OPS - 1].va_address);
        cleanup();
    }
}

static void query_tests(void)
{
    for (unsigned userq = 0; userq <= 1; userq++) {
        for (unsigned optin = 0; optin <= 1; optin++) {
            setup(userq);
            struct drm_amdgpu_info info = {.query = AMDGPU_INFO_GEM_VA_BATCH};
            struct drm_amdgpu_info_gem_va_batch caps = {0};
            info.gem_va_batch.flags = optin ? AMDGPU_GEM_VA_BATCH_VM_TIMELINE : 0;
            int result = probe_batch(&info, &caps, sizeof(caps));
            assert(result == (userq && !optin ? -EOPNOTSUPP : 0));
            if (!result) {
                assert(caps.cookie == AMDGPU_GEM_VA_BATCH_COOKIE && caps.max_ops == AMDGPU_GEM_VA_BATCH_MAX_OPS);
                assert(caps.features & AMDGPU_GEM_VA_BATCH_VM_TIMELINE);
            }
            cleanup();
        }
    }
    for (unsigned bad = 0; bad < 4; bad++) {
        setup(false);
        struct drm_amdgpu_info info = {.query = AMDGPU_INFO_GEM_VA_BATCH};
        struct drm_amdgpu_info_gem_va_batch caps = {0};
        info.gem_va_batch.flags = AMDGPU_GEM_VA_BATCH_VM_TIMELINE;
        if (bad == 0) info.gem_va_batch.flags |= AMDGPU_GEM_VA_BATCH_WAIT_COMPLETE;
        if (bad == 1) info.gem_va_batch.pad = 1;
        if (bad == 2) priv.vm.use_cpu_for_update = true;
        assert(probe_batch(&info, bad == 3 ? (void *)bad_pointer : &caps, sizeof(caps)) ==
               (bad < 2 ? -EINVAL : bad == 2 ? -EOPNOTSUPP : -EFAULT));
        cleanup();
    }
    setup(false);
    struct drm_amdgpu_info info = {.query = AMDGPU_INFO_GEM_VA_BATCH};
    struct drm_amdgpu_info_gem_va_batch caps;
    memset(&caps, 0xa5, sizeof(caps));
    assert(!probe_batch(&info, &caps, offsetof(struct drm_amdgpu_info_gem_va_batch, features)));
    assert(caps.cookie == AMDGPU_GEM_VA_BATCH_COOKIE && caps.features == UINT64_C(0xa5a5a5a5a5a5a5a5));
    cleanup();
}

static void conditional_flush_tests(void)
{
    setup(true); struct drm_amdgpu_gem_va_batch a = request(true); flush_trigger = 0;
    assert(!run(&a) && published == 1 && !dependency_stage);
    assert(first_syncobj.fence->kind == 1 && priv.vm.last_tlb_inv == &leaves[5]);
    assert(!dma_fence_is_signaled(first_syncobj.fence));
    for (unsigned i = 0; i < ARRAY_SIZE(leaves); i++) leaves[i].signaled = true;
    assert(dma_fence_is_signaled(first_syncobj.fence));
    cleanup();
    /* A queued flush changes the fence before its deferred sequence callback runs. */
    setup(true); a = request(true); flush_trigger = 2;
    assert(!run(&a) && published == 1 && dependency_stage == 3 && !priv.vm.tlb_seq);
    assert(first_syncobj.fence->kind == 2);
    struct dma_fence_array *array = (void *)first_syncobj.fence->dependency;
    bool found = false;
    for (unsigned i = 0; i < array->num_fences; i++) found |= array->fences[i] == &leaves[7];
    assert(found); cleanup();
    /* A synchronous PDE sequence update also requests the final flush. */
    setup(true); a = request(true); priv.vm.tlb_seq = UINT64_MAX;
    assert(!run(&a) && dependency_stage == 3 && !priv.vm.tlb_seq); cleanup();
}

static void sticky_ioctl_tests(void)
{
    setup(true); struct drm_amdgpu_gem_va_batch a = request(true);
    assert(!run(&a));
    for (unsigned i = 0; i < ARRAY_SIZE(leaves); i++) leaves[i].signaled = true;
    first_syncobj.fence->error = -ETIMEDOUT;
    first_syncobj.fence->signaled = true;
    /* Removing a userspace timeline node cannot remove the VM's record. */
    dma_fence_put(first_syncobj.fence); first_syncobj.fence = NULL;
    dma_fence_chain_free(first_syncobj.chain); first_syncobj.chain = NULL;
    unsigned before = allocation_count;
    assert(run(&a) == -EIO && mutations == 3 && published == 1 && allocation_count == before);
    assert(amdgpu_vm_batch_status(&priv.vm) == -EIO); cleanup();

    setup(true); a = request(true); failure_stage = 3;
    assert(run(&a) == -EIO && mutations == 3 && !published);
    failure_stage = 0; before = allocation_count;
    assert(run(&a) == -EIO && mutations == 3 && !published && allocation_count == before); cleanup();

    setup(true); a = request(true); flush_trigger = 0;
    assert(!run(&a) && first_syncobj.fence->kind == 1);
    leaves[1].error = -EIO; leaves[1].signaled = true;
    assert(!dma_fence_is_signaled(first_syncobj.fence));
    assert(run(&a) == -EIO && mutations == 3 && published == 1); cleanup();
}

static void *concurrent_batch(void *data)
{
    struct drm_amdgpu_gem_va_batch *a = data;
    assert(run(a) == 0);
    return NULL;
}

static void concurrency_test(void)
{
    for (unsigned iteration = 0; iteration < 25; iteration++) {
        setup(true);
        struct drm_amdgpu_gem_va_batch args[2] = {request(true), request(true)};
        struct drm_amdgpu_gem_va_batch_op other_ops[3];
        memcpy(other_ops, operations, sizeof(other_ops));
        for (unsigned i = 0; i < 3; i++) other_ops[i].va_address += 0x80000;
        args[1].operations = (uintptr_t)other_ops;
        args[1].vm_timeline_syncobj_out = 8;
        pthread_t threads[2];
        assert(!pthread_create(&threads[0], NULL, concurrent_batch, &args[0]));
        assert(!pthread_create(&threads[1], NULL, concurrent_batch, &args[1]));
        assert(!pthread_join(threads[0], NULL));
        assert(!pthread_join(threads[1], NULL));
        assert(published == 2 && mutations == 6);
        bool first_high = applied[0].va_address >= 0x80000;
        for (unsigned i = 0; i < 3; i++) {
            assert((applied[i].va_address >= 0x80000) == first_high);
            assert((applied[i + 3].va_address >= 0x80000) != first_high);
        }
        for (unsigned i = 0; i < ARRAY_SIZE(leaves); i++) leaves[i].signaled = true;
        assert(!dma_fence_is_signaled(syncobjs[0].fence));
        assert(!dma_fence_is_signaled(syncobjs[1].fence));
        syncobjs[0].fence->signaled = syncobjs[1].fence->signaled = true;
        assert(dma_fence_is_signaled(syncobjs[0].fence));
        assert(dma_fence_is_signaled(syncobjs[1].fence));
        cleanup();
    }
}

int main(void)
{
    legacy_tests();
    timeline_tests();
    validation_tests();
    boundary_tests();
    capability_and_limit_tests();
    query_tests();
    conditional_flush_tests();
    sticky_ioctl_tests();
    concurrency_test();
    printf("VA batch: %u cases passed\n", cases);
    return 0;
}
'''

if __name__ == '__main__':
    unittest.main()
