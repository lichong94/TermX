# -*- coding: utf-8 -*-
"""VT100 / VT220 / ANSI / xterm 终端仿真内核。

自带屏幕缓冲、回滚、备用屏幕、256 色/真彩、滚动区域与常见私有模式，
不依赖任何第三方库。UI 层只负责把 ScreenBuffer 画出来。
"""
import codecs
import time
import unicodedata

BOLD = 1
UNDERLINE = 2
REVERSE = 4
ITALIC = 8
BLINK = 16
INVISIBLE = 32
STRIKE = 64
FAINT = 128                    # SGR 2 弱化，必须与粗体分开（BUG-05）


class Cell(object):
    __slots__ = ("ch", "fg", "bg", "attrs")

    def __init__(self, ch=" ", fg=None, bg=None, attrs=0):
        self.ch = ch
        self.fg = fg
        self.bg = bg
        self.attrs = attrs

    def copy(self):
        return Cell(self.ch, self.fg, self.bg, self.attrs)


def blank_row(cols):
    return [Cell() for _ in range(cols)]


def char_width(ch):
    if not ch:
        return 1
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


class ScreenBuffer(object):
    def __init__(self, cols=80, rows=24, scheme=None, encoding="UTF-8",
                 scrollback=5000, reply_cb=None, bell_cb=None, title_cb=None):
        self.scheme = scheme or {"fg": "#CCCCCC", "bg": "#000000", "ansi": ["#000000"] * 16}
        self.cols = max(2, cols)
        self.rows = max(1, rows)
        self.scrollback_max = scrollback
        self.reply_cb = reply_cb
        self.bell_cb = bell_cb
        self.title_cb = title_cb
        self.encoding = encoding
        self.decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
        self.reset()

    # ------------------------------------------------------------------ 基础
    def reset(self):
        self.lines = [blank_row(self.cols) for _ in range(self.rows)]
        self.scrollback = []
        self.alt_active = False
        self.alt_saved = None
        self.cx = 0
        self.cy = 0
        self.fg = None
        self.bg = None
        self.attrs = 0
        self.saved_cursor = None
        self.saved_alt_cursor = None
        self.top = 0
        self.bottom = self.rows - 1
        self.origin_mode = False
        self.autowrap = True
        self.wrap_pending = False
        self.insert_mode = False
        self.app_cursor = False
        self.app_keypad = False
        self.cursor_visible = True
        self.bracketed_paste = False
        self.mouse_mode = 0
        self.mouse_sgr = False
        self.focus_events = False
        self.title = ""
        self.icon_title = ""
        self.scroll_offset = 0
        self.dirty = set(range(self.rows))
        self.full_redraw = True
        self.version = 0
        self.tabstops = set(range(8, self.cols, 8))
        self.state = "GROUND"
        self.params = []
        self.intermediates = ""
        self.private = ""
        self.osc_buf = ""
        self.ignore_until_st = False
        self.replies = []
        self.charset = 0
        self._scrolled_up = 0
        # v1.4.10：每行展示元数据 (全局行号, 该行产生时间 HH:MM:SS)。
        # v1.4.11：行号/时间戳改为“该行真正产生可见内容时”才分配，
        # 空白行保持 (None, None)，渲染时整行不显示行号与时间。
        self._seq = 0
        self.line_meta = [self._new_meta() for _ in range(self.rows)]
        self.scrollback_meta = []
        # 本次 feed 中内容发生变化的行，feed 结束时统一结算行号与时间戳
        self._touched = set()
        # 当前命令的执行时刻。用户按下回车时更新；本条命令的命令行与它的
        # 输出行都标注这个时刻——这正是用户要的"时间是执行这一行命令的时间"。
        self._cmd_stamp = None

    def _new_meta(self):
        """新建一行的元数据。行号与时间戳都留空：等这一行真正写出可见内容时才分配。
        这样“还没有命令的空会话”“清屏/滚屏产生的空白行”都不会显示行号和时间，
        而“时间”对应的是这一行命令真正产生的时间，而不是行被创建的时间。

        取值含义：
          (None, None) -> 该行没有任何可见内容，左侧行号栏整行留空
          (seq, stamp) -> 已编号，seq 为全局行号，stamp 为 HH:MM:SS 时间戳
        """
        return (None, None)

    def _cells_blank(self, cells):
        """给定一行单元格，判断它是否没有任何可见字符。"""
        for c in cells:
            if c.ch and c.ch.strip():
                return False
        return True

    def _row_blank(self, y):
        """该行是否没有任何可见字符。"""
        if not (0 <= y < self.rows):
            return True
        return self._cells_blank(self.lines[y])

    def _sync_meta(self):
        """保证元数据数组与屏幕/回滚行数一致，任何漏同步都不会造成错位。"""
        if len(self.line_meta) < self.rows:
            self.line_meta += [self._new_meta()
                               for _ in range(self.rows - len(self.line_meta))]
        elif len(self.line_meta) > self.rows:
            del self.line_meta[self.rows:]
        n = len(self.scrollback)
        if len(self.scrollback_meta) < n:
            pad = self._new_meta()
            self.scrollback_meta = ([pad] * (n - len(self.scrollback_meta))
                                    + self.scrollback_meta)
        elif len(self.scrollback_meta) > n:
            del self.scrollback_meta[:len(self.scrollback_meta) - n]

    def set_encoding(self, enc):
        try:
            self.decoder = codecs.getincrementaldecoder(enc)(errors="replace")
            self.encoding = enc
        except Exception:
            pass

    def set_scheme(self, scheme):
        self.scheme = scheme
        self.full_redraw = True
        self.version += 1

    def _touch(self, y):
        self.dirty.add(y)
        self.version += 1

    def _reply(self, data):
        self.replies.append(data)
        if self.reply_cb:
            try:
                self.reply_cb(data)
            except Exception:
                pass

    def _bell(self):
        if self.bell_cb:
            try:
                self.bell_cb()
            except Exception:
                pass

    def pop_replies(self):
        r = self.replies
        self.replies = []
        return b"".join(r)

    # ------------------------------------------------------------------ 尺寸
    def resize(self, cols, rows):
        cols, rows = max(2, cols), max(1, rows)
        if cols == self.cols and rows == self.rows:
            return
        old = self.lines
        new = []
        for i in range(rows):
            if i < len(old):
                line = old[i][:cols]
                if len(line) < cols:
                    line += blank_row(cols - len(line))
            else:
                line = blank_row(cols)
            new.append(line)
        # 被裁掉的主屏行进回滚
        for line in old[rows:]:
            if any(c.ch.strip() for c in line):
                self.scrollback.append(line)
                self.scrollback_meta.append(self._new_meta())
        self.lines = new
        self.line_meta = (self.line_meta[:rows] +
                          [self._new_meta() for _ in range(max(0, rows - len(self.line_meta)))])
        self.cols, self.rows = cols, rows
        self.cx = min(self.cx, cols - 1)
        self.cy = min(self.cy, rows - 1)
        self.top, self.bottom = 0, rows - 1
        self.wrap_pending = False
        self.scroll_offset = 0
        self.full_redraw = True
        self.version += 1
        self.trim_scrollback()

    def trim_scrollback(self):
        if len(self.scrollback) > self.scrollback_max:
            cut = len(self.scrollback) - self.scrollback_max
            del self.scrollback[:cut]
            if len(self.scrollback_meta) >= cut:
                del self.scrollback_meta[:cut]
            else:
                self.scrollback_meta = []
            self.scroll_offset = min(self.scroll_offset, len(self.scrollback))
        self._sync_meta()

    # ------------------------------------------------------------------ 屏幕操作
    def _scroll_up(self, n=1):
        for _ in range(min(n, self.bottom - self.top + 1)):
            removed = self.lines.pop(self.top)
            removed_meta = (self.line_meta.pop(self.top)
                            if self.top < len(self.line_meta) else self._new_meta())
            if not self.alt_active:
                self.scrollback.append(removed)
                self.scrollback_meta.append(removed_meta)
                self._scrolled_up += 1
            self.lines.insert(self.bottom, blank_row(self.cols))
            self.line_meta.append(self._new_meta())
        if not self.alt_active and self.scroll_offset > 0:
            # 回滚状态下新历史到来：保持当前查看的位置，不被拉回底部
            self.scroll_offset = min(len(self.scrollback), self.scroll_offset + n)
        self.trim_scrollback()
        self.full_redraw = True
        self.version += 1

    def _scroll_down(self, n=1):
        for _ in range(min(n, self.bottom - self.top + 1)):
            self.lines.pop(self.bottom)
            self.lines.insert(self.top, blank_row(self.cols))
            if self.bottom < len(self.line_meta):
                self.line_meta.pop(self.bottom)
            self.line_meta.insert(self.top, self._new_meta())
        self.full_redraw = True
        self.version += 1

    def _line_feed(self):
        if self.cy == self.bottom:
            self._scroll_up(1)
        elif self.cy < self.rows - 1:
            self.cy += 1

    def _reverse_index(self):
        if self.cy == self.top:
            self._scroll_down(1)
        elif self.cy > 0:
            self.cy -= 1

    def _set_cell(self, x, y, cell):
        if 0 <= y < self.rows and 0 <= x < self.cols:
            old = self.lines[y][x]
            old_vis = bool(old.ch and old.ch.strip())
            new_vis = bool(cell.ch and cell.ch.strip())
            self.lines[y][x] = cell
            self._touch(y)
            if old_vis != new_vis or (old_vis and new_vis and old.ch != cell.ch):
                # 该行可见内容发生变化 -> 记录下来，feed 结束时统一结算。
                # 这里绝不能逐格打时间戳：ConPTY 整屏重绘会把每一格都重写一遍，
                # 逐格打点会让所有行的时间都变成当前时刻（用户看到的 46 行时间全同）。
                self._touched.add(y)

    def _erase_cell(self, x, y):
        self._set_cell(x, y, Cell(" ", self.fg, self.bg, self.attrs & ~REVERSE))

    def _put_char(self, ch):
        w = char_width(ch)
        if w == 0:
            # 组合字符：附加到前一格
            x = max(0, self.cx - 1)
            if 0 <= self.cy < self.rows and 0 <= x < self.cols:
                c = self.lines[self.cy][x]
                c.ch = (c.ch or " ") + ch
                self._touch(self.cy)
            return
        if self.wrap_pending and self.autowrap:
            self.cx = 0
            self._line_feed()
            self.wrap_pending = False
        if w == 2 and self.cx >= self.cols - 1:
            if self.autowrap:
                self.cx = 0
                self._line_feed()
            else:
                self.cx = self.cols - 2
        cell = Cell(ch, self.fg, self.bg, self.attrs)
        if self.insert_mode and self.cx < self.cols:
            line = self.lines[self.cy]
            line.pop(self.cols - 1)
            line.insert(self.cx, blank_row(1)[0])
        self._set_cell(self.cx, self.cy, cell)
        if w == 2 and self.cx + 1 < self.cols:
            self._set_cell(self.cx + 1, self.cy, Cell("", self.fg, self.bg, self.attrs))
        self.cx += w
        if self.cx >= self.cols:
            self.cx = self.cols - 1
            self.wrap_pending = self.autowrap
        self.full_redraw = False

    def _erase_display(self, mode):
        if mode == 0:
            for x in range(self.cx, self.cols):
                self._erase_cell(x, self.cy)
            for y in range(self.cy + 1, self.rows):
                self.lines[y] = blank_row(self.cols)
                self._touch(y)
                self._touched.add(y)
        elif mode == 1:
            for y in range(0, self.cy):
                self.lines[y] = blank_row(self.cols)
                self._touch(y)
                self._touched.add(y)
            for x in range(0, min(self.cx + 1, self.cols)):
                self._erase_cell(x, self.cy)
        elif mode in (2, 3):
            self.lines = [blank_row(self.cols) for _ in range(self.rows)]
            self.line_meta = [self._new_meta() for _ in range(self.rows)]
            self.dirty = set(range(self.rows))
            self.version += 1
        if mode == 3:
            self.scrollback = []
            self.scrollback_meta = []
            self.scroll_offset = 0

    def _erase_line(self, mode):
        y = self.cy
        if mode == 0:
            for x in range(self.cx, self.cols):
                self._erase_cell(x, y)
        elif mode == 1:
            for x in range(0, min(self.cx + 1, self.cols)):
                self._erase_cell(x, y)
        elif mode == 2:
            self.lines[y] = blank_row(self.cols)
            self._touch(y)
            self._touched.add(y)

    def _delete_chars(self, n):
        line = self.lines[self.cy]
        x = self.cx
        del line[x:min(self.cols, x + n)]
        line.extend(blank_row(self.cols - len(line)))
        self._touch(self.cy)

    def _insert_chars(self, n):
        line = self.lines[self.cy]
        x = self.cx
        for _ in range(min(n, self.cols - x)):
            line.insert(x, Cell(" ", self.fg, self.bg, self.attrs))
            if len(line) > self.cols:
                line.pop()
        self._touch(self.cy)

    def _insert_lines(self, n):
        if self.top <= self.cy <= self.bottom:
            for _ in range(min(n, self.bottom - self.cy + 1)):
                self.lines.pop(self.bottom)
                self.lines.insert(self.cy, blank_row(self.cols))
                if self.bottom < len(self.line_meta):
                    self.line_meta.pop(self.bottom)
                self.line_meta.insert(self.cy, self._new_meta())
            self.full_redraw = True
            self.version += 1
            self.cx = 0

    def _delete_lines(self, n):
        if self.top <= self.cy <= self.bottom:
            for _ in range(min(n, self.bottom - self.cy + 1)):
                self.lines.pop(self.cy)
                self.lines.insert(self.bottom, blank_row(self.cols))
                if self.cy < len(self.line_meta):
                    self.line_meta.pop(self.cy)
                self.line_meta.append(self._new_meta())
            self.full_redraw = True
            self.version += 1
            self.cx = 0

    def _goto(self, x, y):
        self.cx = max(0, min(self.cols - 1, x))
        lo = self.top if self.origin_mode else 0
        hi = self.bottom if self.origin_mode else self.rows - 1
        self.cy = max(lo, min(hi, y + lo))
        self.wrap_pending = False

    # ------------------------------------------------------------------ SGR
    def _sgr(self, ps):
        ansi = self.scheme.get("ansi", ["#000000"] * 16)
        i = 0
        while i < len(ps):
            p = ps[i]
            if p == 0:
                self.fg = self.bg = None
                self.attrs = 0
            elif p == 1:
                self.attrs |= BOLD
            elif p == 2:
                # SGR 2 是“弱化”（faint），旧代码误当粗体，于是 Man page /
                # 日志里的次要文字反而被加粗显示（测试报告 BUG-05）
                self.attrs |= FAINT
            elif p == 3:
                self.attrs |= ITALIC
            elif p == 4:
                self.attrs |= UNDERLINE
            elif p in (5, 6):
                self.attrs |= BLINK
            elif p == 7:
                self.attrs |= REVERSE
            elif p == 8:
                self.attrs |= INVISIBLE
            elif p == 9:
                self.attrs |= STRIKE
            elif p == 21:
                self.attrs &= ~BOLD        # 部分终端用 21 表示“取消粗体”
            elif p == 22:
                self.attrs &= ~(BOLD | FAINT)
            elif p == 23:
                self.attrs &= ~ITALIC
            elif p == 24:
                self.attrs &= ~UNDERLINE
            elif p == 25:
                self.attrs &= ~BLINK
            elif p == 27:
                self.attrs &= ~REVERSE
            elif p == 28:
                self.attrs &= ~INVISIBLE
            elif p == 29:
                self.attrs &= ~STRIKE
            elif 30 <= p <= 37:
                self.fg = ansi[p - 30] if p - 30 < len(ansi) else self.scheme.get("fg")
            elif p == 39:
                self.fg = None
            elif 40 <= p <= 47:
                self.bg = ansi[p - 40] if p - 40 < len(ansi) else self.scheme.get("bg")
            elif p == 49:
                self.bg = None
            elif 90 <= p <= 97:
                self.fg = ansi[p - 82] if p - 82 < len(ansi) else self.scheme.get("fg")
            elif 100 <= p <= 107:
                self.bg = ansi[p - 92] if p - 92 < len(ansi) else self.scheme.get("bg")
            elif p in (38, 48):
                is_fg = p == 38
                color = None
                if i + 1 < len(ps) and ps[i + 1] == 5 and i + 2 < len(ps):
                    from .config import color_256
                    color = color_256(ps[i + 2], self.scheme)
                    i += 2
                elif i + 1 < len(ps) and ps[i + 1] == 2 and i + 4 < len(ps):
                    color = "#%02X%02X%02X" % (ps[i + 2] & 255, ps[i + 3] & 255, ps[i + 4] & 255)
                    i += 4
                if color:
                    if is_fg:
                        self.fg = color
                    else:
                        self.bg = color
            i += 1

    # ------------------------------------------------------------------ CSI
    def _csi(self, final, ps, private, inter):
        def arg(n, default=1):
            if n < len(ps) and ps[n] > 0:
                return ps[n]
            return default

        f = final
        if f == "A":
            self.cy = max(self.top if self.origin_mode else 0, self.cy - arg(0))
            self.wrap_pending = False
        elif f == "B" or f == "e":
            self.cy = min(self.bottom if self.origin_mode else self.rows - 1, self.cy + arg(0))
            self.wrap_pending = False
        elif f == "C" or f == "a":
            self.cx = min(self.cols - 1, self.cx + arg(0))
            self.wrap_pending = False
        elif f == "D":
            self.cx = max(0, self.cx - arg(0))
            self.wrap_pending = False
        elif f in ("H", "f"):
            self._goto(arg(1, 1) - 1, arg(0, 1) - 1)
        elif f == "G" or f == "`":
            self.cx = max(0, min(self.cols - 1, arg(0) - 1))
            self.wrap_pending = False
        elif f == "d":
            self._goto(self.cx, arg(0) - 1)
        elif f == "E":
            self.cy = min(self.rows - 1, self.cy + arg(0))
            self.cx = 0
        elif f == "F":
            self.cy = max(0, self.cy - arg(0))
            self.cx = 0
        elif f == "J":
            self._erase_display(arg(0, 0))
        elif f == "K":
            self._erase_line(arg(0, 0))
        elif f == "L":
            self._insert_lines(arg(0))
        elif f == "M":
            self._delete_lines(arg(0))
        elif f == "P":
            self._delete_chars(arg(0))
        elif f == "@":
            self._insert_chars(arg(0))
        elif f == "S":
            self._scroll_up(arg(0))
        elif f == "T":
            self._scroll_down(arg(0))
        elif f == "X":
            for x in range(self.cx, min(self.cols, self.cx + arg(0))):
                self._erase_cell(x, self.cy)
        elif f == "m":
            self._sgr(ps)
        elif f == "s":
            self.saved_cursor = (self.cx, self.cy, self.fg, self.bg, self.attrs,
                                 self.origin_mode, self.autowrap)
        elif f == "u":
            if self.saved_cursor:
                (self.cx, self.cy, self.fg, self.bg, self.attrs,
                 self.origin_mode, self.autowrap) = self.saved_cursor
                self.cx = min(self.cx, self.cols - 1)
                self.cy = min(self.cy, self.rows - 1)
        elif f == "r":
            if private == "?":
                pass
            else:
                t = arg(0, 1) - 1
                b = arg(1, self.rows) - 1
                if 0 <= t < b < self.rows:
                    self.top, self.bottom = t, b
                self._goto(0, 0)
        elif f == "n":
            if ps and ps[0] == 6:
                self._reply(b"\x1b[%d;%dR" % (self.cy + 1, self.cx + 1))
            elif ps and ps[0] == 5:
                self._reply(b"\x1b[0n")
        elif f == "c":
            if private == ">":
                self._reply(b"\x1b[>0;10;1c")
            else:
                self._reply(b"\x1b[?1;2;4;6;9;15;22c")
        elif f == "b":
            ch = self.lines[self.cy][self.cx].ch if self.cx < self.cols else " "
            for _ in range(min(arg(0), self.cols - self.cx)):
                self._put_char(ch)
        elif f == "g":
            if arg(0, 0) == 0:
                self.tabstops.discard(self.cx)
            elif arg(0, 0) == 3:
                self.tabstops.clear()
        elif f == "h" or f == "l":
            self._mode(f == "h", ps, private)
        elif f == "t":
            pass  # 窗口操作请求，桌面终端忽略
        elif f == "q" and private == "?":
            pass  # DECSCUSR 由 UI 处理
        # 其余 CSI 静默忽略

    def _mode(self, enable, ps, private):
        if private != "?":
            if 4 in ps:
                self.insert_mode = enable
            if 20 in ps:
                pass  # LNM
            return
        for p in ps:
            if p == 1:
                self.app_cursor = enable
            elif p == 7:
                self.autowrap = enable
            elif p == 12:
                pass
            elif p == 25:
                self.cursor_visible = enable
                self.full_redraw = True
            elif p == 1000:
                self.mouse_mode = 1000 if enable else 0
            elif p == 1002:
                self.mouse_mode = 1002 if enable else 0
            elif p == 1003:
                self.mouse_mode = 1003 if enable else 0
            elif p == 1005:
                self.mouse_sgr = False
            elif p == 1006:
                self.mouse_sgr = enable
                self.mouse_mode = self.mouse_mode or 1000
            elif p == 1015:
                self.mouse_mode = 1015 if enable else 0
            elif p == 1004:
                self.focus_events = enable
            elif p == 2004:
                self.bracketed_paste = enable
            elif p in (47, 1047, 1049):
                self._alt_screen(enable, p == 1049)
            elif p in (1048, 1049):
                if enable:
                    self.saved_alt_cursor = (self.cx, self.cy)
                elif self.saved_alt_cursor:
                    self.cx, self.cy = self.saved_alt_cursor
                    self.cx = min(self.cx, self.cols - 1)
                    self.cy = min(self.cy, self.rows - 1)

    def _alt_screen(self, enable, save_cursor):
        if enable and not self.alt_active:
            self.alt_saved = (self.lines, self.scrollback, self.line_meta,
                              self.scrollback_meta, self.cx, self.cy,
                              self.saved_cursor, self.top, self.bottom)
            if save_cursor:
                self.saved_alt_cursor = (self.cx, self.cy)
            self.lines = [blank_row(self.cols) for _ in range(self.rows)]
            self.scrollback = []
            self.line_meta = [self._new_meta() for _ in range(self.rows)]
            self.scrollback_meta = []
            self.cx = self.cy = 0
            self.top, self.bottom = 0, self.rows - 1
            self.alt_active = True
            self.scroll_offset = 0
            self.wrap_pending = False
        elif not enable and self.alt_active and self.alt_saved:
            (self.lines, self.scrollback, self.line_meta, self.scrollback_meta,
             self.cx, self.cy, self.saved_cursor, self.top, self.bottom) = self.alt_saved
            self.alt_saved = None
            self.alt_active = False
            self.scroll_offset = 0
            self.wrap_pending = False
            self.cx = min(self.cx, self.cols - 1)
            self.cy = min(self.cy, self.rows - 1)
        self.full_redraw = True
        self.version += 1

    # ------------------------------------------------------------------ OSC
    def _osc(self, text):
        if not text:
            return
        parts = text.split(";", 1)
        try:
            code = int(parts[0])
        except ValueError:
            return
        val = parts[1] if len(parts) > 1 else ""
        if code in (0, 1, 2):
            if code in (0, 2):
                self.title = val
            else:
                self.icon_title = val
            if self.title_cb:
                try:
                    self.title_cb(self.title)
                except Exception:
                    pass
        elif code == 52:
            # OSC 52 剪贴板：由 UI 层按需处理
            pass
        # 4/10/11 调色板查询：静默忽略，不影响显示

    # ------------------------------------------------------------------ 输入流
    def feed(self, data):
        """喂入主机输出的字节流。"""
        if not data:
            return
        # ConPTY（Windows 伪控制台）采用“整屏重绘”模式：滚屏时不会产生
        # LF/RI 等滚动序列，而是用绝对定位重写整屏，_scroll_up 永远不会
        # 触发，回滚区因此永远为空。这里在喂入前后做帧对比：若整屏内容
        # 整体上移 k 行，则把被推出屏幕顶部的 k 行放入回滚区。
        # 流式协议（Telnet/Raw 等）走 _scroll_up 路径，跳过本检测避免重复入栈。
        snap_cells = None
        snap_texts = None
        snap_meta = None
        if not self.alt_active and self.top == 0 and self.bottom == self.rows - 1:
            snap_cells = [list(row) for row in self.lines]
            snap_texts = ["".join(c.ch for c in row).rstrip() for row in snap_cells]
            snap_meta = list(self.line_meta)
        self._scrolled_up = 0
        self._touched = set()
        text = self.decoder.decode(data)
        for ch in text:
            self._consume(ch)
        scrolled = self._scrolled_up
        self._scrolled_up = 0
        shifted = False
        if (snap_cells is not None and scrolled == 0
                and not self.alt_active and self.top == 0
                and self.bottom == self.rows - 1):
            shifted = self._detect_scrollback(snap_cells, snap_texts, snap_meta)
        self._commit_meta(shifted)

    def _commit_meta(self, shifted=False):
        """feed 结束：结算本轮的行号与时间戳。

        核心原则：
        1) **时间戳只在"这一行真正产生/执行内容"时确定，之后永不因
           内容重绘而刷新。** 这正是为了对付 ConPTY 的整屏重绘——PowerShell
           每次输出都会把整屏重写一遍，如果内容一变就刷新时间，屏幕上几十行
           的时间会全部变成当前时刻（用户截图里几十行时间完全相同就是这个
           原因）。
        2) **行号稳定**：一行被擦除后再写入，行号沿用原来的，不乱跳；
           只有"从未有过内容的新行"才会拿到新的全局行号。

        具体规则：
        - 该行第一次出现可见内容 -> 分配全局行号 + 时间。
          行号按从上到下的顺序分配，因此同一批产生的多行，行号也是递增的。
        - 已有行号、内容也被改写 -> 行号不变、时间不变（时间即该行命令
          真正产生/执行的时刻）。
        - 该行被清空 -> 保留行号、抹掉时间戳；重新写入时补一个新的时间。
        """
        now = None
        touched = self._touched
        self._touched = set()
        # 1) 被清空的行：保留行号，抹掉时间戳
        for y in touched:
            if not (0 <= y < self.rows) or y >= len(self.line_meta):
                continue
            seq, stamp = self.line_meta[y]
            if seq is None:
                continue
            if self._row_blank(y):
                self.line_meta[y] = (seq, None)
        # 2) 补齐：没有行号的行分配行号与时间；有行号但缺时间的补时间
        for y in range(min(self.rows, len(self.line_meta))):
            seq, stamp = self.line_meta[y]
            blank = self._row_blank(y)
            if seq is None:
                if blank:
                    continue
                if now is None:
                    now = time.strftime("%H:%M:%S")
                self._seq += 1
                # 时间优先取“当前这条命令的执行时刻”（用户按下回车的时刻）；
                # 没有命令上下文时（启动横幅、脚本下发等）取当前时刻。
                self.line_meta[y] = (self._seq, self._cmd_stamp or now)
            elif stamp is None and not blank:
                if now is None:
                    now = time.strftime("%H:%M:%S")
                self.line_meta[y] = (seq, self._cmd_stamp or now)

    def clear_screen(self):
        """清空当前屏幕（保留回滚历史）。

        行号/时间戳元数据一并清空：清屏后屏幕是空的，左侧行号栏
        不该还留着上一屏的行号与时间。全局行号计数器 _seq 不重置，
        因此清屏前后行号仍然单调递增，不会出现重复行号。
        """
        self.lines = [blank_row(self.cols) for _ in range(self.rows)]
        self.line_meta = [self._new_meta() for _ in range(self.rows)]
        self.cx = self.cy = 0
        self.dirty = set(range(self.rows))
        self.full_redraw = True
        self.version += 1

    def note_command(self, y=None):
        """用户按下回车：命令这一刻真正执行。

        - 记录执行时刻 _cmd_stamp，接下来产生的行（命令回显与它的输出）
          都标注这个时刻——这正是用户要的"时间是执行这一行命令的时间"；
        - 同时把当前命令行（光标所在行）已有的时间戳刷成执行时刻
          （行号不变）。
        """
        stamp = time.strftime("%H:%M:%S")
        self._cmd_stamp = stamp
        if y is None:
            y = self.cy
        self.refresh_line_time(y, stamp)

    def refresh_line_time(self, y, stamp=None):
        """刷新某一行的时间戳（行号不变）。

        时间对应“这一行命令真正产生/执行”的时刻，
        而不是行被创建（滚屏进历史区）的时刻。
        """
        if not (0 <= y < len(self.line_meta)):
            return
        seq, _stamp = self.line_meta[y]
        if seq is not None:
            self.line_meta[y] = (seq, stamp or time.strftime("%H:%M:%S"))

    def _detect_scrollback(self, old_cells, old_texts, old_meta=None):
        """帧对比：检测整屏上移并把滚出屏幕顶部的行存入回滚区。

        返回 True 表示检测到整屏上移（内容整体滚出顶部）。
        feed() 用它来决定"是否刷新已有行的时间戳"：
        整屏上移时上面的行只是滚动了位置、时间不该变；
        没有上移时说明内容真的被改写，才刷新该行时间。
        """
        if not old_texts or not old_texts[0]:
            return False
        new_texts = [self.line_text(y) for y in range(self.rows)]
        if old_texts[0] == new_texts[0]:
            return False
        shift = 0
        for k in range(1, self.rows):
            for i in range(self.rows - k):
                # 上滚 k 行：新屏第 i 行来自旧屏第 i+k 行
                if new_texts[i] != old_texts[i + k]:
                    break
            else:
                shift = k
                break
        if shift <= 0:
            return False
        for y in range(shift):
            self.scrollback.append(old_cells[y])
            self.scrollback_meta.append(
                old_meta[y] if old_meta and y < len(old_meta) else self._new_meta())
        if old_meta:
            # 屏幕上移 shift 行：行号/时间跟着内容一起上移，底部是新产生的行
            self.line_meta = (list(old_meta[shift:]) +
                              [self._new_meta() for _ in range(shift)])
        if self.scroll_offset > 0:
            # 回滚状态下新历史到来：保持当前查看的位置
            self.scroll_offset = min(len(self.scrollback), self.scroll_offset + shift)
        self.trim_scrollback()
        self.full_redraw = True
        self.version += 1
        return True

    def _consume(self, ch):
        o = ord(ch)
        st = self.state
        if self.ignore_until_st:
            if ch == "\x07" or ch == "\x1b":
                self.ignore_until_st = False
                if ch == "\x1b":
                    self.state = "ST_ESC"
            return
        if st == "GROUND":
            if o == 0x1b:
                self.state = "ESC"
            elif 0x80 <= o <= 0x9f:
                c1 = {0x9b: "[", 0x9d: "]", 0x90: "P", 0x9f: "_", 0x98: "^"}.get(o)
                if c1:
                    self._enter_escape(c1)
                elif o == 0x9c:
                    pass
            elif o == 0x0d:
                self.cx = 0
                self.wrap_pending = False
            elif o == 0x0a or o == 0x0b or o == 0x0c:
                self._line_feed()
            elif o == 0x08:
                self.cx = max(0, self.cx - 1)
                self.wrap_pending = False
            elif o == 0x09:
                nxt = [t for t in self.tabstops if t > self.cx]
                self.cx = min(nxt) if nxt else self.cols - 1
                self.wrap_pending = False
            elif o == 0x07:
                self._bell()
            elif o < 0x20 or o == 0x7f:
                pass
            else:
                self._put_char(ch)
        elif st == "ESC":
            self._enter_escape(ch)
        elif st == "ST_ESC":
            self.state = "GROUND"
        elif st == "CSI":
            if o == 0x1b:
                self.state = "ESC"
            elif 0x30 <= o <= 0x3f:
                if ch in "?<>=!":
                    self.private += ch
                else:
                    self._param(ch)
            elif 0x20 <= o <= 0x2f:
                self.intermediates += ch
            elif 0x40 <= o <= 0x7e:
                self._csi(ch, self._params(), self.private, self.intermediates)
                self.state = "GROUND"
            elif o < 0x20:
                self._consume_control_in_escape(ch)
            else:
                self.state = "GROUND"
        elif st == "OSC":
            if ch == "\x07":
                self._osc(self.osc_buf)
                self.osc_buf = ""
                self.state = "GROUND"
            elif ch == "\x1b":
                self.state = "OSC_ESC"
            else:
                self.osc_buf += ch
        elif st == "OSC_ESC":
            if ch == "\\":
                self._osc(self.osc_buf)
                self.osc_buf = ""
                self.state = "GROUND"
            else:
                self.osc_buf += "\x1b" + ch
                self.state = "OSC"
        elif st == "CHARSET":
            self.charset = 0
            self.state = "GROUND"

    def _consume_control_in_escape(self, ch):
        if ch == "\r":
            self.cx = 0
        elif ch == "\n":
            self._line_feed()
        elif ch == "\x07":
            self._bell()

    def _enter_escape(self, ch):
        if ch == "[":
            self.state = "CSI"
            self.params = []
            self.private = ""
            self.intermediates = ""
        elif ch == "]":
            self.state = "OSC"
            self.osc_buf = ""
        elif ch in "P^_X":
            self.ignore_until_st = True
            self.state = "GROUND"
        elif ch in "()*+-./":
            self.state = "CHARSET"
        elif ch == "7":
            self.saved_cursor = (self.cx, self.cy, self.fg, self.bg, self.attrs,
                                 self.origin_mode, self.autowrap)
            self.state = "GROUND"
        elif ch == "8":
            if self.saved_cursor:
                (self.cx, self.cy, self.fg, self.bg, self.attrs,
                 self.origin_mode, self.autowrap) = self.saved_cursor
                self.cx = min(self.cx, self.cols - 1)
                self.cy = min(self.cy, self.rows - 1)
            self.state = "GROUND"
        elif ch == "D":
            self._line_feed()
            self.state = "GROUND"
        elif ch == "E":
            self.cx = 0
            self._line_feed()
            self.state = "GROUND"
        elif ch == "M":
            self._reverse_index()
            self.state = "GROUND"
        elif ch == "H":
            self.tabstops.add(self.cx)
            self.state = "GROUND"
        elif ch == "c":
            self.reset()
        elif ch == "=":
            self.app_keypad = True
            self.state = "GROUND"
        elif ch == ">":
            self.app_keypad = False
            self.state = "GROUND"
        elif ch == "#":
            self.state = "CHARSET"
        elif ch == "O":
            self.state = "GROUND"
        elif ch == "\\":
            self.state = "GROUND"
        else:
            # ESC + 普通字符（Alt 组合）当作普通键，显示时忽略
            self.state = "GROUND"

    def _param(self, ch):
        if ch == ";":
            self.params.append([])
        else:
            if not self.params:
                self.params.append([])
            self.params[-1].append(ch)

    def _params(self):
        out = []
        for grp in self.params:
            s = "".join(grp)
            try:
                out.append(int(s)) if s else out.append(0)
            except ValueError:
                out.append(0)
        if not out:
            out = [0]
        return out

    # ------------------------------------------------------------------ 查询接口
    def visible_lines(self):
        """返回当前视口应显示的 rows 行（含回滚）。"""
        if self.scroll_offset <= 0:
            return self.lines
        total = len(self.scrollback)
        start = max(0, total - self.scroll_offset)
        view = self.scrollback[start:start + self.rows]
        if len(view) < self.rows:
            view = view + self.lines[:self.rows - len(view)]
        return view

    def visible_meta(self):
        """返回当前视口每行的 (行号, 时间)，与 visible_lines() 一一对应。

        空白行（没有任何可见字符）一律返回 (None, None)，
        渲染时左侧行号栏整行留空——这就是"没有命令的时候不需要显示
        行号和时间"的实现。行号本身仍保留在 line_meta 里（不清除），
        这样同一物理行被清空后重新写入时，行号不会乱跳。
        """
        self._sync_meta()
        lines = self.visible_lines()
        if self.scroll_offset <= 0:
            metas = list(self.line_meta)
        else:
            total = len(self.scrollback_meta)
            start = max(0, total - self.scroll_offset)
            view = self.scrollback_meta[start:start + self.rows]
            if len(view) < self.rows:
                view = view + self.line_meta[:self.rows - len(view)]
            metas = list(view)
        out = []
        for i, meta in enumerate(metas):
            cells = lines[i] if i < len(lines) else None
            if cells is None or self._cells_blank(cells):
                out.append((None, None))
            else:
                out.append(meta if meta else (None, None))
        return out

    def scroll_view(self, delta):
        old = self.scroll_offset
        self.scroll_offset = max(0, min(len(self.scrollback), self.scroll_offset + delta))
        if self.scroll_offset != old:
            self.full_redraw = True
            self.version += 1
        return self.scroll_offset

    def line_text(self, y):
        if 0 <= y < self.rows:
            # ch == "" 是全角字符的占位列，不能当作空格输出
            return "".join(c.ch for c in self.lines[y]).rstrip()
        return ""

    def plain_text(self):
        return "\n".join(self.line_text(y) for y in range(self.rows))

    def region_text(self, row1, col1, row2, col2):
        out = []
        for y in range(max(1, row1), min(self.rows, row2) + 1):
            line = self.lines[y - 1]
            out.append("".join(c.ch for c in line[max(0, col1 - 1):col2]).rstrip())
        return "\r\n".join(out)

    def cursor_snapshot(self):
        return (self.cy + 1, self.cx + 1)

    def all_text(self):
        lines = ["".join(c.ch for c in r).rstrip() for r in self.scrollback]
        lines += [self.line_text(y) for y in range(self.rows)]
        return "\n".join(lines)
