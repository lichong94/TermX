# -*- coding: utf-8 -*-
"""各类对话框：会话属性、全局选项、快速连接、批量下发、脚本、文件传输、查找、关于。"""
import ctypes
import ipaddress
import json
import os
import re
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog, colorchooser

from . import ui_theme
from . import config as _cfg
from . import sessions as _sessions      # 取模块级保存错误状态（BUG-04）
from .config import APP_VER, APP_BUILD, ENCODINGS, PROTOCOLS, SCHEMES, path_of
from .sessions import (AccountStore, _dpapi, get_password, set_password,
                       new_session, VariableTable)
from .scripting import BatchDispatcher, parse_login_script

TERM_TYPES = ["xterm", "xterm-256color", "vt100", "vt220", "vt320", "linux",
              "ansi", "scoansi", "wyse50", "qnx"]
PARITY = ["N", "E", "O", "M", "S"]
FLOW = ["none", "xonxoff", "hardware"]
SERIAL_PORTS = ["COM%d" % i for i in range(1, 33)]
SERIAL_BAUD = ["1200", "2400", "4800", "9600", "19200", "38400",
               "57600", "115200", "230400", "460800", "921600"]

# 批量下发"关闭分页"的内置厂商命令（均在用户视图执行，只对当前会话有效）
PAGING_VENDORS = {
    "华为": "screen-length 0 temporary",   # VRP：0=关闭分屏，temporary 临时生效
    "华三": "screen-length disable",       # Comware：关闭当前用户分屏显示
}


def detect_serial_ports():
    """枚举本机真实存在的 COM 口（QueryDosDeviceW），失败时返回空列表。"""
    try:
        import ctypes.wintypes as wt
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.QueryDosDeviceW.argtypes = [wt.LPCWSTR, wt.LPWSTR, wt.DWORD]
        k.QueryDosDeviceW.restype = wt.DWORD
        buf = ctypes.create_unicode_buffer(32768)
        n = k.QueryDosDeviceW(None, buf, len(buf))
        if not n:
            return []
        raw = "".join(buf[:n])
        found = [x for x in raw.split("\x00") if re.match(r"^COM\d+$", x, re.I)]
        return sorted(found, key=lambda s: int(s[3:]))
    except Exception:
        return []

SESSION_PAGES = [
    ("连接", [
        ("name", "会话名称", "entry", None),
        ("folder", "分组路径", "entry", None),
        ("protocol", "协议", "combo", PROTOCOLS),
        ("host", "主机名 / IP", "entry", None),
        ("port", "端口", "spin", None),
        ("account", "账号(选择)", "account", None),
        ("auth", "认证方式", "combo", ["password", "publickey", "gssapi", "none"]),
        ("keyfile", "私钥文件", "browse", None),
        ("command", "远程命令", "entry", None),
        ("no_terminal", "仅端口转发 (-N)", "check", None),
        ("sftp_path", "SFTP 起始目录", "entry", None),
    ]),
    ("SSH / 高级", [
        ("ssh_strict", "主机密钥检查", "combo", ["accept-new", "ask", "yes", "no"]),
        ("ssh_compression", "启用压缩 (-C)", "check", None),
        ("ssh_agent_forward", "Agent 转发 (-A)", "check", None),
        ("ssh_x11", "X11 转发 (-X)", "check", None),
        ("ssh_alive_interval", "保活间隔(秒)", "spin", None),
    ]),
    ("终端", [
        ("term_type", "终端仿真", "combo", TERM_TYPES),
        ("encoding", "字符编码", "combo", [""] + ENCODINGS),
        ("cols", "列数(0=自动)", "spin", None),
        ("rows", "行数(0=自动)", "spin", None),
        ("scrollback", "回滚行数(0=全局)", "spin", None),
        ("scheme", "配色方案", "combo", [""] + list(SCHEMES.keys())),
        ("font_family", "字体", "combo", ["", "Consolas", "DejaVu Sans Mono", "Cascadia Mono", "Lucida Console",
                                            "Courier New", "Microsoft YaHei Mono", "等线"]),
        ("font_size", "字号(0=全局)", "spin", None),
    ]),
    ("外观 / 备注", [
        ("label_color", "标签颜色", "color", None),
        ("tags", "标签", "entry", None),
        ("favorite", "收藏", "check", None),
        ("description", "备注", "text", None),
    ]),
    ("日志", [
        ("log_enabled", "启用会话日志", "check", None),
        ("log_file", "日志文件", "browse", None),
        ("log_append", "追加写入", "check", None),
        ("log_timestamp", "添加时间戳", "check", None),
        ("log_strip_ansi", "剥离转义序列", "check", None),
    ]),
    ("自动化", [
        ("auto_command", "连接后自动执行", "entry", None),
        ("script", "启动脚本文件", "browse", None),
        ("login_script", "Expect/Send 登录脚本", "text", None),
        ("anti_idle", "防空闲保活", "check", None),
        ("anti_idle_interval", "保活间隔(秒)", "spin", None),
        ("anti_idle_string", "保活字符串", "entry", None),
        ("auto_reconnect", "断线自动重连", "check", None),
        ("auto_reconnect_delay", "重连间隔(秒)", "spin", None),
        ("auto_reconnect_times", "最大重连次数", "spin", None),
    ]),
    ("代理", [
        ("proxy", "代理模式", "combo", ["inherit", "none", "custom"]),
        ("proxy_type", "代理类型", "combo", ["socks5", "http", "none"]),
        ("proxy_host", "代理主机", "entry", None),
        ("proxy_port", "代理端口", "spin", None),
        ("proxy_user", "代理用户名", "entry", None),
        ("proxy_pass", "代理密码", "password", None),
    ]),
    ("串口", [
        ("serial_port", "串口 (COM)", "combo", SERIAL_PORTS),
        ("serial_baud", "波特率", "combo", SERIAL_BAUD),
        ("serial_data", "数据位", "combo", ["5", "6", "7", "8"]),
        ("serial_parity", "校验位", "combo", PARITY),
        ("serial_stop", "停止位", "combo", ["1", "1.5", "2"]),
        ("serial_flow", "流控", "combo", FLOW),
    ]),
    ("本地 Shell", [
        ("shell_path", "程序路径", "browse", None),
        ("shell_args", "启动参数", "entry", None),
        ("shell_cwd", "工作目录", "browse_dir", None),
    ]),
]

OPTION_PAGES = [
    ("常规", [
        ("data_dir", "运行目录", "datadir", None),
        ("data_dir_info", "", "info",
         lambda: "留空 = 自动（优先 D:\\TermX，不可用则落到系统 AppData）。\n"
                 "可指向任意目录（覆盖安装时关联已有数据目录，已有文件优先保留）。\n"
                 "当前实际：%s%s" % (
                     _cfg.data_dir(),
                     "（已指定）" if _cfg.get_configured_data_dir() else "（自动）")),
        ("local_shell", "默认本地 Shell", "entry", None),
        ("tab_position", "标签位置", "combo", ["top", "bottom", "left", "right"]),
        ("show_toolbar", "显示工具栏", "check", None),
        ("show_statusbar", "显示状态栏", "check", None),
        ("show_session_tree", "显示会话树", "check", None),
        ("tree_width", "会话树宽度", "spin", None),
        ("confirm_close_tab", "关闭标签时确认", "check", None),
        ("single_instance", "单实例模式", "check", None),
    ]),
    ("外观", [
        ("font_family", "终端字体", "combo", ["Consolas", "DejaVu Sans Mono",
                                               "Cascadia Mono", "Lucida Console",
                                               "Courier New", "Microsoft YaHei Mono", "等线"]),
        ("font_size", "字号", "spin", None),
        ("scheme", "配色方案", "combo", list(SCHEMES.keys())),
        ("cursor_style", "光标样式", "combo", ["block", "bar", "underline"]),
        ("cursor_blink", "光标闪烁", "check", None),
        ("show_line_gutter", "终端左侧显示时间戳与行号", "check", None),
        ("wheel_scrollback_first", "鼠标滚轮优先回滚(程序开启鼠标模式时)", "check", None),
        ("opacity", "窗口不透明度(%)", "spin", None),
        ("bell_visual", "响铃时视觉提示", "check", None),
        ("theme_ui", "界面主题", "combo", ui_theme.DISPLAY_NAMES),
        ("theme_sync_scheme", "切换主题时同步终端配色", "check", None),
    ]),
    ("终端", [
        ("encoding", "默认编码", "combo", ENCODINGS),
        ("cols", "初始列数", "spin", None),
        ("rows", "初始行数", "spin", None),
        ("scrollback", "回滚缓冲行数", "spin", None),
        ("backspace", "Backspace 发送", "combo", ["DEL", "BS"]),
        ("bracketed_paste", "支持括号粘贴", "check", None),
        ("confirm_paste_multiline", "多行粘贴确认", "check", None),
        ("auto_copy_selection", "选中即复制", "check", None),
        ("local_echo", "本地回显", "check", None),
        ("word_chars", "双击选词附加字符", "entry", None),
    ]),
    ("日志", [
        ("auto_log", "所有会话自动记录日志", "check", None),
        ("log_dir", "日志目录", "entry", None),
        ("log_timestamp", "日志时间戳", "check", None),
        ("log_strip_ansi", "剥离转义序列", "check", None),
    ]),
    ("SSH", [
        ("ssh_path", "ssh.exe 路径 (auto=随包内置)", "browse", None),
        ("ssh_strict_host_key", "主机密钥检查", "combo", ["accept-new", "ask", "yes", "no"]),
        ("ssh_legacy_algo", "兼容老旧设备算法 (ssh-rsa / SHA-1 KEX / CBC)", "check", None),
        ("ssh_alive_interval", "ServerAliveInterval", "spin", None),
        ("ssh_compression", "默认压缩", "check", None),
        ("ssh_agent_forward", "默认 Agent 转发", "check", None),
        ("ssh_x11", "默认 X11 转发", "check", None),
    ]),
    ("代理", [
        ("proxy_type", "代理类型", "combo", ["none", "socks5", "http"]),
        ("proxy_host", "代理主机", "entry", None),
        ("proxy_port", "代理端口", "spin", None),
        ("proxy_user", "代理用户名", "entry", None),
        ("proxy_pass", "代理密码", "password", None),
    ]),
    ("自动化", [
        ("script_engine", "VBScript/JScript 引擎 (builtin=内置, wsh=系统)",
         "combo", ["builtin", "wsh"]),
        ("anti_idle", "全局防空闲", "check", None),
        ("anti_idle_interval", "防空闲间隔(秒)", "spin", None),
        ("anti_idle_string", "防空闲字符串", "entry", None),
        ("auto_reconnect", "全局自动重连", "check", None),
        ("auto_reconnect_delay", "重连间隔(秒)", "spin", None),
        ("auto_reconnect_times", "最大次数", "spin", None),
        ("script_dir", "脚本目录", "entry", None),
    ]),
    ("批量下发", [
        ("batch_delay", "命令间延时(秒)", "spin", None),
        ("batch_timeout", "等待超时(秒)", "spin", None),
        ("batch_wait_prompt", "等待提示符", "check", None),
        ("batch_parallel", "多会话并发", "check", None),
        ("batch_stop_on_error", "出错即停止", "check", None),
    ]),
]


def _scrollable(parent):
    outer = ttk.Frame(parent)
    canvas = tk.Canvas(outer, highlightthickness=0, borderwidth=0)
    sb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
    inner = ttk.Frame(canvas)
    inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")
    canvas.configure(yscrollcommand=sb.set)
    canvas.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.bind("<Configure>", lambda e: canvas.itemconfig(inner_id, width=e.width))
    def wheel(e):
        canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")
    canvas.bind("<MouseWheel>", wheel)
    outer.inner_frame = inner
    return outer, inner


class FieldDialog(tk.Toplevel):
    """基于字段 schema 的通用多页属性对话框。"""
    def __init__(self, parent, title, pages, values, width=680, height=560):
        tk.Toplevel.__init__(self, parent)
        self.title(title)
        self.transient(parent)
        self.resizable(True, True)
        self.geometry("%dx%d" % (width, height))
        self.parent = parent
        self.pages = pages
        self.values = dict(values)
        self.vars = {}
        self.widgets = {}          # key -> 控件句柄（combo 等需要后续操作的控件）
        self.result = None
        # 绑定的账号库 ID：不在表单里显示，随会话一起保存
        self._account_id = self.values.get("account_id", "") or ""
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        for page_title, fields in pages:
            outer, f = _scrollable(nb)
            nb.add(outer, text=page_title)
            self._build_fields(f, fields)
            self._extra(nb, outer, page_title)
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=6)
        ttk.Button(bar, text="确定", command=self._ok).pack(side="right", padx=4)
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="right", padx=4)
        ttk.Button(bar, text="应用", command=self._apply).pack(side="right", padx=4)
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()
        self.focus_set()

    def _extra(self, nb, outer, page_title):
        pass

    def _build_fields(self, parent, fields):
        for i, (key, label, kind, opts) in enumerate(fields):
            row = ttk.Frame(parent)
            row.pack(fill="x", padx=10, pady=3)
            ttk.Label(row, text=label, width=20, anchor="w").pack(side="left")
            val = self.values.get(key, "")
            if kind == "check":
                var = tk.BooleanVar(value=bool(val))
                ttk.Checkbutton(row, variable=var).pack(side="left")
            elif kind == "combo":
                var = tk.StringVar(value=str(val))
                cb = ttk.Combobox(row, textvariable=var, values=list(opts or []))
                cb.pack(side="left", fill="x", expand=True)
                self.widgets[key] = cb
            elif kind == "account":
                # 账号库下拉：凭据以账号库为准，选中即绑定（account_id）。
                # 表单默认只显示这一行（1.4.15 起去掉了独立的用户名/密码两行）；
                # 只有选「（手动输入用户名/密码）」时才展开手填区。
                var = tk.StringVar(value="")
                aid = self.values.get("account_id", "") or ""
                label = _label_of_account_id(aid)
                if aid and not label:
                    # 账号已被删除：明确提示重新绑定，且不把失效 ID 写回会话
                    var.set(ACCOUNT_STALE)
                    self._account_id = ""
                else:
                    var.set(label or ACCOUNT_NONE)
                cb = ttk.Combobox(row, textvariable=var, state="readonly",
                                  values=_account_choices() + [ACCOUNT_STALE])
                cb.pack(side="left", fill="x", expand=True)
                cb.bind("<<ComboboxSelected>>",
                        lambda e, c=cb: self._apply_account(c.get()))
                ttk.Button(row, text="管理账号...", width=11,
                           command=lambda c=cb: self._manage_accounts(c)).pack(
                               side="left", padx=(4, 0))
                self.widgets[key] = cb
                # 只读提示行：明确展示当前绑定的是哪个账号、用哪个用户名登录。
                # 从会话管理器右键进属性时一眼就能看到，也能直接在上面的
                # 下拉里重新选一个账号完成重新绑定。
                info = ttk.Frame(parent)
                info.pack(fill="x", padx=10, pady=(0, 3))
                self._account_info = tk.StringVar(value="")
                ttk.Label(info, textvariable=self._account_info, wraplength=470,
                          justify="left", foreground="#5A5A5A").pack(anchor="w")
                _refresh_account_info(self)
                # 手动凭据区：默认收起，选「（手动输入用户名/密码）」时展开
                self._manual_frame = ttk.Frame(parent)
                for mkey, mlabel, mkind in (("username", "用户名", "entry"),
                                            ("password", "密码", "password")):
                    mrow = ttk.Frame(self._manual_frame)
                    mrow.pack(fill="x", padx=10, pady=3)
                    ttk.Label(mrow, text=mlabel, width=20, anchor="w").pack(side="left")
                    mvar = tk.StringVar(value=str(self.values.get(mkey, "") or ""))
                    ttk.Entry(mrow, textvariable=mvar,
                              show="*" if mkind == "password" else "").pack(
                                  side="left", fill="x", expand=True)
                    self.vars[mkey] = (mkind, mvar)
                # 未绑定账号且原本填过用户名/密码时展开手填区，避免看起来"没信息"
                self._toggle_manual(not aid and
                                    bool(self.values.get("username") or
                                         self.values.get("password_enc")))
            elif kind == "spin":
                var = tk.StringVar(value=str(val))
                ttk.Spinbox(row, from_=0, to=999999, textvariable=var, width=14).pack(side="left")
            elif kind == "text":
                var = tk.StringVar(value=str(val))
                txt = tk.Text(row, height=4, width=48)
                txt.insert("1.0", str(val))
                txt.pack(side="left", fill="x", expand=True)
                self.vars[key] = ("text", txt)
                continue
            elif kind == "datadir":
                var = tk.StringVar(value=str(val))
                ttk.Entry(row, textvariable=var).pack(side="left", fill="x", expand=True)
                ttk.Button(row, text="...", width=3,
                           command=lambda v=var: self._browse(v, "browse_dir")).pack(side="left", padx=2)
                ttk.Button(row, text="自动", width=5,
                           command=lambda v=var: v.set("")).pack(side="left")
            elif kind == "info":
                try:
                    text = opts() if callable(opts) else str(val or opts or "")
                except Exception:
                    text = ""
                lbl = ttk.Label(row, text=text, wraplength=430, foreground="#888888",
                                justify="left")
                lbl.pack(side="left", fill="x", expand=True)
                continue
            elif kind == "color":
                var = tk.StringVar(value=str(val))
                ttk.Entry(row, textvariable=var).pack(side="left", fill="x", expand=True)
                ttk.Button(row, text="...", width=3,
                           command=lambda v=var: self._pick_color(v)).pack(side="left", padx=2)
            elif kind in ("browse", "browse_dir"):
                var = tk.StringVar(value=str(val))
                ttk.Entry(row, textvariable=var).pack(side="left", fill="x", expand=True)
                ttk.Button(row, text="...", width=3,
                           command=lambda v=var, k=kind: self._browse(v, k)).pack(side="left", padx=2)
            else:
                var = tk.StringVar(value=str(val))
                ttk.Entry(row, textvariable=var, show="*" if kind == "password" else "").pack(
                    side="left", fill="x", expand=True)
            self.vars[key] = (kind, var)

    # ---------------------------------------------------------------- 账号选择辅助
    def _set_var(self, key, value):
        item = self.vars.get(key)
        if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], tk.Variable):
            item[1].set(value)

    def _toggle_manual(self, visible):
        """显示/隐藏手动填写的用户名、密码（仅"不使用账号库"时可见）。"""
        frame = getattr(self, "_manual_frame", None)
        if frame is None:
            return
        try:
            if visible:
                frame.pack(fill="x", padx=0, pady=0)
            else:
                frame.pack_forget()
        except Exception:
            pass

    def _apply_account(self, label):
        """账号下拉变化：绑定账号，或切换到手动输入。

        - 选中某个账号 -> 记录 account_id，隐藏手动区（凭据由账号库提供）；
        - 选"（不使用账号库）" -> 清空 account_id，显示手动区。
        """
        if not label or label in (ACCOUNT_NONE, ACCOUNT_STALE):
            self._account_id = ""
            self._toggle_manual(label == ACCOUNT_NONE)
            _refresh_account_info(self)
            return None
        a = _fill_account(label, lambda k, v: setattr(self, "_account_id", v or ""))
        if a is None:
            self._account_id = ""
            self._toggle_manual(True)
        else:
            self._toggle_manual(False)
        _refresh_account_info(self)
        return a

    def _manage_accounts(self, cb):
        AccountManagerDialog(self, on_change=lambda: _refresh_account_combo(cb))

    def _pick_color(self, var):
        c = colorchooser.askcolor(color=var.get() or "#FFFFFF", parent=self)
        if c and c[1]:
            var.set(c[1])

    def _browse(self, var, kind):
        if kind == "browse_dir":
            p = filedialog.askdirectory(parent=self)
        else:
            p = filedialog.askopenfilename(parent=self)
        if p:
            var.set(p)

    def collect(self):
        out = dict(self.values)
        for key, item in self.vars.items():
            if not isinstance(item, tuple) or len(item) != 2:
                continue
            kind, var = item
            if kind == "account":        # 仅用于选择回填，不写入会话数据
                continue
            if kind == "text":
                try:
                    out[key] = var.get("1.0", "end").rstrip("\n")
                except Exception:
                    pass
                continue
            if not isinstance(var, tk.Variable):
                continue
            value = var.get()
            if kind in ("spin",):
                try:
                    value = int(float(value))
                except Exception:
                    value = 0
            elif key in ("port", "proxy_port", "serial_baud", "serial_data",
                         "serial_stop", "anti_idle_interval", "auto_reconnect_delay",
                         "auto_reconnect_times", "ssh_alive_interval", "scrollback"):
                try:
                    value = int(float(value))
                except Exception:
                    value = 0
            out[key] = value
        # 账号引用：绑定了账号就带上 account_id（密码由账号库提供）
        out["account_id"] = self._account_id or ""
        if out["account_id"]:
            # 绑定了账号：用户名以账号库为准（改账号后立即生效），
            # 手动区填的内容一律忽略。
            try:
                from .sessions import get_username
                u = get_username(out)
                if u:
                    out["username"] = u
            except Exception:
                pass
        else:
            # 未绑定账号：手动区的用户名/密码生效
            if "username" in self.vars:
                try:
                    out["username"] = self.vars["username"][1].get()
                except Exception:
                    pass
        return out

    def _apply(self):
        self.result = self.collect()
        return self.result

    def _ok(self):
        self._apply()
        self.destroy()

    def show(self):
        self.wait_window()
        return self.result


def attach_folder_fuzzy(cb, get_folders):
    """分组下拉支持模糊查询：输入即过滤已有分组；不存在的新分组保存时自动创建。"""
    try:
        cb.configure(values=list(get_folders() or []))
    except Exception:
        pass

    def on_key(event):
        if event.keysym in ("Up", "Down", "Return", "Escape", "Tab",
                            "Left", "Right", "Home", "End"):
            return
        txt = (cb.get() or "").strip().lower()
        try:
            folders = list(get_folders() or [])
        except Exception:
            folders = []
        cb.configure(values=[f for f in folders if txt in f.lower()] if txt else folders)
    cb.bind("<KeyRelease>", on_key, add="+")


