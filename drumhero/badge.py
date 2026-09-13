"""The LIVE badge: a small floating window, top right of every screen, over everything.

Owned by the stream daemon (twitch.daemon): it exists exactly while the stream process does,
so seeing it means being live, whether the game is open or not. Cocoa through PyObjC:
a borderless transparent window at the top of the window levels (over a fullscreen Space
too: FullScreenAuxiliary), on every Space (CanJoinAllSpaces) and one per display, stationary
in Mission Control, no Dock icon (accessory activation policy), never takes the focus. A red
pill "LIVE mm:ss" with a blinking dot (amber STARTING until ffmpeg reports, dim STOPPING,
the miss colour with the reason after an unrequested end) and an x that calls `on_close`
(the daemon stops the stream). Also captured by the stream: viewers see it too.

    python -m drumhero.badge --demo     # shows one for 10 s, cycling the states
"""
import sys
import time

import objc
from AppKit import (NSApplication, NSApplicationActivationPolicyAccessory, NSBackingStoreBuffered, NSBezierPath, NSColor,
                    NSFont, NSFontWeightBold, NSMakeRect, NSScreen, NSView, NSWindow, NSWindowCollectionBehaviorCanJoinAllSpaces,
                    NSWindowCollectionBehaviorFullScreenAuxiliary, NSWindowCollectionBehaviorIgnoresCycle,
                    NSWindowCollectionBehaviorStationary, NSWindowStyleMaskBorderless, NSFontAttributeName,
                    NSForegroundColorAttributeName, NSAttributedString)
from Foundation import NSObject, NSTimer, NSRunLoop, NSRunLoopCommonModes

H = 26                 # the pill's height in points
MAX_LEVEL = 2147483631   # kCGMaximumWindowLevel: over a fullscreen Space's shielding level too
MARGIN = 8             # from the screen's top right corner
TICK_S = 0.5           # the daemon's callback and the redraw run this often
COLORS = {             # state -> (text, fill)
    "live": ((1.0, 1.0, 1.0), (0.78, 0.12, 0.16)),
    "starting": ((0.08, 0.08, 0.1), (0.92, 0.67, 0.16)),
    "stopping": ((0.86, 0.86, 0.86), (0.27, 0.27, 0.31)),
    "ended": ((0.95, 0.35, 0.35), (0.09, 0.09, 0.11)),
}


def _rgb(c, a=1.0):
    return NSColor.colorWithSRGBRed_green_blue_alpha_(c[0], c[1], c[2], a)


class BadgeView(NSView):
    """The pill and the x, drawn by hand; a click on the x calls the badge's on_close."""

    def initWithFrame_(self, frame):
        self = objc.super(BadgeView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.state = "starting"
        self.label = ""
        self.warn = ""
        self.hot = False
        self.badge = None
        return self

    def isFlipped(self):
        return False

    def text_width(self, s, font):
        return NSAttributedString.alloc().initWithString_attributes_(s, {NSFontAttributeName: font}).size().width if s else 0

    def font(self):
        return NSFont.monospacedSystemFontOfSize_weight_(13, NSFontWeightBold)

    def layout_width(self):
        """The window's width for the current text: the pill, a gap, the x square."""
        f = self.font()
        return 24 + self.text_width(self.label, f) + (self.text_width(self.warn, f) + 6 if self.warn else 0) + 10 + 6 + H

    def drawRect_(self, rect):
        f = self.font()
        text_c, fill_c = COLORS[self.state]
        w = self.layout_width()
        pill_w = w - 6 - H
        pill = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(NSMakeRect(0, 0, pill_w, H), H / 2, H / 2)
        _rgb(fill_c).setFill()
        pill.fill()
        if self.state == "ended":
            _rgb(text_c).setStroke()
            pill.setLineWidth_(1)
            pill.stroke()
        blink = self.state != "live" or int(time.time() * 2) % 2 == 0
        _rgb(text_c if blink else fill_c).setFill()
        NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(8, H / 2 - 5, 10, 10)).fill()
        attrs = {NSFontAttributeName: f, NSForegroundColorAttributeName: _rgb(text_c)}
        x = 24
        NSAttributedString.alloc().initWithString_attributes_(self.label, attrs).drawAtPoint_((x, (H - 16) / 2))
        if self.warn:
            x += self.text_width(self.label, f) + 6
            wattrs = {NSFontAttributeName: f, NSForegroundColorAttributeName: _rgb((1.0, 0.88, 0.47))}
            NSAttributedString.alloc().initWithString_attributes_(self.warn, wattrs).drawAtPoint_((x, (H - 16) / 2))
        # the x
        bx = NSMakeRect(pill_w + 6, 0, H, H)
        box = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(bx, 6, 6)
        _rgb((0.78, 0.12, 0.16) if self.hot else (0.09, 0.09, 0.11)).setFill()
        box.fill()
        _rgb((1, 1, 1) if self.hot else (0.47, 0.47, 0.51)).setStroke()
        box.setLineWidth_(1)
        box.stroke()
        xattrs = {NSFontAttributeName: NSFont.systemFontOfSize_(18), NSForegroundColorAttributeName: _rgb((1, 1, 1) if self.hot else (0.86, 0.86, 0.86))}
        xs = NSAttributedString.alloc().initWithString_attributes_("×", xattrs)
        sz = xs.size()
        xs.drawAtPoint_((bx.origin.x + (H - sz.width) / 2, (H - sz.height) / 2 + 1))

    def x_rect(self):
        return NSMakeRect(self.layout_width() - H, 0, H, H)

    def mouseDown_(self, event):
        p = self.convertPoint_fromView_(event.locationInWindow(), None)
        r = self.x_rect()
        if r.origin.x <= p.x <= r.origin.x + r.size.width and 0 <= p.y <= H and self.badge is not None:
            self.badge.on_close()

    def mouseMoved_(self, event):
        p = self.convertPoint_fromView_(event.locationInWindow(), None)
        r = self.x_rect()
        hot = r.origin.x <= p.x <= r.origin.x + r.size.width and 0 <= p.y <= H
        if hot != self.hot:
            self.hot = hot
            self.setNeedsDisplay_(True)

    def mouseExited_(self, event):
        if self.hot:
            self.hot = False
            self.setNeedsDisplay_(True)

    def acceptsFirstMouse_(self, event):
        return True


