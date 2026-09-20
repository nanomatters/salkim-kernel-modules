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
                          name + r'\([^;]*?\)\s*\n\{.*?^\}', source, re.M | re.S)
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

    def test_available_memory_pin_credit(self):
        body = '\n'.join(self.function('amdgpu/amdgpu_amdkfd_gpuvm.c', name) for name in
                         ('__get_available_memory', 'amdgpu_amdkfd_get_available_memory'))
        self.run_program(ACCOUNT_COMMON + body + ACCOUNT_MAIN)

    def test_memory_admission_and_pins(self):
        body = '\n'.join(self.function('amdgpu/amdgpu_amdkfd_gpuvm.c', name) for name in
                         ('__get_available_memory', 'amdgpu_amdkfd_get_available_memory',
                          'amdgpu_amdkfd_reserve_mem_limit', 'amdgpu_amdkfd_account_vram_pin',
                          'amdgpu_amdkfd_account_vram_unpin'))
        body += '\n' + '\n'.join(self.function('amdgpu/amdgpu_object.c', name) for name in
                                  ('amdgpu_bo_get_xcp', 'amdgpu_bo_pin', 'amdgpu_bo_unpin'))
        self.run_program(ACCOUNT_COMMON + PIN_COMMON + body + PIN_MAIN)


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



ACCOUNT_COMMON = r"""
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <sys/types.h>
#define MiB (1024ULL * 1024)
#define PAGE_SIZE 4096
#define VRAM_AVAILABLITY_ALIGN (2 * MiB)
#define ALIGN_DOWN(v,a) ((v) & ~((a) - 1))
#define min3(a,b,c) ((a) < (b) ? ((a) < (c) ? (a) : (c)) : ((b) < (c) ? (b) : (c)))
#define atomic64_read(p) (*(p))
#define lockdep_assert_held(p) assert(*(p))
#define KFD_XCP_MEMORY_SIZE(a,id) ((a)->size[id])
#define ESTIMATE_PT_SIZE(v) (v)
static uint64_t amdgpu_amdkfd_total_mem_size;
static bool no_system_mem_limit;
struct amdgpu_ras { uint64_t reserved_pages_in_bytes; };
struct amdgpu_device {
    bool apu_prefer_gtt;
    struct { bool is_app_apu; } gmc;
    struct { int64_t vram_used[2]; uint64_t vram_used_aligned[2], vram_pinned[2]; } kfd;
    uint64_t size[2], vram_pin_size, vram_pin_size_xcp[2], vram_pin_size_common;
    uint64_t visible_pin_size, gart_pin_size;
    void *dev;
    struct amdgpu_ras *ras;
};
static struct {
    uint64_t max_system_mem_limit, max_ttm_mem_limit, system_mem_used, ttm_mem_used;
    bool mem_limit_lock;
} kfd_mem_limit;
static struct amdgpu_ras *amdgpu_ras_get_context(struct amdgpu_device *a) { return a->ras; }
static void spin_lock(bool *p) { assert(!*p); *p = true; }
static void spin_unlock(bool *p) { assert(*p); *p = false; }
"""
ACCOUNT_MAIN = r"""
int main(void)
{
    struct amdgpu_device adev = {0};
    struct amdgpu_ras ras = { .reserved_pages_in_bytes = 2 * MiB };
    adev.size[0] = 64 * MiB;
    adev.kfd.vram_used_aligned[0] = 16 * MiB;
    adev.kfd.vram_pinned[0] = 8 * MiB;
    adev.vram_pin_size_xcp[0] = 10 * MiB;
    adev.vram_pin_size_common = 2 * MiB;
    assert(amdgpu_amdkfd_get_available_memory(&adev, 0) == 44 * MiB);
    adev.ras = &ras;
    amdgpu_amdkfd_total_mem_size = 2 * MiB;
    assert(amdgpu_amdkfd_get_available_memory(&adev, 0) == 40 * MiB);
    adev.vram_pin_size_xcp[0] = 60 * MiB;
    assert(amdgpu_amdkfd_get_available_memory(&adev, 0) == 0);
    adev.apu_prefer_gtt = true;
    kfd_mem_limit.max_system_mem_limit = 32 * MiB;
    kfd_mem_limit.max_ttm_mem_limit = 64 * MiB;
    kfd_mem_limit.system_mem_used = 12 * MiB;
    assert(amdgpu_amdkfd_get_available_memory(&adev, 0) == 20 * MiB);
    no_system_mem_limit = true;
    assert(amdgpu_amdkfd_get_available_memory(&adev, 0) == 32 * MiB);
    assert(!kfd_mem_limit.mem_limit_lock);
    return 0;
}
"""



