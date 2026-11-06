"""HUD 换位覆盖层：实时截取屏幕上的若干区域，成对交换（可旋转）后盖在原处。

- 只读屏幕（GDI BitBlt，和录屏软件同类），不读写游戏进程和文件。
- 覆盖窗口：分层窗口（逐像素 alpha，边缘羽化）、置顶、鼠标穿透、不抢焦点、不被截屏捕获。
- 支持多个"状态"（如技能盘普通 / 按住 RB 激活）：每个状态有一块识别图案（白色字/标志），
  只比对"白色笔画比紧邻的一圈亮多少"，不受半透明 UI 背后画面变化的影响；
  都不匹配（菜单、过场）就隐藏。
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

import numpy as np  # noqa: E402
from PIL import Image, ImageGrab, ImageTk  # noqa: E402

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32
dwmapi = ctypes.windll.dwmapi
winmm = ctypes.windll.winmm

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.CreateWindowExW.restype = wt.HWND
user32.GetDC.argtypes = [wt.HWND]
user32.GetDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
user32.GetForegroundWindow.restype = wt.HWND
user32.PeekMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT]
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.DestroyWindow.argtypes = [wt.HWND]
user32.SetWindowDisplayAffinity.argtypes = [wt.HWND, wt.DWORD]
user32.UpdateLayeredWindow.argtypes = [wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE),
                                       wt.HDC, ctypes.POINTER(wt.POINT), wt.COLORREF,
                                       ctypes.POINTER(BLENDFUNCTION), wt.DWORD]
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.c_void_p, wt.UINT, ctypes.POINTER(ctypes.c_void_p),
                                   wt.HANDLE, wt.DWORD]
gdi32.CreateDIBSection.restype = wt.HBITMAP
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wt.HDC]
gdi32.BitBlt.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         wt.HDC, ctypes.c_int, ctypes.c_int, wt.DWORD]

WS_POPUP = 0x80000000
WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x8, 0x80, 0x08000000
WS_EX_LAYERED, WS_EX_TRANSPARENT = 0x80000, 0x20
WDA_EXCLUDEFROMCAPTURE = 0x11
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
SRCCOPY = 0x00CC0020
PM_REMOVE = 1
WM_HOTKEY = 0x0312
MOD_NOREPEAT = 0x4000
HWND_TOPMOST = wt.HWND(-1)
SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE = 0x2, 0x1, 0x10
ULW_ALPHA, AC_SRC_ALPHA = 2, 1

ROT_NAMES = {0: "不旋转", 1: "顺时针90°", 2: "180°", 3: "逆时针90°"}
SHAPES = ["胶囊", "椭圆", "矩形"]

BASE = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
CONFIG = os.path.join(BASE, "hud_config.json")
DEFAULTS = {
    "states": [],          # [{name, detect:[x,y,w,h], template, pairs:[{a:[x,y,w,h], b:[...], rot:k}]}]
    "match": 60,           # 匹配度（%）≥ 此值显示（从隐藏到显示）
    "switch": 35,          # 已在显示时，另一状态 ≥ 此值且分数更高就立刻切换
    "debug_log": False,    # 逐帧记录匹配度到 hud_debug.csv，事件后的过渡帧存到 hud_debug_frames/
    "predict_ms": 500,     # 标志瞬间消失时预判切到另一状态，最多等它出现这么久（0=关闭）
    "shape": "胶囊",
    "feather": 4,          # 边缘羽化像素
    "fg_only": True,
    "game_title": "Endfield",
    "hotkey_vk": 0x77,     # F8
    "pad_toggle": True,    # XInput 手柄 View(Back) + 右摇杆按下 切换
    "countdown": 5,
}


def norm_pair(p):
    if isinstance(p, dict):
        return {"a": list(p["a"]), "b": list(p["b"]), "rot": int(p.get("rot", 0)) % 4}
    return {"a": list(p[0]), "b": list(p[1]), "rot": 0}   # 旧格式 [A, B]


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        with open(CONFIG, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    if cfg["shape"] not in SHAPES:
        cfg["shape"] = "胶囊"
    for st in cfg["states"]:
        st["pairs"] = [norm_pair(p) for p in st["pairs"]]
    return cfg


def save_config(cfg):
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ---------------- 识别：白色笔画 vs 紧邻一圈 ----------------
def _dilate(m, r):
    out = m.copy()
    h, w = m.shape
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            ys, yd = (slice(dy, h), slice(0, h - dy)) if dy >= 0 else (slice(0, h + dy), slice(-dy, h))
            xs, xd = (slice(dx, w), slice(0, w - dx)) if dx >= 0 else (slice(0, w + dx), slice(-dx, w))
            out[yd, xd] |= m[ys, xs]
    return out


def analyze_template(rgb):
    """rgb: HxWx3 uint8。返回 (stroke 掩码, ring 掩码, 模板对比度)。
    stroke = 亮且接近白色的像素（UI 的白字/白色线条，基本不透明）；ring = 笔画外 1~3 像素的一圈。"""
    a = rgb.astype(np.float32)
    gray = a.mean(axis=2)
    sat = a.max(axis=2) - a.min(axis=2)
    thr = max(170.0, float(np.percentile(gray, 85)))
    stroke = (gray >= thr) & (sat < 70)
    ring = _dilate(stroke, 3) & ~_dilate(stroke, 1)
    if stroke.sum() < 8 or ring.sum() < 8:
        return stroke, ring, 0.0
    return stroke, ring, float(gray[stroke].mean() - gray[ring].mean())


def match_score(bgr, stroke, ring, c0):
    """0~1+：当前画面里笔画比周围亮多少，相对模板的比例。"""
    gray = bgr[..., :3].mean(axis=2)
    s = gray[stroke]
    if s.mean() < 120:
        return 0.0
    return max(0.0, float(s.mean() - gray[ring].mean()) / c0)


HOLD = object()   # 当前状态掉分、又没有别的状态成立：先保持，连续几帧才隐藏


def decide(shown, scores, on_thr, off_thr, sw_thr):
    """返回应显示的状态名，None = 隐藏，HOLD = 暂时保持。
    - 隐藏中：最高分 ≥ on_thr 才显示（防菜单等误触发）
    - 显示中：别的状态 ≥ sw_thr 且分数超过当前状态 → 立刻切换（游戏淡入动画刚开始就切）
    - 当前状态 ≥ off_thr（回差）→ 保持"""
    if not scores:
        return None
    top = max(scores, key=scores.get)
    ts = scores[top]
    if shown is None:
        return top if ts >= on_thr else None
    cur = scores.get(shown, 0.0)
    if top != shown and ts >= sw_thr and ts > cur:
        return top
    if cur >= off_thr:
        return shown
    if ts >= on_thr:
        return top
    return HOLD


def save_frames(frames):
    d = os.path.join(BASE, "hud_debug_frames")
    os.makedirs(d, exist_ok=True)
    for f in os.listdir(d):
        if f.endswith(".png"):
            os.remove(os.path.join(d, f))
    for ms, ev, img in frames:
        Image.fromarray(img).save(os.path.join(d, "%08.1f%s.png" % (ms, ("_" + ev) if ev else "")))


class Switcher:
    """逐帧决定显示哪个状态。在 decide() 之上加两件事：
    - 回差保持：都不成立时连续 2 帧才隐藏
    - 预判：当前状态的标志一帧内从 ≥ off_thr 掉到 < 0.2（游戏里按下/松开 RB 时标志瞬间消失），
      而且只有一个别的状态 → 立刻切到那个状态，predict_s 秒内即使还没识别到也保持（等它的淡入动画）"""

    COLLAPSE = 0.2

    def __init__(self, names, on_thr, off_thr, sw_thr, predict_s):
        self.names = list(names)
        self.on, self.off, self.sw = on_thr, off_thr, sw_thr
        self.predict_s = predict_s
        self.shown = None
        self.low = 0
        self.prev = {}
        self.pred_until = 0.0
        self.event = None     # 本帧发生的事件（诊断用）："预判" / "切换" / "隐藏"

    def step(self, scores, now):
        self.event = None
        sh = self.shown
        if (sh is not None and self.predict_s > 0 and len(self.names) == 2
                and self.prev.get(sh, 0.0) >= self.off and scores.get(sh, 0.0) < self.COLLAPSE):
            self.shown = next(n for n in self.names if n != sh)
            self.pred_until = now + self.predict_s
            self.low = 0
            self.prev = scores
            self.event = "预判"
            return self.shown
        res = decide(sh, scores, self.on, self.off, self.sw)
        if res is HOLD:
            if now < self.pred_until:
                self.low = 0
            else:
                self.low += 1
                if self.low >= 2:
                    self.shown = None
                    self.event = "隐藏"
        else:
            self.low = 0
            if res != sh:
                self.shown = res
                self.pred_until = 0.0
                self.event = "切换" if res else "隐藏"
        self.prev = scores
        return self.shown


# ---------------- 形状遮罩 ----------------
def make_mask(w, h, shape, feather):
    """返回 HxW uint16，0~256（256=完全不透明），边缘 feather 像素渐变。"""
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    x = xs + 0.5 - w / 2
    y = ys + 0.5 - h / 2
    a, b = w / 2, h / 2
    if shape == "矩形":
        d = np.maximum(np.abs(x) - a, np.abs(y) - b)
    elif shape == "椭圆":
        r = np.sqrt((x / a) ** 2 + (y / b) ** 2)
        d = (r - 1) * min(a, b)
    else:  # 胶囊：沿长边的线段 + 半径 = 短边一半
        rad = min(a, b)
        if w >= h:
            px = np.clip(x, -(a - rad), a - rad)
            d = np.sqrt((x - px) ** 2 + y ** 2) - rad
        else:
            py = np.clip(y, -(b - rad), b - rad)
            d = np.sqrt(x ** 2 + (y - py) ** 2) - rad
    f = max(feather, 0.5)
    alpha = np.clip(-d / f, 0, 1)
    return (alpha * 256).astype(np.uint16)


def guess_rotation(a, b):
    """根据两块区域的形状和相对位置，猜 A 的内容放到 B 时要顺时针转几个 90°。"""
    def elong(r):
        return max(r[2], r[3]) / max(1, min(r[2], r[3])) >= 1.3
    ac = (a[0] + a[2] / 2, a[1] + a[3] / 2)
    bc = (b[0] + b[2] / 2, b[1] + b[3] / 2)
    if not elong(a) and not elong(b):
        return 0
    a_wide, b_wide = a[2] > a[3], b[2] > b[3]
    if a_wide != b_wide:   # 一横一竖：中心 = 竖的 x，横的 y
        c = (bc[0], ac[1]) if a_wide else (ac[0], bc[1])
    else:
        c = ((ac[0] + bc[0]) / 2, (ac[1] + bc[1]) / 2)
    va = (ac[0] - c[0], ac[1] - c[1])
    vb = (bc[0] - c[0], bc[1] - c[1])
    best, bestd = 0, 1e18
    v = va
    for k in range(4):
        d = (v[0] - vb[0]) ** 2 + (v[1] - vb[1]) ** 2
        if d < bestd:
            best, bestd = k, d
        v = (-v[1], v[0])   # 屏幕坐标下顺时针 90°
    return best


# ---------------- XInput（手柄组合键切换） ----------------
class XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [("wButtons", wt.WORD), ("bLeftTrigger", ctypes.c_ubyte), ("bRightTrigger", ctypes.c_ubyte),
                ("sThumbLX", ctypes.c_short), ("sThumbLY", ctypes.c_short),
                ("sThumbRX", ctypes.c_short), ("sThumbRY", ctypes.c_short)]


class XINPUT_STATE(ctypes.Structure):
    _fields_ = [("dwPacketNumber", wt.DWORD), ("Gamepad", XINPUT_GAMEPAD)]


_xinput = None
for _n in ("xinput1_4", "xinput9_1_0"):
    try:
        _xinput = getattr(ctypes.windll, _n)
        break
    except OSError:
        pass
XI_BACK, XI_RTHUMB = 0x0020, 0x0080


def pad_buttons():
    if not _xinput:
        return 0
    b = 0
    st = XINPUT_STATE()
    for i in range(4):
        if _xinput.XInputGetState(i, ctypes.byref(st)) == 0:
            b |= st.Gamepad.wButtons
    return b


def make_dib(ref_dc, w, h):
    dc = gdi32.CreateCompatibleDC(ref_dc)
    bi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    bits = ctypes.c_void_p()
    bmp = gdi32.CreateDIBSection(ref_dc, ctypes.byref(bi), 0, ctypes.byref(bits), None, 0)
    old = gdi32.SelectObject(dc, bmp)
    arr = np.ctypeslib.as_array((ctypes.c_ubyte * (w * h * 4)).from_address(bits.value)).reshape(h, w, 4)
    return dc, bmp, old, arr


def free_dib(dc, bmp, old):
    gdi32.SelectObject(dc, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(dc)


# ---------------- 覆盖块 ----------------
class Patch:
    """一个覆盖窗口：在 dst 处显示 src 的内容，顺时针旋转 rot×90°。"""

    def __init__(self, cls, hinst, screen, dst, src, rot, shape, feather):
        self.x, self.y, self.w, self.h = dst
        self.src = src
        self.rot = rot
        self.hwnd = user32.CreateWindowExW(
            WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_LAYERED | WS_EX_TRANSPARENT,
            cls, "HudSwap", WS_POPUP, self.x, self.y, self.w, self.h, None, None, hinst, None)
        user32.SetWindowDisplayAffinity(self.hwnd, WDA_EXCLUDEFROMCAPTURE)
        self.dc, self.bmp, self.old, self.arr = make_dib(screen, self.w, self.h)
        mask = make_mask(self.w, self.h, shape, feather)
        self.mask3 = mask[..., None]
        self.arr[..., 3] = np.minimum(mask, 255)
        sw, sh = src[2], src[3]
        rw, rh = (sh, sw) if rot % 2 else (sw, sh)     # 旋转后的尺寸
        self.resize = (rw, rh) != (self.w, self.h)
        if self.resize:
            self.ri = (np.arange(self.h) * rh // self.h)
            self.ci = (np.arange(self.w) * rw // self.w)
        self.pt = wt.POINT(self.x, self.y)
        self.size = wt.SIZE(self.w, self.h)
        self.zero = wt.POINT(0, 0)
        self.blend = BLENDFUNCTION(0, 0, 255, AC_SRC_ALPHA)

    def render(self, pix, bx, by):
        sx, sy, sw, sh = self.src
        img = pix[sy - by:sy - by + sh, sx - bx:sx - bx + sw, :3]
        if self.rot:
            img = np.rot90(img, -self.rot)
        if self.resize:
            img = img[self.ri][:, self.ci]
        self.arr[..., :3] = (img.astype(np.uint16) * self.mask3) >> 8   # 预乘 alpha
        user32.UpdateLayeredWindow(self.hwnd, None, ctypes.byref(self.pt), ctypes.byref(self.size), self.dc,
                                   ctypes.byref(self.zero), 0, ctypes.byref(self.blend), ULW_ALPHA)

    def show(self, vis):
        user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE if vis else SW_HIDE)
        if vis:
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def destroy(self):
        user32.DestroyWindow(self.hwnd)
        free_dib(self.dc, self.bmp, self.old)


# ---------------- 覆盖层引擎 ----------------
class Overlay(threading.Thread):
    def __init__(self, cfg, on_status, on_scores):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.on_status = on_status
        self.on_scores = on_scores
        self.stop_evt = threading.Event()
        self.enabled = True
        self.states = []

    def _wndproc(self, hwnd, msg, wp, lp):
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _register_class(self):
        hinst = kernel32.GetModuleHandleW(None)
        self._proc = WNDPROC(self._wndproc)

        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", WNDPROC),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE),
                        ("hIcon", wt.HICON), ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                        ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON)]

        self._cls = "HudSwapPatch_%d" % id(self)
        wc = WNDCLASSEXW(ctypes.sizeof(WNDCLASSEXW), 0, self._proc, 0, 0, hinst,
                         None, None, None, None, self._cls, None)
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            raise ctypes.WinError()
        return hinst

    def _game_foreground(self):
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        return bool(self.cfg["game_title"]) and self.cfg["game_title"] in buf.value

    @staticmethod
    def _show(state, vis):
        if state is not None:
            for p in state["patches"]:
                p.show(vis)

    def run(self):
        try:
            self._run()
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.on_status("出错：%s" % e)

    def _run(self):
        hinst = self._register_class()
        screen = user32.GetDC(None)
        bad = []
        for st in self.cfg["states"]:
            try:
                rgb = np.asarray(Image.open(os.path.join(BASE, st["template"])).convert("RGB"))
            except OSError:
                bad.append(st["name"])
                continue
            stroke, ring, c0 = analyze_template(rgb)
            if c0 < 25:
                bad.append(st["name"])
                continue
            patches = []
            for p in st["pairs"]:
                patches.append(Patch(self._cls, hinst, screen, p["a"], p["b"], (4 - p["rot"]) % 4,
                                     self.cfg["shape"], self.cfg["feather"]))
                patches.append(Patch(self._cls, hinst, screen, p["b"], p["a"], p["rot"],
                                     self.cfg["shape"], self.cfg["feather"]))
            self.states.append({"name": st["name"], "detect": st["detect"], "stroke": stroke, "ring": ring,
                                "c0": c0, "patches": patches})
        if not self.states:
            self.on_status("没有可用的状态（识别图案里没有白色字/线条？）请重新框选")
            user32.ReleaseDC(None, screen)
            return
        winmm.timeBeginPeriod(1)
        hot_ok = user32.RegisterHotKey(None, 1, MOD_NOREPEAT, self.cfg["hotkey_vk"])

        rects = []
        for s in self.states:
            rects.append(s["detect"])
            rects += [p.src for p in s["patches"]]
        bx = min(r[0] for r in rects)
        by = min(r[1] for r in rects)
        bw = max(r[0] + r[2] for r in rects) - bx
        bh = max(r[1] + r[3] for r in rects) - by
        mem, bmp, old, pix = make_dib(screen, bw, bh)

        msg = wt.MSG()
        shown = None
        low = 0
        last_fg = 0.0
        fg = True
        pad_prev = False
        frames, t0, cost = 0, time.perf_counter(), 0.0
        on_thr = self.cfg["match"] / 100.0
        off_thr = on_thr * 0.8
        sw_thr = self.cfg["switch"] / 100.0
        by_name = {s["name"]: s for s in self.states}
        sw = Switcher(by_name, on_thr, off_thr, sw_thr, self.cfg["predict_ms"] / 1000.0)
        log = None
        frames_saved = []
        rec_until = 0.0
        if self.cfg["debug_log"]:
            log = open(os.path.join(BASE, "hud_debug.csv"), "w", encoding="utf-8-sig")
            log.write("毫秒,显示中,%s,事件\n" % ",".join(s["name"] for s in self.states))
            log_t0 = time.perf_counter()
        warn =("（识别图案无效：%s）" % "、".join(bad)) if bad else ""
        while not self.stop_evt.is_set():
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                if msg.message == WM_HOTKEY:
                    self.enabled = not self.enabled
                user32.DispatchMessageW(ctypes.byref(msg))
            if self.cfg["pad_toggle"]:
                b = pad_buttons()
                combo = bool((b & XI_BACK) and (b & XI_RTHUMB))
                if combo and not pad_prev:
                    self.enabled = not self.enabled
                pad_prev = combo
            now = time.perf_counter()
            if self.cfg["fg_only"] and now - last_fg > 0.2:
                fg = self._game_foreground()
                last_fg = now
            if not (self.enabled and (fg or not self.cfg["fg_only"])):
                self._show(shown, False)
                shown = None
                sw.shown, sw.pred_until, sw.prev = None, 0.0, {}
                if now - t0 >= 0.5:
                    self.on_status("已关闭（F8 打开）" if not self.enabled else "游戏不在前台，已隐藏")
                    t0 = now
                time.sleep(0.03)
                continue

            t = time.perf_counter()
            gdi32.BitBlt(mem, 0, 0, bw, bh, screen, bx, by, SRCCOPY)
            gdi32.GdiFlush()
            scores = {}
            for s in self.states:
                x, y, w, h = s["detect"]
                scores[s["name"]] = match_score(pix[y - by:y - by + h, x - bx:x - bx + w],
                                                s["stroke"], s["ring"], s["c0"])
            if not sw.shown and shown is not None:   # 外部（前台切换等）隐藏过，同步
                shown = None
            nxt = by_name.get(sw.step(scores, t))
            if nxt is not shown:
                if nxt is not None:           # 先画好新状态再显示，避免闪旧内容
                    for p in nxt["patches"]:
                        p.render(pix, bx, by)
                    self._show(nxt, True)
                self._show(shown, False)
                shown = nxt
            if log:
                ms = (t - log_t0) * 1000
                log.write("%.1f,%s,%s,%s\n" % (ms, shown["name"] if shown else "",
                                                ",".join("%.3f" % scores[s["name"]] for s in self.states),
                                                sw.event or ""))
                if sw.event and len(frames_saved) < 240:
                    rec_until = t + 0.5       # 事件后 0.5 秒逐帧存图
                if t < rec_until and len(frames_saved) < 240:
                    frames_saved.append((ms, sw.event or "", pix[..., 2::-1].copy()))
            if shown is not None:
                for p in shown["patches"]:
                    p.render(pix, bx, by)
            cost += time.perf_counter() - t
            frames += 1
            if now - t0 >= 0.5:
                self.on_status("运行中 %d 帧/秒，每帧 %.1f ms，当前：%s%s%s" % (
                    frames / (now - t0), cost / frames * 1000, shown["name"] if shown else "无匹配（隐藏）",
                    "" if hot_ok else "（F8 被占用）", warn))
                self.on_scores(scores)
                frames, t0, cost = 0, now, 0.0
            # 屏幕 BitBlt 本身会等一次屏幕合成（≈1 帧），已与刷新率同步；异常快时才补等，避免空转
            if time.perf_counter() - t < 0.003 and dwmapi.DwmFlush() != 0:
                time.sleep(0.004)

        self._show(shown, False)
        if log:
            log.close()
            if frames_saved:     # 事件后的过渡帧另开线程写盘
                threading.Thread(target=save_frames, args=(frames_saved,), daemon=False).start()
        free_dib(mem, bmp, old)
        user32.ReleaseDC(None, screen)
        if hot_ok:
            user32.UnregisterHotKey(None, 1)
        for s in self.states:
            for p in s["patches"]:
                p.destroy()
        user32.UnregisterClassW(self._cls, hinst)
        winmm.timeEndPeriod(1)


# ---------------- 框选界面 ----------------
class Picker:
    """全屏显示截图。
    第 1 步：框一小块只在这个状态出现的白色字/标志（如 RB、字母 Y），越小越准。
    之后：成对框选要交换的区域——先拖出 A，再在 B 的中心单击（自动同尺寸，按旋转转向）或拖出 B。
    R 切换旋转（放 B 之前改将要放的，放完后改上一对），Shift 拖动为正方形，右键撤销，回车完成，Esc 取消。
    """
    COLORS = ["#ff4040", "#40c0ff", "#40ff60", "#ffd040", "#ff60ff", "#60ffff", "#ffffff", "#ff9040"]

    def __init__(self, root, image, done):
        self.done = done
        self.img = image
        self.vx, self.vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
        self.detect = None
        self.pairs = []        # [{a, b, rot}]
        self.pending = None    # 已框 A
        self.pend_rot = 0
        self.items = []
        self.pair_labels = []
        self.top = tk.Toplevel(root)
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        self.top.geometry("%dx%d%+d%+d" % (image.width, image.height, self.vx, self.vy))
        self.cv = tk.Canvas(self.top, highlightthickness=0, cursor="crosshair")
        self.cv.pack(fill="both", expand=True)
        self.tkimg = ImageTk.PhotoImage(image)
        self.cv.create_image(0, 0, image=self.tkimg, anchor="nw")
        self.tip_bg = self.cv.create_rectangle(0, 0, 0, 0, fill="black", outline="")
        self.tip = self.cv.create_text(image.width // 2, 40, fill="white", font=("Microsoft YaHei", 15, "bold"))
        self.cur = None
        self.ghost = None
        self.mouse = (0, 0)
        self.cv.bind("<ButtonPress-1>", self.press)
        self.cv.bind("<B1-Motion>", self.drag)
        self.cv.bind("<ButtonRelease-1>", self.release)
        self.cv.bind("<Button-3>", self.undo)
        self.cv.bind("<Motion>", self.hover)
        self.top.bind("<Escape>", lambda e: self.finish(cancel=True))
        self.top.bind("<Return>", lambda e: self.finish())
        self.top.bind("<KeyPress-r>", self.rotate)
        self.top.bind("<KeyPress-R>", self.rotate)
        self.top.focus_force()
        self.update_tip()

    def color(self, i=None):
        return self.COLORS[(len(self.pairs) if i is None else i) % len(self.COLORS)]

    def update_tip(self, extra=""):
        if self.detect is None:
            t = "第 1 步：框一小块只在这个状态出现的白色字/标志（如 RB、字母 Y），框得越贴越准"
        elif self.pending is None:
            t = "第 %d 对：拖动框出区域 A　｜　R 改上一对旋转　回车完成　右键撤销　Esc 取消" % (len(self.pairs) + 1)
        else:
            t = "第 %d 对：在 B 的中心单击（同尺寸）或拖出 B　｜　A→B %s（R 切换）" % (
                len(self.pairs) + 1, ROT_NAMES[self.pend_rot])
        self.cv.itemconfigure(self.tip, text=t + extra)
        x1, y1, x2, y2 = self.cv.bbox(self.tip)
        self.cv.coords(self.tip_bg, x1 - 12, y1 - 6, x2 + 12, y2 + 6)

    def press(self, e):
        self.start = (e.x, e.y)
        col = "#ffffff" if self.detect is None else self.color()
        self.cur = self.cv.create_rectangle(e.x, e.y, e.x, e.y, outline=col, width=2,
                                            dash=(4, 2) if self.detect is None else None)

    def _box(self, e):
        x0, y0 = self.start
        dx, dy = e.x - x0, e.y - y0
        if e.state & 0x1:
            s = max(abs(dx), abs(dy))
            dx = s if dx >= 0 else -s
            dy = s if dy >= 0 else -s
        x1, y1 = x0 + dx, y0 + dy
        return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)

    def drag(self, e):
        if self.cur:
            self.cv.coords(self.cur, *self._box(e))

    def _b_size(self):
        _, _, w, h = self.pending
        return (h, w) if self.pend_rot % 2 else (w, h)

    def hover(self, e=None):
        if e is not None:
            self.mouse = (e.x, e.y)
        if self.ghost:
            self.cv.delete(self.ghost)
            self.ghost = None
        if self.pending is not None and self.cur is None:
            w, h = self._b_size()
            mx, my = self.mouse
            self.ghost = self.cv.create_rectangle(mx - w // 2, my - h // 2, mx - w // 2 + w, my - h // 2 + h,
                                                  outline=self.color(), dash=(2, 2))

    def rotate(self, e=None):
        if self.pending is not None:
            self.pend_rot = (self.pend_rot + 1) % 4
            self.hover()
            self.update_tip()
        elif self.pairs:
            p = self.pairs[-1]
            p["rot"] = (p["rot"] + 1) % 4
            self.cv.itemconfigure(self.pair_labels[-1], text="%d B %s" % (len(self.pairs), ROT_NAMES[p["rot"]]))
            self.update_tip("　（第 %d 对：%s）" % (len(self.pairs), ROT_NAMES[p["rot"]]))

    def release(self, e):
        if not self.cur:
            return
        x0, y0, x1, y1 = self._box(e)
        small = x1 - x0 < 4 or y1 - y0 < 4
        if small and self.pending is not None:
            w, h = self._b_size()
            x0, y0 = e.x - w // 2, e.y - h // 2
            x1, y1 = x0 + w, y0 + h
        elif small:
            self.cv.delete(self.cur)
            self.cur = None
            return
        self.cv.coords(self.cur, x0, y0, x1, y1)
        rect = [x0 + self.vx, y0 + self.vy, x1 - x0, y1 - y0]
        if self.detect is None:
            crop = np.asarray(self.img.crop((x0, y0, x1, y1)).convert("RGB"))
            _, _, c0 = analyze_template(crop)
            if c0 < 25:
                self.cv.delete(self.cur)
                self.cur = None
                self.update_tip("　【这块里找不到明显的白色字/线条，换一块】")
                return
            lab = self.cv.create_text(x0, y0 - 2, text="识别", anchor="sw", fill="white",
                                      font=("Microsoft YaHei", 10, "bold"))
            self.detect = rect
            self.items.append([self.cur, lab])
        elif self.pending is None:
            lab = self.cv.create_text(x0 + 2, y0, text="%d A" % (len(self.pairs) + 1), anchor="nw",
                                      fill=self.color(), font=("Microsoft YaHei", 9, "bold"))
            self.pending = rect
            elong = max(rect[2], rect[3]) / max(1, min(rect[2], rect[3])) >= 1.3
            self.pend_rot = 1 if elong else 0
            self.items.append([self.cur, lab])
        else:
            a = self.pending
            rot = self.pend_rot if small else guess_rotation(a, rect)
            if small and self.pend_rot % 2 == 1:
                g = guess_rotation(a, rect)       # 方向（顺/逆）按实际位置推算
                if g % 2 == 1:
                    rot = g
            col = self.color()
            lab = self.cv.create_text(x0 + 2, y0, text="%d B %s" % (len(self.pairs) + 1, ROT_NAMES[rot]),
                                      anchor="nw", fill=col, font=("Microsoft YaHei", 9, "bold"))
            line = self.cv.create_line(a[0] - self.vx + a[2] // 2, a[1] - self.vy + a[3] // 2,
                                       (x0 + x1) // 2, (y0 + y1) // 2, fill=col, arrow="both", dash=(3, 3))
            self.pairs.append({"a": a, "b": rect, "rot": rot})
            self.pair_labels.append(lab)
            self.pending = None
            self.items.append([self.cur, lab, line])
        self.cur = None
        self.hover(e)
        self.update_tip()

    def undo(self, e=None):
        if not self.items:
            return
        for it in self.items.pop():
            self.cv.delete(it)
        if self.pending is not None:
            self.pending = None
        elif self.pairs:
            p = self.pairs.pop()
            self.pair_labels.pop()
            self.pending = p["a"]
            self.pend_rot = p["rot"]
        else:
            self.detect = None
        self.hover(e)
        self.update_tip()

    def finish(self, cancel=False):
        if not cancel and (self.detect is None or not self.pairs):
            self.update_tip("　【至少需要 1 个识别图案 + 1 对交换区域】")
            return
        self.top.destroy()
        if cancel:
            self.done(None)
        else:
            x, y, w, h = self.detect
            crop = self.img.crop((x - self.vx, y - self.vy, x - self.vx + w, y - self.vy + h))
            self.done({"detect": self.detect, "pairs": self.pairs, "crop": crop})


# ---------------- 主界面 ----------------
class App:
    def __init__(self):
        self.cfg = load_config()
        self.overlay = None
        self.scores = {}
        self.root = tk.Tk()
        self.root.title("HUD 换位覆盖层")
        self.root.resizable(False, False)
        icon = os.path.join(BASE, "icon.ico")
        if os.path.exists(icon):
            self.root.iconbitmap(default=icon)
        f = ttk.Frame(self.root, padding=12)
        f.pack(fill="both", expand=True)

        ttk.Label(f, text="状态（每个状态：识别图案 + 若干对交换区域）",
                  font=("Microsoft YaHei", 10, "bold")).pack(anchor="w")
        self.tree = ttk.Treeview(f, columns=("pairs", "score"), height=5)
        self.tree.heading("#0", text="名称")
        self.tree.heading("pairs", text="交换对数")
        self.tree.heading("score", text="当前匹配度")
        self.tree.column("#0", width=170)
        self.tree.column("pairs", width=70, anchor="center")
        self.tree.column("score", width=90, anchor="center")
        self.tree.pack(fill="x", pady=4)

        row = ttk.Frame(f)
        row.pack(anchor="w")
        ttk.Button(row, text="新建状态（倒计时截图）", command=self.new_state).pack(side="left")
        ttk.Button(row, text="重新框选", command=self.redo_state).pack(side="left", padx=4)
        ttk.Button(row, text="删除", command=self.del_state).pack(side="left")

        row = ttk.Frame(f)
        row.pack(anchor="w", pady=(8, 0))
        ttk.Label(row, text="匹配度 ≥").pack(side="left")
        self.match = tk.IntVar(value=self.cfg["match"])
        ttk.Spinbox(row, from_=10, to=100, increment=5, textvariable=self.match, width=4,
                    command=self.changed).pack(side="left", padx=2)
        ttk.Label(row, text="% 时显示，状态间切换 ≥").pack(side="left")
        self.switch = tk.IntVar(value=self.cfg["switch"])
        ttk.Spinbox(row, from_=10, to=100, increment=5, textvariable=self.switch, width=4,
                    command=self.changed).pack(side="left", padx=2)
        ttk.Label(row, text="%").pack(side="left")
        ttk.Label(row, text="   形状").pack(side="left")
        self.shape = tk.StringVar(value=self.cfg["shape"])
        sh = ttk.Combobox(row, textvariable=self.shape, values=SHAPES, width=5, state="readonly")
        sh.pack(side="left", padx=4)
        sh.bind("<<ComboboxSelected>>", lambda e: self.changed())
        ttk.Label(row, text=" 羽化").pack(side="left")
        self.feather = tk.IntVar(value=self.cfg["feather"])
        ttk.Spinbox(row, from_=0, to=20, textvariable=self.feather, width=3, command=self.changed).pack(side="left", padx=2)
        ttk.Label(row, text="像素   截图倒计时").pack(side="left")
        self.cd = tk.IntVar(value=self.cfg["countdown"])
        ttk.Spinbox(row, from_=1, to=15, textvariable=self.cd, width=3, command=self.changed).pack(side="left", padx=2)

        self.fg_only = tk.BooleanVar(value=self.cfg["fg_only"])
        ttk.Checkbutton(f, text="只在游戏窗口（标题含 %s）在前台时显示" % self.cfg["game_title"],
                        variable=self.fg_only, command=self.changed).pack(anchor="w", pady=(8, 0))
        self.pad = tk.BooleanVar(value=self.cfg["pad_toggle"])
        ttk.Checkbutton(f, text="手柄 View(Back) + 右摇杆按下 开关覆盖层（仅 XInput 手柄）",
                        variable=self.pad, command=self.changed).pack(anchor="w")
        row = ttk.Frame(f)
        row.pack(anchor="w")
        ttk.Label(row, text="预判：标志瞬间消失时立刻切到另一状态，最多等它").pack(side="left")
        self.pred = tk.IntVar(value=self.cfg["predict_ms"])
        ttk.Spinbox(row, from_=0, to=2000, increment=50, textvariable=self.pred, width=5,
                    command=self.changed).pack(side="left", padx=2)
        ttk.Label(row, text="毫秒出现（0 = 关闭，仅两个状态时生效）").pack(side="left")
        self.dlog = tk.BooleanVar(value=self.cfg["debug_log"])
        ttk.Checkbutton(f, text="记录诊断：逐帧匹配度写入 hud_debug.csv（排查延迟用，平时关掉）",
                        variable=self.dlog, command=self.changed).pack(anchor="w")
        ttk.Label(f, text="键盘 F8 开关覆盖层", foreground="#666").pack(anchor="w")

        row = ttk.Frame(f)
        row.pack(fill="x", pady=(10, 0))
        self.btn = ttk.Button(row, text="开始", command=self.toggle)
        self.btn.pack(side="left")
        self.status = ttk.Label(row, text="未运行", width=62)
        self.status.pack(side="left", padx=8)

        self.refresh_tree()
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        if self.cfg["states"]:
            self.root.after(300, self.start)

    def refresh_tree(self):
        sel = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for i, s in enumerate(self.cfg["states"]):
            sc = self.scores.get(s["name"])
            self.tree.insert("", "end", iid=str(i), text=s["name"],
                             values=(len(s["pairs"]), "-" if sc is None else "%d%%" % round(sc * 100)))
        for s in sel:
            if self.tree.exists(s):
                self.tree.selection_set(s)

    def selected(self):
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def new_state(self):
        name = simpledialog.askstring("新建状态", "状态名称（如：普通 / 激活）", parent=self.root)
        if name:
            self.capture(lambda res: self.add_state(name, res))

    def redo_state(self):
        i = self.selected()
        if i is None:
            messagebox.showinfo("提示", "先在列表里选一个状态")
            return
        self.capture(lambda res: self.add_state(self.cfg["states"][i]["name"], res, index=i))

    def del_state(self):
        i = self.selected()
        if i is None:
            return
        was = self.overlay is not None
        self.stop()
        st = self.cfg["states"].pop(i)
        try:
            os.remove(os.path.join(BASE, st["template"]))
        except OSError:
            pass
        save_config(self.cfg)
        self.refresh_tree()
        if was:
            self.start()

    def add_state(self, name, res, index=None):
        fn = "tmpl_%d.png" % int(time.time() * 1000)
        res["crop"].save(os.path.join(BASE, fn))
        st = {"name": name, "detect": res["detect"], "template": fn, "pairs": res["pairs"]}
        if index is None:
            self.cfg["states"].append(st)
        else:
            try:
                os.remove(os.path.join(BASE, self.cfg["states"][index]["template"]))
            except OSError:
                pass
            self.cfg["states"][index] = st
        save_config(self.cfg)
        self.refresh_tree()

    def capture(self, on_done):
        was_running = self.overlay is not None
        self.stop()
        self.root.iconify()
        self._countdown(self.cfg["countdown"], was_running, on_done)

    def _countdown(self, n, was_running, on_done):
        if n > 0:
            self.root.title("%d 秒后截图：切到游戏，摆好要截的状态（如按住 RB）" % n)
            self.root.after(1000, lambda: self._countdown(n - 1, was_running, on_done))
            return
        self.root.title("HUD 换位覆盖层")
        img = ImageGrab.grab(all_screens=True)

        def done(res):
            self.root.deiconify()
            if res:
                on_done(res)
            if res or was_running:
                self.start()

        Picker(self.root, img, done)

    def changed(self):
        try:
            self.cfg["match"] = int(self.match.get())
            self.cfg["switch"] = int(self.switch.get())
            self.cfg["predict_ms"] = int(self.pred.get())
            self.cfg["countdown"] = int(self.cd.get())
            self.cfg["feather"] = int(self.feather.get())
        except (tk.TclError, ValueError):
            return
        self.cfg["shape"] = self.shape.get()
        self.cfg["fg_only"] = self.fg_only.get()
        self.cfg["pad_toggle"] = self.pad.get()
        self.cfg["debug_log"] = self.dlog.get()
        save_config(self.cfg)
        if self.overlay:
            self.stop()
            self.start()

    def set_status(self, text):
        self.root.after(0, lambda: self.status.configure(text=text))

    def set_scores(self, scores):
        def upd():
            self.scores = scores
            self.refresh_tree()
        self.root.after(0, upd)

    def start(self):
        if self.overlay:
            return
        self.overlay = Overlay(self.cfg, self.set_status, self.set_scores)
        self.overlay.start()
        self.btn.configure(text="停止")

    def stop(self):
        if self.overlay:
            self.overlay.stop_evt.set()
            self.overlay.join(2)
            self.overlay = None
        self.btn.configure(text="开始")
        self.status.configure(text="未运行")

    def toggle(self):
        self.stop() if self.overlay else self.start()

    def quit(self):
        self.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
