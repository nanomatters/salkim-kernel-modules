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
AMD = 'amdgpu/drivers/gpu/drm/amd/amdgpu/'


class UserqUpstreamTests(unittest.TestCase):
    def source(self, file):
        revision = os.environ.get('USERQ_TEST_REVISION')
        if revision:
            return subprocess.check_output(['git', 'show', revision + ':' + AMD + file],
                                           cwd=ROOT, text=True)
        return (ROOT / AMD / file).read_text()

    def function(self, file, name):
        match = re.search(r'^(?:static )?(?:int|void)\s+' + name + r'\(.*?^\}',
                          self.source(file), re.M | re.S)
        self.assertIsNotNone(match, name)
        return match.group()

    def run_program(self, source):
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
            program = directory / 'test.c'
            binary = directory / 'test'
            program.write_text(source)
            subprocess.run(flags + [str(program), '-o', str(binary)], check=True, timeout=30)
            subprocess.run([str(binary)], check=True, timeout=30)

    def test_destroy_watchdog(self):
        body = self.function('amdgpu_userq.c', 'amdgpu_userq_destroy')
        self.run_program(DESTROY_HARNESS.replace('ACTUAL_DESTROY', body))


    def test_wptr_buffers(self):
        body = self.function('amdgpu_userq_fence.c', 'amdgpu_userq_fence_read_wptr')
        body += '\n' + self.function('mes_userqueue.c', 'mes_userq_create_wptr_mapping')
        self.run_program(WPTR_HARNESS.replace('ACTUAL_WPTR', body))


DESTROY_HARNESS = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdlib.h>
#include <stdio.h>
struct mutex { bool held; };
struct work { bool pending; struct mutex *needed; };
struct amdgpu_usermode_queue;
struct amdgpu_userq_funcs { void (*mqd_destroy)(struct amdgpu_usermode_queue *); };
struct amdgpu_device { const struct amdgpu_userq_funcs *userq_funcs[1];
                       struct { int wq; } *reset_domain; void *dev; };
struct amdgpu_userq_mgr { struct amdgpu_device *adev; struct work resume_work, reset_work;
                         struct mutex userq_mutex; int userq_count[1]; };
struct amdgpu_usermode_queue { unsigned queue_type; struct work hang_detect_work;
                              struct amdgpu_userq_mgr *userq_mgr;
                              void *fence_drv; struct { void *obj; } db_obj; };
