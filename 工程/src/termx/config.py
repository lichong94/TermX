# -*- coding: utf-8 -*-
"""全局配置、配色方案与便携路径。所有数据只写入 exe 同目录，不碰注册表/AppData。"""
import json
import os
import sys
import threading

APP_NAME = "TermX"
APP_TITLE = "TermX 轻量终端"
APP_VER = "1.4.20"
APP_BUILD = "2026.11 · 缺陷修复版"

ENCODINGS = ["UTF-8", "GBK", "GB18030", "Big5", "Shift_JIS", "EUC-KR",
             "ISO-8859-1", "Windows-1252", "KOI8-R", "CP437"]

PROTOCOLS = ["SSH2", "SSH1", "Telnet", "Rlogin", "Raw", "Serial", "LocalShell", "SFTP"]

# 16 色 ANSI 基础色表（按配色方案）
SCHEMES = {
    "Dark+": {
        "fg": "#D4D4D4", "bg": "#1E1E1E", "cursor": "#FFFFFF", "sel": "#264F78",
        "ansi": ["#1E1E1E", "#C01C28", "#26A269", "#A1A100", "#12488B",
                 "#A347BA", "#2AA1B3", "#C4C7C5", "#5E5E5E", "#F66151",
                 "#33DA7A", "#E9AD0C", "#51A1FF", "#D172BC", "#00C7D4", "#FFFFFF"],
    },
    "SecureCRT 经典": {
        "fg": "#C8C8C8", "bg": "#000000", "cursor": "#00FF00", "sel": "#304A6E",
        "ansi": ["#000000", "#B00000", "#00B000", "#B0B000", "#0000B0",
                 "#B000B0", "#00B0B0", "#B0B0B0", "#404040", "#FF4040",
                 "#40FF40", "#FFFF40", "#4040FF", "#FF40FF", "#40FFFF", "#FFFFFF"],
    },
    "Solarized Dark": {
        "fg": "#839496", "bg": "#002B36", "cursor": "#93A1A1", "sel": "#073642",
        "ansi": ["#073642", "#DC322F", "#859900", "#B58900", "#268BD2",
                 "#D33682", "#2AA198", "#EEE8D5", "#002B36", "#CB4B16",
                 "#586E75", "#657B83", "#839496", "#6C71C4", "#93A1A1", "#FDF6E3"],
    },
    "护眼绿": {
        "fg": "#2F4F2F", "bg": "#C7EDCC", "cursor": "#006400", "sel": "#A8D5B5",
        "ansi": ["#C7EDCC", "#A00000", "#008000", "#808000", "#000080",
                 "#800080", "#008080", "#2F4F2F", "#6B8E6B", "#D00000",
                 "#00A000", "#A0A000", "#0000D0", "#A000A0", "#00A0A0", "#101010"],
    },
    "白底": {
        "fg": "#1A1A1A", "bg": "#FFFFFF", "cursor": "#000000", "sel": "#ADD6FF",
        "ansi": ["#FFFFFF", "#C00000", "#008000", "#808000", "#0000C0",
                 "#800080", "#008080", "#C0C0C0", "#808080", "#FF0000",
                 "#00FF00", "#FFFF00", "#0000FF", "#FF00FF", "#00FFFF", "#000000"],
    },
    "One Half Dark": {
        "fg": "#DCDFE4", "bg": "#282C34", "cursor": "#61AFEF", "sel": "#3E4451",
        "ansi": ["#282C34", "#E06C75", "#98C379", "#E5C07B", "#61AFEF",
                 "#C678DD", "#56B6C2", "#DCDFE4", "#5A6374", "#E06C75",
                 "#98C379", "#E5C07B", "#61AFEF", "#C678DD", "#56B6C2", "#FFFFFF"],
    },
    # 参考 Windows Terminal 的 Campbell 配色（深底、亮蓝强调）
    "Windows Terminal 午夜": {
        "fg": "#CCCCCC", "bg": "#0C0C0C", "cursor": "#60CDFF", "sel": "#264F78",
        "ansi": ["#0C0C0C", "#C50F1F", "#13A10E", "#C19C00", "#0037DA",
                 "#881798", "#3A96DD", "#CCCCCC", "#767676", "#E74856",
                 "#16C60C", "#F9F1A5", "#3B78FF", "#B4009E", "#61D6D6", "#F2F2F2"],
    },
    # 参考 MobaXterm / Xshell 的亮色办公配色（白底、蓝链接、低饱和 ANSI）
    "MobaXterm 晴空": {
        "fg": "#1F2933", "bg": "#FFFFFF", "cursor": "#1A6BB5", "sel": "#CBE4FF",
        "ansi": ["#FFFFFF", "#C0392B", "#1E8449", "#B7950B", "#1F618D",
                 "#884EA0", "#138D75", "#52616B", "#7B8A97", "#E74C3C",
                 "#2ECC71", "#D4AC0D", "#2E86C1", "#9B59B6", "#1ABC9C", "#11181F"],
    },
}

