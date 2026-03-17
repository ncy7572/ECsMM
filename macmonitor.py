#!/usr/bin/env python3
"""
macmonitor.py — btop-style macOS System Resource Monitor

Metrics : CPU · GPU · Memory (active/wired/cached) · Swap · NET Upload · NET Download
Refresh : every 3 seconds
GPU requires sudo (run: sudo python3 macmonitor.py)

Requirements: pip install rich psutil
"""

from __future__ import annotations

import json, os, re, sys, time, signal, subprocess, threading
import psutil
from collections import deque
from typing import Optional

try:
    from rich.console import Console, Group
    from rich.live import Live
    from rich.text import Text
    from rich.panel import Panel
    from rich import box
except ImportError:
    sys.exit("Missing dependency — run:  pip install rich psutil")


# ── defaults ──────────────────────────────────────────────────────────────────

HISTORY  = 80           # history samples kept per metric
INTERVAL = 3.0          # seconds between refreshes
SPARK    = "▁▂▃▄▅▆▇█"  # sparkline characters (low → high)
DOT      = "■"          # bar dot character

CLR = dict(
    cpu        = "green3",
    gpu        = "dark_orange",
    mem        = "gold1",
    swap       = "khaki3",
    up         = "medium_purple1",
    dn         = "cyan1",
    title      = "bright_white",
    dim        = "grey42",
    border     = "grey35",
    dot_empty  = "grey15",
)

SHOW = dict(
    cpu  = True,
    gpu  = True,
    mem  = True,
    swap = True,
    up   = True,
    dn   = True,
)

# ── config loader ──────────────────────────────────────────────────────────────

def load_config() -> None:
    """
    Read config.json from the same directory as this script and apply any
    values it contains, falling back silently to the defaults above.
    """
    global HISTORY, INTERVAL, DOT, CLR, SHOW

    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if not os.path.exists(cfg_path):
        return

    try:
        with open(cfg_path) as f:
            cfg = json.load(f)
    except Exception as e:
        print(f"[macmonitor] config.json parse error: {e}", file=sys.stderr)
        return

    if "interval" in cfg:
        INTERVAL = max(float(cfg["interval"]), 0.5)   # floor at 0.5 s
    if "history" in cfg:
        HISTORY  = max(int(cfg["history"]), 10)        # floor at 10 samples
    if "dot" in cfg:
        DOT = str(cfg["dot"])[:1] or DOT              # single character only
    if "colors" in cfg:
        CLR.update({k: v for k, v in cfg["colors"].items() if k in CLR})
    if "show" in cfg and isinstance(cfg["show"], dict):
        SHOW.update({k: bool(v) for k, v in cfg["show"].items() if k in SHOW})

load_config()


# ── tiny helpers ───────────────────────────────────────────────────────────────

def sparkline(data: deque, w: int) -> str:
    """Render the last `w` samples from `data` as a block sparkline."""
    if not data:
        return " " * w
    vals = list(data)[-w:]
    mx   = max(max(vals), 0.001)
    s    = "".join(SPARK[min(int(v / mx * 7), 7)] for v in vals)
    return " " * (w - len(s)) + s


def dot_bar(pct: float, w: int, color: str) -> Text:
    """
    Gradient dot progress bar.
    Filled region: bright leading edge → slightly dimmer tail.
    Empty region : very dark dots so the background texture shows.
    """
    pct = max(0.0, min(100.0, pct))
    n   = int(pct / 100 * w)
    t   = Text()
    # Filled: first 35 % normal (dimmer tail), last 65 % bold (bright leading edge)
    split = int(n * 0.35)
    t.append(DOT * split,       style=color)
    t.append(DOT * (n - split), style=f"bold {color}")
    # Empty: dark dots
    t.append(DOT * (w - n),     style=CLR["dot_empty"])
    return t



def fmt_bps(n: float) -> str:
    for u in ("B ", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:6.1f} {u}/s"
        n /= 1024
    return f"{n:6.1f} PB/s"


def fmt_mem(n: int) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"


def sysctl_int(name: str) -> Optional[int]:
    try:
        r = subprocess.run(
            ["sysctl", "-n", name],
            capture_output=True, text=True, timeout=2,
        )
        if r.returncode != 0:
            return None
        return int(r.stdout.strip())
    except Exception:
        return None


def memory_pressure_level() -> tuple[Optional[int], str]:
    level = sysctl_int("kern.memorystatus_vm_pressure_level")
    labels = {
        0: "normal",
        1: "normal",
        2: "warning",
        4: "critical",
    }
    return level, labels.get(level, f"level {level}" if level is not None else "unknown")


