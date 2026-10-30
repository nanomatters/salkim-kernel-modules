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


class VaReplaceTests(unittest.TestCase):
    def test_surviving_mappings_and_completion(self):
        source = (AMD / 'amdgpu_gem.c').read_text()
        names = ('amdgpu_gem_va_batch_boundary', 'amdgpu_gem_va_update_fence',
                 'amdgpu_gem_va_update_vm', 'amdgpu_gem_va_ioctl')
        functions = []
        for name in names:
            match = re.search(r'^(?:static )?(?:int|struct \w+ \*)\s*' + name
                              + r'\(.*?^\}', source, re.M | re.S)
            self.assertIsNotNone(match, name)
            functions.append(match.group())
        flags = shlex.split(os.environ.get('CC', 'cc'))
        flags += ['-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                  '-Wno-unused-parameter', '-Wno-sign-compare',
                  '-I' + str(ROOT / 'amdgpu/include/uapi/drm')]
        flags += shlex.split(subprocess.check_output(
            ['pkg-config', '--cflags', 'libdrm'], text=True))
        if os.environ.get('USERQ_TEST_SANITIZERS'):
            flags += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                      '-fno-sanitize-recover=all']
        with tempfile.TemporaryDirectory(prefix='va-replace-test-') as temporary:
            path = pathlib.Path(temporary)
            program, binary = path / 'test.c', path / 'test'
            program.write_text(HARNESS.replace('ACTUAL_FUNCTIONS', '\n'.join(functions)))
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
typedef uint64_t u64;
typedef uint32_t u32;
#define AMDGPU_GPU_PAGE_SIZE 4096ULL
#define AMDGPU_VA_RESERVED_BOTTOM 4096ULL
#define AMDGPU_VA_RESERVED_TOP 4096ULL
#define AMDGPU_GMC_HOLE_START 0x0000800000000000ULL
#define AMDGPU_GMC_HOLE_END 0xffff800000000000ULL
#define AMDGPU_GMC_HOLE_MASK 0x0000ffffffffffffULL
#define ERESTARTSYS 512
#define DRM_EXEC_INTERRUPTIBLE_WAIT 1
#define DRM_EXEC_IGNORE_DUPLICATES 2
#define ARRAY_SIZE(x) (sizeof(x) / sizeof((x)[0]))
#define check_add_overflow __builtin_add_overflow
#define dev_dbg(...) ((void)0)
#define DRM_ERROR(...) ((void)0)
#define unlikely(x) (x)
struct dma_fence { unsigned mask, waits, refs; };
struct drm_gem_object { unsigned id, refs; bool locked; };
struct amdgpu_bo { struct { struct drm_gem_object base; } tbo; bool always_valid; };
struct amdgpu_bo_va { struct { struct amdgpu_bo *bo; } base;
                      struct dma_fence *last_pt_update; unsigned id, updates; };
struct amdgpu_bo_va_mapping { u64 start, last; struct amdgpu_bo_va *bo_va; };
struct amdgpu_vm { struct dma_fence *last_update, *last_tlb_inv; };
struct amdgpu_device { struct { u64 max_pfn; } vm_manager; bool debug_vm; };
struct amdgpu_fpriv { struct amdgpu_vm vm; struct amdgpu_bo_va *prt_va; };
struct drm_device { void *dev; };
struct drm_file { struct amdgpu_fpriv *driver_priv; };
struct drm_syncobj { unsigned refs; };
struct dma_fence_chain { unsigned unused; };
struct drm_exec { bool first, contended; };
static struct amdgpu_device device;
static struct amdgpu_fpriv private;
static struct drm_syncobj syncobj;
static struct dma_fence_chain chain;
static struct amdgpu_bo bos[3];
static struct amdgpu_bo_va vas[4];
static struct amdgpu_bo_va_mapping mappings[2];
static struct dma_fence fences[6], merged, stub;
static bool pd_locked, mutated, merge_fail, vm_ready;
static unsigned cases, lookups, maps, published, published_mask, clear_updates, pde_updates;
static unsigned lock_calls, contended_id, lock_error_id, update_error_id;
static bool bad_syncobj;
#define drm_exec_until_all_locked(e) \
    for (; drm_exec_cleanup(e);) if (false) { retry: continue; } else
