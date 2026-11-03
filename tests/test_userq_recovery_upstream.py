#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
import os
import pathlib
import re
import shlex
import subprocess
import tempfile
import unittest


AMD = 'drivers/gpu/drm/amd/amdgpu/'
for ROOT in pathlib.Path(__file__).resolve().parents:
    if (ROOT / AMD / 'amdgpu_userq.c').is_file():
        break
    if (ROOT / 'amdgpu' / AMD / 'amdgpu_userq.c').is_file():
        AMD = 'amdgpu/' + AMD
        break
else:
    raise RuntimeError('AMDGPU source tree not found')


def source(file):
    revision = os.environ.get('USERQ_TEST_REVISION')
    if revision:
        return subprocess.check_output(['git', 'show', revision + ':' + AMD + file],
                                       cwd=ROOT, text=True)
    return (ROOT / AMD / file).read_text()


def function(file, name):
    match = re.search(r'^(?:static )?(?:int|void)\s+' + name + r'\([^;]*?\)\n\{.*?^\}',
                      source(file), re.M | re.S)
    if match is None:
        raise AssertionError(name)
    return match.group()


def run_program(body):
    flags = shlex.split(os.environ.get('CC', 'cc'))
    flags += ['-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
              '-Wno-unused-parameter']
    if os.environ.get('USERQ_TEST_SANITIZERS'):
        flags += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer']
    with tempfile.TemporaryDirectory(prefix='amdgpu-core-upstream-') as temporary:
        directory = pathlib.Path(temporary)
        program, binary = directory / 'test.c', directory / 'test'
        program.write_text(body)
        subprocess.run(flags + [str(program), '-o', str(binary)], check=True, timeout=30)
        subprocess.run([str(binary)], check=True, timeout=30)


class CoreUpstreamTests(unittest.TestCase):
    def test_gang_dependency_ownership(self):
        body = function('amdgpu_cs.c', 'amdgpu_cs_submit')
        start = body.index('\tfor (i = 0; i < p->gang_size; ++i) {')
        end = body.index('\n\tif (p->gang_size > 1)', start)
        run_program(GANG_HARNESS.replace('ACTUAL_DEPENDENCIES', body[start:end]))

    def test_idle_buffer_validation(self):
        body = function('amdgpu_userq.c', 'amdgpu_userq_bo_validate')
        run_program(IDLE_HARNESS.replace('ACTUAL_IDLE_VALIDATION', body))

    def test_reset_mapping_rewrite(self):
        reset = function('amdgpu_vm.c', 'amdgpu_vm_bo_reset_state_machine')
        update = function('amdgpu_vm.c', 'amdgpu_vm_bo_needs_update')
        body = function('amdgpu_vm.c', 'amdgpu_vm_bo_update_internal')
        start = body.index('\tif (!clear && bo_va->base.moved)')
        end = body.index('\n\tbatching =', start)
        harness = RESET_HARNESS.replace('ACTUAL_NEEDS_UPDATE', update).replace('ACTUAL_RESET', reset)
        run_program(harness.replace('ACTUAL_INVALIDATE', body[start:end]))