def cpu_brand() -> str:
    try:
        r = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True, text=True, timeout=2,
        )
        name = r.stdout.strip()
        name = re.sub(r"^Apple\s+", "", name)
        return name[:28]
    except Exception:
        return "CPU"


# ── powermetrics reader (GPU only) ────────────────────────────────────────────

class PowerMetrics:
    """
    Spawns `sudo powermetrics` in a background thread and parses GPU usage.
    Fails silently without sudo — gpu_pct stays None.
    """

    def __init__(self) -> None:
        self._lock   = threading.Lock()
        self._gpu    : Optional[float] = None
        self._ready  = False
        self._proc   : Optional[subprocess.Popen] = None
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        cmd = ["sudo", "-n", "powermetrics",
               "--samplers", "gpu_power", "-i", "1000"]
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            )
            buf: list[str] = []
            for raw in self._proc.stdout:          # type: ignore[union-attr]
                line = raw.rstrip()
                if line.startswith("****"):
                    if buf:
                        self._parse(buf)
                        self._ready = True
                    buf = [line]
                else:
                    buf.append(line)
        except Exception:
            pass

    def _parse(self, lines: list[str]) -> None:
        txt = "\n".join(lines)
        m = re.search(r"GPU HW active residency:\s+([\d.]+)%", txt)
        if m:
            with self._lock:
                self._gpu = float(m.group(1))

    def gpu(self) -> Optional[float]:
        with self._lock:
            return self._gpu

    @property
    def ready(self) -> bool:
        return self._ready

    def stop(self) -> None:
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass


# ── data collector ─────────────────────────────────────────────────────────────

class Monitor:
    def __init__(self) -> None:
        self.cpu_h = deque(maxlen=HISTORY)
        self.gpu_h = deque(maxlen=HISTORY)
        self.mem_h = deque(maxlen=HISTORY)
        self.swap_h = deque(maxlen=HISTORY)
        self.up_h  = deque(maxlen=HISTORY)
        self.dn_h  = deque(maxlen=HISTORY)

        self.cpu_pct    : float           = 0.0
        self.gpu_pct    : Optional[float] = None
        # memory
        self.mem_pct    : float = 0.0
        self.mem_active : int   = 0   # recently used pages
        self.mem_wired  : int   = 0   # kernel / pinned pages
        self.mem_cached : int   = 0   # inactive / disk-cache pages
        self.mem_used   : int   = 0   # total non-free (active+wired+cached)
        self.mem_tot    : int   = 0
        self.swap_used  : int   = 0
        self.swap_tot   : int   = 0
        self.mem_pressure_raw   : Optional[int] = None
        self.mem_pressure_level : str = "unknown"
        # network
        self.net_up : float = 0.0
        self.net_dn : float = 0.0

        self._prev_net  = psutil.net_io_counters()
        self._prev_time = time.monotonic()

        self.cpu_name = cpu_brand()
        self.pm       = PowerMetrics()

        psutil.cpu_percent()   # discard first (always-zero) reading

    def update(self) -> None:
        # ── CPU ───────────────────────────────────────────────────────────────
        self.cpu_pct = psutil.cpu_percent()
        self.cpu_h.append(self.cpu_pct)

        # ── Memory ────────────────────────────────────────────────────────────
        m = psutil.virtual_memory()
        self.mem_tot    = m.total
        self.mem_active = getattr(m, "active",   0)
        self.mem_wired  = getattr(m, "wired",    0)
        # inactive = disk cache held by OS (reclaimable)
        self.mem_cached = getattr(m, "inactive", 0)
        # Total "used" = everything that is not free/available
        self.mem_used   = m.total - m.available
        self.mem_pct    = self.mem_used / max(self.mem_tot, 1) * 100
        self.mem_h.append(self.mem_pct)
        s = psutil.swap_memory()
        self.swap_used = s.used
        self.swap_tot  = s.total
        self.swap_h.append(s.percent if s.total else 0.0)
        self.mem_pressure_raw, self.mem_pressure_level = memory_pressure_level()

        # ── Network ───────────────────────────────────────────────────────────
        now = time.monotonic()
        net = psutil.net_io_counters()
        dt  = max(now - self._prev_time, 1e-6)
        self.net_up = max((net.bytes_sent - self._prev_net.bytes_sent) / dt, 0.0)
        self.net_dn = max((net.bytes_recv - self._prev_net.bytes_recv) / dt, 0.0)
        self._prev_net, self._prev_time = net, now
        self.up_h.append(self.net_up)
        self.dn_h.append(self.net_dn)

        # ── GPU (via powermetrics) ─────────────────────────────────────────────
        g = self.pm.gpu()
        self.gpu_pct = g
        if g is not None:
            self.gpu_h.append(g)

    def stop(self) -> None:
        self.pm.stop()


