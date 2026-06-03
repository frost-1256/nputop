import os
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from npu_top import discover_devices, render_bar, sample_from_raw, RawSample


def raw(timestamp, busy_us):
    return RawSample(
        timestamp=timestamp,
        busy_us=busy_us,
        current_freq_mhz=None,
        max_freq_mhz=None,
        min_freq_mhz=None,
        efficient_freq_mhz=None,
        memory_bytes=None,
        runtime_status=None,
        power_state=None,
        sched_mode=None,
        module_version=None,
        vendor=None,
        device=None,
        class_code=None,
    )


class NpuTopTests(unittest.TestCase):
    def test_utilization_uses_busy_delta_over_wall_delta(self):
        sample = sample_from_raw(raw(11.0, 1_250_000), raw(10.0, 1_000_000))
        self.assertAlmostEqual(sample.util_percent, 25.0)
        self.assertAlmostEqual(sample.interval_s, 1.0)

    def test_counter_reset_is_reported(self):
        sample = sample_from_raw(raw(11.0, 100), raw(10.0, 200))
        self.assertIsNone(sample.util_percent)
        self.assertTrue(sample.counter_reset)

    def test_bar_is_clamped(self):
        self.assertEqual(render_bar(150.0, 10), "[##########]")
        self.assertEqual(render_bar(-5.0, 10), "[----------]")

    def test_discovers_accel_class_device(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            dev = root / "devices" / "pci0000:00" / "0000:00:0b.0"
            accel = dev / "accel" / "accel0"
            accel.mkdir(parents=True)
            (dev / "npu_busy_time_us").write_text("123\n", encoding="utf-8")
            (dev / "vendor").write_text("0x8086\n", encoding="utf-8")

            class_root = root / "class" / "accel"
            class_root.mkdir(parents=True)
            os.symlink(accel, class_root / "accel0")

            devices = discover_devices(root)
            self.assertEqual(len(devices), 1)
            self.assertEqual(devices[0].name, "accel0")
            self.assertEqual(devices[0].pci_id, "0000:00:0b.0")


if __name__ == "__main__":
    unittest.main()
