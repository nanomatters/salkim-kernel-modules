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


class UserqEvictionTests(unittest.TestCase):
    def test_release_preserves_completion_and_shared_fences(self):
        functions = []
        for file, name in (
                ('amdgpu_eviction_fence.c', 'amdgpu_eviction_fence_remove_all'),
                ('amdgpu_object.c', 'amdgpu_bo_release_notify')):
            source = (AMD / file).read_text()
            match = re.search(r'^void ' + name + r'\([^;]*?\)\n\{.*?^\}',
                              source, re.M | re.S)
            self.assertIsNotNone(match, name)
            functions.append(match.group())
        command = shlex.split(os.environ.get('CC', 'cc'))
        command += ['-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                    '-Wno-unused-parameter']
        if os.environ.get('USERQ_TEST_SANITIZERS'):
            command += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                        '-fno-sanitize-recover=all']
        with tempfile.TemporaryDirectory(prefix='userq-eviction-') as temporary:
            directory = pathlib.Path(temporary)
            program, binary = directory / 'test.c', directory / 'test'
            program.write_text(HARNESS.replace('ACTUAL_FUNCTIONS', '\n\n'.join(functions)))
            subprocess.run(command + [str(program), '-o', str(binary)], check=True, timeout=30)
            subprocess.run([str(binary)], check=True, timeout=30)


HARNESS = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

#define DMA_RESV_USAGE_KERNEL 0
#define DMA_RESV_USAGE_WRITE 1
#define DMA_RESV_USAGE_READ 2
#define DMA_RESV_USAGE_BOOKKEEP 3
#define TTM_PL_VRAM 1
#define AMDGPU_GEM_CREATE_VRAM_WIPE_ON_RELEASE 1
#define AMDGPU_KERNEL_JOB_ID_CLEAR_ON_RELEASE 7
static bool warn_on(bool value) { assert(!value); return value; }
#define WARN_ON(value) warn_on(value)
#define WARN_ON_ONCE(value) (value)

struct dma_fence_ops { int id; };
struct dma_fence {
    const struct dma_fence_ops *ops;
    uint64_t context;
    unsigned refs, signaling;
    bool signaled;
};
struct dma_resv {
    struct dma_fence *fences[12];
    unsigned usages[12], count;
    bool locked;
};
struct dma_resv_iter { unsigned index; struct dma_resv *resv; };
struct amdgpu_device { bool in_suspend, unplugged; };
struct resource { unsigned mem_type; bool cleared; };
struct ttm_buffer_object {
    struct { struct dma_resv _resv, *resv; } base;
    struct amdgpu_device *bdev;
    struct resource *resource;
};
struct amdgpu_bo {
    struct ttm_buffer_object tbo;
    unsigned flags;
    bool is_amdgpu, vm_bo, kfd_bo;
};
static const struct dma_fence_ops amdgpu_eviction_fence_ops = {1};
static const struct dma_fence_ops gpu_ops = {2}, pt_ops = {3}, tlb_ops = {4};
static const struct dma_fence_ops kfd_ops = {5}, clear_ops = {6}, stub_ops = {7};
static struct dma_fence stub = {.ops = &stub_ops, .refs = 1, .signaled = true};
static struct dma_fence clear_fence;
static unsigned cases, evictions, clear_calls, replacement_calls;
static struct dma_resv *expected_resv;

static void dma_resv_assert_held(struct dma_resv *resv) { assert(resv->locked); }
static struct dma_fence *dma_fence_get(struct dma_fence *f)
{ assert(f->refs); f->refs++; return f; }
static void dma_fence_put(struct dma_fence *f) { assert(f->refs); f->refs--; }
static struct dma_fence *dma_fence_get_stub(void) { return dma_fence_get(&stub); }
static struct dma_fence *next_fence(struct dma_resv_iter *it)
{
    dma_resv_assert_held(it->resv);
    return it->index < it->resv->count ? it->resv->fences[it->index++] : NULL;
}
#define dma_resv_for_each_fence(it, r, usage, f) \
    for ((it)->index = 0, (it)->resv = (r); ((f) = next_fence(it)); )