static unsigned cancelled, detached, unmapped, destroyed, powered, resets;
static int unmap_error;
#define trace_amdgpu_userq_destroy_start(q) ((void)0)
#define trace_amdgpu_userq_destroy_end(q,r) ((void)0)
#define adev_to_drm(a) (a)
#define kfree(p) free(p)
static void mutex_lock(struct mutex *lock) { assert(!lock->held); lock->held = true; }
static void mutex_unlock(struct mutex *lock) { assert(lock->held); lock->held = false; }
static void cancel_delayed_work_sync(struct work *work)
{
    if (work->pending && work->needed)
        assert(!work->needed->held);
    work->pending = false;
    cancelled++;
}
static void amdgpu_userq_wait_for_last_fence(struct amdgpu_usermode_queue *q)
{ assert(q->userq_mgr->userq_mutex.held); }
static void amdgpu_userq_detach_doorbell(struct amdgpu_usermode_queue *q)
{ assert(q->userq_mgr->userq_mutex.held); detached++; }
static int amdgpu_userq_unmap_helper(struct amdgpu_usermode_queue *q)
{ assert(q->userq_mgr->userq_mutex.held); unmapped++; return unmap_error; }
static void atomic_dec(int *value) { assert(*value == 1); --*value; }
static void amdgpu_userq_fence_driver_free(struct amdgpu_usermode_queue *q)
{ assert(q->userq_mgr->userq_mutex.held); }
static void queue_work(int wq, struct work *work) { (void)wq; (void)work; resets++; }
static void mqd_destroy(struct amdgpu_usermode_queue *q)
{ assert(!q->userq_mgr->userq_mutex.held); destroyed++; }
static void amdgpu_bo_reserve(void *bo, bool interruptible) { (void)bo; (void)interruptible; }
static void amdgpu_bo_unpin(void *bo) { (void)bo; }
static void amdgpu_bo_unreserve(void *bo) { (void)bo; }
static void amdgpu_bo_unref(void **bo) { *bo = NULL; }
static void pm_runtime_put_autosuspend(void *dev) { (void)dev; powered++; }
ACTUAL_DESTROY
int main(void)
{
    const struct amdgpu_userq_funcs funcs = {.mqd_destroy = mqd_destroy};
    for (unsigned pending = 0; pending < 2; pending++) {
        for (unsigned failed = 0; failed < 2; failed++) {
            struct amdgpu_device adev = {.userq_funcs = {&funcs}};
            struct { int wq; } domain = {0};
            adev.reset_domain = (void *)&domain;
            struct amdgpu_userq_mgr mgr = {.adev = &adev, .userq_count = {1}};
            struct amdgpu_usermode_queue *queue = calloc(1, sizeof(*queue));
            assert(queue);
            queue->userq_mgr = &mgr;
            queue->hang_detect_work.pending = pending;
            queue->hang_detect_work.needed = &mgr.userq_mutex;
            unmap_error = failed ? -EIO : 0;
            assert(amdgpu_userq_destroy(&mgr, queue) == unmap_error);
            assert(!mgr.userq_mutex.held && mgr.userq_count[0] == 0);
        }
    }
    assert(cancelled == 8 && detached == 4 && unmapped == 4);
    assert(destroyed == 4 && powered == 4 && resets == 2);
    puts("UserQ destruction: pending watchdog and cleanup cases passed");
    return 0;
}
'''


WPTR_HARNESS = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
typedef uint64_t u64;
#define AMDGPU_GPU_PAGE_SHIFT 12
#define PAGE_SIZE 4096
#define TTM_NUM_MOVE_FENCES 2
#define AMDGPU_GMC_HOLE_MASK 0x0000ffffffffffffULL
#define DRM_EXEC_IGNORE_DUPLICATES 1
#define DRM_ERROR(...) ((void)0)
#define unlikely(v) (v)
#define le64_to_cpu(v) (v)
#define drm_exec_until_all_locked(e) for (bool once = true; once; once = false)
#define drm_exec_retry_on_contention(e) ((void)0)
struct drm_exec { int unused; };
struct gem_object { size_t size; bool imported; };
struct amdgpu_bo { struct { struct gem_object base; } tbo; uint64_t words[1024]; unsigned refs; };
struct amdgpu_bo_va { struct { struct amdgpu_bo *bo; } base; };
struct amdgpu_bo_va_mapping { u64 start, last, offset; struct amdgpu_bo_va *bo_va; };
struct amdgpu_vm { struct amdgpu_bo_va_mapping *mapping; };
struct amdgpu_userq_obj { struct amdgpu_bo *obj; u64 gpu_addr; };
struct amdgpu_usermode_queue { struct amdgpu_vm *vm; struct { u64 wptr_gpu_addr; } *userq_prop;
                              struct amdgpu_userq_obj wptr_obj; };
struct amdgpu_userq_mgr { int evf_mgr; };
struct amdgpu_device { int unused; };
static int pd_error, bo_error, map_error, attach_error, gart_error;
static unsigned maps, unmaps, cleanups, attached, gart;
bool drm_gem_is_imported(struct gem_object *object) { return object->imported; }
static struct amdgpu_userq_mgr *uq_mgr_to_fpriv(struct amdgpu_userq_mgr *mgr) { return mgr; }
static int drm_exec_prepare_obj(struct drm_exec *exec, struct gem_object *object, unsigned fences)
{ (void)exec; (void)object; assert(fences == TTM_NUM_MOVE_FENCES + 1); return bo_error; }
static struct amdgpu_bo *amdgpu_bo_ref(struct amdgpu_bo *bo) { bo->refs++; return bo; }
static void amdgpu_bo_unref(struct amdgpu_bo **bo) { assert((*bo)->refs); (*bo)->refs--; *bo = NULL; }
static int amdgpu_evf_mgr_attach_fence(int *mgr, struct amdgpu_bo *bo)
{ (void)mgr; (void)bo; attached++; return attach_error; }
static int amdgpu_ttm_alloc_gart(void *ttm) { (void)ttm; gart++; return gart_error; }
static u64 amdgpu_bo_gpu_offset(struct amdgpu_bo *bo) { (void)bo; return 0x2000000; }
static void drm_exec_init(struct drm_exec *exec, unsigned flags, unsigned objects)
{ (void)exec; (void)flags; (void)objects; }
static void drm_exec_fini(struct drm_exec *exec) { (void)exec; cleanups++; }
static int amdgpu_vm_lock_pd(struct amdgpu_vm *vm, struct drm_exec *exec, unsigned fences)
{ (void)vm; (void)exec; (void)fences; return pd_error; }
static struct amdgpu_bo_va_mapping *amdgpu_vm_bo_lookup_mapping(struct amdgpu_vm *vm, u64 address)
{
    u64 page = address >> AMDGPU_GPU_PAGE_SHIFT;
    return vm->mapping && page >= vm->mapping->start && page <= vm->mapping->last ? vm->mapping : NULL;
}
static int drm_exec_lock_obj(struct drm_exec *exec, struct gem_object *object)
{ (void)exec; (void)object; return bo_error; }
static int amdgpu_bo_kmap(struct amdgpu_bo *bo, void **pointer)
{ assert(!bo->tbo.base.imported); maps++; *pointer = bo->words; return map_error; }
static void amdgpu_bo_kunmap(struct amdgpu_bo *bo) { (void)bo; unmaps++; }
ACTUAL_WPTR
int main(void)
{
    struct amdgpu_device adev = {0};
    struct amdgpu_bo bo = {.tbo.base.size = 8192};
    struct amdgpu_bo_va bo_va = {.base.bo = &bo};
    const u64 base = 0x123450000ULL;
    struct amdgpu_bo_va_mapping mapping = {.start = base >> 12, .last = base >> 12, .bo_va = &bo_va};
    struct amdgpu_vm vm = {.mapping = &mapping};
    struct { u64 wptr_gpu_addr; } props = {0};
    struct amdgpu_usermode_queue queue = {.vm = &vm, .userq_prop = (void *)&props};
    for (unsigned i = 0; i < 1024; ++i)
        bo.words[i] = 0x1000000000000000ULL + i;
    u64 result = 0;
    unsigned cases = 0;
    for (u64 mapping_offset = 0; mapping_offset <= 4096; mapping_offset += 4096) {
        mapping.offset = mapping_offset;
        for (u64 offset = 0; offset < 4096; offset += 8) {
            props.wptr_gpu_addr = base + offset;
            assert(amdgpu_userq_fence_read_wptr(&adev, &queue, &result) == 0);
            assert(result == bo.words[(mapping_offset + offset) / 8]);
            cases++;
        }
    }
    assert(maps == cases && unmaps == cases && cleanups == cases);
    props.wptr_gpu_addr = base | 0xffff000000000000ULL;
    assert(amdgpu_userq_fence_read_wptr(&adev, &queue, &result) == 0);
    assert(result == bo.words[512]);
    props.wptr_gpu_addr = base + 4096;
    assert(amdgpu_userq_fence_read_wptr(&adev, &queue, &result) == -EINVAL);
    props.wptr_gpu_addr = base;
    bo_va.base.bo = NULL;
    assert(amdgpu_userq_fence_read_wptr(&adev, &queue, &result) == -EINVAL);
    bo_va.base.bo = &bo;
    pd_error = -EINTR;
    assert(amdgpu_userq_fence_read_wptr(&adev, &queue, &result) == -EINTR);
    pd_error = 0;
    bo_error = -EDEADLK;
    assert(amdgpu_userq_fence_read_wptr(&adev, &queue, &result) == -EDEADLK);
    bo_error = 0;
    unsigned old_unmaps = unmaps;
    map_error = -EIO;
    assert(amdgpu_userq_fence_read_wptr(&adev, &queue, &result) == -EIO);
    assert(unmaps == old_unmaps);
    map_error = 0;
    mapping.offset = 0;
    bo.tbo.base.size = PAGE_SIZE;
    struct amdgpu_userq_mgr mgr = {0};
    assert(mes_userq_create_wptr_mapping(&adev, &mgr, &queue, base) == 0);
    assert(queue.wptr_obj.obj == &bo && bo.refs == 1);
    assert(queue.wptr_obj.gpu_addr == 0x2000000);
    struct amdgpu_bo imported = {.tbo.base = {.size = PAGE_SIZE, .imported = true}};
    bo_va.base.bo = &imported;
    unsigned old_maps = maps, old_attached = attached, old_gart = gart;
    assert(amdgpu_userq_fence_read_wptr(&adev, &queue, &result) == -EINVAL);
    assert(maps == old_maps);
    amdgpu_bo_unref(&queue.wptr_obj.obj);
    assert(mes_userq_create_wptr_mapping(&adev, &mgr, &queue, base) == -EINVAL);
    assert(imported.refs == 0 && attached == old_attached && gart == old_gart);
    bo_va.base.bo = &bo;
    assert(bo.refs == 0);
    bo.tbo.base.size = PAGE_SIZE * 2;
    assert(mes_userq_create_wptr_mapping(&adev, &mgr, &queue, base) == -EINVAL);
    assert(bo.refs == 0);
    bo.tbo.base.size = PAGE_SIZE;
    attach_error = -ENOMEM;
    assert(mes_userq_create_wptr_mapping(&adev, &mgr, &queue, base) == -ENOMEM);
    assert(bo.refs == 0);
    attach_error = 0;
    gart_error = -EIO;
    assert(mes_userq_create_wptr_mapping(&adev, &mgr, &queue, base) == -EIO);
    assert(bo.refs == 0);
    printf("UserQ WPTR: %u offsets, imported buffers and mapping failures passed\n", cases);
    return 0;
}
'''


if __name__ == '__main__':
    unittest.main()
