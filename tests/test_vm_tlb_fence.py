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
SOURCE = 'amdgpu/drivers/gpu/drm/amd/amdgpu/amdgpu_vm_tlb_fence.c'


class VmTlbFenceTests(unittest.TestCase):
    def test_completion_and_errors(self):
        revision = os.environ.get('USERQ_TEST_REVISION')
        if revision:
            source = subprocess.check_output(['git', 'show', revision + ':' + SOURCE],
                                             cwd=ROOT, text=True)
        else:
            source = (ROOT / SOURCE).read_text()
        source = re.sub(r'^#include .*$', '', source, flags=re.M)
        command = shlex.split(os.environ.get('CC', 'cc'))
        command += ['-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                    '-Wno-unused-parameter']
        if os.environ.get('USERQ_TEST_SANITIZERS'):
            command += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                        '-fno-sanitize-recover=all']
        with tempfile.TemporaryDirectory(prefix='vm-tlb-fence-') as temporary:
            directory = pathlib.Path(temporary)
            program, binary = directory / 'test.c', directory / 'test'
            program.write_text(HARNESS.replace('ACTUAL_SOURCE', source))
            subprocess.run(command + [str(program), '-o', str(binary)], check=True, timeout=30)
            subprocess.run([str(binary)], check=True, timeout=30)


HARNESS = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

struct dma_fence;
struct dma_fence_ops {
    const char *(*get_driver_name)(struct dma_fence *);
    const char *(*get_timeline_name)(struct dma_fence *);
};
struct dma_fence {
    const struct dma_fence_ops *ops;
    struct dma_fence *children[3];
    unsigned refs, num_children;
    uint64_t context, seqno;
    int error;
    bool signaled, allocated;
};
struct dma_fence_unwrap { unsigned i; };
struct work_struct { void (*fn)(struct work_struct *); };
struct amdgpu_device { void *dev; };
struct amdgpu_vm { uint16_t pasid; uint64_t tlb_fence_context, tlb_seq; };
typedef int spinlock_t;
#define container_of(p, t, m) ((t *)((char *)(p) - offsetof(t, m)))
#define spin_lock_init(p) (*(p) = 0)
#define atomic64_read(p) (*(p))
#define INIT_WORK(w, f) ((w)->fn = (f))
#define kmalloc_obj(obj) allocate(sizeof(obj))
#define dev_err(...) ((void)0)
#define dma_fence_unwrap_for_each(f, it, root) \
    for ((it)->i = 0; (it)->i < ((root)->num_children ? (root)->num_children : 1) && \
         ((f) = (root)->num_children ? (root)->children[(it)->i] : (root)); ++(it)->i)

static bool allocation_failure;
static int wait_error, flush_error;
static unsigned waits, flushes, signals, frees, cases;
static struct work_struct *pending;
static struct dma_fence *expected_dependency;
static struct dma_fence stub = {.refs = 1, .signaled = true};
static void *allocate(size_t size) { return allocation_failure ? NULL : calloc(1, size); }
static struct dma_fence *dma_fence_get(struct dma_fence *f)
{ if (f) { assert(f->refs); f->refs++; } return f; }
static void dma_fence_put(struct dma_fence *f)
{
    if (!f) return;
    assert(f->refs);
    if (!--f->refs && f->allocated) { frees++; free(f); }
}
static struct dma_fence *dma_fence_get_stub(void) { return dma_fence_get(&stub); }
static void dma_fence_init64(struct dma_fence *f, const struct dma_fence_ops *ops,
                            spinlock_t *lock, uint64_t context, uint64_t seqno)
{
    f->ops = ops;
    f->refs = 1;
    f->context = context;
    f->seqno = seqno;
    f->allocated = true;
}
static int dma_fence_wait(struct dma_fence *f, bool interruptible)
{
    assert(f == expected_dependency && !interruptible && !flushes && !signals);
    waits++;
    if (wait_error) return wait_error;
    f->signaled = true;
    for (unsigned i = 0; i < f->num_children; i++) f->children[i]->signaled = true;
    return 0;
}
static int dma_fence_get_status(struct dma_fence *f)
{ return !f->signaled ? 0 : f->error ? f->error : 1; }
static void dma_fence_set_error(struct dma_fence *f, int error)
{ assert(error < 0 && !f->signaled); f->error = error; }
static void dma_fence_signal(struct dma_fence *f)
{ assert(!f->signaled && (wait_error || flushes == 1)); f->signaled = true; signals++; }
static int amdgpu_gmc_flush_gpu_tlb_pasid(struct amdgpu_device *dev, uint16_t pasid,
                                       unsigned type, bool all_hubs, unsigned inst)
{
    assert(pasid == 37 && type == 2 && all_hubs && !inst && !signals);
    assert(!expected_dependency || (waits == 1 && expected_dependency->signaled));
    flushes++;
    return flush_error;
}
static bool schedule_work(struct work_struct *work)
{ assert(!pending); pending = work; return true; }

