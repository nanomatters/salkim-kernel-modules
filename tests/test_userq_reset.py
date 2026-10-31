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


def function(source, name):
    match = re.search(r'^(?:static )?(?:int|void) ' + name + r'\(.*?^\}', source, re.M | re.S)
    if match is None:
        raise AssertionError('Missing production function ' + name)
    return match.group()


class UserqResetTests(unittest.TestCase):
    def test_compute_reset_and_removed_queue_lifetime(self):
        mes = (AMD / 'mes_userqueue.c').read_text()
        gfx = (AMD / 'amdgpu_gfx.c').read_text()
        userq = (AMD / 'amdgpu_userq.c').read_text()
        source = '\n'.join([
            function(mes, 'mes_userq_cancel'),
            function(mes, 'mes_userq_reset'),
            function(mes, 'mes_userq_reset_queue'),
            function(gfx, 'amdgpu_gfx_reset_mes_compute'),
            function(userq, 'amdgpu_userq_map_helper'),
            function(userq, 'amdgpu_userq_restore_helper'),
            function(userq, 'amdgpu_userq_post_reset'),
        ])
        self.compile_run(HARNESS.replace('ACTUAL_SOURCE', source))

    @staticmethod
    def compile_run(program):
        command = shlex.split(os.environ.get('CC', 'cc'))
        command += ['-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror', '-Wno-sign-compare']
        if os.environ.get('USERQ_TEST_SANITIZERS'):
            command += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-fno-sanitize-recover=all']
        with tempfile.TemporaryDirectory(prefix='userq-reset-') as temporary:
            directory = pathlib.Path(temporary)
            source, binary = directory / 'test.c', directory / 'test'
            source.write_text(program)
            subprocess.run(command + [str(source), '-o', str(binary)], check=True, timeout=30)
            subprocess.run([str(binary)], check=True, timeout=30)


HARNESS = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
typedef uint64_t u64;
#include <stdio.h>
#include <string.h>

#define AMDGPU_MAX_COMPUTE_RINGS 4
#define AMDGPU_RING_TYPE_COMPUTE 1
#define AMDGPU_USERQ_STATE_UNMAPPED 0
#define AMDGPU_USERQ_STATE_MAPPED 1
#define AMDGPU_USERQ_STATE_PREEMPTED 2
#define AMDGPU_USERQ_STATE_HUNG 3
#define AMDGPU_USERQ_STATE_CANCELED 5
static unsigned quarantine_claims;
struct mutex { int held; unsigned *state, before; };
static void mutex_lock(struct mutex *mutex) { assert(!mutex->held); mutex->held = 1; if (mutex->state) mutex->before = *mutex->state; }
static void mutex_unlock(struct mutex *mutex)
{
    assert(mutex->held);
    if (mutex->state && mutex->before == AMDGPU_USERQ_STATE_MAPPED && *mutex->state == AMDGPU_USERQ_STATE_HUNG)
        quarantine_claims++;
    mutex->held = 0;
}
static struct mutex *lock_guard(struct mutex *mutex) { mutex_lock(mutex); return mutex; }
static void unlock_guard(struct mutex **mutex) { mutex_unlock(*mutex); }
#define JOIN_(a,b) a##b
#define JOIN(a,b) JOIN_(a,b)
#define guard(type) __attribute__((cleanup(unlock_guard))) struct mutex *JOIN(guard_, __COUNTER__) = lock_guard
#define dev_err(...) ((void)0)
#define READ_ONCE(value) (value)
#define xa_for_each(xa, id, item) \
    for ((id) = 0; (id) < (xa)->count && (((item) = (xa)->queues[id]), 1); (id)++)

