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


if __name__ == '__main__':
    unittest.main()