DEFAULT_SETTINGS = {
    "font_family": "Consolas",
    "font_size": 12,
    "scheme": "SecureCRT 经典",
    "cols": 100,
    "rows": 30,
    "scrollback": 5000,
    "cursor_style": "block",       # block | bar | underline
    "cursor_blink": True,
    "show_line_gutter": True,      # 终端左侧时间戳 / 行号栏
    "wheel_scrollback_first": True,  # 程序开启鼠标模式时滚轮仍优先回滚
    "opacity": 100,
    "encoding": "UTF-8",
    "backspace": "DEL",            # DEL(\x7f) | BS(\x08)
    "local_echo": False,
    "bracketed_paste": True,
    "confirm_paste_multiline": True,
    "auto_copy_selection": True,
    "word_chars": "-.:_/@",
    "bell_visual": True,
    "log_dir": "logs",
    "auto_log": False,
    "log_timestamp": False,
    "log_strip_ansi": True,
    "ssh_path": "auto",
    "ssh_strict_host_key": "accept-new",
    "ssh_legacy_algo": True,       # 兼容只支持 ssh-rsa/SHA-1 KEX/CBC 的老旧网络设备
    "ssh_alive_interval": 30,
    "ssh_compression": False,
    "ssh_agent_forward": False,
    "ssh_x11": False,
    "proxy_type": "none",          # none | socks5 | http
    "proxy_host": "",
    "proxy_port": 1080,
    "proxy_user": "",
    "proxy_pass": "",
    "local_shell": "powershell.exe",
    "anti_idle": False,
    "anti_idle_interval": 60,
    "anti_idle_string": "",
    "auto_reconnect": False,
    "auto_reconnect_delay": 5,
    "auto_reconnect_times": 3,
    "auto_command": "",
    "login_script": "",
    "script_dir": "scripts",
    "script_engine": "builtin",     # builtin=内置 VBScript/JScript 引擎 | wsh=系统 WSH
    "key_highlights": [],          # [{"text":"error","fg":"","bg":"#..."}]
    "macros": [],                  # [{"name":"","text":"","key":""}]
    "send_strings": [],            # [{"name":"","text":""}]
    "keymap": {},                  # "Control-Shift-A": "..."
    "device_templates": {},        # 自建设备模板 {"名称": {"lang":"commands","body":"..."}}
    "deleted_templates": [],       # 已删除的内置模板名称（黑名单，可用同名自建模板恢复）
    "data_dir": "",                # 运行目录（空=自动：优先 D:\TermX；可在全局选项自定义）
    "show_toolbar": True,
    "show_statusbar": True,
    "show_session_tree": True,
    "tree_width": 210,
    "tab_position": "top",
    "confirm_close_tab": False,
    "single_instance": False,
    "theme_ui": "SecureCRT 经典",
    "theme_sync_scheme": True,  # 切换界面主题时同步套用该主题自带的终端配色
    "ui_rev": 0,                 # 外观版本，用于一次性迁移老配置
    "batch_delay": 0,
    "batch_timeout": 30,
    "batch_wait_prompt": True,
    "batch_parallel": False,
    "batch_stop_on_error": False,
    # 批量下发：连接并登录确认后，先自动执行"关闭分页"命令
    # 内置厂商见 dialogs.PAGING_VENDORS（华为/华三），用户可增删改
    "paging_vendors": {},          # 自建/覆盖的厂商 {"厂商名": "命令"}
    "paging_deleted": [],          # 已删除的内置厂商（黑名单，可用同名自建恢复）
    "paging_active": "",           # 批量下发窗口上次选中的厂商（空=不执行）
}

