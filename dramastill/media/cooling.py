from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path

from dramastill.config.defaults import VideoSettings


@dataclass(frozen=True)
class Reading:
    cpu_temp_c: float | None = None
    gpu_temp_c: float | None = None
    cpu_percent: float | None = None
    gpu_percent: float | None = None
    ram_used_percent: float | None = None
    ram_available_mb: float | None = None
    rss_mb: float | None = None
    user_active: bool = False
    sensors_ok: bool = False


@dataclass
class CoolingState:
    level: int = 0
    hot_streak: int = 0
    cool_streak: int = 0
    last_step: float = 0.0


@dataclass(frozen=True)
class Decision:
    mode: str
    level: int
    projects: int
    chunk_workers: int
    encode_threads: int
    allow_nvenc: bool
    nice: int
    sensors_ok: bool
    summary: str


_lock = threading.Lock()
_state = CoolingState()
_cached: tuple[float, tuple, Decision] | None = None
_probe: Reading | None = None
_cpu_sample: tuple[int, int] | None = None
_gpu_missing = False
_windows_temp: float | None = None
_windows_temp_tried = False


def cpu_count() -> int:
    return max(1, os.cpu_count() or 1)


def reset_for_tests(probe: Reading | None = None) -> None:
    global _state, _cached, _probe, _cpu_sample
    with _lock:
        _state = CoolingState()
        _cached = None
        _probe = probe
        _cpu_sample = None


def mode_of(settings: VideoSettings) -> str:
    name = (getattr(settings, "cooling_mode", "") or "").strip()
    if name in {"eco", "balanced", "cool"}:
        return name
    if settings.performance_mode == "low" or settings.render_mode == "low_resource":
        return "eco"
    if settings.performance_mode == "maximum":
        return "cool"
    return "balanced"