struct amdgpu_device;
struct amdgpu_usermode_queue;
struct amdgpu_fence { int unused; };
struct amdgpu_ring { int unused; };
struct amdgpu_gfx_deferred_entry { struct amdgpu_ring *ring; struct amdgpu_fence *fence; };
struct amdgpu_mes_hung_queue_hqd_info { int pipe_index, queue_index, queue_type; };
struct mes_remove_queue_input { int unused; };
struct mes_reset_queue_input { unsigned doorbell_offset, queue_type; bool use_mmio; };
struct mes_funcs { int (*reset_hw_queue)(void *, struct mes_reset_queue_input *);
                   int (*remove_hw_queue)(void *, struct mes_remove_queue_input *); };
struct amdgpu_mes { unsigned hung_queue_hqd_info_offset; struct mes_funcs *funcs; };
struct amdgpu_userq_funcs { int (*map)(struct amdgpu_usermode_queue *);
                          int (*restore)(struct amdgpu_usermode_queue *); };
struct amdgpu_device {
    struct { struct { bool use_mmio_for_reset; struct mutex reset_mutex; unsigned mes_hung_db_array[32]; } mec; } gfx;
    struct amdgpu_mes mes;
    struct { int sem; } reset_domain_storage, *reset_domain;
    struct { unsigned count; struct amdgpu_usermode_queue *queues[4]; } userq_doorbell_xa;
    const struct amdgpu_userq_funcs *userq_funcs[3];
};
struct amdgpu_userq_mgr { struct amdgpu_device *adev; int userq_mutex; };
struct amdgpu_usermode_queue { struct mutex state_mutex; struct amdgpu_userq_mgr *userq_mgr; unsigned state, queue_type, doorbell_index; int hang_detect_lock, hang_detect_work;
    bool hang_detect_stopped, hang_detect_armed, hang_detect_reset_requested; };

static struct amdgpu_usermode_queue *expected_queue;
#define spin_lock_irqsave(lock, flags) do { assert(expected_queue->state_mutex.held && !*(lock)); *(lock) = 1; (flags) = 0; } while (0)
#define spin_unlock_irqrestore(lock, flags) do { assert(*(lock) && !(flags)); *(lock) = 0; } while (0)
static void cancel_delayed_work(int *work)
{
    assert(work == &expected_queue->hang_detect_work && expected_queue->state_mutex.held);
    assert(expected_queue->hang_detect_stopped && !expected_queue->hang_detect_armed && !expected_queue->hang_detect_reset_requested);
}
static unsigned cases, stops, starts, suspends, resumes, detects, removes, resets, maps, restores;
static unsigned cancel_traces, ring_resets, ring_replays, kcqs, kfds, queue_mmio_resets, queue_packet_resets, forced_fences;
static int suspend_result, resume_result, detect_result, remove_result, reset_result;
static int ring_reset_result, replay_result, lock_result, restore_result, map_result;
static bool pending_fence;
static unsigned reset_requests;
static bool supports_suspend, suspended_hw, removed_hw;
static unsigned detected_hung;
static char events[128];
static unsigned event_count;
static void event(char c) { assert(event_count + 1 < sizeof(events)); events[event_count++] = c; }
static void amdgpu_gfx_reset_stop_compute_scheds(struct amdgpu_device *adev, struct amdgpu_ring *ring)
{ (void)adev; (void)ring; stops++; event('s'); }
static void amdgpu_gfx_reset_start_compute_scheds(struct amdgpu_device *adev, struct amdgpu_ring *ring)
{ (void)adev; (void)ring; starts++; event('S'); }
static int amdgpu_mes_suspend(struct amdgpu_device *adev, unsigned id)
{ (void)adev; assert(!id); suspends++; event('p'); suspended_hw = supports_suspend && !suspend_result; return suspend_result; }
static bool amdgpu_mes_suspend_resume_all_supported(struct amdgpu_device *adev)
{ (void)adev; return supports_suspend; }
static int amdgpu_mes_resume(struct amdgpu_device *adev, unsigned id)
{ (void)adev; assert(!id); resumes++; event('r'); suspended_hw = false; return resume_result; }
static int amdgpu_mes_detect_and_reset_hung_queues(struct amdgpu_device *adev, int type, bool detect,
                                                  unsigned *count, unsigned *array, unsigned id)
{ assert(type == 1 && detect && array == adev->gfx.mec.mes_hung_db_array && !id);
  detects++; *count = detected_hung; return detect_result; }
