#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unittest

for ROOT in Path(__file__).resolve().parents:
    if (ROOT / 'drivers/gpu/drm/amd/amdgpu/navi10_ih.c').is_file():
        AMD = 'drivers/gpu/drm/amd/'
        break
    if (ROOT / 'amdgpu/drivers/gpu/drm/amd/amdgpu/navi10_ih.c').is_file():
        AMD = 'amdgpu/drivers/gpu/drm/amd/'
        break
else:
    ROOT = Path(os.environ['IH_TEST_ROOT'])
    AMD = ('amdgpu/' if (ROOT / 'amdgpu/drivers').is_dir() else '') + 'drivers/gpu/drm/amd/'


class IhOverflowTests(unittest.TestCase):
    def test_overflow_pulse_publication_and_errors(self):
        path = AMD + 'amdgpu/navi10_ih.c'
        revision = os.environ.get('IH_TEST_REVISION')
        source = subprocess.check_output(['git', 'show', revision + ':' + path],
                                         cwd=ROOT, text=True) if revision else (ROOT / path).read_text()
        match = re.search(r'^static int navi10_ih_toggle_ring_interrupts\([^;]*?\)\n\{.*?^\}',
                          source, re.M | re.S)
        self.assertIsNotNone(match)
        header = (ROOT / AMD / 'include/asic_reg/oss/osssys_5_0_0_sh_mask.h').read_text()
        definitions = []
        for field in ['RB_ENABLE', 'RB_GPU_TS_ENABLE', 'ENABLE_INTR',
                      'WPTR_OVERFLOW_CLEAR', 'WPTR_OVERFLOW_ENABLE']:
            for suffix in ['_MASK', '__SHIFT']:
                name = 'IH_RB_CNTL__' + field + suffix
                definition = re.search(r'^#define ' + name + r'\s+\S+', header, re.M)
                self.assertIsNotNone(definition)
                definitions.append(definition.group())
        program = HARNESS.replace('ACTUAL_DEFINITIONS', '\n'.join(definitions))
        program = program.replace('ACTUAL_FUNCTION', match.group())
        command = shlex.split(os.environ.get('CC', 'cc'))
        command += ['-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                    '-Wno-unused-parameter']
        if os.environ.get('IH_TEST_SANITIZERS'):
            command += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                        '-fno-sanitize-recover=all']
        with tempfile.TemporaryDirectory(prefix='amdgpu-ih-overflow-') as temporary:
            directory = Path(temporary)
            source_file, binary = directory / 'test.c', directory / 'test'
            source_file.write_text(program)
            subprocess.run(command + [str(source_file), '-o', str(binary)], check=True, timeout=30)
            subprocess.run([str(binary)], check=True, timeout=30)


