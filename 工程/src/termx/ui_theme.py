# -*- coding: utf-8 -*-
"""SecureCRT 经典风格界面主题：银灰工具栏、彩色图标、停靠式会话管理器。

图标由纯 Python 逐像素绘制后编码为 PNG，再交给 Tk 的 PhotoImage 加载，
不依赖任何外部图片资源，打包时无需额外 datas。
"""
import base64
import struct
import tkinter as tk
import zlib

UI_FONT = ("Microsoft YaHei UI", 9)
SMALL_FONT = ("Microsoft YaHei UI", 8)

# ---------------------------------------------------------------- 配色
CRT = {
    "name": "SecureCRT 经典",
    "bg": "#F0F0F0",
    "fg": "#202020",
    "muted": "#6A6A6A",
    "border": "#C6CCD6",
    "panel": "#F4F6F9",
    "panel_hdr": "#D8E3F3",
    "hdr_fg": "#1C3F73",
    "toolbar": "#F5F6F8",
    "hover": "#E2ECFA",
    "pressed": "#C8DCF5",
    "tab_bg": "#E2E6ED",
    "tab_sel": "#FFFFFF",
    "tab_fg": "#3A3A3A",
    "tab_sel_fg": "#123F7A",
    "status": "#EDEDED",
    "status_seg": "#F8F8F8",
    "tree_bg": "#FFFFFF",
    "tree_fg": "#202020",
    "sel": "#2D6CCD",
    "sel_fg": "#FFFFFF",
    "accent": "#2D6CCD",
    "entry_bg": "#FFFFFF",
    "field": "#FFFFFF",
    "tab_border": "#C6CCD6",
    "scheme": "SecureCRT 经典",
    "cursor_style": "block",
    "ref": "参考 SecureCRT：银灰工具栏 + 纯黑终端",
}

LIGHT = dict(CRT, name="浅色", panel_hdr="#ECECEC", hdr_fg="#333333",
             accent="#3A7BD5", sel="#3A7BD5", tab_sel_fg="#2A5FA0",
             scheme="白底", ref="简洁浅色外壳，适合白天办公")

DARK = {
    "name": "深色",
    "bg": "#252526",
    "fg": "#E6E6E6",
    "muted": "#9A9A9A",
    "border": "#3E3E42",
    "panel": "#2D2D30",
    "panel_hdr": "#333740",
    "hdr_fg": "#DCE6F5",
    "toolbar": "#2D2D30",
    "hover": "#3A4A63",
    "pressed": "#094771",
    "tab_bg": "#2D2D30",
    "tab_sel": "#1E1E1E",
    "tab_fg": "#C8C8C8",
    "tab_sel_fg": "#FFFFFF",
    "status": "#2D2D30",
    "status_seg": "#333333",
    "tree_bg": "#252526",
    "tree_fg": "#E6E6E6",
    "sel": "#094771",
    "sel_fg": "#FFFFFF",
    "accent": "#4A90D9",
    "entry_bg": "#1E1E1E",
    "field": "#1E1E1E",
    "tab_border": "#3E3E42",
    "scheme": "One Half Dark",
    "cursor_style": "block",
    "ref": "VS Code 风格深色外壳，长时使用更舒适",
}

# ---- 新增三套：分别参考 Windows Terminal / MobaXterm / iTerm2 ----------------
WT = {
    "name": "Windows Terminal · 午夜",
    "bg": "#1B1B1F", "fg": "#E8EAED", "muted": "#9AA0A6",
    "border": "#34363C", "panel": "#202124",
    "panel_hdr": "#25262A", "hdr_fg": "#60CDFF",
    "toolbar": "#27282D", "hover": "#343946", "pressed": "#0B4A72",
    "tab_bg": "#202124", "tab_sel": "#0C0C0C",
    "tab_fg": "#A9ADB4", "tab_sel_fg": "#60CDFF",
    "tab_border": "#0078D4",
    "status": "#18191C", "status_seg": "#242529",
    "tree_bg": "#1B1B1F", "tree_fg": "#DDE1E6",
    "sel": "#0078D4", "sel_fg": "#FFFFFF", "accent": "#60CDFF",
    "entry_bg": "#111214", "field": "#111214",
    "scheme": "Windows Terminal 午夜",
    "cursor_style": "bar",
    "ref": "参考 Windows Terminal：深灰外壳 + Campbell 配色",
}