static int amdgpu_gfx_mes_reset_queue_start(struct amdgpu_ring *ring, unsigned id,
                                            struct amdgpu_fence *fence, bool mmio)
{ (void)fence; assert(ring && !id && mmio); ring_resets++; event('k'); return ring_reset_result; }
static int amdgpu_gfx_reset_mes_kcq(struct amdgpu_device *adev, struct amdgpu_ring *ring, unsigned db,
                                   struct amdgpu_ring **out, struct amdgpu_fence **fence)
{ (void)adev; (void)ring; (void)db; *out = NULL; *fence = NULL; kcqs++; return 0; }
static int amdgpu_amdkfd_reset_mes_queue(struct amdgpu_device *adev, unsigned id, int type, int pipe,
                                        int queue, unsigned db)
{ (void)adev; (void)id; (void)type; (void)pipe; (void)queue; (void)db; kfds++; return 0; }
static int amdgpu_mes_reset_queue_mmio(struct amdgpu_device *adev, int type, unsigned vmid,
                                        unsigned me, unsigned pipe, unsigned queue, unsigned xcc)
{ (void)adev; assert(type == 1 && !vmid && me == 1 && pipe == 0 && queue == 2 && !xcc);
  assert(quarantine_claims == 1 && expected_queue->state == AMDGPU_USERQ_STATE_HUNG && !expected_queue->state_mutex.held && expected_queue->hang_detect_stopped);
  queue_mmio_resets++; return reset_result; }
static int amdgpu_mes_reset_user_queue(struct amdgpu_device *adev, int type, unsigned db, unsigned xcc)
{ (void)adev; assert(type == 1 && db == 8200 && !xcc); assert(quarantine_claims == 1 && expected_queue->state == AMDGPU_USERQ_STATE_HUNG && !expected_queue->state_mutex.held && expected_queue->hang_detect_stopped);
  queue_packet_resets++; return reset_result; }
static void amdgpu_userq_fence_driver_force_completion(struct amdgpu_usermode_queue *queue)
{ assert(removed_hw && queue->state == AMDGPU_USERQ_STATE_CANCELED); forced_fences++; }
static int amdgpu_ring_reset_helper_end(struct amdgpu_ring *ring, struct amdgpu_fence *fence)
{ (void)fence; assert(ring && !suspended_hw && resumes); ring_replays++; event('e'); return replay_result; }
static void amdgpu_mes_lock(struct amdgpu_mes *mes) { assert(mes); }
static void amdgpu_mes_unlock(struct amdgpu_mes *mes) { assert(mes); }
static int down_read_trylock(int *sem) { assert(sem); return lock_result; }
static void up_read(int *sem) { assert(sem); }
static void trace_amdgpu_userq_state_start(struct amdgpu_usermode_queue *queue) { assert(queue); }
static void trace_amdgpu_userq_state_changed(struct amdgpu_usermode_queue *queue, unsigned state)
{ assert(queue); if (state == AMDGPU_USERQ_STATE_CANCELED) cancel_traces++; }
static int remove_hw_queue(void *mes, struct mes_remove_queue_input *input)
{ assert(mes && input); removes++; event('u'); if (!remove_result) removed_hw = true; return remove_result; }
static int reset_hw_queue(void *mes, struct mes_reset_queue_input *input)
{ assert(mes && input && !input->use_mmio); resets++; event('x'); return reset_result; }
static int mes_userq_unmap(struct amdgpu_usermode_queue *queue)
{ assert(queue->state_mutex.held); struct mes_remove_queue_input input = {0}; return remove_hw_queue(&queue->userq_mgr->adev->mes, &input); }
static int queue_map(struct amdgpu_usermode_queue *queue)
{ maps++; assert(queue->state != AMDGPU_USERQ_STATE_CANCELED); return map_result; }
static int queue_restore(struct amdgpu_usermode_queue *queue)
{ restores++; assert(queue->state != AMDGPU_USERQ_STATE_CANCELED); return restore_result; }
static bool amdgpu_userq_fence_has_pending(struct amdgpu_usermode_queue *queue, u64 *progress)
{ assert(queue->state_mutex.held); *progress = 42; return pending_fence; }
static void amdgpu_userq_request_reset(struct amdgpu_usermode_queue *queue)
{ assert(queue->state_mutex.held && queue->state == AMDGPU_USERQ_STATE_HUNG); reset_requests++; }
static void amdgpu_userq_resume_hang_detect_work(struct amdgpu_usermode_queue *queue)
{ assert(queue->state_mutex.held && !queue->hang_detect_stopped); }
static const struct amdgpu_userq_funcs queue_funcs = {queue_map, queue_restore};
static struct mes_funcs mes_funcs = {reset_hw_queue, remove_hw_queue};