PIN_COMMON = r"""
#include <errno.h>
#include <string.h>
typedef uint32_t u32;
typedef int8_t s8;
#define MAX_XCP 2
#define ALIGN(v,a) ALIGN_DOWN((v) + (a) - 1, a)
#define AMDGPU_GEM_DOMAIN_VRAM 1u
#define AMDGPU_GEM_DOMAIN_GTT 2u
#define AMDGPU_GEM_CREATE_VRAM_CONTIGUOUS 1u
#define AMDGPU_GEM_CREATE_NO_CPU_ACCESS 2u
#define AMDGPU_GEM_CREATE_CPU_ACCESS_REQUIRED 4u
#define TTM_PL_VRAM 0
#define TTM_PL_TT 1
#define TTM_PL_FLAG_CONTIGUOUS 1u
#define KFD_IOC_ALLOC_MEM_FLAGS_VRAM 1u
#define KFD_IOC_ALLOC_MEM_FLAGS_GTT 2u
#define KFD_IOC_ALLOC_MEM_FLAGS_USERPTR 4u
#define KFD_IOC_ALLOC_MEM_FLAGS_DOORBELL 8u
#define KFD_IOC_ALLOC_MEM_FLAGS_MMIO_REMAP 16u
#define unlikely(v) (v)
#define dev_err(...) ((void)0)
#define pr_err(...) ((void)0)
#define pr_debug(...) ((void)0)
static unsigned warnings;
#define WARN_ONCE(c, ...) ((c) ? (++warnings, 1) : 0)
#define atomic64_add(v,p) (*(p) += (v))
#define atomic64_sub(v,p) (*(p) -= (v))
struct gem { void *import_attach; };
struct ttm_resource { unsigned mem_type, placement; };
struct ttm_buffer_object {
    struct amdgpu_device *bdev;
    struct ttm_resource *resource;
    struct gem base;
    void *ttm;
    unsigned pin_count;
};
struct ttm_placement { unsigned num_placement; };
struct ttm_operation_ctx { bool interruptible, no_wait_gpu; };
struct amdgpu_bo {
    struct ttm_buffer_object tbo;
    struct ttm_placement placement;
    struct { unsigned mem_type, flags; } placements[1];
    unsigned preferred_domains, flags;
    int8_t xcp_id;
    void *kfd_bo;
    uint64_t size;
};
static int validate_error;
static struct amdgpu_device *amdgpu_ttm_adev(struct amdgpu_device *a) { return a; }
static uint64_t amdgpu_bo_size(struct amdgpu_bo *b) { return b->size; }
static uint64_t amdgpu_vram_mgr_bo_visible_size(struct amdgpu_bo *b) { return b->size; }
static bool amdgpu_ttm_tt_get_usermm(void *p) { return !!p; }
static bool drm_gem_is_imported(struct gem *p) { return !!p->import_attach; }
static u32 amdgpu_mem_type_to_domain(unsigned t) { return t == TTM_PL_VRAM ? AMDGPU_GEM_DOMAIN_VRAM : AMDGPU_GEM_DOMAIN_GTT; }
static u32 amdgpu_bo_get_preferred_domain(struct amdgpu_device *a, u32 d) { return d; }
static void dma_buf_pin(void *p) { assert(p); }
static void dma_buf_unpin(void *p) { assert(p); }
static void amdgpu_bo_placement_from_domain(struct amdgpu_bo *b, u32 domain)
{
    b->tbo.resource->mem_type = domain == AMDGPU_GEM_DOMAIN_VRAM ? TTM_PL_VRAM : TTM_PL_TT;
}
static int ttm_bo_validate(struct ttm_buffer_object *b, struct ttm_placement *p, struct ttm_operation_ctx *c)
{
    return validate_error;
}
static void ttm_bo_pin(struct ttm_buffer_object *b) { b->pin_count++; }
static void ttm_bo_unpin(struct ttm_buffer_object *b) { assert(b->pin_count); b->pin_count--; }
"""
PIN_MAIN = r"""
int main(void)
{
    struct amdgpu_device adev = {0};
    struct ttm_resource resources[4] = {0};
    struct amdgpu_bo bo[4] = {0};
    kfd_mem_limit.max_system_mem_limit = kfd_mem_limit.max_ttm_mem_limit = 128 * MiB;
    adev.size[0] = adev.size[1] = 64 * MiB;
    adev.kfd.vram_used[0] = adev.kfd.vram_used_aligned[0] = 16 * MiB;
    adev.kfd.vram_used[1] = adev.kfd.vram_used_aligned[1] = 32 * MiB;
    for (unsigned i = 0; i < 4; i++) {
        bo[i].tbo.bdev = &adev;
        bo[i].tbo.resource = &resources[i];
        bo[i].size = (i == 0 ? 8 : i == 1 ? 4 : i == 2 ? 2 : 32) * MiB;
        bo[i].xcp_id = i == 2 ? -1 : i == 3 ? 1 : 0;
        if (i == 0 || i == 3) bo[i].kfd_bo = &bo[i];
        assert(!amdgpu_bo_pin(&bo[i], AMDGPU_GEM_DOMAIN_VRAM));
    }
    assert(adev.vram_pin_size == 46 * MiB);
    assert(adev.vram_pin_size_xcp[0] == 12 * MiB && adev.vram_pin_size_xcp[1] == 32 * MiB);
    assert(adev.vram_pin_size_common == 2 * MiB);
    assert(adev.kfd.vram_pinned[0] == 8 * MiB && adev.kfd.vram_pinned[1] == 32 * MiB);
    assert(amdgpu_amdkfd_get_available_memory(&adev, 0) == 42 * MiB);
    assert(amdgpu_amdkfd_get_available_memory(&adev, 1) == 30 * MiB);
    assert(!amdgpu_bo_pin(&bo[0], AMDGPU_GEM_DOMAIN_VRAM));
    assert(adev.kfd.vram_pinned[0] == 8 * MiB);
    amdgpu_bo_unpin(&bo[0]);
    assert(adev.kfd.vram_pinned[0] == 8 * MiB);
    for (unsigned i = 0; i < 4; i++) amdgpu_bo_unpin(&bo[i]);
    assert(!adev.vram_pin_size && !adev.vram_pin_size_common && !adev.visible_pin_size);
    assert(!adev.kfd.vram_pinned[0] && !adev.kfd.vram_pinned[1]);
    assert(!adev.vram_pin_size_xcp[0] && !adev.vram_pin_size_xcp[1]);
    validate_error = -ENOMEM;
    assert(amdgpu_bo_pin(&bo[0], AMDGPU_GEM_DOMAIN_VRAM) == -ENOMEM);
    assert(!adev.vram_pin_size && !adev.kfd.vram_pinned[0]);
    validate_error = 0;
    assert(!amdgpu_bo_pin(&bo[0], AMDGPU_GEM_DOMAIN_GTT));
    assert(adev.gart_pin_size == 8 * MiB && !adev.kfd.vram_pinned[0]);
    amdgpu_bo_unpin(&bo[0]);
    assert(!adev.gart_pin_size);
    bo[0].xcp_id = -1;
    assert(!amdgpu_bo_pin(&bo[0], AMDGPU_GEM_DOMAIN_VRAM));
    amdgpu_bo_unpin(&bo[0]);
    assert(warnings == 2 && !adev.vram_pin_size && !adev.vram_pin_size_common);
    bo[0].xcp_id = 0;
    assert(!amdgpu_bo_pin(&bo[0], AMDGPU_GEM_DOMAIN_VRAM));
    /* Admission uses raw usage and gives back the KFD pin credit. */
    adev.kfd.vram_used[0] = 64 * MiB - 4096;
    adev.kfd.vram_used_aligned[0] = 64 * MiB;
    assert(!amdgpu_amdkfd_reserve_mem_limit(&adev, 4096, KFD_IOC_ALLOC_MEM_FLAGS_VRAM, 0));
    assert(adev.kfd.vram_used[0] == 64 * MiB);
    assert(amdgpu_amdkfd_reserve_mem_limit(&adev, 4096, KFD_IOC_ALLOC_MEM_FLAGS_VRAM, 0) == -ENOMEM);
    assert(adev.kfd.vram_used[0] == 64 * MiB);
    assert(!kfd_mem_limit.mem_limit_lock);
    amdgpu_bo_unpin(&bo[0]);
    return 0;
}
"""


if __name__ == '__main__':
    unittest.main()