GANG_HARNESS = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
struct dma_fence { unsigned refs; };
struct drm_sched_fence { struct dma_fence scheduled; };
struct drm_sched_job {
    struct drm_sched_fence *s_fence;
    struct dma_fence *dependencies[8];
    unsigned count;
};
struct amdgpu_job { struct drm_sched_job base; };
struct amdgpu_cs_parser {
    struct amdgpu_job *jobs[8], *gang_leader;
    unsigned gang_size;
};
static unsigned calls, fail_at;
static void dma_fence_get(struct dma_fence *fence) { assert(fence->refs); fence->refs++; }
static void dma_fence_put(struct dma_fence *fence) { assert(fence->refs); fence->refs--; }
static int drm_sched_job_add_dependency(struct drm_sched_job *job, struct dma_fence *fence)
{
    /* The scheduler consumes the reference on both success and failure. */
    if (++calls == fail_at) {
        dma_fence_put(fence);
        return -ENOMEM;
    }
    assert(job->count < 8);
    job->dependencies[job->count++] = fence;
    return 0;
}
static int add_dependencies(struct amdgpu_cs_parser *p)
{
    struct amdgpu_job *leader = p->gang_leader;
    unsigned i;
    int r;
ACTUAL_DEPENDENCIES
    return 0;
}
int main(void)
{
    unsigned cases = 0;
    for (unsigned count = 1; count <= 8; count++) {
        for (unsigned leader = 0; leader < count; leader++) {
            for (unsigned fail = 0; fail < count; fail++) {
                struct amdgpu_job jobs[8] = {0};
                struct drm_sched_fence fences[8] = {0};
                struct amdgpu_cs_parser parser = {.gang_size = count, .gang_leader = &jobs[leader]};
                calls = 0;
                fail_at = fail;
                for (unsigned i = 0; i < count; i++) {
                    fences[i].scheduled.refs = 1;
                    jobs[i].base.s_fence = &fences[i];
                    parser.jobs[i] = &jobs[i];
                }
                assert(add_dependencies(&parser) == (fail ? -ENOMEM : 0));
                assert(calls == (fail ? fail : count - 1));
                struct drm_sched_job *owner = &jobs[leader].base;
                assert(owner->count == (fail ? fail - 1 : count - 1));
                for (unsigned i = 0; i < owner->count; i++)
                    dma_fence_put(owner->dependencies[i]);
                for (unsigned i = 0; i < count; i++) {
                    assert(fences[i].scheduled.refs == 1);
                    dma_fence_put(&fences[i].scheduled);
                }
                cases++;
            }
        }
    }
    printf("Gang dependencies: %u success and allocation-failure cases passed\n", cases);
    return 0;
}
'''


IDLE_HARNESS = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#define likely(value) (value)
#define unlikely(value) (value)
#define TTM_NUM_MOVE_FENCES 2
#define AMDGPU_GEM_DOMAIN_CPU 1
#define AMDGPU_GEM_DOMAIN_GTT 2
#define TTM_PL_SYSTEM 0
#define TTM_PL_TT 1
#define TTM_PL_VRAM 2
#define MAX_BOS 8
#define container_of(ptr, type, field) ((type *)((char *)(ptr) - offsetof(type, field)))
struct list_head { struct list_head *next, *prev; };
static void list_init(struct list_head *head) { head->next = head->prev = head; }
static bool list_empty(const struct list_head *head) { return head->next == head; }
static void list_add_tail(struct list_head *node, struct list_head *head)
{
    node->next = head;
    node->prev = head->prev;
    head->prev->next = node;
    head->prev = node;
}
static void list_move(struct list_head *node, struct list_head *head)
{
    node->prev->next = node->next;
    node->next->prev = node->prev;
    list_add_tail(node, head);
}
#define list_first_entry(head, type, field) container_of((head)->next, type, field)
#define list_for_each_entry(pos, head, field) \
    for (struct list_head *node = (head)->next; node != (head) && \
         ((pos) = container_of(node, __typeof__(*(pos)), field), true); node = node->next)
struct ttm_resource { unsigned mem_type; };
struct ttm_tt { bool userptr; };
struct drm_gem_object { bool held; };
struct ttm_buffer_object { struct drm_gem_object base; struct ttm_resource *resource; struct ttm_tt *ttm; };
struct amdgpu_bo {
    struct ttm_buffer_object tbo;
    unsigned allowed_domains, refs, validations, updates, id;
    unsigned placement;
};
struct amdgpu_vm_bo_base { struct amdgpu_bo *bo; struct list_head vm_status; };
struct amdgpu_bo_va { struct amdgpu_vm_bo_base base; bool mapped; };
struct amdgpu_vm { struct { struct list_head idle, evicted, needs_update; } individual; bool individual_lock; };
struct amdgpu_device { int unused; };
struct drm_exec { struct amdgpu_bo *locked[MAX_BOS]; unsigned count; };
struct ttm_operation_ctx { bool interruptible, no_wait_gpu; };
static struct amdgpu_device device;
static struct amdgpu_vm vm;
static struct amdgpu_bo bos[MAX_BOS];
static struct amdgpu_bo_va vas[MAX_BOS];
static struct ttm_resource resources[MAX_BOS];
static struct ttm_tt tt[MAX_BOS];
static struct drm_exec exec;
static int lock_error, validate_error, update_error;
static unsigned error_bo, steps, moves_on_lock;
static bool move_on_lock;
static void spin_lock(bool *lock) { assert(!*lock); *lock = true; }
static void spin_unlock(bool *lock) { assert(*lock); *lock = false; }
static inline bool amdgpu_ttm_tt_is_userptr(struct ttm_tt *value) { return value && value->userptr; }
static struct amdgpu_bo *amdgpu_bo_ref(struct amdgpu_bo *bo)
{
    assert(vm.individual_lock && bo->refs);
    bo->refs++;
    return bo;
}
static void amdgpu_bo_unref(struct amdgpu_bo **bo)
{
    assert(!vm.individual_lock && (*bo)->refs > 1);
    (*bo)->refs--;
    *bo = NULL;
}
static int drm_exec_prepare_obj(struct drm_exec *owner, struct drm_gem_object *object, unsigned fences)
{
    struct amdgpu_bo *bo = container_of(object, struct amdgpu_bo, tbo.base);
    assert(!vm.individual_lock && fences == TTM_NUM_MOVE_FENCES + 1);
    assert(bo->refs >= 2 && ++steps < 100);
    if (move_on_lock && bo->id == error_bo) {
        move_on_lock = false;
        moves_on_lock++;
        list_move(&vas[bo->id].base.vm_status, &vm.individual.needs_update);
    }
    if (lock_error && bo->id == error_bo)
        return lock_error;
    if (!object->held) {
        object->held = true;
        bo->refs++;
        assert(owner->count < MAX_BOS);
        owner->locked[owner->count++] = bo;
    }
    return 0;
}
static void amdgpu_bo_placement_from_domain(struct amdgpu_bo *bo, unsigned domains)
{
    assert(!vm.individual_lock && bo->tbo.base.held);
    bo->placement = domains;
}
static int ttm_bo_validate(struct ttm_buffer_object *object, unsigned *placement, struct ttm_operation_ctx *ctx)
{
    struct amdgpu_bo *bo = container_of(object, struct amdgpu_bo, tbo);
    assert(!vm.individual_lock && object->base.held && bo->refs >= 2);
    assert(placement == &bo->placement && !ctx->no_wait_gpu);
    bo->validations++;
    if (validate_error && bo->id == error_bo)
        return validate_error;
    if (!object->resource || object->resource->mem_type == TTM_PL_SYSTEM) {
        object->resource = &resources[bo->id];
        object->resource->mem_type = TTM_PL_TT;
        list_move(&vas[bo->id].base.vm_status, &vm.individual.needs_update);
    }
    return 0;
}
static int amdgpu_vm_bo_update(struct amdgpu_device *adev, struct amdgpu_bo_va *va, bool clear)
{
    struct amdgpu_bo *bo = va->base.bo;
    assert(adev == &device && !vm.individual_lock && !clear && bo->tbo.base.held);
    assert(bo->tbo.resource && bo->tbo.resource->mem_type != TTM_PL_SYSTEM);
    bo->updates++;
    if (update_error && bo->id == error_bo)
        return update_error;
    va->mapped = true;
    list_move(&va->base.vm_status, &vm.individual.idle);
    return 0;
}
ACTUAL_IDLE_VALIDATION
static void setup(void)
{
    memset(&vm, 0, sizeof(vm));
    memset(&exec, 0, sizeof(exec));
    memset(bos, 0, sizeof(bos));
    memset(vas, 0, sizeof(vas));
    memset(tt, 0, sizeof(tt));
    list_init(&vm.individual.idle);
    list_init(&vm.individual.evicted);
    list_init(&vm.individual.needs_update);
    for (unsigned i = 0; i < MAX_BOS; i++) {
        resources[i].mem_type = TTM_PL_TT;
        bos[i] = (struct amdgpu_bo){.id = i, .refs = 1, .allowed_domains = AMDGPU_GEM_DOMAIN_GTT,
                 .tbo = {.resource = &resources[i], .ttm = &tt[i]}};
        vas[i].base.bo = &bos[i];
        list_add_tail(&vas[i].base.vm_status, &vm.individual.idle);
    }
    lock_error = validate_error = update_error = 0;
    steps = error_bo = moves_on_lock = 0;
    move_on_lock = false;
}
static void finish(void)
{
    assert(!vm.individual_lock);
    for (unsigned i = 0; i < exec.count; i++) {
        exec.locked[i]->tbo.base.held = false;
        assert(exec.locked[i]->refs == 2);
        exec.locked[i]->refs--;
    }
    for (unsigned i = 0; i < MAX_BOS; i++)
        assert(bos[i].refs == 1 && !bos[i].tbo.base.held);
    exec.count = 0;
}
static void missing_storage(unsigned id, bool discarded)
{
    resources[id].mem_type = TTM_PL_SYSTEM;
    if (discarded)
        bos[id].tbo.resource = NULL;
}
static void success(void)
{
    assert(amdgpu_userq_bo_validate(&device, &exec, &vm) == 0);
    assert(!vm.individual_lock);
}
int main(void)
{
    unsigned cases = 0;
    setup();
    missing_storage(0, false);
    missing_storage(1, true);
    missing_storage(2, false);
    bos[2].allowed_domains |= AMDGPU_GEM_DOMAIN_CPU;
    missing_storage(3, false);
    tt[3].userptr = true;
    resources[4].mem_type = TTM_PL_VRAM;
    vas[5].base.bo = NULL;
    missing_storage(6, true);
    bos[6].tbo.ttm = NULL;
    success();
    for (unsigned i = 0; i < MAX_BOS; i++) {
        bool expected = i == 0 || i == 1 || i == 6;
        assert(vas[i].mapped == expected);
        assert(bos[i].updates == (expected ? 1u : 0u));
    }
    finish();
    cases++;

    for (unsigned state = 0; state < 3; state++) {
        for (unsigned discarded = 0; discarded < 2; discarded++) {
            setup();
            missing_storage(0, discarded);
            if (state)
                list_move(&vas[0].base.vm_status, state == 1 ? &vm.individual.evicted : &vm.individual.needs_update);
            success();
            assert(vas[0].mapped && bos[0].updates == 1);
            finish();
            cases++;
        }
    }
    for (unsigned failure = 0; failure < 4; failure++) {
        setup();
        missing_storage(0, true);
        if (failure == 0) lock_error = -EDEADLK;
        if (failure == 1) lock_error = -ENOMEM;
        if (failure == 2) validate_error = -EBUSY;
        if (failure == 3) update_error = -ENOMEM;
        int expected = lock_error ? lock_error : (validate_error ? validate_error : update_error);
        assert(amdgpu_userq_bo_validate(&device, &exec, &vm) == expected);
        assert(!vas[0].mapped);
        finish();
        lock_error = validate_error = update_error = 0;
        success();
        assert(vas[0].mapped);
        finish();
        cases++;
    }
    setup();
    for (unsigned i = 0; i < MAX_BOS; i++)
        missing_storage(i, i % 2);
    move_on_lock = true;
    success();
    assert(moves_on_lock == 1);
    for (unsigned i = 0; i < MAX_BOS; i++)
        assert(vas[i].mapped && bos[i].updates == 1);
    finish();
    cases++;
    printf("UserQ idle restore: %u validation, retry and list mutation cases passed\n", cases);
    return 0;
}
'''


