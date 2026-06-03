#!/usr/bin/env python3
"""Top-like monitor for Intel AI Boost / intel_vpu NPUs."""

from __future__ import annotations

import argparse
import curses
import json
import os
import re
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


DEFAULT_INTERVAL = 1.0
PCI_RE = re.compile(r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$")


def read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def read_int(path: Path) -> Optional[int]:
    value = read_text(path)
    if value is None or value == "":
        return None
    try:
        return int(value, 0)
    except ValueError:
        return None


def first_existing_int(paths: Iterable[Path]) -> Optional[int]:
    for path in paths:
        value = read_int(path)
        if value is not None:
            return value
    return None


def first_existing_text(paths: Iterable[Path]) -> Optional[str]:
    for path in paths:
        value = read_text(path)
        if value:
            return value
    return None


def format_bytes(value: Optional[int]) -> str:
    if value is None:
        return "--"
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = float(value)
    unit = units[0]
    for unit in units:
        if abs(amount) < 1024.0 or unit == units[-1]:
            break
        amount /= 1024.0
    if unit == "B":
        return f"{int(amount)} {unit}"
    return f"{amount:.2f} {unit}"


def format_seconds_from_us(value: Optional[int]) -> str:
    if value is None:
        return "--"
    seconds = value / 1_000_000.0
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{int(minutes)}m{int(seconds):02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{int(hours)}h{int(minutes):02d}m"


def clamp_percent(value: float) -> float:
    if value < 0:
        return 0.0
    if value > 100:
        return 100.0
    return value


@dataclass(frozen=True)
class NPUDevice:
    name: str
    device_path: Path
    class_path: Optional[Path] = None
    devnode: Optional[Path] = None
    sysfs_root: Path = Path("/sys")

    @property
    def pci_id(self) -> str:
        return self.device_path.name if PCI_RE.match(self.device_path.name) else "--"

    @property
    def driver(self) -> str:
        driver_link = self.device_path / "driver"
        if driver_link.exists() or driver_link.is_symlink():
            return driver_link.resolve().name
        return "intel_vpu" if self.busy_path.exists() else "--"

    @property
    def busy_path(self) -> Path:
        return self.device_path / "npu_busy_time_us"

    def module_path(self, name: str) -> Path:
        return self.sysfs_root / "module" / "intel_vpu" / name

    def read_raw(self) -> "RawSample":
        freq_dir = self.device_path / "freq"
        return RawSample(
            timestamp=time.monotonic(),
            busy_us=read_int(self.busy_path),
            current_freq_mhz=first_existing_int(
                (
                    freq_dir / "current_freq",
                    self.device_path / "npu_current_frequency_mhz",
                )
            ),
            max_freq_mhz=first_existing_int(
                (
                    freq_dir / "hw_max_freq",
                    freq_dir / "set_max_freq",
                    self.device_path / "npu_max_frequency_mhz",
                )
            ),
            min_freq_mhz=first_existing_int(
                (
                    freq_dir / "hw_min_freq",
                    freq_dir / "set_min_freq",
                )
            ),
            efficient_freq_mhz=read_int(freq_dir / "hw_efficient_freq"),
            memory_bytes=read_int(self.device_path / "npu_memory_utilization"),
            runtime_status=read_text(self.device_path / "power" / "runtime_status"),
            power_state=read_text(self.device_path / "power_state"),
            sched_mode=first_existing_text(
                (
                    self.device_path / "sched_mode",
                    self.module_path("parameters/sched_mode"),
                )
            ),
            module_version=read_text(self.module_path("version")),
            vendor=read_text(self.device_path / "vendor"),
            device=read_text(self.device_path / "device"),
            class_code=read_text(self.device_path / "class"),
        )


@dataclass(frozen=True)
class RawSample:
    timestamp: float
    busy_us: Optional[int]
    current_freq_mhz: Optional[int]
    max_freq_mhz: Optional[int]
    min_freq_mhz: Optional[int]
    efficient_freq_mhz: Optional[int]
    memory_bytes: Optional[int]
    runtime_status: Optional[str]
    power_state: Optional[str]
    sched_mode: Optional[str]
    module_version: Optional[str]
    vendor: Optional[str]
    device: Optional[str]
    class_code: Optional[str]


@dataclass(frozen=True)
class Sample:
    raw: RawSample
    util_percent: Optional[float]
    interval_s: Optional[float]
    counter_reset: bool = False


def sample_from_raw(current: RawSample, previous: Optional[RawSample]) -> Sample:
    if previous is None or current.busy_us is None or previous.busy_us is None:
        return Sample(current, None, None)

    wall_us = (current.timestamp - previous.timestamp) * 1_000_000.0
    busy_delta = current.busy_us - previous.busy_us
    if wall_us <= 0 or busy_delta < 0:
        return Sample(current, None, None, counter_reset=True)

    return Sample(current, clamp_percent((busy_delta / wall_us) * 100.0), wall_us / 1_000_000.0)


def accel_to_device_path(class_path: Path) -> Optional[Path]:
    device_link = class_path / "device"
    if device_link.exists() or device_link.is_symlink():
        return device_link.resolve()

    resolved = class_path.resolve()
    if resolved.parent.name == "accel" and resolved.parent.parent.exists():
        return resolved.parent.parent
    return None


def devnode_for_accel(name: str) -> Optional[Path]:
    devnode = Path("/dev/accel") / name
    return devnode if devnode.exists() else None


def discover_devices(sysfs_root: Path = Path("/sys")) -> list[NPUDevice]:
    devices: list[NPUDevice] = []
    seen: set[Path] = set()

    accel_root = sysfs_root / "class" / "accel"
    for class_path in sorted(accel_root.glob("accel*")):
        device_path = accel_to_device_path(class_path)
        if device_path is None:
            continue
        if not (device_path / "npu_busy_time_us").exists():
            continue
        resolved = device_path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        devices.append(
            NPUDevice(
                name=class_path.name,
                class_path=class_path,
                devnode=devnode_for_accel(class_path.name),
                device_path=resolved,
                sysfs_root=sysfs_root,
            )
        )

    driver_root = sysfs_root / "bus" / "pci" / "drivers" / "intel_vpu"
    if driver_root.exists():
        for entry in sorted(driver_root.iterdir()):
            if not PCI_RE.match(entry.name):
                continue
            device_path = entry.resolve()
            if not (device_path / "npu_busy_time_us").exists():
                continue
            if device_path in seen:
                continue
            seen.add(device_path)
            accel_names = sorted((device_path / "accel").glob("accel*"))
            class_path = None
            devnode = None
            name = entry.name
            if accel_names:
                name = accel_names[0].name
                class_candidate = sysfs_root / "class" / "accel" / name
                class_path = class_candidate if class_candidate.exists() else accel_names[0]
                devnode = devnode_for_accel(name)
            devices.append(
                NPUDevice(
                    name=name,
                    class_path=class_path,
                    devnode=devnode,
                    device_path=device_path,
                    sysfs_root=sysfs_root,
                )
            )

    return devices


def device_from_path(path_text: str, sysfs_root: Path = Path("/sys")) -> NPUDevice:
    path = Path(path_text)
    if str(path).startswith("/dev/accel/"):
        class_path = sysfs_root / "class" / "accel" / path.name
        device_path = accel_to_device_path(class_path)
        if device_path is None:
            raise SystemExit(f"Cannot resolve {path_text} to a sysfs NPU device")
        return NPUDevice(path.name, device_path.resolve(), class_path, path, sysfs_root)

    if path.name.startswith("accel") and not path.exists():
        class_path = sysfs_root / "class" / "accel" / path.name
        device_path = accel_to_device_path(class_path)
        if device_path is None:
            raise SystemExit(f"Cannot resolve {path_text} to a sysfs NPU device")
        return NPUDevice(path.name, device_path.resolve(), class_path, devnode_for_accel(path.name), sysfs_root)

    if not path.exists():
        raise SystemExit(f"Device path does not exist: {path_text}")

    if path.name.startswith("accel"):
        device_path = accel_to_device_path(path)
        if device_path is None:
            raise SystemExit(f"Cannot resolve {path_text} to a PCI device")
        return NPUDevice(path.name, device_path.resolve(), path, devnode_for_accel(path.name), sysfs_root)

    device_path = path.resolve()
    if not (device_path / "npu_busy_time_us").exists():
        raise SystemExit(f"Missing npu_busy_time_us under {device_path}")
    accel_names = sorted((device_path / "accel").glob("accel*"))
    name = accel_names[0].name if accel_names else device_path.name
    return NPUDevice(name, device_path, None, devnode_for_accel(name), sysfs_root)


def select_device(args: argparse.Namespace) -> NPUDevice:
    if args.device:
        return device_from_path(args.device)
    devices = discover_devices()
    if not devices:
        raise SystemExit(
            "No Intel AI Boost NPU found. Looked under /sys/class/accel and "
            "/sys/bus/pci/drivers/intel_vpu. Use a recent kernel with the "
            "intel_vpu driver and NPU firmware enabled."
        )
    return devices[0]


def render_bar(percent: Optional[float], width: int) -> str:
    width = max(1, width)
    if percent is None:
        return "[" + ("?" * width) + "]"
    filled = round((clamp_percent(percent) / 100.0) * width)
    return "[" + ("#" * filled) + ("-" * (width - filled)) + "]"


def render_history(values: Iterable[Optional[float]], width: int) -> str:
    chars = " .:-=+*#%@"
    values_list = list(values)[-max(1, width) :]
    out = []
    for value in values_list:
        if value is None:
            out.append("?")
            continue
        idx = round((clamp_percent(value) / 100.0) * (len(chars) - 1))
        out.append(chars[idx])
    return "".join(out).rjust(width)


def sample_to_dict(device: NPUDevice, sample: Sample) -> dict[str, object]:
    raw = sample.raw
    return {
        "name": device.name,
        "pci_id": device.pci_id,
        "driver": device.driver,
        "devnode": str(device.devnode) if device.devnode else None,
        "device_path": str(device.device_path),
        "util_percent": sample.util_percent,
        "interval_s": sample.interval_s,
        "busy_time_us": raw.busy_us,
        "current_freq_mhz": raw.current_freq_mhz,
        "max_freq_mhz": raw.max_freq_mhz,
        "min_freq_mhz": raw.min_freq_mhz,
        "efficient_freq_mhz": raw.efficient_freq_mhz,
        "memory_bytes": raw.memory_bytes,
        "runtime_status": raw.runtime_status,
        "power_state": raw.power_state,
        "sched_mode": raw.sched_mode,
        "module_version": raw.module_version,
        "vendor": raw.vendor,
        "device": raw.device,
        "class_code": raw.class_code,
        "counter_reset": sample.counter_reset,
    }


def format_sample_line(device: NPUDevice, sample: Sample) -> str:
    raw = sample.raw
    util = "--.-%" if sample.util_percent is None else f"{sample.util_percent:5.1f}%"
    freq = "--"
    if raw.current_freq_mhz is not None and raw.max_freq_mhz is not None:
        freq = f"{raw.current_freq_mhz}/{raw.max_freq_mhz} MHz"
    elif raw.current_freq_mhz is not None:
        freq = f"{raw.current_freq_mhz} MHz"
    return (
        f"{time.strftime('%H:%M:%S')} {device.name} {device.pci_id} "
        f"util {util} freq {freq} mem {format_bytes(raw.memory_bytes)} "
        f"power {raw.runtime_status or '--'} state {raw.power_state or '--'} "
        f"sched {raw.sched_mode or '--'} busy {format_seconds_from_us(raw.busy_us)}"
    )


def addstr(screen: curses.window, y: int, x: int, text: str, attr: int = 0) -> None:
    height, width = screen.getmaxyx()
    if y < 0 or y >= height or x >= width:
        return
    clipped = text[: max(0, width - x - 1)]
    try:
        screen.addstr(y, x, clipped, attr)
    except curses.error:
        pass


def draw_screen(
    screen: curses.window,
    device: NPUDevice,
    sample: Sample,
    history: deque[Optional[float]],
    interval: float,
) -> None:
    screen.erase()
    height, width = screen.getmaxyx()
    if height < 12 or width < 50:
        addstr(screen, 0, 0, "npu-top: terminal too small")
        screen.refresh()
        return

    raw = sample.raw
    header = f"npu-top {time.strftime('%H:%M:%S')}  Intel AI Boost / {device.driver}"
    addstr(screen, 0, 0, header.ljust(width - 1), curses.A_REVERSE)

    devnode = str(device.devnode) if device.devnode else "--"
    addstr(screen, 2, 2, f"Device : {device.name}  dev {devnode}  PCI {device.pci_id}")
    addstr(
        screen,
        3,
        2,
        f"IDs    : vendor {raw.vendor or '--'}  device {raw.device or '--'}  class {raw.class_code or '--'}",
    )
    addstr(
        screen,
        4,
        2,
        f"Power  : runtime {raw.runtime_status or '--'}  state {raw.power_state or '--'}  sched {raw.sched_mode or '--'}",
    )

    percent_text = "--.-%" if sample.util_percent is None else f"{sample.util_percent:5.1f}%"
    bar_width = min(50, max(12, width - 28))
    addstr(screen, 6, 2, f"Usage  : {render_bar(sample.util_percent, bar_width)} {percent_text}")

    freq = "--"
    if raw.current_freq_mhz is not None and raw.max_freq_mhz is not None:
        freq = f"{raw.current_freq_mhz} / {raw.max_freq_mhz} MHz"
    elif raw.current_freq_mhz is not None:
        freq = f"{raw.current_freq_mhz} MHz"
    addstr(screen, 7, 2, f"Freq   : {freq}")
    addstr(screen, 8, 2, f"Memory : {format_bytes(raw.memory_bytes)} resident")
    addstr(screen, 9, 2, f"Busy   : {format_seconds_from_us(raw.busy_us)} total")

    graph_width = max(10, width - 14)
    addstr(screen, 11, 2, "History: " + render_history(history, graph_width))

    module = raw.module_version or "--"
    addstr(screen, height - 3, 2, f"Module : intel_vpu {module}")
    addstr(screen, height - 2, 2, f"Update : every {interval:.1f}s")
    addstr(screen, height - 1, 0, "q quit  r reset  +/- interval".ljust(width - 1), curses.A_REVERSE)
    screen.refresh()


def run_curses(device: NPUDevice, interval: float) -> None:
    def inner(screen: curses.window) -> None:
        signal.signal(signal.SIGINT, signal.default_int_handler)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        screen.nodelay(True)
        history: deque[Optional[float]] = deque(maxlen=240)
        previous: Optional[RawSample] = None
        current_interval = interval

        while True:
            raw = device.read_raw()
            sample = sample_from_raw(raw, previous)
            previous = raw
            history.append(sample.util_percent)
            draw_screen(screen, device, sample, history, current_interval)

            deadline = time.monotonic() + current_interval
            while time.monotonic() < deadline:
                try:
                    key = screen.getch()
                except curses.error:
                    key = -1
                if key in (ord("q"), ord("Q"), 27):
                    return
                if key in (ord("r"), ord("R")):
                    history.clear()
                    previous = None
                    break
                if key in (ord("+"), ord("=")):
                    current_interval = max(0.2, current_interval - 0.1)
                    break
                if key in (ord("-"), ord("_")):
                    current_interval = min(10.0, current_interval + 0.1)
                    break
                time.sleep(0.05)

    curses.wrapper(inner)


def collect_sample(device: NPUDevice, interval: float) -> Sample:
    previous = device.read_raw()
    time.sleep(interval)
    return sample_from_raw(device.read_raw(), previous)


def print_devices(devices: list[NPUDevice], as_json: bool) -> None:
    if as_json:
        payload = []
        for device in devices:
            raw = device.read_raw()
            payload.append(sample_to_dict(device, Sample(raw, None, None)))
        print(json.dumps(payload, indent=2, sort_keys=True))
        return

    if not devices:
        print("No Intel AI Boost NPU devices found.")
        return
    for index, device in enumerate(devices):
        raw = device.read_raw()
        devnode = str(device.devnode) if device.devnode else "--"
        print(
            f"{index}: {device.name} dev={devnode} pci={device.pci_id} "
            f"driver={device.driver} path={device.device_path} "
            f"module={raw.module_version or '--'}"
        )


def positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="npu-top",
        description="Top-like monitor for Intel AI Boost NPUs using intel_vpu sysfs counters.",
    )
    parser.add_argument("-d", "--device", help="Device path, accel name, or /dev/accel node")
    parser.add_argument("-i", "--interval", type=positive_float, default=DEFAULT_INTERVAL, help="Refresh interval in seconds")
    parser.add_argument("-1", "--once", action="store_true", help="Print one measured sample and exit")
    parser.add_argument("--stream", action="store_true", help="Print samples continuously without curses")
    parser.add_argument("--json", action="store_true", help="Use JSON output with --once, --stream, or --list")
    parser.add_argument("--list", action="store_true", help="List detected Intel AI Boost NPU devices")
    parser.add_argument("--no-curses", action="store_true", help="Use line output instead of the curses UI")
    parser.add_argument("--sysfs-root", default="/sys", help=argparse.SUPPRESS)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.interval < 1.0 and not args.json:
        print("warning: kernel documentation recommends a 1 second read period for npu_busy_time_us", file=sys.stderr)

    sysfs_root = Path(args.sysfs_root)
    if args.list:
        print_devices(discover_devices(sysfs_root), args.json)
        return 0

    if args.device:
        device = device_from_path(args.device, sysfs_root)
    else:
        devices = discover_devices(sysfs_root)
        if not devices:
            print(
                "No Intel AI Boost NPU found. Looked under /sys/class/accel and "
                "/sys/bus/pci/drivers/intel_vpu.",
                file=sys.stderr,
            )
            return 1
        device = devices[0]

    if args.once or (not sys.stdout.isatty() and not args.stream):
        sample = collect_sample(device, args.interval)
        if args.json:
            print(json.dumps(sample_to_dict(device, sample), indent=2, sort_keys=True))
        else:
            print(format_sample_line(device, sample))
        return 0

    if args.stream or args.no_curses:
        previous: Optional[RawSample] = None
        while True:
            raw = device.read_raw()
            sample = sample_from_raw(raw, previous)
            previous = raw
            if args.json:
                print(json.dumps(sample_to_dict(device, sample), sort_keys=True), flush=True)
            else:
                print(format_sample_line(device, sample), flush=True)
            time.sleep(args.interval)

    run_curses(device, args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