MOBA = {
    "name": "MobaXterm · 晴空",
    "bg": "#F2F5F9", "fg": "#243447", "muted": "#6B7A8D",
    "border": "#C9D6E4", "panel": "#FFFFFF",
    "panel_hdr": "#2F7DBF", "hdr_fg": "#FFFFFF",
    "toolbar": "#F7FAFD", "hover": "#E3F0FC", "pressed": "#C7E2F7",
    "tab_bg": "#E6EDF5", "tab_sel": "#FFFFFF",
    "tab_fg": "#4A5B6E", "tab_sel_fg": "#1A6BB5",
    "tab_border": "#C9D6E4",
    "status": "#EAF0F6", "status_seg": "#FFFFFF",
    "tree_bg": "#FFFFFF", "tree_fg": "#243447",
    "sel": "#2F7DBF", "sel_fg": "#FFFFFF", "accent": "#1E88E5",
    "entry_bg": "#FFFFFF", "field": "#FFFFFF",
    "scheme": "MobaXterm 晴空",
    "cursor_style": "block",
    "ref": "参考 MobaXterm：蓝色标题栏 + 白底终端",
}

SOLAR = {
    "name": "iTerm2 · Solarized",
    "bg": "#002B36", "fg": "#93A1A1", "muted": "#657B83",
    "border": "#0A4B58", "panel": "#073642",
    "panel_hdr": "#073642", "hdr_fg": "#B58900",
    "toolbar": "#073642", "hover": "#0A4B58", "pressed": "#1A6577",
    "tab_bg": "#073642", "tab_sel": "#002B36",
    "tab_fg": "#839496", "tab_sel_fg": "#FDF6E3",
    "tab_border": "#2AA198",
    "status": "#052B33", "status_seg": "#0A3A45",
    "tree_bg": "#002B36", "tree_fg": "#93A1A1",
    "sel": "#2AA198", "sel_fg": "#002B36", "accent": "#B58900",
    "entry_bg": "#00212B", "field": "#00212B",
    "scheme": "Solarized Dark",
    "cursor_style": "block",
    "ref": "参考 iTerm2 / Solarized：深青底 + 琥珀强调",
}

THEMES = {"crt": CRT, "light": LIGHT, "dark": DARK,
          "wt": WT, "moba": MOBA, "solar": SOLAR}
THEME_LABELS = [
    ("crt", "SecureCRT 经典"), ("light", "浅色"), ("dark", "深色"),
    ("wt", "Windows Terminal · 午夜"), ("moba", "MobaXterm · 晴空"),
    ("solar", "iTerm2 · Solarized"),
]
DISPLAY_NAMES = [label for _, label in THEME_LABELS]
PLACEHOLDER = "搜索会话..."


def normalize(value):
    """把历史配置值 / 中文显示名统一成内部主题键。"""
    v = (value or "").strip().lower()
    if v in ("securecrt 经典", "crt", "securecrt", "classic", ""):
        return "crt"
    if v in ("浅色", "light"):
        return "light"
    if v in ("深色", "dark"):
        return "dark"
    if v in ("wt", "windows terminal", "windows terminal · 午夜", "午夜"):
        return "wt"
    if v in ("moba", "mobaxterm", "mobaxterm · 晴空", "晴空"):
        return "moba"
    if v in ("solar", "solarized", "iterm2", "iterm2 · solarized"):
        return "solar"
    for key, label in THEME_LABELS:
        if v == label.lower():
            return key
    return "crt"


def display_name(key):
    return dict(THEME_LABELS).get(key, "SecureCRT 经典")


def scheme_for(key):
    """主题自带终端配色名。"""
    return THEMES.get(key, CRT).get("scheme", "SecureCRT 经典")