static void dma_resv_replace_fences(struct dma_resv *resv, uint64_t context,
                                    struct dma_fence *replacement, unsigned usage)
{
    assert(resv == expected_resv && usage == DMA_RESV_USAGE_BOOKKEEP);
    dma_resv_assert_held(resv);
    replacement_calls++;
    for (unsigned i = 0; i < resv->count; i++) {
        struct dma_fence *old = resv->fences[i];
        if (old->context != context) continue;
        resv->fences[i] = dma_fence_get(replacement);
        resv->usages[i] = usage;
        dma_fence_put(old);
    }
}
static void dma_resv_add_fence(struct dma_resv *resv, struct dma_fence *f, unsigned usage)
{
    dma_resv_assert_held(resv);
    assert(resv->count < 12);
    resv->fences[resv->count] = dma_fence_get(f);
    resv->usages[resv->count++] = usage;
}
static bool dma_resv_trylock(struct dma_resv *resv)
{ if (resv->locked) return false; resv->locked = true; return true; }
static void dma_resv_unlock(struct dma_resv *resv)
{ assert(resv->locked); resv->locked = false; }
static int dma_resv_reserve_fences(struct dma_resv *resv, unsigned count)
{ dma_resv_assert_held(resv); assert(count == 1); return 0; }
static struct amdgpu_device *amdgpu_ttm_adev(struct amdgpu_device *dev) { return dev; }
static struct amdgpu_bo *ttm_to_amdgpu_bo(struct ttm_buffer_object *bo)
{ return (struct amdgpu_bo *)bo; }
static bool amdgpu_bo_is_amdgpu_bo(struct ttm_buffer_object *bo)
{ return ttm_to_amdgpu_bo(bo)->is_amdgpu; }
static struct amdgpu_device *adev_to_drm(struct amdgpu_device *dev) { return dev; }
static bool drm_dev_is_unplugged(struct amdgpu_device *dev) { return dev->unplugged; }
static void amdgpu_amdkfd_release_notify(struct amdgpu_bo *bo) { assert(bo->kfd_bo); }
static void amdgpu_amdkfd_remove_all_eviction_fences(struct amdgpu_bo *bo)
{
    struct dma_resv *resv = &bo->tbo.base._resv;
    dma_resv_assert_held(resv);
    for (unsigned i = 0; i < resv->count; i++)
        if (resv->fences[i]->ops == &kfd_ops)
            dma_resv_replace_fences(resv, resv->fences[i]->context,
                                    &stub, DMA_RESV_USAGE_BOOKKEEP);
}
static struct amdgpu_device *amdgpu_ttm_next_clear_entity(struct amdgpu_device *dev)
{ return dev; }
static int amdgpu_ttm_clear_buffer(struct amdgpu_device *entity, struct amdgpu_bo *bo,
                                  struct dma_resv *resv, struct dma_fence **fence,
                                  bool delayed, unsigned job)
{
    assert(resv == expected_resv && !delayed && job == AMDGPU_KERNEL_JOB_ID_CLEAR_ON_RELEASE);
    dma_resv_assert_held(resv);
    for (unsigned i = 0; i < resv->count; i++)
        assert(resv->fences[i]->ops != &amdgpu_eviction_fence_ops);
    clear_calls++;
    clear_fence = (struct dma_fence){.ops = &clear_ops, .context = 100, .refs = 1};
    *fence = &clear_fence;
    return 0;
}
static void amdgpu_vram_mgr_set_cleared(struct resource *resource) { resource->cleared = true; }

ACTUAL_FUNCTIONS