class Ticker(NSObject):
    def initWithBadge_(self, badge):
        self = objc.super(Ticker, self).init()
        self.badge = badge
        return self

    def tick_(self, timer):
        self.badge._tick()


class Badge:
    """`Badge(on_tick, on_close).run()` takes over the main thread: `on_tick()` runs every
    TICK_S and returns (state, label, warn) or None to quit; `on_close()` runs when the x is
    clicked. `set(state, label, warn)` from on_tick is what the windows show."""

    def __init__(self, on_tick, on_close):
        self.on_tick = on_tick
        self.on_close = on_close
        self.app = NSApplication.sharedApplication()
        self.app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        self.windows = {}          # screen frame (as a tuple) -> (window, view)
        self.state, self.label, self.warn = "starting", "STARTING 00:00", ""
        self._screens_at = 0

    def _window(self, screen):
        frame = screen.visibleFrame()
        w = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(NSMakeRect(0, 0, 200, H), NSWindowStyleMaskBorderless,
                                                                          NSBackingStoreBuffered, False)
        w.setLevel_(MAX_LEVEL)
        w.setOpaque_(False)
        w.setBackgroundColor_(NSColor.clearColor())
        w.setHasShadow_(False)
        w.setIgnoresMouseEvents_(False)
        w.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorFullScreenAuxiliary
                                 | NSWindowCollectionBehaviorStationary | NSWindowCollectionBehaviorIgnoresCycle)
        w.setAcceptsMouseMovedEvents_(True)
        view = BadgeView.alloc().initWithFrame_(NSMakeRect(0, 0, 200, H))
        view.badge = self
        w.setContentView_(view)
        w.orderFrontRegardless()
        return w, view

    def _place(self):
        """One window per screen, top right of its visible area (under the menu bar when it is
        there, at the very top when a fullscreen Space hides it); screens come and go."""
        screens = list(NSScreen.screens())
        keys = []
        for s in screens:
            vf = s.visibleFrame()
            key = (vf.origin.x, vf.origin.y, vf.size.width, vf.size.height)
            keys.append(key)
            if key not in self.windows:
                self.windows[key] = self._window(s)
            w, view = self.windows[key]
            width = view.layout_width()
            w.setFrame_display_(NSMakeRect(vf.origin.x + vf.size.width - width - MARGIN, vf.origin.y + vf.size.height - H - MARGIN, width, H), True)
            view.setFrame_(NSMakeRect(0, 0, width, H))
        for key in list(self.windows):
            if key not in keys:
                self.windows.pop(key)[0].orderOut_(None)

    def set(self, state, label, warn=""):
        self.state, self.label, self.warn = state, label, warn
        for w, view in self.windows.values():
            view.state, view.label, view.warn = state, label, warn
            view.setNeedsDisplay_(True)

    def _tick(self):
        r = self.on_tick()
        if r is None:
            self.app.terminate_(None)
            return
        self.set(*r)
        self._place()

    def run(self):
        self._place()
        ticker = Ticker.alloc().initWithBadge_(self)
        timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(TICK_S, ticker, "tick:", None, True)
        NSRunLoop.mainRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
        self.app.run()


def demo(seconds=10):
    t0 = time.time()
    states = [("starting", "STARTING", ""), ("live", "LIVE", ""), ("live", "LIVE", "0.82x  dropped 17"), ("stopping", "STOPPING", ""),
              ("ended", "STREAM ENDED · ffmpeg exited (1)", "")]

    def tick():
        s = time.time() - t0
        if s > seconds:
            return None
        st, label, warn = states[int(s / 2) % len(states)]
        return st, (label if st == "ended" else f"{label} {int(s) // 60:02d}:{int(s) % 60:02d}"), warn

    Badge(tick, lambda: print("x clicked")).run()


if __name__ == "__main__":
    if "--demo" in sys.argv:
        demo(float(sys.argv[sys.argv.index("--demo") + 1]) if sys.argv[-1] != "--demo" else 10)
    else:
        print(__doc__)