HARNESS = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
ACTUAL_DEFINITIONS
#define REG_SET_FIELD(value, reg, field, number) \
    (((value) & ~reg##__##field##_MASK) | (((uint32_t)(number) << reg##__##field##__SHIFT) & reg##__##field##_MASK))
struct amdgpu_ih_regs { unsigned ih_rb_cntl, ih_rb_rptr, ih_rb_wptr, psp_reg_id; };
struct amdgpu_ih_ring { struct amdgpu_ih_regs ih_regs; bool enabled; unsigned rptr; };
struct amdgpu_device { bool vf, indirect; int psp; struct { struct amdgpu_ih_ring ih; } irq; };
static struct amdgpu_device *device;
static struct amdgpu_ih_ring *ring;
static unsigned attempts, fail_at, controls, pointers, barriers;
static uint32_t raw, writes[3];
static bool overflow;
static bool amdgpu_sriov_vf(struct amdgpu_device *adev) { return adev->vf; }
static bool amdgpu_sriov_reg_indirect_ih(struct amdgpu_device *adev) { return adev->indirect; }
static uint32_t read_reg(unsigned reg) { assert(reg == ring->ih_regs.ih_rb_cntl); return raw; }
static void write_reg(unsigned reg, uint32_t value)
{
    if (reg != ring->ih_regs.ih_rb_cntl) {
        assert((reg == ring->ih_regs.ih_rb_rptr || reg == ring->ih_regs.ih_rb_wptr) && !value);
        pointers++;
        return;
    }
    assert(controls < 3);
    writes[controls++] = value;
    if (!(raw & IH_RB_CNTL__WPTR_OVERFLOW_CLEAR_MASK) &&
        (value & IH_RB_CNTL__WPTR_OVERFLOW_CLEAR_MASK) &&
        (value & IH_RB_CNTL__RB_ENABLE_MASK) &&
        (value & IH_RB_CNTL__WPTR_OVERFLOW_ENABLE_MASK))
        overflow = false;
    raw = value;
}
static int psp_reg_program(int *psp, unsigned reg, uint32_t value)
{
    assert(psp == &device->psp && reg == ring->ih_regs.psp_reg_id);
    if (++attempts == fail_at)
        return -ETIMEDOUT;
    /* Model an MSI arriving before a successful PSP call returns. */
    if (value & IH_RB_CNTL__RB_ENABLE_MASK)
        assert(ring->enabled && barriers);
    write_reg(ring->ih_regs.ih_rb_cntl, value);
    return 0;
}
#define RREG32(reg) read_reg(reg)
#define WREG32(reg, value) write_reg(reg, value)
#define WREG32_NO_KIQ(reg, value) write_reg(reg, value)
#define smp_wmb() ((void)barriers++)
ACTUAL_FUNCTION
int main(void)
{
    unsigned cases = 0;
    for (unsigned index = 0; index < 3; index++)
    for (unsigned vf = 0; vf < 2; vf++)
    for (unsigned indirect = 0; indirect < 2; indirect++)
    for (unsigned enable = 0; enable < 2; enable++)
    for (unsigned clear = 0; clear < 2; clear++)
    for (unsigned failure = 0; failure < 4; failure++) {
        struct amdgpu_device adev = {.vf = vf, .indirect = indirect};
        struct amdgpu_ih_ring other = {0};
        device = &adev;
        ring = index ? &other : &adev.irq.ih;
        ring->ih_regs = (struct amdgpu_ih_regs){10, 11, 12, 13};
        ring->rptr = 99;
        ring->enabled = !enable;
        raw = UINT32_C(0x12340000) | IH_RB_CNTL__WPTR_OVERFLOW_ENABLE_MASK |
              IH_RB_CNTL__ENABLE_INTR_MASK;
        raw = REG_SET_FIELD(raw, IH_RB_CNTL, WPTR_OVERFLOW_CLEAR, clear);
        overflow = true;
        attempts = controls = pointers = barriers = 0;
        fail_at = failure;
        unsigned expected_attempts = enable ? 3 : 1;
        bool failed = vf && indirect && failure && failure <= expected_attempts;
        int result = navi10_ih_toggle_ring_interrupts(&adev, ring, enable);
        assert(result == (failed ? -ETIMEDOUT : 0));
        assert(ring->enabled == (!failed && enable));
        assert(attempts == (vf && indirect ? (failed ? failure : expected_attempts) : 0));
        assert(barriers == (enable && vf && indirect));
        if (failed) {
            assert(controls == failure - 1 && !pointers && ring->rptr == 99);
        } else if (enable) {
            assert(controls == 3 && !pointers && ring->rptr == 99 && !overflow);
            assert(!(writes[0] & IH_RB_CNTL__WPTR_OVERFLOW_CLEAR_MASK));
            assert(writes[1] & IH_RB_CNTL__WPTR_OVERFLOW_CLEAR_MASK);
            assert(!(writes[2] & IH_RB_CNTL__WPTR_OVERFLOW_CLEAR_MASK));
            assert(raw & IH_RB_CNTL__RB_ENABLE_MASK);
        } else {
            assert(controls == 1 && pointers == 2 && !ring->rptr);
            assert(!(raw & IH_RB_CNTL__RB_ENABLE_MASK) && overflow);
        }
        if (!failed) {
            assert(raw & IH_RB_CNTL__RB_GPU_TS_ENABLE_MASK);
            assert(!!(raw & IH_RB_CNTL__ENABLE_INTR_MASK) == (enable || index));
            const uint32_t changed = IH_RB_CNTL__RB_ENABLE_MASK | IH_RB_CNTL__RB_GPU_TS_ENABLE_MASK |
                IH_RB_CNTL__ENABLE_INTR_MASK | IH_RB_CNTL__WPTR_OVERFLOW_CLEAR_MASK;
            assert((raw & ~changed) == (UINT32_C(0x12340000) | IH_RB_CNTL__WPTR_OVERFLOW_ENABLE_MASK));
        }
        cases++;
    }
    printf("IH overflow: %u publication, pulse and failure cases passed\n", cases);
}
'''


if __name__ == '__main__':
    unittest.main()