#define drm_exec_retry_on_contention(e) do { if ((e)->contended) goto retry; } while (0)
static void unlock_all(void)
{
    pd_locked = false;
    for (unsigned i = 0; i < 3; ++i)
        bos[i].tbo.base.locked = false;
}
static void drm_exec_init(struct drm_exec *exec, unsigned flags, unsigned count)
{ assert(!pd_locked); exec->first = true; exec->contended = false; }
static bool drm_exec_cleanup(struct drm_exec *exec)
{
    if (exec->first) { exec->first = false; return true; }
    if (!exec->contended)
        return false;
    unlock_all();
    exec->contended = false;
    return true;
}
static void drm_exec_fini(struct drm_exec *exec) { unlock_all(); }
static int drm_exec_lock_obj(struct drm_exec *exec, struct drm_gem_object *obj)
{
    ++lock_calls;
    if (obj->id == contended_id) {
        contended_id = 99;
        exec->contended = true;
        return -EDEADLK;
    }
    if (obj->id == lock_error_id)
        return -EINTR;
    obj->locked = true;
    return 0;
}
static int amdgpu_vm_lock_pd(struct amdgpu_vm *vm, struct drm_exec *exec, unsigned slots)
{ assert(slots == 2); pd_locked = true; return 0; }
static struct amdgpu_device *drm_to_adev(struct drm_device *dev) { return &device; }
static struct drm_gem_object *drm_gem_object_lookup(struct drm_file *file, uint32_t handle)
{
    if (handle != 1)
        return NULL;
    bos[0].tbo.base.refs++;
    return &bos[0].tbo.base;
}
static struct amdgpu_bo *gem_to_amdgpu_bo(struct drm_gem_object *obj)
{ return &bos[obj->id]; }
static void drm_gem_object_put(struct drm_gem_object *obj)
{ if (obj) { assert(obj->refs); obj->refs--; } }
static bool amdgpu_vm_is_bo_always_valid(struct amdgpu_vm *vm, struct amdgpu_bo *bo)
{ return bo && bo->always_valid; }
static int amdgpu_vm_verify_parameters(struct amdgpu_device *dev, struct amdgpu_bo *bo,
                                       u64 address, u64 offset, u64 size)
{
    u64 sum;
    if (!size || ((address | offset | size) & 4095) ||
        check_add_overflow(address, size, &sum) ||
        check_add_overflow(offset, size, &sum) || (bo && sum > 64 * 4096ULL))
        return -EINVAL;
    return 0;
}
static struct amdgpu_bo_va_mapping *amdgpu_vm_bo_lookup_mapping(struct amdgpu_vm *vm, u64 address)
{
    assert(pd_locked && !mutated);
    ++lookups;
    address /= 4096;
    for (unsigned i = 0; i < maps; ++i)
        if (address >= mappings[i].start && address <= mappings[i].last)
            return &mappings[i];
    return NULL;
}
static struct amdgpu_bo_va *amdgpu_vm_bo_find(struct amdgpu_vm *vm, struct amdgpu_bo *bo)
{ assert(pd_locked && bo->tbo.base.locked); return &vas[bo->tbo.base.id]; }
static int amdgpu_gem_add_input_fence(struct drm_file *file, u64 handles, uint32_t count)
{ return 0; }
static int amdgpu_gem_update_timeline_node(struct drm_file *file, uint32_t handle, u64 point,
                                          struct drm_syncobj **obj, struct dma_fence_chain **node)
{
    if (!handle)
        return 0;
    if (bad_syncobj)
        return -ENOENT;
    syncobj.refs++;
    *obj = &syncobj;
    *node = point ? &chain : NULL;
    return 0;
}
static void drm_syncobj_put(struct drm_syncobj *obj) { assert(obj->refs); obj->refs--; }
static void dma_fence_chain_free(struct dma_fence_chain *node) {}
static struct dma_fence *dma_fence_get_stub(void) { stub.refs++; return &stub; }
static void dma_fence_put(struct dma_fence *f) { assert(f->refs); f->refs--; }
static int dma_fence_wait(struct dma_fence *f, bool interruptible)
{ assert(!interruptible); f->waits++; return 0; }
static struct dma_fence *merge(unsigned count, struct dma_fence **array)
{
    if (merge_fail)
        return NULL;
    merged.mask = 0;
    merged.refs = 1;
    for (unsigned i = 0; i < count; ++i)
        if (array[i]) merged.mask |= array[i]->mask;
    return &merged;
}
#define dma_fence_unwrap_merge(...) merge(ARRAY_SIZE(((struct dma_fence *[]){__VA_ARGS__})), \
                                           (struct dma_fence *[]){__VA_ARGS__})
