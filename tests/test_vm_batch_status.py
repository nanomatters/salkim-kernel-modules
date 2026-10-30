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


class VmBatchStatusTests(unittest.TestCase):
    def test_persistent_status(self):
        source = (AMD / 'amdgpu_vm.c').read_text()
        header = (AMD / 'amdgpu_vm.h').read_text()
        entry = re.search(r'^struct amdgpu_vm_batch_entry \{.*?^\};', header, re.M | re.S)
        self.assertIsNotNone(entry)
        begin = source.index('struct amdgpu_vm_batch_state {')
        final = re.search(r'^static void amdgpu_vm_batch_fini\(.*?^\}', source[begin:], re.M | re.S)
        self.assertIsNotNone(final)
        actual = entry.group() + '\n' + source[begin:begin + final.end()]
        command = shlex.split(os.environ.get('CC', 'cc'))
        command += ['-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror', '-pthread']
        if os.environ.get('USERQ_TEST_SANITIZERS'):
            command += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-fno-sanitize-recover=all']
        with tempfile.TemporaryDirectory(prefix='vm-batch-status-') as temporary:
            directory = pathlib.Path(temporary)
            program, binary = directory / 'test.c', directory / 'test'
            program.write_text(HARNESS.replace('ACTUAL_SOURCE', actual))
            subprocess.run(command + [str(program), '-o', str(binary)], check=True, timeout=30)
            subprocess.run([str(binary)], check=True, timeout=30)


