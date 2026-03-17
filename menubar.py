#!/usr/bin/env python3
"""
menubar.py — macOS menu bar monitor

Shows three live activity bars in the menu bar:
CPU · GPU · NET

Refreshes every 3 seconds by default via the shared config in macmonitor.py.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

import objc

try:
    from AppKit import (
        NSApp,
        NSApplication,
        NSApplicationActivationPolicyAccessory,
        NSBezierPath,
        NSButton,
        NSColor,
        NSFont,
        NSFontAttributeName,
        NSForegroundColorAttributeName,
        NSImage,
        NSImageOnly,
        NSPopover,
        NSPopoverBehaviorTransient,
        NSStatusBar,
        NSView,
        NSViewController,
    )
    from Foundation import NSMakeRect, NSObject, NSPoint, NSString, NSTimer
except ImportError:
    sys.exit("Missing dependency — run: pip install pyobjc-framework-Cocoa")

from macmonitor import INTERVAL, Monitor, SHOW, fmt_bps, fmt_mem


def rel_pct(value: float, peak: float) -> float:
    return value / max(peak, 1e-9) * 100


def color_for_metric(name: str) -> NSColor:
    colors = {
        "cpu": NSColor.colorWithCalibratedRed_green_blue_alpha_(0.30, 0.67, 0.43, 1.0),
        "gpu": NSColor.colorWithCalibratedRed_green_blue_alpha_(0.77, 0.53, 0.30, 1.0),
        "mem": NSColor.colorWithCalibratedRed_green_blue_alpha_(0.76, 0.63, 0.28, 1.0),
        "net": NSColor.colorWithCalibratedRed_green_blue_alpha_(0.31, 0.63, 0.78, 1.0),
        "idle": NSColor.secondaryLabelColor(),
    }
    return colors[name]


def overlay_mem_label(used: int, total: int) -> str:
    gb = 1024 ** 3
    if used >= gb and total >= gb:
        return f"{used / gb:.0f} / {total / gb:.1f} GB"
    return f"{fmt_mem(used)} / {fmt_mem(total)}"


class DashboardView(NSView):
    def initWithFrame_(self, frame):
        self = objc.super(DashboardView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.snapshot = []
        self.cpu_name = "CPU"
        self.clock = "--:--:--"
        return self

    @objc.python_method
    def update_snapshot(self, cpu_name: str, clock: str, snapshot: list[dict]) -> None:
        self.cpu_name = cpu_name
        self.clock = clock
        self.snapshot = snapshot
        self.setNeedsDisplay_(True)

    @objc.python_method
    def _font(self, size: float, bold: bool = False) -> NSFont:
        return NSFont.systemFontOfSize_weight_(size, 0.6 if bold else 0.0)

    @objc.python_method
    def _mono(self, size: float, bold: bool = False) -> NSFont:
        font = NSFont.userFixedPitchFontOfSize_(size)
        if font is not None:
            return font
        return NSFont.monospacedSystemFontOfSize_weight_(size, 0.6 if bold else 0.0)

    @objc.python_method
    def _draw_text(self, text: str, rect, color: NSColor,
                   size: float = 12.0, bold: bool = False, mono: bool = False,
                   align: Optional[int] = None) -> None:
        font = self._mono(size, bold) if mono else self._font(size, bold)
        para = objc.lookUpClass("NSMutableParagraphStyle").alloc().init()
        if align is not None:
            para.setAlignment_(align)
        NSString.stringWithString_(text).drawInRect_withAttributes_(
            rect,
            {
                NSFontAttributeName: font,
                NSForegroundColorAttributeName: color,
                "NSParagraphStyle": para,
            },
        )

    @objc.python_method
    def _rounded_rect(self, rect, radius: float):
        return NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, radius, radius)

    @objc.python_method
    def _draw_bar(self, x: float, y: float, w: float, h: float, pct: Optional[float], color: NSColor) -> None:
        track = self._rounded_rect(NSMakeRect(x, y, w, h), h / 2)
        NSColor.colorWithWhite_alpha_(0.78, 1.0).setFill()
        track.fill()

        if pct is None:
            return

        fill_w = max(2.0, w * max(0.0, min(100.0, pct)) / 100.0)
        fill = self._rounded_rect(NSMakeRect(x, y, fill_w, h), h / 2)
        color.setFill()
        fill.fill()

    @objc.python_method
    def _draw_bar_overlay_label(self, x: float, y: float, w: float, h: float, text: str) -> None:
        label_h = 9.0
        label_y = y + (h - label_h) / 2.0 + 0.6
        self._draw_text(
            text,
            NSMakeRect(x, label_y, w, label_h),
            NSColor.colorWithWhite_alpha_(0.10, 1.0),
            size=8.5,
            mono=True,
            align=1,
        )

    @objc.python_method
    def _draw_history_bars(self, x: float, y: float, w: float, h: float, hist, color: NSColor) -> None:
        vals = list(hist)
        if not vals:
            return
        slots = max(12, min(28, int(w / 7)))
        bucket = max(1, len(vals) // slots)
        reduced = [
            max(vals[i:i + bucket], default=0.0)
            for i in range(0, len(vals), bucket)
        ][-slots:]
        mx = max(max(reduced), 0.001)
        count = len(reduced)
        gap = 2.0
        bar_w = max(3.0, (w - gap * (count - 1)) / count)
        faded = color.colorWithAlphaComponent_(0.22)
        for idx, val in enumerate(reduced):
            bar_h = max(2.0, (val / mx) * h)
            px = x + idx * (bar_w + gap)
            py = y
            base = self._rounded_rect(NSMakeRect(px, py, bar_w, h), 1.2)
            NSColor.colorWithWhite_alpha_(0.92, 1.0).setFill()
            base.fill()
            fg = self._rounded_rect(NSMakeRect(px, py, bar_w, bar_h), 1.2)
            faded.setFill()
            fg.fill()

    @objc.python_method
    def _draw_card(self, rect, item: dict) -> None:
        bg = self._rounded_rect(rect, 16.0)
        NSColor.colorWithWhite_alpha_(1.0, 0.88).setFill()
        bg.fill()

        pad = 14.0
        title_size = 12.5 if rect.size.height >= 78.0 else 11.5
        value_size = 12.5 if len(item["value"]) <= 6 else 11.0
        subtitle_size = 11.0 if rect.size.height >= 78.0 else 10.0
        title_y = rect.origin.y + rect.size.height - 28.0
        bar_y = rect.origin.y + 34.0
        self._draw_text(
            item["title"],
            NSMakeRect(rect.origin.x + pad, title_y, rect.size.width - 110.0, 18.0),
            item["color"],
            size=title_size,
            bold=True,
        )
        self._draw_text(
            item["value"],
            NSMakeRect(rect.origin.x + rect.size.width - 92.0, title_y, 78.0, 18.0),
            item["color"],
            size=value_size,
            bold=True,
            mono=True,
        )

        self._draw_bar(rect.origin.x + pad, bar_y, rect.size.width - pad * 2, 10.0, item["pct"], item["color"])
        if item["subtitle"]:
            if item.get("subtitle_on_bar", False):
                self._draw_bar_overlay_label(
                    rect.origin.x + pad,
                    bar_y,
                    rect.size.width - pad * 2,
                    10.0,
                    item["subtitle"],
                )
            else:
                self._draw_text(
                    item["subtitle"],
                    NSMakeRect(rect.origin.x + pad, bar_y + 12.0, rect.size.width - pad * 2, 14.0),
                    NSColor.secondaryLabelColor(),
                    size=subtitle_size,
                    mono=True,
                )
        self._draw_history_bars(rect.origin.x + pad, rect.origin.y + 8.0, rect.size.width - pad * 2, 12.0, item["hist"], item["color"])

    def drawRect_(self, _rect) -> None:
        bounds = self.bounds()
        outer = self._rounded_rect(bounds, 18.0)
        NSColor.colorWithCalibratedRed_green_blue_alpha_(0.965, 0.967, 0.975, 0.96).setFill()
        outer.fill()

        self._draw_text(
            "MACMONITOR",
            NSMakeRect(16.0, bounds.size.height - 34.0, 140.0, 18.0),
            NSColor.labelColor(),
            size=13.0,
            bold=True,
        )
        self._draw_text(
            self.clock,
            NSMakeRect(bounds.size.width - 110.0, bounds.size.height - 34.0, 90.0, 18.0),
            NSColor.secondaryLabelColor(),
            size=12.0,
            mono=True,
        )
        self._draw_text(
            self.cpu_name,
            NSMakeRect(16.0, bounds.size.height - 56.0, 200.0, 16.0),
            NSColor.secondaryLabelColor(),
            size=12.0,
        )

        item_count = max(len(self.snapshot), 1)
        cols = 2
        rows = (item_count + cols - 1) // cols
        left = 16.0
        right = 16.0
        top = bounds.size.height - 74.0
        bottom = 10.0
        gap_x = 12.0
        gap_y = 12.0
        card_w = (bounds.size.width - left - right - gap_x) / 2.0
        avail_h = max(top - bottom, 120.0)
        card_h = max(78.0, (avail_h - gap_y * max(rows - 1, 0)) / rows)

        for idx, item in enumerate(self.snapshot[:6]):
            row = idx // 2
            col = idx % 2
            x = left + col * (card_w + gap_x)
            y = top - (row + 1) * card_h - row * gap_y
            self._draw_card(NSMakeRect(x, y, card_w, card_h), item)


class MenuBarApp(NSObject):
    def applicationDidFinishLaunching_(self, _notification) -> None:
        self.monitor = Monitor()
        self.status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(26.0)
        self.button = self.status_item.button()
        self.button.setToolTip_("CPU / GPU / NET")
        self.button.setImagePosition_(NSImageOnly)
        self.button.setTarget_(self)
        self.button.setAction_("togglePopover:")

        self._build_popover()

        self.refresh_(None)
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            INTERVAL, self, "refresh:", None, True
        )

    def applicationWillTerminate_(self, _notification) -> None:
        if getattr(self, "timer", None):
            self.timer.invalidate()
        if getattr(self, "monitor", None):
            self.monitor.stop()

    @objc.python_method
    def _build_popover(self) -> None:
        self.popover = NSPopover.alloc().init()
        self.popover.setBehavior_(NSPopoverBehaviorTransient)

        controller = NSViewController.alloc().init()
        root = NSView.alloc().initWithFrame_(NSMakeRect(0.0, 0.0, 460.0, 430.0))

        self.dashboard = DashboardView.alloc().initWithFrame_(NSMakeRect(12.0, 44.0, 436.0, 374.0))
        root.addSubview_(self.dashboard)

        quit_button = NSButton.alloc().initWithFrame_(NSMakeRect(368.0, 10.0, 80.0, 24.0))
        quit_button.setTitle_("Quit")
        quit_button.setTarget_(self)
        quit_button.setAction_("quit:")
        root.addSubview_(quit_button)

        controller.setView_(root)
        self.popover.setContentSize_((460.0, 430.0))
        self.popover.setContentViewController_(controller)

    @objc.python_method
    def _clock(self) -> str:
        import time
        return time.strftime("%H:%M:%S")

    @objc.python_method
    def _snapshot(self, net_up_pct: float, net_dn_pct: float) -> list[dict]:
        gpu_pct = self.monitor.gpu_pct
        swap_pct = self.monitor.swap_used / max(self.monitor.swap_tot, 1) * 100 if self.monitor.swap_tot else 0.0
        items = []
        if SHOW["cpu"]:
            items.append(dict(title="CPU", value=f"{self.monitor.cpu_pct:5.1f}%", subtitle="", subtitle_on_bar=False, pct=self.monitor.cpu_pct, hist=self.monitor.cpu_h, color=color_for_metric("cpu")))
        if SHOW["gpu"]:
            items.append(dict(title="GPU", value=f"{gpu_pct:5.1f}%" if gpu_pct is not None else "N/A", subtitle="" if gpu_pct is not None else "run with sudo", subtitle_on_bar=False, pct=gpu_pct, hist=self.monitor.gpu_h, color=color_for_metric("gpu")))
        if SHOW["mem"]:
            items.append(dict(title="MEM", value=f"{self.monitor.mem_pct:5.1f}%", subtitle=overlay_mem_label(self.monitor.mem_used, self.monitor.mem_tot), subtitle_on_bar=True, pct=self.monitor.mem_pct, hist=self.monitor.mem_h, color=color_for_metric("mem")))
        if SHOW["swap"]:
            items.append(dict(title="SWAP", value=f"{swap_pct:5.1f}%", subtitle=overlay_mem_label(self.monitor.swap_used, self.monitor.swap_tot), subtitle_on_bar=True, pct=swap_pct, hist=self.monitor.swap_h, color=color_for_metric("mem")))
        if SHOW["up"]:
            items.append(dict(title="NET UP", value=fmt_bps(self.monitor.net_up).strip(), subtitle="", subtitle_on_bar=False, pct=net_up_pct, hist=self.monitor.up_h, color=color_for_metric("net")))
        if SHOW["dn"]:
            items.append(dict(title="NET DN", value=fmt_bps(self.monitor.net_dn).strip(), subtitle="", subtitle_on_bar=False, pct=net_dn_pct, hist=self.monitor.dn_h, color=color_for_metric("net")))
        return items

    @objc.python_method
    def _bar_image(self, cpu_pct: float, gpu_pct: Optional[float], net_pct: float) -> NSImage:
        size = 18.0
        pad_x = 1.0
        pad_y = 1.0
        gap = 1.0
        bar_h = 3.0
        bar_w = size - pad_x * 2
        image = NSImage.alloc().initWithSize_((size, size))
        image.lockFocus()

        rows = [
            ("cpu", cpu_pct, size - pad_y - bar_h),
            ("gpu", gpu_pct, size - pad_y - bar_h * 2 - gap),
            ("mem", self.monitor.mem_pct, size - pad_y - bar_h * 3 - gap * 2),
            ("net", net_pct, size - pad_y - bar_h * 4 - gap * 3),
        ]

        for name, pct, y in rows:
            bg = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(pad_x, y, bar_w, bar_h), 1.0, 1.0
            )
            color_for_metric("idle").setFill()
            bg.fill()

            if pct is not None:
                pct = max(0.0, min(100.0, pct))
                fill_w = max(1.0, bar_w * pct / 100.0)
                fg = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    NSMakeRect(pad_x, y, fill_w, bar_h), 1.0, 1.0
                )
                color_for_metric(name).setFill()
                fg.fill()

        image.unlockFocus()
        image.setTemplate_(False)
        return image

    @objc.python_method
    def _set_icon(self, cpu_pct: float, gpu_pct: Optional[float], net_pct: float) -> None:
        self.button.setTitle_("")
        self.button.setImage_(self._bar_image(cpu_pct, gpu_pct, net_pct))

    def refresh_(self, _timer) -> None:
        self.monitor.update()

        up_peak = max(self.monitor.up_h, default=0.0)
        dn_peak = max(self.monitor.dn_h, default=0.0)
        net_up_pct = rel_pct(self.monitor.net_up, max(up_peak, 1e-9))
        net_dn_pct = rel_pct(self.monitor.net_dn, max(dn_peak, 1e-9))
        net_pct = max(net_up_pct, net_dn_pct)

        self._set_icon(self.monitor.cpu_pct, self.monitor.gpu_pct, net_pct)
        self.dashboard.update_snapshot(self.monitor.cpu_name, self._clock(), self._snapshot(net_up_pct, net_dn_pct))

    def togglePopover_(self, _sender) -> None:
        if self.popover.isShown():
            self.popover.performClose_(None)
        else:
            self.popover.showRelativeToRect_ofView_preferredEdge_(self.button.bounds(), self.button, 3)
            NSApp.activateIgnoringOtherApps_(True)
            window = self.popover.contentViewController().view().window()
            if window is not None:
                window.makeKeyAndOrderFront_(None)

    def quit_(self, _sender) -> None:
        NSApp.terminate_(None)


def main() -> None:
    if os.geteuid() == 0:
        sys.exit(
            "Run menubar.py as your normal user, not with sudo.\n"
            "If you want GPU data, first run `sudo -v`, then run `python3 menubar.py`."
        )
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    delegate = MenuBarApp.alloc().init()
    app.setDelegate_(delegate)
    app.run()


if __name__ == "__main__":
    main()
