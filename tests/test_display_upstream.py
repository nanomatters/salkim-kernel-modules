#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
import os
import pathlib
import re
import shlex
import subprocess
import tempfile
import unittest


def find_display():
    if root := os.environ.get('DISPLAY_TEST_ROOT'):
        roots = [pathlib.Path(root)]
    else:
        roots = pathlib.Path(__file__).resolve().parents
    for root in roots:
        for prefix in ('', 'amdgpu'):
            path = root / prefix / 'drivers/gpu/drm/amd/display'
            if path.is_dir():
                return path
    raise RuntimeError('Cannot find the AMD display source tree')


DISPLAY = find_display()


class DisplayUpstreamTests(unittest.TestCase):
    def function(self, file, name):
        source = (DISPLAY / file).read_text()
        match = re.search(r'^(?:static )?(?:bool|void|int|uint32_t) ' + name +
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
            subprocess.run(command + [str(program), '-lm', '-o', str(binary)],
                           check=True, timeout=30)
            subprocess.run([str(binary)], check=True, timeout=30)

    def test_urgent_bandwidth_encoding(self):
        paths = [
            'dml2_core/dml2_core_dcn4_calcs.c',
            'dml2_core/dml2_core_dcn5_calcs_dchub.c',
            'dml2_core/dml2_core_dcn6_calcs_dchub.c',
            'dml2_dpmm/dml2_dpmm_dcn4.c',
            'dml2_dpmm/dml2_dpmm_dcn5.c',
        ]
        expressions = []
        declarations = set()
        for path in paths:
            source = (DISPLAY / 'dc/dml2_0/dml21/src' / path).read_text()
            assignments = re.findall(r'\.frac_urg_bw_\w+ = ([^;]+);', source)
            assignments += re.findall(r'->frac_urg_bw_\w+ = ([^;]+);', source)
            self.assertTrue(assignments, path)
            expressions.extend(re.sub(r'mode_lib->mp\.Fraction\w+', 'fraction', expr)
                               for expr in assignments)
            declarations.update(re.findall(
                r'unsigned int u0_10_fixed_point_scale = [^;]+;', source))
        bodies = '\n'.join('static unsigned encode_' + str(i) + '(double fraction)\n{\n' +
                           '\n'.join(declarations) + '\nreturn ' + expr + ';\n}'
                           for i, expr in enumerate(expressions))
        calls = '\n'.join('check(encode_' + str(i) + ');' for i in range(len(expressions)))
        self.run_program(r'''
#include <assert.h>
#include <math.h>
#define math_min2(a, b) fmin((a), (b))
#define math_ceil(a) ceil(a)
ACTUAL_FUNCTIONS
static void check(unsigned (*encode)(double))
{
    assert(encode(0.0) == 0);
    assert(encode(0.5) == 512);
    assert(encode(1.0) == 1023);
    for (unsigned i = 1; i < 1024; i++) {
        double boundary = (double)i / 1024;
        assert(encode(boundary) == i);
        assert(encode(boundary - 1e-7) == i);
        assert(encode(boundary + 1e-7) == (i == 1023 ? 1023 : i + 1));
    }
}
int main(void)
{
ACTUAL_CALLS
    return 0;
}
'''.replace('ACTUAL_FUNCTIONS', bodies).replace('ACTUAL_CALLS', calls))

    def test_prefetch_deadline_frame(self):
        base = 'dc/dml2_0/dml21/src/dml2_standalone_libraries/alternate_pstate_shared_lib'
        header = (DISPLAY / (base + '.h')).read_text()
        defines = '\n'.join(re.findall(r'^#define (?:END_SWATH_REC|MAX_FRAME_COUNT) .*$',
                                       header, re.M))
        names = ['in_circular_range', 'count_vstartups', 'calculate_swath_deadline_dst',
                 'is_dst_in_next_frame', 'is_end_swath_in_next_frame',
                 'is_svp_dst_in_next_frame']
        functions = '\n'.join(self.function(base + '.c', name) for name in names)
        self.run_program(r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
ACTUAL_DEFINES
ACTUAL_FUNCTIONS
int main(void)
{
    const uint32_t frames[] = {0, 1, MAX_FRAME_COUNT - 1, MAX_FRAME_COUNT};
    for (unsigned i = 0; i < sizeof(frames) / sizeof(frames[0]); i++) {
        uint32_t frame = frames[i];
        /* Prefetch starts before the line-counter wrap and finishes after it. */
        assert(is_svp_dst_in_next_frame(0, 1000, 2, 10000, 20000, 1000,
                                       1000, 50, frame, 1000, frame, 100));
        /* These intervals do not cross the line-counter wrap. */
        assert(!is_svp_dst_in_next_frame(0, 5, 2, 10000, 20000, 1000,
                                        1000, 50, frame, 1000, frame, 100));
        assert(!is_svp_dst_in_next_frame(0, 1000, 2, 10000, 20000, 1000,
                                        1000, 50, frame, 5, frame, 100));
        assert(!is_svp_dst_in_next_frame(1, 1000, 2, 10000, 20000, 1000,
                                        1000, 50, frame, 1000, frame, 100));
        /* A deadline after the ordinary frame boundary needs no correction. */
        assert(is_svp_dst_in_next_frame(0, 5, 2, 200000, 250000, 1000,
                                       1000, 50, frame, 1000, frame, 100));
    }
    return 0;
}
'''.replace('ACTUAL_DEFINES', defines).replace('ACTUAL_FUNCTIONS', functions))

    def test_dsc_median_permutations(self):
        function = self.function('dc/dml/dsc/rc_calc_fpu.c', 'median3')
        self.run_program(r'''
#include <assert.h>
#define swap(a, b) do { int tmp = (a); (a) = (b); (b) = tmp; } while (0)
ACTUAL_FUNCTION
int main(void)
{
    for (int a = -5; a <= 5; a++) {
        for (int b = -5; b <= 5; b++) {
            for (int c = -5; c <= 5; c++) {
                int result = median3(a, b, c);
                assert(result == a || result == b || result == c);
                assert((a <= result) + (b <= result) + (c <= result) >= 2);
                assert((a >= result) + (b >= result) + (c >= result) >= 2);
            }
        }
    }
    assert(median3(0, 12, -1) == 0);
    return 0;
}
'''.replace('ACTUAL_FUNCTION', function))

    def test_full_update_exits_self_refresh(self):
        function = self.function('amdgpu_dm/amdgpu_dm.c',
                                 'amdgpu_dm_mod_power_update_streams')
        self.run_program(r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
struct drm_crtc { int unused; };
struct drm_crtc_state { bool active, modeset; };
struct dc_stream_state { void *dm_stream_context; };
struct dm_crtc_state { struct drm_crtc_state base; struct dc_stream_state *stream; };
struct amdgpu_dm_connector { int psr_caps; };
struct amdgpu_display_manager { int dc_lock, dc, power_module; };
struct drm_atomic_commit { struct dm_crtc_state *old_state, *new_state; };
static unsigned psr, replay, added, replaced, removed, sequence;
#define to_dm_crtc_state(state) ((struct dm_crtc_state *)(state))
#define drm_atomic_crtc_needs_modeset(state) ((state)->modeset)
#define for_each_oldnew_crtc_in_state(state, crtc, old, new, i) \
    for ((i) = 0, (crtc) = NULL, (old) = &(state)->old_state->base, \
         (new) = &(state)->new_state->base; (i) == 0; ++(i))
#define scoped_guard(...) for (int guard = 1; guard; guard = 0)
#define dc_exit_ips_for_hw_access(dc) ((void)(dc))
#define psr_event_hw_programming 1
#define replay_event_hw_programming 2
#define replay_event_general_ui 3
static void set_event(struct amdgpu_display_manager *dm, struct dc_stream_state *stream,
                      bool active, unsigned event, bool value)
{
    (void)dm;
    if (!stream)
        return;
    assert(active && value);
    assert(sequence == 0);
    if (event == psr_event_hw_programming)
        psr++;
    else if (event == replay_event_hw_programming)
        replay++;
    else
        assert(false);
}
#define amdgpu_dm_psr_set_event set_event
#define amdgpu_dm_replay_set_event set_event
#define mod_power_add_stream(...) ((void)(aconnector), added++, sequence++)
#define mod_power_replace_stream(...) ((void)(aconnector), replaced++, sequence++)
#define mod_power_remove_stream(...) (removed++, sequence++)
ACTUAL_FUNCTION
static void check(bool old_active, bool new_active, bool modeset, bool same,
                  unsigned events, unsigned add, unsigned replace, unsigned remove)
{
    struct amdgpu_display_manager dm = {0};
    struct amdgpu_dm_connector connector = {0};
    struct dc_stream_state first = { &connector }, second = { &connector };
    struct dm_crtc_state old = { {old_active, false}, old_active ? &first : NULL };
    struct dm_crtc_state new = { {new_active, modeset}, new_active ? (same ? &first : &second) : NULL };
    struct drm_atomic_commit state = { &old, &new };
    psr = replay = added = replaced = removed = sequence = 0;
    amdgpu_dm_mod_power_update_streams(&state, &dm);
    assert(psr == events && replay == events);
    assert(added == add && replaced == replace && removed == remove);
}
int main(void)
{
    check(true, true, false, true, 1, 0, 0, 0);
    check(true, true, true, true, 1, 0, 0, 0);
    check(true, true, true, false, 1, 0, 1, 0);
    check(false, true, true, false, 0, 1, 0, 0);
    check(true, false, true, false, 1, 0, 0, 1);
    check(false, false, false, true, 0, 0, 0, 0);
    return 0;
}
'''.replace('ACTUAL_FUNCTION', function).replace(
            '(i) == 0; ++(i)', '(void)(crtc), (i) == 0; ++(i)'))


if __name__ == '__main__':
    unittest.main()