static bool amdgpu_vm_ready(struct amdgpu_vm *vm) { return vm_ready; }
static int amdgpu_vm_clear_freed(struct amdgpu_device *dev, struct amdgpu_vm *vm,
                                struct dma_fence **out)
{ assert(pd_locked && mutated); clear_updates++; return 0; }
static int amdgpu_vm_bo_update(struct amdgpu_device *dev, struct amdgpu_bo_va *va, bool clear)
{
    assert(pd_locked && mutated && !clear && clear_updates && !pde_updates);
    if (va->base.bo)
        assert(va->base.bo->tbo.base.locked);
    va->updates++;
    return va->id == update_error_id ? -ENOMEM : 0;
}
static int amdgpu_vm_update_pdes(struct amdgpu_device *dev, struct amdgpu_vm *vm, bool immediate)
{ assert(pd_locked && mutated && clear_updates); pde_updates++; return 0; }
static int amdgpu_vm_bo_map(struct amdgpu_device *dev, struct amdgpu_bo_va *va,
                            u64 address, u64 offset, u64 size, uint32_t flags)
{ assert(pd_locked); mutated = true; return 0; }
static int amdgpu_vm_bo_unmap(struct amdgpu_device *dev, struct amdgpu_bo_va *va, u64 address)
{ assert(pd_locked); mutated = true; return 0; }
static int amdgpu_vm_bo_clear_mappings(struct amdgpu_device *dev, struct amdgpu_vm *vm,
                                       u64 address, u64 size)
{ assert(pd_locked); mutated = true; return 0; }
static int amdgpu_vm_bo_replace_map(struct amdgpu_device *dev, struct amdgpu_bo_va *va,
                                    u64 address, u64 offset, u64 size, uint32_t flags)
{ assert(pd_locked); mutated = true; return 0; }
static void drm_syncobj_replace_fence(struct drm_syncobj *obj, struct dma_fence *f)
{ assert(pd_locked); published++; published_mask = f->mask; }
static void drm_syncobj_add_point(struct drm_syncobj *obj, struct dma_fence_chain *node,
                                  struct dma_fence *f, u64 point)
{ assert(node && point); drm_syncobj_replace_fence(obj, f); }
ACTUAL_FUNCTIONS
static struct drm_amdgpu_gem_va reset(void)
{
    memset(bos, 0, sizeof(bos));
    memset(vas, 0, sizeof(vas));
    memset(fences, 0, sizeof(fences));
    memset(&syncobj, 0, sizeof(syncobj));
    memset(&merged, 0, sizeof(merged));
    memset(&stub, 0, sizeof(stub));
    for (unsigned i = 0; i < 6; ++i) fences[i].mask = 1u << i;
    for (unsigned i = 0; i < 4; ++i) {
        vas[i].id = i;
        vas[i].last_pt_update = &fences[i + 2];
        if (i < 3) { bos[i].tbo.base.id = i; vas[i].base.bo = &bos[i]; }
    }
    device.vm_manager.max_pfn = 4096;
    device.debug_vm = false;
    private.vm.last_update = &fences[0];
    private.vm.last_tlb_inv = &fences[1];
    private.prt_va = &vas[3];
    mappings[0] = (struct amdgpu_bo_va_mapping){16, 31, &vas[1]};
    maps = 1;
    mutated = pd_locked = merge_fail = bad_syncobj = false;
    vm_ready = true;
    lookups = published = published_mask = clear_updates = pde_updates = lock_calls = 0;
    contended_id = lock_error_id = update_error_id = 99;
    return (struct drm_amdgpu_gem_va){
        .handle = 1, .operation = AMDGPU_VA_OP_REPLACE,
        .va_address = 20 * 4096, .map_size = 8 * 4096,
        .flags = AMDGPU_VM_PAGE_READABLE,
        .vm_timeline_syncobj_out = 1, .vm_timeline_point = UINT64_C(0x100000001),
    };
}
static int invoke(struct drm_amdgpu_gem_va *args)
{
    struct drm_file file = {.driver_priv = &private};
    struct drm_device dev = {0};
    int result = amdgpu_gem_va_ioctl(&dev, args, &file);
    assert(!pd_locked && !syncobj.refs && !merged.refs && !stub.refs);
    for (unsigned i = 0; i < 3; ++i)
        assert(!bos[i].tbo.base.refs && !bos[i].tbo.base.locked);
    ++cases;
    return result;
}
int main(void)
{
    for (unsigned clear = 0; clear < 2; ++clear) {
        for (unsigned prt = 0; prt < 2; ++prt) {
            for (unsigned split = 0; split < 2; ++split) {
                struct drm_amdgpu_gem_va a = reset();
                if (clear) { a.operation = AMDGPU_VA_OP_CLEAR; a.offset_in_bo = 3; }
                if (prt) mappings[0].bo_va = &vas[3];
                if (split) {
                    mappings[0].last = 23;
                    mappings[1] = (struct amdgpu_bo_va_mapping){24, 31, &vas[2]};
                    maps = 2;
                }
                assert(invoke(&a) == 0 && mutated && published == 1 && pde_updates == 1);
                assert(vas[0].updates == !clear);
                assert(vas[prt ? 3 : 1].updates == 1 && vas[2].updates == split);
                unsigned expected = 3 | (!clear ? 4 : 0) | (prt ? 32 : 8) | (split ? 16 : 0);
                assert(published_mask == expected && lookups == 2);
            }
        }
    }
    struct drm_amdgpu_gem_va a = reset();
    a.flags = AMDGPU_VM_PAGE_PRT;
    a.handle = 0;
    assert(invoke(&a) == 0 && vas[3].updates == 1 && vas[1].updates == 1);
    assert(published_mask == (3 | 32 | 8));
    a = reset(); mappings[0].bo_va = &vas[0];
    assert(invoke(&a) == 0 && vas[0].updates == 1 && published_mask == 7);
    a = reset(); mappings[0].start = 20; mappings[0].last = 27;
    assert(invoke(&a) == 0 && !vas[1].updates && published_mask == 7);
    a = reset(); a.vm_timeline_syncobj_out = 0;
    assert(invoke(&a) == 0 && !lookups && !vas[1].updates && !published);
    a = reset(); a.vm_timeline_point = 0;
    assert(invoke(&a) == 0 && vas[1].updates == 1 && published == 1);
    a = reset(); a.operation = AMDGPU_VA_OP_MAP;
    assert(invoke(&a) == 0 && !lookups && !vas[1].updates && published_mask == 7);
    a = reset(); a.flags |= AMDGPU_VM_DELAY_UPDATE;
    assert(invoke(&a) == -EINVAL && !mutated && !lookups && !lock_calls);
    for (unsigned invalid = 0; invalid < 5; ++invalid) {
        a = reset();
        if (invalid == 0) a.map_size = 0;
        if (invalid == 1) a.map_size++;
        if (invalid == 2) a.offset_in_bo++;
        if (invalid == 3) a.map_size = UINT64_MAX;
        if (invalid == 4) a.offset_in_bo = 64 * 4096;
        assert(invoke(&a) == -EINVAL && !mutated && !lookups && !lock_calls);
    }
    a = reset(); bad_syncobj = true;
    assert(invoke(&a) == -ENOENT && !mutated && !published);
    a = reset(); lock_error_id = 1;
    assert(invoke(&a) == -EINTR && !mutated && !published);
    a = reset(); contended_id = 1;
    assert(invoke(&a) == 0 && mutated && lookups == 3 && published_mask == 15);
    a = reset(); merge_fail = true;
    assert(invoke(&a) == 0 && published == 1 && published_mask == 0);
    assert(fences[0].waits && fences[1].waits && fences[2].waits && fences[3].waits);
    a = reset(); update_error_id = 1;
    assert(invoke(&a) == 0 && mutated && !pde_updates && published_mask == 15);
    a = reset(); vm_ready = false;
    assert(invoke(&a) == 0 && mutated && !clear_updates && !pde_updates && published == 1);
    printf("GEM_VA surviving mappings: %u cases passed\n", cases);
    return 0;
}
'''


if __name__ == '__main__':
    unittest.main()
