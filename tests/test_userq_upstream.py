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


if __name__ == '__main__':
    unittest.main()