VARIABLE_HELP_TEXT = """【导入变量 CSV 使用教程】

一、变量表是什么
  一个 CSV 文件：第一行是变量名（列名），从第二行起每一行对应一台设备。
  下发时按勾选目标的顺序一一对应——第 1 台设备用第 1 行，第 2 台用第 2 行，
  依此类推；若设备数多于表格行数，多出来的设备只用内置变量。

二、变量表怎么展开（重要）
  在「③ 下发参数 → 变量展开」里选：
  · 每台设备执行全部变量行（默认）：变量表 500 行、勾选 10 台设备，
    就会在**每台设备上各执行 500 条命令**（命令里的变量逐行替换）。
    适合"同一个检查项，用不同参数在所有设备上跑一遍"。
    例：命令 display ap elabel all | include {SN码}，变量表有 500 个 SN码，
    则每台设备依次执行 500 条，分别包含这 500 个 SN码。
  · 每行变量对应一台设备（旧）：第 1 台设备用第 1 行，第 2 台用第 2 行，
    适合"每台设备的配置各不相同"。
  结果表的"会话"列会标注 [行N]，导出的 CSV/TXT 也带变量行号，
  500 条结果也能分清是哪一行变量产生的。

三、三步上手
  1) 点上方「变量模板」按钮，保存一个 CSV 模板并用 Excel 打开填写。
     模板示例列：loopback, vlan, uplink, 备注
     示例行：10.0.0.1, 100, GigabitEthernet0/1, 示例设备1
  2) 点「导入变量 CSV」选中填好的文件，按钮旁会显示「变量表: N 行 / 列 …」。
  3) 在命令输入框里用 {变量名}（即 CSV 首行的列名）引用，例如：
       interface {uplink}
       ip address {loopback} 255.255.255.255
       description VLAN {vlan} - {备注}
     三种写法都支持：{vlan} / ${vlan} / %vlan%。

四、内置变量（无需写进 CSV，自动提供）
     {name}     会话名称          {host} / {ip}   主机地址
     {user}     登录用户名        {port}          端口
     {protocol} 协议              {folder}        分组
     {index}    序号（从 0 开始）  {no}           序号（从 1 开始）
  例：echo 正在配置第 {no} 台 {name} ({host})

五、命令区其它语法（每行一条）
     SLEEP 500          等待 500 毫秒
     WAIT "提示符" 30    等待指定提示符出现，最多 30 秒
     空行               只向目标发送一个回车
     # 开头的行         注释，跳过不下发

六、常见问题
  · 找不到变量？命令里会原样保留 {xxx}。请确认 CSV 首行列名与命令里的名字
    完全一致（区分大小写不要在名字里加空格）。
  · Excel 保存 CSV 后中文乱码？本版已兼容 UTF-8 / GBK / UTF-16 与逗号、
    分号、制表符分隔；若仍有问题，可另存为「CSV UTF-8」再导入。
  · 表头写在第几行？必须是第一行；前面不要插标题或说明行。
  · 只想用内置变量？不导入 CSV 即可，命令里照常写 {host} {name} {no}。
"""

# ------------------------------------------------------------------ 账号库
# 账号下拉的"手动输入"选项：账号库没有对应条目时，选它可临时手填凭据。
# 表单默认只显示账号下拉（1.4.15 起去掉了独立的用户名/密码两行）。
ACCOUNT_NONE = "（手动输入用户名/密码）"
# 会话绑定的账号已被删除时的提示项：提醒重新绑定，避免把失效 ID 写回。
ACCOUNT_STALE = "（原账号已删除，请重新选择）"


def _account_store():
    return AccountStore()


def _account_labels():
    try:
        return [AccountStore.label(a) for a in _account_store().all()]
    except Exception:
        return []


def _account_choices():
    """账号下拉的完整选项：先是"不绑定"，再是账号库里的全部账号。"""
    return [ACCOUNT_NONE] + _account_labels()


def _label_of_account_id(aid):
    """按账号 ID 反查下拉显示文本（用于编辑时回显当前绑定）。"""
    if not aid:
        return ""
    try:
        acc = AccountStore().get(aid)
    except Exception:
        acc = None
    if not acc:
        return ""
    try:
        return AccountStore.label(acc)
    except Exception:
        return ""


def _account_info_text(aid):
    """"当前绑定账号"提示行的文字：显示账号名、登录用户名与改密提示。"""
    if not aid:
        return "当前未绑定账号：凭据需在下方手动填写，或到「管理账号」中添加后选择。"
    try:
        acc = AccountStore().get(aid)
    except Exception:
        acc = None
    if not acc:
        return "当前绑定的账号已不存在，请在上方重新选择一个账号。"
    name = (acc.get("name") or "").strip()
    user = (acc.get("username") or "").strip()
    shown = ("%s · %s" % (name, user)) if (name and name != user) else (name or user)
    return ("当前绑定账号：%s（登录用户名 %s）；在账号管理里改密码会自动生效，"
            "也可在上方下拉里重新绑定其它账号。" % (shown or "未命名", user or "未设置"))


def _refresh_account_info(obj):
    """刷新对话框里的"当前绑定账号"提示行（没有该行时静默跳过）。"""
    var = getattr(obj, "_account_info", None)
    if var is None:
        return
    try:
        var.set(_account_info_text(getattr(obj, "_account_id", "") or ""))
    except Exception:
        pass


def _refresh_account_combo(cb):
    """账号下拉刷新（账号库增删改后调用）。"""
    labels = _account_choices()
    try:
        cb.configure(values=labels)
        if cb.get() not in labels:
            cb.set(ACCOUNT_NONE)
    except Exception:
        pass


def _fill_account(label, set_var):
    """选中账号后把会话**绑定**到该账号。

    表单里不再有独立的用户名/密码输入框（1.4.15 起），凭据一律以账号库
    为准：会话只记 account_id，连接时从账号库实时取用户名和密码，
    于是以后在「工具 → 账号管理」里改一次，所有绑定会话立即生效。
    """
    if not label or label == ACCOUNT_NONE:
        return None
    try:
        a = _account_store().by_label(label)
    except Exception:
        a = None
    if not a:
        return None
    # 记录账号 ID，供保存会话时写入 account_id
    try:
        set_var("account_id", a.get("id", "") or "")
    except Exception:
        pass
    return a


class AccountEditDialog(tk.Toplevel):
    """添加 / 编辑账号：名称、用户名、密码（DPAPI 加密保存）。"""
    def __init__(self, parent, account=None):
        tk.Toplevel.__init__(self, parent)
        self.title("编辑账号" if account else "添加账号")
        self.transient(parent)
        self.resizable(False, False)
        self.result = None
        self.account = dict(account or {})
        self.v = {"name": tk.StringVar(value=self.account.get("name", "")),
                  "username": tk.StringVar(value=self.account.get("username", "")),
                  "password": tk.StringVar(value="")}
        box = ttk.Frame(self, padding=12)
        box.pack(fill="both", expand=True)
        rows = [("name", "名称", False), ("username", "用户名", False),
                ("password", "密码", True)]
        for key, label, secret in rows:
            row = ttk.Frame(box)
            row.pack(fill="x", pady=5)
            ttk.Label(row, text=label, width=10).pack(side="left")
            e = ttk.Entry(row, textvariable=self.v[key], width=28,
                          show="*" if secret else "")
            e.pack(side="left", fill="x", expand=True)
            if secret:
                self.pwd_entry = e
        self.show_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text="显示密码", variable=self.show_var,
                        command=self._toggle_show).pack(anchor="w", pady=(2, 0))
        ttk.Label(box, text="名称留空时自动使用用户名；编辑时密码留空表示不修改。",
                  foreground="#888888", wraplength=280, justify="left").pack(
                      anchor="w", pady=(8, 0))
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=12, pady=10)
        ttk.Button(bar, text="确定", command=self._ok).pack(side="right", padx=4)
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="right")
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()
        self.after(30, self.focus_set)

    def _toggle_show(self):
        self.pwd_entry.configure(show="" if self.show_var.get() else "*")

    def _ok(self):
        user = self.v["username"].get().strip()
        name = self.v["name"].get().strip() or user
        pwd = self.v["password"].get()
        if not user:
            messagebox.showwarning("账号", "用户名（账号）不能为空。", parent=self)
            return
        if not self.account and not pwd:
            messagebox.showwarning("账号", "添加账号必须输入密码。", parent=self)
            return
        out = {"id": self.account.get("id", ""), "name": name, "username": user,
               "password_enc": self.account.get("password_enc", "")}
        if pwd:
            out["password_enc"] = _dpapi(pwd, True)
        self.result = out
        self.destroy()

    def show(self):
        self.wait_window()
        return self.result


class AccountManagerDialog(tk.Toplevel):
    """账号库管理：添加 / 编辑 / 删除。"""
    def __init__(self, parent, on_change=None):
        tk.Toplevel.__init__(self, parent)
        self.title("账号管理")
        self.transient(parent)
        self.geometry("480x360")
        self.minsize(420, 280)
        self.store = _account_store()
        self.on_change = on_change
        box = ttk.Frame(self, padding=10)
        box.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(box, columns=("name", "username"),
                                 show="headings", height=10)
        self.tree.heading("name", text="名称")
        self.tree.heading("username", text="用户名")
        self.tree.column("name", width=180)
        self.tree.column("username", width=180)
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(box, command=self.tree.yview)
        sb.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<Double-1>", lambda e: self._edit())
        bar = ttk.Frame(box)
        bar.pack(side="right", padx=(8, 0), fill="y")
        ttk.Button(bar, text="添加", command=self._add, width=9).pack(pady=3)
        ttk.Button(bar, text="编辑", command=self._edit, width=9).pack(pady=3)
        ttk.Button(bar, text="删除", command=self._del, width=9).pack(pady=3)
        ttk.Button(bar, text="关闭", command=self.destroy, width=9).pack(pady=(14, 3))
        ttk.Label(self, text="密码使用 Windows DPAPI 加密保存在运行目录 accounts.json。",
                  foreground="#888888").pack(anchor="w", padx=12, pady=(0, 8))
        self._refresh()
        self.grab_set()

    def _refresh(self):
        self.tree.delete(*self.tree.get_children())
        for a in self.store.all():
            self.tree.insert("", "end", iid=a.get("id", ""),
                             values=[AccountStore.label(a), a.get("username", "")])
        if callable(self.on_change):
            self.on_change()

    def _unique(self, name, aid):
        return not any((a.get("name") or a.get("username")) == name and
                       a.get("id") != aid for a in self.store.all())

    def _check_saved(self):
        """账号库写盘失败时的即时提示（BUG-04）。"""
        err = _sessions.LAST_ACCOUNT_SAVE_ERROR
        if err:
            messagebox.showwarning("保存失败",
                                   "账号库保存失败：%s\n请检查运行目录是否可写、磁盘是否已满。"
                                   % err, parent=self)

    def _add(self):
        d = AccountEditDialog(self).show()
        if not d:
            return
        if not self._unique(d["name"], ""):
            messagebox.showwarning("账号管理", "账号名称「%s」已存在。" % d["name"],
                                   parent=self)
            return
        self.store.add(d)
        self._check_saved()
        self._refresh()
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("账号管理", "请先选中要编辑的账号。", parent=self)
            return
        a = self.store.get(sel[0])
        if not a:
            return
        d = AccountEditDialog(self, a).show()
        if not d:
            return
        if not self._unique(d["name"], a.get("id")):
            messagebox.showwarning("账号管理", "账号名称「%s」已存在。" % d["name"],
                                   parent=self)
            return
        self.store.update(d)
        self._check_saved()
        self._refresh()

    def _del(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("账号管理", "请先选中要删除的账号。", parent=self)
            return
        a = self.store.get(sel[0])
        if not a:
            return
        if not messagebox.askyesno("账号管理",
                                   "确定删除账号「%s」？" % AccountStore.label(a),
                                   parent=self):
            return
        # 解绑所有引用该账号的会话，避免会话指向已删除的账号
        try:
            from .sessions import unbind_account
            n = unbind_account(a.get("id"))
        except Exception:
            n = 0
        self.store.remove(a.get("id"))
        self._check_saved()
        self._refresh()
        if n:
            messagebox.showinfo("账号管理",
                                "已删除账号，并解绑 %d 个使用该账号的会话。" % n,
                                parent=self)


class AccountSelectDialog(tk.Toplevel):
    """登录时选择账号：下拉选一个已保存账号，返回其用户名与明文密码。"""
    def __init__(self, parent, title="选择账号"):
        tk.Toplevel.__init__(self, parent)
        self.title(title)
        self.transient(parent)
        self.resizable(False, False)
        self.result = None
        self.var = tk.StringVar()
        row = ttk.Frame(self, padding=12)
        row.pack(fill="x")
        ttk.Label(row, text="账号", width=8).pack(side="left")
        self.cb = ttk.Combobox(row, textvariable=self.var, width=26,
                               state="readonly", values=_account_labels())
        self.cb.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="管理账号...", width=11,
                   command=self._manage).pack(side="left", padx=(6, 0))
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=12, pady=10)
        ttk.Button(bar, text="确定", command=self._ok).pack(side="right", padx=4)
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="right")
        labels = _account_labels()
        if labels:
            self.var.set(labels[0])
            self.cb.set(labels[0])
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()

    def _manage(self):
        AccountManagerDialog(self, on_change=lambda: _refresh_account_combo(self.cb))

    def _ok(self):
        label = self.var.get().strip()
        a = _account_store().by_label(label) if label else None
        if not a:
            messagebox.showwarning("选择账号", "请先选择账号（可在「管理账号」中添加）。",
                                   parent=self)
            return
        a["password"] = AccountStore.password(a)
        self.result = a
        self.destroy()

    def show(self):
        self.wait_window()
        return self.result


class SessionDialog(FieldDialog):
    def __init__(self, parent, session, store, on_save=None):
        self.store = store
        self.on_save = on_save
        s = dict(session)
        # 绑定了账号库时不再把明文密码填进表单：密码由账号库提供，
        # 在「工具 → 账号管理」里改一次即可对所有绑定会话生效。
        # 未绑定账号时，手动区展示原密码（用户可留空表示不修改）。
        if s.get("account_id"):
            s["password"] = ""
        else:
            s["password"] = get_password(s)
        # 分组路径改为下拉选择已有分组（仍可手动输入新分组名）
        pages = []
        for page_title, fields in SESSION_PAGES:
            fs = []
            for f in fields:
                item = list(f)
                if item[0] == "folder":
                    item[2] = "combo"
                    item[3] = [""] + [x for x in (store.folders or []) if x != ""]
                fs.append(item)
            pages.append((page_title, fs))
        FieldDialog.__init__(self, parent, "会话属性 - " + s.get("name", ""),
                             pages, s, 700, 580)
        # 分组下拉支持模糊过滤已有分组；输入的新分组保存时自动创建
        cb = self.widgets.get("folder")
        if cb is not None:
            attach_folder_fuzzy(
                cb, lambda: [x for x in (getattr(self.store, "folders", None) or []) if x])

    def _extra(self, nb, outer, page_title):
        if page_title == "SSH / 高级":
            frame = getattr(outer, "inner_frame", outer)
            box = ttk.LabelFrame(frame, text="端口转发 (-L / -R / -D)")
            box.pack(fill="x", padx=10, pady=8)
            cols = ("kind", "addr", "port", "target", "target_port")
            self.fw_tree = ttk.Treeview(box, columns=cols, show="headings", height=4)
            heads = {"kind": "类型", "addr": "监听地址", "port": "端口",
                     "target": "目标主机", "target_port": "目标端口"}
            for c in cols:
                self.fw_tree.heading(c, text=heads[c])
                self.fw_tree.column(c, width=100)
            self.fw_tree.pack(side="left", fill="x", expand=True, padx=4, pady=4)
            for f in self.values.get("forwards") or []:
                self.fw_tree.insert("", "end", values=[f.get(c, "") for c in cols])
            bar = ttk.Frame(box)
            bar.pack(side="right", padx=4)
            ttk.Button(bar, text="添加", command=self._add_forward).pack(pady=2)
            ttk.Button(bar, text="删除", command=self._del_forward).pack(pady=2)

    def _add_forward(self):
        d = ForwardDialog(self)
        f = d.show()
        if f:
            self.fw_tree.insert("", "end", values=[f.get(c, "") for c in
                               ("kind", "addr", "port", "target", "target_port")])

    def _del_forward(self):
        for i in self.fw_tree.selection():
            self.fw_tree.delete(i)

    def collect(self):
        out = FieldDialog.collect(self)
        if hasattr(self, "fw_tree"):
            out["forwards"] = []
            for iid in self.fw_tree.get_children():
                v = self.fw_tree.item(iid, "values")
                out["forwards"].append(dict(zip(("kind", "addr", "port", "target",
                                                 "target_port"), v)))
        plain = out.pop("password", "")
        out["password"] = ""
        from .sessions import _dpapi
        # 绑定了账号库时不保存密码副本（密码跟随账号库）；未绑定才加密存一份
        if out.get("account_id"):
            out["password_enc"] = ""
        else:
            out["password_enc"] = (_dpapi(plain, True) if plain
                                   else self.values.get("password_enc", ""))
        return out

    def _ok(self):
        self._apply()
        if self.on_save and self.result:
            self.on_save(self.result)
        self.destroy()


class ForwardDialog(tk.Toplevel):
    def __init__(self, parent):
        tk.Toplevel.__init__(self, parent)
        self.title("添加端口转发")
        self.transient(parent)
        self.result = None
        self.v = {}
        fields = [("kind", "类型", ["L", "R", "D"]), ("addr", "监听地址", None),
                  ("port", "本地端口", None), ("target", "目标主机", None),
                  ("target_port", "目标端口", None)]
        for key, label, opts in fields:
            r = ttk.Frame(self); r.pack(fill="x", padx=10, pady=3)
            ttk.Label(r, text=label, width=12).pack(side="left")
            if opts:
                var = tk.StringVar(value="L")
                ttk.Combobox(r, textvariable=var, values=opts, state="readonly").pack(side="left")
            else:
                var = tk.StringVar(value="127.0.0.1" if key == "addr" else
                                   ("22" if key == "target_port" else ""))
                ttk.Entry(r, textvariable=var).pack(side="left", fill="x", expand=True)
            self.v[key] = var
        bar = ttk.Frame(self); bar.pack(fill="x", pady=8)
        ttk.Button(bar, text="确定", command=self._ok).pack(side="right", padx=5)
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="right")
        self.grab_set()

    def _ok(self):
        self.result = {k: v.get() for k, v in self.v.items()}
        self.destroy()

    def show(self):
        self.wait_window()
        return self.result


class OptionsDialog(FieldDialog):
    def __init__(self, parent, cfg, on_save=None):
        self.cfg = cfg
        self.on_save = on_save
        values = dict(cfg.values)
        # 运行目录：预填用户指定值；自动模式留空（提示行里显示当前实际目录）
        values.setdefault("data_dir", "")
        values["data_dir"] = _cfg.get_configured_data_dir() or ""
        FieldDialog.__init__(self, parent, "全局选项", OPTION_PAGES,
                             values, 680, 560)

    def _ok(self):
        self.result = FieldDialog.collect(self)
        nd = (self.result.get("data_dir") or "").strip()
        cur = _cfg.get_configured_data_dir() or ""
        changed = os.path.normpath(nd) != os.path.normpath(cur) if (nd and cur) else bool(nd) != bool(cur)
        if changed:
            if nd and not os.path.isdir(nd):
                messagebox.showwarning("运行目录", "目录不存在：%s" % nd, parent=self)
                return
            # 先把最新配置写进旧位置的 settings.json，随后随数据一起迁移
            for k, v in self.result.items():
                if k in self.cfg.values:
                    self.cfg.set(k, v, save=False)
            self.cfg.set("data_dir", nd, save=False)
            self.cfg.save()
            try:
                actual = _cfg.set_configured_data_dir(nd or None)
            except Exception as e:
                messagebox.showwarning("运行目录", "设置失败：%s" % e, parent=self)
                return
            messagebox.showinfo("运行目录",
                                "运行目录已更新：\n%s\n\n数据文件已自动迁移；"
                                "重启 TermX 后完全生效。" % actual, parent=self)
            self.destroy()
            return
        for k, v in self.result.items():
            if k in self.cfg.values:
                self.cfg.set(k, v, save=False)
        self.cfg.save()
        if self.on_save:
            self.on_save()
        self.destroy()


class DataDirDialog(tk.Toplevel):
    """首次运行/覆盖安装：选择或关联运行目录。
    这是软件唯一会创建数据文件夹的入口 —— 完全由用户指定。"""
    def __init__(self, parent):
        tk.Toplevel.__init__(self, parent)
        self.title("选择 TermX 运行目录")
        self.geometry("640x430")
        self.transient(parent)
        self.resizable(False, False)
        self.result = None
        frm = ttk.Frame(self, padding=16)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="请选择 TermX 运行数据的保存目录。\n"
                            "配置、会话、日志、密钥等只会写入该目录；\n"
                            "除该目录外，软件不会在任何其他位置创建文件夹。",
                  justify="left", wraplength=580).pack(anchor="w", pady=(0, 12))
        self.var = tk.StringVar()
        found = _cfg.find_existing_data_dirs()
        suggested = _cfg._suggested_dir()
        opts = [(d, "关联已有数据目录：%s" % d) for d in found]
        if suggested not in found:
            opts.append((suggested, "使用建议目录（首次使用将新建）：%s" % suggested))
        if opts:
            self.var.set(opts[0][0])
        for val, text in opts:
            ttk.Radiobutton(frm, text=text, value=val,
                            variable=self.var).pack(anchor="w", pady=2)
        row = ttk.Frame(frm)
        row.pack(fill="x", pady=(10, 0))
        ttk.Radiobutton(row, text="其他目录：", value="__custom__",
                        variable=self.var).pack(side="left")
        self.custom_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.custom_var).pack(side="left", fill="x",
                                                          expand=True, padx=4)
        ttk.Button(row, text="...", width=3, command=self._browse).pack(side="left")
        ttk.Label(frm, text="提示：覆盖安装时选「关联已有数据目录」即可沿用原来的会话与配置；"
                            "之后也可在 全局选项 → 常规 → 运行目录 里修改。",
                  wraplength=580, justify="left",
                  foreground="#888888").pack(anchor="w", pady=(12, 0))
        bar = ttk.Frame(self, padding=(16, 0, 16, 12))
        bar.pack(fill="x")
        ttk.Button(bar, text="确定", command=self._ok).pack(side="right")
        ttk.Button(bar, text="退出程序", command=self._quit).pack(side="right", padx=6)
        self.grab_set()

    def _browse(self):
        p = filedialog.askdirectory(parent=self)
        if p:
            self.custom_var.set(p)
            self.var.set("__custom__")

    def _ok(self):
        if self.var.get() == "__custom__":
            p = self.custom_var.get().strip()
            if not p:
                messagebox.showwarning("运行目录", "请填写或浏览选择目录", parent=self)
                return
        else:
            p = self.var.get().strip()
        if not p:
            messagebox.showwarning("运行目录", "请选择运行目录", parent=self)
            return
        try:
            self.result = _cfg.set_configured_data_dir(p)
        except Exception as e:
            messagebox.showwarning("运行目录", "设置失败：%s" % e, parent=self)
            return
        self.destroy()

    def _quit(self):
        self.result = None
        self.destroy()


