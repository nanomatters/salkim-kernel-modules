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


class VmBatchJobTests(unittest.TestCase):
    def test_scheduler_rechecks_vm_completion(self):
        source = (AMD / 'amdgpu_job.c').read_text()
        function = re.search(r'^static struct dma_fence \*amdgpu_job_run\(.*?^\}', source, re.M | re.S)
        self.assertIsNotNone(function)
        command = shlex.split(os.environ.get('CC', 'cc'))
        command += ['-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror']
        if os.environ.get('USERQ_TEST_SANITIZERS'):
            command += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-fno-sanitize-recover=all']
        with tempfile.TemporaryDirectory(prefix='vm-batch-job-') as temporary:
            directory = pathlib.Path(temporary)
            program, binary = directory / 'test.c', directory / 'test'
            program.write_text(HARNESS.replace('ACTUAL_SOURCE', function.group()))
            subprocess.run(command + [str(program), '-o', str(binary)], check=True, timeout=30)
            subprocess.run([str(binary)], check=True, timeout=30)


HARNESS = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

struct dma_fence { int error; };
struct drm_sched_fence { struct dma_fence finished; };
struct amdgpu_vm { bool opted_in; int status; };
struct amdgpu_device { unsigned generation; };
struct amdgpu_ring;
struct drm_gpu_scheduler { struct amdgpu_ring *ring; };
struct drm_sched_job { struct drm_gpu_scheduler *sched; struct drm_sched_fence *s_fence; };
struct amdgpu_ring { struct amdgpu_device *adev; const char *name; struct drm_gpu_scheduler sched; };
struct amdgpu_job {
    struct drm_sched_job base;
    struct amdgpu_vm *vm;
    unsigned generation, job_run_counter, num_ibs;
    struct dma_fence *gang_submit;
    void *ibs;
};
static unsigned cases, status_calls, schedules, frees, traces, error_sets;
static int schedule_result;
static unsigned expected_runs;
static struct dma_fence hardware_fence;
#define to_amdgpu_ring(sched) ((sched)->ring)
#define to_amdgpu_job(p) ((struct amdgpu_job *)((char *)(p) - offsetof(struct amdgpu_job, base)))
#define ERR_PTR(error) ((void *)(intptr_t)(error))
#define dev_dbg(...) ((void)0)
#define dev_err(...) ((void)0)
static void trace_amdgpu_sched_run_job(struct amdgpu_job *job)
{ assert(job->base.sched); traces++; }
static unsigned amdgpu_vm_generation(struct amdgpu_device *adev, struct amdgpu_vm *vm)
{ (void)vm; return adev->generation; }
static int amdgpu_vm_batch_status(struct amdgpu_vm *vm)
{ assert(vm); status_calls++; return vm->opted_in ? vm->status : 0; }
static void dma_fence_set_error(struct dma_fence *fence, int error)
{ assert(error < 0); error_sets++; fence->error = error; }
static int amdgpu_ib_schedule(struct amdgpu_ring *ring, unsigned count, void *ibs,
                              struct amdgpu_job *job, struct dma_fence **fence)
{
    assert(ring == job->base.sched->ring && count == job->num_ibs && ibs == job->ibs);
    assert(!job->base.s_fence->finished.error);
    assert(!job->vm || !job->vm->opted_in || job->vm->status >= 0);
    schedules++;
    if (schedule_result)
        return schedule_result;
    *fence = &hardware_fence;
    return 0;
}
static void amdgpu_job_free_resources(struct amdgpu_job *job)
{ assert(job->job_run_counter == expected_runs + 1); frees++; }

ACTUAL_SOURCE

