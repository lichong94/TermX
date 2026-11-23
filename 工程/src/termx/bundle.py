# -*- coding: utf-8 -*-
"""内置运行时组件解析层 —— 让 TermX 完全自带依赖。

设计目标：软件解压/拷贝后即可使用，不读取系统安装的 OpenSSH、不要求 VC++
运行库、不要求 WSH、不要求外部 rz/sz、不要求单独安装 X Server。

目录约定（随包分发，名字固定为 TermX_Bin）：
    TermX_Bin/openssh/   OpenSSH 全套（ssh/scp/sftp/ssh-keygen/...）
    TermX_Bin/runtime/   VC++ 运行库 DLL
    TermX_Bin/ssh1/      支持 SSH-1 的 plink.exe
    TermX_Bin/x11/       便携 X Server（VcXsrv）
    TermX_Bin/fonts/     开源等宽字体

单文件 exe 版把同一目录打进 PyInstaller 的 _MEIPASS，运行时直接从临时
解压目录读取；绿色目录版放在 exe 同目录。源码运行时读工程根的 vendor/。
"""
import os
import socket
import subprocess
import sys
import time

BIN_DIR = "TermX_Bin"

# 每个组件的关键文件，用于自检与状态展示
REQUIRED = {
    "openssh": ["ssh.exe", "scp.exe", "sftp.exe", "ssh-keygen.exe",
                "ssh-add.exe", "ssh-agent.exe", "ssh-keyscan.exe"],
    "runtime": ["msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll"],
    "ssh1": ["plink.exe"],
    "fonts": ["Mono.ttf"],
}

_xserver_proc = None


def _roots():
    out = []
    if getattr(sys, "frozen", False):
        out.append(os.path.join(os.path.dirname(sys.executable), BIN_DIR))
        mei = getattr(sys, "_MEIPASS", None)
        if mei:
            out.append(os.path.join(mei, BIN_DIR))
            out.append(mei)
    else:
        # <project>/src/termx/bundle.py -> <project>
        base = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        out.append(os.path.join(base, BIN_DIR))
        out.append(os.path.join(base, "vendor"))
    return [r for r in out if r]


def root():
    """返回第一个真实存在的内置组件根目录。"""
    rs = _roots()
    for r in rs:
        if os.path.isdir(r) and any(os.path.isdir(os.path.join(r, g))
                                    for g in ("openssh", "runtime", "ssh1", "x11", "fonts")):
            return r
    return rs[0] if rs else ""


def group(name):
    for r in _roots():
        p = os.path.join(r, name)
        if os.path.isdir(p):
            return p
    return os.path.join(root(), name)


def tool(group_name, filename, fallback=""):
    """解析一个内置工具的绝对路径；找不到时返回 fallback（通常为空）。"""
    p = os.path.join(group(group_name), filename)
    if os.path.isfile(p):
        return p
    for r in _roots():
        q = os.path.join(r, filename)
        if os.path.isfile(q):
            return q
    return fallback


def bundled_env(extra=None):
    """构造子进程环境：内置工具目录置于 PATH 最前。

    系统 OpenSSH 目录即使存在也不再被依赖；System32 由 Windows 加载器自行
    解析核心 DLL，无需出现在 PATH 里。
    """
    env = dict(os.environ)
    dirs = [group(g) for g in ("openssh", "runtime", "ssh1", "x11", "fonts")]
    dirs = [d for d in dirs if os.path.isdir(d)]
    old = env.get("PATH", "")
    # 去掉系统自带 OpenSSH 目录，保证解析到的永远是随包版本
    kept = [p for p in old.split(";") if p and "OpenSSH" not in p.upper()]
    env["PATH"] = ";".join(dirs + kept)
    env.setdefault("TERM", "xterm")
    if extra:
        env.update({str(k): str(v) for k, v in extra.items() if v is not None})
    return env


def spawn_kwargs(extra_env=None):
    """给 subprocess 用的统一参数（无黑框 + 内置环境）。"""
    kw = {"env": bundled_env(extra_env),
          "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return kw


def apply_current_process():
    """把内置 VC 运行库目录登记到当前进程的 DLL 搜索路径。"""
    d = group("runtime")
    if os.path.isdir(d) and hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(d)
        except Exception:
            pass
    load_font()


def load_font():
    """用 FR_PRIVATE 方式临时注册内置等宽字体（不写注册表、不装系统字体）。"""
    p = tool("fonts", "Mono.ttf")
    if not p or not sys.platform.startswith("win"):
        return False
    try:
        import ctypes
        from ctypes import wintypes
        gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        gdi.AddFontResourceExW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                           ctypes.c_void_p]
        gdi.AddFontResourceExW.restype = ctypes.c_int
        return bool(gdi.AddFontResourceExW(p, 0x10, None))
    except Exception:
        return False


