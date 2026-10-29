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
SOURCE = 'amdgpu/drivers/gpu/drm/amd/amdgpu/amdgpu_vm.c'


class UserqLockTests(unittest.TestCase):
    def test_cursor_and_references(self):
        revision = os.environ.get('USERQ_TEST_REVISION')
        if revision:
            source = subprocess.check_output(
                ['git', 'show', revision + ':' + SOURCE], cwd=ROOT, text=True)
        else:
            source = (ROOT / SOURCE).read_text()
        function = re.search(r'^int amdgpu_vm_lock_individual\(.*?^\}',
                             source, re.M | re.S)
        self.assertIsNotNone(function)
        flags = shlex.split(os.environ.get('CC', 'cc'))
        flags += ['-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror']
        if os.environ.get('USERQ_TEST_SANITIZERS'):
            flags += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                      '-fno-sanitize-recover=all']
        with tempfile.TemporaryDirectory(prefix='userq-lock-test-') as temporary:
            directory = pathlib.Path(temporary)
            program = directory / 'test.c'
            binary = directory / 'test'
            program.write_text(HARNESS.replace('ACTUAL_FUNCTION', function.group()))
            subprocess.run(flags + [str(program), '-o', str(binary)], check=True, timeout=30)
            subprocess.run([str(binary)], check=True, timeout=30)


HARNESS = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>

struct list_head { struct list_head *next, *prev; };
static void list_init(struct list_head *head)
{ head->next = head->prev = head; }
static void list_add_tail(struct list_head *node, struct list_head *head)
{
    node->next = head;
    node->prev = head->prev;
    head->prev->next = node;
    head->prev = node;
}
static void list_move_tail(struct list_head *node, struct list_head *head)
{
    node->prev->next = node->next;
    node->next->prev = node->prev;
    list_add_tail(node, head);
}
#define list_is_head(node, head) ((node) == (head))
#define list_entry(ptr, type, member) ((type *)((char *)(ptr) - offsetof(type, member)))
#define unlikely(value) (value)

struct spinlock { bool held; };
struct drm_gem_object { unsigned id; };
struct amdgpu_bo {
    struct { struct drm_gem_object base; } tbo;
    unsigned refs, gets, puts;
};
struct amdgpu_bo_va {
    struct {
        struct amdgpu_bo *bo;
        struct list_head vm_status;
    } base;
};
struct amdgpu_vm {
    struct { struct list_head idle, needs_update; } individual;
    struct spinlock individual_lock;
};
struct drm_exec { unsigned count, prepared[4]; };
static struct amdgpu_vm vm;
static struct amdgpu_bo bos[4];
static struct amdgpu_bo_va entries[4];
static unsigned move_id, fail_id;
static int prepare_error;
static bool moved;

static void spin_lock(struct spinlock *lock)
{ assert(!lock->held); lock->held = true; }
static void spin_unlock(struct spinlock *lock)
{ assert(lock->held); lock->held = false; }
static struct amdgpu_bo *amdgpu_bo_ref(struct amdgpu_bo *bo)
{
    assert(vm.individual_lock.held && bo->refs);
    bo->refs++;
    bo->gets++;
    return bo;
}
static void amdgpu_bo_unref(struct amdgpu_bo **ptr)
{
    struct amdgpu_bo *bo = *ptr;
    assert(!vm.individual_lock.held && bo->refs == 2);
    bo->refs--;
    bo->puts++;
    *ptr = NULL;
}
static int drm_exec_prepare_obj(struct drm_exec *exec, struct drm_gem_object *obj,
                                unsigned num_fences)
{
    assert(!vm.individual_lock.held && num_fences == 7 && exec->count < 4);
    const unsigned id = obj->id;
    assert(id && id <= 4 && bos[id - 1].refs == 2);
    exec->prepared[exec->count++] = id;
    if (id == move_id && !moved) {
        /* TTM moves the selected BO while its reservation is being acquired. */
        spin_lock(&vm.individual_lock);
        list_move_tail(&entries[id - 1].base.vm_status, &vm.individual.needs_update);
        moved = true;
        spin_unlock(&vm.individual_lock);
    }
    return id == fail_id ? prepare_error : 0;
}

ACTUAL_FUNCTION

static void run_case(unsigned count, bool null_entry, unsigned move,
                     unsigned fail, int error)
{
    vm = (struct amdgpu_vm){0};
    list_init(&vm.individual.idle);
    list_init(&vm.individual.needs_update);
    struct amdgpu_bo_va empty = {0};
    for (unsigned i = 0; i < count; i++) {
        bos[i] = (struct amdgpu_bo){.tbo.base.id = i + 1, .refs = 1};
        entries[i] = (struct amdgpu_bo_va){.base.bo = &bos[i]};
        list_add_tail(&entries[i].base.vm_status, &vm.individual.idle);
        if (null_entry && i == 1)
            list_add_tail(&empty.base.vm_status, &vm.individual.idle);
    }
    struct drm_exec exec = {0};
    move_id = move;
    fail_id = fail;
    prepare_error = error;
    moved = false;
    assert(amdgpu_vm_lock_individual(&vm, &exec, 7) == (fail ? error : 0));
    assert(!vm.individual_lock.held);
    assert(exec.count == (fail ? fail : count));
    for (unsigned i = 0; i < count; i++) {
        const bool visited = !fail || i < fail;
        assert(bos[i].refs == 1 && bos[i].gets == visited && bos[i].puts == visited);
        if (visited)
            assert(exec.prepared[i] == i + 1);
    }
    assert(moved == (move && (!fail || move <= fail)));
    assert(list_is_head(vm.individual.needs_update.next, &vm.individual.needs_update) == !moved);
}

int main(void)
{
    unsigned cases = 0;
    run_case(0, false, 0, 0, 0);
    cases++;
    run_case(4, false, 0, 0, 0);
    cases++;
    run_case(4, true, 0, 0, 0);
    cases++;
    for (unsigned move = 1; move <= 4; move++) {
        run_case(4, false, move, 0, 0);
        cases++;
    }
    const int errors[] = {-EDEADLK, -ENOMEM};
    for (unsigned e = 0; e < 2; e++) {
        for (unsigned fail = 1; fail <= 4; fail++) {
            for (unsigned move = 0; move <= 1; move++) {
                run_case(4, false, move ? fail : 0, fail, errors[e]);
                cases++;
            }
        }
    }
    printf("UserQ individual locking: %u traversal, movement and reference cases passed\n", cases);
    return 0;
}
'''


if __name__ == '__main__':
    unittest.main()