# ── rendering ──────────────────────────────────────────────────────────────────

def pressure_style(level: Optional[int], base_color: str) -> str:
    if level == 4:
        return "bold red1"
    if level == 2:
        return "bold dark_orange"
    return f"bold {base_color}"


def _panel(title: str, val_str: str, pct: float,
           hist: deque, color: str, w: int,
           subtitle_style: Optional[str] = None) -> Panel:
    """Standard metric panel: gradient dot bar + sparkline."""
    inner = w - 6
    subtitle_style = subtitle_style or f"bold {CLR['title']}"
    body  = dot_bar(pct, inner, color)
    body.append("\n")
    body.append(sparkline(hist, inner), style=CLR["dim"])
    return Panel(
        body,
        title          = f"[bold {color}] {title} [/]",
        title_align    = "left",
        subtitle       = f"[{subtitle_style}] {val_str} [/]",
        subtitle_align = "right",
        border_style   = CLR["border"],
        box            = box.ROUNDED,
        padding        = (0, 1),
    )



def build_screen(mon: Monitor, width: int) -> Group:
    def rel_pct(v: float, hist: deque) -> float:
        mx = max(max(hist, default=0.0), 1e-9)
        return v / mx * 100

    gpu_v      = mon.gpu_pct if mon.gpu_pct is not None else 0.0
    gpu_str    = f"{gpu_v:5.1f}%" if mon.gpu_pct is not None else "N/A (needs sudo)"
    swap_pct   = mon.swap_used / max(mon.swap_tot, 1) * 100 if mon.swap_tot else 0.0
    swap_str   = f"{swap_pct:5.1f}%"
    swap_style = pressure_style(mon.mem_pressure_raw, CLR["swap"])

    panels = []
    if SHOW["cpu"]:
        panels.append(
            _panel(f"CPU  [{mon.cpu_name}]", f"{mon.cpu_pct:5.1f}%",
                   mon.cpu_pct, mon.cpu_h, CLR["cpu"], width)
        )

    if SHOW["gpu"]:
        panels.append(
            _panel("GPU", gpu_str,
                   gpu_v, mon.gpu_h, CLR["gpu"], width)
        )

    if SHOW["mem"]:
        panels.append(
            _panel(f"MEM  {fmt_mem(mon.mem_used)} / {fmt_mem(mon.mem_tot)}",
                   f"{mon.mem_pct:5.1f}%",
                   mon.mem_pct, mon.mem_h, CLR["mem"], width)
        )

    if SHOW["swap"]:
        panels.append(
            _panel(f"SWAP  {fmt_mem(mon.swap_used)} / {fmt_mem(mon.swap_tot)}",
                   swap_str,
                   swap_pct, mon.swap_h, CLR["swap"], width,
                   subtitle_style=swap_style)
        )

    if SHOW["up"]:
        panels.append(
            _panel("NET ↑  Upload", fmt_bps(mon.net_up),
                   rel_pct(mon.net_up, mon.up_h), mon.up_h, CLR["up"], width)
        )

    if SHOW["dn"]:
        panels.append(
            _panel("NET ↓  Download", fmt_bps(mon.net_dn),
                   rel_pct(mon.net_dn, mon.dn_h), mon.dn_h, CLR["dn"], width)
        )

    ts  = time.strftime("%H:%M:%S")
    hdr = Text()
    hdr.append("  macmonitor ",              style="bold bright_white on grey15")
    hdr.append("  macOS resource monitor  ", style="dim white on grey11")
    hdr.append(f"  {ts}  ",                 style="bold white on grey15")
    hdr.append("\n")

    sudo_hint = "" if mon.pm.ready else "  ·  run with sudo for GPU"
    ftr = Text(
        f"  Ctrl-C to quit  ·  refreshes every {INTERVAL:.0f}s{sudo_hint}\n",
        style=CLR["dim"],
    )

    return Group(hdr, *panels, ftr)


# ── entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    console = Console()
    mon     = Monitor()

    def _exit(sig, _frame):
        mon.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _exit)
    signal.signal(signal.SIGTERM, _exit)

    mon.update()

    with Live(
        build_screen(mon, console.width),
        console            = console,
        refresh_per_second = 4,
        screen             = True,
    ) as live:
        while True:
            time.sleep(INTERVAL)
            mon.update()
            live.update(build_screen(mon, console.width))


if __name__ == "__main__":
    main()