def apply(root, style, theme_key):
    """把主题应用到 ttk 样式与根窗口，返回配色字典。"""
    c = THEMES.get(theme_key, CRT)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    try:
        root.configure(bg=c["bg"])
        style.configure(".", background=c["bg"], foreground=c["fg"],
                        fieldbackground=c["field"], font=UI_FONT,
                        bordercolor=c["border"])
        style.configure("TFrame", background=c["bg"])
        style.configure("TLabel", background=c["bg"], foreground=c["fg"])
        style.configure("TLabelframe", background=c["bg"], foreground=c["fg"],
                        bordercolor=c["border"])
        style.configure("TLabelframe.Label", background=c["bg"], foreground=c["fg"])
        style.configure("TButton", background=c["toolbar"], foreground=c["fg"],
                        bordercolor=c["border"], lightcolor=c["toolbar"],
                        darkcolor=c["toolbar"], padding=(7, 3))
        style.map("TButton",
                  background=[("pressed", c["pressed"]), ("active", c["hover"])],
                  foreground=[("disabled", c["muted"])])
        style.configure("TCheckbutton", background=c["bg"], foreground=c["fg"])
        style.configure("TRadiobutton", background=c["bg"], foreground=c["fg"])
        style.configure("TEntry", fieldbackground=c["entry_bg"],
                        foreground=c["fg"], insertcolor=c["fg"],
                        bordercolor=c["border"])
        style.configure("TCombobox", fieldbackground=c["entry_bg"],
                        foreground=c["fg"], bordercolor=c["border"],
                        arrowcolor=c["fg"])
        style.configure("TSpinbox", fieldbackground=c["entry_bg"],
                        foreground=c["fg"], bordercolor=c["border"],
                        arrowcolor=c["fg"])
        style.configure("Treeview", background=c["tree_bg"],
                        foreground=c["tree_fg"], fieldbackground=c["tree_bg"],
                        borderwidth=0, rowheight=22, font=UI_FONT)
        style.map("Treeview",
                  background=[("selected", c["sel"])],
                  foreground=[("selected", c["sel_fg"])])
        style.configure("Treeview.Heading", background=c["panel_hdr"],
                        foreground=c["hdr_fg"], relief="flat")
        style.configure("TNotebook", background=c["panel"], borderwidth=0,
                        tabmargins=(2, 3, 2, 0))
        style.configure("TNotebook.Tab", background=c["tab_bg"],
                        foreground=c["tab_fg"], padding=(12, 5),
                        borderwidth=0, font=UI_FONT,
                        bordercolor=c.get("tab_border", c["border"]))
        style.map("TNotebook.Tab",
                  background=[("selected", c["tab_sel"]), ("active", c["hover"])],
                  foreground=[("selected", c["tab_sel_fg"])])
        style.configure("TScrollbar", background=c["toolbar"],
                        troughcolor=c["bg"], bordercolor=c["border"],
                        arrowcolor=c["fg"])
        style.configure("TProgressbar", background=c["accent"],
                        troughcolor=c["bg"], bordercolor=c["border"])
        style.configure("TSeparator", background=c["border"])
        style.configure("TPanedwindow", background=c["bg"])
        style.configure("Sash", sashthickness=4, gripcount=0)
    except Exception:
        pass
    return c


# ---------------------------------------------------------------- 图标绘制
W = H = 16
CLEAR = (0, 0, 0, 0)
WHITE = (255, 255, 255, 255)
BLUE = (46, 110, 195, 255)
BLUE_D = (24, 68, 140, 255)
GREEN = (32, 152, 68, 255)
GREEN_D = (18, 105, 45, 255)
RED = (206, 56, 46, 255)
YELLOW = (242, 192, 48, 255)
YELLOW_D = (176, 126, 20, 255)
GRAY = (122, 132, 148, 255)
GRAY_D = (72, 82, 98, 255)
PURPLE = (142, 92, 190, 255)
CYAN = (36, 160, 196, 255)
INK = (58, 66, 80, 255)


def _canvas():
    return [[CLEAR for _ in range(W)] for _ in range(H)]


def _box(c, x0, y0, x1, y1, col):
    for y in range(max(0, int(y0)), min(H, int(y1) + 1)):
        for x in range(max(0, int(x0)), min(W, int(x1) + 1)):
            c[y][x] = col


def _frame(c, x0, y0, x1, y1, col, t=1):
    _box(c, x0, y0, x1, y0 + t - 1, col)
    _box(c, x0, y1 - t + 1, x1, y1, col)
    _box(c, x0, y0, x0 + t - 1, y1, col)
    _box(c, x1 - t + 1, y0, x1, y1, col)


def _line(c, x0, y0, x1, y1, col, t=1):
    x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
    err = dx + dy
    pad = (t - 1) // 2
    while True:
        _box(c, x0 - pad, y0 - pad, x0 + pad, y0 + pad, col)
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


def _disc(c, cx, cy, r, col):
    for y in range(H):
        for x in range(W):
            if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                c[y][x] = col


def _ring(c, cx, cy, r, col, t=1):
    for y in range(H):
        for x in range(W):
            d2 = (x - cx) ** 2 + (y - cy) ** 2
            if (r - t) ** 2 < d2 <= r * r:
                c[y][x] = col


def _arrow(c, x, y, col, direction="right", size=4):
    if direction == "right":
        for i in range(size):
            _line(c, x + i, y - (size - 1 - i), x + i, y + (size - 1 - i), col)
    elif direction == "left":
        for i in range(size):
            _line(c, x - i, y - (size - 1 - i), x - i, y + (size - 1 - i), col)
    elif direction == "up":
        for i in range(size):
            _line(c, x - (size - 1 - i), y - i, x + (size - 1 - i), y - i, col)
    else:
        for i in range(size):
            _line(c, x - (size - 1 - i), y + i, x + (size - 1 - i), y + i, col)