_lock = threading.RLock()


def app_dir():
    """便携根目录：exe 所在目录；源码运行时取工程目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# 默认数据目录（自动模式）：D 盘根目录下新建文件夹；可用全局选项自定义
DATA_DIR_NAME = "TermX"
_POINTER_NAME = "data_dir.txt"
_resolved_dir = None          # data_dir() 缓存


def _user_base():
    """系统用户级目录：%LOCALAPPDATA%，始终可写且不污染安装目录。"""
    return (os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
            or os.path.expanduser("~"))


def _pointer_dir():
    return os.path.join(_user_base(), DATA_DIR_NAME)


def _pointer_file():
    return os.path.join(_pointer_dir(), _POINTER_NAME)


def _copy_data_tree(src, dest):
    """把 src 下的文件复制到 dest（目标已有同名文件则保留目标，用于"关联已有目录"）。"""
    if not src or not os.path.isdir(src):
        return 0
    if os.path.abspath(src) == os.path.abspath(dest):
        return 0
    import shutil
    count = 0
    for root, _dirs, files in os.walk(src):
        for fn in files:
            if fn in (_POINTER_NAME, ".migrated", ".write_test"):
                continue          # 指针/标记文件不属于数据
            fp = os.path.join(root, fn)
            rel = os.path.relpath(fp, src)
            dp = os.path.join(dest, rel)
            try:
                if os.path.isfile(dp):
                    continue
                os.makedirs(os.path.dirname(dp), exist_ok=True)
                shutil.copy2(fp, dp)
                count += 1
            except Exception:
                continue
    return count


def get_configured_data_dir():
    """读取全局选项里指定的运行目录；返回 None 表示自动。"""
    try:
        pf = _pointer_file()
        if os.path.isfile(pf):
            with open(pf, "r", encoding="utf-8") as f:
                d = f.read().strip()
            if d and os.path.isdir(d):
                return os.path.abspath(d)
    except Exception:
        pass
    return None


def find_existing_data_dirs():
    """检测本机已有的 TermX 数据目录（含 settings.json/sessions.json），供覆盖安装时关联。"""
    out = []
    for c in _existing_candidates():
        try:
            if os.path.isdir(c) and (os.path.isfile(os.path.join(c, "settings.json"))
                                     or os.path.isfile(os.path.join(c, "sessions.json"))):
                out.append(os.path.abspath(c))
        except Exception:
            continue
    return out


def _portable_dir():
    """便携目标目录：exe（或源码）所在目录下的 TermX_Data。

    绿色解压版期望所有运行数据（settings.json / sessions.json /
    accounts.json 等，以及 ssh/、keys/ 子目录）都固定在软件目录里面，
    跟着软件一起走，不散落到 D 盘或上层目录。
    """
    return os.path.join(app_dir(), "TermX_Data")


def _is_portable_dir(d):
    try:
        return os.path.abspath(d) == os.path.abspath(_portable_dir())
    except Exception:
        return False


def _existing_candidates():
    # 便携优先：exe 同目录的 TermX_Data 排最前
    return [_portable_dir(),
            os.path.join("D:\\", DATA_DIR_NAME),
            os.path.join(_user_base(), DATA_DIR_NAME)]


def set_configured_data_dir(path):
    """设置运行目录（path 为空/None=恢复自动）。切换时自动把旧目录数据迁过去，
    目标目录已有同名文件时保留目标（关联语义）。返回解析后的新实际目录。
    只有这里（用户显式指定）才允许创建新文件夹。"""
    global _resolved_dir
    with _lock:
        old = data_dir()
        os.makedirs(_pointer_dir(), exist_ok=True)
        pf = _pointer_file()
        if path:
            d = os.path.abspath(os.path.normpath(path))
            os.makedirs(d, exist_ok=True)          # 用户指定 → 允许创建
            probe = os.path.join(d, ".write_test")
            with open(probe, "w") as f:
                f.write("ok")
            os.remove(probe)
            with open(pf, "w", encoding="utf-8") as f:
                f.write(d)
        else:
            try:
                os.remove(pf)
            except Exception:
                pass
        _resolved_dir = None
        new = data_dir()
        if os.path.abspath(old) != os.path.abspath(new):
            _copy_data_tree(old, new)
        _migrate_legacy(new)
        return new

# 运行时数据文件（迁移时搬这些，其余文件原样复制）
_MIGRATE_FILES = None


def _dir_usable(d):
    """目录已存在且可写（只探测，绝不创建任何文件夹）。"""
    if not os.path.isdir(d):
        return False
    try:
        probe = os.path.join(d, ".write_test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except Exception:
        return False


def _dir_writable(d):
    """目录已存在且可写（探测用，不留下任何文件）。"""
    return _dir_usable(d)


def _suggested_dir():
    r"""自动模式的建议目录：exe 同目录的 TermX_Data（便携优先）。

    exe 所在目录不可写时（例如放在 Program Files 下）才回退到
    %LOCALAPPDATA%\TermX。只返回路径，不创建。
    """
    if _portable_usable():
        return _portable_dir()
    return os.path.join(_user_base(), DATA_DIR_NAME)


def _migrate_legacy(dest):
    """首次启用数据目录时，把旧位置的配置/会话等搬过来（覆盖安装自动加载）。

    便携版的数据要固定在软件目录里，因此把上一版策略留下的
    D:\\TermX、%APPDATA%\\TermX 也纳入迁移源——只复制、不删除源目录，
    旧数据仍可作为备份。仅在目标目录已存在时执行（不主动创建）。
    """
    if not os.path.isdir(dest):
        return
    marker = os.path.join(dest, ".migrated")
    if os.path.isfile(marker):
        return
    sources = [
        os.path.join(app_dir(), "TermX_Data"),
        os.path.join("D:\\", DATA_DIR_NAME),
        os.path.join(os.environ.get("APPDATA", ""), DATA_DIR_NAME),
        os.path.join(_user_base(), DATA_DIR_NAME),
    ]
    for src in sources:
        if not src or not os.path.isdir(src):
            continue
        if os.path.abspath(src) == os.path.abspath(dest):
            continue
        _copy_data_tree(src, dest)
    try:
        with open(marker, "w") as f:
            f.write("migrated from legacy locations")
    except Exception:
        pass


def _portable_usable():
    """exe 所在目录可写时，数据目录就固定在软件目录里（便携语义）。"""
    parent = app_dir()
    try:
        return os.path.isdir(parent) and _dir_writable(parent)
    except Exception:
        return False


def data_dir():
    r"""运行时数据目录：
    1) 全局选项/首次运行显式指定的目录（优先级最高）；
    2) 便携目录 = exe 同目录的 TermX_Data（软件目录可写时无条件使用；
       目录不存在由 ensure_data_dir() 创建，并把旧位置的数据复制过来）；
    3) 非便携场景（exe 目录不可写）才回退：D:\TermX、%LOCALAPPDATA%\TermX
       等已有数据的目录；
    4) 都没有时返回建议路径但不创建。"""
    global _resolved_dir
    with _lock:
        if _resolved_dir:
            return _resolved_dir
        d = get_configured_data_dir()
        if d:
            _resolved_dir = d
            return d
        if _portable_usable():
            _resolved_dir = _portable_dir()
            return _resolved_dir
        for c in _existing_candidates():
            if os.path.isfile(os.path.join(c, "settings.json")) or \
               os.path.isfile(os.path.join(c, "sessions.json")):
                _resolved_dir = os.path.abspath(c)
                return _resolved_dir
        _resolved_dir = _suggested_dir()      # 不存在、不创建，等用户指定
        return _resolved_dir


def _legacy_default_dirs():
    """上一版策略的默认目录：当时软件推荐 D:\\TermX 或 %LOCALAPPDATA%\\TermX。

    指针文件指向这些位置时，基本可以判定是当年首次运行弹窗里"接受默认"
    留下的，而不是用户特意指定的——因此升级时可以安全地切换到软件目录。
    """
    out = set()
    for p in (os.path.join("D:\\", DATA_DIR_NAME),
              os.path.join(_user_base(), DATA_DIR_NAME),
              os.path.join(os.environ.get("APPDATA", ""), DATA_DIR_NAME)):
        try:
            out.add(os.path.abspath(p))
        except Exception:
            pass
    return out


def ensure_data_dir():
    """确保数据目录可用，并返回 (实际目录, 说明文字)。

    便携版（绿色解压）期望数据固定在 exe 同目录的 TermX_Data 里：
    - 目录不存在时直接创建（这就是软件自己的目录，不算"往系统里塞东西"）；
    - 若指针文件仍指向上一版策略的默认位置（D:\\TermX、%LOCALAPPDATA%\\TermX），
      说明那是当年首次运行"接受默认"留下的，这里自动切换到软件目录，
      并把旧数据复制过来（只复制不删源，旧目录留作备份）；
    - 把指针改写为软件目录，之后仍可在 全局选项 → 常规 → 运行目录 里改到别处。

    非便携场景（用户显式指定过其它目录，或 exe 目录不可写）保持原有行为：
    不自动创建，交给首次运行弹窗让用户选。
    """
    global _resolved_dir
    with _lock:
        note = ""
        configured = get_configured_data_dir()
        if configured and _portable_usable():
            try:
                if os.path.abspath(configured) in _legacy_default_dirs():
                    # 旧默认位置 → 升级到软件目录
                    dest = _portable_dir()
                    os.makedirs(dest, exist_ok=True)
                    _copy_data_tree(configured, dest)
                    try:
                        os.makedirs(_pointer_dir(), exist_ok=True)
                        with open(_pointer_file(), "w", encoding="utf-8") as f:
                            f.write(dest)
                    except Exception:
                        pass
                    _resolved_dir = dest
                    note = ("运行目录已切换到软件目录：%s\n"
                            "（原 %s 的数据已复制过来，原目录保留作备份）"
                            % (dest, configured))
                    _migrate_legacy(dest)
                    return dest, note
            except Exception:
                pass

        d = data_dir()
        portable = _is_portable_dir(d)
        if not os.path.isdir(d):
            if portable and not get_configured_data_dir():
                try:
                    os.makedirs(d, exist_ok=True)
                    note = "已创建运行目录：%s" % d
                except Exception as e:
                    note = "运行目录创建失败：%s" % e
            else:
                return d, note
        if portable:
            _migrate_legacy(d)
        return d, note


def needs_setup():
    """尚未指定运行目录且找不到任何已有数据目录 → 首次运行需要弹窗选择。

    便携版（绿色解压）例外：数据目录固定在 exe 同目录的 TermX_Data，
    由 ensure_data_dir() 自动创建，不再打扰用户弹窗选目录。
    """
    d = data_dir()
    if os.path.isdir(d):
        return False
    if _is_portable_dir(d) and not get_configured_data_dir():
        return False        # 便携目录自动创建，不弹窗
    return True


def path_of(*parts):
    return os.path.join(data_dir(), *parts)


LAST_SAVE_ERROR = ""          # 最近一次 settings.json 写盘失败原因（供 UI 提示）


class Config(object):
    def __init__(self):
        self.values = dict(DEFAULT_SETTINGS)
        self.file = path_of("settings.json")
        self.load()

    def load(self):
        try:
            with open(self.file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self.values.update(data)
        except Exception:
            pass

    def rebind(self, file=None):
        """运行目录变更后重新绑定配置文件（不改内存里已有的设置）。"""
        self.file = file or path_of("settings.json")
        self.load()
        return self.file

    def save(self):
        """写盘并返回是否成功；不再静默吞掉错误。

        目录不存在时重建：便携目录（exe 同目录 TermX_Data）由软件自己创建，
        其余情况仅当它是用户显式指定的运行目录才重建，遵守
        「除用户指定目录外不创建任何文件夹」的约束。
        """
        global LAST_SAVE_ERROR
        with _lock:
            tmp = self.file + ".tmp"
            try:
                folder = os.path.dirname(self.file)
                if folder and not os.path.isdir(folder):
                    configured = get_configured_data_dir()
                    portable = _is_portable_dir(folder)
                    if not ((configured and
                             os.path.abspath(configured) == os.path.abspath(folder))
                            or portable):
                        LAST_SAVE_ERROR = "运行目录不存在：%s" % folder
                        return False
                    os.makedirs(folder, exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.values, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self.file)
                LAST_SAVE_ERROR = ""
                return True
            except Exception as e:
                LAST_SAVE_ERROR = "%s: %s" % (type(e).__name__, e)
                return False

    def get(self, key, default=None):
        with _lock:
            return self.values.get(key, DEFAULT_SETTINGS.get(key, default))

    def set(self, key, value, save=True):
        with _lock:
            self.values[key] = value
        if save:
            self.save()

    def scheme(self):
        return SCHEMES.get(self.get("scheme"), SCHEMES["Dark+"])

    def font(self):
        return (self.get("font_family"), self.get("font_size"))


def color_256(n, scheme):
    """把 256 色索引转成 #rrggbb。

    SGR "38;5;<n>" 解析失败时 n 可能是 None / 字符串 / 超范围数字：
    旧实现直接比较大小并拿 ansi[n]，None 会抛 TypeError，负数会走
    负索引取到色表末尾（画面变成莫名其妙的颜色），见测试报告 BUG-06。
    """
    ansi = scheme.get("ansi") or []
    fail = scheme.get("fg", "#C8C8C8")
    try:
        n = int(n)
    except (TypeError, ValueError):
        return fail
    if n < 0 or n > 255:
        return fail
    if n < 16:
        return ansi[n] if n < len(ansi) else fail
    if 16 <= n <= 231:
        idx = n - 16
        r, g, b = idx // 36, (idx % 36) // 6, idx % 6
        levels = (0, 95, 135, 175, 215, 255)
        return "#%02X%02X%02X" % (levels[r], levels[g], levels[b])
    if 232 <= n <= 255:
        v = 8 + (n - 232) * 10
        return "#%02X%02X%02X" % (v, v, v)
    return fail


def parse_color(text):
    """解析 #rgb/#rrggbb/rgb(r,g,b)/颜色名，失败返回 None。"""
    if not text:
        return None
    t = text.strip()
    if t.startswith("#") and len(t) in (4, 7):
        if len(t) == 4:
            t = "#" + "".join(c * 2 for c in t[1:])
        return t[:7].upper()
    if t.lower().startswith("rgb"):
        try:
            nums = [int(x.strip()) for x in t[t.find("(") + 1:t.find(")")].split(",")]
            return "#%02X%02X%02X" % tuple(max(0, min(255, n)) for n in nums[:3])
        except Exception:
            return None
    names = {"black": "#000000", "red": "#FF0000", "green": "#008000",
             "yellow": "#FFFF00", "blue": "#0000FF", "magenta": "#FF00FF",
             "cyan": "#00FFFF", "white": "#FFFFFF"}
    return names.get(t.lower())