RESET_HARNESS = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#define container_of(ptr, type, field) ((type *)((char *)(ptr) - offsetof(type, field)))
struct list_head { struct list_head *next, *prev; };
static void list_init(struct list_head *head) { head->next = head->prev = head; }
static bool list_empty(const struct list_head *head) { return head->next == head; }
static void list_add(struct list_head *node, struct list_head *head)
{
    node->next = head->next;
    node->prev = head;
    head->next->prev = node;
    head->next = node;
}
static void list_move(struct list_head *node, struct list_head *head)
{
    node->prev->next = node->next;
    node->next->prev = node->prev;
    list_add(node, head);
}
static void list_splice_init(struct list_head *from, struct list_head *to)
{
    while (!list_empty(from))
        list_move(from->prev, to);
}
#define list_for_each_entry(pos, head, field) \
    for (struct list_head *node = (head)->next; node != (head) && \
         ((pos) = container_of(node, __typeof__(*(pos)), field), true); node = node->next)
#define list_for_each_entry_safe(pos, tmp, head, field) \
    for (struct list_head *node = (head)->next, *next = node->next; node != (head) && \
         ((pos) = container_of(node, __typeof__(*(pos)), field), \
          (tmp) = next == (head) ? NULL : container_of(next, __typeof__(*(tmp)), field), (void)(tmp), true); \
         node = next, next = node->next)
