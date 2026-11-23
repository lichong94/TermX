# -*- coding: utf-8 -*-
"""终端显示控件：基于 tk.Text 的 ANSI 着色渲染、回滚、选择、键位映射。"""
import re
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

from .ansi import ScreenBuffer, BOLD, UNDERLINE, REVERSE, ITALIC, STRIKE, INVISIBLE, FAINT, Cell

CTRL = 0x0004
SHIFT = 0x0001
ALT = 0x20000


def _dim_color(fg, bg, ratio=0.45):
    """把前景色按 ratio 混入背景色，用于渲染 SGR 2（弱化）。

    颜色不是标准 #rrggbb 时原样返回，绝不影响正常显示。
    """
    try:
        f = str(fg).lstrip("#")
        b = str(bg).lstrip("#")
        if len(f) != 6 or len(b) != 6:
            return fg
        fr, fgc, fb = int(f[0:2], 16), int(f[2:4], 16), int(f[4:6], 16)
        br, bgc, bb = int(b[0:2], 16), int(b[2:4], 16), int(b[4:6], 16)
        return "#%02X%02X%02X" % (int(fr * (1 - ratio) + br * ratio),
                                  int(fgc * (1 - ratio) + bgc * ratio),
                                  int(fb * (1 - ratio) + bb * ratio))
    except Exception:
        return fg


SPECIAL_KEYS = {
    "Up": ("\x1b[A", "\x1bOA"), "Down": ("\x1b[B", "\x1bOB"),
    "Right": ("\x1b[C", "\x1bOC"), "Left": ("\x1b[D", "\x1bOD"),
    "Home": ("\x1b[H", "\x1bOH"), "End": ("\x1b[F", "\x1bOF"),
    "Insert": ("\x1b[2~", "\x1b[2~"), "Delete": ("\x1b[3~", "\x1b[3~"),
    "Prior": ("\x1b[5~", "\x1b[5~"), "Next": ("\x1b[6~", "\x1b[6~"),
    "F1": ("\x1bOP", "\x1bOP"), "F2": ("\x1bOQ", "\x1bOQ"),
    "F3": ("\x1bOR", "\x1bOR"), "F4": ("\x1bOS", "\x1bOS"),
    "F5": ("\x1b[15~", "\x1b[15~"), "F6": ("\x1b[17~", "\x1b[17~"),
    "F7": ("\x1b[18~", "\x1b[18~"), "F8": ("\x1b[19~", "\x1b[19~"),
    "F9": ("\x1b[20~", "\x1b[20~"), "F10": ("\x1b[21~", "\x1b[21~"),
    "F11": ("\x1b[23~", "\x1b[23~"), "F12": ("\x1b[24~", "\x1b[24~"),
}


def scroll_metrics(sb, rows, offset):
    """计算滚动条滑块位置；与 offset_from_frac() 严格互逆。"""
    total = sb + rows
    if total <= 0:
        return 0.0, 1.0
    first = max(0.0, (sb - offset) / float(total))
    last = min(1.0, first + rows / float(total))
    return first, last


def offset_from_frac(frac, sb, rows):
    """把滑块顶部位置换算成回滚偏移（scroll_offset）。"""
    total = sb + rows
    off = int(round((1.0 - float(frac)) * total - rows))
    return max(0, min(sb, off))


