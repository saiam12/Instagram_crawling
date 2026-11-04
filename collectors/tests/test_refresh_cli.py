import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.name == 'nt', 'PowerShell wrapper runs on Windows')
class RefreshCliTests(unittest.TestCase):
    def test_collector_style_options_and_legacy_names(self):
        cases = [
            (['--uploaded', '--background'], 'uploaded', True),
            (['--last-collected', '--background'], 'last-collected', True),
            (['--input-xlsx', 'external.xlsx', '--data-dir', 'output', '--start-row', '2', '--end-row', '7', '--interval-hours', '4', '--collection-count', '5', '--schedule-basis', 'last-collected', '--background'], 'last-collected', False),
            (['--input-xlsx', 'external.xlsx', '--age-basis', 'first-collected', '--initial-interval-hours', '2', '--background'], 'uploaded', True),
            (['-InputXlsx', 'external.xlsx', '-ScheduleBasis', 'LastCollected', '-Background'], 'last-collected', True),
            (['--schedule-basis=last-collected', '--interval-hours=4'], 'last-collected', False),
            ([], 'uploaded', True),
            (['--duration-hours', '0'], 'uploaded', True),
            (['--duration-hours', '2'], 'uploaded', True),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'refresh.ps1').write_text((Path(__file__).resolve().parents[1] / 'refresh.ps1').read_text(encoding='utf-8'), encoding='utf-8-sig')
            (root / 'collector.ps1').write_text('param([string]$Avd,[Parameter(Position=0,ValueFromRemainingArguments=$true)][string[]]$CollectorArguments)\nConvertTo-Json -InputObject $CollectorArguments -Compress\nexit 7', encoding='utf-8-sig')
            def invoke(options):
                return subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(root / 'refresh.ps1'), *options], capture_output=True, text=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
            for options, basis, age in cases:
                with self.subTest(options=options):
                    result = invoke(options)
                    self.assertEqual(result.returncode, 7, result.stderr)
                    args = json.loads(result.stdout)
                    self.assertEqual(args[args.index('--schedule-basis') + 1], basis)
                    self.assertEqual('--age-schedule' in args, age)
                    if age:
                        expected_age = options[options.index('--age-basis') + 1] if '--age-basis' in options else ('first-collected' if basis == 'last-collected' else 'uploaded')
                        self.assertEqual(args[args.index('--age-basis') + 1], expected_age)
                    self.assertEqual(args.count('--schedule-basis'), 1)
                    if '--duration-hours' in options:
                        self.assertEqual(args[args.index('--duration-hours') + 1], options[options.index('--duration-hours') + 1])
                    if age and '--initial-interval-hours' not in options:
                        self.assertEqual(args[args.index('--initial-interval-hours') + 1], '2')
                    if '--interval-hours' in options:
                        for flag in ('--input-xlsx', '--data-dir', '--start-row', '--end-row', '--interval-hours', '--collection-count'):
                            self.assertEqual(args[args.index(flag) + 1], options[options.index(flag) + 1])
                        self.assertIn('--background', args)
            for options in (['--uploaded', '--last-collected'], ['--last-collected', '--uploaded'], ['--schedule-basis', 'uploaded', '--last-collected'], ['--start-row', '0'], ['--schedule-basis', 'invalid'], ['--interval-hours'], ['--start-row', '5', '--end-row', '2']):
                with self.subTest(invalid=options):
                    self.assertNotEqual(invoke(options).returncode, 7)