ACTUAL_SOURCE

struct fixture { struct amdgpu_device dev; struct amdgpu_userq_mgr manager; struct amdgpu_usermode_queue queue; };
static void init(struct fixture *f)
{
    memset(f, 0, sizeof(*f));
    stops = starts = suspends = resumes = detects = removes = resets = maps = restores = 0;
    cancel_traces = ring_resets = ring_replays = kcqs = kfds = queue_mmio_resets = queue_packet_resets = forced_fences = 0;
    suspend_result = resume_result = detect_result = remove_result = reset_result = 0;
    ring_reset_result = replay_result = restore_result = map_result = 0; reset_requests = 0;
    pending_fence = false;
    lock_result = 1;
    supports_suspend = true;
    suspended_hw = removed_hw = false;
    detected_hung = 0;
    memset(events, 0, sizeof(events)); event_count = 0;
    quarantine_claims = 0; expected_queue = &f->queue;
    f->queue.state_mutex.state = &f->queue.state;
    f->dev.gfx.mec.use_mmio_for_reset = true;
    f->dev.mes.funcs = &mes_funcs;
    f->dev.reset_domain = &f->dev.reset_domain_storage;
    f->dev.userq_funcs[1] = &queue_funcs;
    f->dev.userq_doorbell_xa.count = 1;
    f->dev.userq_doorbell_xa.queues[0] = &f->queue;
    f->manager.adev = &f->dev;
    f->queue.userq_mgr = &f->manager;
    f->queue.state = AMDGPU_USERQ_STATE_MAPPED;
    f->queue.queue_type = 1;
    f->queue.doorbell_index = 8200;
    f->queue.hang_detect_armed = f->queue.hang_detect_reset_requested = true;
}
static int run(struct fixture *f)
{ cases++; return amdgpu_gfx_reset_mes_compute(&f->dev, NULL, NULL, &f->queue, NULL, NULL); }
static void supported_suspend_cancels(void)
{
    struct fixture f;
    init(&f);
    /* Reproduce firmware rejecting RESET while SUSPEND and REMOVE succeed. */
    reset_result = -ETIMEDOUT;
    assert(!run(&f));
    assert(!strcmp(events, "spurS"));
    assert(removes == 1 && !resets && cancel_traces == 1 && starts == 1);
    assert(removed_hw && f.queue.state == AMDGPU_USERQ_STATE_CANCELED && !f.queue.state_mutex.held);
    cases++;
    assert(!mes_userq_cancel(&f.queue) && removes == 1 && !f.queue.state_mutex.held);
    assert(!amdgpu_userq_map_helper(&f.queue) && !maps);
    assert(!amdgpu_userq_restore_helper(&f.queue) && !restores);
    assert(!amdgpu_userq_post_reset(&f.dev, false) && !maps);
    assert(!amdgpu_userq_post_reset(&f.dev, true) && !maps);
    memset(events, 0, sizeof(events)); event_count = 0;
    assert(!run(&f) && !event_count);
}
static void failures_do_not_report_success(void)
{
    struct fixture f;
    init(&f); remove_result = -EIO;
    assert(run(&f) == -EIO && removes == 1 && !resets && !cancel_traces && !starts);
    assert(f.queue.state == AMDGPU_USERQ_STATE_MAPPED && !removed_hw && resumes == 1);
    init(&f); resume_result = -ETIMEDOUT;
    assert(run(&f) == -ETIMEDOUT && !starts && removed_hw);
    assert(f.queue.state == AMDGPU_USERQ_STATE_CANCELED);
    init(&f); remove_result = -EIO; resume_result = -ETIMEDOUT;
    assert(run(&f) == -EIO && !starts && !removed_hw);
    init(&f); suspend_result = -EIO; detect_result = -EBUSY;
    assert(run(&f) == -EBUSY && detects == 1 && !removes && !resets && !starts && resumes == 1);
}
static void unsupported_or_failed_suspend_keeps_reset(void)
{
    struct fixture f;
    init(&f); supports_suspend = false;
    assert(!run(&f) && resets == 1 && removes == 1 && !detects);
    assert(!strcmp(events, "spxurS"));
    init(&f); supports_suspend = false; reset_result = -ETIMEDOUT;
    assert(run(&f) == -ETIMEDOUT && resets == 1 && !removes && !starts);
    init(&f); suspend_result = -EIO;
    assert(!run(&f) && detects == 1 && resets == 1 && removes == 1);
    init(&f); suspend_result = -EIO; reset_result = -EIO;
    assert(run(&f) == -EIO && detects == 1 && resets == 1 && !removes && !starts);
    init(&f); suspend_result = -EIO; detected_hung = 1;
    assert(!run(&f) && kcqs == 1 && kfds == 1 && !forced_fences);
}
static void kernel_queue_and_kfd_paths(void)
{
    struct fixture f;
    struct amdgpu_ring ring = {0};
    struct amdgpu_fence fence = {0};
    struct mes_remove_queue_input input = {0};
    init(&f); cases++;
    assert(!amdgpu_gfx_reset_mes_compute(&f.dev, &ring, &fence, NULL, NULL, NULL));
    assert(!strcmp(events, "spkreS") && ring_resets == 1 && ring_replays == 1 && !removes && !resets);
    init(&f); resume_result = -EIO; cases++;
    assert(amdgpu_gfx_reset_mes_compute(&f.dev, &ring, &fence, NULL, NULL, NULL) == -EIO);
    assert(ring_resets == 1 && !ring_replays && !starts);
    init(&f); ring_reset_result = -EBUSY; cases++;
    assert(amdgpu_gfx_reset_mes_compute(&f.dev, &ring, &fence, NULL, NULL, NULL) == -EBUSY);
    assert(!ring_replays && !starts);
    init(&f); cases++;
    assert(!amdgpu_gfx_reset_mes_compute(&f.dev, NULL, NULL, NULL, NULL, &input));
    assert(removes == 1 && !resets && starts == 1);
    init(&f); lock_result = 0; cases++;
    assert(amdgpu_gfx_reset_mes_compute(&f.dev, NULL, NULL, NULL, NULL, &input) == -EIO);
    assert(!removes && !starts && resumes == 1);
}
static void detected_queue_cancellation(void)
{
    struct fixture f;
    init(&f); cases++;
    assert(!mes_userq_reset_queue(&f.dev, NULL, 1, 0, 2, 8200));
    assert(queue_mmio_resets == 1 && !queue_packet_resets && removes == 1 && forced_fences == 1);
    assert(f.queue.state == AMDGPU_USERQ_STATE_CANCELED && !f.queue.state_mutex.held);
    cases++;
    assert(!mes_userq_reset_queue(&f.dev, NULL, 1, 0, 2, 8200));
    assert(queue_mmio_resets == 1 && removes == 1 && forced_fences == 1);
    init(&f); reset_result = -EIO; cases++;
    assert(mes_userq_reset_queue(&f.dev, NULL, 1, 0, 2, 8200) == -EIO);
    assert(queue_mmio_resets == 1 && !removes && !forced_fences && f.queue.state == AMDGPU_USERQ_STATE_HUNG);
    cases++;
    assert(!amdgpu_userq_post_reset(&f.dev, false) && !maps);
    init(&f); remove_result = -EBUSY; cases++;
    assert(mes_userq_reset_queue(&f.dev, NULL, 1, 0, 2, 8200) == -EBUSY);
    assert(queue_mmio_resets == 1 && removes == 1 && !forced_fences && f.queue.state == AMDGPU_USERQ_STATE_HUNG);
    cases++;
    assert(!amdgpu_userq_post_reset(&f.dev, false) && !maps);
    init(&f); cases++;
    assert(!mes_userq_reset_queue(&f.dev, &f.queue, 1, 0, 2, 8200));
    assert(!queue_mmio_resets && !removes && !forced_fences);
    init(&f); cases++;
    assert(!mes_userq_reset_queue(&f.dev, NULL, 1, 0, 2, 8199));
    assert(!queue_mmio_resets && !removes && !forced_fences);
    init(&f); f.dev.gfx.mec.use_mmio_for_reset = false; cases++;
    assert(!mes_userq_reset_queue(&f.dev, NULL, 1, 0, 2, 8200));
    assert(!queue_mmio_resets && queue_packet_resets == 1 && removes == 1 && forced_fences == 1);
}
static void normal_queue_lifecycle(void)
{
    struct fixture f;
    init(&f); f.queue.state = AMDGPU_USERQ_STATE_UNMAPPED; cases++;
    assert(!amdgpu_userq_map_helper(&f.queue) && maps == 1);
    init(&f); f.queue.state = AMDGPU_USERQ_STATE_UNMAPPED; map_result = -EIO; cases++;
    assert(amdgpu_userq_map_helper(&f.queue) == -EIO && maps == 1);
    assert(!reset_requests && f.queue.state == AMDGPU_USERQ_STATE_HUNG && !f.queue.state_mutex.held);
    init(&f); f.queue.state = AMDGPU_USERQ_STATE_UNMAPPED; map_result = -EIO; pending_fence = true; cases++;
    assert(amdgpu_userq_map_helper(&f.queue) == -EIO && maps == 1);
    assert(reset_requests == 1 && f.queue.state == AMDGPU_USERQ_STATE_HUNG && !f.queue.state_mutex.held);
    init(&f); f.queue.state = AMDGPU_USERQ_STATE_PREEMPTED; cases++;
    assert(!amdgpu_userq_restore_helper(&f.queue) && restores == 1);
    init(&f); f.queue.state = AMDGPU_USERQ_STATE_PREEMPTED; restore_result = -EIO; cases++;
    assert(amdgpu_userq_restore_helper(&f.queue) == -EIO && restores == 1);
    assert(reset_requests == 1 && f.queue.state == AMDGPU_USERQ_STATE_HUNG && !f.queue.state_mutex.held);
    init(&f); f.queue.state = AMDGPU_USERQ_STATE_HUNG; cases++;
    assert(!amdgpu_userq_post_reset(&f.dev, false) && maps == 1);
    init(&f); f.queue.state = AMDGPU_USERQ_STATE_HUNG; cases++;
    assert(!amdgpu_userq_post_reset(&f.dev, true) && !maps);
    init(&f); f.queue.state = AMDGPU_USERQ_STATE_HUNG; f.queue.hang_detect_stopped = true; cases++;
    assert(!amdgpu_userq_post_reset(&f.dev, false) && !maps);
}
int main(void)
{
    supported_suspend_cancels();
    failures_do_not_report_success();
    unsupported_or_failed_suspend_keeps_reset();
    kernel_queue_and_kfd_paths();
    detected_queue_cancellation();
    normal_queue_lifecycle();
    printf("%u UserQ recovery cases passed\n", cases);
}
'''

if __name__ == '__main__':
    unittest.main()