class TerminalView(tk.Frame):
    def __init__(self, parent, cfg, on_input=None, on_resize=None, on_context=None,
                 on_title=None, on_bell=None, confirm_paste=None):
        tk.Frame.__init__(self, parent, highlightthickness=0, takefocus=1)
        self.cfg = cfg
        self.on_input = on_input
        self.on_resize = on_resize
        self.on_context = on_context
        self.confirm_paste = confirm_paste
        self.external_title = on_title
        self.on_bell = on_bell
        self.scheme = cfg.scheme()
        self.buffer = ScreenBuffer(cfg.get("cols", 100), cfg.get("rows", 30),
                                   self.scheme, cfg.get("encoding", "UTF-8"),
                                   cfg.get("scrollback", 5000),
                                   reply_cb=self._reply, bell_cb=self._bell,
                                   title_cb=self._title)
        self._fonts = {}
        self._tags = {}
        self._sigs = []
        self._cursor_visible = True
        self._blink_state = True
        self._pending = False
        self._resize_job = None
        self._last_size = None
        self._gutter_active = False   # 用户是否已执行过命令（控制行号栏显隐）
        self._gutter_shown = False    # 行号栏当前是否已 pack 显示
        self.highlights = []
        self._find_term = ""
        self._find_pos = "1.0"
        self._sel_marks = None
        self._paste_cb = None
        self._build_fonts()
        self._build_widgets()
        self._apply_scheme()
        self._reset_text_grid()
        self._bind_events()
        self._blink()
        self.after(200, self._measure)

    # ------------------------------------------------------------------ 构建
    def _build_fonts(self):
        fam = self.cfg.get("font_family", "Consolas")
        size = int(self.cfg.get("font_size", 12))
        base = tkfont.Font(root=self, family=fam, size=size)
        self.font_normal = base
        self.font_bold = tkfont.Font(root=self, family=fam, size=size, weight="bold")
        self.font_italic = tkfont.Font(root=self, family=fam, size=size, slant="italic")
        self.font_bolditalic = tkfont.Font(root=self, family=fam, size=size,
                                           weight="bold", slant="italic")
        self._char_w = max(1, base.measure("M"))
        self._char_h = max(1, base.metrics("linespace"))

    def _build_widgets(self):
        self.text = tk.Text(self, wrap="none", state="disabled", borderwidth=0,
                            highlightthickness=0, insertwidth=0, padx=4, pady=2,
                            font=self.font_normal, undo=False, takefocus=1,
                            exportselection=True, cursor="xterm",
                            selectbackground=self.scheme.get("sel", "#264F78"))
        # v1.4.10：左侧时间戳 / 行号栏。行数、字体、内边距与主终端保持一致，
        # 通过 text 的 yscrollcommand 同步 yview，保证回滚时逐行对齐。
        self.gutter = tk.Text(self, wrap="none", state="disabled", borderwidth=0,
                              highlightthickness=0, insertwidth=0, padx=4, pady=2,
                              width=19, font=self.font_normal, undo=False,
                              takefocus=0, exportselection=False, cursor="arrow",
                              spacing1=0, spacing2=0, spacing3=0)
        self.scroll = ttk.Scrollbar(self, orient="vertical", command=self._scroll_cmd)
        self.text.configure(yscrollcommand=self._yscroll)
        self.scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        # 行号栏默认不显示；只有用户执行命令/产生历史后才由 _apply_gutter_visibility 显示
        self._gutter_shown = False
        self._gutter_cache = None

    def _apply_scheme(self):
        s = self.scheme
        self.text.configure(bg=s["bg"], fg=s["fg"],
                            selectbackground=s.get("sel", "#264F78"),
                            selectforeground=s["fg"],
                            insertbackground=s.get("cursor", s["fg"]))
        self.gutter.configure(bg=s["bg"], fg=s.get("gutter_fg", "#8A8A8A"),
                              insertbackground=s["bg"])
        self._gutter_tags()
        self._gutter_cache = None
        self.buffer.set_scheme(s)

    def _gutter_tags(self):
        s = self.scheme
        num = s.get("gutter_num") or s.get("cursor") or "#4EC9B0"
        active = s.get("gutter_active") or "#C586C0"
        self.gutter.tag_configure("gutter_time",
                                  foreground=s.get("gutter_fg", "#8A8A8A"))
        self.gutter.tag_configure("gutter_num", foreground=num)
        self.gutter.tag_configure("gutter_active", foreground=active)
        try:
            self.gutter.tag_raise("gutter_active")
        except Exception:
            pass

    def _reset_text_grid(self):
        rows = self.buffer.rows
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", "\n" * (rows - 1))
        self.text.configure(state="disabled")
        self._sigs = [None] * rows
        self._tags.clear()
        for name in list(self.text.tag_names()):
            if name.startswith("termx_"):
                self.text.tag_delete(name)
        self._reset_gutter_grid(rows)
        self.full_redraw = True

    def _bind_events(self):
        t = self.text
        t.bind("<KeyPress>", self._on_key, add="+")
        t.bind("<ButtonPress-1>", self._on_button_down, add="+")
        t.bind("<ButtonRelease-1>", self._on_button_up, add="+")
        t.bind("<Button-3>", self._on_right_click, add="+")
        t.bind("<MouseWheel>", self._on_wheel, add="+")
        t.bind("<Button-4>", self._on_wheel, add="+")
        t.bind("<Button-5>", self._on_wheel, add="+")
        # v1.4.10：鼠标停在右侧滚动条上滚轮，同样要能回滚查看历史
        self.scroll.bind("<MouseWheel>", self._on_scroll_wheel, add="+")
        self.scroll.bind("<Button-4>", self._on_scroll_wheel, add="+")
        self.scroll.bind("<Button-5>", self._on_scroll_wheel, add="+")
        # 让滚轮在整块终端（含左侧行号栏）上方都生效：停在行号栏时也能回滚，
        # 否则光标落在行号栏上滚轮会被吞掉、表现为“滚轮无效”。
        # 注意：滚轮落在主文本区时由下方 text 绑定先处理并返回 break，不会重复触发。
        self.bind("<MouseWheel>", self._on_wheel, add="+")
        self.bind("<Button-4>", self._on_wheel, add="+")
        self.bind("<Button-5>", self._on_wheel, add="+")
        t.bind("<Configure>", self._on_configure, add="+")
        t.bind("<FocusIn>", lambda e: self._set_cursor_tag(), add="+")
        t.bind("<<Selection>>", self._on_selection, add="+")

    # ------------------------------------------------------------------ 尺寸
    def _measure(self):
        try:
            w = max(1, self.text.winfo_width() - 8)
            h = max(1, self.text.winfo_height() - 4)
            cols = max(20, w // self._char_w)
            rows = max(5, h // self._char_h)
        except Exception:
            return
        if (cols, rows) != self._last_size:
            self._last_size = (cols, rows)
            self.buffer.resize(cols, rows)
            self._reset_text_grid()
            self.refresh(True)
            if self.on_resize:
                try:
                    self.on_resize(cols, rows)
                except Exception:
                    pass

    def _on_configure(self, event):
        if self._resize_job:
            try:
                self.after_cancel(self._resize_job)
            except Exception:
                pass
        self._resize_job = self.after(160, self._measure)

    def apply_settings(self):
        self._build_fonts()
        self.text.configure(font=self.font_normal)
        self.gutter.configure(font=self.font_normal)
        self._gutter_cache = None
        self._apply_gutter_visibility()
        self.scheme = self.cfg.scheme()
        self._apply_scheme()
        self.buffer.set_encoding(self.cfg.get("encoding", "UTF-8"))
        self.buffer.scrollback_max = int(self.cfg.get("scrollback", 5000))
        self.buffer.trim_scrollback()
        self._last_size = None
        self._reset_text_grid()
        self._measure()

    # ------------------------------------------------------------------ 渲染
    def feed(self, data):
        self.buffer.feed(data)
        self.schedule_refresh()

    def schedule_refresh(self):
        if self._pending:
            return
        self._pending = True
        try:
            self.after_idle(self._do_refresh)
        except Exception:
            self._pending = False

    def _do_refresh(self):
        self._pending = False
        try:
            self.refresh()
        except Exception:
            pass

    def _fg_of(self, cell):
        s = self.scheme
        fg = cell.fg or s["fg"]
        bg = cell.bg or s["bg"]
        if cell.attrs & REVERSE:
            fg, bg = bg, fg
        if cell.attrs & INVISIBLE:
            fg = bg
        if cell.attrs & BOLD and fg in s.get("ansi", [])[:8]:
            idx = s["ansi"].index(fg)
            if idx < 8 and idx + 8 < len(s["ansi"]):
                fg = s["ansi"][idx + 8]
        if (cell.attrs & FAINT) and not (cell.attrs & BOLD):
            # SGR 2 弱化：把前景色往背景色混，模拟“暗显示”，
            # 而不是像旧版那样当粗体处理（BUG-05）
            fg = _dim_color(fg, bg)
        return fg, bg

    def _tag_for(self, cell):
        fg, bg = self._fg_of(cell)
        a = cell.attrs
        key = (fg, bg, bool(a & BOLD), bool(a & ITALIC), bool(a & UNDERLINE),
               bool(a & STRIKE))
        name = self._tags.get(key)
        if name:
            return name
        name = "termx_%d" % (len(self._tags) + 1)
        font = self.font_normal
        if (a & BOLD) and (a & ITALIC):
            font = self.font_bolditalic
        elif a & BOLD:
            font = self.font_bold
        elif a & ITALIC:
            font = self.font_italic
        self.text.tag_configure(name, foreground=fg, background=bg, font=font,
                                underline=1 if (a & UNDERLINE) else 0,
                                overstrike=1 if (a & STRIKE) else 0)
        self._tags[key] = name
        return name

    def _row_sig(self, row):
        parts = []
        last = None
        for c in row:
            cur = (c.fg, c.bg, c.attrs)
            if cur != last:
                parts.append(str(cur))
                last = cur
            parts.append(c.ch or " ")
        return "".join(parts)

    def refresh(self, force=False):
        b = self.buffer
        rows = b.rows
        if len(self._sigs) != rows:
            self._reset_text_grid()
            force = True
        lines = b.visible_lines()
        at_bottom = b.scroll_offset == 0
        full = force or getattr(self, "full_redraw", False) or b.full_redraw
        self.text.configure(state="normal")
        try:
            for y in range(rows):
                row = lines[y] if y < len(lines) else [Cell()] * b.cols
                sig = self._row_sig(row)
                if not full and sig == self._sigs[y]:
                    continue
                self._render_line(y + 1, row)
                self._sigs[y] = sig
            if not full:
                # 自愈校验：逐行确认组件内容与缓冲区一致。任何原因造成的
                # 渲染残留/错位（如光标占位与差异渲染交错）都会在此被纠正，
                # 避免偶发的字符重复显示残留到下一次全量重绘。
                t = self.text
                for y in range(rows):
                    row = lines[y] if y < len(lines) else None
                    if row is None or self._sigs[y] is None:
                        continue
                    expect = "".join(c.ch for c in row).rstrip()
                    actual = t.get("%d.0" % (y + 1), "%d.end" % (y + 1)).rstrip()
                    if actual != expect:
                        self._render_line(y + 1, row)
        finally:
            self.text.configure(state="disabled")
        b.dirty.clear()
        b.full_redraw = False
        self.full_redraw = False
        # 每个字符格都挂了显式前景/背景的样式 tag，创建晚于 sel、优先级更高，
        # 会把选区高亮完全盖住；这里把 sel 提到最高优先级，保证选中有反差色。
        try:
            self.text.tag_raise("sel")
        except Exception:
            pass
        self._set_cursor_tag(at_bottom)
        self._apply_highlights()
        self._update_scrollbar()
        self._render_gutter(b.visible_meta(), b.cy if at_bottom else -1)

    def _render_line(self, lineno, row):
        t = self.text
        start = "%d.0" % lineno
        end = "%d.end" % lineno
        t.delete(start, end)
        chars = []
        offsets = []
        for c in row:
            if c.ch == "":
                offsets.append(len(chars))
                continue
            offsets.append(len(chars))
            chars.append(c.ch or " ")
        text = "".join(chars).rstrip()
        if text:
            t.insert(start, text)
        # 样式段
        seg_start = 0
        prev = None
        for i, c in enumerate(row):
            if c.ch == "":
                continue
            key = self._tag_for(c)
            off = offsets[i]
            if off >= len(text):
                break
            if prev is None:
                prev, seg_start = key, off
            elif key != prev:
                if off > seg_start:
                    t.tag_add(prev, "%d.%d" % (lineno, seg_start),
                              "%d.%d" % (lineno, off))
                prev, seg_start = key, off
        if prev and len(text) > seg_start:
            t.tag_add(prev, "%d.%d" % (lineno, seg_start),
                      "%d.%d" % (lineno, len(text)))

    def _set_cursor_tag(self, at_bottom=True):
        t = self.text
        if t.tag_ranges("termx_cursor"):
            t.tag_delete("termx_cursor")
        if not at_bottom or not self.buffer.cursor_visible or not self._blink_state:
            return
        b = self.buffer
        if b.cy >= len(b.lines):
            return
        y, x = b.cy + 1, b.cx
        row = b.lines[b.cy]
        off = 0
        for c in row[:x]:
            if c.ch != "":
                off += 1
        ch = row[x].ch if x < len(row) else " "
        fg, bg = self._fg_of(row[x]) if x < len(row) else (self.scheme["fg"], self.scheme["bg"])
        style = self.cfg.get("cursor_style", "block")
        if style == "bar":
            t.tag_configure("termx_cursor", background=self.scheme.get("cursor", fg))
        else:
            t.tag_configure("termx_cursor", background=self.scheme.get("cursor", fg),
                            foreground=bg, underline=0 if style == "block" else 1)
        # 渲染完成后 Text 处于 disabled 状态，此时 insert() 会被 Tk 静默忽略；
        # 而 _render_line 会 rstrip 掉行尾空格，导致光标几乎总在“行尾”。
        # 必须在 normal 状态下插入占位空格，否则行尾光标完全不显示。
        prev_state = str(t.cget("state"))
        if prev_state != "normal":
            t.configure(state="normal")
        try:
            start = "%d.%d" % (y, off)
            stop = "%d.%d" % (y, off + 1)
            line = t.get("%d.0" % y, "%d.end" % y)
            if off >= len(line):
                t.insert(start, ch or " ")
            t.tag_add("termx_cursor", start, stop)
            t.see(start)
        except Exception:
            pass
        finally:
            if prev_state != "normal":
                try:
                    t.configure(state="disabled")
                except Exception:
                    pass

    def _blink(self):
        if self.cfg.get("cursor_blink", True):
            self._blink_state = not self._blink_state
            try:
                self._set_cursor_tag(self.buffer.scroll_offset == 0)
            except Exception:
                pass
        try:
            self.after(520, self._blink)
        except Exception:
            pass

    def _apply_highlights(self):
        for name in list(self.text.tag_names()):
            if name.startswith("hl_"):
                self.text.tag_delete(name)
        rules = list(self.highlights)
        if self._find_term:
            rules.append({"text": self._find_term, "fg": "", "bg": "#B8860B"})
        if not rules:
            return
        for i, rule in enumerate(rules):
            word = rule.get("text", "")
            if not word:
                continue
            name = "hl_%d" % i
            self.text.tag_configure(name, background=parse_bg(rule.get("bg")),
                                    foreground=parse_fg(rule.get("fg")))
            pos = "1.0"
            while True:
                try:
                    p = self.text.search(word, pos, stopindex="end", nocase=1)
                except Exception:
                    break
                if not p:
                    break
                self.text.tag_add(name, p, "%s + %dc" % (p, len(word)))
                pos = "%s + 1c" % p

    # ---------------------------------------------------------------- 行号栏
    def _apply_gutter_visibility(self):
        """仅在用户执行过命令（或已有历史）且开启选项时显示行号栏。
        没有命令的空闲会话不显示行号/时间，避免无意义的占位。"""
        want = (self.cfg.get("show_line_gutter", True)
                and (self._gutter_active or len(self.buffer.scrollback) > 0))
        if want == self._gutter_shown:
            return
        if want:
            self.gutter.pack(side="left", fill="y", before=self.text)
        else:
            self.gutter.pack_forget()
        self._gutter_shown = want

    def _reset_gutter_grid(self, rows):
        g = self.gutter
        g.configure(state="normal")
        g.delete("1.0", "end")
        g.insert("1.0", "\n" * max(0, rows - 1))
        g.configure(state="disabled")
        self._gutter_cache = None

    def _render_gutter(self, metas, active_idx=-1):
        """左侧显示 [HH:MM:SS] 与全局行号；当前命令行高亮。

        没有内容的行（meta 为 None）整行留空——不显示行号，也不显示时间，
        对应用户要求“没有命令的时候不需要显示行号和时间”。
        """
        self._apply_gutter_visibility()
        if not self._gutter_shown:
            return
        cache = (tuple(metas), active_idx)
        if cache == getattr(self, "_gutter_cache", None):
            return
        self._gutter_cache = cache
        g = self.gutter
        rows = len(metas)
        lines = []
        for meta in metas:
            seq, stamp = meta if meta else (None, None)
            if seq is None:
                # 这一行没有内容 -> 不显示行号与时间，只保留竖线对齐
                lines.append("%18s\u2502" % "")
            else:
                lines.append("[%-8s] %5d \u2502" % (stamp or "", seq))
        g.configure(state="normal")
        try:
            g.delete("1.0", "end")
            g.insert("1.0", "\n".join(lines))
            for y in range(1, rows + 1):
                meta = metas[y - 1] if y - 1 < len(metas) else None
                seq = meta[0] if meta else None
                if seq is None:
                    # 无内容的行：不加时间/行号配色
                    g.tag_add("gutter_active", "%d.0" % y, "%d.end" % y)
                    continue
                start = "%d.0" % y
                g.tag_add("gutter_time", start, "%d.10" % y)
                g.tag_add("gutter_num", "%d.11" % y, "%d.end" % y)
                if y - 1 == active_idx:
                    g.tag_add("gutter_active", start, "%d.end" % y)
            try:
                g.tag_raise("gutter_active")
            except Exception:
                pass
        finally:
            g.configure(state="disabled")
            self._sync_gutter_view()

    def _sync_gutter_view(self):
        try:
            self.gutter.yview_moveto(float(self.text.yview()[0]))
        except Exception:
            pass

    # ---------------------------------------------------------------- 滚动条
    def _update_scrollbar(self):
        sb = len(self.buffer.scrollback)
        rows = self.buffer.rows
        first, last = scroll_metrics(sb, rows, self.buffer.scroll_offset)
        try:
            self.scroll.set(first, last)
        except Exception:
            pass

    def _scroll_cmd(self, *args):
        sb = len(self.buffer.scrollback)
        rows = self.buffer.rows
        if args and args[0] == "moveto":
            # 必须与 scroll_metrics() 互逆，否则拖动滑块会跳行/滚不动
            self.buffer.scroll_offset = offset_from_frac(args[1], sb, rows)
        elif args and args[0] == "scroll":
            n = int(args[1])
            unit = args[2]
            step = rows if unit == "pages" else 3
            self.buffer.scroll_view(-n * step)
        self.buffer.full_redraw = True
        self.refresh(True)

    def _yscroll(self, first, last):
        # 滚动条位置由 _update_scrollbar 手动控制；这里只把主终端的视图
        # 同步给左侧行号栏，避免 t.see() 造成两边错行。
        self._sync_gutter_view()

    # ------------------------------------------------------------------ 事件
    def _reply(self, data):
        if self.on_input:
            try:
                self.on_input(data, reply=True)
            except Exception:
                pass

    def _bell(self):
        if self.cfg.get("bell_visual", True):
            self.schedule_refresh()
        if self.on_bell:
            try:
                self.on_bell()
            except Exception:
                pass

    def _title(self, title):
        if self.external_title:
            try:
                self.external_title(title)
            except Exception:
                pass

    def _on_selection(self, event=None):
        if self.cfg.get("auto_copy_selection", True):
            self.copy(silent=True)

    def _on_button_down(self, event):
        self.text.focus_set()
        b = self.buffer
        if b.mouse_mode and not (event.state & SHIFT):
            x = event.x // self._char_w
            y = event.y // self._char_h
            if b.mouse_sgr:
                seq = "\x1b[<0;%d;%dM" % (x + 1, y + 1)
            else:
                seq = "\x1b[M" + chr(32) + chr(32 + x + 1) + chr(32 + y + 1)
            self._send(seq.encode("latin-1"))

    def _on_button_up(self, event):
        if self.buffer.mouse_mode and not (event.state & SHIFT):
            x = event.x // self._char_w
            y = event.y // self._char_h
            if self.buffer.mouse_sgr:
                self._send(("\x1b[<0;%d;%dm" % (x + 1, y + 1)).encode())
        if self.cfg.get("auto_copy_selection", True):
            self.copy(silent=True)

    def _on_right_click(self, event):
        if self.on_context:
            try:
                self.on_context(event, self)
            except Exception:
                pass
        return "break"

    def _wheel_delta(self, event):
        """Windows MouseWheel(delta) 与 Linux Button-4/5 统一成行数。"""
        num = getattr(event, "num", None)
        if num == 4:
            return 1
        if num == 5:
            return -1
        d = int(getattr(event, "delta", 0) or 0)
        if not d:
            return 0
        step = max(1, abs(d) // 40)
        return step if d > 0 else -step

    def _on_wheel(self, event):
        # 滚轮向上（delta>0）= 查看更早的输出 = 回滚偏移增大。
        n = self._wheel_delta(event)
        if not n:
            return "break"
        mods = SHIFT | CTRL | ALT
        if (self.buffer.mouse_mode and not (event.state & mods)
                and not bool(self.cfg.get("wheel_scrollback_first", True))):
            btn = 64 if n > 0 else 65
            x, y = event.x // self._char_w, event.y // self._char_h
            self._send(("\x1b[<%d;%d;%dM" % (btn, x + 1, y + 1)).encode(),
                       from_wheel=True)
            return "break"
        self.buffer.scroll_view(n)
        self.buffer.full_redraw = True
        self.refresh(True)
        return "break"

    def _on_scroll_wheel(self, event):
        """鼠标停在右侧滚动条上滚轮：强制回滚，不转发给终端程序。"""
        n = self._wheel_delta(event)
        if n:
            self.buffer.scroll_view(n)
            self.buffer.full_redraw = True
            self.refresh(True)
        return "break"

    def _on_key(self, event):
        k = event.keysym
        state = event.state
        if k in ("Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
                 "Caps_Lock", "Num_Lock", "Super_L", "Super_R"):
            return "break"
        ctrl, shift, alt = bool(state & CTRL), bool(state & SHIFT), bool(state & ALT)
        # F9/F10/F11 是主窗口注册的全局快捷键（主题/脚本/批量下发）。
        # 终端默认吞掉所有按键，这里放行给 bind_all，否则焦点在终端时快捷键失效。
        if k in ("F9", "F10", "F11") and not (ctrl or shift or alt):
            return
        # Ctrl+,（全局选项）/ Ctrl+. 也是主窗口注册的全局快捷键。
        # 终端默认吞掉 Ctrl 组合键，这里放行给 bind_all，否则焦点在终端时失效。
        if ctrl and not (shift or alt) and k in ("comma", "period"):
            return
        # 自定义按键映射：把某个组合键替换成指定字符串
        keymap = self.cfg.get("keymap") or {}
        if keymap:
            name = "-".join((["Control"] if ctrl else []) +
                            (["Alt"] if alt else []) +
                            (["Shift"] if shift else []) + [k])
            if name in keymap:
                val = str(keymap[name]).replace("\\r", "\r").replace(
                    "\\n", "\n").replace("\\t", "\t")
                self._send(val.encode("utf-8", "replace"))
                return "break"
        # 本地快捷键
        if ctrl and shift and k.lower() == "c":
            self.copy(); return "break"
        if ctrl and shift and k.lower() == "v":
            self.paste(); return "break"
        if ctrl and k == "Insert":
            self.copy(); return "break"
        if shift and k == "Insert":
            self.paste(); return "break"
        if ctrl and k in ("plus", "equal", "KP_Add"):
            self.cfg.set("font_size", min(48, int(self.cfg.get("font_size")) + 1)); self.apply_settings(); return "break"
        if ctrl and k in ("minus", "KP_Subtract"):
            self.cfg.set("font_size", max(6, int(self.cfg.get("font_size")) - 1)); self.apply_settings(); return "break"
        if ctrl and k == "Home":
            self.buffer.scroll_offset = len(self.buffer.scrollback); self.refresh(True); return "break"
        if ctrl and k == "End":
            self.buffer.scroll_offset = 0; self.refresh(True); return "break"
        if not ctrl and k in ("Prior", "Next") and not self.buffer.alt_active:
            self.buffer.scroll_view(-10 if k == "Prior" else 10); self.refresh(True); return "break"
        # 组合键
        if ctrl:
            if k == "space":
                self._send(b"\x00"); return "break"
            if k == "Break":
                self._send(b"\x03"); return "break"
            if "a" <= k.lower() <= "z":
                code = ord(k.upper()) - 64
                if alt:
                    self._send(b"\x1b" + bytes([code]))
                else:
                    self._send(bytes([code]))
                return "break"
            if k in ("bracketleft", "bracketright", "backslash", "asciicircum", "underscore"):
                m = {"bracketleft": 27, "bracketright": 29, "backslash": 28,
                     "asciicircum": 30, "underscore": 31}
                self._send(bytes([m[k]])); return "break"
        if alt and not ctrl:
            if k in SPECIAL_KEYS:
                seq = SPECIAL_KEYS[k][0]
                self._send(b"\x1b" + seq.encode()); return "break"
            ch = event.char
            if ch and len(ch) == 1:
                self._send(b"\x1b" + ch.encode("utf-8", "replace")); return "break"
        if k in SPECIAL_KEYS:
            norm, app = SPECIAL_KEYS[k]
            seq = app if self.buffer.app_cursor else norm
            if shift and k in ("Prior", "Next"):
                pass
            if k in ("Up", "Down", "Left", "Right", "Home", "End") and (shift or ctrl) \
                    and seq.startswith("\x1b["):
                mod = 2 if shift else 5
                seq = seq.replace("[", "[1;%d" % mod, 1)
            self._send(seq.encode()); return "break"
        if k in ("Return", "KP_Enter"):
            # 回车 = 命令真正执行：_send 里检测到 \r 会自动调用
            # _mark_command_executed，这里不再重复调用。
            self._send(b"\r"); return "break"
        if k == "BackSpace":
            self._send(b"\x7f" if self.cfg.get("backspace") == "DEL" else b"\x08"); return "break"
        if k == "Tab":
            self._send(b"\x1b[Z" if shift else b"\t"); return "break"
        if k == "Escape":
            self._send(b"\x1b"); return "break"
        if k == "Delete" and ctrl and shift:
            self._send(b"\x1b[3;3~"); return "break"
        ch = event.char
        if ch and len(ch) == 1 and ord(ch) >= 32:
            self._send(ch.encode("utf-8", "replace"))
        return "break"

    def _mark_command_executed(self):
        """用户按下回车：命令这一刻真正执行。

        - 记录执行时刻，命令行（光标所在行）的时间戳刷成这个时刻
          （左侧行号栏显示的“时间”就是执行这一行命令的时间）；
        - 行号栏从此刻起显示——没有执行过命令的空闲会话不显示行号和时间。
        """
        self._gutter_active = True
        try:
            self.buffer.note_command()
        except Exception:
            pass

    def _send(self, data, reply=False, from_wheel=False):
        # 用户一旦开始输入（回车或敲字），立刻把视图从回滚状态拉回底部，
        # 定位到当前光标所在行。否则用户翻到历史后敲命令，画面还停在旧
        # 位置，看不到自己输入的内容。终端自动应答（reply=True）与
        # 「滚轮转发成鼠标报文」不触发，避免打断查看历史。
        if not reply and not from_wheel and self.buffer.scroll_offset:
            try:
                self.scroll_to_bottom()
            except Exception:
                self.buffer.scroll_offset = 0
                self.buffer.full_redraw = True
        # 只有真正执行了命令（数据里带换行）才算“执行过命令”，
        # 这时行号/时间戳栏才显示；仅仅敲字不动回车不显示。
        if b"\r" in data or b"\n" in data:
            self._mark_command_executed()
        if self.on_input:
            try:
                self.on_input(data, reply=reply)
            except Exception:
                pass

    # ------------------------------------------------------------------ 公共操作
    def copy(self, silent=False):
        try:
            sel = self.text.get("sel.first", "sel.last")
        except Exception:
            sel = ""
        if not sel:
            return ""
        self.clipboard_clear()
        self.clipboard_append(sel)
        return sel

    def paste(self, text=None):
        if text is None:
            try:
                text = self.clipboard_get()
            except Exception:
                return
        if not text:
            return
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if self.cfg.get("confirm_paste_multiline", True) and "\n" in text and self.confirm_paste:
            if not self.confirm_paste(text):
                return
        data = text.encode("utf-8", "replace")
        if self.buffer.bracketed_paste and self.cfg.get("bracketed_paste", True):
            data = b"\x1b[200~" + data.replace(b"\n", b"\r") + b"\x1b[201~"
        else:
            data = data.replace(b"\n", b"\r")
        self._send(data)

    def select_all(self):
        self.text.tag_add("sel", "1.0", "end")
        try:
            self.text.tag_raise("sel")
        except Exception:
            pass

    def get_selected_text(self):
        return self.copy(silent=True)

    def clear_screen(self):
        # 清屏同时清掉行号/时间戳元数据，否则清屏后左侧仍留着上一屏的
        # 行号与时间（用户看到的"没有命令却显示行号和时间"）。
        try:
            self.buffer.clear_screen()
        except Exception:
            self.buffer.lines = [[Cell() for _ in range(self.buffer.cols)]
                                 for _ in range(self.buffer.rows)]
            self.buffer.cx = self.buffer.cy = 0
        self.buffer.full_redraw = True
        self.refresh(True)

    def scroll_to_bottom(self):
        self.buffer.scroll_offset = 0
        self.buffer.full_redraw = True
        self.refresh(True)

    def set_find(self, term):
        self._find_term = term
        self._apply_highlights()
        return self.find_next(term)

    def find_next(self, term=None):
        term = term or self._find_term
        if not term:
            return False
        pos = self.text.search(term, self._find_pos, stopindex="end", nocase=1)
        if not pos:
            self._find_pos = "1.0"
            pos = self.text.search(term, "1.0", stopindex="end", nocase=1)
        if pos:
            self.text.tag_remove("sel", "1.0", "end")
            end = "%s + %dc" % (pos, len(term))
            self.text.tag_add("sel", pos, end)
            self.text.see(pos)
            self._find_pos = end
            return True
        return False

    def set_highlights(self, rules):
        self.highlights = rules or []
        self._apply_highlights()

    def set_encoding(self, enc):
        self.buffer.set_encoding(enc)

    def screen_text(self, row1=1, col1=1, row2=None, col2=None):
        b = self.buffer
        row2 = row2 or b.rows
        col2 = col2 or b.cols
        return b.region_text(row1, col1, row2, col2)


def parse_bg(value):
    from .config import parse_color
    return parse_color(value) or "#B8860B"


def parse_fg(value):
    from .config import parse_color
    return parse_color(value) or "#000000"