class ThemeDialog(tk.Toplevel):
    """卡片式主题选择器：点一张卡片就即时切换，窗口不关闭，方便连续对比。"""
    SWATCH_KEYS = ("toolbar", "panel_hdr", "accent", "tab_sel", "tree_bg")
    BOLD = ("Microsoft YaHei UI", 10, "bold")
    TITLE_FONT = ("Microsoft YaHei UI", 12, "bold")
    MONO = ("Consolas", 9)

    def __init__(self, parent, cfg, current_key, on_pick, on_close=None):
        tk.Toplevel.__init__(self, parent)
        self.title("切换界面主题")
        self.transient(parent)
        self.resizable(False, False)
        self.geometry("700x730")
        self.cfg = cfg
        self.on_pick = on_pick
        self.on_close = on_close
        self.current = current_key
        self.cards = {}
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda e: self._close())

        # 底部栏先占位，避免卡片区 expand 后把它挤出可视区
        foot = tk.Frame(self)
        foot.pack(fill="x", side="bottom", padx=14, pady=8)
        self.sync_var = tk.BooleanVar(value=bool(cfg.get("theme_sync_scheme", True)))
        chk = tk.Checkbutton(foot, text="切换主题时同步套用该主题自带的终端配色",
                             variable=self.sync_var, command=self._sync_toggle)
        chk.pack(side="left")
        ttk.Button(foot, text="关闭", command=self._close).pack(side="right")
        self.foot_frame = foot
        self.foot_chk = chk

        head = tk.Frame(self)
        head.pack(fill="x", padx=16, pady=(14, 4))
        self.title_lbl = tk.Label(head, text="界面主题", font=self.TITLE_FONT, anchor="w")
        self.title_lbl.pack(side="left")
        self.hint_lbl = tk.Label(head, text="点击卡片即时预览 · 选择会被记住",
                                 font=ui_theme.SMALL_FONT, anchor="e")
        self.hint_lbl.pack(side="right")
        self.head_frame = head

        grid = tk.Frame(self)
        grid.pack(fill="both", expand=True, padx=14, pady=6)
        for col in range(2):
            grid.columnconfigure(col, weight=1)
        for row in range(3):
            grid.rowconfigure(row, weight=1)
        self.grid_frame = grid

        for idx, (key, label) in enumerate(ui_theme.THEME_LABELS):
            t = ui_theme.THEMES[key]
            scheme = SCHEMES.get(t.get("scheme", ""), {})
            tbg = scheme.get("bg", "#000000")
            ansi = scheme.get("ansi") or ["#000000"] * 16

            card = tk.Frame(grid, relief="solid", bd=1, cursor="hand2")
            card.grid(row=idx // 2, column=idx % 2, padx=6, pady=6, sticky="nsew")
            name = tk.Label(card, text=label, font=self.BOLD, anchor="w")
            name.pack(fill="x", padx=10, pady=(8, 0))
            ref = tk.Label(card, text=t.get("ref", ""), font=ui_theme.SMALL_FONT,
                           anchor="nw", wraplength=290, justify="left", height=2)
            ref.pack(fill="x", padx=10, pady=(2, 6))

            sw = tk.Frame(card)
            sw.pack(fill="x", padx=10, pady=(0, 6))
            for k in self.SWATCH_KEYS:
                f = tk.Frame(sw, width=34, height=12, bd=0)
                f.pack(side="left", padx=(0, 3))
                f.pack_propagate(False)
                f.configure(bg=t[k])

            term = tk.Frame(card, height=46, bd=0, bg=tbg)
            term.pack(fill="x", padx=10, pady=(0, 4))
            term.pack_propagate(False)
            line = tk.Frame(term, bg=tbg)
            line.pack(fill="x", padx=6, pady=(6, 0))
            l1 = tk.Label(line, text="$ ls -la", bg=tbg,
                          fg=ansi[10] if len(ansi) > 10 else "#00FF00",
                          font=self.MONO, anchor="w")
            l1.pack(side="left")
            l2 = tk.Label(term, text="  config.ini   README.md", bg=tbg,
                          fg=ansi[12] if len(ansi) > 12 else "#5C9EFF",
                          font=self.MONO, anchor="w")
            l2.pack(fill="x", padx=6)

            badge = tk.Label(card, text="", font=ui_theme.SMALL_FONT, anchor="w")
            badge.pack(fill="x", padx=10, pady=(0, 8))
            for w in (card, name, ref, sw, term, line, l1, l2, badge):
                w.bind("<Button-1>", lambda e, k=key: self._pick(k))
            self.cards[key] = dict(frame=card, name=name, ref=ref, sw=sw,
                                   badge=badge, theme=t, scheme_name=t.get("scheme", ""))

        self.refresh(current_key)
        self.after(120, self.focus_force)

    def _pick(self, key):
        self.current = key
        if self.on_pick:
            self.on_pick(key)
        self.refresh(key)

    def _sync_toggle(self):
        self.cfg.set("theme_sync_scheme", bool(self.sync_var.get()))
        self.cfg.save()

    def _close(self):
        if self.on_close:
            try:
                self.on_close()
            except Exception:
                pass
        self.destroy()

    def refresh(self, current_key):
        """当前主题变化时只重绘外壳，卡片里的色块/终端预览保持各自主题的原色。"""
        self.current = current_key
        c = ui_theme.THEMES.get(current_key, ui_theme.CRT)
        try:
            self.configure(bg=c["bg"])
            for w in (self.head_frame, self.grid_frame, self.foot_frame):
                w.configure(bg=c["bg"])
            self.title_lbl.configure(bg=c["bg"], fg=c["fg"])
            self.hint_lbl.configure(bg=c["bg"], fg=c["muted"])
            self.foot_chk.configure(bg=c["bg"], fg=c["fg"],
                                    activebackground=c["bg"], activeforeground=c["fg"],
                                    selectcolor=c["entry_bg"])
        except Exception:
            pass
        for key, w in self.cards.items():
            selected = (key == current_key)
            try:
                bg = c["panel"]
                w["frame"].configure(bg=bg,
                                     highlightbackground=c["accent"] if selected else c["border"],
                                     highlightcolor=c["accent"] if selected else c["border"],
                                     highlightthickness=2 if selected else 1)
                w["name"].configure(bg=bg, fg=c["accent"] if selected else c["fg"])
                w["ref"].configure(bg=bg, fg=c["muted"])
                w["sw"].configure(bg=bg)
                w["badge"].configure(bg=bg, fg=c["accent"] if selected else c["muted"],
                                     text="● 当前使用" if selected
                                     else "终端：" + w["scheme_name"])
            except Exception:
                pass


class QuickConnectDialog(tk.Toplevel):
    """快速连接：按协议切换表单，Serial 使用专用串口参数界面。"""
    HINTS = {
        "SSH2": "使用随包内置 OpenSSH 客户端，无需系统安装任何组件。",
        "SSH1": "SSH1 走内置 plink，公钥认证需要 .ppk 格式私钥。",
        "Telnet": "内置纯 Python Telnet 客户端，自动处理 IAC 协商。",
        "Rlogin": "内置 Rlogin 客户端，适用于老式 Unix 主机。",
        "Raw": "原始 TCP 连接：不做任何协议协商，字节流直达终端。",
        "Serial": "内置串口驱动，无需额外组件；连接前请确认 COM 编号。",
        "LocalShell": "本机 Shell：可指定 cmd.exe / powershell.exe / wsl.exe。",
        "SFTP": "使用随包内置 sftp.exe，连接后可在文件传输窗口操作。",
    }

    def __init__(self, parent, cfg, on_connect, store=None):
        tk.Toplevel.__init__(self, parent)
        self.title("快速连接")
        self.transient(parent)
        self.geometry("500x505")
        self.minsize(470, 480)
        self.result = None
        self.cfg = cfg
        self.store = store
        self.v = {}
        self.active_keys = []
        self._built_proto = None
        self._account_id = ""      # 当前绑定的账号库 ID（随会话保存）

        # 底部按钮先 pack，保证窗口缩放时永远不会被表单挤出可视区
        bar = ttk.Frame(self)
        bar.pack(fill="x", side="bottom", pady=10)
        ttk.Button(bar, text="连接", command=self._ok).pack(side="right", padx=6)
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="right")

        head = ttk.Frame(self)
        head.pack(fill="x", padx=14, pady=(14, 4))
        ttk.Label(head, text="协议", width=13).pack(side="left")
        last = (cfg.get("quick_connect_last") or {}) if cfg else {}
        last_proto = (cfg.get("quick_connect_proto") or "SSH2") if cfg else "SSH2"
        self.v["protocol"] = tk.StringVar(value=last_proto if last_proto in PROTOCOLS else "SSH2")
        cb = ttk.Combobox(head, textvariable=self.v["protocol"], values=PROTOCOLS)
        cb.pack(side="left", fill="x", expand=True)
        self.v["protocol"].trace_add("write", lambda *_: self._on_proto_change())

        self.body = ttk.Frame(self)
        self.body.pack(fill="x", padx=14, pady=2)
        self.hint = ttk.Label(self, text="", wraplength=460, justify="left",
                              foreground="#5A5A5A")
        self.hint.pack(fill="x", padx=14, pady=(6, 2))
        # 分组 + 保存选项（需求：新建/快速连接默认保存到会话列表）
        self.last_values = (last.get(self.v["protocol"].get()) or {}) if isinstance(last, dict) else {}
        grp_row = ttk.Frame(self)
        grp_row.pack(fill="x", padx=14, pady=(6, 0))
        ttk.Label(grp_row, text="分组", width=13).pack(side="left")
        folders = [""]
        if store is not None:
            try:
                folders += [f for f in (store.folders or []) if f]
            except Exception:
                pass
        self.v["folder"] = tk.StringVar(value=self.last_values.get("folder", ""))
        self.folder_cb = ttk.Combobox(grp_row, textvariable=self.v["folder"], values=folders)
        self.folder_cb.pack(side="left", fill="x", expand=True)
        if store is not None:
            attach_folder_fuzzy(
                self.folder_cb,
                lambda: [f for f in (getattr(store, "folders", None) or []) if f])
        self.save_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(self, text="保存到会话列表（同名会话自动复用）",
                        variable=self.save_var).pack(anchor="w", padx=14, pady=4)
        self.bind("<Escape>", lambda e: self.destroy())
        self._rebuild()
        self.grab_set()
        self.after(100, lambda: self.focus_force())

    def _remembered(self, key, proto):
        """上一次该协议使用的值（密码经 DPAPI 加密存储）。"""
        try:
            last = (self.cfg.get("quick_connect_last") or {}) if self.cfg else {}
            vals = last.get(proto) or {}
            if key == "password":
                enc = vals.get("password_enc", "")
                if enc:
                    from .sessions import _dpapi
                    return _dpapi(enc, False)
                return ""
            return vals.get(key, None)
        except Exception:
            return None

    def _default(self, key, proto):
        remembered = self._remembered(key, proto)
        if remembered is not None:
            return remembered
        if key == "port":
            return {"Telnet": "23", "Rlogin": "513", "Raw": "23"}.get(proto, "22")
        if key == "encoding":
            return (self.cfg.get("encoding", "UTF-8") if self.cfg else "UTF-8")
        return {
            "serial_port": "COM1", "serial_baud": "115200", "serial_data": "8",
            "serial_parity": "N", "serial_stop": "1", "serial_flow": "none",
            "shell_path": "", "shell_args": "",
        }.get(key, "")

    def _on_proto_change(self):
        if self.v["protocol"].get() != self._built_proto:
            self._rebuild()

    def _rebuild(self):
        proto = self.v["protocol"].get()
        self._built_proto = proto
        self._account_cb = None       # 串口 / 本地 Shell 不显示账号选择
        for w in self.body.winfo_children():
            w.destroy()
        self.hint.configure(text=self.HINTS.get(proto, ""))
        if proto == "Serial":
            fields = [
                ("serial_port", "串口 (COM)", "combo", SERIAL_PORTS),
                ("serial_baud", "波特率", "combo", SERIAL_BAUD),
                ("serial_data", "数据位", "combo", ["5", "6", "7", "8"]),
                ("serial_parity", "校验位", "combo", PARITY),
                ("serial_stop", "停止位", "combo", ["1", "1.5", "2"]),
                ("serial_flow", "流控", "combo", FLOW),
                ("encoding", "编码", "combo", ENCODINGS),
            ]
        elif proto == "LocalShell":
            fields = [
                ("shell_path", "程序路径", "entry", None),
                ("shell_args", "启动参数", "entry", None),
                ("encoding", "编码", "combo", ENCODINGS),
            ]
        else:
            fields = [
                ("__account__", "账号(选择)", "account", None),
                ("host", "主机名 / IP", "entry", None),
                ("port", "端口", "entry", None),
                ("encoding", "编码", "combo", ENCODINGS),
            ]
        self.active_keys = [f[0] for f in fields if f[0] != "__account__"]
        for key, label, kind, opts in fields:
            if kind == "account":
                row = ttk.Frame(self.body)
                row.pack(fill="x", pady=5)
                ttk.Label(row, text=label, width=13).pack(side="left")
                if "account" not in self.v:
                    self.v["account"] = tk.StringVar()
                cb = ttk.Combobox(row, textvariable=self.v["account"],
                                  state="readonly", values=_account_choices())
                cb.pack(side="left", fill="x", expand=True)
                cb.bind("<<ComboboxSelected>>",
                        lambda e, c=cb: self._apply_account(c.get()))
                ttk.Button(row, text="管理账号...", width=11,
                           command=lambda c=cb: self._manage_accounts(c)).pack(
                               side="left", padx=(4, 0))
                self._account_cb = cb
                # 只读提示行：显示当前选中的账号与登录用户名
                info = ttk.Frame(self.body)
                info.pack(fill="x", padx=13, pady=(0, 3))
                self._account_info = tk.StringVar(value="")
                ttk.Label(info, textvariable=self._account_info, wraplength=430,
                          justify="left", foreground="#5A5A5A").pack(anchor="w")
                _refresh_account_info(self)
                # 手动凭据区：默认收起，选「（手动输入用户名/密码）」时展开
                self._manual_frame = ttk.Frame(self.body)
                for mkey, mlabel, mkind in (("username", "用户名", "entry"),
                                            ("password", "密码", "password")):
                    mrow = ttk.Frame(self._manual_frame)
                    mrow.pack(fill="x", pady=5)
                    ttk.Label(mrow, text=mlabel, width=13).pack(side="left")
                    mvar = tk.StringVar(
                        value=str(self._default(mkey, proto) or ""))
                    ttk.Entry(mrow, textvariable=mvar,
                              show="*" if mkind == "password" else "").pack(
                                  side="left", fill="x", expand=True)
                    self.v[mkey] = mvar
                self._toggle_manual(False)
                continue
            row = ttk.Frame(self.body)
            row.pack(fill="x", pady=5)
            ttk.Label(row, text=label, width=13).pack(side="left")
            var = self.v.get(key)
            if var is None:
                var = tk.StringVar(value=self._default(key, proto))
                self.v[key] = var
            if kind == "combo":
                combo = ttk.Combobox(row, textvariable=var, values=list(opts or []))
                combo.pack(side="left", fill="x", expand=True)
                if key == "serial_port":
                    self._port_cb = combo
                    ttk.Button(row, text="检测串口", width=9,
                               command=self._detect_ports).pack(side="left", padx=(4, 0))
            else:
                ttk.Entry(row, textvariable=var,
                          show="*" if kind == "password" else "").pack(
                              side="left", fill="x", expand=True)

    def _toggle_manual(self, visible):
        """显示/隐藏手动填写的用户名、密码（仅"不使用账号库"时可见）。"""
        frame = getattr(self, "_manual_frame", None)
        if frame is None:
            return
        try:
            if visible:
                frame.pack(fill="x", padx=0, pady=0)
            else:
                frame.pack_forget()
        except Exception:
            pass

    def _apply_account(self, label):
        """账号下拉变化：绑定账号，或切换到手动输入。

        - 选中某个账号 -> 记录 account_id，隐藏手动区（凭据由账号库提供）；
        - 选"（不使用账号库）" -> 清空 account_id，显示手动区。
        """
        if not label or label in (ACCOUNT_NONE, ACCOUNT_STALE):
            self._account_id = ""
            self._toggle_manual(label == ACCOUNT_NONE)
            _refresh_account_info(self)
            return None
        a = _fill_account(label, lambda k, v: setattr(self, "_account_id", v or ""))
        if a is None:
            self._account_id = ""
            self._toggle_manual(True)
        else:
            self._toggle_manual(False)
        _refresh_account_info(self)
        return a

    def _manage_accounts(self, cb):
        AccountManagerDialog(self, on_change=lambda: _refresh_account_combo(cb))

    def _detect_ports(self):
        ports = detect_serial_ports()
        combo = getattr(self, "_port_cb", None)
        if combo is not None and combo.winfo_exists():
            combo.configure(values=ports or SERIAL_PORTS)
        if ports:
            self.v["serial_port"].set(ports[0])
            self.hint.configure(text="检测到本机串口：" + "、".join(ports) +
                                "。选择后点「连接」即可打开。")
        else:
            self.hint.configure(text="本机暂未检测到可用串口（插上 USB 转串口可点此重新检测）；"
                                "也可手动填写 COM 编号，列表保留 COM1-COM32。")

    def _ok(self):
        d = {"protocol": self.v["protocol"].get()}
        for k in self.active_keys:
            d[k] = self.v[k].get()
        d["folder"] = self.v["folder"].get() if "folder" in self.v else ""
        d["save"] = bool(self.save_var.get())
        if d["protocol"] == "Serial":
            for k in ("serial_baud", "serial_data"):
                try:
                    d[k] = int(float(d.get(k) or 0))
                except Exception:
                    d[k] = 0
            try:
                d["serial_stop"] = float(d.get("serial_stop") or 1)
            except Exception:
                d["serial_stop"] = 1
        else:
            try:
                d["port"] = int(d.get("port") or 22)
            except Exception:
                d["port"] = 22
        # 账号引用：绑定了账号就带上 account_id，用户名取自账号库，
        # 密码不复制（连接时实时从账号库取，改一次全部生效）
        d["account_id"] = self._account_id or ""
        manual_user = self.v["username"].get() if "username" in self.v else ""
        manual_pwd = self.v["password"].get() if "password" in self.v else ""
        if d["account_id"]:
            try:
                from .sessions import get_username
                d["username"] = get_username(d) or manual_user
            except Exception:
                d["username"] = manual_user
            d["password"] = ""
        else:
            d["username"] = manual_user
            d["password"] = manual_pwd
        # 记住本次输入（含加密后的密码），下次打开默认填入
        if self.cfg:
            try:
                memo = {}
                for k in self.active_keys:
                    if k == "password":
                        continue
                    memo[k] = d.get(k)
                memo["username"] = d.get("username", "")
                memo["account_id"] = d.get("account_id", "")
                pwd = d.get("password") or ""
                if pwd:
                    from .sessions import _dpapi
                    memo["password_enc"] = _dpapi(pwd, True)
                memo["folder"] = d.get("folder", "")
                all_last = dict(self.cfg.get("quick_connect_last") or {})
                all_last[d["protocol"]] = memo
                self.cfg.set("quick_connect_last", all_last)
                self.cfg.set("quick_connect_proto", d["protocol"])
            except Exception:
                pass
        self.result = d
        self.destroy()


class FindDialog(tk.Toplevel):
    def __init__(self, parent, on_find):
        tk.Toplevel.__init__(self, parent)
        self.title("查找")
        self.transient(parent)
        self.geometry("360x120")
        self.on_find = on_find
        self.var = tk.StringVar()
        ttk.Label(self, text="查找内容:").pack(anchor="w", padx=10, pady=(10, 2))
        e = ttk.Entry(self, textvariable=self.var)
        e.pack(fill="x", padx=10)
        e.focus_set()
        e.bind("<Return>", lambda ev: self._next())
        bar = ttk.Frame(self); bar.pack(fill="x", pady=10)
        ttk.Button(bar, text="查找下一个", command=self._next).pack(side="right", padx=6)
        ttk.Button(bar, text="关闭", command=self.destroy).pack(side="right")
        self.bind("<Escape>", lambda e: self.destroy())

    def _next(self):
        self.on_find(self.var.get())