struct amdgpu_bo { int unused; };
struct amdgpu_vm_bo_status { struct list_head idle, needs_update, evicted; };
struct amdgpu_vm {
    struct { struct amdgpu_bo *bo; } root;
    struct amdgpu_vm_bo_status kernel, always_valid, individual;
    bool individual_lock, root_locked;
};
struct amdgpu_vm_bo_base {
    struct list_head vm_status;
    struct amdgpu_bo *bo;
    struct amdgpu_vm *vm;
    struct amdgpu_vm_bo_status *lists;
    bool moved;
};
struct amdgpu_bo_va {
    struct amdgpu_vm_bo_base base;
    struct list_head valids, invalids;
    bool cleared;
};
static void spin_lock(bool *lock) { assert(!*lock); *lock = true; }
static void spin_unlock(bool *lock) { assert(*lock); *lock = false; }
static void amdgpu_vm_assert_locked(struct amdgpu_vm *vm) { assert(vm->root_locked); }
static struct amdgpu_vm_bo_status *amdgpu_vm_bo_lock_lists(struct amdgpu_vm_bo_base *base)
{
    assert(base->vm->root_locked);
    return base->lists;
}
static void amdgpu_vm_bo_unlock_lists(struct amdgpu_vm_bo_base *base) { assert(base->vm->root_locked); }
ACTUAL_NEEDS_UPDATE
ACTUAL_RESET
static void invalidate_mappings(struct amdgpu_bo_va *bo_va)
{
    bool clear = false, flush_tlb = false;
ACTUAL_INVALIDATE
    assert(flush_tlb);
}
static unsigned list_count(struct list_head *head)
{
    unsigned count = 0;
    for (struct list_head *node = head->next; node != head; node = node->next) {
        assert(node->next->prev == node && node->prev->next == node);
        assert(++count <= 20);
    }
    return count;
}
int main(void)
{
    struct amdgpu_vm vm = {.root_locked = true};
    struct amdgpu_vm_bo_status *groups[] = {&vm.kernel, &vm.always_valid, &vm.individual};
    struct amdgpu_bo objects[10] = {0};
    struct amdgpu_bo_va mappings[10] = {0};
    struct list_head ranges[10][3];
    for (unsigned g = 0; g < 3; g++) {
        list_init(&groups[g]->idle);
        list_init(&groups[g]->needs_update);
        list_init(&groups[g]->evicted);
    }
    vm.root.bo = &objects[9];
    for (unsigned i = 0; i < 10; i++) {
        struct amdgpu_bo_va *va = &mappings[i];
        va->base.bo = &objects[i];
        va->base.vm = &vm;
        va->base.lists = groups[i == 9 ? 0 : i / 3];
        list_init(&va->valids);
        list_init(&va->invalids);
        list_add(&ranges[i][0], &va->valids);
        list_add(&ranges[i][1], &va->valids);
        list_add(&ranges[i][2], &va->invalids);
        struct amdgpu_vm_bo_status *group = va->base.lists;
        struct list_head *state = i % 3 == 0 ? &group->idle :
                                  i % 3 == 1 ? &group->needs_update : &group->evicted;
        list_add(&va->base.vm_status, state);
    }
    for (unsigned pass = 0; pass < 2; pass++) {
        /* A full rewrite is needed even if some mappings were already pending. */
        amdgpu_vm_bo_reset_state_machine(&vm);
        assert(!vm.individual_lock && vm.root_locked && !mappings[9].base.moved);
        assert(list_count(&vm.kernel.idle) == 1 && vm.kernel.idle.next == &mappings[9].base.vm_status);
        for (unsigned g = 0; g < 3; g++) {
            assert(list_count(&groups[g]->needs_update) == 2);
            assert(list_count(&groups[g]->evicted) == 1);
            for (unsigned state = 0; state < 3; state++) {
                struct amdgpu_bo_va *va = &mappings[g * 3 + state];
                assert(va->base.moved == (state != 2));
                if (state == 2)
                    continue;
                invalidate_mappings(va);
                assert(list_count(&va->valids) == 0 && list_count(&va->invalids) == 3);
                list_splice_init(&va->invalids, &va->valids);
                va->base.moved = false;
                list_move(&va->base.vm_status, &groups[g]->idle);
            }
        }
    }
    puts("VM reset: pending and idle mappings, root PD and repeated reset cases passed");
    return 0;
}
'''


if __name__ == '__main__':
    unittest.main()
