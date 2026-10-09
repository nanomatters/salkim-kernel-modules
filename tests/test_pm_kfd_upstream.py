#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Exercise upstream PM and KFD fixes using the driver's C functions."""
import os
import pathlib
import re
import shlex
import subprocess
import tempfile
import unittest


ROOT = next(parent for parent in reversed(pathlib.Path(__file__).resolve().parents)
            if (parent / 'drivers/gpu/drm/amd').is_dir() or
            (parent / 'amdgpu/drivers/gpu/drm/amd').is_dir())
AMD = ROOT / 'drivers/gpu/drm/amd'
if not AMD.is_dir():
    AMD = ROOT / 'amdgpu/drivers/gpu/drm/amd'


class PmKfdUpstreamTests(unittest.TestCase):
    def function(self, file, name):
        revision = os.environ.get('PM_KFD_TEST_REVISION')
        if revision:
            source = subprocess.check_output(
                ['git', 'show', revision + ':' + str((AMD / file).relative_to(ROOT))],
                cwd=ROOT, text=True)
        else:
            source = (AMD / file).read_text()
        match = re.search(r'^(?:static\s+)?(?:int|void|size_t)\s+' +
                          name + r'\(.*?^\}', source, re.M | re.S)
        self.assertIsNotNone(match, name)
        return match.group()

    def run_program(self, source):
        flags = shlex.split(os.environ.get('CC', 'cc'))
        flags += ['-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                  '-Wno-unused-parameter', '-Wno-unused-function', '-Wno-sign-compare']
        if os.environ.get('PM_KFD_TEST_SANITIZERS'):
            flags += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer']
        with tempfile.TemporaryDirectory() as temporary:
            directory = pathlib.Path(temporary)
            program, binary = directory / 'test.c', directory / 'test'
            program.write_text(source)
            subprocess.run(flags + [str(program), '-o', str(binary)], check=True, timeout=30)
            subprocess.run([str(binary)], check=True, timeout=30)

    def test_umd_profile_recovery(self):
        source = self.function('pm/amdgpu_dpm.c', 'amdgpu_dpm_reapply_umd_profile')
        source = '\n'.join(self.function('pm/swsmu/amdgpu_smu.c', name) for name in
                           ('smu_enter_umd_pstate', 'smu_reapply_power_state_dynamic',
                            'smu_reapply_performance_level')) + '\n' + source
        self.run_program(PM_COMMON + source + UMD_MAIN)

    def test_debug_exit_order(self):
        body = self.function('amdkfd/kfd_process.c', 'kfd_process_notifier_release_internal')
        self.run_program(DEBUG_HARNESS.replace('ACTUAL_EXIT', body))

    def test_irq_drain_completion(self):
        body = '\n'.join(self.function('amdgpu/amdgpu_ids.c', name) for name in
                         ('amdgpu_pasid_drain_irq', 'amdgpu_pasid_drain_irq_done'))
        self.run_program(DRAIN_HARNESS.replace('ACTUAL_DRAIN', body))


PM_COMMON = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <string.h>
enum amd_dpm_forced_level {
    AMD_DPM_FORCED_LEVEL_AUTO, AMD_DPM_FORCED_LEVEL_LOW,
    AMD_DPM_FORCED_LEVEL_HIGH, AMD_DPM_FORCED_LEVEL_MANUAL,
    AMD_DPM_FORCED_LEVEL_PROFILE_STANDARD, AMD_DPM_FORCED_LEVEL_PROFILE_MIN_SCLK,
    AMD_DPM_FORCED_LEVEL_PROFILE_MIN_MCLK, AMD_DPM_FORCED_LEVEL_PROFILE_PEAK,
    AMD_DPM_FORCED_LEVEL_PERF_DETERMINISM,
};
enum { AMD_IP_BLOCK_TYPE_GFX, AMD_CG_STATE_UNGATE, AMD_CG_STATE_GATE };
struct mutex { bool held; };
struct amd_pm_funcs {
    int (*reapply_performance_level)(void *, enum amd_dpm_forced_level);
    int (*force_performance_level)(void *, enum amd_dpm_forced_level);
    enum amd_dpm_forced_level (*get_performance_level)(void *);
};
struct amdgpu_device {
    struct { const struct amd_pm_funcs *pp_funcs; void *pp_handle; } powerplay;
    struct {
        struct mutex mutex, stable_pstate_ctx_lock;
        struct { bool thermal_active; enum amd_dpm_forced_level forced_level; } dpm;
        bool dpm_enabled;
        void *stable_pstate_ctx;
    } pm;
    void *dev;
    bool recovery;
};
struct smu_dpm_context {
    enum amd_dpm_forced_level dpm_level, saved_dpm_level;
    void *dpm_context;
};
struct smu_context {
    struct amdgpu_device *adev;
    struct smu_dpm_context smu_dpm;
    bool pm_enabled, is_apu;
};
static unsigned clocks, stable_on, stable_off, forced, rules, workload, disabled, fallbacks;
static int cg_error, stable_error, rule_error, force_error, workload_error, fallback_error;
#define dev_warn(...) ((void)0)
#define dev_err(...) ((void)0)
#define dev_info(...) ((void)0)
static void mutex_lock(struct mutex *lock) { assert(!lock->held); lock->held = true; }
static void mutex_unlock(struct mutex *lock) { assert(lock->held); lock->held = false; }
static int amdgpu_device_ip_set_clockgating_state(struct amdgpu_device *adev, int ip, int state)
{
    assert(adev->pm.mutex.held && ip == AMD_IP_BLOCK_TYPE_GFX);
    clocks++;
    return state == AMD_CG_STATE_UNGATE ? cg_error : 0;
}
static int amdgpu_asic_update_umd_stable_pstate(struct amdgpu_device *adev, bool enable)
{
    assert(adev->pm.mutex.held);
    if (enable) stable_on++; else stable_off++;
    return enable ? stable_error : 0;
}
static int amdgpu_dpm_force_performance_level(struct amdgpu_device *adev,
                                            enum amd_dpm_forced_level level)
{
    struct smu_context *smu = adev->powerplay.pp_handle;
    assert(adev->pm.stable_pstate_ctx_lock.held && !adev->pm.mutex.held);
    assert(level == AMD_DPM_FORCED_LEVEL_AUTO);
    fallbacks++;
    if (!fallback_error) smu->smu_dpm.dpm_level = level;
    return fallback_error;
}
static int unused_force(void *p, enum amd_dpm_forced_level level) { return 0; }
static enum amd_dpm_forced_level get_level(void *p)
{
    struct smu_context *smu = p;
    return smu->smu_dpm.dpm_level;
}
static void smu_gpo_control(struct smu_context *s, bool enable) { assert(!enable); disabled++; }
static void smu_gfx_ulv_control(struct smu_context *s, bool enable) { assert(!enable); disabled++; }
static void smu_deep_sleep_control(struct smu_context *s, bool enable) { assert(!enable); disabled++; }
static int smu_apply_clocks_adjust_rules(struct smu_context *s) { rules++; return rule_error; }
static int smu_asic_set_performance_level(struct smu_context *s, enum amd_dpm_forced_level level)
{
    forced++;
    return force_error;
}
static int smu_bump_power_profile_mode(struct smu_context *s, void *p, int n)
{
    workload++;
    return workload_error;
}
static void clear_results(void)
{
    clocks = stable_on = stable_off = forced = rules = workload = disabled = fallbacks = 0;
    cg_error = stable_error = rule_error = force_error = workload_error = fallback_error = 0;
}
'''

UMD_MAIN = r'''
int main(void)
{
    struct amdgpu_device adev = {0};
    struct smu_context smu = { .adev = &adev, .pm_enabled = true };
    struct amd_pm_funcs funcs = { smu_reapply_performance_level, unused_force, get_level };
    adev.powerplay.pp_funcs = &funcs;
    adev.powerplay.pp_handle = &smu;
    adev.pm.dpm_enabled = true;
    smu.smu_dpm.dpm_context = &smu;
    smu.smu_dpm.saved_dpm_level = AMD_DPM_FORCED_LEVEL_HIGH;
    for (int level = AMD_DPM_FORCED_LEVEL_AUTO; level <= AMD_DPM_FORCED_LEVEL_PERF_DETERMINISM; level++) {
        clear_results();
        smu.smu_dpm.dpm_level = level;
        assert(!amdgpu_dpm_reapply_umd_profile(&adev));
        bool profile = level >= AMD_DPM_FORCED_LEVEL_PROFILE_STANDARD &&
                       level <= AMD_DPM_FORCED_LEVEL_PROFILE_PEAK;
        assert(forced == profile && clocks == profile && stable_on == profile);
        assert(disabled == (profile ? 3u : 0u));
        assert(workload == profile && !fallbacks && !stable_off);
        assert(smu.smu_dpm.dpm_level == level);
        assert(smu.smu_dpm.saved_dpm_level == AMD_DPM_FORCED_LEVEL_HIGH);
        assert(!adev.pm.mutex.held && !adev.pm.stable_pstate_ctx_lock.held);
    }
    for (int failure = 0; failure < 5; failure++) {
        clear_results();
        smu.smu_dpm.dpm_level = AMD_DPM_FORCED_LEVEL_PROFILE_PEAK;
        adev.pm.stable_pstate_ctx = &smu;
        if (failure == 0) cg_error = -EIO;
        if (failure == 1) stable_error = -EIO;
        if (failure == 2) rule_error = -EIO;
        if (failure == 3) force_error = -EIO;
        if (failure == 4) workload_error = -EIO;
        assert(!amdgpu_dpm_reapply_umd_profile(&adev));
        assert(fallbacks == 1 && stable_off == 1);
        assert(smu.smu_dpm.dpm_level == AMD_DPM_FORCED_LEVEL_AUTO);
        assert(!adev.pm.stable_pstate_ctx);
        assert(!adev.pm.mutex.held && !adev.pm.stable_pstate_ctx_lock.held);
    }
    clear_results();
    smu.smu_dpm.dpm_level = AMD_DPM_FORCED_LEVEL_PROFILE_PEAK;
    adev.pm.stable_pstate_ctx = &smu;
    force_error = fallback_error = -EIO;
    assert(!amdgpu_dpm_reapply_umd_profile(&adev));
    assert(adev.pm.stable_pstate_ctx == &smu);
    assert(smu.smu_dpm.dpm_level == AMD_DPM_FORCED_LEVEL_PROFILE_PEAK);
    clear_results();
    adev.pm.dpm.thermal_active = true;
    assert(!amdgpu_dpm_reapply_umd_profile(&adev));
    assert(!forced && !clocks && !disabled);
    adev.pm.dpm.thermal_active = false;
    funcs.get_performance_level = NULL;
    adev.pm.dpm.forced_level = AMD_DPM_FORCED_LEVEL_PROFILE_PEAK;
    assert(!amdgpu_dpm_reapply_umd_profile(&adev) && forced == 1);
    clear_results();
    funcs.reapply_performance_level = NULL;
    assert(!amdgpu_dpm_reapply_umd_profile(&adev) && !forced);
    return 0;
}
'''


DEBUG_HARNESS = r"""
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <string.h>
struct work { bool pending; };
struct mutex { bool held; };
struct amdgpu_device { unsigned gfxoff; };
struct kfd_node { struct amdgpu_device *adev; bool restore; };
struct kfd_process_device { struct kfd_node *dev; };
struct kfd_process;
struct pqm { struct kfd_process *owner; bool destroyed; };
struct kfd_process {
    struct work eviction_work, restore_work, signal_work;
    struct pqm pqm;
    int n_pdds, debugged_process_count, context_id, mmu_notifier;
    struct kfd_process_device *pdds[1];
    struct { bool ttmp_setup; } runtime_info;
    struct mutex mutex;
    struct kfd_process *debugger_process;
    void *mm;
    bool table_removed, debug_disabled, retired;
    unsigned unrefs;
};
static struct kfd_process *visible;
static int kfd_processes_srcu;
static unsigned puts;
#define KFD_CONTEXT_ID_PRIMARY 0
#define atomic_read(p) (*(p))
#define hash_for_each_rcu(table, index, target, member) \
    for ((index) = 0; (index) < (visible ? 1u : 0u) && ((target) = visible); (index)++)
static int srcu_read_lock(int *p) { return 1; }
static void srcu_read_unlock(int *p, int index) { assert(index == 1); }
static void mutex_lock_nested(struct mutex *p, int n) { assert(!p->held); p->held = true; }
static void mutex_unlock(struct mutex *p) { assert(p->held); p->held = false; }
static void kfd_process_table_remove(struct kfd_process *p) { p->table_removed = true; }
static bool cancel_delayed_work_sync(struct work *p) { bool pending = p->pending; p->pending = false; return pending; }
static void kfd_unref_process(struct kfd_process *p) { p->unrefs++; }
static void debug_disable(struct kfd_process *p, bool drain)
{
    assert(drain == !p->table_removed);
    assert(!p->retired && p->mm);
    p->debug_disabled = true;
    if (p->debugger_process) p->debugger_process->debugged_process_count--;
}
#define kfd_dbg_trap_disable(p, drain) debug_disable(p, drain)
static void kfd_process_dequeue_from_all_devices(struct kfd_process *p)
{
    assert(p->table_removed && p->debug_disabled && p->mm);
    p->retired = true;
}
static void pqm_uninit(struct pqm *p) { assert(p->owner->retired); p->destroyed = true; }
static bool kfd_dbg_is_rlc_restore_supported(struct kfd_node *n) { return n->restore; }
static void amdgpu_gfx_off_ctrl(struct amdgpu_device *a, bool enable) { assert(enable); a->gfxoff++; }
static void mmu_notifier_put(int *p) { puts++; }
ACTUAL_EXIT
int main(void)
{
    for (unsigned flags = 0; flags < 64; flags++) {
        struct amdgpu_device adev = {0};
        struct kfd_node node = { .adev = &adev, .restore = flags & 1 };
        struct kfd_process_device pdd = { .dev = &node };
        struct kfd_process p = {0}, target = {0};
        p.pqm.owner = &p;
        p.mm = &p;
        p.pdds[0] = &pdd;
        p.n_pdds = !!(flags & 2);
        p.runtime_info.ttmp_setup = flags & 4;
        p.signal_work.pending = flags & 8;
        p.context_id = !!(flags & 16);
        p.debugged_process_count = !!(flags & 32);
        target.mm = &target;
        target.debugger_process = &p;
        visible = flags & 32 ? &target : NULL;
        puts = 0;
        kfd_process_notifier_release_internal(&p);
        assert(p.retired && p.pqm.destroyed && !p.mm && p.debug_disabled);
        assert(p.unrefs == !!(flags & 8));
        assert(puts == !(flags & 16));
        assert(adev.gfxoff == (!(flags & 1) && !!(flags & 2) && !!(flags & 4)));
        assert(target.debug_disabled == !!(flags & 32));
        assert(!p.debugged_process_count && !target.mutex.held);
    }
    return 0;
}
"""



DRAIN_HARNESS = r"""
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
typedef uint32_t u32;
#define ERESTARTSYS 512
struct amdgpu_device { int unused; };
struct completion { bool done; };
struct amdgpu_fpriv { struct completion irq_drain_done; };
static struct amdgpu_fpriv owner;
static bool locked, has_owner = true;
static unsigned waits, sends;
static int send_error, scenario;
void amdgpu_pasid_drain_irq_done(u32 pasid);
static void amdgpu_pasid_lock(unsigned long *flags) { assert(!locked); locked = true; *flags = 1; }
static void amdgpu_pasid_unlock(unsigned long flags) { assert(locked && flags == 1); locked = false; }
static struct amdgpu_fpriv *amdgpu_pasid_get_fpriv_locked(u32 pasid)
{
    assert(locked && pasid == 7);
    return has_owner ? &owner : NULL;
}
static void reinit_completion(struct completion *c) { assert(locked); c->done = false; }
static void complete(struct completion *c) { assert(locked); c->done = true; }
static int amdgpu_amdkfd_send_close_event_drain_irq(struct amdgpu_device *a, u32 *packet)
{
    assert(!locked && packet[0] == 123);
    sends++;
    if (scenario == 1) amdgpu_pasid_drain_irq_done(7);
    return send_error;
}
static long wait_for_completion_interruptible_timeout(struct completion *c, unsigned long timeout)
{
    assert(!locked && timeout == 50);
    waits++;
    if (scenario == 2) amdgpu_pasid_drain_irq_done(7);
    if (scenario == 3) return -ERESTARTSYS;
    return c->done ? 1 : 0;
}
ACTUAL_DRAIN
int main(void)
{
    struct amdgpu_device adev = {0};
    u32 packet[] = {123};
    for (scenario = 0; scenario < 4; scenario++) {
        owner.irq_drain_done.done = true;
        waits = sends = 0;
        int ret = amdgpu_pasid_drain_irq(&adev, 7, packet, 50);
        assert(ret == (scenario == 0 ? -ETIMEDOUT : scenario == 3 ? -ERESTARTSYS : 0));
        assert(waits == 1 && sends == 1 && !locked);
    }
    has_owner = false;
    waits = sends = 0;
    assert(amdgpu_pasid_drain_irq(&adev, 7, packet, 50) == -ENOENT);
    amdgpu_pasid_drain_irq_done(7);
    assert(!waits && !sends && !locked);
    has_owner = true;
    send_error = -EIO;
    assert(!amdgpu_pasid_drain_irq(&adev, 7, packet, 50));
    assert(!waits && sends == 1 && !locked);
    return 0;
}
"""


if __name__ == '__main__':
    unittest.main()