ACTUAL_SOURCE

static void run_case(bool oom, bool has_dependency, int dependency_error,
                     int child_error, int flush_result, int wait_result)
{
    struct amdgpu_device dev = {0};
    struct amdgpu_vm vm = {.pasid = 37, .tlb_fence_context = 9, .tlb_seq = UINT64_C(0x100000001)};
    struct dma_fence child[3] = {{.refs = 1}, {.refs = 1}, {.refs = 1}};
    struct dma_fence dependency = {.refs = 1, .error = dependency_error};
    struct dma_fence previous = {.refs = 1};
    struct dma_fence *out = &previous;
    if (child_error) {
        dependency.num_children = 3;
        for (unsigned i = 0; i < 3; i++) dependency.children[i] = &child[i];
        child[1].error = child_error;
    }
    allocation_failure = oom;
    flush_error = flush_result;
    wait_error = wait_result;
    waits = flushes = signals = frees = 0;
    expected_dependency = has_dependency ? &dependency : NULL;
    int expected = has_dependency && wait_result ? wait_result :
                   has_dependency && dependency_error ? dependency_error :
                   has_dependency && child_error ? child_error : flush_result;
    int result = amdgpu_vm_tlb_fence_create(&dev, &vm, expected_dependency, &out);
    if (oom) {
        assert(result == expected && !pending);
        assert(out == (expected ? &previous : &stub));
        assert(previous.refs == (unsigned)!!expected);
        assert(!signals && !frees);
    } else {
        assert(!result && pending && out != &previous && !previous.refs);
        assert(!out->signaled && out->refs == 2 && !waits && !flushes);
        assert(out->context == vm.tlb_fence_context && out->seqno == vm.tlb_seq);
        struct work_struct *work = pending;
        pending = NULL;
        work->fn(work);
        assert(out->signaled && out->error == expected && out->refs == 1);
        assert(signals == 1);
        assert(!container_of(out, struct amdgpu_tlb_fence, base)->dependency);
    }
    assert(waits == (unsigned)has_dependency);
    assert(flushes == (unsigned)!(has_dependency && wait_result));
    assert(dependency.refs == 1);
    for (unsigned i = 0; i < 3; i++) assert(child[i].refs == 1);
    dma_fence_put(out);
    assert(stub.refs == 1 && frees == (unsigned)!oom);
    cases++;
}

int main(void)
{
    for (unsigned oom = 0; oom < 2; oom++) {
        run_case(oom, false, 0, 0, 0, 0);
        run_case(oom, false, 0, 0, -ETIMEDOUT, 0);
        run_case(oom, true, 0, 0, 0, 0);
        run_case(oom, true, 0, 0, -ETIMEDOUT, 0);
        run_case(oom, true, -EIO, 0, 0, 0);
        run_case(oom, true, -EIO, 0, -ETIMEDOUT, 0);
        run_case(oom, true, 0, -EIO, 0, 0);
        run_case(oom, true, 0, -EIO, -ETIMEDOUT, 0);
        run_case(oom, true, 0, 0, 0, -EINTR);
    }
    printf("%u TLB completion cases passed\n", cases);
    return 0;
}
'''

if __name__ == '__main__':
    unittest.main()