struct fixture {
    struct amdgpu_device device;
    struct amdgpu_ring ring;
    struct amdgpu_vm vm;
    struct drm_sched_fence fence;
    struct amdgpu_job job;
};
static void init(struct fixture *f)
{
    *f = (struct fixture){0};
    status_calls = schedules = frees = traces = error_sets = 0;
    schedule_result = 0;
    f->device.generation = 7;
    f->ring.adev = &f->device;
    f->ring.name = "gfx";
    f->ring.sched.ring = &f->ring;
    f->job.base.sched = &f->ring.sched;
    f->job.base.s_fence = &f->fence;
    f->job.generation = 7;
    f->job.vm = &f->vm;
    f->job.num_ibs = 2;
    f->job.ibs = &f->vm;
}
static struct dma_fence *run(struct fixture *f)
{
    expected_runs = f->job.job_run_counter;
    struct dma_fence *result = amdgpu_job_run(&f->job.base);
    assert(frees == 1 && traces == 1 && f->job.job_run_counter == expected_runs + 1);
    cases++;
    return result;
}
static void healthy_jobs(void)
{
    struct fixture f;
    /* VM update jobs have no application VM and cannot depend on its failure. */
    init(&f);
    f.job.vm = NULL;
    f.vm.status = -EIO;
    assert(run(&f) == &hardware_fence && schedules == 1 && !status_calls);
    /* Clients that have not opted in retain the existing submission path. */
    init(&f);
    f.vm.status = -EIO;
    assert(run(&f) == &hardware_fence && schedules == 1 && status_calls == 1);
    for (unsigned status = 0; status <= 1; ++status) {
        init(&f);
        f.vm.opted_in = true;
        f.vm.status = status;
        assert(run(&f) == &hardware_fence && schedules == 1 && status_calls == 1);
    }
}
static void failed_jobs(void)
{
    struct fixture f;
    const int errors[] = {-EIO, -ECANCELED, -ENOMEM};
    for (unsigned i = 0; i < sizeof(errors) / sizeof(errors[0]); ++i) {
        init(&f);
        f.vm.opted_in = true;
        /* The ioctl accepted the job before its VM update dependency failed. */
        assert(!f.vm.status && !f.fence.finished.error);
        f.vm.status = errors[i];
        assert(run(&f) == NULL && !schedules && status_calls == 1);
        assert(f.fence.finished.error == errors[i] && error_sets == 1);
    }
    /* A failure must remain sticky on later jobs. */
    init(&f);
    f.vm.opted_in = true;
    f.vm.status = -EIO;
    f.job.job_run_counter = 2;
    assert(run(&f) == NULL && !schedules && f.fence.finished.error == -EIO);
}
static void recovery_and_driver_errors(void)
{
    struct fixture f;
    for (unsigned reason = 0; reason < 3; ++reason) {
        init(&f);
        f.vm.opted_in = true;
        f.vm.status = -EIO;
        if (reason == 0)
            f.job.generation--;
        else if (reason == 1) {
            f.job.job_run_counter = 1;
            f.job.gang_submit = &hardware_fence;
        } else
            f.fence.finished.error = -ENODEV;
        assert(run(&f) == NULL && !schedules && !status_calls);
        assert(f.fence.finished.error == (reason == 2 ? -ENODEV : -ECANCELED));
    }
    /* A gang's first submission is valid. */
    init(&f);
    f.job.gang_submit = &hardware_fence;
    assert(run(&f) == &hardware_fence && schedules == 1);
    /* A normal nongang job may be resubmitted after recovery. */
    init(&f);
    f.job.job_run_counter = 1;
    assert(run(&f) == &hardware_fence && schedules == 1);
    /* Actual IB submission errors still return ERR_PTR to the scheduler. */
    init(&f);
    schedule_result = -ENOSPC;
    assert(run(&f) == ERR_PTR(-ENOSPC) && schedules == 1 && !f.fence.finished.error);
}
int main(void)
{
    healthy_jobs();
    failed_jobs();
    recovery_and_driver_errors();
    printf("VM batch scheduler guard: %u cases passed\n", cases);
    return 0;
}
'''

if __name__ == '__main__':
    unittest.main()
