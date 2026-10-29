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


class UserqRestoreTests(unittest.TestCase):
    def test_moved_bo_reservations(self):
        def function(file, name):
            revision = os.environ.get('USERQ_TEST_REVISION')
            if revision:
                source = subprocess.check_output(['git', 'show', revision + ':' + AMD + file],
                                                 cwd=ROOT, text=True)
            else:
                source = (ROOT / AMD / file).read_text()
            match = re.search(r'^(?:static )?int\s+' + name + r'\(.*?^\}', source, re.M | re.S)
            self.assertIsNotNone(match, name)
            return match.group()

        source = HARNESS.replace('ACTUAL_HANDLE_MOVED', function('amdgpu_vm.c', 'amdgpu_vm_handle_moved'))
        source = source.replace('ACTUAL_VALIDATE', function('amdgpu_userq.c', 'amdgpu_userq_bo_validate'))
        source = source.replace('ACTUAL_RESTORE', function('amdgpu_userq.c', 'amdgpu_userq_vm_validate_and_restore_queue'))
        flags = shlex.split(os.environ.get('CC', 'cc'))
        flags += ['-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                  '-Wno-unused-parameter', '-Wno-sign-compare']
        if os.environ.get('USERQ_TEST_SANITIZERS'):
            flags += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer']
        with tempfile.TemporaryDirectory(prefix='userq-restore-') as temporary:
            directory = pathlib.Path(temporary)
            program = directory / 'test.c'
            binary = directory / 'test'
            program.write_text(source)
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

struct list_head { struct list_head *next, *prev; };
static void list_init(struct list_head *head) { head->next = head->prev = head; }
static bool list_empty(const struct list_head *head) { return head->next == head; }
static void list_add(struct list_head *entry, struct list_head *head)
{
    entry->next = head->next;
    entry->prev = head;
    head->next->prev = entry;
    head->next = entry;
}
static void list_del(struct list_head *entry)
{
    entry->prev->next = entry->next;
    entry->next->prev = entry->prev;
}
static void list_move(struct list_head *entry, struct list_head *head)
{ list_del(entry); list_add(entry, head); }
#define container_of(pointer, type, member) ((type *)((char *)(pointer) - offsetof(type, member)))
#define list_first_entry(head, type, member) container_of((head)->next, type, member)
#define list_for_each_entry(pos, head, member) \
    for (struct list_head *node = (head)->next; node != (head) && \
         ((pos) = container_of(node, __typeof__(*(pos)), member), true); node = node->next)
#define list_for_each_entry_safe(pos, tmp, head, member) \
    for (struct list_head *node = (head)->next, *next = node->next; node != (head) && \
         ((pos) = container_of(node, __typeof__(*(pos)), member), \
          (tmp) = next == (head) ? NULL : container_of(next, __typeof__(*(tmp)), member), (void)(tmp), true); \
         node = next, next = node->next)

struct ww_acquire_ctx { unsigned id; };
struct dma_resv { struct ww_acquire_ctx *owner; };
struct drm_gem_object { struct dma_resv *resv; bool imported; };
struct ttm_resource { int mem_type; };
struct ttm_tt { bool userptr; };
struct ttm_buffer_object { struct drm_gem_object base; struct ttm_tt *ttm; struct ttm_resource *resource; };
struct amdgpu_bo { struct ttm_buffer_object tbo; unsigned allowed_domains; int placement; unsigned refs; };
struct amdgpu_vm;
struct amdgpu_bo_va {
    struct { struct list_head vm_status; struct amdgpu_bo *bo; } base;
    struct amdgpu_vm *vm;
    void *last_pt_update;
    bool cleared;
    bool mapped;
};
struct amdgpu_vm_bo_status { struct list_head idle, needs_update, evicted; };
struct amdgpu_vm {
    struct amdgpu_vm_bo_status always_valid, individual;
    int individual_lock;
    bool is_compute_context;
    void *last_update, *last_tlb_inv;
};
struct amdgpu_device { bool debug_vm; };
struct xarray { void *values[4]; };
struct amdgpu_userq_mgr { struct amdgpu_device *adev; struct xarray userq_xa; };
struct amdgpu_usermode_queue { struct { struct amdgpu_bo *obj; uint64_t gpu_addr; } wptr_obj; };
struct amdgpu_fpriv { struct amdgpu_userq_mgr userq_mgr; struct amdgpu_vm vm; struct amdgpu_bo_va *prt_va; int evf_mgr; };
struct amdgpu_hmm_range { struct amdgpu_bo *bo; };
struct ttm_operation_ctx { bool interruptible, no_wait_gpu; };
struct drm_exec { struct ww_acquire_ctx ticket; };

#define uq_mgr_to_fpriv(mgr) container_of(mgr, struct amdgpu_fpriv, userq_mgr)
#define GFP_KERNEL 0
#define DRM_EXEC_IGNORE_DUPLICATES 1
#define TTM_NUM_MOVE_FENCES 2
#define AMDGPU_GEM_DOMAIN_CPU 1
#define AMDGPU_GEM_DOMAIN_GTT 2
#define TTM_PL_SYSTEM 0
#define unlikely(condition) (condition)
#define drm_exec_until_all_locked(exec) for (bool once = true; once; once = false)
#define drm_exec_retry_on_contention(exec) ((void)0)
#define xa_for_each(array, index, value) \
    for ((index) = 0; (index) < 4; ++(index)) \
        if (((value) = (array)->values[(index)]) != NULL)
#define drm_file_err(...) ((void)0)

static struct amdgpu_bo target_bo;
static struct amdgpu_bo_va target_va;
static struct dma_resv target_resv;
static struct ttm_tt target_tt;
static struct ttm_resource target_resource;
static struct ww_acquire_ctx standalone, foreign, temporary_lock;
static struct amdgpu_fpriv fpriv;
static struct amdgpu_bo_va prt;
static struct amdgpu_device device;
static bool move_during_validation;
static unsigned restored, updates, unlocks;
static int update_error, validation_error;

static void spin_lock(int *lock) { assert(!*lock); *lock = 1; }
static void spin_unlock(int *lock) { assert(*lock); *lock = 0; }
static bool dma_resv_trylock(struct dma_resv *resv)
{
    if (resv->owner)
        return false;
    resv->owner = &temporary_lock;
    return true;
}
static struct ww_acquire_ctx *dma_resv_locking_ctx(struct dma_resv *resv) { return resv->owner; }
static void dma_resv_unlock(struct dma_resv *resv)
{ assert(resv->owner == &temporary_lock); resv->owner = NULL; unlocks++; }
static void *amdgpu_ttm_tt_get_usermm(struct ttm_tt *ttm) { return ttm->userptr ? ttm : NULL; }
static bool amdgpu_ttm_tt_is_userptr(struct ttm_tt *ttm) { return ttm->userptr; }
static bool drm_gem_is_imported(struct drm_gem_object *object) { return object->imported; }
static void amdgpu_vm_bo_evicted(void *base) { (void)base; }
static int amdgpu_vm_bo_update(struct amdgpu_device *adev, struct amdgpu_bo_va *bo_va, bool clear)
{
    if (bo_va == &target_va) {
        updates++;
        if (update_error)
            return update_error;
        bo_va->cleared = clear;
        bo_va->mapped = !clear && bo_va->base.bo->tbo.resource->mem_type != TTM_PL_SYSTEM;
        list_move(&bo_va->base.vm_status, &bo_va->vm->individual.idle);
    }
    return 0;
}

ACTUAL_HANDLE_MOVED

static void drm_exec_init(struct drm_exec *exec, unsigned flags, unsigned count) { exec->ticket.id = 5; }
#define drm_exec_ticket(exec) (&(exec)->ticket)
static void drm_exec_fini(struct drm_exec *exec)
{
    if (target_resv.owner == &exec->ticket)
        target_resv.owner = NULL;
}
static int amdgpu_vm_lock_pd(struct amdgpu_vm *vm, struct drm_exec *exec, unsigned count) { return 0; }
static int amdgpu_vm_lock_individual(struct amdgpu_vm *vm, struct drm_exec *exec, unsigned count)
{
    if (!list_empty(&vm->individual.idle)) {
        assert(!target_resv.owner);
        target_resv.owner = &exec->ticket;
    }
    return 0;
}
static int amdgpu_vm_validate(struct amdgpu_device *adev, struct amdgpu_vm *vm,
                              struct ww_acquire_ctx *ticket, int (*callback)(void *, struct amdgpu_bo *), void *data)
{
    if (move_during_validation)
        list_move(&target_va.base.vm_status, &vm->individual.needs_update);
    return 0;
}
static int drm_exec_prepare_obj(struct drm_exec *exec, struct drm_gem_object *object, unsigned count)
{
    assert(!object->resv->owner || object->resv->owner == &exec->ticket);
    object->resv->owner = &exec->ticket;
    return 0;
}
static struct amdgpu_bo *amdgpu_bo_ref(struct amdgpu_bo *bo) { bo->refs++; return bo; }
static void amdgpu_bo_unref(struct amdgpu_bo **bo) { assert((*bo)->refs > 1); (*bo)->refs--; *bo = NULL; }
static void amdgpu_bo_placement_from_domain(struct amdgpu_bo *bo, unsigned domains) { }
static int ttm_bo_validate(struct ttm_buffer_object *bo, void *placement, struct ttm_operation_ctx *ctx)
{
    assert(bo->base.resv->owner);
    if (validation_error)
        return validation_error;
    if (bo->resource->mem_type == TTM_PL_SYSTEM) {
        bo->resource->mem_type = 1;
        list_move(&target_va.base.vm_status, &target_va.vm->individual.needs_update);
    }
    return 0;
}
static void amdgpu_ttm_tt_set_user_pages(struct ttm_tt *ttm, struct amdgpu_hmm_range *range) { }
static int amdgpu_ttm_tt_get_user_pages(struct amdgpu_bo *bo, struct amdgpu_hmm_range *range) { return 0; }
static struct amdgpu_hmm_range *amdgpu_hmm_range_alloc(struct amdgpu_bo *bo) { return calloc(1, sizeof(struct amdgpu_hmm_range)); }
static void amdgpu_hmm_range_free(struct amdgpu_hmm_range *range) { free(range); }
static void xa_init(struct xarray *array) { *array = (struct xarray){0}; }
static void *xa_load(struct xarray *array, unsigned long key) { assert(key < 4); return array->values[key]; }
static void xa_store(struct xarray *array, unsigned long key, void *value, unsigned flags) { assert(key < 4); array->values[key] = value; }
static void xa_erase(struct xarray *array, unsigned long key) { assert(key < 4); array->values[key] = NULL; }
static void xa_destroy(struct xarray *array) { }
static int amdgpu_userq_validate_vm(void *data, struct amdgpu_bo *bo) { return 0; }
static int amdgpu_vm_update_pdes(struct amdgpu_device *adev, struct amdgpu_vm *vm, bool immediate) { return 0; }
static int dma_fence_wait(void *fence, bool interruptible) { return 0; }
static int amdgpu_ttm_alloc_gart(struct ttm_buffer_object *bo) { return 0; }
static uint64_t amdgpu_bo_gpu_offset(struct amdgpu_bo *bo) { return 0x100000; }
static int amdgpu_evf_mgr_rearm(int *manager, struct drm_exec *exec) { return 0; }
static int amdgpu_userq_restore_all(struct amdgpu_userq_mgr *manager) { restored++; return 0; }

ACTUAL_VALIDATE

ACTUAL_RESTORE

static void setup(void)
{
    fpriv = (struct amdgpu_fpriv){0};
    target_resv = (struct dma_resv){0};
    target_tt = (struct ttm_tt){0};
    target_resource = (struct ttm_resource){.mem_type = 1};
    target_bo = (struct amdgpu_bo){.tbo = {.base = {.resv = &target_resv}, .ttm = &target_tt,
                                        .resource = &target_resource}, .refs = 1};
    target_va = (struct amdgpu_bo_va){.base.bo = &target_bo, .vm = &fpriv.vm};
    device = (struct amdgpu_device){0};
    fpriv.userq_mgr.adev = &device;
    fpriv.prt_va = &prt;
    struct amdgpu_vm_bo_status *lists[] = {&fpriv.vm.always_valid, &fpriv.vm.individual};
    for (unsigned i = 0; i < 2; i++) {
        list_init(&lists[i]->idle);
        list_init(&lists[i]->needs_update);
        list_init(&lists[i]->evicted);
    }
    list_add(&target_va.base.vm_status, &fpriv.vm.individual.idle);
    move_during_validation = false;
    updates = restored = unlocks = 0;
    update_error = validation_error = 0;
}

int main(void)
{
    setup();
    struct amdgpu_usermode_queue queue = {.wptr_obj.obj = &target_bo};
    fpriv.userq_mgr.userq_xa.values[0] = &queue;
    target_resource.mem_type = TTM_PL_SYSTEM;
    assert(amdgpu_userq_vm_validate_and_restore_queue(&fpriv.userq_mgr) == 0);
    assert(restored == 1 && updates == 1 && target_va.mapped && !target_va.cleared);

    setup();
    move_during_validation = true;
    assert(amdgpu_userq_vm_validate_and_restore_queue(&fpriv.userq_mgr) == 0);
    assert(restored == 1 && updates == 1 && !target_va.cleared && !target_resv.owner);

    setup();
    assert(amdgpu_userq_vm_validate_and_restore_queue(&fpriv.userq_mgr) == 0);
    assert(restored == 1 && !updates && !target_va.cleared);

    for (unsigned evicted = 0; evicted < 2; evicted++) {
        setup();
        list_move(&target_va.base.vm_status, evicted ? &fpriv.vm.individual.evicted :
                                                    &fpriv.vm.individual.needs_update);
        target_resource.mem_type = TTM_PL_SYSTEM;
        assert(amdgpu_userq_vm_validate_and_restore_queue(&fpriv.userq_mgr) == 0);
        assert(restored == 1 && updates == 1 && target_va.mapped && !target_va.cleared);
        assert(target_resource.mem_type != TTM_PL_SYSTEM && target_bo.refs == 1);
    }

    for (unsigned state = 0; state < 6; state++) {
        setup();
        list_move(&target_va.base.vm_status, &fpriv.vm.individual.needs_update);
        struct ww_acquire_ctx *ticket = &standalone;
        if (state == 1 || state == 3 || state == 4)
            target_resv.owner = &standalone;
        else if (state == 2)
            target_resv.owner = &foreign;
        if (state == 3 || state == 5)
            ticket = NULL;
        if (state == 4) {
            device.debug_vm = true;
            target_tt.userptr = true;
        }
        assert(amdgpu_vm_handle_moved(&device, &fpriv.vm, ticket) == 0);
        assert(updates == 1 && !restored);
        assert(target_va.cleared == (state == 2 || state == 3));
        assert(unlocks == (state == 0 || state == 5));
        if (state == 1 || state == 3 || state == 4)
            assert(target_resv.owner == &standalone);
        if (state == 2)
            assert(target_resv.owner == &foreign);
    }

    setup();
    move_during_validation = true;
    update_error = -ENOMEM;
    assert(amdgpu_userq_vm_validate_and_restore_queue(&fpriv.userq_mgr) == -ENOMEM);
    assert(!restored && !target_resv.owner);

    setup();
    list_move(&target_va.base.vm_status, &fpriv.vm.individual.needs_update);
    validation_error = -ENOMEM;
    assert(amdgpu_userq_vm_validate_and_restore_queue(&fpriv.userq_mgr) == -ENOMEM);
    assert(!restored && !updates && !target_resv.owner && target_bo.refs == 1);
    puts("UserQ restore: 13 reservation, residency and failure cases passed");
    return 0;
}
'''


if __name__ == '__main__':
    unittest.main()
