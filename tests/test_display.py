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
AMD = ROOT / 'amdgpu/drivers/gpu/drm/amd'


class DisplayTests(unittest.TestCase):
    def function(self, path, name):
        source = path.read_text()
        match = re.search(r'^(?:static )?(?:bool|unsigned long long) ' + name +
                          r'\(.*?^\}', source, re.M | re.S)
        self.assertIsNotNone(match, name)
        return match.group()

    def run_program(self, source):
        with tempfile.TemporaryDirectory() as temporary:
            directory = pathlib.Path(temporary)
            program = directory / 'test.c'
            binary = directory / 'test'
            program.write_text(source)
            command = shlex.split(os.environ.get('CC', 'cc'))
            command += ['-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror']
            if os.environ.get('DISPLAY_TEST_SANITIZERS'):
                command += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer']
            subprocess.run(command + [str(program), '-o', str(binary)], check=True, timeout=30)
            subprocess.run([str(binary)], check=True, timeout=30)

    def test_frl_default(self):
        header = (AMD / 'include/amd_shared.h').read_text()
        enum = re.search(r'^enum DC_FEATURE_MASK \{.*?^\};', header, re.M | re.S)
        self.assertIsNotNone(enum)
        source = (AMD / 'amdgpu/amdgpu_drv.c').read_text()
        default = re.search(r'^uint amdgpu_dc_feature_mask = .*?;', source, re.M)
        self.assertIsNotNone(default)
        self.run_program('#include <assert.h>\ntypedef unsigned int uint;\n' +
                         enum.group() + '\n' + default.group() + '\n' +
                         'int main(void) {\n'
                         'assert(amdgpu_dc_feature_mask == '
                         '(DC_MULTI_MON_PP_MCLK_SWITCH_MASK | DC_FRL_MASK));\n'
                         'assert(amdgpu_dc_feature_mask & (1u << 10));\nreturn 0;\n}\n')

    def test_vrr_timing_gate(self):
        gate = self.function(AMD / 'display/amdgpu_dm/amdgpu_dm_freesync.c',
                             'amdgpu_dm_is_vrr_timing_valid')
        nominal = self.function(AMD / 'display/modules/freesync/freesync.c',
                                'mod_freesync_calc_nominal_field_rate')
        source = (AMD / 'display/amdgpu_dm/amdgpu_dm_freesync.c').read_text()
        threshold = re.search(r'^#define AMDGPU_DM_VRR_MIN_REFRESH_RANGE .*$', source, re.M)
        self.assertIsNotNone(threshold)
        self.run_program(HARNESS.replace('ACTUAL_THRESHOLD', threshold.group())
                         .replace('ACTUAL_FUNCTIONS', nominal + '\n' + gate))


HARNESS = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
typedef uint64_t u64;
struct dc_stream_state {
    struct { unsigned pix_clk_100hz, h_total, v_total; } timing;
};
#define min_t(type, a, b) ((type)(a) < (type)(b) ? (type)(a) : (type)(b))
static u64 div_u64(u64 value, unsigned divisor) { return value / divisor; }
ACTUAL_THRESHOLD
ACTUAL_FUNCTIONS
int main(void)
{
    struct dc_stream_state stream = {0};
    const int maxima[] = {48, 60, 120, 144, 240};
    assert(amdgpu_dm_is_vrr_timing_valid(NULL, 48, 144));
    assert(amdgpu_dm_is_vrr_timing_valid(&stream, 48, 144));
    stream.timing.pix_clk_100hz = 600000;
    stream.timing.h_total = stream.timing.v_total = 1000;
    assert(amdgpu_dm_is_vrr_timing_valid(&stream, 0, 144));
    assert(amdgpu_dm_is_vrr_timing_valid(&stream, 48, 0));
    assert(amdgpu_dm_is_vrr_timing_valid(&stream, -1, 144));
    assert(amdgpu_dm_is_vrr_timing_valid(&stream, 48, -1));
    stream.timing.h_total = 0;
    assert(amdgpu_dm_is_vrr_timing_valid(&stream, 48, 144));
    stream.timing.h_total = 1000;
    stream.timing.v_total = 0;
    assert(amdgpu_dm_is_vrr_timing_valid(&stream, 48, 144));
    stream.timing.v_total = 1000;
    for (int rate = 1; rate <= 240; ++rate) {
        stream.timing.pix_clk_100hz = rate * 10000;
        for (int minimum = 1; minimum <= 240; ++minimum) {
            for (unsigned i = 0; i < sizeof(maxima) / sizeof(maxima[0]); ++i) {
                int usable = rate < maxima[i] ? rate : maxima[i];
                assert(amdgpu_dm_is_vrr_timing_valid(&stream, minimum, maxima[i]) ==
                       (usable - minimum >= 10));
            }
        }
    }
    return 0;
}
'''


if __name__ == '__main__':
    unittest.main()