class AboutDialog(tk.Toplevel):
    def __init__(self, parent):
        tk.Toplevel.__init__(self, parent)
        self.title("关于 TermX")
        self.transient(parent)
        self.geometry("640x520")
        txt = tk.Text(self, wrap="word", padx=14, pady=12)
        sb = ttk.Scrollbar(self, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(fill="both", expand=True)
        txt.insert("1.0", ABOUT_TEXT % (APP_VER, APP_BUILD))
        txt.configure(state="disabled")
        ttk.Button(self, text="关闭", command=self.destroy).pack(pady=8)


ABOUT_TEXT = """TermX 轻量终端 %s · %s
绿色免安装 · 零注册表写入 · 零外部依赖
所有运行组件随包携带：解压后直接可用，无需安装/下载任何东西。

【界面主题】查看 → 切换主题（或按 F9）打开卡片式主题选择器，点击即时生效：
 · SecureCRT 经典 / 浅色 / 深色（基础三套）
 · Windows Terminal · 午夜 / MobaXterm · 晴空 / iTerm2 · Solarized（新增三套）
每套主题同时包含窗口外壳配色、终端 16 色、光标样式；
可在全局选项 → 外观取消「切换主题时同步终端配色」以单独固定自己的配色。

【随包自带组件】TermX_Bin/
 · openssh/  OpenSSH 全套：ssh / scp / sftp / ssh-keygen / ssh-add / ssh-agent / ssh-keyscan
 · runtime/   VC++ 运行库：msvcp140 / vcruntime140 等
 · ssh1/      SSH-1 客户端 plink（PuTTY）
 · x11/       便携 X Server，X11 转发时自动启动，无需另装 X Server
 · fonts/     开源等宽字体（私有方式加载，不写入系统字体）
 另有纯 Python 实现：Telnet / Rlogin / Raw TCP / 串口、Zmodem 协议栈、
 VBScript/JScript 脚本引擎。全部在软件内部，不调用系统外部工具。

【连接协议】SSH1/SSH2、Telnet、Rlogin、Raw TCP、串口、本地 Shell(PowerShell/CMD/WSL)、SFTP
【终端仿真】VT100/VT220/VT320/xterm/ANSI/SCOANSI、256 色与真彩、回滚缓冲、备用屏幕、
            多编码(UTF-8/GBK/Big5/Shift-JIS...)、OSC 标题、鼠标报告、括号粘贴
【会话管理】分组树、收藏、搜索、快速连接、克隆、导入导出、SecureCRT .ini 会话导入
【SSH 特性】密码/公钥/键盘交互、密钥文件、X11/Agent 转发、本地/远程/动态端口转发、
            主机密钥策略、压缩、保活、SOCKS5/HTTP 代理、SFTP/SCP 文件传输
【自动化】批量命令下发(并发/顺序、变量替换、等待提示符、超时、结果导出)、
          Python 脚本、VBScript/JScript 脚本(内置引擎，可切系统 WSH)、命令脚本、
          Expect/Send 登录脚本、宏、按键映射、录制、防空闲、自动重连
【其它】多标签、查找与关键字高亮、会话日志(时间戳/剥离转义)、发送字符串按钮栏、
        标签重命名/配色、锁定会话、克隆会话、屏幕文本导出、清屏、状态栏、
        广播输入(Chat Window)、自定义高亮/按钮栏/宏/按键映射、操作录制

【兼容性说明】
 · SSH1 由随包 plink 提供（PuTTY 私钥需 .ppk 格式）
 · Zmodem 协议栈已内置，自动收发生效；服务端只需 lrzsz 的 rz/sz 命令
 · X11 转发会自动启动随包 X Server；密码/公钥交互由随包 OpenSSH 处理
 · VBScript/JScript 默认由内置引擎执行（覆盖 SecureCRT 常用语法与 crt API）；
   全局选项可切到系统 WSH 引擎以获得完整语言语法；Python 脚本提供完整 crt 对象
 · 本地 Shell 使用 Windows 自带的 cmd.exe / PowerShell，属于操作系统本身
 · SSH 配置与 known_hosts 落在数据目录 TermX/ssh，不污染用户 .ssh 目录

【常用快捷键】
 Ctrl+T 新建会话标签      Ctrl+W 关闭标签       Ctrl+Tab 切换标签
 Ctrl+Shift+C/V 复制/粘贴  Ctrl+Insert / Shift+Insert 复制/粘贴
 Ctrl+F 查找              Ctrl+L 清屏           Ctrl+Home/End 回滚首尾
 Ctrl++ / Ctrl+- 字号缩放  PgUp/PgDn 回滚       F11 批量下发窗口
 Alt+1..9 切换到第 N 个标签

【绿色说明】所有配置、会话、日志默认保存在 D 盘根目录的 TermX 文件夹下
（D 盘不可用时自动落到系统 AppData，绝不在安装包/exe 同目录生成运行文件夹）；
运行目录可在 全局选项 → 常规 → 运行目录 自定义，覆盖安装时可关联指定目录，
不写注册表，已有配置自动沿用。
SSH/SFTP 使用 Windows 自带 OpenSSH；本地终端使用 ConPTY 伪控制台。
"""


def _read_csv_rows(path):
    """读 CSV，兼容 Excel 编辑后常见的编码退化与分隔符变化。

    Excel 在中文 Windows 上把 CSV 另存为 ANSI/GBK，或按区域设置用分号/
    制表符分隔、另存为 UTF-16 文本，都会让「用 utf-8-sig 硬读」把中文
    表头读成乱码（导致找不到「主机」列）。这里逐个编码尝试，并用
    csv.Sniffer 探测分隔符，选出表头里含中文列名/英文别名最多的那个结果。
    """
    import csv
    with open(path, "rb") as f:
        raw = f.read()
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        encodings = ("utf-16", "utf-8-sig", "gbk")
    else:
        encodings = ("utf-8-sig", "gbk", "utf-16", "latin-1")
    want = ("名称", "主机", "端口", "协议", "用户名", "密码", "分组",
            "name", "host", "ip", "port", "user", "password")
    best, best_score = None, -1
    for enc in encodings:
        try:
            text = raw.decode(enc)
        except Exception:
            continue
        sample = text[:4096]
        delimiters = [","]
        # Sniffer 对短文本容易误判，所以所有候选分隔符都逐一尝试，
        # 再按"表头单元格精确命中列名的个数"打分选最优。
        try:
            sniffed = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
            if sniffed not in delimiters:
                delimiters.append(sniffed)
        except Exception:
            pass
        for d in ",;\t|":
            if d not in delimiters:
                delimiters.append(d)
        for d in delimiters:
            try:
                rows = [r for r in csv.reader(text.splitlines(), delimiter=d)
                        if any((c or "").strip() for c in r)]
            except Exception:
                continue
            if not rows:
                continue
            head = [(c or "").strip().lower() for c in rows[0]]
            score = sum(1 for w in want if w in head)
            # 列数更多说明确实被正确拆开，避免整行挤成一列却"包含"列名
            score += min(len(head), len(want)) * 0.01
            if score > best_score:
                best, best_score = rows, score
    return best if best is not None else []


def read_table_file(path):
    """读取设备列表文件，返回 (headers, rows)。

    支持 .xlsx（纯标准库解析：zip + XML，无需 openpyxl）与 .csv。
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        rows = _read_csv_rows(path)
    elif ext == ".xlsx":
        rows = _read_xlsx(path)
    elif ext == ".xls":
        raise ValueError("旧版 .xls 请先在 Excel 中另存为 .xlsx 或 .csv")
    else:
        raise ValueError("仅支持 .xlsx / .csv")
    if not rows:
        return [], []
    # 去掉可能的说明行/空列，统一为字符串
    rows = [[("" if c is None else str(c)).strip() for c in r] for r in rows]
    # 首行若不是表头（用户插入了说明行），尝试往下找含「主机」的那一行
    def is_header(cells):
        low = " ".join((c or "").lower() for c in cells)
        return any(w in low for w in ("主机", "host", "ip", "地址"))
    if not is_header(rows[0]):
        for i, r in enumerate(rows[1:6], 1):
            if is_header(r):
                rows = rows[i:]
                break
    headers = [h for h in rows[0]]
    return headers, rows[1:]


def _read_xlsx(path):
    """最小化 xlsx 读取：首个工作表的字符串值网格。"""
    import zipfile
    import xml.etree.ElementTree as ET
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall("m:si", ns):
                shared.append("".join(t.text or "" for t in si.iter(
                    "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")))
        # 取第一个工作表
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        sheet_name = None
        for sh in wb.findall("m:sheets/m:sheet", ns):
            sheet_name = sh.get("name")
            break
        target = "xl/worksheets/sheet1.xml"
        if sheet_name and "xl/worksheets/%s.xml" % sheet_name in z.namelist():
            target = "xl/worksheets/%s.xml" % sheet_name
        root = ET.fromstring(z.read(target))
        grid = []
        for row in root.findall("m:sheetData/m:row", ns):
            cells = {}
            for c in row.findall("m:c", ns):
                ref = c.get("r") or ""
                col_letters = "".join(ch for ch in ref if ch.isalpha()) or "A"
                idx = 0
                for ch in col_letters:
                    idx = idx * 26 + (ord(ch.upper()) - 64)
                t = c.get("t")
                if t == "s":
                    v = c.find("m:v", ns)
                    val = shared[int(v.text)] if v is not None and v.text else ""
                elif t == "inlineStr":
                    is_el = c.find("m:is", ns)
                    val = "".join(x.text or "" for x in is_el.iter(
                        "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")) \
                        if is_el is not None else ""
                else:
                    v = c.find("m:v", ns)
                    val = v.text if v is not None and v.text is not None else ""
                cells[idx] = val
            if cells:
                width = max(cells)
                grid.append([cells.get(i, "") for i in range(1, width + 1)])
        return grid


def _col_letters(n):
    """把 1-based 列号转成 Excel 列字母（1->A, 27->AA）。"""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def write_xlsx_table(path, rows, sheet_name="Sheet1"):
    """用标准库写出最小 xlsx（inlineStr 单元格），无需 openpyxl 等第三方库。

    与 _read_xlsx() 对称：所有单元格写成 inlineStr，Excel / WPS 可直接打开，
    编辑保存后中文表头不会因 ANSI/GBK 编码退化而丢失。
    """
    import zipfile
    from xml.sax.saxutils import escape

    def cell(ref, value):
        text = "" if value is None else str(value)
        if text == "":
            return '<c r="%s"/>' % ref
        return ('<c r="%s" t="inlineStr"><is><t xml:space="preserve">%s</t>'
                '</is></c>' % (ref, escape(text)))

    body = []
    for ri, row in enumerate(rows, 1):
        cells = "".join(cell("%s%d" % (_col_letters(ci), ri), v)
                        for ci, v in enumerate(row, 1))
        body.append('<row r="%d">%s</row>' % (ri, cells))

    sheet = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/'
             'spreadsheetml/2006/main"><sheetData>%s</sheetData></worksheet>'
             % "".join(body))
    content_types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
                     'content-types">'
                     '<Default Extension="rels" ContentType="application/'
                     'vnd.openxmlformats-package.relationships+xml"/>'
                     '<Default Extension="xml" ContentType="application/xml"/>'
                     '<Override PartName="/xl/workbook.xml" ContentType="application/'
                     'vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                     '<Override PartName="/xl/worksheets/sheet1.xml" ContentType='
                     '"application/vnd.openxmlformats-officedocument.spreadsheetml.'
                     'worksheet+xml"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/'
            '2006/relationships"><Relationship Id="rId1" Type="http://'
            'schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'officeDocument" Target="xl/workbook.xml"/></Relationships>')
    wb = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/'
          '2006/main" xmlns:r="http://schemas.openxmlformats.org/'
          'officeDocument/2006/relationships"><sheets><sheet name="%s" '
          'sheetId="1" r:id="rId1"/></sheets></workbook>' % escape(sheet_name))
    wb_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<Relationships xmlns="http://schemas.openxmlformats.org/'
               'package/2006/relationships"><Relationship Id="rId1" Type="http://'
               'schemas.openxmlformats.org/officeDocument/2006/relationships/'
               'worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return path


def write_device_template(path):
    """生成设备导入模板（xlsx）。

    1.4.16 起改用 xlsx：CSV 模板被 Excel 编辑保存后会退化为 ANSI/GBK，
    中文表头乱码导致「缺少主机列」导入失败；xlsx 内部是 UTF-8 XML，
    无论怎么编辑都不会丢编码。
    """
    headers = ["名称", "主机", "端口", "协议", "用户名", "密码", "分组"]
    sample = [
        ["核心交换机-1F", "192.168.1.2", "22", "SSH2", "admin", "Admin@123", "交换机"],
        ["出口路由器", "192.168.1.1", "22", "SSH2", "admin", "", "路由器"],
        ["楼层接入-AP", "192.168.10.5", "22", "SSH2", "admin", "", "无线"],
    ]
    return write_xlsx_table(path, [headers] + sample, sheet_name="设备列表")


def write_variable_template(path):
    """生成导入变量 CSV 模板：首行是变量名，每行对应一台设备。

    命令里用 {变量名} / ${变量名} / %变量名% 引用；内置变量 name/host/ip/
    user/port/index/no/protocol/folder 不需要写在表里。
    """
    import csv
    headers = ["loopback", "vlan", "uplink", "备注"]
    sample = [
        ["10.0.0.1", "100", "GigabitEthernet0/1", "示例设备1"],
        ["10.0.0.2", "200", "GigabitEthernet0/2", "示例设备2"],
    ]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(sample)
    return path


def import_devices_from_table(store, path):
    """把表格导入为会话；返回 (新增数, 提示信息)。列：名称/主机/端口/协议/用户名/密码/分组。"""
    headers, rows = read_table_file(path)
    if not rows:
        return 0, "文件中没有数据行"
    low = [h.strip().lower() for h in headers]
    # 列索引映射（找不到主机列则报错）
    def col(*names):
        for n in names:
            if n in low:
                return low.index(n)
        return -1
    i_name = col("名称", "设备名称", "name")
    i_host = col("主机", "ip", "地址", "host")
    i_port = col("端口", "port")
    i_proto = col("协议", "protocol")
    i_user = col("用户名", "username", "user")
    i_pwd = col("密码", "password")
    i_grp = col("分组", "分组名称", "group", "folder")
    if i_host < 0:
        return 0, "缺少「主机/IP」列，请使用导入模板"
    added = 0
    for r in rows:
        def get(i):
            return r[i] if 0 <= i < len(r) else ""
        host = get(i_host)
        if not host:
            continue
        proto = (get(i_proto) or "SSH2").strip() or "SSH2"
        if proto not in PROTOCOLS:
            proto = "SSH2"
        port = get(i_port)
        try:
            port = int(float(port)) if port else new_session(proto).get("port", 22)
        except Exception:
            port = 22
        name = get(i_name) or host
        s = new_session(proto, name)
        s["host"] = host
        s["port"] = port
        if i_user >= 0 and get(i_user):
            s["username"] = get(i_user)
        if i_pwd >= 0 and get(i_pwd):
            set_password(s, get(i_pwd))
        grp = (get(i_grp) if i_grp >= 0 else "").strip()
        if grp:
            s["folder"] = grp
        store.add(s)
        added += 1
    return added, ""


class PagingVendorDialog(tk.Toplevel):
    """管理"关闭分页"的厂商与命令：可添加、编辑、删除、恢复内置厂商。

    数据落在运行目录的 settings.json：
      paging_vendors  自建/覆盖项 {"厂商名": "命令"}
      paging_deleted  已删除的内置厂商名（黑名单）
    """
    COLS = (("vendor", "厂商", 150), ("cmd", "命令", 330), ("src", "来源", 110))

    def __init__(self, parent, controller):
        tk.Toplevel.__init__(self, parent)
        self.title("管理关闭分页厂商")
        self.geometry("700x430")
        self.transient(parent)
        self.controller = controller
        self._build()
        self._reload()
        try:
            self.grab_set()
        except Exception:
            pass

    def _build(self):
        ttk.Label(self, justify="left", foreground="#5A5A5A",
                  text="下面这些厂商会出现在批量下发窗口的“关闭分页”下拉框中。\n"
                       "选中某个厂商后，每台设备连接并确认登录后会先下发该命令，"
                       "再执行你填写的批量命令。\n"
                       "命令均在用户视图执行，只对当前会话生效，不会写入设备配置。"
                  ).pack(fill="x", padx=10, pady=(8, 4))

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=10, pady=4)
        self.tree = ttk.Treeview(body, columns=[c[0] for c in self.COLS],
                                 show="headings", height=10,
                                 selectmode="browse")
        for cid, title, w in self.COLS:
            self.tree.heading(cid, text=title)
            self.tree.column(cid, width=w, anchor="w",
                             stretch=(cid == "cmd"))
        vsb = ttk.Scrollbar(body, command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda e: self._edit())

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=10, pady=6)
        for text, func in (("添加厂商", self._add), ("编辑", self._edit),
                           ("删除", self._del), ("恢复内置厂商", self._restore),
                           ("关闭", self.destroy)):
            ttk.Button(bar, text=text, command=func).pack(side="left", padx=3)
        self.status = ttk.Label(self, text="", foreground="#5A5A5A")
        self.status.pack(fill="x", padx=10, pady=(0, 8))

    # ---------------------------------------------------------------- 数据
    def _all(self):
        """返回 {厂商名: (命令, 来源标记)}；来源标记=内置 / 自建 / 自建·覆盖内置。"""
        cfg = self.controller.cfg
        deleted = set(cfg.get("paging_deleted") or [])
        out = {}
        for name, cmd in PAGING_VENDORS.items():
            if name not in deleted:
                out[name] = (cmd, "内置")
        for name, cmd in (cfg.get("paging_vendors") or {}).items():
            if isinstance(cmd, str) and cmd.strip():
                out[name] = (cmd.strip(),
                             "自建·覆盖内置" if name in PAGING_VENDORS else "自建")
        return out

    def _reload(self, select=None):
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        for name, (cmd, src) in self._all().items():
            self.tree.insert("", "end", iid=name, values=(name, cmd, src))
        if select and self.tree.exists(select):
            self.tree.selection_set(select)
            self.tree.see(select)

    def _selected(self):
        sel = self.tree.selection()
        return sel[0] if sel else ""

    def _save(self, ok_text, select=None):
        """写盘并回读校验，失败时把真实原因显示在状态栏。"""
        cfg = self.controller.cfg
        ok = bool(cfg.save())
        path = getattr(cfg, "file", "")
        if ok and path:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    disk = json.load(f)
                ok = (disk.get("paging_vendors") == cfg.get("paging_vendors")
                      and disk.get("paging_deleted") == cfg.get("paging_deleted"))
            except Exception:
                ok = False
        self._reload(select)
        if ok:
            self.status.configure(text=ok_text, foreground="#5A5A5A")
        else:
            err = getattr(_cfg, "LAST_SAVE_ERROR", "") or "写入后校验失败"
            self.status.configure(text="保存失败：%s（运行目录：%s）" % (err, path),
                                  foreground="#C00000")
        return ok

    def _ask_vendor(self, title, name="", cmd=""):
        n = simpledialog.askstring(
            title, "厂商名称（显示在“关闭分页”下拉框中）：",
            initialvalue=name, parent=self)
        if n is None:
            return None
        n = n.strip()
        if not n:
            messagebox.showwarning(title, "厂商名称不能为空", parent=self)
            return None
        c = simpledialog.askstring(
            title, "登录后下发的关闭分页命令（用户视图）：",
            initialvalue=cmd, parent=self)
        if c is None:
            return None
        c = c.strip()
        if not c:
            messagebox.showwarning(title, "命令不能为空", parent=self)
            return None
        return n, c

    # ---------------------------------------------------------------- 增删改
    def _add(self):
        r = self._ask_vendor("添加厂商")
        if not r:
            return
        name, cmd = r
        if name in self._all() and not messagebox.askyesno(
                "添加厂商", "「%s」已存在，用它覆盖原命令吗？" % name, parent=self):
            return
        cfg = self.controller.cfg
        custom = dict(cfg.get("paging_vendors") or {})
        custom[name] = cmd
        cfg.set("paging_vendors", custom, save=False)
        gone = list(cfg.get("paging_deleted") or [])
        if name in gone:                      # 与已删除的内置项同名 → 恢复显示
            gone.remove(name)
            cfg.set("paging_deleted", gone, save=False)
        self._save("已添加厂商：%s" % name, select=name)

    def _edit(self):
        name = self._selected()
        if not name:
            messagebox.showinfo("编辑厂商", "请先在列表中选择要编辑的厂商。", parent=self)
            return
        old = self._all().get(name, ("", ""))[0]
        r = self._ask_vendor("编辑厂商", name, old)
        if not r:
            return
        new_name, cmd = r
        cfg = self.controller.cfg
        custom = dict(cfg.get("paging_vendors") or {})
        if new_name != name:
            if new_name in self._all() and not messagebox.askyesno(
                    "编辑厂商", "「%s」已存在，用它覆盖原命令吗？" % new_name,
                    parent=self):
                return
            custom.pop(name, None)
            if name in PAGING_VENDORS:      # 改名的内置项 → 原内置项不再显示
                gone = list(cfg.get("paging_deleted") or [])
                if name not in gone:
                    gone.append(name)
                cfg.set("paging_deleted", gone, save=False)
            if (cfg.get("paging_active") or "") == name:
                cfg.set("paging_active", "", save=False)
        custom[new_name] = cmd
        cfg.set("paging_vendors", custom, save=False)
        gone = list(cfg.get("paging_deleted") or [])
        if new_name in gone:
            gone.remove(new_name)
            cfg.set("paging_deleted", gone, save=False)
        self._save("已保存厂商：%s" % new_name, select=new_name)

    def _del(self):
        name = self._selected()
        if not name:
            messagebox.showinfo("删除厂商", "请先在列表中选择要删除的厂商。", parent=self)
            return
        if not PAGING_VENDORS.get(name):
            tip = "删除后不再出现在下拉框中。"
        else:
            tip = "删除后不再出现在下拉框中，可用“恢复内置厂商”随时找回。"
        if not messagebox.askyesno("删除厂商", "确定删除「%s」？\n%s" % (name, tip),
                                   parent=self):
            return
        cfg = self.controller.cfg
        custom = dict(cfg.get("paging_vendors") or {})
        custom.pop(name, None)
        cfg.set("paging_vendors", custom, save=False)
        if name in PAGING_VENDORS:
            gone = list(cfg.get("paging_deleted") or [])
            if name not in gone:
                gone.append(name)
            cfg.set("paging_deleted", gone, save=False)
        if (cfg.get("paging_active") or "") == name:
            cfg.set("paging_active", "", save=False)
        self._save("已删除厂商：%s" % name)

    def _restore(self):
        cfg = self.controller.cfg
        gone = list(cfg.get("paging_deleted") or [])
        if not gone:
            self.status.configure(text="内置厂商都在列表中，无需恢复",
                                  foreground="#5A5A5A")
            return
        custom = dict(cfg.get("paging_vendors") or {})
        for name in list(gone):
            if name in custom and not messagebox.askyesno(
                    "恢复内置厂商",
                    "「%s」存在自建命令；恢复内置后以内置命令为准，"
                    "要删除该自建项吗？" % name, parent=self):
                continue
            custom.pop(name, None)
        cfg.set("paging_vendors", custom, save=False)
        cfg.set("paging_deleted", [], save=False)
        self._save("已恢复 %d 个内置厂商" % len(gone))


class BatchWindow(tk.Toplevel):
    """批量下发 / 聊天窗口。"""
    def __init__(self, parent, controller):
        tk.Toplevel.__init__(self, parent)
        self.title("批量下发 · 命令窗口")
        self.geometry("920x720")
        self.transient(parent)
        self.controller = controller
        self.variables = VariableTable()
        self.dispatcher = None
        self.results = []
        self._stop = None
        self._build()
        self.refresh_targets()
        self._update_var_expand_hint()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # 窗口尺寸变化时同步目标区高度
        self.bind("<Configure>", lambda e: self._fit_target_height())
        # 整页滚轮：捕获子控件冒泡上来的 MouseWheel，落到外层滚动容器
        self.bind("<MouseWheel>", self._on_main_wheel)

    def _build(self):
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=6, pady=6)
        # 批量脚本页整体放进带滚动条的容器：目标多、变量教程展开或参数区较高时，
        # ①②③④ 都能通过右侧滚动条滚到，不再被挤出窗口外看不见。
        script_page = ttk.Frame(nb)
        nb.add(script_page, text="批量脚本")
        nb.add(self._build_chat(nb), text="聊天窗口(广播输入)")

        outer = ttk.Frame(script_page)
        outer.pack(fill="both", expand=True)
        main_canvas = tk.Canvas(outer, highlightthickness=0, borderwidth=0)
        main_sb = ttk.Scrollbar(outer, orient="vertical", command=main_canvas.yview)
        main_canvas.configure(yscrollcommand=main_sb.set)
        main_sb.pack(side="right", fill="y")
        main_canvas.pack(side="left", fill="both", expand=True)
        main = ttk.Frame(main_canvas)
        self.main_canvas = main_canvas
        self._main_win = main_canvas.create_window((0, 0), window=main, anchor="nw")
        main.bind("<Configure>", lambda e: main_canvas.configure(scrollregion=main_canvas.bbox("all")))
        main_canvas.bind("<Configure>", self._on_main_canvas_configure)

        top = ttk.LabelFrame(main, text="① 目标会话（勾选要下发的标签页或会话；未打开的会话会在下发时自动连接）")
        top.pack(fill="x", padx=4, pady=4)
        bar = ttk.Frame(top); bar.pack(fill="x", padx=6, pady=2)
        ttk.Label(bar, text="按分组:").pack(side="left", padx=(2, 4))
        self.group_var = tk.StringVar(value="全部分组")
        self.group_cb = ttk.Combobox(bar, textvariable=self.group_var, width=18,
                                     state="readonly", values=["全部分组"])
        self.group_cb.pack(side="left", padx=(0, 8))
        self.group_cb.bind("<<ComboboxSelected>>", lambda e: self.refresh_targets())
        ttk.Button(bar, text="全选", command=lambda: self._select_all(True)).pack(side="left", padx=2)
        ttk.Button(bar, text="全不选", command=lambda: self._select_all(False)).pack(side="left", padx=2)
        ttk.Button(bar, text="仅已连接", command=self._select_connected).pack(side="left", padx=2)
        ttk.Button(bar, text="全选会话管理器", command=self._select_store_all).pack(side="left", padx=2)
        ttk.Button(bar, text="导入设备 Excel/CSV", command=self._import_devices).pack(side="left", padx=2)
        ttk.Button(bar, text="下载设备模板(.xlsx)", command=self._save_template).pack(side="left", padx=2)
        ttk.Button(bar, text="刷新", command=self.refresh_targets).pack(side="left", padx=2)
        # 目标列表放在固定高度的 Canvas 里并带滚动条：设备很多时列表只在
        # 自己的框内滚动，不再无限增高把下面的命令输入区挤出窗口。
        self.target_canvas = tk.Canvas(top, height=140, highlightthickness=0,
                                       borderwidth=0, background="#f0f0f0")
        tsb = ttk.Scrollbar(top, orient="vertical", command=self.target_canvas.yview)
        self.target_canvas.configure(yscrollcommand=tsb.set)
        tsb.pack(side="right", fill="y", padx=(0, 4), pady=4)
        self.target_canvas.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=4)
        self.target_frame = ttk.Frame(self.target_canvas)
        self._target_win = self.target_canvas.create_window(
            (0, 0), window=self.target_frame, anchor="nw")
        # 内部 Frame 宽度随 Canvas 变化，保持复选框不横向溢出
        self.target_frame.bind("<Configure>", self._on_target_frame_configure)
        self.target_canvas.bind("<Configure>", self._on_target_canvas_configure)
        # 鼠标滚轮在目标列表里滚动
        self.target_canvas.bind("<MouseWheel>", self._on_target_wheel)

        mid = ttk.LabelFrame(main, text="② 命令 / 脚本（每行一条，支持 {host} {name} {user} {no} 及 CSV 列变量；SLEEP 毫秒 / WAIT \"提示符\"）")
        mid.pack(fill="x", padx=4, pady=4)
        self.cmd = tk.Text(mid, height=9, wrap="none", undo=True)
        sb = ttk.Scrollbar(mid, command=self.cmd.yview)
        self.cmd.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.cmd.pack(fill="both", expand=True, padx=4, pady=4)
        self.cmd.insert("1.0", "# 示例：批量查看系统信息（变量来自会话或 CSV）\n"
                               "uname -a\n"
                               "echo 当前设备={name} 地址={host}\n"
                               "df -h\n")
        tbar = ttk.Frame(mid); tbar.pack(fill="x", padx=4, pady=2)
        ttk.Button(tbar, text="载入脚本", command=self._load_script).pack(side="left", padx=2)
        ttk.Button(tbar, text="保存脚本", command=self._save_script).pack(side="left", padx=2)
        ttk.Button(tbar, text="导入变量 CSV", command=self._load_csv).pack(side="left", padx=2)
        ttk.Button(tbar, text="变量模板", command=self._save_var_template).pack(side="left", padx=2)
        self.var_label = ttk.Label(tbar, text="变量表: 未导入")
        self.var_label.pack(side="left", padx=10)

        # 导入变量使用说明（放在命令输入框下方、③下发参数上方）。
        # 默认收起，只占一行；点开才展开完整教程，避免把命令输入区挤出窗口。
        vh = ttk.LabelFrame(main, text="导入变量 · 使用说明")
        vh.pack(fill="x", padx=4, pady=(0, 4))
        vhead = ttk.Frame(vh); vhead.pack(fill="x", padx=6, pady=3)
        self._var_help_open = False
        self._var_help_btn = ttk.Button(
            vhead, text="▸ 展开教程", width=12,
            command=self._toggle_var_help)
        self._var_help_btn.pack(side="left", padx=(0, 8))
        ttk.Label(vhead,
                  text="首行写变量名、每行对应一台设备；命令里用 {变量名} 引用。"
                       "点「变量模板」可直接下载示例 CSV。",
                  foreground="#5A5A5A", wraplength=700,
                  justify="left").pack(side="left", fill="x", expand=True)
        self._var_help_body = ttk.Frame(vh)
        txt = tk.Text(self._var_help_body, height=9, wrap="word",
                      font=("Microsoft YaHei UI", 9), background="#f7f7f7",
                      relief="flat", padx=8, pady=6)
        vsb = ttk.Scrollbar(self._var_help_body, command=txt.yview)
        txt.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        txt.insert("1.0", VARIABLE_HELP_TEXT)
        txt.configure(state="disabled")

        opt = ttk.LabelFrame(main, text="③ 下发参数")
        opt.pack(fill="x", padx=4, pady=4)
        # v1.4.14：统一顺序执行（一次一个目标，跑完关掉再下一个）。
        # 并发会同时创建几十个标签与连接线程把界面卡死，故禁用该选项。
        self.o_parallel = tk.BooleanVar(value=False)
        self.o_wait = tk.BooleanVar(value=bool(self.controller.cfg.get("batch_wait_prompt")))
        self.o_stop = tk.BooleanVar(value=bool(self.controller.cfg.get("batch_stop_on_error")))
        self.o_delay = tk.StringVar(value=str(self.controller.cfg.get("batch_delay", 0)))
        self.o_timeout = tk.StringVar(value=str(self.controller.cfg.get("batch_timeout", 30)))
        self.o_prompt = tk.StringVar(value=r"[$#>]\s*$")
        self.o_newline = tk.BooleanVar(value=True)
        cb_par = ttk.Checkbutton(opt, text="多会话并发（已停用：改为顺序执行）",
                                  variable=self.o_parallel, state="disabled")
        cb_par.grid(row=0, column=0, sticky="w", padx=6)
        ttk.Checkbutton(opt, text="等待提示符", variable=self.o_wait).grid(row=0, column=1, sticky="w", padx=6)
        ttk.Checkbutton(opt, text="出错即停止", variable=self.o_stop).grid(row=0, column=2, sticky="w", padx=6)
        ttk.Checkbutton(opt, text="命令末加回车", variable=self.o_newline).grid(row=0, column=3, sticky="w", padx=6)
        ttk.Label(opt, text="命令间隔(秒)").grid(row=1, column=0, sticky="e", padx=4)
        ttk.Entry(opt, textvariable=self.o_delay, width=7).grid(row=1, column=1, sticky="w")
        ttk.Label(opt, text="超时(秒)").grid(row=1, column=2, sticky="e", padx=4)
        ttk.Entry(opt, textvariable=self.o_timeout, width=7).grid(row=1, column=3, sticky="w")
        ttk.Label(opt, text="提示符正则").grid(row=1, column=4, sticky="e", padx=4)
        ttk.Entry(opt, textvariable=self.o_prompt, width=20).grid(row=1, column=5, sticky="w")
        # 1.4.17：变量表展开方式。默认"每台设备执行全部变量行"——
        # 变量表 500 行时，选中的每台设备都会把这 500 行各执行一遍。
        ttk.Label(opt, text="变量展开:").grid(row=2, column=0, sticky="e", padx=4, pady=(4, 0))
        self.o_var_expand = tk.StringVar(
            value="每行变量对应一台设备（旧）"
            if self.controller.cfg.get("batch_var_expand") == "index"
            else "每台设备执行全部变量行")
        var_cb = ttk.Combobox(
            opt, textvariable=self.o_var_expand, state="readonly", width=30,
            values=["每台设备执行全部变量行", "每行变量对应一台设备（旧）"])
        var_cb.grid(row=2, column=1, columnspan=3, sticky="w", pady=(4, 0))
        var_cb.bind("<<ComboboxSelected>>", lambda e: self._update_var_expand_hint())
        self.var_stat = ttk.Label(opt, text="", foreground="#B8860B")
        self.var_stat.grid(row=2, column=4, columnspan=2, sticky="w", padx=6, pady=(4, 0))

        # 关闭分页：选厂商 → 每台设备连接并确认登录后先下发对应命令
        ttk.Label(opt, text="关闭分页:").grid(row=3, column=0, sticky="e",
                                              padx=4, pady=(4, 0))
        self.p_paging = tk.StringVar(value="（不执行）")
        self.paging_cb = ttk.Combobox(opt, textvariable=self.p_paging,
                                      state="readonly", width=30)
        self.paging_cb.grid(row=3, column=1, columnspan=3, sticky="w", pady=(4, 0))
        self.paging_cb.bind("<<ComboboxSelected>>",
                            lambda e: self._on_paging_change())
        self.btn_paging_mgr = ttk.Button(opt, text="管理厂商…",
                                         command=self._manage_paging)
        self.btn_paging_mgr.grid(row=3, column=4, sticky="w", padx=4, pady=(4, 0))
        self.paging_hint = ttk.Label(opt, text="", foreground="#5A5A5A")
        self.paging_hint.grid(row=3, column=5, sticky="w", padx=4, pady=(4, 0))
        self._refresh_paging_cb()

        ctl = ttk.Frame(main); ctl.pack(fill="x", padx=4, pady=4)
        self.btn_run = ttk.Button(ctl, text="开始下发", command=self._run)
        self.btn_run.pack(side="left", padx=2)
        self.btn_pause = ttk.Button(ctl, text="暂停", command=self._pause, state="disabled")
        self.btn_pause.pack(side="left", padx=2)
        self.btn_stop = ttk.Button(ctl, text="停止", command=self._stop_run, state="disabled")
        self.btn_stop.pack(side="left", padx=2)
        ttk.Button(ctl, text="导出结果 CSV", command=lambda: self._export("csv")).pack(side="left", padx=2)
        ttk.Button(ctl, text="导出结果 TXT", command=lambda: self._export("txt")).pack(side="left", padx=2)
        self.progress = ttk.Progressbar(ctl, mode="determinate", length=180)
        self.progress.pack(side="right", padx=4)
        self.stat = ttk.Label(ctl, text="就绪")
        self.stat.pack(side="right", padx=6)

        bot = ttk.LabelFrame(main, text="④ 执行结果（双击查看完整输出）")
        bot.pack(fill="x", padx=4, pady=4)
        cols = ("target", "command", "status", "time", "error")
        self.tree = ttk.Treeview(bot, columns=cols, show="headings", height=8)
        for c, t, w in zip(cols, ("会话", "命令", "状态", "耗时", "错误"),
                           (150, 330, 70, 70, 160)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w)
        vsb = ttk.Scrollbar(bot, command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", self._show_output)

    def _build_chat(self, nb):
        f = ttk.Frame(nb)
        ttk.Label(f, text="在此输入的内容会实时发送到下方勾选的所有会话（广播 / Chat Window）").pack(
            anchor="w", padx=8, pady=8)
        self.chat_log = tk.Text(f, height=14, state="disabled")
        self.chat_log.pack(fill="both", expand=True, padx=8, pady=4)
        row = ttk.Frame(f); row.pack(fill="x", padx=8, pady=8)
        self.chat_var = tk.StringVar()
        e = ttk.Entry(row, textvariable=self.chat_var)
        e.pack(side="left", fill="x", expand=True)
        e.bind("<Return>", lambda ev: self._chat_send())
        ttk.Button(row, text="发送(回车)", command=self._chat_send).pack(side="left", padx=4)
        self.chat_all = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="发到所有标签", variable=self.chat_all).pack(side="left", padx=4)
        return f

    def _chat_send(self):
        text = self.chat_var.get()
        if not text:
            return
        self.chat_var.set("")
        self._append_chat(">> " + text)
        if self.chat_all.get():
            targets = self.controller.tabs()
        else:
            # 广播只发到已连接的标签，不会为会话行开新标签
            targets = [t for k, t, v in getattr(self, "target_vars", [])
                       if v.get() and k == "tab" and t.is_connected()]
        for t in targets:
            if t.is_connected():
                t.send_text(text + "\r")

    def _append_chat(self, line):
        self.chat_log.configure(state="normal")
        self.chat_log.insert("end", line + "\n")
        self.chat_log.see("end")
        self.chat_log.configure(state="disabled")

    def _iter_groups(self):
        try:
            return [f for f in (self.controller.store.folders or []) if f]
        except Exception:
            return []

    def _on_target_frame_configure(self, event):
        """内部 Frame 尺寸变化时同步 Canvas 滚动区域。"""
        try:
            self.target_canvas.configure(scrollregion=self.target_canvas.bbox("all"))
        except Exception:
            pass

    def _on_target_canvas_configure(self, event):
        """Canvas 宽度变化时把内部 Frame 拉成同宽，避免出现横向滚动条。"""
        try:
            self.target_canvas.itemconfigure(self._target_win, width=event.width)
        except Exception:
            pass

    def _fit_target_height(self):
        """目标区高度随窗口自适应：约为窗口高度的 20%，范围 90~170px。

        高度整体缩小一半（原 40% / 110~320px），把空间让给下方的②③④；
        目标多时可在目标区自己的滚动条里翻看。
        """
        try:
            h = self.winfo_height()
            if h <= 1:
                return
            want = max(90, min(170, int(h * 0.2)))
            cur = self.target_canvas.cget("height")
            if int(cur) != want:
                self.target_canvas.configure(height=want)
        except Exception:
            pass

    def _on_main_canvas_configure(self, event):
        """外层滚动容器宽度变化时，把内部 Frame 拉成同宽，避免横向溢出。"""
        try:
            self.main_canvas.itemconfigure(self._main_win, width=event.width)
        except Exception:
            pass

    def _widget_in_target(self, w):
        """判断控件是否位于目标区画布内（含其动态生成的复选框）。"""
        try:
            while w is not None:
                if w is self.target_canvas:
                    return True
                w = w.winfo_parent()
                if not w:
                    break
                w = self.nametowidget(w)
        except Exception:
            pass
        return False

    def _on_main_wheel(self, event):
        """整页滚轮：落在目标区就滚目标列表，否则滚动外层整页。

        cmd 文本、tree 表格、教程文本、chat 文本各自绑定了滚轮并 return "break"
        阻断冒泡，不会走到这里；ttk 标签/按钮等无滚轮绑定会冒泡到本方法。
        """
        try:
            delta = int(getattr(event, "delta", 0) or 0)
            if not delta:
                return "break"
            steps = -1 if delta > 0 else 1
            if self._widget_in_target(event.widget):
                self.target_canvas.yview_scroll(steps, "units")
            else:
                self.main_canvas.yview_scroll(steps, "units")
        except Exception:
            pass
        return "break"

    def _on_target_wheel(self, event):
        """目标列表内滚轮滚动（不外传，避免影响命令区）。"""
        try:
            delta = int(getattr(event, "delta", 0) or 0)
            if not delta:
                return "break"
            self.target_canvas.yview_scroll(-1 if delta > 0 else 1, "units")
        except Exception:
            pass
        return "break"

    def _target_columns(self):
        """按目标区可用宽度算出列数（每列固定宽度，避免宽度跳动）。"""
        try:
            w = self.target_canvas.winfo_width()
        except Exception:
            w = 0
        if w <= 1:
            w = 860
        return max(2, min(6, int(w // 210)))

    def refresh_targets(self):
        for w in self.target_frame.winfo_children():
            w.destroy()
        self.target_vars = []        # [(kind, obj, var)] kind: tab | session
        groups = self._iter_groups()
        cur = self.group_var.get()
        if cur not in (["全部分组", "未分组"] + groups):
            cur = "全部分组"
        self.group_cb.configure(values=["全部分组", "未分组"] + groups)
        if cur not in ("全部分组", "未分组"):
            pass
        self.group_var.set(cur)

        def in_group(folder):
            if cur == "全部分组":
                return True
            if cur == "未分组":
                return not (folder or "").strip()
            return (folder or "") == cur

        # 已打开的标签页
        shown_keys = set()
        row = 0
        ncol = self._target_columns()
        # 固定列宽：复选框文字长短不一，不固定会让列宽跳动
        for c in range(ncol):
            self.target_frame.grid_columnconfigure(c, weight=0, minsize=200)
        for tab in self.controller.tabs():
            if not in_group(tab.session.get("folder", "")):
                continue
            var = tk.BooleanVar(value=tab.is_connected())
            cb = ttk.Checkbutton(self.target_frame, text="● " + tab.name, variable=var)
            cb.grid(row=row // ncol, column=row % ncol, sticky="w",
                    padx=6, pady=1)
            self.target_vars.append(("tab", tab, var))
            shown_keys.add(self._sess_key(tab.session))
            row += 1
        # 会话管理器中未打开的会话（也参与下发，运行时自动连接）
        try:
            sessions = self.controller.store.all()
        except Exception:
            sessions = []
        for s in sessions:
            if not in_group(s.get("folder", "")):
                continue
            key = self._sess_key(s)
            if key in shown_keys:
                continue
            proto = s.get("protocol", "")
            if proto in ("LocalShell",):
                continue   # 本地 Shell 不适合批量远程下发，默认不列
            var = tk.BooleanVar(value=False)
            label = "%s (%s%s)" % (s.get("name", "未命名"), proto,
                                   ("·" + s.get("folder")) if s.get("folder") else "")
            cb = ttk.Checkbutton(self.target_frame, text=label, variable=var)
            cb.grid(row=row // ncol, column=row % ncol, sticky="w",
                    padx=6, pady=1)
            self.target_vars.append(("session", s, var))
            row += 1
        # 内容高度变化后同步滚动区域
        try:
            self.target_canvas.configure(scrollregion=self.target_canvas.bbox("all"))
        except Exception:
            pass

    @staticmethod
    def _sess_key(s):
        return (s.get("protocol", ""), s.get("host", ""), str(s.get("port", "")),
                s.get("username", ""))

    def selected_targets(self):
        """返回勾选的目标（会话描述或已打开的标签对象）。"""
        return self._resolve_targets()

    def resolve_targets_sync(self, timeout=15):
        """解析勾选的目标：**只做筛选，不在这里开标签**。

        目标很多时一次性 open_session 会创建同等数量的 widget 树与连接线程，
        把界面卡死。真正的建标签/连接交给 dispatcher 在顺序循环里逐个进行。
        """
        out = []
        for kind, obj, var in getattr(self, "target_vars", []):
            if not var.get():
                continue
            if kind == "tab":
                out.append(obj)          # 已打开的标签，直接用
                continue
            out.append(dict(obj))        # 未打开的：先记下会话，执行时再开
        return out

    def _resolve_targets(self):
        return self.resolve_targets_sync()

    def _find_open_tab(self, session):
        """找一个已打开的同会话标签（避免重复开窗口）。"""
        key = self._sess_key(session)
        for t in self.controller.tabs():
            if self._sess_key(t.session) == key:
                return t
        return None

    def _select_all(self, value):
        for _k, _o, v in self.target_vars:
            v.set(value)

    def _select_connected(self):
        for k, t, v in self.target_vars:
            v.set(bool(t.is_connected()) if k == "tab" else False)

    def _select_store_all(self):
        """全选会话管理器里的所有会话（含未打开的）。"""
        self.group_var.set("全部分组")
        self.refresh_targets()
        for _k, _o, v in self.target_vars:
            v.set(True)

    def _save_template(self):
        p = filedialog.asksaveasfilename(
            parent=self, title="保存设备导入模板",
            defaultextension=".xlsx", initialfile="设备导入模板.xlsx",
            filetypes=[("Excel 模板", "*.xlsx")])
        if p:
            try:
                write_device_template(p)
            except Exception as e:
                messagebox.showerror("TermX", "模板保存失败：%s" % e, parent=self)
                return
            messagebox.showinfo(
                "TermX",
                "模板已保存：\n%s\n\n用 Excel 打开填写（保持 .xlsx 格式），"
                "点「导入设备 Excel/CSV」即可。\n"
                "注意：请勿另存为 CSV，Excel 保存 CSV 会把中文表头改成 ANSI "
                "编码，导致导入时找不到「主机」列。" % p, parent=self)

    def _import_devices(self):
        p = filedialog.askopenfilename(
            parent=self, title="导入设备列表",
            filetypes=[("Excel 工作簿", "*.xlsx"), ("CSV 文件", "*.csv"),
                       ("所有", "*.*")])
        if not p:
            return
        try:
            n, err = import_devices_from_table(self.controller.store, p)
        except Exception as e:
            messagebox.showerror("TermX", "导入失败：%s" % e, parent=self)
            return
        if err:
            messagebox.showwarning("TermX", err, parent=self)
            return
        self.refresh_targets()
        messagebox.showinfo("TermX", "已导入 %d 台设备到会话管理器" % n, parent=self)

    def _toggle_var_help(self):
        """展开/收起「导入变量使用说明」教程区。"""
        self._var_help_open = not self._var_help_open
        try:
            if self._var_help_open:
                self._var_help_body.pack(fill="x", padx=6, pady=(0, 6))
                self._var_help_btn.configure(text="▾ 收起教程")
            else:
                self._var_help_body.pack_forget()
                self._var_help_btn.configure(text="▸ 展开教程")
        except Exception:
            pass

    def _save_var_template(self):
        p = filedialog.asksaveasfilename(
            parent=self, title="保存变量导入模板",
            defaultextension=".csv", initialfile="变量导入模板.csv",
            filetypes=[("CSV 模板", "*.csv")])
        if not p:
            return
        try:
            write_variable_template(p)
        except Exception as e:
            messagebox.showerror("TermX", "模板保存失败：%s" % e, parent=self)
            return
        messagebox.showinfo(
            "TermX",
            "模板已保存：\n%s\n\n首行是变量名，每行对应一台设备；"
            "命令里用 {变量名} 引用，例如 interface {uplink}。\n"
            "内置变量 name/host/ip/user/port/index/no/protocol/folder "
            "无需写在表里，会自动提供。" % p, parent=self)

    def _load_script(self):
        p = filedialog.askopenfilename(parent=self, filetypes=[("脚本", "*.txt;*.tcl;*.py;*.cmd"), ("所有", "*.*")])
        if p:
            self.cmd.delete("1.0", "end")
            self.cmd.insert("1.0", open(p, "r", encoding="utf-8", errors="replace").read())

    def _save_script(self):
        p = filedialog.asksaveasfilename(parent=self, defaultextension=".txt")
        if p:
            open(p, "w", encoding="utf-8").write(self.cmd.get("1.0", "end"))

    def _load_csv(self):
        p = filedialog.askopenfilename(parent=self, filetypes=[("CSV", "*.csv"), ("所有", "*.*")])
        if not p:
            return
        try:
            n = self.variables.load(p)
        except Exception as e:
            messagebox.showerror("TermX", "变量表导入失败：%s" % e, parent=self)
            return
        self.var_label.configure(text="变量表: %d 行 / 列 %s" %
                                 (n, ",".join(self.variables.headers[:6])))
        self._update_var_expand_hint()

    def _update_var_expand_hint(self):
        """显示当前模式下将执行多少条命令，避免 500 行变量时用户没预期。"""
        lbl = getattr(self, "var_stat", None)
        if lbl is None:
            return
        try:
            rows = len(getattr(self.variables, "rows", []) or [])
            mode = "index" if self.o_var_expand.get().startswith("每行") else "all"
            if rows <= 0:
                lbl.configure(text="（未导入变量表）")
            elif mode == "all":
                lbl.configure(text="每台设备执行 %d 行变量" % rows)
            else:
                lbl.configure(text="第 N 台设备用第 N 行变量")
        except Exception:
            pass

    # ------------------------ 关闭分页（登录后自动执行） ------------------------
    def _all_paging_vendors(self):
        """内置厂商（扣除已删除的）+ 自建厂商（cfg["paging_vendors"]）。

        同名自建项覆盖内置项，因此内置项删除后仍可再建同名厂商。
        """
        cfg = self.controller.cfg
        deleted = set(cfg.get("paging_deleted") or [])
        out = {k: v for k, v in PAGING_VENDORS.items() if k not in deleted}
        for name, cmd in (cfg.get("paging_vendors") or {}).items():
            if isinstance(cmd, str) and cmd.strip():
                out[name] = cmd.strip()
        return out

    def _refresh_paging_cb(self, select=None):
        names = list(self._all_paging_vendors().keys())
        self.paging_cb.configure(values=["（不执行）"] + names)
        want = select if select is not None else \
            (self.controller.cfg.get("paging_active") or "")
        self.p_paging.set(want if want in names else "（不执行）")
        self._on_paging_change()

    def _on_paging_change(self):
        name = (self.p_paging.get() or "").strip()
        cmd = self._all_paging_vendors().get(name, "")
        try:
            self.paging_hint.configure(
                text=("登录后自动执行：" + cmd) if cmd else "登录后不执行关闭分页")
        except Exception:
            pass

    def _manage_paging(self):
        dlg = PagingVendorDialog(self, self.controller)
        self.wait_window(dlg)
        self._refresh_paging_cb()

    def _run(self):
        targets = self.selected_targets()
        if not targets:
            messagebox.showwarning("TermX", "请先勾选至少一个目标会话", parent=self)
            return
        lines = self.cmd.get("1.0", "end").splitlines()
        # 全是空行也允许（每行发一个回车），只拒绝完全没有任何内容的输入
        if not lines or (not any(l.strip() for l in lines)
                         and not any(l == "" for l in lines)):
            messagebox.showwarning("TermX", "命令内容为空", parent=self)
            return
        for i in self.tree.get_children():
            self.tree.delete(i)
        self.results = []
        try:
            delay, timeout = float(self.o_delay.get()), float(self.o_timeout.get())
        except Exception:
            delay, timeout = 0.0, 30.0
        var_expand = "index" if self.o_var_expand.get().startswith("每行") else "all"
        # 记住本次选择
        self.controller.cfg.set("batch_var_expand", var_expand)
        # 关闭分页：选中厂商则登录确认后先下发对应命令（空/"（不执行）" = 关闭该功能）
        paging_name = (self.p_paging.get() or "").strip()
        paging_cmd = self._all_paging_vendors().get(paging_name, "") \
            if paging_name and paging_name != "（不执行）" else ""
        if paging_cmd:
            self.controller.cfg.set("paging_active", paging_name)
        else:
            paging_name = ""
            self.controller.cfg.set("paging_active", "")
        opts = {"parallel": self.o_parallel.get(), "wait_prompt": self.o_wait.get(),
                "stop_on_error": self.o_stop.get(), "delay": delay, "timeout": timeout,
                "prompt": self.o_prompt.get(), "send_newline": self.o_newline.get(),
                "var_expand": var_expand,
                "paging_cmd": paging_cmd, "paging_name": paging_name}
        self.dispatcher = BatchDispatcher(
            self.controller, targets, lines, self.variables, opts,
            on_progress=self._progress, on_result=self._result,
            on_done=self._done,
            on_log=lambda s: self._ui(self._append_chat, s))
        self.progress.configure(maximum=max(1, self.dispatcher.total), value=0)
        self.btn_run.configure(state="disabled")
        self.btn_pause.configure(state="normal")
        self.btn_stop.configure(state="normal")
        self.stat.configure(text="执行中 ...")
        self.dispatcher.run()

    def _ui(self, func, *args):
        return self.controller.ui_call(func, *args)

    def _progress(self, n, total, target, cmd, status):
        self._ui(lambda: (self.progress.configure(value=n),
                          self.stat.configure(text="%d/%d · %s" % (n, total, status))))

    def _result(self, r):
        self.results.append(r)
        target = r.target
        if getattr(r, "var_row", 0):
            target = "%s [行%d]" % (target, r.var_row)
        self._ui(lambda: self.tree.insert("", "end", values=(
            target, r.command[:80], "成功" if r.ok else "失败",
            "%.2f" % r.elapsed, r.error)))

    def _done(self, results):
        self._ui(lambda: (self.btn_run.configure(state="normal"),
                          self.btn_pause.configure(state="disabled"),
                          self.btn_stop.configure(state="disabled"),
                          self.stat.configure(text="完成 · 共 %d 条" % len(results)),
                          self._append_chat("== 下发完成，共 %d 条结果 ==" % len(results))))

    def _pause(self):
        if self.dispatcher:
            paused = not self.dispatcher._pause.is_set()
            self.dispatcher.pause(paused)
            self.btn_pause.configure(text="继续" if paused else "暂停")

    def _stop_run(self):
        if self.dispatcher:
            self.dispatcher.stop()
            self.dispatcher.pause(False)
            self._append_chat("== 已请求停止 ==")

    def _export(self, fmt):
        if not self.results:
            messagebox.showinfo("TermX", "暂无结果", parent=self)
            return
        p = filedialog.asksaveasfilename(parent=self,
                                         defaultextension="." + fmt,
                                         filetypes=[(fmt.upper(), "*." + fmt)])
        if p:
            BatchDispatcher.export(self.results, p, fmt)
            messagebox.showinfo("TermX", "已导出到:\n" + p, parent=self)

    def _show_output(self, event):
        sel = self.tree.selection()
        if not sel:
            return
        idx = self.tree.index(sel[0])
        if idx >= len(self.results):
            return
        r = self.results[idx]
        w = tk.Toplevel(self)
        w.title("输出 - %s · %s" % (r.target, r.command[:40]))
        w.geometry("760x520")
        t = tk.Text(w, wrap="word")
        s = ttk.Scrollbar(w, command=t.yview)
        t.configure(yscrollcommand=s.set)
        s.pack(side="right", fill="y")
        t.pack(fill="both", expand=True)
        t.insert("1.0", "会话: %s\n命令: %s\n状态: %s\n%s\n\n%s" %
                 (r.target, r.command, "成功" if r.ok else "失败", "=" * 60, r.output))
        t.configure(state="disabled")

    def _on_close(self):
        if self.dispatcher:
            self.dispatcher.stop()
        self.destroy()


DEVICE_TEMPLATES = {
    "华为 VRP 配置模板": {
        "hint": "适用华为 S/CE/AR 系列；请先在目标标签连上设备再运行",
        "body": """\
# ================= 华为 VRP 设备配置脚本 =================
# 用法：目标标签先连接设备（保存了密码会自动应答登录提示），再运行本脚本。
# 按需修改 VLAN / 端口 / IP / 用户名密码 后运行。每行一条命令自动发送。
# 特殊指令：SLEEP 毫秒 | WAIT "提示文字" 超时秒

# ---- 关闭分屏，避免长输出翻页 ----
screen-length 0 temporary
SLEEP 300

# ---- 进入系统视图并修改主机名 ----
system-view
SLEEP 300
sysname Switch-TermX

# ---- 创建 VLAN 并配置名称 ----
vlan batch 10 20
vlan 10
 description Office
quit
vlan 20
 description Server
quit

# ---- 上联口配置 Trunk ----
interface GigabitEthernet0/0/1
 port link-type trunk
 port trunk allow-pass vlan 10 20
quit

# ---- 配置 VLANIF 管理地址 ----
interface Vlanif10
 ip address 192.168.10.1 255.255.255.0
quit

# ---- 配置 SSH/Telnet 登录用户（务必修改用户名密码）----
aaa
 local-user termx password irreversible Termx@123
 local-user termx privilege level 15
 local-user termx service-type ssh telnet
quit
ssh user termx authentication-type password
stelnet server enable
user-interface vty 0 4
 authentication-mode aaa
 protocol inbound all
quit
SLEEP 300

# ---- 保存配置 ----
return
save
WAIT "save the configuration" 10
y
# ===== 脚本结束 =====
""",
    },
    "华三 Comware 配置模板": {
        "hint": "适用华三 S 系列/MSR 系列；密码登录提示 Username:/Password:",
        "body": """\
# ================= 华三 Comware 设备配置脚本 =================
# 用法：目标标签先连接设备，再运行本脚本。请按需修改 VLAN/端口/IP/用户名密码。

# ---- 关闭分屏 ----
screen-length disable
SLEEP 300

# ---- 进入系统视图并修改主机名 ----
system-view
SLEEP 300
sysname H3C-Switch-TermX

# ---- 创建 VLAN ----
vlan 10
 description Office
quit
vlan 20
 description Server
quit

# ---- 上联口配置 Trunk ----
interface GigabitEthernet1/0/1
 port link-type trunk
 port trunk permit vlan 10 20
quit

# ---- 配置 VLAN 接口管理地址 ----
interface Vlan-interface10
 ip address 192.168.10.1 255.255.255.0
quit

# ---- 配置 SSH/Telnet 登录用户（务必修改用户名密码）----
local-user termx class manage
 password hash Termx@123
 service-type ssh telnet
 authorization-attribute user-role network-admin
quit
ssh server enable
line vty 0 4
 authentication-mode scheme
quit
SLEEP 300

# ---- 保存配置 ----
return
save
WAIT "[Y/N]" 15
Y
# ===== 脚本结束 =====
""",
    },
    "锐捷 Ruijie 配置模板": {
        "hint": "适用锐捷 N/S 系列交换机；配置模式与思科 IOS 相近",
        "body": """\
# ================= 锐捷 Ruijie 设备配置脚本 =================
# 用法：目标标签先连接设备，再运行本脚本。请按需修改 VLAN/端口/IP/用户名密码。

# ---- 进入特权与全局配置模式 ----
enable
SLEEP 300
configure terminal
SLEEP 300
hostname Ruijie-Switch-TermX

# ---- 创建 VLAN ----
vlan 10
 name Office
exit
vlan 20
 name Server
exit

# ---- 上联口配置 Trunk ----
interface GigabitEthernet 0/1
 switchport mode trunk
 switchport allowed vlan add 10,20
exit

# ---- 配置 SVI 管理地址 ----
interface VLAN 10
 ip address 192.168.10.1 255.255.255.0
exit

# ---- 配置 SSH/Telnet 登录（务必修改用户名密码）----
line vty 0 4
 login local
 transport input ssh
exit
enable password Termx@123
username admin privilege 15 password Termx@123
ip ssh version 2
SLEEP 300

# ---- 保存配置 ----
end
write
WAIT "confirm" 15
y
# ===== 脚本结束 =====
""",
    },
    "思科 IOS 配置模板": {
        "hint": "适用思科 IOS/IOS-XE 交换机路由器",
        "body": """\
# ================= 思科 IOS 设备配置脚本 =================
# 用法：目标标签先连接设备，再运行本脚本。请按需修改 VLAN/端口/IP/用户名密码。

terminal length 0
SLEEP 300
configure terminal
SLEEP 300
hostname Cisco-Switch-TermX

vlan 10
 name Office
exit
vlan 20
 name Server
exit

interface GigabitEthernet0/1
 switchport mode trunk
 switchport trunk allowed vlan 10,20
exit

interface Vlan10
 ip address 192.168.10.1 255.255.255.0
 no shutdown
exit

username admin privilege 15 secret Termx@123
line vty 0 4
 login local
 transport input ssh
exit
ip domain-name local
crypto key generate rsa
WAIT "bits" 20
1024
ip ssh version 2
SLEEP 300

end
write memory
# ===== 脚本结束 =====
""",
    },
    "通用网络设备登录": {
        "hint": "通用登录应答：可先运行本模板完成登录，再运行对应厂商模板",
        "body": """\
# ================= 通用网络设备登录脚本 =================
# 目标会话未保存密码时可用本脚本完成登录。请修改下方用户名/密码。
WAIT "name:" 30
SEND "admin\\r"
WAIT "assword:" 15
SEND "your-password\\r"
# 登录后按需进入配置模式
SLEEP 800
# enable / system-view 按设备类型自行选择
""",
    },
}


class ScriptWindow(tk.Toplevel):
    def __init__(self, parent, controller):
        tk.Toplevel.__init__(self, parent)
        self.title("脚本编辑器 / 运行器")
        self.geometry("880x660")
        self.transient(parent)
        self.controller = controller
        self.stop_event = None
        self._multi = None
        self.targets_spec = None
        self._build()

    def _build(self):
        # 第一行：语言 / 目标标签 / 刷新 + 打开保存模板（拆成两行避免下拉框被挤没）
        top = ttk.Frame(self); top.pack(fill="x", padx=6, pady=(6, 0))
        ttk.Label(top, text="语言:").pack(side="left")
        self.lang = tk.StringVar(value="python")
        ttk.Combobox(top, textvariable=self.lang, width=12, state="readonly",
                     values=["python", "vbscript", "jscript", "commands"]).pack(side="left", padx=4)
        ttk.Label(top, text="目标标签:").pack(side="left", padx=(10, 2))
        self.tab_idx = tk.StringVar(value="当前标签")
        self.tab_combo = ttk.Combobox(top, textvariable=self.tab_idx, width=20, state="readonly")
        self.tab_combo.pack(side="left", padx=4)
        self._refresh_tabs()
        ttk.Button(top, text="刷新", command=self._refresh_tabs, width=6).pack(side="left")
        ttk.Button(top, text="模板", command=self._template).pack(side="right", padx=3)
        ttk.Button(top, text="保存", command=self._save).pack(side="right", padx=3)
        ttk.Button(top, text="打开", command=self._open).pack(side="right", padx=3)

        # 第二行：设备模板（内置 + 自建）与运行目标
        row2 = ttk.Frame(self); row2.pack(fill="x", padx=6, pady=4)
        ttk.Label(row2, text="设备模板:").pack(side="left")
        self.vendor = tk.StringVar(value="")
        self.vendor_cb = ttk.Combobox(row2, textvariable=self.vendor, width=24,
                                      state="readonly",
                                      values=list(DEVICE_TEMPLATES.keys()))
        self.vendor_cb.pack(side="left", padx=4)
        self.vendor_cb.bind("<<ComboboxSelected>>", lambda e: self._load_device_template())
        ttk.Button(row2, text="新建模板", command=self._new_template, width=9).pack(side="left", padx=(6, 0))
        ttk.Button(row2, text="保存模板", command=self._save_template, width=9).pack(side="left", padx=3)
        ttk.Button(row2, text="重命名", command=self._rename_template, width=7).pack(side="left", padx=3)
        ttk.Button(row2, text="删除模板", command=self._del_template, width=9).pack(side="left", padx=3)
        ttk.Label(row2, text="运行目标:").pack(side="left", padx=(16, 2))
        self.targets_label = tk.StringVar(value="当前标签（单会话）")
        ttk.Label(row2, textvariable=self.targets_label).pack(side="left")
        ttk.Button(row2, text="选择目标...", command=self._pick_targets, width=10).pack(side="left", padx=3)
        ttk.Button(row2, text="清除", command=self._clear_targets, width=5).pack(side="left")
        self.targets_spec = None       # 多目标运行规格（None = 单标签模式）

        pane = ttk.PanedWindow(self, orient="vertical")
        pane.pack(fill="both", expand=True, padx=6, pady=4)
        edit = ttk.LabelFrame(pane, text="脚本内容")
        self.editor = tk.Text(edit, height=16, wrap="none", undo=True,
                              font=("Consolas", 10))
        esb = ttk.Scrollbar(edit, command=self.editor.yview)
        self.editor.configure(yscrollcommand=esb.set)
        esb.pack(side="right", fill="y")
        self.editor.pack(fill="both", expand=True)
        pane.add(edit, weight=3)
        out = ttk.LabelFrame(pane, text="运行输出")
        self.output = tk.Text(out, height=8, state="disabled")
        osb = ttk.Scrollbar(out, command=self.output.yview)
        self.output.configure(yscrollcommand=osb.set)
        osb.pack(side="right", fill="y")
        self.output.pack(fill="both", expand=True)
        pane.add(out, weight=1)

        bar = ttk.Frame(self); bar.pack(fill="x", padx=6, pady=6)
        self.btn_run = ttk.Button(bar, text="▶ 运行", command=self._run)
        self.btn_run.pack(side="left", padx=3)
        self.btn_stop = ttk.Button(bar, text="■ 停止", command=self._stop, state="disabled")
        self.btn_stop.pack(side="left", padx=3)
        ttk.Button(bar, text="清空输出", command=lambda: self.output.configure(
            state="normal") or self.output.delete("1.0", "end") or
            self.output.configure(state="disabled")).pack(side="left", padx=3)
        self.status = ttk.Label(bar, text="就绪")
        self.status.pack(side="right", padx=6)
        self.editor.insert("1.0", PY_TEMPLATE)
        self._refresh_vendor_cb()      # 过滤已删除的内置模板

    def _refresh_tabs(self):
        names = ["当前标签"] + ["%d: %s" % (i + 1, t.name)
                               for i, t in enumerate(self.controller.tabs())]
        self.tab_combo.configure(values=names)
        if self.tab_idx.get() not in names:
            self.tab_idx.set("当前标签")

    def _template(self):
        lang = self.lang.get()
        tpl = PY_TEMPLATE if lang == "python" else (JS_TEMPLATE if lang == "jscript" else
              (VBS_TEMPLATE if lang == "vbscript" else CMD_TEMPLATE))
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", tpl)

    def _all_templates(self):
        """内置模板（扣除已删除的）+ 自建模板（cfg["device_templates"]）。

        自建模板同名可覆盖/恢复内置模板，因此内置模板删除后仍可再新建同名模板。
        """
        cfg = self.controller.cfg
        deleted = set(cfg.get("deleted_templates") or [])
        tpl = {k: v for k, v in DEVICE_TEMPLATES.items() if k not in deleted}
        for name, item in (cfg.get("device_templates") or {}).items():
            if isinstance(item, str):        # 兼容早期纯文本存储
                item = {"lang": "commands", "body": item}
            tpl[name] = item
        return tpl

    def _refresh_vendor_cb(self, select=None):
        names = list(self._all_templates().keys())
        self.vendor_cb.configure(values=names)
        if select is not None and select in names:
            self.vendor.set(select)
        elif self.vendor.get() not in names:
            self.vendor.set("")

    def _load_device_template(self):
        key = self.vendor.get()
        tpl = self._all_templates().get(key)
        if not tpl:
            return
        self.lang.set(tpl.get("lang") or "commands")
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", tpl["body"])
        hint = tpl.get("hint", "")
        self.status.configure(text=("已载入设备模板：%s%s" %
                                    (key, ("（" + hint + "）") if hint else
                                     "（请按需修改后运行）")))

    def _persist_templates(self, ok_text):
        """模板改动统一落盘：写完回读校验，失败时给出明确提示并支持重选运行目录。"""
        cfg = self.controller.cfg
        res = cfg.save()
        known = res is not None          # 简易配置桩返回 None 时不做回读校验
        ok = True if res is None else bool(res)
        path = getattr(cfg, "file", "")
        if ok and known and path:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    disk = json.load(f)
                ok = (disk.get("device_templates") == cfg.get("device_templates"))
            except Exception:
                ok = False
        if ok:
            self.status.configure(text=ok_text)
            return True
        err = getattr(_cfg, "LAST_SAVE_ERROR", "") or "写入后校验失败"
        path = path or _cfg.data_dir()
        if messagebox.askyesno(
                "模板保存失败",
                "模板未能写入运行目录：\n%s\n\n原因：%s\n\n"
                "常见原因：运行目录被删除、移动或没有写入权限。\n"
                "现在重新选择运行目录吗？" % (path, err), parent=self):
            dlg = DataDirDialog(self)
            self.wait_window(dlg)
            if dlg.result:
                if hasattr(cfg, "rebind"):
                    cfg.rebind()
                res = cfg.save()
                ok = True if res is None else bool(res)
        self.status.configure(text=("模板保存失败：%s" % err) if not ok else ok_text)
        return bool(ok)

    def _save_template(self):
        """把当前编辑器内容保存回选中的模板（内置模板自动变为同名自建模板）。"""
        key = self.vendor.get().strip()
        if not key:
            messagebox.showinfo("保存模板", "请先在设备模板下拉中选中要保存的模板。",
                                parent=self)
            return
        cfg = self.controller.cfg
        custom = dict(cfg.get("device_templates") or {})
        custom[key] = {"lang": self.lang.get(),
                       "body": self.editor.get("1.0", "end").rstrip("\n")}
        cfg.set("device_templates", custom, save=False)
        gone = list(cfg.get("deleted_templates") or [])
        if key in gone:                       # 同名重建时从黑名单移除
            gone.remove(key)
            cfg.set("deleted_templates", gone, save=False)
        if self._persist_templates("已保存模板：%s（内容取自当前编辑器）" % key):
            self._refresh_vendor_cb(select=key)

    def _new_template(self):
        """把当前编辑器内容保存为新模板，自动出现在设备模板下拉里。"""
        name = simpledialog.askstring("新建模板", "模板名称（将出现在设备模板下拉中）：",
                                      parent=self)
        if name is None:
            return
        name = name.strip()
        if not name:
            messagebox.showwarning("新建模板", "模板名称不能为空", parent=self)
            return
        cfg = self.controller.cfg
        custom = dict(cfg.get("device_templates") or {})
        custom[name] = {"lang": self.lang.get(),
                        "body": self.editor.get("1.0", "end").rstrip("\n")}
        cfg.set("device_templates", custom, save=False)
        gone = list(cfg.get("deleted_templates") or [])
        if name in gone:                      # 与已删除内置模板同名 → 恢复显示
            gone.remove(name)
            cfg.set("deleted_templates", gone, save=False)
        self._refresh_vendor_cb(select=name)
        self._persist_templates("已保存自建模板：%s（内容取自当前编辑器）" % name)

    def _rename_template(self):
        """重命名选中的自建模板（内置模板只读，不可重命名）。"""
        key = self.vendor.get().strip()
        if not key:
            messagebox.showinfo("重命名模板", "请先在设备模板下拉中选中要重命名的自建模板。", parent=self)
            return
        custom = self.controller.cfg.get("device_templates") or {}
        if key not in custom:
            messagebox.showwarning("重命名模板",
                                   "「%s」是内置模板，不能重命名；\n可先选中它载入内容，再点「新建模板」另存为自己的模板。" % key,
                                   parent=self)
            return
        new = simpledialog.askstring("重命名模板", "新的模板名称：",
                                     initialvalue=key, parent=self)
        if new is None:
            return
        new = new.strip()
        if not new:
            messagebox.showwarning("重命名模板", "模板名称不能为空", parent=self)
            return
        if new == key:
            return
        if new in self._all_templates():
            messagebox.showwarning("重命名模板",
                                   "模板「%s」已存在，请换一个名称。" % new, parent=self)
            return
        custom = dict(custom)
        tpl = custom.pop(key)
        custom[new] = tpl
        self.controller.cfg.set("device_templates", custom, save=False)
        self._refresh_vendor_cb(select=new)
        self._persist_templates("已重命名模板：%s → %s" % (key, new))

    def _del_template(self):
        """删除选中的模板：自建模板直接移除，内置模板记入黑名单不再显示。"""
        key = self.vendor.get().strip()
        if not key:
            messagebox.showinfo("删除模板", "请先在设备模板下拉中选中要删除的模板。", parent=self)
            return
        if key not in self._all_templates():
            messagebox.showwarning("删除模板", "模板「%s」不存在。" % key, parent=self)
            return
        cfg = self.controller.cfg
        custom = cfg.get("device_templates") or {}
        builtin = key in DEVICE_TEMPLATES
        tip = ("「%s」是内置模板，删除后不再出现在下拉中；\n"
               "之后仍可用「新建模板」以同名重建。\n确定删除？" % key) if builtin else \
              ("确定删除模板「%s」？" % key)
        if not messagebox.askyesno("删除模板", tip, parent=self):
            return
        # 与内置同名的自建模板：两者一起清掉，否则内置模板会在下拉里"复活"
        if builtin:
            gone = list(cfg.get("deleted_templates") or [])
            if key not in gone:
                gone.append(key)
            cfg.set("deleted_templates", gone, save=False)
        if key in custom:
            custom = dict(custom)
            custom.pop(key, None)
            cfg.set("device_templates", custom, save=False)
        self._refresh_vendor_cb()
        self._persist_templates("已删除模板：%s%s" %
                                (key, "（内置）" if builtin else ""))

    def _open(self):
        p = filedialog.askopenfilename(parent=self, filetypes=[
            ("脚本", "*.py;*.js;*.vbs;*.txt"), ("所有", "*.*")])
        if p:
            self.editor.delete("1.0", "end")
            self.editor.insert("1.0", open(p, "r", encoding="utf-8", errors="replace").read())
            low = p.lower()
            self.lang.set("python" if low.endswith(".py") else
                          "jscript" if low.endswith(".js") else
                          "vbscript" if low.endswith(".vbs") else "commands")

    def _save(self):
        ext = {"python": ".py", "jscript": ".js", "vbscript": ".vbs", "commands": ".txt"}[self.lang.get()]
        p = filedialog.asksaveasfilename(parent=self, defaultextension=ext)
        if p:
            open(p, "w", encoding="utf-8").write(self.editor.get("1.0", "end"))

    def _log(self, text):
        self.controller.ui_call(self._append, str(text))

    def _append(self, text):
        self.output.configure(state="normal")
        self.output.insert("end", str(text) + "\n")
        self.output.see("end")
        self.output.configure(state="disabled")

    def _pick_targets(self):
        dlg = TargetSelectDialog(self, self.controller)
        self.wait_window(dlg)
        spec = dlg.spec
        if not spec:
            return
        self.targets_spec = spec
        self.targets_label.set(describe_targets(spec))

    def _clear_targets(self):
        self.targets_spec = None
        self.targets_label.set("当前标签（单会话）")

    def _resolve_multi_targets(self):
        """解析多目标规格为**会话描述列表**（不开标签、不连接）。

        关键：这里只做"筛选"，不碰 open_session。目标多时一次性开几十个
        标签会创建同等数量的 widget 树与连接线程，把界面卡死。
        真正的建标签/连接放到执行队列里逐个进行（见 _run_next）。
        """
        spec = self.targets_spec or {}
        store = self.controller.store
        sess_list = []
        seen = set()

        def add_session(s):
            if (s.get("protocol") or "") == "LocalShell":
                return          # 本地 Shell 不参与远程批量执行
            k = BatchWindow._sess_key(s)
            if k in seen:
                return
            seen.add(k)
            sess_list.append(dict(s))

        if spec.get("all"):
            for s in store.all():
                add_session(s)
        for g in spec.get("groups") or []:
            for s in store.all():
                if (s.get("folder") or "") == g:
                    add_session(s)
        for s in spec.get("sessions") or []:
            add_session(dict(s))
        for token in spec.get("nets") or []:
            try:
                net = ipaddress.ip_network(str(token).strip(), strict=False)
            except ValueError:
                continue
            for s in store.all():
                host = str(s.get("host", "")).strip()
                try:
                    ip = ipaddress.ip_address(host)
                except ValueError:
                    continue
                if ip in net:
                    add_session(s)
        # 手动输入的 IP（一行一个，可统一绑定账号）：直接作为临时会话描述
        # 加入队列，不写进会话管理器，也不在这里打开任何标签。
        for s in spec.get("manual") or []:
            add_session(dict(s))
        return sess_list

    def _find_open_tab(self, session):
        """找一个已打开的同会话标签（避免重复开窗口）。"""
        key = BatchWindow._sess_key(session)
        for t in self.controller.tabs():
            if BatchWindow._sess_key(t.session) == key:
                return t
        return None

    def _run_multi(self):
        code = self.editor.get("1.0", "end")
        targets = self._resolve_multi_targets()
        if not targets:
            messagebox.showwarning("TermX", "没有匹配的运行目标（网段内可能没有会话）",
                                   parent=self)
            return
        # 队列里存"会话描述"，执行时逐个开标签、跑完即关——
        # 不再一次性创建几十个标签窗口把界面卡死。
        self._multi = {"queue": list(targets), "code": code,
                       "lang": self.lang.get(), "total": len(targets),
                       "ok": 0, "fail": 0, "cur": None, "cur_tab": None,
                       "cur_opened": False}
        self.stop_event = threading.Event()
        self.btn_run.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self._append("== 多目标运行：共 %d 个设备（顺序执行，目标：%s） ==" %
                     (len(targets), describe_targets(self.targets_spec)))
        self.status.configure(text="运行中 0/%d ..." % len(targets))
        self.after(50, self._run_next)

    def _run_next(self):
        q = getattr(self, "_multi", None)
        if not q:
            return
        if self.stop_event and self.stop_event.is_set():
            self._append("== 已停止：剩余 %d 个目标未执行 ==" %
                         (len(q["queue"]) + (1 if q["cur"] else 0)))
            q["queue"] = []
        if not q["queue"]:
            self._multi = None
            self._done(True, "多目标运行完成：成功 %d，失败 %d" % (q["ok"], q["fail"]))
            return
        session = q["queue"].pop(0)
        # 复用已打开的同会话标签；没有才临时开一个（跑完会关掉）
        tab = self._find_open_tab(session)
        opened = False
        if tab is None:
            try:
                tab = self.controller.open_session(dict(session), activate=False)
                opened = True
            except Exception as e:
                q["fail"] += 1
                self._append("--- [%s] 打开失败：%s ---" % (session.get("name", ""), e))
                self.status.configure(text="运行中 %d/%d ..." %
                                     (q["ok"] + q["fail"], q["total"]))
                self.after(50, self._run_next)
                return
        q["cur"] = session
        q["cur_tab"] = tab
        q["cur_opened"] = opened
        self._append("--- [%s] 检查目标状态（%s） ---" %
                     (tab.name, "已连接" if tab.is_connected() else "连接中"))
        self._run_with_ready(tab, q["code"], q["lang"], on_done=self._on_one_done)

    # ------------------------------------------------------------------ 目标就绪检测
    # 登录提示关键字：只匹配屏幕最后一行，避免横幅/日志里的 password 误判
    LOGIN_PROMPT_KEYWORDS = (
        "login:", "login：", "username:", "username：", "user name:",
        "password:", "password：", "passwd:", "passphrase", "passcode",
        "用户名", "密码", "账号", "帐号",
    )
    READY_CHECK_INTERVAL = 0.4

    def _ready_timeout(self):
        """就绪检测超时（秒），可在全局设置 script_ready_timeout 覆盖。"""
        try:
            return max(5, int(self.controller.cfg.get("script_ready_timeout") or 60))
        except Exception:
            return 60

    def _last_screen_line(self, tab):
        try:
            text = tab.screen_text() or ""
        except Exception:
            return ""
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        return lines[-1].lower() if lines else ""

    def _wait_ready(self, tab, stop):
        """阻塞等待目标就绪：已连接，且屏幕最后一行不再是登录提示（已登录）。

        在工作线程中调用；返回 (ok, 失败原因)。自动登录/登录脚本应答期间
        屏幕会停留在 Username:/Password: 等提示，检测到即继续等待。
        """
        if (tab.session.get("protocol") or "") == "LocalShell":
            return True, ""                       # 本地 Shell 无登录概念
        timeout = self._ready_timeout()
        deadline = time.time() + timeout
        # 阶段一：等待连接建立（连接中/自动重连中都会走到这里等待）
        while not tab.is_connected():
            if stop is not None and stop.is_set():
                return False, "已停止"
            if tab.user_closed:
                return False, "连接已断开（标签已被手动关闭），请先重新连接"
            if time.time() >= deadline:
                return False, "等待目标连接超时（%d 秒）" % timeout
            time.sleep(self.READY_CHECK_INTERVAL)
        # 阶段二：等待登录完成（屏幕出现命令提示符、不再停留在登录提示）
        while True:
            if stop is not None and stop.is_set():
                return False, "已停止"
            if not tab.is_connected():
                return False, "连接在登录完成前断开"
            if time.time() >= deadline:
                return False, ("等待登录完成超时（%d 秒）：屏幕未出现命令提示符，"
                               "可能未登录或需要人工输入凭据" % timeout)
            last = self._last_screen_line(tab)
            if not last or any(k in last for k in self.LOGIN_PROMPT_KEYWORDS):
                time.sleep(self.READY_CHECK_INTERVAL)
                continue
            # 最后一行不是登录提示：等输出静止后再确认一次（排除横幅/回显未刷完）
            try:
                tab.wait_quiet(0.6, stop=stop)
            except Exception:
                time.sleep(0.6)
            last = self._last_screen_line(tab)
            if not last or any(k in last for k in self.LOGIN_PROMPT_KEYWORDS):
                continue
            return True, ""

    def _run_with_ready(self, tab, code, lang, on_done):
        """先确保目标已连接并处于登录状态，再执行脚本。"""
        stop = self.stop_event

        def worker():
            ok, msg = self._wait_ready(tab, stop)

            def finish():
                if ok:
                    self._append("[%s] 目标就绪（已连接且已登录），开始执行" % tab.name)
                    self.controller.engine.run(code, "script", tab, lang,
                                               on_done=on_done, on_log=self._log,
                                               stop_event=stop)
                else:
                    on_done(False, msg)

            self.controller.ui_call(finish)

        threading.Thread(target=worker, daemon=True,
                         name="ready-" + tab.name).start()

    def _on_one_done(self, ok, msg):
        def finish():
            q = getattr(self, "_multi", None)
            if q is None:
                return
            q["ok" if ok else "fail"] += 1
            if msg and str(msg).strip():
                self._append(str(msg).strip())
            cur = q.get("cur") or {}
            self._append("--- [%s] %s ---" %
                         (cur.get("name", ""), "完成" if ok else "失败"))
            # 临时开的标签用完即关，避免几十个窗口堆着；用户自己打开的不动
            if q.get("cur_opened") and q.get("cur_tab") is not None:
                try:
                    self.controller.close_tab(q["cur_tab"])
                except Exception:
                    pass
                q["cur_tab"] = None
                q["cur_opened"] = False
            done = q["ok"] + q["fail"]
            self.status.configure(text="运行中 %d/%d ..." % (done, q["total"]))
            self.after(50, self._run_next)
        self.controller.ui_call(finish)

    def _run(self):
        self._refresh_tabs()
        if getattr(self, "targets_spec", None):
            self._run_multi()
            return
        code = self.editor.get("1.0", "end")
        idx = self.tab_idx.get()
        tab = self.controller.active_tab() if idx == "当前标签" else \
            self.controller.tab_at(int(idx.split(":")[0]) - 1)
        if tab is None:
            messagebox.showwarning("TermX", "没有可用的标签页", parent=self)
            return
        self.stop_event = threading.Event()
        self.btn_run.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.status.configure(text="检查目标状态 ...")
        self._run_with_ready(tab, code, self.lang.get(), on_done=self._done)

    def _done(self, ok, msg):
        def finish():
            self.btn_run.configure(state="normal")
            self.btn_stop.configure(state="disabled")
            self.status.configure(text="完成" if ok else "失败")
            if msg:
                self._append(msg.strip())
        self.controller.ui_call(finish)

    def _stop(self):
        if self.stop_event:
            self.stop_event.set()
            self._append("== 已请求停止 ==")


def describe_targets(spec):
    """把多目标规格转成一句话摘要。"""
    if not spec:
        return "当前标签（单会话）"
    parts = []
    if spec.get("all"):
        parts.append("全部会话")
    if spec.get("groups"):
        parts.append("分组[%s]" % ",".join(spec["groups"]))
    if spec.get("sessions"):
        parts.append("%d 个会话" % len(spec["sessions"]))
    if spec.get("nets"):
        parts.append("网段[%s]" % ",".join(str(n) for n in spec["nets"]))
    if spec.get("manual"):
        parts.append("手动 IP %d 台" % len(spec["manual"]))
    return " ".join(parts) if parts else "未选择"


class TargetSelectDialog(tk.Toplevel):
    """脚本运行目标选择：全部会话 / 分组 / 具体会话 / 网段 / 手动输入 IP + 账号。"""

    def __init__(self, parent, controller):
        tk.Toplevel.__init__(self, parent)
        self.title("选择运行目标")
        self.geometry("660x680")
        self.minsize(600, 600)
        self.transient(parent)
        self.controller = controller
        self.spec = None

        tip = ttk.Label(self, text="支持多选（Ctrl/Shift 点选）；未打开的会话运行时会自动连接；"
                                   "网段如 10.36.36.0/24，多个用英文逗号分隔，匹配会话主机 IP。",
                        wraplength=620, justify="left", foreground="#5A5A5A")
        tip.pack(fill="x", padx=12, pady=(10, 4))

        self.all_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(self, text="全部会话（会话管理器中的所有会话，本地 Shell 除外）",
                        variable=self.all_var).pack(anchor="w", padx=12)

        mid = ttk.Frame(self); mid.pack(fill="both", expand=True, padx=12, pady=6)
        gbox = ttk.LabelFrame(mid, text="按分组选择（可多选）")
        gbox.pack(side="left", fill="both", expand=True, padx=(0, 6))
        self.group_lb = tk.Listbox(gbox, selectmode="extended", exportselection=False)
        gsb = ttk.Scrollbar(gbox, command=self.group_lb.yview)
        self.group_lb.configure(yscrollcommand=gsb.set)
        gsb.pack(side="right", fill="y")
        self.group_lb.pack(fill="both", expand=True, padx=4, pady=4)

        sbox = ttk.LabelFrame(mid, text="按会话选择（可多选）")
        sbox.pack(side="left", fill="both", expand=True)
        self.sess_lb = tk.Listbox(sbox, selectmode="extended", exportselection=False)
        ssb = ttk.Scrollbar(sbox, command=self.sess_lb.yview)
        self.sess_lb.configure(yscrollcommand=ssb.set)
        ssb.pack(side="right", fill="y")
        self.sess_lb.pack(fill="both", expand=True, padx=4, pady=4)

        nrow = ttk.Frame(self); nrow.pack(fill="x", padx=12, pady=4)
        ttk.Label(nrow, text="网段(CIDR):").pack(side="left")
        self.net_var = tk.StringVar()
        e = ttk.Entry(nrow, textvariable=self.net_var)
        e.pack(side="left", fill="x", expand=True, padx=4)

        # ---------------- 手动输入多个 IP，统一绑定一个账号 ----------------
        mbox = ttk.LabelFrame(self, text="手动输入 IP（一行一个，可写 IP:端口；"
                                         "统一绑定一个账号，批量添加为运行目标）")
        mbox.pack(fill="x", padx=12, pady=(6, 2))
        mtop = ttk.Frame(mbox); mtop.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Label(mtop, text="账号:", width=8).pack(side="left")
        self.manual_account = tk.StringVar(value=ACCOUNT_NONE)
        self.manual_account_cb = ttk.Combobox(
            mtop, textvariable=self.manual_account, state="readonly",
            values=_account_choices())
        self.manual_account_cb.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ttk.Button(mtop, text="管理账号...", width=11,
                   command=self._manage_accounts).pack(side="left", padx=(0, 8))
        ttk.Label(mtop, text="端口:").pack(side="left")
        self.manual_port = tk.StringVar(value="22")
        ttk.Entry(mtop, textvariable=self.manual_port, width=6).pack(side="left")
        self.manual_info = tk.StringVar(value="")
        ttk.Label(mbox, textvariable=self.manual_info, wraplength=610,
                  justify="left", foreground="#5A5A5A").pack(
                      fill="x", padx=6, pady=(0, 2))
        self.ip_text = tk.Text(mbox, height=5, wrap="none", undo=True)
        isb = ttk.Scrollbar(mbox, orient="vertical", command=self.ip_text.yview)
        self.ip_text.configure(yscrollcommand=isb.set)
        isb.pack(side="right", fill="y", padx=(0, 6), pady=(0, 6))
        self.ip_text.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.ip_text.insert("1.0", "# 示例（# 开头为注释行，可删除）：\n"
                                   "# 10.36.36.11\n# 10.36.36.12:2222\n")
        self.manual_account_cb.bind("<<ComboboxSelected>>",
                                    lambda ev: self._update_manual_info())
        self._update_manual_info()

        bar = ttk.Frame(self); bar.pack(fill="x", padx=12, pady=8)
        ttk.Button(bar, text="确定", command=self._ok).pack(side="right", padx=4)
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="right")

        # 填充数据
        try:
            groups = [f for f in (controller.store.folders or []) if f]
        except Exception:
            groups = []
        for g in groups:
            self.group_lb.insert("end", g)
        self._sessions = []
        try:
            all_sessions = controller.store.all()
        except Exception:
            all_sessions = []
        for s in all_sessions:
            if (s.get("protocol") or "") == "LocalShell":
                continue
            label = "%s (%s %s)" % (s.get("name", "未命名"), s.get("protocol", ""),
                                    s.get("host", ""))
            self.sess_lb.insert("end", label)
            self._sessions.append(s)
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()

    def _manage_accounts(self):
        AccountManagerDialog(
            self, on_change=lambda: (
                self.manual_account_cb.configure(values=_account_choices()),
                self._update_manual_info()))

    def _update_manual_info(self):
        label = self.manual_account.get()
        if not label or label == ACCOUNT_NONE:
            self.manual_info.set("未选账号：这些 IP 将不携带凭据，"
                                 "连接时会停在登录提示等待人工输入。")
            return
        try:
            a = AccountStore().by_label(label)
        except Exception:
            a = None
        if not a:
            self.manual_info.set("所选账号已不存在，请重新选择。")
            return
        self.manual_info.set(
            "统一绑定账号：%s（登录用户名 %s）；密码在账号管理里改一次即全部生效。"
            % (AccountStore.label(a), a.get("username", "") or "未设置"))

    def _parse_manual_ips(self):
        """把文本框内容解析成 (host, port) 列表，返回 (列表, 错误信息)。"""
        try:
            default_port = int(float(self.manual_port.get() or 22))
        except Exception:
            return [], "端口必须是数字"
        result = []
        seen = set()
        bad = []
        for raw in self.ip_text.get("1.0", "end").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            host, port = line, default_port
            # 支持 IP:端口 / [IPv6]:端口
            if line.startswith("["):
                m = re.match(r"^\[([^\]]+)\](?::(\d+))?$", line)
                if m:
                    host, port = m.group(1), int(m.group(2) or default_port)
            elif line.count(":") == 1:
                h, p = line.rsplit(":", 1)
                if p.isdigit():
                    host, port = h, int(p)
            try:
                ipaddress.ip_address(host.strip())
            except ValueError:
                bad.append(line)
                continue
            key = (host.strip(), port)
            if key in seen:
                continue
            seen.add(key)
            result.append(key)
        if bad:
            return [], "以下行不是合法 IP 地址：\n" + "\n".join(bad[:8])
        return result, ""

    def _ok(self):
        nets = []
        for token in re.split(r"[,;,\s]+", self.net_var.get() or ""):
            token = token.strip()
            if not token:
                continue
            try:
                ipaddress.ip_network(token, strict=False)
                nets.append(token)
            except ValueError:
                messagebox.showerror("运行目标", "网段格式不正确：%s\n示例：10.36.36.0/24" % token,
                                     parent=self)
                return
        groups = [self.group_lb.get(i) for i in self.group_lb.curselection()]
        sessions = [self._sessions[i] for i in self.sess_lb.curselection()]
        all_flag = self.all_var.get()

        manual_raw, err = self._parse_manual_ips()
        if err:
            messagebox.showerror("运行目标", err, parent=self)
            return
        manual = []
        if manual_raw:
            label = self.manual_account.get()
            acc = None
            if label and label != ACCOUNT_NONE:
                try:
                    acc = AccountStore().by_label(label)
                except Exception:
                    acc = None
                if not acc:
                    messagebox.showerror("运行目标", "所选账号已不存在，请重新选择。",
                                         parent=self)
                    return
            from .sessions import new_session, get_username
            for host, port in manual_raw:
                s = new_session("SSH2", host)
                s["host"] = host
                s["port"] = port
                if acc:
                    s["account_id"] = acc.get("id", "") or ""
                    s["username"] = get_username(s)
                    s["password"] = ""
                    s["password_enc"] = ""
                manual.append(s)

        if not (all_flag or groups or sessions or nets or manual):
            messagebox.showwarning("运行目标", "请至少选择一种运行目标", parent=self)
            return
        self.spec = {"all": all_flag, "groups": groups,
                     "sessions": [dict(s) for s in sessions], "nets": nets,
                     "manual": manual}
        self.destroy()


PY_TEMPLATE = '''# TermX Python 脚本（兼容 SecureCRT crt 对象常用 API）
# 例：自动登录并采集信息
crt.Screen.Synchronous = True
crt.Screen.WaitForString("login:", 20)
crt.Screen.Send("admin\\r")
crt.Screen.WaitForString("Password:", 20)
crt.Screen.Send("your-password\\r")
crt.Screen.WaitForString("$", 20)
crt.Screen.Send("uname -a\\r")
idx = crt.Screen.WaitForStrings(["$", "#"], 20)
crt.Dialog.MessageBox("执行完成，匹配序号=%d" % crt.Screen.MatchIndex)
crt.Sleep(200)
'''

JS_TEMPLATE = '''// TermX JScript 脚本（由内置脚本引擎执行，无需系统组件）
crt.Screen.WaitForString("login:", 20);
crt.Screen.Send("admin\\r");
crt.Screen.WaitForString("Password:", 20);
crt.Screen.Send("your-password\\r");
crt.Screen.Send("show version\\r");
crt.Sleep(1000);
crt.Dialog.MessageBox("执行完成");
'''

VBS_TEMPLATE = '''' TermX VBScript 脚本（由内置脚本引擎执行，无需系统组件）
crt.Screen.WaitForString "login:", 20
crt.Screen.Send "admin" & vbCr
crt.Screen.WaitForString "Password:", 20
crt.Screen.Send "your-password" & vbCr
crt.Screen.Send "show version" & vbCr
crt.Dialog.MessageBox "执行完成"
'''

CMD_TEMPLATE = '''# 命令脚本：每行一条，发送到当前会话
# 支持 SLEEP 毫秒、WAIT "提示符" 超时秒
show clock
WAIT "#" 10
show version
SLEEP 500
'''


class TransferDialog(tk.Toplevel):
    """SFTP/SCP 文件传输与端口转发管理。"""
    def __init__(self, parent, controller, tab=None):
        tk.Toplevel.__init__(self, parent)
        self.title("文件传输 / 端口转发")
        self.transient(parent)
        self.geometry("640x420")
        self.controller = controller
        self.tab = tab or controller.active_tab()
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        nb.add(self._build_transfer(nb), text="上传 / 下载")
        nb.add(self._build_forward(nb), text="端口转发")
        nb.add(self._build_zmodem(nb), text="Zmodem / 说明")

    def _build_transfer(self, nb):
        f = ttk.Frame(nb)
        ttk.Label(f, text="使用随包内置的 scp.exe / sftp.exe，不依赖系统 OpenSSH",
                  foreground="#4A90D9").pack(anchor="w", padx=10, pady=10)
        self.local_var = tk.StringVar()
        self.remote_var = tk.StringVar(value="~/")
        rows = [("本地文件", self.local_var, self._pick_local),
                ("远程路径", self.remote_var, None)]
        for label, var, cmd in rows:
            r = ttk.Frame(f); r.pack(fill="x", padx=10, pady=6)
            ttk.Label(r, text=label, width=12).pack(side="left")
            ttk.Entry(r, textvariable=var).pack(side="left", fill="x", expand=True)
            if cmd:
                ttk.Button(r, text="浏览", command=cmd).pack(side="left", padx=4)
        bar = ttk.Frame(f); bar.pack(fill="x", padx=10, pady=10)
        ttk.Button(bar, text="上传 (scp)", command=self._upload).pack(side="left", padx=4)
        ttk.Button(bar, text="下载 (scp)", command=self._download).pack(side="left", padx=4)
        ttk.Button(bar, text="打开交互式 SFTP 标签", command=self._open_sftp).pack(side="left", padx=4)
        self.tr_log = tk.Text(f, height=9, state="disabled")
        self.tr_log.pack(fill="both", expand=True, padx=10, pady=6)
        return f

    def _build_forward(self, nb):
        f = ttk.Frame(nb)
        cols = ("kind", "addr", "port", "target", "target_port")
        tree = ttk.Treeview(f, columns=cols, show="headings")
        for c, t in zip(cols, ("类型", "监听地址", "端口", "目标主机", "目标端口")):
            tree.heading(c, text=t); tree.column(c, width=110)
        tree.pack(fill="both", expand=True, padx=10, pady=10)
        if self.tab:
            for x in self.tab.session.get("forwards") or []:
                tree.insert("", "end", values=[x.get(c, "") for c in cols])
        ttk.Button(f, text="在会话属性中编辑转发规则",
                   command=lambda: self.controller.edit_session(self.tab.session["id"])
                   if self.tab else None).pack(pady=6)
        return f

    def _build_zmodem(self, nb):
        f = ttk.Frame(nb)
        bar = ttk.Frame(f)
        bar.pack(fill="x", padx=10, pady=8)
        ttk.Button(bar, text="Zmodem 上传文件...",
                   command=self._zmodem_upload).pack(side="left", padx=3)
        ttk.Button(bar, text="打开接收目录",
                   command=self._open_downloads).pack(side="left", padx=3)
        t = tk.Text(f, wrap="word", padx=12, pady=10)
        t.pack(fill="both", expand=True)
        t.insert("1.0",
                 "Zmodem 协议栈已内置（纯 Python 实现，不需要 rz.exe / sz.exe）\n\n"
                 "· 接收：在远端执行 sz 文件名，TermX 自动接管字节流并把文件\n"
                 "  保存到 TermX_Data/downloads，状态栏显示进度。\n"
                 "· 上传：点上面的「Zmodem 上传文件...」，再在远端执行 rz 即可。\n"
                 "· 远端只需安装常规 lrzsz 包（Linux 发行版自带或 yum/apt 装一次），\n"
                 "  本地不需要任何额外工具。\n\n"
                 "SFTP 标签提供 ls / cd / get / put / rm / mkdir 等交互式命令，\n"
                 "底层使用随包内置 sftp.exe，兼容 OpenSSH 服务器与密钥认证。")
        t.configure(state="disabled")
        return f

    def _zmodem_upload(self):
        paths = filedialog.askopenfilenames(parent=self, title="选择要上传的文件")
        if paths and self.tab:
            self.tab.start_zmodem_upload(paths)
            self._log("已启动 Zmodem 上传：%d 个文件（请在远端执行 rz）" % len(paths))

    def _open_downloads(self):
        d = path_of("downloads")
        try:
            os.makedirs(d, exist_ok=True)
            os.startfile(d)
        except Exception as e:
            messagebox.showwarning("TermX", str(e), parent=self)

    def _log(self, text):
        self.tr_log.configure(state="normal")
        self.tr_log.insert("end", text + "\n")
        self.tr_log.see("end")
        self.tr_log.configure(state="disabled")

    def _pick_local(self):
        p = filedialog.askopenfilename(parent=self)
        if p:
            self.local_var.set(p)

    def _target(self):
        if not self.tab:
            return ""
        s = self.tab.session
        return ("%s@%s" % (s.get("username", ""), s.get("host", ""))) if s.get("username") \
            else s.get("host", "")

    def _upload(self):
        self.controller.scp_transfer(self.tab, self.local_var.get(),
                                     self.remote_var.get(), upload=True, log=self._log)

    def _download(self):
        self.controller.scp_transfer(self.tab, self.local_var.get(),
                                     self.remote_var.get(), upload=False, log=self._log)

    def _open_sftp(self):
        if self.tab:
            self.controller.open_sftp(self.tab.session)


class CustomizeDialog(tk.Toplevel):
    """自定义：关键字高亮、发送字符串按钮栏、宏与快捷键、按键映射。"""

    PAGES = [
        ("关键字高亮", "key_highlights",
         [("text", "关键字"), ("fg", "前景色"), ("bg", "背景色")],
         {"text": "", "fg": "", "bg": "#B8860B"}),
        ("发送字符串按钮栏", "send_strings",
         [("name", "按钮名称"), ("text", "发送内容")],
         {"name": "常用命令", "text": ""}),
        ("宏 / 快捷键", "macros",
         [("name", "名称"), ("key", "快捷键"), ("text", "发送内容"), ("script", "脚本路径")],
         {"name": "宏", "key": "<Control-Alt-m>", "text": "", "script": ""}),
        ("按键映射", "keymap",
         [("key", "按键(如 Control-Alt-A)"), ("value", "替换为")],
         {"key": "", "value": ""}),
    ]

    def __init__(self, parent, cfg, on_save=None):
        tk.Toplevel.__init__(self, parent)
        self.title("自定义 · 高亮 / 按钮栏 / 宏 / 键位")
        self.geometry("760x520")
        self.transient(parent)
        self.cfg = cfg
        self.on_save = on_save
        self.pages = {}
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        for title, key, cols, defaults in self.PAGES:
            frame = ttk.Frame(nb)
            nb.add(frame, text=title)
            self._build_page(frame, key, cols, defaults)
        help_frame = ttk.Frame(nb)
        nb.add(help_frame, text="使用说明")
        self._build_help_page(help_frame)
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=6)
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="right", padx=4)
        ttk.Button(bar, text="保存", command=self._save).pack(side="right", padx=4)
        ttk.Label(bar, text="提示：\\r=回车  \\n=换行  \\t=Tab；宏快捷键用 Tk 事件格式，如 <Control-Alt-m>",
                  foreground="#888888").pack(side="left", padx=4)
        self.grab_set()

    def _build_page(self, parent, key, cols, defaults):
        tree = ttk.Treeview(parent, columns=[c for c, _ in cols], show="headings", height=12)
        for c, label in cols:
            tree.heading(c, text=label)
            tree.column(c, width=170)
        sb = ttk.Scrollbar(parent, command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True, padx=6, pady=6)
        data = self.cfg.get(key) or []
        if isinstance(data, dict):
            data = [{"key": k, "value": v} for k, v in data.items()]
        for item in data:
            tree.insert("", "end", values=[item.get(c, "") for c, _ in cols])
        editor = ttk.Frame(parent)
        editor.pack(fill="x", padx=6, pady=4)
        entries = {}
        for c, label in cols:
            ttk.Label(editor, text=label, width=16).pack(side="left")
            var = tk.StringVar(value=str(defaults.get(c, "")))
            ttk.Entry(editor, textvariable=var, width=18).pack(side="left", padx=2)
            entries[c] = var
        ttk.Button(editor, text="添加", command=lambda: self._add(tree, entries, cols)).pack(side="left", padx=6)
        ttk.Button(editor, text="删除选中", command=lambda: self._del(tree)).pack(side="left", padx=2)
        self.pages[key] = (tree, [c for c, _ in cols], entries)

    @staticmethod
    def _add(tree, entries, cols):
        vals = [entries[c].get() for c, _ in cols]
        if any(vals):
            tree.insert("", "end", values=vals)

    def _build_help_page(self, parent):
        """使用说明：每个功能页的详细设置教程。"""
        style = ttk.Style(self)
        bg = style.lookup("TFrame", "background") or "#FFFFFF"
        fg = style.lookup("TLabel", "foreground") or "#202020"
        muted = style.lookup("TLabel", "foreground") or "#6A6A6A"
        try:
            accent = style.lookup("TNotebook.Tab", "foreground") or fg
        except Exception:
            accent = fg
        txt = tk.Text(parent, wrap="word", relief="flat", bd=0,
                      bg=bg, fg=fg, padx=14, pady=10,
                      font=ui_theme.UI_FONT, state="normal")
        sb = ttk.Scrollbar(parent, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(fill="both", expand=True)
        mono = ("Consolas", 9)

        txt.tag_configure("h", font=("Microsoft YaHei UI", 11, "bold"),
                          foreground=accent, spacing1=8, spacing3=4)
        txt.tag_configure("b", font=("Microsoft YaHei UI", 9, "bold"))
        txt.tag_configure("m", font=mono, foreground=fg)
        txt.tag_configure("tip", foreground=muted, lmargin2=18)

        def h(s):
            txt.insert("end", s + "\n", "h")

        def t(s, tag=None):
            txt.insert("end", s, tag)

        def line(s, indent=True):
            if indent:
                txt.insert("end", "  ")
            txt.insert("end", s + "\n")

        h("通用操作")
        line("· 窗口顶部标签页切换功能：关键字高亮 / 发送字符串按钮栏 / 宏-快捷键 / 按键映射 / 使用说明。")
        line("· 每个功能页都是「表格 + 编辑行」结构：在下方输入框填好内容，点【添加】加入表格；")
        line("  选中表格中的一行后点【删除选中】即可移除；要修改就先删旧行再重新添加。")
        line("· 修改只有在点【保存】后才生效并写入配置；点【取消】关闭窗口则放弃本次全部修改。")
        line("· 所有配置保存在 D:\\TermX\\settings.json，覆盖安装软件后会自动加载，无需重新设置。")

        h("一、关键字高亮 —— 让终端里的重要信息自动变色")
        t("作用：", "b")
        line("终端输出中出现你设定的关键字时，该文字自动按设定的颜色显示，方便一眼发现 error、fail 等告警。", False)
        t("设置步骤（例：把 error 高亮为红底白字）：", "b")
        line("1. 切到【关键字高亮】页；")
        line("2. 在下方编辑行依次填入：关键字 = error，前景色 = #FFFFFF（白色），背景色 = #CC0000（红色）；")
        line("3. 点【添加】，表格中出现一行「error  #FFFFFF  #CC0000」；")
        line("4. 点【保存】。所有已打开会话中的 error 立即变为红底白字，新开的会话同样生效。")
        t("列说明：", "b")
        line("· 关键字：要高亮的文字。按「不区分大小写的子串」匹配：填 fail 会同时高亮 FAIL、Failed；")
        line("  填的是普通文字而非正则表达式，不会解析 * ? 等特殊符号。")
        line("· 前景色：文字颜色。支持 #RRGGBB 十六进制（如 #FF0000）或英文颜色名（red、yellow、lime 等）；")
        line("  留空 = 不改变文字颜色。")
        line("· 背景色：底色，写法同上；留空 = 不改变底色。（Ctrl+F 查找结果会临时用深黄底显示）")
        t("常用示例：", "b")
        t("        error        ", "m"); t("前景 #FFFFFF   背景 #CC0000", "tip"); t("  → 红底白字，突出错误\n", None)
        t("        fail         ", "m"); t("前景 #FF5555   背景 留空", "tip"); t("  → 红字，不打底色\n", None)
        t("        shutdown     ", "m"); t("前景 #FFFF55   背景 #7A0000", "tip"); t("  → 黄字红底，接口关闭告警\n", None)
        t("        up           ", "m"); t("前景 #55FF55   背景 留空", "tip"); t("  → 绿字，接口正常\n", None)
        t("注意：", "b")
        line("· 关键字不要选太通用的词（如单个字母 e、空格），否则整屏都会变色。")
        line("· 想改颜色：删除旧行，再用新颜色重新添加同一关键字即可；规则按表格顺序生效。")

        h("二、发送字符串按钮栏 —— 一键发送常用命令")
        t("作用：", "b")
        line("保存后每个会话终端上方会出现一排按钮（最多显示 12 个），点一下就把「发送内容」发送到该")
        line("会话，适合高频命令、登录取证等重复操作。", False)
        t("设置步骤（例：一键保存交换机配置）：", "b")
        line("1. 切到【发送字符串按钮栏】页；")
        line("2. 按钮名称 = 保存配置，发送内容 = save\\r；")
        line("3. 点【添加】→【保存】；")
        line("4. 打开任意会话，点上方「保存配置」按钮，相当于手敲 save 后回车。")
        t("转义符（写在「发送内容」里）：", "b")
        line("· \\r = 回车（最常用，多数命令要以 \\r 结尾才会真正执行）")
        line("· \\n = 换行        · \\t = Tab")
        t("示例：", "b")
        t("        show running-config\\r", "m"); t("   查看配置（思科/锐捷）\n", "tip")
        t("        dis cur\\r", "m"); t("                    查看配置（华为）\n", "tip")
        t("        admin\\rpassword\\r", "m"); t("           连续发送两行：先用户名再密码\n", "tip")

        h("三、宏 / 快捷键 —— 按组合键触发发送或脚本")
        t("作用：", "b")
        line("为「发送一段文字」或「运行一个 Python 脚本」绑定快捷键，在软件任意界面按下都会触发。", False)
        t("设置步骤（例：Ctrl+Alt+S 一键保存配置）：", "b")
        line("1. 切到【宏 / 快捷键】页；")
        line("2. 名称 = 保存配置，快捷键 = <Control-Alt-s>，发送内容 = save\\r；")
        line("3. 点【添加】→【保存】；")
        line("4. 之后在任意会话按 Ctrl+Alt+S 即自动发送 save 回车。")
        t("列说明：", "b")
        line("· 名称：仅用于自己辨认，可随意。")
        line("· 快捷键：必须写成 Tk 事件格式 <修饰键-按键>，注意带尖括号。修饰键可用 Control、Alt、Shift")
        line("  任意组合；按键可以是字母、数字或 F1~F12。例：<Control-Alt-m>、<F7>、<Control-Shift-h>。")
        line("· 发送内容：与按钮栏相同，支持 \\r \\n \\t 转义。")
        line("· 脚本路径：可填一个 Python 脚本的完整路径（如 C:\\scripts\\backup.py），脚本由内置脚本引擎")
        line("  执行，可使用 crt.Screen.Send / crt.Screen.WaitForString / crt.Log 等 API。")
        line("  发送内容和脚本可以同时填：先发送文字，再执行脚本。")
        t("注意：", "b")
        line("· 不要占用内置快捷键：Ctrl+T / Ctrl+N / Ctrl+W / Ctrl+F / Ctrl+L / Ctrl+, /")
        line("  F9 / F10 / F11 / Ctrl+Tab / Alt+1~9。")
        line("· 快捷键格式写错（如漏掉尖括号）不会报错但不会生效，请对照示例填写。")

        h("四、按键映射 —— 改写按键含义")
        t("作用：", "b")
        line("把某个按键组合「替换」成一段文字。在终端里按下该组合键时，实际发送的是你设定的内容。", False)
        t("设置步骤（例：F8 一键输入 show version）：", "b")
        line("1. 切到【按键映射】页；")
        line("2. 按键 = F8（这里不带尖括号，直接写 F8；组合键写 Control-Alt-A 这种格式）；")
        line("3. 替换为 = show version\\r；")
        line("4. 点【添加】→【保存】；在会话里按 F8，终端自动输入 show version 并回车。")
        t("与「宏 / 快捷键」的区别：", "b")
        line("· 按键映射只在终端输入焦点时生效，适合「代替手敲」；")
        line("· 宏快捷键全局生效，还能运行脚本，适合「一键完成整套动作」。")

        h("保存与生效")
        line("· 点【保存】后：关键字高亮立即应用到所有已打开会话；按钮栏在重新打开会话标签后出现；")
        line("  宏快捷键、按键映射立即重新绑定，无需重启软件。")
        txt.configure(state="disabled")
        txt.bind("<Key>", lambda e: "break")
        txt.bind("<Button-1>", lambda e: "break")

    @staticmethod
    def _add(tree, entries, cols):
        vals = [entries[c].get() for c, _ in cols]
        if any(vals):
            tree.insert("", "end", values=vals)

    @staticmethod
    def _del(tree):
        for iid in tree.selection():
            tree.delete(iid)

    def _save(self):
        for key, (tree, cols, _entries) in self.pages.items():
            if key == "keymap":
                mapping = {}
                for iid in tree.get_children():
                    v = tree.item(iid, "values")
                    if len(v) >= 2 and v[0]:
                        mapping[v[0]] = v[1]
                self.cfg.set(key, mapping, save=False)
            else:
                items = []
                for iid in tree.get_children():
                    v = tree.item(iid, "values")
                    items.append(dict(zip(cols, v)))
                self.cfg.set(key, items, save=False)
        self.cfg.save()
        if self.on_save:
            self.on_save()
        self.destroy()