def _glyphs():
    g = {}

    c = _canvas()                                   # 新建会话：文档 + 绿色加号
    _box(c, 2, 1, 11, 14, WHITE)
    _frame(c, 2, 1, 11, 14, BLUE_D)
    _line(c, 9, 1, 11, 3, BLUE_D)
    _box(c, 4, 5, 9, 5, BLUE)
    _box(c, 4, 7, 9, 7, GRAY)
    _disc(c, 12, 12, 4, GREEN)
    _box(c, 12, 10, 12, 14, WHITE)
    _box(c, 10, 12, 14, 12, WHITE)
    g["new"] = c

    c = _canvas()                                   # 快速连接：闪电
    _box(c, 8, 1, 12, 6, YELLOW)
    _box(c, 6, 6, 11, 8, YELLOW)
    _box(c, 4, 8, 9, 14, YELLOW)
    _line(c, 9, 2, 6, 8, YELLOW_D)
    _line(c, 6, 8, 9, 8, YELLOW_D)
    _line(c, 9, 8, 6, 14, YELLOW_D)
    _line(c, 12, 2, 13, 5, GRAY)
    _line(c, 3, 12, 2, 14, GRAY)
    g["quick"] = c

    c = _canvas()                                   # 连接：绿色三角
    _disc(c, 8, 8, 7, GREEN_D)
    _disc(c, 8, 8, 6, GREEN)
    _arrow(c, 6, 8, WHITE, "right", 4)
    g["connect"] = c

    c = _canvas()                                   # 断开：红圆 + 白叉
    _disc(c, 8, 8, 7, (168, 38, 30, 255))
    _disc(c, 8, 8, 6, RED)
    _line(c, 5, 5, 11, 11, WHITE)
    _line(c, 11, 5, 5, 11, WHITE)
    g["disconnect"] = c

    c = _canvas()                                   # 复制：两张纸
    _box(c, 1, 4, 10, 14, WHITE)
    _frame(c, 1, 4, 10, 14, BLUE_D)
    _box(c, 5, 1, 14, 11, WHITE)
    _frame(c, 5, 1, 14, 11, BLUE)
    _box(c, 7, 4, 12, 4, BLUE)
    _box(c, 7, 6, 12, 6, GRAY)
    _box(c, 7, 8, 11, 8, GRAY)
    g["copy"] = c

    c = _canvas()                                   # 粘贴：剪贴板
    _box(c, 2, 3, 13, 14, WHITE)
    _frame(c, 2, 3, 13, 14, GRAY_D)
    _box(c, 5, 1, 10, 4, BLUE)
    _box(c, 4, 6, 11, 6, BLUE)
    _box(c, 4, 8, 11, 8, GRAY)
    _box(c, 4, 10, 9, 10, GRAY)
    g["paste"] = c

    c = _canvas()                                   # 查找：放大镜
    _ring(c, 6, 6, 5, BLUE_D, 2)
    _line(c, 10, 10, 14, 14, GRAY_D, 2)
    _disc(c, 6, 6, 2, (225, 239, 255, 255))
    g["find"] = c

    c = _canvas()                                   # 清屏：窗口 + 刷子
    _box(c, 1, 2, 14, 12, WHITE)
    _frame(c, 1, 2, 14, 12, BLUE_D)
    _box(c, 1, 2, 14, 4, BLUE)
    _box(c, 3, 6, 8, 6, GRAY)
    _box(c, 3, 8, 7, 8, GRAY)
    _box(c, 8, 8, 13, 14, CYAN)
    _box(c, 9, 12, 12, 15, GRAY_D)
    g["clear"] = c

    c = _canvas()                                   # 批量下发：三支箭
    for yy, col in ((3, BLUE), (8, GREEN), (13, PURPLE)):
        _box(c, 1, yy, 9, yy + 1, col)
        _arrow(c, 10, yy, col, "right", 3)
    g["batch"] = c

    c = _canvas()                                   # 脚本：卷轴
    _box(c, 3, 2, 12, 14, WHITE)
    _frame(c, 3, 2, 12, 14, PURPLE)
    _box(c, 3, 2, 12, 3, PURPLE)
    for yy in (6, 8, 10, 12):
        _box(c, 5, yy, 10, yy, GRAY)
    g["script"] = c

    c = _canvas()                                   # 文件传输：上下箭
    _box(c, 3, 4, 6, 12, GREEN)
    _arrow(c, 4, 3, GREEN, "up", 4)
    _box(c, 9, 4, 12, 12, BLUE)
    _arrow(c, 11, 13, BLUE, "down", 4)
    g["transfer"] = c

    c = _canvas()                                   # 选项：齿轮
    _disc(c, 8, 8, 6, GRAY)
    _disc(c, 8, 8, 3, WHITE)
    for xx, yy in ((8, 1), (8, 15), (1, 8), (15, 8),
                   (3, 3), (13, 3), (3, 13), (13, 13)):
        _box(c, xx - 1, yy - 1, xx + 1, yy + 1, GRAY_D)
    g["options"] = c

    c = _canvas()                                   # 文件夹
    _box(c, 1, 4, 6, 5, YELLOW_D)
    _box(c, 1, 5, 14, 13, YELLOW)
    _frame(c, 1, 5, 14, 13, YELLOW_D)
    _box(c, 1, 3, 7, 4, YELLOW_D)
    g["folder"] = c

    c = _canvas()                                   # 会话：CRT 显示器
    _box(c, 1, 2, 14, 11, GRAY_D)
    _box(c, 2, 3, 13, 9, (35, 45, 60, 255))
    _box(c, 3, 4, 12, 8, (70, 160, 205, 255))
    _box(c, 4, 5, 8, 5, WHITE)
    _box(c, 4, 7, 7, 7, WHITE)
    _box(c, 6, 11, 9, 13, GRAY)
    _box(c, 3, 14, 12, 14, GRAY_D)
    g["session"] = c

    c = _canvas()                                   # 收藏：星
    _line(c, 8, 1, 8, 14, YELLOW, 2)
    _line(c, 1, 8, 15, 8, YELLOW, 2)
    _line(c, 3, 3, 13, 13, YELLOW, 2)
    _line(c, 13, 3, 3, 13, YELLOW, 2)
    _disc(c, 8, 8, 3, YELLOW_D)
    _disc(c, 8, 8, 2, YELLOW)
    g["star"] = c

    c = _canvas()                                   # 搜索小图标
    _ring(c, 6, 6, 4, GRAY_D, 2)
    _line(c, 9, 9, 13, 13, GRAY_D, 2)
    g["search"] = c

    c = _canvas()                                   # 刷新
    _ring(c, 8, 8, 5, BLUE, 2)
    _box(c, 11, 1, 15, 5, CLEAR)
    _arrow(c, 13, 3, BLUE, "down", 3)
    g["refresh"] = c

    c = _canvas()                                   # 状态灯：绿
    _disc(c, 8, 8, 5, GREEN_D)
    _disc(c, 8, 8, 4, (72, 200, 96, 255))
    g["online"] = c

    c = _canvas()                                   # 状态灯：灰
    _disc(c, 8, 8, 5, (110, 110, 110, 255))
    _disc(c, 8, 8, 4, (170, 170, 170, 255))
    g["offline"] = c

    return g


