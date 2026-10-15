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


if __name__ == '__main__':
    unittest.main()