def evaluate(settings: VideoSettings, reading: Reading, state: CoolingState, now: float) -> tuple[Decision, CoolingState]:
    state = replace(state)
    limit = _clamp(getattr(settings, "temp_limit_c", 82) or 82, 60, 95)
    warning = min(limit - 3, _clamp(getattr(settings, "temp_warning_c", 72) or 72, 45, limit - 3))
    temp = _hottest(reading)
    hot = _is_hot(settings, reading, temp, limit)
    cool = _is_cool(settings, reading, temp, warning)
    if hot:
        state.hot_streak += 1
        state.cool_streak = 0
    elif cool:
        state.cool_streak += 1
        state.hot_streak = 0
    else:
        state.hot_streak = 0
        state.cool_streak = 0
    if now - state.last_step >= 20:
        if state.hot_streak >= 2 and state.level < 2:
            state.level += 1
            state.hot_streak = 0
            state.last_step = now
        elif state.cool_streak >= 3 and state.level > 0:
            state.level -= 1
            state.cool_streak = 0
            state.last_step = now

    mode = mode_of(settings)
    quiet = settings.render_mode == "low_resource" or mode == "eco"
    cores = cpu_count()
    budget = settings.cpu_threads if settings.cpu_threads > 0 else cores
    sensors = bool(reading.sensors_ok and temp is not None)
    if quiet or (mode == "cool" and not sensors):
        workers = 1
    elif mode == "cool" and sensors and state.level == 0 and budget >= 4:
        workers = min(2, max(1, budget - 1))
    else:
        workers = 1
    if state.level >= 1:
        workers = 1
    threads = max(1, budget // workers)
    if quiet:
        threads = min(threads, 2)
    elif mode == "balanced" or (mode == "cool" and not sensors):
        threads = min(threads, max(1, cores // 2))
    if state.level == 1:
        threads = max(1, threads // 2)
    if state.level >= 2:
        threads = 1

    requested = max(1, int(settings.max_parallel_projects or 1))
    if quiet:
        projects = 1
    elif mode == "cool" and sensors and state.level == 0:
        projects = min(requested, 3)
    else:
        projects = min(requested, 2)
    if reading.user_active:
        projects = min(projects, 1)
    if state.level == 1:
        projects = min(projects, 1)
    ram_cap = int(getattr(settings, "max_ram_mb", 0) or 0)
    if state.level >= 2 or (ram_cap > 0 and reading.rss_mb is not None and reading.rss_mb > ram_cap):
        projects = 0

    if settings.encoder == "libx264":
        nvenc = False
    elif settings.encoder == "h264_nvenc" and state.level < 2:
        nvenc = True
    elif mode == "cool" and sensors and state.level == 0 and not quiet:
        nvenc = True
    else:
        nvenc = False

    nice = {"eco": 10, "balanced": 5, "cool": 1}[mode]
    if mode == "cool" and not sensors:
        nice = 5
    if reading.user_active:
        nice += 5
    if state.level == 1:
        nice += 3
    if state.level >= 2:
        nice += 6
    nice = min(19, nice)
    summary = _summary(mode, state.level, projects, threads, workers, sensors, temp, reading)
    decision = Decision(
        mode=mode,
        level=state.level,
        projects=projects,
        chunk_workers=workers,
        encode_threads=threads,
        allow_nvenc=nvenc,
        nice=nice,
        sensors_ok=sensors,
        summary=summary,
    )
    return decision, state


def current(settings: VideoSettings) -> Decision:
    global _cached, _state
    with _lock:
        now = time.monotonic()
        key = _settings_key(settings)
        if _cached is not None and _cached[1] == key and now - _cached[0] < 2.0:
            return _cached[2]
        reading = _probe if _probe is not None else read_machine()
        decision, _state = evaluate(settings, reading, _state, now)
        _cached = (now, key, decision)
        return decision


def apply_priority(nice: int) -> None:
    """Lower the process priority. Never raises priority, never touches clocks or fans."""

    if sys.platform == "win32":
        _windows_priority(nice)
        return
    try:
        current_nice = os.nice(0)
        delta = int(nice) - int(current_nice)
        if delta > 0:
            os.nice(delta)
    except OSError:
        return


def read_machine() -> Reading:
    cpu_temp = _cpu_temperature()
    gpu_temp, gpu_percent = _gpu()
    cpu_percent = _cpu_percent()
    used, available = _memory()
    rss = _process_tree_rss_mb()
    sensors = cpu_temp is not None or gpu_temp is not None
    return Reading(
        cpu_temp_c=cpu_temp,
        gpu_temp_c=gpu_temp,
        cpu_percent=cpu_percent,
        gpu_percent=gpu_percent,
        ram_used_percent=used,
        ram_available_mb=available,
        rss_mb=rss,
        user_active=_user_active(),
        sensors_ok=sensors,
    )


def _settings_key(settings: VideoSettings) -> tuple:
    return (
        mode_of(settings),
        settings.render_mode,
        settings.encoder,
        int(settings.cpu_threads),
        int(settings.max_parallel_projects),
        int(getattr(settings, "temp_warning_c", 72) or 72),
        int(getattr(settings, "temp_limit_c", 82) or 82),
        int(getattr(settings, "max_cpu_percent", 0) or 0),
        int(getattr(settings, "max_gpu_percent", 0) or 0),
        int(getattr(settings, "max_ram_mb", 0) or 0),
    )


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def _hottest(reading: Reading) -> float | None:
    values = [item for item in (reading.cpu_temp_c, reading.gpu_temp_c) if item is not None]
    if not values:
        return None
    return max(values)


def _is_hot(settings: VideoSettings, reading: Reading, temp: float | None, limit: int) -> bool:
    if temp is not None and temp >= limit:
        return True
    cpu_cap = int(getattr(settings, "max_cpu_percent", 0) or 0)
    if cpu_cap > 0 and reading.cpu_percent is not None and reading.cpu_percent >= cpu_cap:
        return True
    gpu_cap = int(getattr(settings, "max_gpu_percent", 0) or 0)
    if gpu_cap > 0 and reading.gpu_percent is not None and reading.gpu_percent >= gpu_cap:
        return True
    return False


def _is_cool(settings: VideoSettings, reading: Reading, temp: float | None, warning: int) -> bool:
    cpu_cap = int(getattr(settings, "max_cpu_percent", 0) or 0)
    gpu_cap = int(getattr(settings, "max_gpu_percent", 0) or 0)
    cpu_ok = reading.cpu_percent is None or cpu_cap <= 0 or reading.cpu_percent <= max(5, cpu_cap - 15)
    gpu_ok = reading.gpu_percent is None or gpu_cap <= 0 or reading.gpu_percent <= max(5, gpu_cap - 15)
    if temp is None:
        return cpu_ok and gpu_ok
    return temp <= warning - 4 and cpu_ok and gpu_ok


def _summary(mode: str, level: int, projects: int, threads: int, workers: int, sensors: bool, temp: float | None, reading: Reading) -> str:
    names = {"eco": "эко", "balanced": "баланс", "cool": "холодная скорость"}
    heat = f"{temp:.0f}°C" if temp is not None else "датчика температуры нет"
    gpu = ""
    if reading.gpu_temp_c is not None:
        gpu = f", видеокарта {reading.gpu_temp_c:.0f}°C"
    hold = ""
    if level == 1:
        hold = " Нагрузка снижена: температура или заданный потолок."
    elif level >= 2:
        hold = " Новые проекты на паузе, пока машина не остынет."
    sensor = "" if sensors else " Без датчика действует осторожный потолок."
    return (
        f"Охлаждение: {names[mode]}, {heat}{gpu}. "
        f"Проектов до {projects}, фрагментов {workers}, потоков кодирования {threads}.{sensor}{hold}"
    )


def _cpu_temperature() -> float | None:
    if sys.platform == "win32":
        return _windows_temperature()
    found: list[float] = []
    thermal = Path("/sys/class/thermal")
    if thermal.exists():
        for zone in thermal.glob("thermal_zone*/temp"):
            found.extend(_read_temp_file(zone))
    hwmon = Path("/sys/class/hwmon")
    if hwmon.exists():
        for node in hwmon.glob("hwmon*/temp*_input"):
            found.extend(_read_temp_file(node))
    if not found:
        return None
    return max(found)


def _read_temp_file(path: Path) -> list[float]:
    try:
        raw = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return []
    value = raw / 1000.0 if raw > 200 else float(raw)
    if 5.0 < value < 120.0:
        return [value]
    return []


def _windows_temperature() -> float | None:
    global _windows_temp_tried, _windows_temp
    if _windows_temp_tried:
        return _windows_temp
    _windows_temp_tried = True
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature | "
                "Select-Object -First 1 -ExpandProperty CurrentTemperature)",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        raw = float((completed.stdout or "").strip())
        celsius = raw / 10.0 - 273.15
    except (OSError, ValueError, subprocess.TimeoutExpired):
        _windows_temp = None
        return None
    _windows_temp = celsius if 5.0 < celsius < 120.0 else None
    return _windows_temp


def _gpu() -> tuple[float | None, float | None]:
    global _gpu_missing
    if _gpu_missing:
        return None, None
    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=temperature.gpu,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        _gpu_missing = True
        return None, None
    if completed.returncode != 0:
        _gpu_missing = True
        return None, None
    line = (completed.stdout or "").strip().splitlines()
    if not line:
        return None, None
    parts = [item.strip() for item in line[0].split(",")]
    if len(parts) < 2:
        return None, None
    try:
        temp = float(parts[0])
        util = float(parts[1])
    except ValueError:
        return None, None
    temp_ok = temp if 0.0 < temp < 120.0 else None
    util_ok = util if 0.0 <= util <= 100.0 else None
    return temp_ok, util_ok


def _cpu_percent() -> float | None:
    global _cpu_sample
    sample = _cpu_times()
    if sample is None:
        return None
    previous = _cpu_sample
    _cpu_sample = sample
    if previous is None:
        return None
    idle = sample[0] - previous[0]
    total = sample[1] - previous[1]
    if total <= 0:
        return None
    return max(0.0, min(100.0, 100.0 * (1.0 - idle / total)))


def _cpu_times() -> tuple[int, int] | None:
    if sys.platform == "win32":
        return _windows_cpu_times()
    path = Path("/proc/stat")
    try:
        parts = path.read_text(encoding="utf-8").splitlines()[0].split()[1:]
        numbers = [int(item) for item in parts]
    except (OSError, IndexError, ValueError):
        return None
    idle = numbers[3] + (numbers[4] if len(numbers) > 4 else 0)
    return idle, sum(numbers)


def _windows_cpu_times() -> tuple[int, int] | None:
    class FileTime(ctypes_filetime()):
        pass

    idle, kernel, user = FileTime(), FileTime(), FileTime()
    try:
        ok = ctypes_windll().kernel32.GetSystemTimes(
            ctypes_byref(idle), ctypes_byref(kernel), ctypes_byref(user)
        )
    except (AttributeError, OSError):
        return None
    if not ok:
        return None
    idle_v = (idle.dwHighDateTime << 32) + idle.dwLowDateTime
    total = (kernel.dwHighDateTime << 32) + kernel.dwLowDateTime
    total += (user.dwHighDateTime << 32) + user.dwLowDateTime
    return int(idle_v), int(total)


def _memory() -> tuple[float | None, float | None]:
    if sys.platform == "win32":
        return _windows_memory()
    path = Path("/proc/meminfo")
    if not path.exists():
        return None, None
    info: dict[str, int] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            key, raw = line.split(":", 1)
            info[key] = int(raw.strip().split()[0])
        total = info["MemTotal"]
        available = info["MemAvailable"]
    except (OSError, KeyError, ValueError, IndexError):
        return None, None
    if total <= 0:
        return None, None
    return 100.0 * (1.0 - available / total), available / 1024.0


def _windows_memory() -> tuple[float | None, float | None]:
    class Status(ctypes_structure()):
        _fields_ = [
            ("dwLength", ctypes_c_ulong()),
            ("dwMemoryLoad", ctypes_c_ulong()),
            ("ullTotalPhys", ctypes_c_ulonglong()),
            ("ullAvailPhys", ctypes_c_ulonglong()),
            ("ullTotalPageFile", ctypes_c_ulonglong()),
            ("ullAvailPageFile", ctypes_c_ulonglong()),
            ("ullTotalVirtual", ctypes_c_ulonglong()),
            ("ullAvailVirtual", ctypes_c_ulonglong()),
            ("ullAvailExtendedVirtual", ctypes_c_ulonglong()),
        ]

    status = Status()
    status.dwLength = ctypes_sizeof(status)
    try:
        ok = ctypes_windll().kernel32.GlobalMemoryStatusEx(ctypes_byref(status))
    except (AttributeError, OSError):
        return None, None
    if not ok:
        return None, None
    return float(status.dwMemoryLoad), float(status.ullAvailPhys) / (1024.0 * 1024.0)


def _process_tree_rss_mb() -> float | None:
    if sys.platform == "win32":
        return _windows_rss_mb()
    total_kb = 0
    seen: set[int] = set()
    pending = [os.getpid()]
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        status = Path(f"/proc/{pid}/status")
        try:
            text = status.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            if line.startswith("VmRSS:"):
                parts = line.split()
                if len(parts) >= 2:
                    total_kb += int(parts[1])
                break
        for child in _children(pid):
            pending.append(child)
    if total_kb <= 0:
        return None
    return total_kb / 1024.0


def _children(pid: int) -> list[int]:
    found: list[int] = []
    proc = Path("/proc")
    try:
        entries = list(proc.iterdir())
    except OSError:
        return found
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8")
            rest = stat.rsplit(")", 1)[1].split()
            ppid = int(rest[1])
        except (OSError, IndexError, ValueError):
            continue
        if ppid == pid:
            found.append(int(entry.name))
    return found


def _windows_rss_mb() -> float | None:
    class Counters(ctypes_structure()):
        _fields_ = [
            ("cb", ctypes_c_ulong()),
            ("PageFaultCount", ctypes_c_ulong()),
            ("PeakWorkingSetSize", ctypes_c_size_t()),
            ("WorkingSetSize", ctypes_c_size_t()),
            ("QuotaPeakPagedPoolUsage", ctypes_c_size_t()),
            ("QuotaPagedPoolUsage", ctypes_c_size_t()),
            ("QuotaPeakNonPagedPoolUsage", ctypes_c_size_t()),
            ("QuotaNonPagedPoolUsage", ctypes_c_size_t()),
            ("PagefileUsage", ctypes_c_size_t()),
            ("PeakPagefileUsage", ctypes_c_size_t()),
        ]

    counters = Counters()
    counters.cb = ctypes_sizeof(counters)
    try:
        handle = ctypes_windll().kernel32.GetCurrentProcess()
        ok = ctypes_windll().psapi.GetProcessMemoryInfo(handle, ctypes_byref(counters), counters.cb)
    except (AttributeError, OSError):
        return None
    if not ok:
        return None
    return float(counters.WorkingSetSize) / (1024.0 * 1024.0)


def _user_active() -> bool:
    if sys.platform != "win32":
        return False
    class LastInput(ctypes_structure()):
        _fields_ = [("cbSize", ctypes_c_uint()), ("dwTime", ctypes_c_uint())]

    info = LastInput()
    info.cbSize = ctypes_sizeof(info)
    try:
        ok = ctypes_windll().user32.GetLastInputInfo(ctypes_byref(info))
        tick = ctypes_windll().kernel32.GetTickCount()
    except (AttributeError, OSError):
        return False
    if not ok:
        return False
    idle_ms = (int(tick) - int(info.dwTime)) & 0xFFFFFFFF
    return idle_ms < 20000


def _windows_priority(nice: int) -> None:
    if nice >= 12:
        priority = 0x00000040
    elif nice >= 5:
        priority = 0x00004000
    else:
        priority = 0x00000020
    try:
        handle = ctypes_windll().kernel32.GetCurrentProcess()
        ctypes_windll().kernel32.SetPriorityClass(handle, priority)
    except (AttributeError, OSError):
        return


def ctypes_windll():
    import ctypes

    return ctypes.windll


def ctypes_byref(value):
    import ctypes

    return ctypes.byref(value)


def ctypes_sizeof(value) -> int:
    import ctypes

    return ctypes.sizeof(value)


def ctypes_structure():
    import ctypes

    return ctypes.Structure


def ctypes_c_ulong():
    import ctypes

    return ctypes.c_ulong


def ctypes_c_ulonglong():
    import ctypes

    return ctypes.c_ulonglong


def ctypes_c_size_t():
    import ctypes

    return ctypes.c_size_t


def ctypes_c_uint():
    import ctypes

    return ctypes.c_uint


def ctypes_filetime():
    import ctypes

    class FileTime(ctypes.Structure):
        _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]

    return FileTime