# ------------------------------------------------------------------ SSH 私有身份
def ssh_identity_options():
    """把 known_hosts / ssh_config 收敛到 TermX_Data，不污染用户 .ssh 目录。

    生成的默认 config 会 Include 用户已有的 ~/.ssh/config（如果存在），
    因此用户既有的别名、跳板机配置仍然生效。
    """
    from .config import path_of
    d = path_of("ssh")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    cfg = os.path.join(d, "config").replace("\\", "/")
    kh = os.path.join(d, "known_hosts").replace("\\", "/")
    if not os.path.isfile(cfg):
        user_cfg = os.path.join(os.path.expanduser("~"), ".ssh", "config")
        lines = ["# TermX 专用 OpenSSH 配置（可自由编辑）",
                 "# 随绿色包携带，不会写到用户目录", ""]
        if os.path.isfile(user_cfg):
            lines.append("Include %s" % user_cfg)
        lines += ["", "Host *",
                  "    ServerAliveInterval 30",
                  "    ServerAliveCountMax 3",
                  ""]
        try:
            with open(cfg, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        except Exception:
            pass
    return ["-F", cfg,
            "-o", "UserKnownHostsFile=%s" % kh,
            "-o", "GlobalKnownHostsFile=NUL"]


def keys_dir():
    from .config import path_of
    d = path_of("keys")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


# ------------------------------------------------------------------ X Server
def xserver_exe():
    d = group("x11")
    for name in ("vcxsrv.exe", "XWin.exe", "Xming.exe", "Xorg.exe"):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    try:
        for base, _dirs, files in os.walk(d):
            for name in files:
                if name.lower() in ("vcxsrv.exe", "xwin.exe", "xming.exe"):
                    return os.path.join(base, name)
    except Exception:
        pass
    return ""


def _display_port(display):
    try:
        tail = display.split(":")[-1]
        n = int(tail.split(".")[0])
        return 6000 + n
    except Exception:
        return 6000


def _port_open(port):
    s = socket.socket()
    s.settimeout(0.25)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def ensure_xserver(display=None, emit=None):
    """X11 转发时自动拉起内置 X Server，返回可用的 DISPLAY 字符串。"""
    global _xserver_proc
    disp = display or os.environ.get("DISPLAY") or ":0.0"
    port = _display_port(disp)
    if _port_open(port):
        return disp
    exe = xserver_exe()
    if not exe:
        if emit:
            emit("未找到内置 X Server，X11 转发已降级为通道模式")
        return ""
    if _xserver_proc and _xserver_proc.poll() is None:
        return disp
    args = [exe, ":0", "-multiwindow", "-clipboard", "-silent-dup-error",
            "-ac", "-noreset"]
    try:
        _xserver_proc = subprocess.Popen(
            args, cwd=os.path.dirname(exe),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
            env=bundled_env())
    except Exception as e:
        if emit:
            emit("内置 X Server 启动失败: %s" % e)
        return ""
    for _ in range(20):
        if _port_open(port):
            return disp
        time.sleep(0.2)
    if emit:
        emit("内置 X Server 已启动，正在等待端口 %d" % port)
    return disp


# ------------------------------------------------------------------ 状态 / 自检
def status():
    """返回 [(组件, 是否内置可用, 路径/说明)]，供关于页与自检使用。"""
    rows = []
    labels = {
        "openssh": "OpenSSH 全套 (SSH/SFTP/SCP/密钥)",
        "runtime": "VC++ 运行库",
        "ssh1": "SSH-1 客户端 (plink)",
        "x11": "便携 X Server",
        "fonts": "内置等宽字体",
    }
    for g, title in labels.items():
        d = group(g)
        if g == "x11":
            p = xserver_exe()
            ok = bool(p)
            rows.append((title, ok, p or "未随包提供"))
            continue
        files = REQUIRED.get(g, [])
        missing = [f for f in files if not os.path.isfile(os.path.join(d, f))]
        ok = bool(files) and not missing
        rows.append((title, ok, d if ok else ("缺少: " + ", ".join(missing))))
    return rows


def verify_isolated(emit=None):
    """在不含系统 OpenSSH 的 PATH 下验证内置 ssh.exe 能独立运行。"""
    emit = emit or (lambda s: None)
    exe = tool("openssh", "ssh.exe")
    if not exe:
        return False
    env = bundled_env()
    # 只保留软件自带目录，彻底排除系统 OpenSSH
    env["PATH"] = ";".join([group("openssh"), group("runtime")])
    try:
        p = subprocess.run([exe, "-V"], capture_output=True, text=True,
                           timeout=12, env=env,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        text = ((p.stdout or "") + (p.stderr or "")).strip()
        emit("  内置 OpenSSH: %s" % (text.splitlines()[0] if text else "(无输出)"))
        return p.returncode == 0 and "OpenSSH" in text
    except Exception as e:
        emit("  内置 OpenSSH 验证失败: %s" % e)
        return False