HARNESS = r'''
#include <assert.h>
#include <errno.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>

struct list_head { struct list_head *next, *prev; };
struct mutex { pthread_mutex_t value; };
struct dma_resv { bool locked; };
struct amdgpu_bo { struct { struct { struct dma_resv *resv; } base; } tbo; };
struct amdgpu_vm_batch_state;
struct amdgpu_vm { struct { struct amdgpu_bo *bo; } root; struct amdgpu_vm_batch_state *batch_state; };
struct dma_fence {
    _Atomic unsigned refs;
    _Atomic bool signaled;
    _Atomic int error;
    struct dma_fence *children[3];
    unsigned count;
};
struct dma_fence_unwrap { unsigned index; };
static _Atomic unsigned allocations, frees;
static bool fail_allocation;
static unsigned cases;
#define container_of(p, t, member) ((t *)((char *)(p) - offsetof(t, member)))
#define INIT_LIST_HEAD(head) ((head)->next = (head)->prev = (head))
#define list_for_each_entry_safe(entry, tmp, head, member) \
    for (struct list_head *node = (head)->next, *next; \
         node != (head) && ((next = node->next), \
         (entry = container_of(node, typeof(*entry), member)), \
         (tmp = next != (head) ? container_of(next, typeof(*tmp), member) : NULL), true); node = next)
#define smp_load_acquire(p) __atomic_load_n(p, __ATOMIC_ACQUIRE)
#define smp_store_release(p, value) __atomic_store_n(p, value, __ATOMIC_RELEASE)
#define dma_resv_assert_held(resv) assert((resv)->locked)
#define kzalloc_obj(obj) allocate(sizeof(obj))
#define kfree release
#define dma_fence_unwrap_for_each(f, it, root) \
    for ((it)->index = 0; (it)->index < ((root)->count ? (root)->count : 1) && \
         ((f) = (root)->count ? (root)->children[(it)->index] : (root)); ++(it)->index)
static void mutex_init(struct mutex *m) { assert(!pthread_mutex_init(&m->value, NULL)); }
static void mutex_lock(struct mutex *m) { assert(!pthread_mutex_lock(&m->value)); }
static void mutex_unlock(struct mutex *m) { assert(!pthread_mutex_unlock(&m->value)); }
static void mutex_destroy(struct mutex *m) { assert(!pthread_mutex_destroy(&m->value)); }
static void list_add_tail(struct list_head *node, struct list_head *head)
{ node->next = head; node->prev = head->prev; head->prev->next = node; head->prev = node; }
static void list_del(struct list_head *node)
{ node->next->prev = node->prev; node->prev->next = node->next; }
static void *allocate(size_t size)
{ if (fail_allocation) return NULL; void *p = calloc(1, size); assert(p); allocations++; return p; }
static void release(void *p) { if (p) { frees++; free(p); } }
static struct dma_fence *dma_fence_get(struct dma_fence *f) { assert(f->refs); f->refs++; return f; }
static void dma_fence_put(struct dma_fence *f) { assert(f->refs); f->refs--; }
static int dma_fence_get_status(struct dma_fence *f)
{ return !f->signaled ? 0 : f->error ? f->error : 1; }

ACTUAL_SOURCE

struct fixture { struct amdgpu_vm vm; struct amdgpu_bo root; struct dma_resv resv; };
static void init(struct fixture *f)
{ *f = (struct fixture){0}; f->resv.locked = true; f->root.tbo.base.resv = &f->resv; f->vm.root.bo = &f->root; }
static void finish(struct fixture *f)
{ amdgpu_vm_batch_fini(&f->vm); cases++; }
static unsigned pending(struct amdgpu_vm *vm)
{
    struct amdgpu_vm_batch_state *s = vm->batch_state;
    unsigned count = 0;
    for (struct list_head *node = s->pending.next; node != &s->pending; node = node->next) count++;
    return count;
}
static void track(struct amdgpu_vm *vm, struct dma_fence *f)
{ struct amdgpu_vm_batch_entry *e = allocate(sizeof(*e)); amdgpu_vm_batch_track(vm, e, f); }
static void basic_tests(void)
{
    struct fixture f;
    init(&f);
    assert(!amdgpu_vm_batch_status(&f.vm) && !allocations && !f.vm.batch_state);
    fail_allocation = true;
    assert(amdgpu_vm_batch_prepare(&f.vm) == -ENOMEM && !f.vm.batch_state);
    fail_allocation = false;
    assert(!amdgpu_vm_batch_prepare(&f.vm) && f.vm.batch_state);
    unsigned before = allocations;
    assert(!amdgpu_vm_batch_prepare(&f.vm) && allocations == before);
    struct dma_fence good = {.refs = 1};
    track(&f.vm, &good);
    assert(good.refs == 2 && pending(&f.vm) == 1 && !amdgpu_vm_batch_status(&f.vm));
    good.signaled = true;
    assert(!amdgpu_vm_batch_status(&f.vm) && good.refs == 1 && !pending(&f.vm));
    finish(&f);

    init(&f); assert(!amdgpu_vm_batch_prepare(&f.vm));
    struct dma_fence bad = {.refs = 1};
    track(&f.vm, &bad);
    bad.error = -ETIMEDOUT; bad.signaled = true;
    assert(amdgpu_vm_batch_status(&f.vm) == -EIO && bad.refs == 1 && !pending(&f.vm));
    assert(amdgpu_vm_batch_prepare(&f.vm) == -EIO);
    track(&f.vm, &good);
    assert(amdgpu_vm_batch_status(&f.vm) == -EIO && good.refs == 1 && !pending(&f.vm));
    finish(&f);

    init(&f); assert(!amdgpu_vm_batch_prepare(&f.vm));
    good.signaled = false;
    track(&f.vm, &good);
    amdgpu_vm_batch_error(&f.vm);
    assert(amdgpu_vm_batch_status(&f.vm) == -EIO && good.refs == 1);
    finish(&f);

    init(&f); assert(!amdgpu_vm_batch_prepare(&f.vm));
    track(&f.vm, &good);
    finish(&f);
    assert(good.refs == 1);
}
static void timeline_recycling_test(void)
{
    struct fixture f;
    init(&f); assert(!amdgpu_vm_batch_prepare(&f.vm));
    struct dma_fence old = {.refs = 1}, newer = {.refs = 1, .signaled = true};
    track(&f.vm, &old);
    /* A userspace timeline can drop its old node. The VM retains its own reference. */
    dma_fence_put(&old);
    assert(old.refs == 1);
    for (unsigned i = 0; i < 100; i++) {
        track(&f.vm, &newer);
        assert(!amdgpu_vm_batch_status(&f.vm) && newer.refs == 1 && pending(&f.vm) == 1);
    }
    old.error = -EIO; old.signaled = true;
    assert(amdgpu_vm_batch_status(&f.vm) == -EIO && !old.refs);
    assert(amdgpu_vm_batch_prepare(&f.vm) == -EIO);
    finish(&f);
}
static void hidden_error_tests(void)
{
    for (unsigned complete = 0; complete <= 1; complete++) {
        struct fixture f;
        init(&f); assert(!amdgpu_vm_batch_prepare(&f.vm));
        struct dma_fence child = {.refs = 1, .signaled = true, .error = -EIO};
        struct dma_fence array = {.refs = 1, .signaled = complete, .children = {&child}, .count = 1};
        track(&f.vm, &array);
        assert(amdgpu_vm_batch_status(&f.vm) == -EIO && array.refs == 1);
        finish(&f);
    }
    struct fixture a, b;
    init(&a); init(&b);
    assert(!amdgpu_vm_batch_prepare(&a.vm) && !amdgpu_vm_batch_prepare(&b.vm));
    amdgpu_vm_batch_error(&a.vm);
    assert(amdgpu_vm_batch_status(&a.vm) == -EIO && !amdgpu_vm_batch_status(&b.vm));
    finish(&a); finish(&b);
}
struct concurrent { struct amdgpu_vm *vm; struct dma_fence fences[1000]; };
static void *producer(void *data)
{
    struct concurrent *c = data;
    for (unsigned i = 0; i < 1000; i++) track(c->vm, &c->fences[i]);
    return NULL;
}
static void *consumer(void *data)
{
    struct concurrent *c = data;
    for (unsigned i = 0; i < 1000; i++) assert(!amdgpu_vm_batch_status(c->vm));
    return NULL;
}
static void concurrent_test(void)
{
    struct fixture f;
    init(&f); assert(!amdgpu_vm_batch_prepare(&f.vm));
    struct concurrent c = {.vm = &f.vm};
    for (unsigned i = 0; i < 1000; i++) { c.fences[i].refs = 1; c.fences[i].signaled = true; }
    pthread_t a, b;
    assert(!pthread_create(&a, NULL, producer, &c) && !pthread_create(&b, NULL, consumer, &c));
    assert(!pthread_join(a, NULL) && !pthread_join(b, NULL));
    assert(!amdgpu_vm_batch_status(&f.vm) && !pending(&f.vm));
    for (unsigned i = 0; i < 1000; i++) assert(c.fences[i].refs == 1);
    finish(&f);
}
int main(void)
{
    basic_tests();
    timeline_recycling_test();
    hidden_error_tests();
    concurrent_test();
    assert(allocations == frees);
    printf("VM batch status: %u cases passed\n", cases);
    return 0;
}
'''

if __name__ == '__main__':
    unittest.main()
