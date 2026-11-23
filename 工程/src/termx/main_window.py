# -*- coding: utf-8 -*-
"""主窗口：会话树、多标签、菜单/工具栏/状态栏，以及脚本引擎的控制接口。"""
import json
import os
import queue
import re
import subprocess
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from tkinter import font as tkfont

from . import bundle, ui_theme
from . import sessions as _sessions
from .config import (APP_NAME, APP_TITLE, APP_VER, APP_BUILD, SCHEMES, Config,
                     path_of, data_dir, needs_setup as _cfg_needs_setup,
                     ensure_data_dir as _cfg_ensure_data_dir)
from .connections import create_connection, ssh_executable, quote_cmdline
from .dialogs import (AboutDialog, AccountManagerDialog, AccountSelectDialog,
                      BatchWindow, CustomizeDialog, DataDirDialog, FindDialog,
                      OptionsDialog, QuickConnectDialog, ScriptWindow,
                      SessionDialog, ThemeDialog, TransferDialog)
from .sessions import SessionStore, get_password, set_password, new_session
from .scripting import Capture, Engine, LoginScriptRunner, AutoLoginRunner
from .terminal import TerminalView

MENU_FONT = ("Microsoft YaHei UI", 9)


class SessionConfigProxy(object):
    """会话级外观覆盖：优先用会话设置，缺省回落到全局。"""
    OVERRIDES = {"scheme": "scheme", "font_family": "font_family",
                 "font_size": "font_size", "encoding": "encoding",
                 "scrollback": "scrollback", "cursor_style": "cursor_style",
                 "cursor_blink": "cursor_blink", "backspace": "backspace",
                 "auto_copy_selection": "auto_copy_selection",
                 "bracketed_paste": "bracketed_paste",
                 "confirm_paste_multiline": "confirm_paste_multiline",
                 "bell_visual": "bell_visual"}

    # 会话级数值项中 0 的含义是“跟随全局”（见 会话编辑 → 回滚行数(0=全局)），
    # 不能当成显式覆盖。否则 scrollback=0 会让回滚缓冲上限为 0，
    # trim_scrollback() 会把每一行历史都丢掉，滚动条与滚轮无内容可滚。
    ZERO_MEANS_GLOBAL = ("font_size", "scrollback")

    def __init__(self, cfg, session):
        self.cfg = cfg
        self.session = session

    def get(self, key, default=None):
        skey = self.OVERRIDES.get(key)
        if skey:
            val = self.session.get(skey, "")
            if val not in ("", None):
                # 布尔项的 False 是合法覆盖，不能按数值 0 处理
                if (key in self.ZERO_MEANS_GLOBAL and not isinstance(val, bool)
                        and val == 0):
                    val = None
                else:
                    return val
        return self.cfg.get(key, default)

    def set(self, key, value, save=True):
        return self.cfg.set(key, value, save)

    def scheme(self):
        name = self.session.get("scheme") or self.cfg.get("scheme")
        return SCHEMES.get(name, SCHEMES["Dark+"])

    def font(self):
        return (self.get("font_family"), self.get("font_size"))

    @property
    def values(self):
        return self.cfg.values

    def save(self):
        self.cfg.save()


class TerminalTab(object):
    """一个终端标签页：连接 + 显示 + 脚本捕获。"""
    def __init__(self, controller, session, frame):
        self.controller = controller
        self.root = controller.root
        self.session = dict(session)
        self.frame = frame
        self.name = session.get("name", "会话")
        self.cols = int(session.get("cols") or controller.cfg.get("cols") or 100)
        self.rows = int(session.get("rows") or controller.cfg.get("rows") or 30)
        self.conn = None
        self.connected = False
        self.user_closed = False
        self.locked = False
        self.log_path = ""
        self._queue = queue.Queue()
        self._captures = []
        self._active_capture = None
        self._default_capture = Capture(self.session.get("encoding") or controller.cfg.get("encoding"))
        self._capt_lock = threading.Lock()
        self._retries = 0
        self._start_at = time.time()
        self._last_anti_idle = time.time()
        self._login_runner = None
        self.zmodem = None                 # 内置 Zmodem 传输实例（按需创建）
        self.recording = False
        self.recorded = []
        self.encoding = self.session.get("encoding") or controller.cfg.get("encoding") or "UTF-8"
        self.proxy = SessionConfigProxy(controller.cfg, self.session)
        self._build_ui()
        self.view.after(15, self._pump)
        self.view.after(1000, self._tick)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        sends = self.controller.cfg.get("send_strings") or []
        if sends:
            bar = ttk.Frame(self.frame)
            bar.pack(fill="x")
            for item in sends[:12]:
                ttk.Button(bar, text=item.get("name", "发送"), width=12,
                           command=lambda t=item.get("text", ""): self.send_text(t)).pack(
                               side="left", padx=2, pady=2)
        self.view = TerminalView(
            self.frame, self.proxy,
            on_input=self.on_input, on_resize=self.on_resize,
            on_context=lambda ev, v: self.controller.show_tab_menu(ev, self),
            on_title=self.on_title, on_bell=self.on_bell,
            confirm_paste=lambda text: messagebox.askyesno(
                "多行粘贴确认", "即将粘贴 %d 行内容到会话 [%s]，确认发送？" %
                (text.count("\n") + 1, self.name), parent=self.root))
        self.view.pack(fill="both", expand=True)
        if self.session.get("scheme"):
            self.view.scheme = self.proxy.scheme()
            self.view._apply_scheme()
        self.view.buffer.set_encoding(self.encoding)
        self.view.set_highlights(self.controller.cfg.get("key_highlights") or [])

    # ------------------------------------------------------------------ 连接
    def start(self):
        self.user_closed = False
        self._start_at = time.time()
        self._queue.put(("state", "正在连接 ..."))
        threading.Thread(target=self._start_thread, daemon=True,
                         name="connect-" + self.name).start()

    def _start_thread(self):
        try:
            conn, info = create_connection(self.session, self.controller.cfg, {
                "on_data": lambda d: self._queue.put(("data", d)),
                "on_state": lambda s: self._queue.put(("state", s)),
                "on_error": lambda s: self._queue.put(("error", s)),
                "on_close": lambda: self._queue.put(("closed", None)),
            })
            self.conn = conn
            self.log_path = getattr(conn, "log_path", "")
            # 密码解密/降级保护有异常时明确告知（BUG-11），不再静默变成空密码
            if _sessions.DPAPI_WARN:
                self._queue.put(("error", "密码提示：" + _sessions.DPAPI_WARN))
                _sessions.DPAPI_WARN = ""
            ok = conn.start()
            if not ok:
                self._queue.put(("closed", None))
                return
            self._queue.put(("started", info))
        except Exception as e:
            self._queue.put(("error", "连接初始化失败: %s" % e))
            self._queue.put(("closed", None))

    def _pump(self):
        try:
            n = 0
            while n < 300:
                try:
                    kind, payload = self._queue.get_nowait()
                except queue.Empty:
                    break
                n += 1
                self._handle(kind, payload)
        except Exception:
            pass
        try:
            self.view.after(15, self._pump)
        except Exception:
            pass

    def _handle(self, kind, payload):
        if kind == "data":
            raw = payload
            payload = self._zmodem_filter(payload)
            if b"*\x18B00" in raw or b"**\x18B0" in raw:
                self.controller.on_zmodem(self)
            if not payload:
                return
            with self._capt_lock:
                caps = list(self._captures) + [self._default_capture]
            for c in caps:
                try:
                    c.push(payload)
                except Exception:
                    pass
            try:
                self.view.feed(payload)
            except Exception:
                pass
        elif kind == "state":
            self.controller.set_status(payload)
        elif kind == "error":
            text = ("\r\n\x1b[1;31m[TermX] %s\x1b[0m\r\n" % payload).encode("utf-8")
            self.view.feed(text)
            self.controller.set_status("错误: " + str(payload)[:80])
        elif kind == "started":
            self.connected = True
            self._retries = 0
            self.controller.set_status("已连接: %s · %s" % (self.name, payload))
            self.controller.refresh_tab_title(self)
            self.on_resize(self.view._last_size[0] if self.view._last_size else self.cols,
                           self.view._last_size[1] if self.view._last_size else self.rows)
            self._after_connect()
        elif kind == "closed":
            was = self.connected
            self.connected = False
            self.view.feed("\r\n\x1b[33m[连接已断开]\x1b[0m\r\n".encode("utf-8"))
            self.controller.refresh_tab_title(self)
            self.controller.set_status("已断开: " + self.name)
            if self._login_runner:
                self._login_runner.stop()
            al = getattr(self, "_auto_login", None)
            if al:
                al.stop()
            if was and not self.user_closed:
                self._maybe_reconnect()

    # ------------------------------------------------------------------ 内置 Zmodem
    def _zmodem_write(self, data):
        if self.conn:
            self.conn.write(data)

    def _new_zmodem(self):
        from . import zmodem
        d = path_of("downloads")
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass

        def status(text):
            self.controller.ui_call(self.controller.set_status, str(text)[:160])

        return zmodem.Transfer(
            send_bytes=self._zmodem_write, directory=d,
            on_progress=lambda n, done, total: status(
                "Zmodem %s  %d/%d 字节" % (n, done, total)),
            on_log=status)

    def _zmodem_filter(self, data):
        """把 Zmodem 协议字节从终端数据流里剥离并交给内置协议栈。"""
        try:
            from . import zmodem
            if self.zmodem is None:
                if not zmodem.looks_like_zmodem(data):
                    return data
                self.zmodem = self._new_zmodem()
            n = self.zmodem.feed(data)
            if n:
                if not self.zmodem.active:
                    self.zmodem = None
                return data[n:]
            return data
        except Exception as e:
            self.controller.set_status("Zmodem: %s" % e)
            self.zmodem = None
            return data

    def start_zmodem_upload(self, paths):
        """主动发起 Zmodem 上传（对接远端 rz）。"""
        try:
            from . import zmodem
            if self.zmodem is None:
                self.zmodem = self._new_zmodem()
            threading.Thread(target=self.zmodem.start_upload,
                             args=(list(paths),), daemon=True,
                             name="zmodem-upload").start()
            self.controller.set_status("Zmodem 上传已启动 · %d 个文件" % len(paths))
        except Exception as e:
            self.controller.set_status("Zmodem 上传失败: %s" % e)

    def _after_connect(self):
        cfg = self.controller.cfg
        if self.conn and self.conn.auto_command:
            def send_auto():
                if self.connected:
                    self.send_text(self.conn.auto_command + "\r")
            self.view.after(700, send_auto)
        if self.session.get("login_script"):
            self._login_runner = LoginScriptRunner(
                self, self.session["login_script"],
                log=lambda s: self.controller.ui_call(self.controller.set_status, s))
            self._login_runner.start()
        else:
            # 免重复登录：会话保存了凭据时，自动应答常见的登录提示
            rules = self._auto_login_rules()
            if rules:
                self._auto_login = AutoLoginRunner(self, rules)
                self._auto_login.start()

    _AUTO_LOGIN_PROMPTS = (
        ("login:", "{{USERNAME}}"), ("Login:", "{{USERNAME}}"),
        ("username:", "{{USERNAME}}"), ("Username:", "{{USERNAME}}"),
        ("用户名:", "{{USERNAME}}"),
        ("password:", "{{PASSWORD}}"), ("Password:", "{{PASSWORD}}"),
        ("密码:", "{{PASSWORD}}"),
        ("passphrase", "{{PASSWORD}}"), ("PASSCODE", "{{PASSWORD}}"),
    )

    def _auto_login_rules(self):
        """根据会话保存的用户名/密码生成自动应答规则。

        用户名和密码都实时取自账号库（若会话绑定了账号），所以在
        「工具 → 账号管理」里改完密码，所有绑定会话立即生效。
        """
        try:
            from .sessions import get_password, get_username
            pwd = get_password(self.session) or ""
            user = get_username(self.session) or ""
        except Exception:
            pwd = ""
            user = self.session.get("username") or ""
        if not user and not pwd:
            return []
        rules = []
        for prompt, tpl in self._AUTO_LOGIN_PROMPTS:
            val = tpl.replace("{{USERNAME}}", user).replace("{{PASSWORD}}", pwd)
            if not val:
                continue
            if tpl == "{{USERNAME}}" and not user:
                continue
            if tpl == "{{PASSWORD}}" and not pwd:
                continue
            rules.append((prompt, val + "\r"))
        return rules
        script = self.session.get("script") or ""
        if script and os.path.isfile(script):
            try:
                code = open(script, "r", encoding="utf-8", errors="replace").read()
                lang = "python" if script.endswith(".py") else \
                       "jscript" if script.endswith(".js") else \
                       "vbscript" if script.endswith(".vbs") else "commands"
                self.view.after(900, lambda: self.controller.engine.run(
                    code, script, self, lang, on_log=self.controller.script_log))
            except Exception:
                pass
        self.controller.store.touch(self.session.get("id", ""))

    def _maybe_reconnect(self):
        en = bool(self.session.get("auto_reconnect", self.controller.cfg.get("auto_reconnect")))
        times = int(self.session.get("auto_reconnect_times") or
                    self.controller.cfg.get("auto_reconnect_times") or 3)
        delay = int(self.session.get("auto_reconnect_delay") or
                    self.controller.cfg.get("auto_reconnect_delay") or 5)
        if not en or self._retries >= times:
            return
        self._retries += 1
        self.controller.set_status("第 %d/%d 次自动重连 %s (%d 秒后)" %
                                   (self._retries, times, self.name, delay))
        self.view.after(delay * 1000, lambda: self.start() if not self.user_closed else None)

    def _tick(self):
        try:
            if self.connected and self.conn:
                interval = int(self.conn.anti_idle_interval or 60)
                if self.conn.anti_idle and time.time() - self._last_anti_idle > interval:
                    self._last_anti_idle = time.time()
                    self.conn.send_anti_idle()
                self.controller.update_duration(self)
        except Exception:
            pass
        try:
            self.view.after(1000, self._tick)
        except Exception:
            pass

    # ------------------------------------------------------------------ 输入/输出
    def on_input(self, data, reply=False):
        if self.locked and not reply:
            return
        if not reply:
            self._last_anti_idle = time.time()
            if self.recording:
                try:
                    txt = data.decode("utf-8", "replace")
                    if txt.strip():
                        self.recorded.append(txt)
                except Exception:
                    pass
            self.controller.broadcast(self, data)
        if self.conn and self.connected:
            self.conn.write(data)
        if self.controller.cfg.get("local_echo") and not reply:
            try:
                self.view.feed(data)
            except Exception:
                pass

    def send_text(self, text):
        if not self.conn or not self.connected:
            return
        if isinstance(text, bytes):
            self.conn.write(text)
        else:
            self.conn.write(str(text).encode(self.encoding, "replace"))

    def send_bytes(self, data):
        self.send_text(data)

    def on_resize(self, cols, rows):
        self.cols, self.rows = cols, rows
        if self.conn and self.connected:
            try:
                self.conn.resize(cols, rows)
            except Exception:
                pass

    def on_title(self, title):
        self.controller.set_tab_hint(self, title)

    def on_bell(self):
        try:
            self.root.bell()
        except Exception:
            pass

    # ------------------------------------------------------------------ 脚本接口
    def is_connected(self):
        return bool(self.connected and self.conn and self.conn.alive)

    def start_capture(self):
        cap = Capture(self.encoding)
        with self._capt_lock:
            self._captures.append(cap)
            self._active_capture = cap
        return cap

    def stop_capture(self, cap):
        with self._capt_lock:
            if cap in self._captures:
                self._captures.remove(cap)
            if self._active_capture is cap:
                self._active_capture = None
        cap.close()

    def wait_for(self, patterns, timeout=30, stop=None, regex=False, since=None):
        with self._capt_lock:
            cap = self._active_capture or self._default_capture
        return cap.wait_for(patterns, timeout, since, regex, stop)

    def wait_quiet(self, seconds=0.5, stop=None):
        """等待输出静止一段时间（用于判断登录/回显流程是否结束）。"""
        with self._capt_lock:
            cap = self._active_capture or self._default_capture
        return cap.wait_quiet(seconds, stop=stop)

    def screen_text(self, row1=1, col1=1, row2=None, col2=None):
        return self.view.screen_text(row1, col1, row2, col2)

    def clear_screen(self):
        self.view.clear_screen()

    def set_caption(self, text):
        self.name = text
        self.controller.refresh_tab_title(self)

    def toggle_log(self, enable=True, path=""):
        self.controller.toggle_log(self, enable, path)

    # ------------------------------------------------------------------ 生命周期
    def disconnect(self):
        self.user_closed = True
        if self._login_runner:
            self._login_runner.stop()
        al = getattr(self, "_auto_login", None)
        if al:
            al.stop()
        conn = self.conn
        if conn:
            try:
                conn.stop()
            except Exception:
                pass
        self.connected = False
        self.controller.refresh_tab_title(self)

    def reconnect(self):
        self.user_closed = False
        if self.conn:
            try:
                self.conn.stop()
            except Exception:
                pass
        self.view.after(300, self.start)

    def destroy(self):
        self.user_closed = True
        self.disconnect()

    def export_record(self, path):
        with open(path, "w", encoding="utf-8") as f:
            f.write("# TermX 录制脚本\n")
            for t in self.recorded:
                f.write(t.replace("\r", "\\r").replace("\n", "\\n") + "\n")