def _chunk(tag, data):
    return (struct.pack(">I", len(data)) + tag + data +
            struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def _png(canvas):
    raw = bytearray()
    for row in canvas:
        raw.append(0)
        for r, g, b, a in row:
            raw.extend((r, g, b, a))
    png = (b"\x89PNG\r\n\x1a\n" +
           _chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0)) +
           _chunk(b"IDAT", zlib.compress(bytes(raw), 9)) +
           _chunk(b"IEND", b""))
    return base64.b64encode(png).decode("ascii")


def make_icons(root):
    out = {}
    for name, canvas in _glyphs().items():
        try:
            out[name] = tk.PhotoImage(master=root, data=_png(canvas))
        except Exception:
            pass
    return out


# ---------------------------------------------------------------- 悬浮提示
class Tooltip(object):
    """工具栏按钮的浅黄色悬浮提示，接近 SecureCRT 的 tooltip 观感。"""

    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _show(self, event=None):
        if self.tip:
            return
        try:
            x = self.widget.winfo_rootx() + 12
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 3
            self.tip = tk.Toplevel(self.widget)
            self.tip.wm_overrideredirect(True)
            self.tip.wm_geometry("+%d+%d" % (x, y))
            tk.Label(self.tip, text=self.text, bg="#FFFFE1", fg="#303030",
                     relief="solid", bd=1, padx=6, pady=2,
                     font=SMALL_FONT).pack()
        except Exception:
            self.tip = None

    def _hide(self, event=None):
        if self.tip:
            try:
                self.tip.destroy()
            except Exception:
                pass
            self.tip = None
