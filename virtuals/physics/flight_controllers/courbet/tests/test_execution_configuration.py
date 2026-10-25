"""Regression checks; outputs and modified ELF copies stay in temporary directories."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from elftools.elf.elffile import ELFFile

COURBET = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(COURBET))

from execution_configuration import prepare_execution_configuration
from execution_configuration.elf import FirmwareELF
from profiles import px4

PX4_PROFILE = COURBET / 'px4/fmu_v6c'
PX4_ELF = COURBET / 'bin/px4_fmu-v6c_default.elf'
PLANE_PROFILE = COURBET / 'ardupilot/plane462'
PLANE_ELF = COURBET / 'bin/arduplane_v462'
COPTER_PROFILE = COURBET / 'ardupilot/copter462'
COPTER_ELF = COURBET / 'bin/arducopter_v462'


class ConfigurationTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='execution-configuration-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def copy_profile(self, source, elf):
        shutil.copytree(source / 'labeled_conf', self.root / 'labeled_conf')
        text = (source / 'profile.toml').read_text()
        text = '\n'.join(f'elf = "{elf}"' if line.startswith('elf = ') else line
                         for line in text.splitlines()) + '\n'
        (self.root / 'profile.toml').write_text(text)

    def generate(self, **kwargs):
        return prepare_execution_configuration(self.root, **kwargs)

    def previous_outputs(self):
        output = self.root / 'unlabeled_conf'
        output.mkdir()
        for name in ('virtuals.txt', 'modifiers.txt', 'symbols.txt', 'firmware.sha256'):
            (output / name).write_text('previous output\n')
        return output

    def assert_previous_outputs(self, output):
        for path in output.iterdir():
            self.assertEqual(path.read_text(), 'previous output\n')


@unittest.skipUnless(PLANE_ELF.exists(), 'ArduPlane ELF not installed')
class SharedAndArduPilotTests(ConfigurationTestCase):
    def setUp(self):
        super().setUp()
        self.copy_profile(PLANE_PROFILE, PLANE_ELF)

    def test_arduplane_resolves_current_elf_and_keeps_legacy_arguments(self):
        output = self.generate()
        virtuals = (output / 'virtuals.txt').read_text()
        self.assertIn('0x8102d84 storage_read_block *\n', virtuals)
        self.assertIn('0x816c67a raise_periodic_irq 66\n', virtuals)
        # The old checked-in output had a stale address for this hook.
        self.assertIn('0x8040dd0 read_mag_when_published\n', virtuals)
        modifiers = (output / 'modifiers.txt').read_text()
        self.assertIn('0x803ede4 r0 -1\n', modifiers)
        self.assertIn('0x80895bc r15 r14\n', modifiers)
        self.assertIn('0x80e70ea r15 0x80e70ec\n', modifiers)
        self.assertTrue((output / 'symbols.txt').is_file())
        self.assertTrue((output / 'firmware.sha256').is_file())
        self.assertFalse((self.root / 'symbols.txt').exists())

    def test_cli_resolves_paths_from_another_working_directory(self):
        (self.root / 'firmware').symlink_to(PLANE_ELF)
        profile = self.root / 'profile.toml'
        profile.write_text(profile.read_text().replace(str(PLANE_ELF), 'firmware'))
        caller = self.root / 'elsewhere'
        caller.mkdir()
        result = subprocess.run([sys.executable, str(COURBET / 'prepare_execution_configuration.py'),
                                 '--profile', str(profile), '--output-dir', 'generated'],
                                cwd=caller, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((caller / 'generated/virtuals.txt').is_file())
        self.assertFalse((self.root / 'unlabeled_conf').exists())

    def test_missing_symbol_preserves_all_previous_outputs(self):
        output = self.previous_outputs()
        source = self.root / 'labeled_conf/modifiers.txt'
        source.write_text(source.read_text() + '\nnonexistent_function r15 r14\n')
        with self.assertRaisesRegex(ValueError, 'modifiers.txt:.*nonexistent_function'):
            self.generate()
        self.assert_previous_outputs(output)

    def test_malformed_modifier_preserves_all_previous_outputs(self):
        output = self.previous_outputs()
        (self.root / 'labeled_conf/modifiers.txt').write_text('stm32_clock_init r0\n')
        with self.assertRaisesRegex(ValueError, 'expected location, action, and value'):
            self.generate()
        self.assert_previous_outputs(output)

    def test_absolute_address_profile_rejects_changed_firmware(self):
        changed = self.root / 'changed.elf'
        shutil.copyfile(PLANE_ELF, changed)
        with changed.open('ab') as stream:
            stream.write(b'changed build')
        with self.assertRaisesRegex(ValueError, 'SHA-256 mismatch'):
            self.generate(elf_path=changed)
        self.assertFalse((self.root / 'unlabeled_conf').exists())

    def test_symbol_offsets_and_odd_object_addresses(self):
        elf = FirmwareELF(PLANE_ELF)
        table = elf.elf.get_section_by_name('.symtab')
        odd_objects = [s for s in table.iter_symbols()
                       if s['st_info']['type'] == 'STT_OBJECT' and s['st_value'] & 1
                       and len(elf.symbols.get(s.name, set())) == 1]
        self.assertTrue(odd_objects)
        symbol = odd_objects[0]
        self.assertEqual(elf.resolve(symbol.name), symbol['st_value'])
        self.assertEqual(elf.resolve('stm32_clock_init+0xc'), 0x816bbe8 + 12)
        self.assertEqual(elf.resolve('0012'), 12)
        self.assertEqual(elf.resolve('0x0816bbe8'), 0x816bbe8)
        elf.symbols['ambiguous'] = {0x1000, 0x2000}
        with self.assertRaisesRegex(ValueError, 'expected one address'):
            elf.resolve('ambiguous')

    def test_modifier_symbol_operand_is_resolved(self):
        (self.root / 'labeled_conf/modifiers.txt').write_text(
            'stm32_clock_init r15 _crt0_entry+0x4\n')
        output = self.generate()
        self.assertEqual((output / 'modifiers.txt').read_text(), '0x816bbe8 r15 0x800530c\n')

    def test_rejects_output_overwriting_labeled_sources(self):
        with self.assertRaisesRegex(ValueError, 'must differ from labeled_conf'):
            self.generate(output_dir=self.root / 'labeled_conf')

    def test_unknown_profile_setting_is_rejected(self):
        profile = self.root / 'profile.toml'
        profile.write_text('elf_path_typo = "wrong"\n' + profile.read_text())
        with self.assertRaisesRegex(ValueError, 'unknown profile settings'):
            self.generate()


@unittest.skipUnless(COPTER_ELF.exists(), 'ArduCopter ELF not installed')
class ArduCopterTests(ConfigurationTestCase):
    def test_regeneration_matches_active_configuration(self):
        self.copy_profile(COPTER_PROFILE, COPTER_ELF)
        output = self.generate()
        for name in ('virtuals.txt', 'modifiers.txt'):
            self.assertEqual((output / name).read_bytes(),
                             (COPTER_PROFILE / 'unlabeled_conf' / name).read_bytes(), name)


@unittest.skipUnless(PX4_ELF.exists() and (shutil.which('gdb-multiarch') or shutil.which('arm-none-eabi-gdb')),
                     'PX4 debug ELF and GDB required')
class PX4Tests(ConfigurationTestCase):
    def setUp(self):
        super().setUp()
        self.copy_profile(PX4_PROFILE, PX4_ELF)

    def test_generated_console_hook_preserves_control_flow_flag(self):
        output = self.generate()
        generated = (output / 'virtuals.txt').read_text()
        poll = next(line for line in generated.splitlines() if 'bridge_console_poll' in line)
        self.assertRegex(poll, r'0x[0-9a-f]+ bridge_console_poll \*0x[0-9a-f]+,0x[0-9a-f]+,0x[0-9a-f]+$')
        self.assertIn('bridge_px4_uorb', generated)
        self.assertIn('0x817a7a0 r1 8\n', (output / 'modifiers.txt').read_text())

    def test_missing_control_flow_marker_is_restored_for_console_poll(self):
        source = self.root / 'labeled_conf/virtuals.txt'
        source.write_text(source.read_text().replace('*g_usart3priv', 'g_usart3priv'))
        output = self.generate()
        self.assertIn('bridge_console_poll *0x', (output / 'virtuals.txt').read_text())

    def test_unresolved_console_argument_preserves_previous_outputs(self):
        output = self.previous_outputs()
        source = self.root / 'labeled_conf/virtuals.txt'
        source.write_text(source.read_text().replace('g_usart3priv', 'missing_uart_device'))
        with self.assertRaisesRegex(ValueError, 'missing_uart_device'):
            self.generate()
        self.assert_previous_outputs(output)

    def test_unresolved_telemetry_argument_preserves_previous_outputs(self):
        output = self.previous_outputs()
        source = self.root / 'labeled_conf/virtuals.txt'
        source.write_text(source.read_text().replace('g_uart7priv', 'missing_telemetry_device'))
        with self.assertRaisesRegex(ValueError, 'missing_telemetry_device'):
            self.generate()
        self.assert_previous_outputs(output)

    def test_changed_instructions_are_rejected(self):
        for symbol_name, width in [('hrt_tim_isr', 4), ('up_timer_initialize', 2)]:
            with self.subTest(symbol=symbol_name):
                binary = self.root / 'changed.elf'
                shutil.copyfile(PX4_ELF, binary)
                with binary.open('r+b') as stream:
                    elf = ELFFile(stream)
                    symbol = elf.get_section_by_name('.symtab').get_symbol_by_name(symbol_name)[0]
                    text = elf.get_section_by_name('.text')
                    offset = text['sh_offset'] + (symbol['st_value'] & ~1) + 12 - text['sh_addr']
                    stream.seek(offset)
                    stream.write(bytes(width))
                with self.assertRaisesRegex(ValueError, symbol_name + r'\+0xc changed'):
                    self.generate(elf_path=binary)
                self.assertFalse((self.root / 'unlabeled_conf').exists())

    def test_incompatible_structure_layout_preserves_previous_outputs(self):
        output = self.previous_outputs()
        with patch.dict(px4.ABI, {'sensor_accel_s': (49, {'x': 20})}):
            with self.assertRaisesRegex(ValueError, 'firmware ABI mismatch'):
                self.generate()
        self.assert_previous_outputs(output)


if __name__ == '__main__':
    unittest.main()