class MainWindow(object):
    def __init__(self, root, cfg=None, store=None):
        self.root = root
        self.cfg = cfg or Config()
        self.store = store or SessionStore()
        # 会话库写盘失败必须立刻告知用户（BUG-04）：静默失败会让新建/修改
        # 的会话在下次启动时凭空消失。
        self.store.on_save_error = self._on_session_save_error
        self.tabs_list = []
        self._tab_closes = {}
        self._tab_text_budget = None      # 标签标题可用像素宽（None=不截断）
        self._tab_font = None
        self.engine = Engine(self)
        self._ui_jobs = queue.Queue()
        self._find_dialog = None
        self._batch_win = None
        self._script_win = None
        self._broadcast = False
        self._closing = False
        self._migrate_theme()
        self._build_style()
        self._build_menu()
        self._build_toolbar()
        self._build_body()
        self._build_status()
        self._bind_shortcuts()
        self._apply_window_options()
        self.refresh_tree()
        self._sync_welcome()
        self.root.after(20, self._pump_ui)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.set_status("就绪 · 双击左侧会话连接，或按 Ctrl+T 快速连接")

    # ------------------------------------------------------------------ 外观
    def _migrate_theme(self):
        """1.2.0 起默认使用 SecureCRT 经典外观；只迁移一次，之后尊重用户选择。"""
        try:
            rev = int(self.cfg.get("ui_rev", 0) or 0)
        except Exception:
            rev = 0
        if rev >= 2:
            return
        self.cfg.set("ui_rev", 2)
        self.cfg.set("theme_ui", "SecureCRT 经典")
        if self.cfg.get("scheme") == "Dark+":
            self.cfg.set("scheme", "SecureCRT 经典")

    def _build_style(self):
        self.style = ttk.Style()
        self.ui_theme_key = ui_theme.normalize(self.cfg.get("theme_ui"))
        self.ui = ui_theme.apply(self.root, self.style, self.ui_theme_key)
        self.ui_bg, self.ui_fg = self.ui["bg"], self.ui["fg"]
        if not hasattr(self, "icons"):
            self.icons = ui_theme.make_icons(self.root)

    def set_ui_theme(self, key):
        """切换整套皮肤：外壳配色 + 终端 16 色 + 光标样式，即时生效并持久化。"""
        key = ui_theme.normalize(key)
        theme = ui_theme.THEMES.get(key, ui_theme.CRT)
        self.cfg.set("theme_ui", ui_theme.display_name(key), save=False)
        if self.cfg.get("theme_sync_scheme", True):
            self.cfg.set("scheme", theme.get("scheme", "SecureCRT 经典"), save=False)
            self.cfg.set("cursor_style", theme.get("cursor_style", "block"), save=False)
        self.cfg.save()
        self.apply_settings()

    def show_theme_picker(self):
        """打开卡片式主题选择器（F9）。"""
        dlg = getattr(self, "_theme_dialog", None)
        if dlg is not None:
            try:
                if dlg.winfo_exists():
                    dlg.lift()
                    dlg.focus_force()
                    return
            except Exception:
                pass
        self._theme_dialog = ThemeDialog(
            self.root, self.cfg, self.ui_theme_key, self.set_ui_theme,
            on_close=lambda: setattr(self, "_theme_dialog", None))

    def _recolor_chrome(self):
        """主题切换后重绘自建的 tk 控件（ttk 控件由 style 自动更新）。"""
        c = self.ui
        plain = [
            (getattr(self, "toolbar", None), c["toolbar"]),
            (getattr(self, "left_hdr", None), c["panel_hdr"]),
            (getattr(self, "search_bar", None), c["bg"]),
            (getattr(self, "left_foot", None), c["bg"]),
            (getattr(self, "statusbar", None), c["status"]),
            (getattr(self, "status_line", None), c["border"]),
            (getattr(self, "search_icon", None), c["entry_bg"]),
            (getattr(self, "search_clear", None), c["bg"]),
        ]
        for w, col in plain:
            if w is not None:
                try:
                    w.configure(bg=col)
                except Exception:
                    pass
        for w in (getattr(self, "left_hdr_lbl", None),
                  getattr(self, "left_hdr_close", None)):
            if w is not None:
                try:
                    w.configure(bg=c["panel_hdr"], fg=c["hdr_fg"])
                except Exception:
                    pass
        for b in getattr(self, "_tool_buttons", []):
            try:
                b.configure(bg=c["toolbar"], fg=c["fg"],
                            activebackground=c["hover"], activeforeground=c["fg"])
            except Exception:
                pass
        for sep in getattr(self, "_tool_seps", []):
            try:
                sep.configure(bg=c["border"])
            except Exception:
                pass
        for item in getattr(self, "_seg_frames", []):
            f, l, plain = item
            back = c["status"] if plain else c["status_seg"]
            try:
                f.configure(bg=back)
                l.configure(bg=back, fg=c["fg"])
            except Exception:
                pass
        for w in getattr(self, "_welcome_widgets", []):
            try:
                w.configure(bg=c["panel"], fg=c["fg"])
            except Exception:
                try:
                    w.configure(bg=c["panel"])
                except Exception:
                    pass
        for b in getattr(self, "_foot_buttons", []):
            try:
                b.configure(bg=c["toolbar"], fg=c["fg"], activebackground=c["hover"])
            except Exception:
                pass
        e = getattr(self, "tree_entry", None)
        if e is not None:
            try:
                shown = e.get() == ui_theme.PLACEHOLDER
                e.configure(foreground=c["muted"] if shown else c["fg"])
            except Exception:
                pass
        # 主题切换后重排/重着色标签关闭按钮
        try:
            self._layout_tab_closes()
        except Exception:
            pass

    def _build_menu(self):
        r = self.root
        menubar = tk.Menu(r, tearoff=0, font=MENU_FONT)
        r.configure(menu=menubar)
        m = tk.Menu(menubar, tearoff=0, font=MENU_FONT)
        menubar.add_cascade(label="文件", menu=m)
        m.add_command(label="新建会话...", accelerator="Ctrl+N", command=self.new_session)
        m.add_command(label="快速连接...", accelerator="Ctrl+T", command=self.quick_connect)
        m.add_separator()
        m.add_command(label="导入会话 (JSON)", command=self.import_sessions)
        m.add_command(label="导入 SecureCRT 会话 (.ini)", command=self.import_crt)
        m.add_command(label="导出会话 (JSON)", command=lambda: self.export_sessions("json"))
        m.add_command(label="导出会话 (CSV)", command=lambda: self.export_sessions("csv"))
        m.add_separator()
        m.add_command(label="保存屏幕文本...", command=self.save_screen_text)
        m.add_command(label="导出录制脚本...", command=self.export_record)
        m.add_separator()
        m.add_command(label="退出", accelerator="Alt+F4", command=self.on_close)

        m = tk.Menu(menubar, tearoff=0, font=MENU_FONT)
        menubar.add_cascade(label="编辑", menu=m)
        m.add_command(label="复制", accelerator="Ctrl+Shift+C", command=lambda: self.call_active("copy"))
        m.add_command(label="粘贴", accelerator="Ctrl+Shift+V", command=lambda: self.call_active("paste"))
        m.add_command(label="全选", command=lambda: self.call_active("select_all"))
        m.add_separator()
        m.add_command(label="查找...", accelerator="Ctrl+F", command=self.show_find)
        m.add_command(label="清屏", accelerator="Ctrl+L", command=lambda: self.call_active("clear_screen"))
        m.add_command(label="滚到底部", command=lambda: self.call_active("scroll_to_bottom"))

        m = tk.Menu(menubar, tearoff=0, font=MENU_FONT)
        menubar.add_cascade(label="查看", menu=m)
        self.var_tree = tk.BooleanVar(value=bool(self.cfg.get("show_session_tree")))
        self.var_toolbar = tk.BooleanVar(value=bool(self.cfg.get("show_toolbar")))
        self.var_statusbar = tk.BooleanVar(value=bool(self.cfg.get("show_statusbar")))
        m.add_checkbutton(label="会话管理器", variable=self.var_tree, command=self.toggle_tree)
        m.add_checkbutton(label="工具栏", variable=self.var_toolbar, command=self.toggle_toolbar)
        m.add_checkbutton(label="状态栏", variable=self.var_statusbar, command=self.toggle_status)
        m.add_separator()
        m.add_command(label="切换主题...", accelerator="F9", command=self.show_theme_picker)
        self.var_ui_theme = tk.StringVar(value=self.ui_theme_key)
        sub = tk.Menu(m, tearoff=0, font=MENU_FONT)
        for key, label in ui_theme.THEME_LABELS:
            sub.add_radiobutton(label=label, variable=self.var_ui_theme, value=key,
                                command=lambda k=key: self.set_ui_theme(k))
        m.add_cascade(label="界面主题", menu=sub)
        m.add_separator()
        m.add_command(label="全局选项...", accelerator="Ctrl+,", command=self.show_options)
        m.add_command(label="会话属性...", command=self.edit_active_session)

        m = tk.Menu(menubar, tearoff=0, font=MENU_FONT)
        menubar.add_cascade(label="连接", menu=m)
        m.add_command(label="断开当前会话", command=self.disconnect_active)
        m.add_command(label="重新连接", command=lambda: self.active_tab() and self.active_tab().reconnect())
        m.add_command(label="克隆会话", command=self.clone_active)
        m.add_command(label="关闭标签", accelerator="Ctrl+W", command=self.close_active_tab)
        m.add_separator()
        m.add_command(label="锁定/解锁会话", command=self.toggle_lock)
        m.add_command(label="重命名标签...", command=self.rename_tab)
        m.add_separator()
        m.add_command(label="打开 SFTP 标签", command=self.open_sftp_active)
        m.add_command(label="文件传输 / 端口转发...", command=self.show_transfer)

        m = tk.Menu(menubar, tearoff=0, font=MENU_FONT)
        menubar.add_cascade(label="脚本", menu=m)
        m.add_command(label="批量下发 / 聊天窗口", accelerator="F11", command=self.show_batch)
        m.add_command(label="脚本编辑器...", accelerator="F10", command=self.show_script)
        m.add_separator()
        self.var_broadcast = tk.BooleanVar(value=False)
        m.add_checkbutton(label="广播输入到所有会话", variable=self.var_broadcast,
                          command=self.toggle_broadcast)
        self.var_record = tk.BooleanVar(value=False)
        m.add_checkbutton(label="录制当前会话操作", variable=self.var_record,
                          command=self.toggle_record)
        m.add_separator()
        m.add_command(label="运行脚本文件...", command=self.run_script_file)

        m = tk.Menu(menubar, tearoff=0, font=MENU_FONT)
        menubar.add_cascade(label="工具", menu=m)
        m.add_command(label="自定义 高亮/按钮栏/宏/键位...", command=self.show_customize)
        m.add_command(label="账号管理...", command=self.manage_accounts)
        m.add_command(label="选择账号登录...", command=self.login_with_account)
        m.add_command(label="内置组件状态 / 零依赖检查...",
                      command=self.show_bundle_status)
        m.add_separator()
        m.add_command(label="生成 SSH 密钥...", command=self.generate_key)
        m.add_command(label="打开数据目录", command=self.open_data_dir)
        m.add_command(label="打开日志目录", command=self.open_log_dir)
        m.add_command(label="重置窗口布局", command=self._apply_window_options)

        m = tk.Menu(menubar, tearoff=0, font=MENU_FONT)
        menubar.add_cascade(label="帮助", menu=m)
        m.add_command(label="快捷键与功能清单", command=self.show_about)
        m.add_command(label="关于 TermX", command=self.show_about)

    def _build_toolbar(self):
        """SecureCRT 风格顶部图标工具栏：分组、扁平按钮、悬浮提示。"""
        c = self.ui
        self.toolbar = tk.Frame(self.root, bg=c["toolbar"])
        self.toolbar.pack(fill="x", side="top")
        self._tool_buttons, self._tool_seps = [], []
        groups = [
            [("new", "新建会话", self.new_session, "新建并保存会话 (Ctrl+N)"),
             ("quick", "快速连接", self.quick_connect, "不保存直接连接 (Ctrl+T)"),
             ("connect", "连接", lambda: self._tree_double(None), "连接会话管理器中选中的会话"),
             ("disconnect", "断开", self.disconnect_active, "断开当前会话")],
            [("copy", "复制", lambda: self.call_active("copy"), "复制选中文本 (Ctrl+Shift+C)"),
             ("paste", "粘贴", lambda: self.call_active("paste"), "粘贴到终端 (Ctrl+Shift+V)"),
             ("find", "查找", self.show_find, "在回滚缓冲中查找 (Ctrl+F)"),
             ("clear", "清屏", lambda: self.call_active("clear_screen"), "清空屏幕 (Ctrl+L)")],
            [("batch", "批量下发", self.show_batch, "向多会话批量下发命令 (F11)"),
             ("script", "脚本", self.show_script, "脚本编辑器 / 运行器 (F10)"),
             ("transfer", "文件传输", self.show_transfer, "SFTP / SCP / Zmodem / 端口转发")],
            [("options", "选项", self.show_options, "全局选项 (Ctrl+,)")],
        ]
        for gi, group in enumerate(groups):
            if gi:
                sep = tk.Frame(self.toolbar, bg=c["border"], width=1)
                sep.pack(side="left", fill="y", padx=6, pady=5)
                self._tool_seps.append(sep)
            for key, label, cmd, tip in group:
                img = self.icons.get(key)
                kw = dict(text=label, command=cmd, bg=c["toolbar"], fg=c["fg"],
                          activebackground=c["hover"], activeforeground=c["fg"],
                          relief="flat", bd=0, padx=5, pady=3,
                          font=ui_theme.UI_FONT, cursor="hand2",
                          highlightthickness=0)
                if img:
                    kw.update(image=img, compound="left", text=" " + label)
                b = tk.Button(self.toolbar, **kw)
                b.pack(side="left", padx=1, pady=3)
                ui_theme.Tooltip(b, tip)
                self._tool_buttons.append(b)
        self.toolbar_line = tk.Frame(self.toolbar, bg=c["border"], height=1)
        self.toolbar_line.pack(fill="x", side="bottom")
        self._tool_seps.append(self.toolbar_line)

    def _build_body(self):
        c = self.ui
        self.paned = ttk.PanedWindow(self.root, orient="horizontal")
        self.paned.pack(fill="both", expand=True, padx=3, pady=(3, 2))
        self.left = ttk.Frame(self.paned, width=int(self.cfg.get("tree_width") or 210))
        self.paned.add(self.left, weight=0)

        # 停靠面板标题栏
        self.left_hdr = tk.Frame(self.left, bg=c["panel_hdr"], height=25)
        self.left_hdr.pack(fill="x")
        self.left_hdr_lbl = tk.Label(self.left_hdr, text="  会话管理器",
                                     bg=c["panel_hdr"], fg=c["hdr_fg"],
                                     font=("Microsoft YaHei UI", 9, "bold"), anchor="w")
        self.left_hdr_lbl.pack(side="left", fill="x", expand=True)
        self.left_hdr_close = tk.Label(self.left_hdr, text="×", bg=c["panel_hdr"],
                                       fg=c["hdr_fg"], font=("Microsoft YaHei UI", 11),
                                       cursor="hand2")
        self.left_hdr_close.pack(side="right", padx=6)
        self.left_hdr_close.bind(
            "<Button-1>", lambda e: (self.var_tree.set(False), self.toggle_tree()))

        # 带占位文字的搜索框
        self.search_bar = tk.Frame(self.left, bg=c["bg"])
        self.search_bar.pack(fill="x", padx=4, pady=(4, 3))
        self.search_icon = tk.Label(self.search_bar, image=self.icons.get("search"),
                                    bg=c["entry_bg"], bd=0)
        self.search_icon.pack(side="left", padx=(3, 0))
        self.tree_search = tk.StringVar()
        self.tree_search.set(ui_theme.PLACEHOLDER)
        self.tree_entry = ttk.Entry(self.search_bar, textvariable=self.tree_search)
        self.tree_entry.pack(side="left", fill="x", expand=True, ipady=2)
        try:
            self.tree_entry.configure(foreground=c["muted"])
        except Exception:
            pass
        self.tree_search.trace_add("write", lambda *_: self.refresh_tree())
        self.search_clear = tk.Label(self.search_bar, text="×", bg=c["bg"],
                                     fg=c["muted"], cursor="hand2",
                                     font=("Microsoft YaHei UI", 10))
        self.search_clear.pack(side="right", padx=4)
        self.search_clear.bind(
            "<Button-1>", lambda e: (self.tree_search.set(""), self.tree_entry.focus_set()))

        def _focus_in(ev):
            if self.tree_entry.get() == ui_theme.PLACEHOLDER:
                self.tree_entry.delete(0, "end")
                try:
                    self.tree_entry.configure(foreground=self.ui["fg"])
                except Exception:
                    pass
                self.refresh_tree()

        def _focus_out(ev):
            if not self.tree_entry.get().strip():
                self.tree_entry.insert(0, ui_theme.PLACEHOLDER)
                try:
                    self.tree_entry.configure(foreground=self.ui["muted"])
                except Exception:
                    pass
                self.refresh_tree()

        self.tree_entry.bind("<FocusIn>", _focus_in)
        self.tree_entry.bind("<FocusOut>", _focus_out)

        # 会话树（带文件夹 / 会话 / 收藏图标）
        tree_box = ttk.Frame(self.left)
        tree_box.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_box, show="tree", selectmode="extended")
        tsb = ttk.Scrollbar(tree_box, command=self.tree.yview)
        self.tree.configure(yscrollcommand=tsb.set)
        tsb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", self._tree_double)
        self.tree.bind("<Button-3>", self._tree_menu)
        # Delete 键删除选中的会话（可多选）
        self.tree.bind("<Delete>", lambda e: self._delete_selected_sessions())
        self.tree.tag_configure("favorite", foreground="#B87A08")
        self.tree.tag_configure("droptgt", background=c.get("sel", "#2D6CCD"),
                                foreground=c.get("sel_fg", "#FFFFFF"))
        # 拖拽会话调整分组
        self._drag_press = None
        self.tree.bind("<Button-1>", self._tree_press, add="+")
        self.tree.bind("<B1-Motion>", self._tree_motion, add="+")
        self.tree.bind("<ButtonRelease-1>", self._tree_drop, add="+")

        # 面板底部快捷操作
        self.left_foot = tk.Frame(self.left, bg=c["bg"])
        self.left_foot.pack(fill="x", pady=(3, 3))
        self._foot_buttons = []
        for key, label, cmd in (("connect", "连接", lambda: self._tree_double(None)),
                                ("new", "新建", self.new_session),
                                ("refresh", "刷新", self.refresh_tree)):
            img = self.icons.get(key)
            kw = dict(text=label, command=cmd, bg=c["toolbar"], fg=c["fg"],
                      activebackground=c["hover"], relief="groove", bd=1,
                      padx=4, pady=1, font=ui_theme.SMALL_FONT, cursor="hand2")
            if img:
                kw.update(image=img, compound="left", text=" " + label)
            b = tk.Button(self.left_foot, **kw)
            b.pack(side="left", padx=(4, 2))
            self._foot_buttons.append(b)

        self.right = ttk.Frame(self.paned)
        self.paned.add(self.right, weight=1)
        self.notebook = ttk.Notebook(self.right)
        self._apply_tab_position()
        self.notebook.pack(fill="both", expand=True)
        self.notebook.bind("<<NotebookTabChanged>>", lambda e: self._tab_changed())
        self.notebook.bind("<Button-3>", self._notebook_right)
        self.notebook.bind("<Configure>", lambda e: self._on_nb_configure(), add="+")

        # 空工作区引导面板
        self.welcome = tk.Frame(self.right, bg=c["panel"])
        self._welcome_widgets = [self.welcome]
        if self.icons.get("session"):
            ic = tk.Label(self.welcome, image=self.icons["session"], bg=c["panel"])
            ic.pack(pady=(0, 12))
            self._welcome_widgets.append(ic)

        def wlabel(text, font, color):
            l = tk.Label(self.welcome, text=text, bg=c["panel"], fg=color, font=font)
            l.pack(pady=2)
            self._welcome_widgets.append(l)

        wlabel("没有打开的会话", ("Microsoft YaHei UI", 12, "bold"), c["tab_sel_fg"])
        wlabel("双击左侧会话管理器中的会话即可连接", ui_theme.UI_FONT, c["muted"])
        wlabel("或按 Ctrl+T 快速连接   ·   F11 批量下发", ui_theme.SMALL_FONT, c["muted"])
        ttk.Button(self.welcome, text="快速连接...",
                   command=self.quick_connect).pack(pady=(16, 4))
        ttk.Button(self.welcome, text="功能与快捷键清单",
                   command=self.show_about).pack()
        self.welcome.place(relx=0.5, rely=0.42, anchor="center")

    def _sync_welcome(self):
        """有会话标签时隐藏引导面板，全部关闭后重新显示。"""
        w = getattr(self, "welcome", None)
        if w is None:
            return
        try:
            if self.tabs_list:
                w.place_forget()
            else:
                w.place(relx=0.5, rely=0.42, anchor="center")
        except Exception:
            pass

    def _build_status(self):
        """SecureCRT 风格的凹入式分段状态栏。"""
        c = self.ui
        self.statusbar = tk.Frame(self.root, bg=c["status"])
        self.statusbar.pack(fill="x", side="bottom")
        self.status_line = tk.Frame(self.statusbar, bg=c["border"], height=1)
        self.status_line.pack(fill="x", side="top")
        row = tk.Frame(self.statusbar, bg=c["status"])
        row.pack(fill="x", padx=3, pady=2)
        self._seg_frames = []

        def seg(text, side="right", expand=False, plain=False):
            back = c["status"] if plain else c["status_seg"]
            f = tk.Frame(row, bg=back, relief="flat" if plain else "sunken",
                         bd=0 if plain else 1)
            f.pack(side=side, padx=1, fill="both" if expand else "y", expand=expand)
            l = tk.Label(f, text=text, bg=back, fg=c["fg"],
                         font=ui_theme.SMALL_FONT, anchor="w")
            l.pack(padx=5, pady=1)
            self._seg_frames.append((f, l, plain))
            return l

        self.lbl_status = seg("就绪", side="left", expand=True)
        self.lbl_broadcast = seg("", plain=True)
        self.lbl_duration = seg("00:00:00")
        self.lbl_size = seg("%sx%s" % (self.cfg.get("cols", 100),
                                       self.cfg.get("rows", 30)))
        self.lbl_enc = seg(self.cfg.get("encoding", "UTF-8"))
        self.lbl_conn = seg("未连接")

    def _apply_tab_position(self):
        # 用“角”定位（nw/sw/wn/en）让标签靠左/上对齐，而不是单方向时的居中
        pos = {"top": "nw", "bottom": "sw", "left": "wn",
               "right": "en"}.get(self.cfg.get("tab_position", "top"), "nw")
        try:
            self.style.configure("TNotebook", tabposition=pos)
        except Exception:
            pass

    def _bind_shortcuts(self):
        r = self.root
        for key in getattr(self, "_macro_bindings", []):
            try:
                r.unbind_all(key)
            except Exception:
                pass
        self._macro_bindings = []
        pairs = [
            ("<Control-t>", lambda e: self.quick_connect()),
            ("<Control-n>", lambda e: self.new_session()),
            ("<Control-w>", lambda e: self.close_active_tab()),
            ("<Control-f>", lambda e: self.show_find()),
            ("<Control-l>", lambda e: self.call_active("clear_screen")),
            ("<Control-comma>", lambda e: self.show_options()),
            ("<F9>", lambda e: self.show_theme_picker()),
            ("<F10>", lambda e: self.show_script()),
            ("<F11>", lambda e: self.show_batch()),
            ("<Control-Tab>", lambda e: self.cycle_tab(1)),
            ("<Control-Shift-Tab>", lambda e: self.cycle_tab(-1)),
            ("<Control-Shift-C>", lambda e: self.call_active("copy")),
            ("<Control-Shift-V>", lambda e: self.call_active("paste")),
        ]
        for seq, fn in pairs:
            r.bind_all(seq, fn)
        for i in range(1, 10):
            r.bind_all("<Alt-%d>" % i, lambda e, n=i: self.select_tab(n - 1))
        for macro in self.cfg.get("macros") or []:
            key = macro.get("key", "").strip()
            if key:
                try:
                    r.bind_all(key, lambda e, m=macro: self._run_macro(m))
                    self._macro_bindings.append(key)
                except Exception:
                    pass

    def _apply_window_options(self):
        r = self.root
        r.title("%s %s · 绿色便携版" % (APP_TITLE, APP_VER))
        r.geometry("1240x780")
        r.minsize(820, 500)
        try:
            alpha = max(40, min(100, int(self.cfg.get("opacity", 100)))) / 100.0
            r.attributes("-alpha", alpha)
        except Exception:
            pass
        self.toggle_tree()
        self.toggle_toolbar()
        self.toggle_status()

    # ------------------------------------------------------------------ 会话树
    def refresh_tree(self):
        if not hasattr(self, "tree"):
            return
        self.tree.delete(*self.tree.get_children())
        q = self.tree_search.get().strip().lower()
        if q == ui_theme.PLACEHOLDER:
            q = ""
        folder_nodes = {}
        def folder_node(path, parent=""):
            if path in folder_nodes:
                return folder_nodes[path]
            parts = [p for p in path.split("/") if p]
            cur = parent
            acc = ""
            for part in parts:
                acc = (acc + "/" + part) if acc else part
                key = "f:" + acc
                if key not in folder_nodes:
                    folder_nodes[key] = self.tree.insert(
                        cur, "end", key, text=" " + part,
                        image=self.icons.get("folder") or "",
                        open=True, tags=("folder",))
                cur = folder_nodes[key]
            return cur
        used_iids = set()
        for s in self.store.all():
            text = s.get("name", "")
            if q and q not in text.lower() and q not in s.get("host", "").lower() \
                    and q not in s.get("folder", "").lower():
                continue
            parent = folder_node(s.get("folder", "")) if s.get("folder") else ""
            tags = []
            if s.get("favorite"):
                tags.append("favorite")
            icon = "star" if s.get("favorite") else "session"
            iid = "s:" + str(s.get("id", "") or "")
            # 防御：历史数据里若仍有重复 id，跳过而不让启动崩溃
            if iid in used_iids:
                continue
            used_iids.add(iid)
            try:
                self.tree.insert(parent, "end", iid,
                                 text=" " + text, image=self.icons.get(icon) or "",
                                 tags=tuple(tags))
            except tk.TclError:
                continue
            color = s.get("label_color") or ""
            if color:
                try:
                    self.tree.tag_configure("c" + s["id"], foreground=color)
                    self.tree.item(iid, tags=tuple(tags) + ("c" + s["id"],))
                except Exception:
                    pass
        for f in self.store.folders:
            if q:
                continue
            folder_node(f)

    def _tree_press(self, event):
        self._drag_press = (event.x, event.y, event.time)

    def _tree_motion(self, event):
        st = getattr(self, "_drag_press", None)
        if not st or (abs(event.x - st[0]) <= 5 and abs(event.y - st[1]) <= 5):
            return
        self.tree.configure(cursor="hand2")
        # 拖动过程中实时高亮目标行（文件夹或会话），让用户看清要落到哪
        iid = self.tree.identify_row(event.y)
        self._set_drop_target(iid if iid and (iid.startswith("f:") or
                                              iid.startswith("s:")) else None)

    def _set_drop_target(self, iid):
        """给目标行加/去 droptgt 高亮标签。"""
        prev = getattr(self, "_drop_iid", None)
        if prev == iid:
            return
        if prev:
            try:
                tags = [t for t in self.tree.item(prev, "tags") if t != "droptgt"]
                self.tree.item(prev, tags=tags)
            except Exception:
                pass
        self._drop_iid = iid
        if iid:
            try:
                tags = list(self.tree.item(iid, "tags"))
                if "droptgt" not in tags:
                    tags.append("droptgt")
                    self.tree.item(iid, tags=tags)
            except Exception:
                pass

    def _tree_drop(self, event):
        """拖拽会话到文件夹/根目录，调整分组并持久化。"""
        self.tree.configure(cursor="")
        self._set_drop_target(None)
        st = getattr(self, "_drag_press", None)
        self._drag_press = None
        if not st:
            return
        if abs(event.x - st[0]) <= 5 and abs(event.y - st[1]) <= 5:
            return  # 普通点击，非拖拽
        sel = [i for i in self.tree.selection() if i.startswith("s:")]
        if not sel:
            return
        iid = self.tree.identify_row(event.y) or ""
        if iid.startswith("f:"):
            target = iid[2:]
        elif iid.startswith("s:"):
            s = self.store.get(iid[2:])
            target = (s or {}).get("folder", "")
        elif iid:
            return  # 拖到文件夹标题等未知位置不处理
        else:
            target = ""  # 拖到空白处 = 根目录
        moved = 0
        for i in sel:
            s = self.store.get(i[2:])
            if s is None or s.get("folder", "") == target:
                continue
            s["folder"] = target
            self.store.update(s)
            moved += 1
        if moved:
            self.refresh_tree()
            self.set_status("已将 %d 个会话移动到「%s」" %
                            (moved, target if target else "根目录"))

    def _delete_selected_sessions(self):
        """删除会话树中当前选中的会话（支持多选批量删除）。"""
        picked = []
        for iid in self.tree.selection():
            if not iid or not iid.startswith("s:"):
                continue
            s = self.store.get(iid[2:])
            if s:
                picked.append(s)
        if not picked:
            # 没选中就退回当前焦点项
            iid = self.tree.focus()
            if iid and iid.startswith("s:"):
                s = self.store.get(iid[2:])
                if s:
                    picked.append(s)
        if not picked:
            return
        self.delete_sessions(picked)

    def _tree_double(self, event):
        iid = self.tree.focus()
        if not iid or not iid.startswith("s:"):
            return
        s = self.store.get(iid[2:])
        if s:
            self.open_session(s)

    def _tree_menu(self, event):
        iid = self.tree.identify_row(event.y) or ""
        menu = tk.Menu(self.root, tearoff=0, font=MENU_FONT)
        if iid.startswith("s:"):
            s = self.store.get(iid[2:])
            if s:
                menu.add_command(label="连接", command=lambda: self.open_session(s))
                menu.add_command(label="在新窗口标签打开", command=lambda: self.open_session(s))
                menu.add_separator()
                menu.add_command(label="属性...", command=lambda: self.edit_session(s["id"]))
                menu.add_command(label="克隆", command=lambda: self.clone_session(s))
                menu.add_command(label="打开 SFTP", command=lambda: self.open_sftp(s))
                menu.add_command(label="删除", command=lambda: self._delete_selected_sessions())
                menu.add_separator()
                menu.add_command(label="收藏/取消", command=lambda: self.toggle_favorite(s))
        elif iid.startswith("f:"):
            folder = iid[2:]
            menu.add_command(label="在此新建会话", command=lambda: self.new_session(folder))
            menu.add_command(label="重命名文件夹...", command=lambda: self.rename_folder(folder))
            menu.add_command(label="删除文件夹", command=lambda: self.remove_folder(folder))
        else:
            menu.add_command(label="新建会话...", command=self.new_session)
            menu.add_command(label="新建文件夹...", command=self.add_folder)
            menu.add_separator()
            menu.add_command(label="导入 SecureCRT 会话", command=self.import_crt)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # ------------------------------------------------------------------ 标签页
    def _focus_terminal(self, tab):
        """让会话标签里的终端文本控件拿到键盘焦点。

        打开会话（快速连接 / 双击会话树）或切换标签后，Notebook 只会切换
        显示页，并不会把键盘焦点交给页内的 tk.Text，导致用户必须再点一下
        终端才能输入命令。这里在布局完成后把焦点补上。

        - 目标必须是 tab.view.text（<KeyPress> 绑在它上面），对外层 Frame
          或 Notebook 设焦点无效。
        - 用 after_idle 等控件完成一次布局，避免新建控件尚未 mapped 时
          focus_set() 失效；_apply_gutter_visibility 这类异步 pack 变化
          也不会影响已 mapped 的 text。
        - 若当前焦点在其它可输入控件（会话树搜索框、对话框输入框等），
          不抢焦点，避免打断用户正在打字。
        """
        if tab is None:
            return
        try:
            if not tab.view.winfo_exists() or not tab.view.text.winfo_exists():
                return
        except Exception:
            return

        def _apply(t=tab):
            try:
                if not t.view.text.winfo_exists():
                    return
                cur = self.root.focus_get()
                if cur is t.view.text:
                    return
                # 焦点在"另一个会话的终端"时允许转移——这正是切换标签的
                # 正常情形，不能当成需要保护的普通输入框。
                is_other_terminal = any(
                    x is not t and getattr(x, "view", None) is not None
                    and cur is getattr(x.view, "text", None)
                    for x in self.tabs_list)
                if not is_other_terminal:
                    # 保护搜索框、对话框输入框等：不打断用户正在打字
                    if isinstance(cur, (tk.Entry, tk.Text, tk.Spinbox)) or \
                            isinstance(cur, ttk.Entry):
                        return
                    # 焦点在顶层对话框时也不抢（对话框可能还没完全关闭）
                    if cur is not None:
                        top = cur.winfo_toplevel()
                        if top is not self.root:
                            return
                t.view.text.focus_set()
            except Exception:
                pass

        self.root.after_idle(_apply)

    def open_session(self, session, activate=True):
        frame = ttk.Frame(self.notebook)
        tab = TerminalTab(self, session, frame)
        self.tabs_list.append(tab)
        self._sync_welcome()
        try:
            self.notebook.add(frame, text=self._tab_label(tab),
                              image=self.icons.get("session") or "", compound="left")
        except Exception:
            self.notebook.add(frame, text=self._tab_label(tab))
        self._make_tab_close(tab, frame)
        if activate:
            self.notebook.select(frame)
        tab.start()
        self.root.after(30, self._layout_tab_closes)
        if activate:
            # 新建会话后自动激活终端窗口，不用再点一下才能输命令
            self._focus_terminal(tab)
        return tab

    def _tab_label(self, tab):
        """标签文字；尾部留白用于放置关闭按钮 ×；空间不足时自动截断标题。"""
        return " " + self._fit_title(self._title_for(tab)) + "   "

    def _fit_title(self, title):
        """按 _tab_text_budget 像素预算截断标题，超出部分以 … 结尾。"""
        budget = getattr(self, "_tab_text_budget", None)
        if not budget or not title:
            return title
        f = getattr(self, "_tab_font", None)
        if f is None:
            try:
                f = tkfont.Font(font=ui_theme.UI_FONT)
                self._tab_font = f
            except Exception:
                return title
        if f.measure(title) <= budget:
            return title
        for n in range(len(title), 0, -1):
            s = title[:n] + "…"
            if f.measure(s) <= budget:
                return s
        return title[:1]

    def _update_tab_widths(self):
        """会话数量多时按标签栏宽度均分预算，自动压缩各标签标题宽度。"""
        nb = getattr(self, "notebook", None)
        if nb is None or not self.tabs_list:
            return
        try:
            w = nb.winfo_width()
        except Exception:
            return
        if w < 200:
            return
        f = getattr(self, "_tab_font", None)
        if f is None:
            try:
                f = tkfont.Font(font=ui_theme.UI_FONT)
                self._tab_font = f
            except Exception:
                return
        # 每个标签的开销：两侧内边距 + 关闭按钮区 + 图标
        overhead = f.measure("  ") + 16 + 34
        budget = max(40, (w - 10) // len(self.tabs_list) - overhead)
        if getattr(self, "_tab_text_budget", None) == budget:
            return
        self._tab_text_budget = budget
        changed = False
        for i, tab in enumerate(self.tabs_list):
            try:
                if str(nb.tab(i, "text")) != self._tab_label(tab):
                    nb.tab(i, text=self._tab_label(tab))
                    changed = True
            except Exception:
                pass
        if changed:
            self.root.after(40, self._layout_tab_closes)

    def _on_nb_configure(self):
        """窗口尺寸变化：先重算标签宽度预算，再摆放关闭按钮。"""
        self._update_tab_widths()
        self._layout_tab_closes()

    def _make_tab_close(self, tab, frame):
        """为标签创建悬浮在标题右侧的关闭按钮。"""
        lbl = tk.Label(self.notebook, text="×", font=ui_theme.SMALL_FONT,
                       bd=0, padx=1, pady=0, cursor="hand2")
        lbl.bind("<Button-1>", lambda e, t=tab: self._close_tab_clicked(t))
        lbl.bind("<Enter>", lambda e, w=lbl: self._hover_tab_close(w, True))
        lbl.bind("<Leave>", lambda e, w=lbl: self._hover_tab_close(w, False))
        self._tab_closes[tab] = (frame, lbl)

    def _hover_tab_close(self, lbl, on):
        try:
            base = self.ui.get("tab_sel_fg") or self.ui.get("fg")
            lbl.configure(fg=(self.ui.get("red") or "#E06C75") if on else base)
        except Exception:
            pass

    def _close_tab_clicked(self, tab):
        if self.cfg.get("confirm_close_tab") and not messagebox.askyesno(
                "关闭标签", "确定关闭 [%s] 并断开连接？" % tab.name, parent=self.root):
            return
        self.close_tab(tab)

    def _style_tab_close(self, lbl, is_sel):
        c = self.ui
        bg = c["tab_sel"] if is_sel else c["tab_bg"]
        fg = c["tab_sel_fg"] if is_sel else c["tab_fg"]
        try:
            lbl.configure(bg=bg, fg=fg)
        except Exception:
            pass

    def _tab_label_spans(self):
        """扫描 ttk.Notebook 的 identify 接口，返回每个标签文字区 (x0, x1)
        像素范围（按标签顺序）。ttk.Notebook 不支持 bbox，这是唯一精确取位方式。"""
        nb = self.notebook
        try:
            total = nb.winfo_width()
        except Exception:
            return []
        if total <= 10:
            return []
        y = 16
        try:
            lh = tkfont.Font(font=ui_theme.UI_FONT).metrics("linespace")
            y = 3 + 5 + lh // 2        # tabmargins top + padding + 半行高
        except Exception:
            pass
        spans = []
        in_lbl = False
        start = 0
        x = 0
        while x < total:
            try:
                el = str(nb.tk.call(nb._w, "identify", x, y))
            except Exception:
                el = ""
            if el == "label" and not in_lbl:
                start = x
                in_lbl = True
            elif el != "label" and in_lbl:
                spans.append((start, x))
                in_lbl = False
            x += 2
        if in_lbl:
            spans.append((start, total))
        return spans

    def _layout_tab_closes(self):
        """把每个 × 关闭按钮摆到对应标签文字区右侧的 padding 空位上。"""
        closes = getattr(self, "_tab_closes", None)
        if not closes:
            return
        nb = getattr(self, "notebook", None)
        if nb is None:
            return
        spans = self._tab_label_spans()
        try:
            sel_frame = str(nb.select())
        except Exception:
            sel_frame = ""
        try:
            lh = tkfont.Font(font=ui_theme.UI_FONT).metrics("linespace")
            ty = 8
        except Exception:
            lh, ty = 17, 8
        for tab in list(self.tabs_list):
            item = closes.get(tab)
            if not item:
                continue
            frame, lbl = item
            span = None
            try:
                idx = nb.index(frame)
                if idx < len(spans):
                    span = spans[idx]
            except Exception:
                span = None
            if not span:
                try:
                    lbl.place_forget()
                except Exception:
                    pass
                continue
            lx0, rx0 = span
            # 文字右侧的 padding 区约 15px 宽，把 × 放进去
            lx = rx0 + 1
            lw = 13
            self._style_tab_close(lbl, str(frame) == sel_frame)
            try:
                lbl.place(x=lx, y=ty, width=lw, height=lh)
            except Exception:
                pass
        # 清理已关闭标签残留的按钮
        for tab, (frame, lbl) in list(closes.items()):
            if tab not in self.tabs_list:
                try:
                    lbl.destroy()
                except Exception:
                    pass
                closes.pop(tab, None)

    def _title_for(self, tab):
        mark = "" if tab.is_connected() else "○ "
        return mark + tab.name

    def refresh_tab_title(self, tab):
        if threading.current_thread() is not threading.main_thread():
            return self.ui_call(self.refresh_tab_title, tab)
        try:
            idx = self.tabs_list.index(tab)
            self.notebook.tab(idx, text=self._tab_label(tab))
        except Exception:
            pass
        self.root.after(20, self._layout_tab_closes)

    def set_tab_hint(self, tab, title):
        # OSC 标题：仅在状态栏提示，避免覆盖用户设定的标签名
        if title:
            self.lbl_conn.configure(text=("%s · %s" % (tab.session.get("host", ""),
                                                       title[:40])))
        self.refresh_tab_title(tab)

    def _tab_changed(self):
        tab = self.active_tab()
        if tab:
            self.lbl_enc.configure(text=tab.encoding)
            self.lbl_size.configure(text="%dx%d" % (tab.cols, tab.rows))
            self.lbl_conn.configure(text="%s://%s:%s" % (tab.session.get("protocol", ""),
                                                         tab.session.get("host", ""),
                                                         tab.session.get("port", "")))
            self.var_record.set(tab.recording)
            # 切换到已有标签时同样自动激活终端，覆盖手动点标签、
            # Ctrl+Tab / Alt+N / activate_tab 等所有切换路径
            self._focus_terminal(tab)
        self._layout_tab_closes()

    def active_tab(self):
        try:
            idx = self.notebook.index(self.notebook.select())
            return self.tabs_list[idx]
        except Exception:
            return None

    def tabs(self):
        return list(self.tabs_list)

    def tab_count(self):
        return len(self.tabs_list)

    def tab_at(self, index):
        try:
            return self.tabs_list[int(index)]
        except Exception:
            return None

    def tab_index(self, tab):
        return self.tabs_list.index(tab) if tab in self.tabs_list else -1

    def activate_tab(self, tab):
        try:
            self.notebook.select(self.tabs_list.index(tab))
        except Exception:
            pass

    def cycle_tab(self, delta):
        if not self.tabs_list:
            return
        i = self.tab_index(self.active_tab())
        self.activate_tab(self.tabs_list[(i + delta) % len(self.tabs_list)])

    def select_tab(self, index):
        if index < len(self.tabs_list):
            self.activate_tab(self.tabs_list[index])

    def close_active_tab(self):
        tab = self.active_tab()
        if not tab:
            return
        if self.cfg.get("confirm_close_tab") and not messagebox.askyesno(
                "关闭标签", "确定关闭 [%s] 并断开连接？" % tab.name, parent=self.root):
            return
        self.close_tab(tab)

    def close_tab(self, tab):
        item = self._tab_closes.pop(tab, None)
        if item:
            try:
                item[1].destroy()
            except Exception:
                pass
        try:
            i = self.tabs_list.index(tab)
            tab.destroy()
            self.notebook.forget(i)
            self.tabs_list.remove(tab)
        except Exception:
            pass
        self._sync_welcome()
        self.root.after(20, self._layout_tab_closes)

    def show_tab_menu(self, event, tab):
        m = tk.Menu(self.root, tearoff=0, font=MENU_FONT)
        m.add_command(label="复制", command=tab.view.copy)
        m.add_command(label="粘贴", command=tab.view.paste)
        m.add_command(label="全选", command=tab.view.select_all)
        m.add_separator()
        m.add_command(label="清屏", command=tab.clear_screen)
        m.add_command(label="查找...", command=self.show_find)
        m.add_command(label="重新连接", command=tab.reconnect)
        m.add_command(label="选择账号登录...", command=lambda t=tab: self.login_with_account(t))
        m.add_command(label="账号管理...", command=self.manage_accounts)
        m.add_command(label="断开", command=tab.disconnect)
        m.add_command(label="克隆会话", command=lambda: self.clone_active())
        m.add_command(label="重命名...", command=lambda: self.rename_tab(tab))
        m.add_separator()
        m.add_command(label="锁定/解锁", command=lambda: self.toggle_lock(tab))
        m.add_command(label="打开 SFTP", command=lambda: self.open_sftp(tab.session))
        m.add_command(label="保存屏幕文本", command=self.save_screen_text)
        m.add_separator()
        m.add_command(label="录制开关", command=lambda: self.toggle_record(tab))
        m.add_command(label="关闭标签", command=lambda: self.close_tab(tab))
        for item in self.cfg.get("send_strings") or []:
            m.add_command(label="发送: " + item.get("name", ""),
                          command=lambda t=item.get("text", ""): tab.send_text(t))
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    def _notebook_right(self, event):
        try:
            idx = self.notebook.index("@%d,%d" % (event.x, event.y))
            tab = self.tabs_list[idx]
            self.show_tab_menu(event, tab)
        except Exception:
            pass

    def call_active(self, method):
        tab = self.active_tab()
        if tab:
            try:
                getattr(tab.view, method)()
            except Exception:
                pass

    def update_duration(self, tab):
        if tab is not self.active_tab():
            return
        sec = int(time.time() - tab._start_at)
        self.lbl_duration.configure(text="%02d:%02d:%02d" %
                                    (sec // 3600, sec % 3600 // 60, sec % 60))

    # ------------------------------------------------------------------ 会话操作
    def new_session(self, folder=""):
        s = new_session("SSH2")
        s["folder"] = folder

        def on_save(data):
            data["id"] = s["id"]
            self.store.add(data)
            self.refresh_tree()

        # SessionDialog._ok 内部已调用 on_save 保存，这里不能再保存一次，
        # 否则 sessions.json 会出现两条同 id 记录，刷新会话树时抛
        # TclError: Item s:<id> already exists（个别电脑启动崩溃的根因）。
        d = SessionDialog(self.root, s, self.store, on_save)
        d.wait_window()
        if d.result:
            if messagebox.askyesno("TermX", "会话已保存，现在连接吗？", parent=self.root):
                self.open_session(d.result)

    def edit_session(self, sid):
        s = self.store.get(sid)
        if not s:
            return
        def on_save(data):
            self.store.update(data)
            self.refresh_tree()
            for tab in self.tabs_list:
                if tab.session.get("id") == sid:
                    tab.session = data
                    tab.proxy.session = data
                    tab.name = data.get("name", tab.name)
                    self.refresh_tab_title(tab)
        SessionDialog(self.root, s, self.store, on_save)

    def edit_active_session(self):
        tab = self.active_tab()
        if tab:
            self.edit_session(tab.session.get("id", ""))

    # ------------------------------------------------------------------ 账号库
    def manage_accounts(self):
        """工具 → 账号管理：添加 / 编辑 / 删除账号。"""
        AccountManagerDialog(self.root)

    def login_with_account(self, tab=None):
        """登录时选择账号：回填会话凭据、立即应答当前登录提示并接管后续提示。"""
        tab = tab or self.active_tab()
        if tab is None:
            messagebox.showinfo("选择账号登录", "请先打开一个会话标签。", parent=self.root)
            return
        if not tab.is_connected():
            messagebox.showwarning("选择账号登录",
                                   "目标尚未连接，请等待连接成功后再选择账号。",
                                   parent=self.root)
            return
        a = AccountSelectDialog(self.root, title="选择账号登录 - " + tab.name).show()
        if not a:
            return
        user = a.get("username", "") or ""
        pwd = a.get("password", "") or ""
        # 绑定账号库引用（而非只复制一份密码快照）：
        # 以后在「工具 → 账号管理」里改密码，这个会话也立即用新密码。
        aid = a.get("id", "") or ""
        tab.session["username"] = user
        if aid:
            tab.session["account_id"] = aid
            tab.session["password"] = ""
            tab.session["password_enc"] = ""
        else:
            set_password(tab.session, pwd)
        try:
            s = self.store.get(tab.session.get("id", ""))
            if s:
                s["username"] = user
                if aid:
                    s["account_id"] = aid
                    s["password"] = ""
                    s["password_enc"] = ""
                else:
                    set_password(s, pwd)
                self.store.update(s)
        except Exception:
            pass
        # 屏幕若正停在登录提示，立即应答；后续提示（含 sudo 二次密码）交给自动登录
        try:
            lines = [l.strip().lower() for l in
                     (tab.screen_text() or "").splitlines() if l.strip()]
            last = lines[-1] if lines else ""
            if any(k in last for k in ("password:", "password：", "passwd:",
                                       "密码", "passphrase", "passcode")):
                tab.send_text(pwd + "\r")
            elif any(k in last for k in ("login:", "login：", "username:",
                                         "username：", "用户名")):
                tab.send_text(user + "\r")
        except Exception:
            pass
        old = getattr(tab, "_auto_login", None)
        if old:
            old.stop()
        tab._auto_login = AutoLoginRunner(tab, tab._auto_login_rules(),
                                          total_timeout=60)
        tab._auto_login.start()
        self.set_status("已使用账号 %s 登录 %s" % (a.get("name") or user, tab.name))

    def delete_session(self, s):
        """删除会话（支持批量）。

        删除时同步清理：
        - sessions.json 里的记录；
        - 该会话已打开的标签页（连同窗口一起关闭）；
        - settings.json 里记住的该会话相关数据。
        """
        self.delete_sessions([s] if isinstance(s, dict) else list(s or []))

    def delete_sessions(self, sessions):
        """批量删除会话，并清理所有关联记录。"""
        sessions = [s for s in (sessions or []) if isinstance(s, dict) and s.get("id")]
        if not sessions:
            return
        if len(sessions) == 1:
            s = sessions[0]
            msg = "确定删除会话 [%s]？" % s.get("name", "")
        else:
            msg = "确定删除选中的 %d 个会话？\n\n%s" % (
                len(sessions),
                "\n".join("  · " + str(x.get("name", "")) for x in sessions[:20]))
        if not messagebox.askyesno("删除会话", msg, parent=self.root):
            return

        ids = set(x.get("id") for x in sessions)
        # 1) 关掉这些会话已打开的标签页
        for tab in list(self.tabs_list):
            if tab.session.get("id") in ids:
                try:
                    self.close_tab(tab)
                except Exception:
                    pass
        # 2) 从会话库移除
        for sid in ids:
            try:
                self.store.remove(sid)
            except Exception:
                pass
        # 3) 清理其它持久化数据里对该会话的引用
        try:
            self._purge_session_refs(ids)
        except Exception:
            pass
        self.refresh_tree()
        self.set_status("已删除 %d 个会话" % len(ids))

    def _purge_session_refs(self, ids):
        """清理其它持久化数据里对这些会话 id 的引用。

        settings.json 里的键名随版本增减，靠固定键名维护容易漏掉，
        因此这里做通用处理：**递归扫描每个设置值，凡是出现被删会话 id
        的条目就整个去掉**（无论是字典的键、列表项还是嵌套结构）。
        """
        if not ids:
            return
        changed = False
        try:
            for key in list(self.cfg.values.keys()):
                val = self.cfg.values.get(key)
                new = self._strip_ids(val, ids)
                if new is not val:
                    self.cfg.set(key, new, save=False)
                    changed = True
            if changed:
                self.cfg.save()
        except Exception:
            pass

    def _strip_ids(self, val, ids):
        """递归剔除 val 中引用了被删会话 id 的条目，返回新值（未变化时返回原对象）。"""
        if isinstance(val, dict):
            out = {}
            hit = False
            for k, v in val.items():
                if k in ids:
                    hit = True
                    continue
                nv = self._strip_ids(v, ids)
                if nv is not v:
                    hit = True
                out[k] = nv
            return out if hit else val
        if isinstance(val, (list, tuple)):
            out = []
            hit = False
            for v in val:
                if isinstance(v, str) and v in ids:
                    hit = True
                    continue
                if isinstance(v, dict) and v.get("id") in ids:
                    hit = True
                    continue
                nv = self._strip_ids(v, ids)
                if nv is not v:
                    hit = True
                out.append(nv)
            return (out if hit else list(val))
        return val

    def clone_session(self, s):
        n = self.store.duplicate(s["id"])
        if n:
            self.refresh_tree()
            self.edit_session(n["id"])

    def clone_active(self):
        tab = self.active_tab()
        if tab:
            s = dict(tab.session)
            s["name"] = tab.name + " - 副本"
            self.open_session(s)

    def toggle_favorite(self, s):
        s["favorite"] = not s.get("favorite")
        self.store.update(s)
        self.refresh_tree()

    def add_folder(self):
        name = simpledialog.askstring("新建文件夹", "文件夹名称(可用 / 表示层级):", parent=self.root)
        if name:
            self.store.add_folder(name)
            self.refresh_tree()

    def rename_folder(self, folder):
        name = simpledialog.askstring("重命名文件夹", "新名称:", initialvalue=folder, parent=self.root)
        if name:
            self.store.rename_folder(folder, name)
            self.refresh_tree()

    def remove_folder(self, folder):
        if messagebox.askyesno("删除文件夹", "删除文件夹 [%s]？会话会移到根目录。" % folder,
                              parent=self.root):
            self.store.remove_folder(folder)
            self.refresh_tree()

    def quick_connect(self):
        d = QuickConnectDialog(self.root, self.cfg, None, self.store)
        d.wait_window()
        if not d.result:
            return
        data = d.result
        proto = data.get("protocol", "SSH2")
        if proto == "Serial":
            com = data.get("serial_port") or "COM1"
            s = new_session("Serial", "%s @ %s" % (com, data.get("serial_baud") or 9600))
            for k in ("serial_port", "serial_baud", "serial_data", "serial_parity",
                      "serial_stop", "serial_flow", "encoding"):
                if data.get(k) not in (None, ""):
                    s[k] = data[k]
        elif proto == "LocalShell":
            s = new_session(proto, "本地 Shell")
            for k in ("shell_path", "shell_args", "encoding"):
                if data.get(k):
                    s[k] = data[k]
        else:
            s = new_session(proto, data.get("host") or "快速连接")
            for k in ("host", "port", "username", "encoding"):
                s[k] = data.get(k) or s[k]
            # 绑定账号库：把 account_id 一起存进会话，连接时凭据实时取自账号库。
            # 少了这一句，会话就丢掉了账号引用，自动应答拿不到密码，
            # SSH 会停在 "password:" 提示符上等用户手输。
            if data.get("account_id"):
                s["account_id"] = data["account_id"]
                # 用户名以账号库为准（改账号后立即生效）
                try:
                    from .sessions import get_username
                    u = get_username(s)
                    if u:
                        s["username"] = u
                except Exception:
                    pass
                # 密码不复制进会话，由账号库统一保管
                s["password"] = ""
                s["password_enc"] = ""
            elif data.get("password"):
                from .sessions import set_password
                set_password(s, data["password"])
        # 分组（需求：快速连接支持分组）
        folder = (data.get("folder") or "").strip()
        if folder:
            s["folder"] = folder
        # 保存/复用（需求：默认保存到会话列表；同协议+主机+端口+用户名直接复用旧会话）
        if getattr(d, "save_var", None) and d.save_var.get():
            old = self._find_same_session(s)
            if old is not None:
                changed = False
                if folder and old.get("folder", "") != folder:
                    old["folder"] = folder
                    changed = True
                # 复用旧会话时同步账号绑定，否则改绑账号后旧会话仍用旧凭据
                if old.get("account_id", "") != s.get("account_id", ""):
                    old["account_id"] = s.get("account_id", "")
                    old["username"] = s.get("username", "")
                    if s.get("account_id"):
                        old["password"] = ""
                        old["password_enc"] = ""
                    changed = True
                if changed:
                    self.store.update(old)
                s = old
                self.store.touch(s.get("id"))
            else:
                self.store.add(s)
            self.refresh_tree()
        self.open_session(s)

    def _find_same_session(self, s):
        """查找与 s 同协议+主机+端口+用户名的既有会话（避免快速连接重复添加）。"""
        proto = s.get("protocol", "")
        host = s.get("host", "")
        port = s.get("port", 0)
        user = s.get("username", "")
        if not host:
            return None
        for old in self.store.all():
            if (old.get("protocol") == proto and old.get("host", "") == host
                    and int(old.get("port") or 0) == int(port or 0)
                    and (old.get("username") or "") == (user or "")):
                return old
        return None

    def import_sessions(self):
        p = filedialog.askopenfilename(parent=self.root, filetypes=[("JSON", "*.json")])
        if p:
            n = self.store.import_json(p)
            self.refresh_tree()
            messagebox.showinfo("TermX", "已导入 %d 个会话" % n, parent=self.root)

    def import_crt(self):
        d = filedialog.askdirectory(parent=self.root, title="选择 SecureCRT Sessions 目录")
        if not d:
            return
        n = self.store.import_crt_dir(d)
        self.refresh_tree()
        messagebox.showinfo("TermX", "已从 SecureCRT 导入 %d 个会话" % n, parent=self.root)

    def export_sessions(self, fmt):
        if fmt == "csv":
            p = filedialog.asksaveasfilename(parent=self.root, defaultextension=".csv")
            if p:
                self.store.export_csv(p)
        else:
            p = filedialog.asksaveasfilename(parent=self.root, defaultextension=".json")
            if p:
                self.store.export_json(p)
        if p:
            messagebox.showinfo("TermX", "已导出到\n" + p, parent=self.root)

    def save_screen_text(self):
        tab = self.active_tab()
        if not tab:
            return
        p = filedialog.asksaveasfilename(parent=self.root, defaultextension=".txt")
        if p:
            open(p, "w", encoding="utf-8").write(tab.view.buffer.all_text())

    def export_record(self):
        tab = self.active_tab()
        if not tab or not tab.recorded:
            messagebox.showinfo("TermX", "当前没有录制内容", parent=self.root)
            return
        p = filedialog.asksaveasfilename(parent=self.root, defaultextension=".txt")
        if p:
            tab.export_record(p)

    # ------------------------------------------------------------------ 对话框
    def show_options(self):
        OptionsDialog(self.root, self.cfg, on_save=self.apply_settings)

    def apply_settings(self):
        self._build_style()
        self._recolor_chrome()
        self._apply_tab_position()
        self._apply_window_options()
        for tab in self.tabs_list:
            try:
                tab.view.apply_settings()
                tab.view.set_highlights(self.cfg.get("key_highlights") or [])
            except Exception:
                pass
        self._bind_shortcuts()
        # 主题菜单的单选状态、以及已打开的主题选择器，一并同步到当前主题
        var = getattr(self, "var_ui_theme", None)
        if var is not None:
            try:
                var.set(self.ui_theme_key)
            except Exception:
                pass
        dlg = getattr(self, "_theme_dialog", None)
        if dlg is not None:
            try:
                if dlg.winfo_exists():
                    dlg.refresh(self.ui_theme_key)
            except Exception:
                pass

    def show_customize(self):
        CustomizeDialog(self.root, self.cfg, on_save=self.apply_settings)

    def show_bundle_status(self):
        """显示随包组件清单，让用户确认无需下载/安装任何东西。"""
        rows = bundle.status()
        text = "TermX 全内置版 · 运行组件清单\n"
        text += "=" * 46 + "\n"
        text += "组件根目录: %s\n\n" % bundle.root()
        for name, ok, path in rows:
            text += " [%s] %s\n        %s\n" % (
                "内置 OK" if ok else "缺失", name, path)
        text += "\n脚本引擎: %s\n" % (
            "系统 WSH" if self.cfg.get("script_engine") == "wsh" else "内置 (零依赖)")
        text += "SSH 实现: %s\n" % os.path.basename(
            ssh_executable(self.cfg))
        text += "\n隔离环境验证: %s\n" % (
            "通过，内置 ssh.exe 可脱离系统 OpenSSH 独立运行"
            if bundle.verify_isolated() else "未通过，请检查 TermX_Bin/openssh")
        messagebox.showinfo("内置组件状态", text, parent=self.root)

    def show_find(self):
        tab = self.active_tab()
        if not tab:
            return
        if self._find_dialog and self._find_dialog.winfo_exists():
            self._find_dialog.lift()
            return
        self._find_dialog = FindDialog(self.root, lambda term: tab.view.set_find(term))

    def show_about(self):
        AboutDialog(self.root)

    def show_batch(self):
        if self._batch_win and self._batch_win.winfo_exists():
            self._batch_win.lift()
            self._batch_win.refresh_targets()
            return
        self._batch_win = BatchWindow(self.root, self)

    def show_script(self):
        if self._script_win and self._script_win.winfo_exists():
            self._script_win.lift()
            self._script_win._refresh_tabs()
            return
        self._script_win = ScriptWindow(self.root, self)

    def show_transfer(self):
        TransferDialog(self.root, self)

    def run_script_file(self):
        p = filedialog.askopenfilename(parent=self.root, filetypes=[
            ("脚本", "*.py;*.js;*.vbs;*.txt"), ("所有", "*.*")])
        if not p:
            return
        tab = self.active_tab()
        if not tab:
            messagebox.showwarning("TermX", "请先打开一个终端标签", parent=self.root)
            return
        lang = "python" if p.endswith(".py") else "jscript" if p.endswith(".js") else \
               "vbscript" if p.endswith(".vbs") else "commands"
        code = open(p, "r", encoding="utf-8", errors="replace").read()
        self.engine.run(code, p, tab, lang, on_log=self.script_log,
                        on_done=lambda ok, msg: self.ui_call(
                            self.set_status, "脚本执行%s%s" %
                            ("完成" if ok else "失败", " · " + msg[:80] if msg else "")))

    def script_log(self, text):
        if threading.current_thread() is not threading.main_thread():
            return self.ui_call(self.script_log, str(text))
        self.set_status(str(text)[:160])
        if self._script_win and self._script_win.winfo_exists():
            try:
                self._script_win._append(str(text))
            except Exception:
                pass

    # ------------------------------------------------------------------ 连接/传输
    def disconnect_active(self):
        tab = self.active_tab()
        if tab:
            tab.disconnect()

    def disconnect_tab(self, tab):
        if tab:
            tab.disconnect()

    def toggle_log(self, tab, enable, path=""):
        if not tab:
            return
        tab.session["log_enabled"] = bool(enable)
        if path:
            tab.session["log_file"] = path
        self.set_status("日志设置已更新（下次连接生效）: %s" % tab.name)

    def script_connect(self, tab, arg, kwargs, new_tab=False):
        text = str(arg or "").strip()
        if not text:
            return
        m = re.search(r'([\w.-]+)@([\w.\-:]+)', text)
        s = new_session("SSH2")
        if m:
            s["username"], s["host"] = m.group(1), m.group(2)
        else:
            s["host"] = text.split()[0]
        for k, v in (kwargs or {}).items():
            if k in s:
                s[k] = v
        self.open_session(s)
        return True

    def open_sftp(self, session):
        s = dict(session)
        s["protocol"] = "SFTP"
        s["name"] = "SFTP · " + s.get("name", "")
        s["no_terminal"] = False
        s["command"] = ""
        self.open_session(s)

    def open_sftp_active(self):
        tab = self.active_tab()
        if tab:
            self.open_sftp(tab.session)

    def scp_transfer(self, tab, local, remote, upload=True, log=None):
        if not tab:
            return
        s = tab.session
        exe = bundle.tool("openssh", "scp.exe") or "scp.exe"
        target = ("%s@%s" % (s.get("username", ""), s.get("host", ""))) if s.get("username") \
            else s.get("host", "")
        args = [exe] + bundle.ssh_identity_options() + ["-P", str(s.get("port") or 22), "-o",
                "StrictHostKeyChecking=%s" % (s.get("ssh_strict") or
                                              self.cfg.get("ssh_strict_host_key") or "accept-new")]
        if s.get("keyfile"):
            args += ["-i", s["keyfile"]]
        if upload:
            args += [local, "%s:%s" % (target, remote)]
        else:
            args += ["%s:%s" % (target, remote), local]
        def run():
            try:
                p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace",
                                     **bundle.spawn_kwargs())
                for line in p.stdout:
                    if log:
                        self.ui_call(log, line.rstrip())
                    else:
                        self.set_status(line.rstrip()[:120])
                p.wait()
                msg = "传输完成 (退出码 %d)" % p.returncode
            except Exception as e:
                msg = "传输失败: %s" % e
            if log:
                self.ui_call(log, msg)
            self.ui_call(self.set_status, msg) if threading.current_thread() is not threading.main_thread() else self.set_status(msg)
        threading.Thread(target=run, daemon=True, name="scp").start()

    def on_zmodem(self, tab):
        self.set_status("Zmodem 传输已由内置协议栈接管 · 接收文件保存在 TermX_Data/downloads")
        try:
            self.root.bell()
        except Exception:
            pass

    def generate_key(self):
        exe = bundle.tool("openssh", "ssh-keygen.exe")
        if not exe:
            messagebox.showerror("TermX", "内置 ssh-keygen.exe 缺失", parent=self.root)
            return
        name = simpledialog.askstring("生成密钥", "密钥文件名(保存在 TermX_Data/keys):",
                                      initialvalue="id_ed25519_termx", parent=self.root)
        if not name:
            return
        path = os.path.join(bundle.keys_dir(), name)
        try:
            p = subprocess.run([exe, "-t", "ed25519", "-f", path, "-N", ""],
                               capture_output=True, text=True, **bundle.spawn_kwargs())
            messagebox.showinfo("TermX", "密钥生成结果:\n%s\n%s\n\n路径: %s" %
                                (p.stdout[-400:], p.stderr[-400:], path), parent=self.root)
        except Exception as e:
            messagebox.showerror("TermX", str(e), parent=self.root)

    def open_data_dir(self):
        os.startfile(data_dir())

    def open_log_dir(self):
        d = os.path.join(data_dir(), self.cfg.get("log_dir", "logs"))
        os.makedirs(d, exist_ok=True)
        os.startfile(d)

    # ------------------------------------------------------------------ 广播/录制/锁定
    def toggle_broadcast(self):
        self._broadcast = bool(self.var_broadcast.get())
        self.lbl_broadcast.configure(
            text="● 广播输入中" if self._broadcast else "",
            foreground="#C05800" if self._broadcast else self.ui["fg"])
        self.set_status("广播输入已%s" % ("开启" if self._broadcast else "关闭"))

    def broadcast(self, source, data):
        if not self._broadcast:
            return
        for tab in self.tabs_list:
            if tab is not source and tab.is_connected() and not tab.locked:
                try:
                    tab.conn.write(data)
                except Exception:
                    pass

    def toggle_record(self, tab=None):
        tab = tab or self.active_tab()
        if not tab:
            return
        tab.recording = not tab.recording
        self.var_record.set(tab.recording)
        if tab.recording:
            tab.recorded = []
        self.set_status("录制已%s (%s)" % ("开启" if tab.recording else "关闭", tab.name))

    def toggle_lock(self, tab=None):
        tab = tab or self.active_tab()
        if not tab:
            return
        tab.locked = not tab.locked
        self.set_status("会话 [%s] 已%s" % (tab.name, "锁定" if tab.locked else "解锁"))
        self.refresh_tab_title(tab)

    def rename_tab(self, tab=None):
        tab = tab or self.active_tab()
        if not tab:
            return
        name = simpledialog.askstring("重命名标签", "标签名称:", initialvalue=tab.name,
                                      parent=self.root)
        if name:
            tab.name = name
            self.refresh_tab_title(tab)

    # ------------------------------------------------------------------ 通用
    def _run_macro(self, macro):
        tab = self.active_tab()
        if not tab:
            return
        text = macro.get("text", "")
        if macro.get("script") and os.path.isfile(macro["script"]):
            try:
                code = open(macro["script"], "r", encoding="utf-8").read()
                self.engine.run(code, macro["script"], tab, "python", on_log=self.script_log)
            except Exception:
                pass
        else:
            tab.send_text(text.replace("\\r", "\r").replace("\\n", "\r"))

    def ui_call(self, func, *args):
        """从后台线程同步调用 UI 函数（避免 tkinter 跨线程崩溃）。"""
        if threading.current_thread() is threading.main_thread():
            return func(*args)
        ev = threading.Event()
        holder = {"value": None, "error": None}
        self._ui_jobs.put((func, args, ev, holder))
        if not ev.wait(15):
            return None
        if holder["error"]:
            raise holder["error"]
        return holder["value"]

    def _pump_ui(self):
        try:
            for _ in range(30):
                try:
                    func, args, ev, holder = self._ui_jobs.get_nowait()
                except queue.Empty:
                    break
                try:
                    holder["value"] = func(*args)
                except Exception as e:
                    holder["error"] = e
                finally:
                    ev.set()
        except Exception:
            pass
        if not self._closing:
            self.root.after(20, self._pump_ui)

    def set_status(self, text):
        if threading.current_thread() is not threading.main_thread():
            return self.ui_call(self.set_status, text)
        try:
            self.lbl_status.configure(text=str(text))
        except Exception:
            pass

    def _on_session_save_error(self, msg):
        """会话库写盘失败：状态栏常驻 + 首次弹一次窗，后续不再打扰。"""
        text = "会话保存失败：%s" % msg
        self.set_status(text)
        if getattr(self, "_save_err_shown", False):
            return
        self._save_err_shown = True
        try:
            messagebox.showwarning("保存失败",
                                   text + "\n\n请检查运行目录是否可写、磁盘是否已满。",
                                   parent=self.root)
        except Exception:
            pass

    # ---- 脚本引擎 / 批量下发所需的控制接口 ----
    def send_to_tab(self, tab, data):
        if tab is not None:
            tab.send_text(data)

    def wait_for(self, tab, patterns, timeout=30):
        if tab is None:
            return (-1, "", 0)
        return tab.wait_for(patterns, float(timeout or 30))

    def toggle_tree(self):
        if self.var_tree.get():
            self.paned.insert(0, self.left)
        else:
            try:
                self.paned.forget(self.left)
            except Exception:
                pass

    def toggle_toolbar(self):
        self.toolbar.pack_forget()
        if self.var_toolbar.get():
            # before=self.paned 保证工具栏始终紧贴菜单栏，不会被挤到窗口底部
            try:
                self.toolbar.pack(fill="x", side="top", before=self.paned)
            except Exception:
                self.toolbar.pack(fill="x", side="top")

    def toggle_status(self):
        if self.var_statusbar.get():
            self.statusbar.pack(fill="x", side="bottom")
        else:
            self.statusbar.pack_forget()

    def on_close(self):
        if self._closing:
            return
        if self.tabs_list and not messagebox.askyesno(
                "退出 TermX", "当前有 %d 个会话标签，确定退出并断开全部连接？" %
                len(self.tabs_list), parent=self.root):
            return
        self._closing = True
        for tab in list(self.tabs_list):
            try:
                tab.destroy()
            except Exception:
                pass
        self.cfg.save()
        self.root.destroy()


def _app_icon_path():
    """定位 TermX.ico：打包后取 _MEIPASS 内的副本，源码运行取 build 目录。"""
    import sys
    candidates = []
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        candidates.append(os.path.join(meipass, "TermX.ico"))
    candidates.append(os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "..", "build", "TermX.ico")))
    for p in candidates:
        if os.path.isfile(p):
            return p
    return ""


def launch():
    root = tk.Tk()
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    # 窗口/任务栏图标与 exe 图标保持一致（-default 同时作用于所有 Toplevel 弹窗）
    try:
        ico = _app_icon_path()
        if ico:
            root.iconbitmap("-default", ico)
    except Exception:
        pass
    # 登记内置 VC 运行库与私有字体：不需要系统预装任何运行库
    bundle.apply_current_process()
    # 首次运行/覆盖安装：选择或关联运行目录（唯一允许创建数据目录的入口）
    if _cfg_needs_setup():
        dlg = DataDirDialog(root)
        root.wait_window(dlg)
        if dlg.result is None:
            root.destroy()
            return None
    else:
        # 便携版：数据目录固定在软件目录里，自动建好并把旧位置的数据带过来
        try:
            _dir, _note = _cfg_ensure_data_dir()
            if _note:
                root.after(600, lambda n=_note: messagebox.showinfo(
                    "TermX 运行目录", n, parent=root))
        except Exception:
            pass
    app = MainWindow(root)
    root.mainloop()
    return app