static void flush_resv(struct dma_resv *resv)
{
    for (unsigned i = 0; i < resv->count; i++) {
        struct dma_fence *f = resv->fences[i];
        if (f->signaled) continue;
        f->signaling++;
        if (f->ops == &amdgpu_eviction_fence_ops) evictions++;
    }
}
static void drop_resv(struct dma_resv *resv)
{
    for (unsigned i = 0; i < resv->count; i++) dma_fence_put(resv->fences[i]);
    resv->count = 0;
}
static void run_case(unsigned count, bool signaled, bool same_context, unsigned mode)
{
    struct amdgpu_device device = {0};
    struct resource resource = {.mem_type = TTM_PL_VRAM};
    struct dma_resv shared = {.locked = true};
    struct amdgpu_bo bo = {.tbo = {.bdev = &device, .resource = &resource}, .is_amdgpu = true};
    struct dma_resv *private = &bo.tbo.base._resv;
    struct dma_fence fences[7] = {
        {.ops = &gpu_ops, .context = 1, .refs = 1},
        {.ops = &pt_ops, .context = 2, .refs = 1},
        {.ops = &tlb_ops, .context = 3, .refs = 1},
        {.ops = &kfd_ops, .context = 4, .refs = 1},
        {.ops = &clear_ops, .context = 5, .refs = 1},
        {.ops = &amdgpu_eviction_fence_ops, .context = 6, .refs = 1, .signaled = signaled},
        {.ops = &amdgpu_eviction_fence_ops, .context = same_context ? 6 : 7, .refs = 1, .signaled = signaled},
    };
    const unsigned usages[] = {DMA_RESV_USAGE_WRITE, DMA_RESV_USAGE_KERNEL,
                              DMA_RESV_USAGE_KERNEL, DMA_RESV_USAGE_BOOKKEEP,
                              DMA_RESV_USAGE_KERNEL, DMA_RESV_USAGE_BOOKKEEP,
                              DMA_RESV_USAGE_BOOKKEEP};
    bo.tbo.base.resv = &shared;
    private->locked = true;
    for (unsigned i = 0; i < 5 + count; i++) {
        dma_resv_add_fence(&shared, &fences[i], usages[i]);
        dma_resv_add_fence(private, &fences[i], usages[i]);
    }
    shared.locked = false;
    expected_resv = private;
    evictions = clear_calls = replacement_calls = 0;
    if (mode) {
        private->locked = false;
        if (mode == 2) bo.flags = AMDGPU_GEM_CREATE_VRAM_WIPE_ON_RELEASE;
        if (mode == 3) bo.tbo.resource = NULL;
        amdgpu_bo_release_notify(&bo.tbo);
        assert(!private->locked);
    } else {
        amdgpu_eviction_fence_remove_all(&bo);
        assert(private->locked);
    }
    assert(clear_calls == (mode == 2));
    assert(resource.cleared == (mode == 2));
    assert(bo.tbo.base.resv == &shared && !shared.locked);
    assert(private->count == 5 + count + (mode == 2));
    for (unsigned i = 0; i < 5 + count; i++) {
        bool removed = i >= 5 || (mode && i == 3);
        assert(private->fences[i] == (removed ? &stub : &fences[i]));
        assert(private->usages[i] == (removed ? DMA_RESV_USAGE_BOOKKEEP : usages[i]));
        assert(shared.fences[i] == &fences[i] && shared.usages[i] == usages[i]);
        assert(fences[i].refs == (removed ? 2u : 3u));
        assert(fences[i].signaled == (i >= 5 && signaled));
        assert(!fences[i].signaling);
    }
    if (mode == 2) {
        assert(private->fences[5 + count] == &clear_fence);
        assert(private->usages[5 + count] == DMA_RESV_USAGE_KERNEL);
        assert(clear_fence.refs == 1 && !clear_fence.signaled);
    }
    unsigned replacements = replacement_calls;
    private->locked = true;
    amdgpu_eviction_fence_remove_all(&bo);
    assert(replacement_calls == replacements);
    flush_resv(private);
    assert(!evictions);
    for (unsigned i = 5; i < 5 + count; i++) assert(!fences[i].signaling);
    drop_resv(private);
    drop_resv(&shared);
    for (unsigned i = 0; i < 7; i++) assert(fences[i].refs == 1);
    assert(stub.refs == 1);
    if (mode == 2) assert(!clear_fence.refs);
    cases++;
}
int main(void)
{
    struct amdgpu_bo empty = {0};
    expected_resv = &empty.tbo.base._resv;
    expected_resv->locked = true;
    amdgpu_eviction_fence_remove_all(&empty);
    assert(!expected_resv->count && stub.refs == 1);
    cases++;
    for (unsigned count = 0; count <= 2; count++)
        for (unsigned signaled = 0; signaled < 2; signaled++)
            for (unsigned same_context = 0; same_context < 2; same_context++)
                for (unsigned mode = 0; mode < 4; mode++)
                    run_case(count, signaled, same_context, mode);
    printf("%u UserQ BO release cases passed\n", cases);
    return 0;
}
'''


if __name__ == '__main__':
    unittest.main()
