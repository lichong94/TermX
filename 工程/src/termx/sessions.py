# -*- coding: utf-8 -*-
"""会话模型、便携存储、SecureCRT .ini 导入导出、批量变量表。"""
import base64
import csv
import ctypes
import hashlib
import json
import os
import re
import threading
import time
import uuid

from .config import PROTOCOLS, data_dir, path_of

SESSION_DEFAULTS = {
    "id": "",
    "name": "新建会话",
    "folder": "",
    "protocol": "SSH2",
    "host": "",
    "port": 22,
    "username": "",
    "password": "",
    "password_enc": "",
    "account_id": "",              # 绑定的账号库 ID：绑定后密码跟随账号库，
                                    # 在账号管理里改一次密码，所有绑定的会话都生效
    "auth": "password",           # password | publickey | gssapi | none
    "keyfile": "",
    "passphrase": "",
    "command": "",                # ssh 远程命令
    "no_terminal": False,         # -N 仅端口转发
    "encoding": "",               # 空 = 跟随全局
    "term_type": "xterm",
    "cols": 0,
    "rows": 0,
    "scheme": "",
    "font_family": "",
    "font_size": 0,
    "scrollback": 0,
    "proxy": "inherit",           # inherit | none | custom
    "proxy_type": "socks5",
    "proxy_host": "",
    "proxy_port": 1080,
    "proxy_user": "",
    "proxy_pass": "",
    "ssh_strict": "",
    "ssh_compression": False,
    "ssh_agent_forward": False,
    "ssh_x11": False,
    "ssh_alive_interval": 0,
    "forwards": [],               # [{kind:L|R|D, addr, port, target}]
    "anti_idle": False,
    "anti_idle_interval": 60,
    "anti_idle_string": "",
    "auto_reconnect": False,
    "auto_reconnect_delay": 5,
    "auto_reconnect_times": 3,
    "auto_command": "",
    "login_script": "",
    "log_enabled": False,
    "log_file": "",
    "log_append": True,
    "log_timestamp": False,
    "log_strip_ansi": True,
    "serial_port": "COM1",
    "serial_baud": 9600,
    "serial_data": 8,
    "serial_parity": "N",
    "serial_stop": 1,
    "serial_flow": "none",
    "rlogin_local_user": "",
    "rlogin_remote_user": "",
    "shell_path": "",
    "shell_args": "",
    "shell_cwd": "",
    "sftp_path": "",
    "description": "",
    "label_color": "",
    "tags": "",
    "favorite": False,
    "script": "",
    "created": 0,
    "last_used": 0,
    "use_count": 0,
}


# ---------------------------------------------------------------- DPAPI 密码保护
# 各协议的默认端口（SecureCRT 导入时用于端口缺失/非法时的回退）
PROTO_DEFAULT_PORT = {"SSH2": 22, "SSH1": 22, "SFTP": 22, "Telnet": 23,
                      "Rlogin": 513, "Serial": 0, "Raw": 0, "LocalShell": 0}


def _parse_crt_port(value):
    """解析 SecureCRT 会话文件里的端口字段，失败返回 0。

    - SecureCRT 自己写的 DWORD 是 8 位十六进制：``00000016`` = 22
    - 手写/第三方工具写的是十进制：``22``、``2222``
    旧版一律按十六进制解析，导致 "22" 变成 34（测试报告 BUG-01）。
    """
    v = str(value or "").strip().strip('"')
    if not v:
        return 0
    try:
        if re.match(r"^0[0-9A-Fa-f]{7}$", v):        # SecureCRT DWORD 定宽十六进制
            return int(v, 16)
        if re.match(r"^0[xX][0-9A-Fa-f]+$", v):
            return int(v, 16)
        if re.match(r"^\d+$", v):                    # 十进制
            return int(v, 10)
    except Exception:
        return 0
    return 0

# 加密链降级时的兜底方案标记（内容会被界面提示为“弱保护/需重填”）
DPAPI_WARN = ""
# 最近一次会话库/账号库保存失败的原因（空串表示成功）
LAST_SESSION_SAVE_ERROR = ""
LAST_ACCOUNT_SAVE_ERROR = ""


def _obf_key():
    """降级保护用密钥：由机器名 + 用户名派生（非密码学强度，只防顺手偷看）。

    DPAPI 在跨机器/跨用户导出的会话上必然解不开；降级分支不能退回“纯
    Base64 编码”（那是可逆明文，等同不加密），这里用机器相关派生密钥做
    异或混淆，换一台机器就读不出来，副本拷走也解不出原文。
    """
    seed = "|".join([os.environ.get("COMPUTERNAME", ""),
                     os.environ.get("USERNAME", ""),
                     os.environ.get("USERDOMAIN", ""),
                     "TermX-PWD-OBF-V2"])
    return hashlib.sha256(seed.encode("utf-8", "replace")).digest()


def _obf_encode(text):
    key = _obf_key()
    raw = text.encode("utf-8", "replace")
    out = bytes(b ^ key[i % len(key)] for i, b in enumerate(raw))
    return "OBF2:" + base64.b64encode(out).decode("ascii")


def _obf_decode(text):
    key = _obf_key()
    if text.startswith("OBF2:"):
        raw = base64.b64decode(text[5:])
        return bytes(b ^ key[i % len(key)] for i, b in enumerate(raw)).decode("utf-8", "replace")
    if text.startswith("OBF:"):          # 1.4.19 及更早写的旧格式，保持兼容读取
        return base64.b64decode(text[4:]).decode("utf-8", "replace")
    return ""


def _dpapi(data, protect=True):
    """用当前 Windows 用户凭据保护密码，零外部依赖。"""
    global DPAPI_WARN
    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]
    try:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        if protect:
            raw = data.encode("utf-8")
            src = BLOB(len(raw), ctypes.cast(ctypes.create_string_buffer(raw, len(raw)),
                                             ctypes.POINTER(ctypes.c_ubyte)))
            dst = BLOB()
            ok = crypt32.CryptProtectData(ctypes.byref(src), None, None, None, None, 0x01,
                                          ctypes.byref(dst))
            if not ok:
                # 加密失败也不能把密码写成空串（那样等于悄悄丢密码），
                # 退回机器相关混淆并留提示。
                DPAPI_WARN = "DPAPI 加密失败，密码改用弱保护存储"
                return _obf_encode(data) if data else ""
            buf = ctypes.string_at(dst.pbData, dst.cbData)
            kernel32.LocalFree(dst.pbData)
            return "DPAPI:" + base64.b64encode(buf).decode("ascii")
        else:
            if data.startswith("OBF2:") or data.startswith("OBF:"):
                try:
                    return _obf_decode(data)
                except Exception:
                    DPAPI_WARN = "密码为弱保护格式且本机无法还原，请重新输入"
                    return ""
            if not data.startswith("DPAPI:"):
                return data
            raw = base64.b64decode(data[6:])
            src = BLOB(len(raw), ctypes.cast(ctypes.create_string_buffer(raw, len(raw)),
                                             ctypes.POINTER(ctypes.c_ubyte)))
            dst = BLOB()
            ok = crypt32.CryptUnprotectData(ctypes.byref(src), None, None, None, None, 0x01,
                                            ctypes.byref(dst))
            if not ok:
                # 典型场景：会话文件从别的机器/别的 Windows 用户拷过来
                DPAPI_WARN = "该密码由其他机器或用户加密，本机无法解开，请重新填写"
                return ""
            buf = ctypes.string_at(dst.pbData, dst.cbData)
            kernel32.LocalFree(dst.pbData)
            return buf.decode("utf-8", "replace")
    except Exception:
        if protect:
            # 极少数环境没有 crypt32（如 Wine）：同样只用机器相关混淆，不再写裸 Base64
            DPAPI_WARN = "当前环境不支持 DPAPI，密码改用弱保护存储"
            return _obf_encode(data) if data else ""
        try:
            return _obf_decode(data)
        except Exception:
            DPAPI_WARN = "密码为弱保护格式且本机无法还原，请重新输入"
            return ""


def get_username(session):
    """取会话用户名。

    若会话绑定了账号库（account_id），**以账号库里的用户名为准**——
    与 get_password 同一套规则：在「工具 → 账号管理」里改了用户名或密码，
    所有绑定该账号的会话立即生效，不必逐个会话重填。
    """
    aid = session.get("account_id") or ""
    if aid:
        try:
            acc = AccountStore().get(aid)
        except Exception:
            acc = None
        if acc:
            user = acc.get("username") or ""
            if user:
                return user
    return session.get("username") or ""


def get_password(session):
    """取会话密码。

    若会话绑定了账号库（account_id），**始终以账号库里的密码为准**——
    这样在「工具 → 账号管理」里改了密码，所有绑定该账号的会话立即生效，
    不必逐个会话重填。未绑定账号时用会话自身保存的密码。
    """
    aid = session.get("account_id") or ""
    if aid:
        try:
            acc = AccountStore().get(aid)
        except Exception:
            acc = None
        if acc:
            pwd = AccountStore.password(acc)
            if pwd:
                return pwd
    enc = session.get("password_enc", "")
    if enc:
        return _dpapi(enc, False)
    return session.get("password", "")


def set_password(session, plain):
    session["password"] = ""
    session["password_enc"] = _dpapi(plain or "", True) if plain else ""


def new_session(protocol="SSH2", name=None):
    s = dict(SESSION_DEFAULTS)
    s["id"] = uuid.uuid4().hex[:12]
    s["protocol"] = protocol
    s["name"] = name or ("新建%s会话" % protocol)
    s["created"] = int(time.time())
    defaults = {
        "SSH2": 22, "SSH1": 22, "Telnet": 23, "Rlogin": 513,
        "Raw": 23, "Serial": 0, "LocalShell": 0, "SFTP": 22,
    }
    s["port"] = defaults.get(protocol, 22)
    s["forwards"] = []
    return s


class SessionStore(object):
    def __init__(self):
        self.file = path_of("sessions.json")
        self.sessions = []
        self.folders = []
        self._lock = threading.RLock()
        # 保存失败时的界面回调（由 MainWindow 注册），不注册就只记错误字符串
        self.on_save_error = None
        self.load()

    def load(self):
        with self._lock:
            try:
                with open(self.file, "r", encoding="utf-8") as f:
                    d = json.load(f)
                sessions = d.get("sessions", [])
                folders = d.get("folders", [])
            except Exception:
                sessions, folders = [], []
            self.sessions, repaired = self._normalize(sessions)
            self.folders = [x for x in (folders or []) if isinstance(x, str) and x]
        if repaired:
            # 把修复结果写回，避免每次启动都重复修复
            self.save()
        if not self.sessions:
            self._seed()

    @staticmethod
    def _normalize(sessions):
        """修复历史数据：缺 id 补新 id；重复 id 去掉完全相同项，
        内容不同的重新分配 id 保留数据。返回 (列表, 是否修复过)。"""
        out, seen, repaired = [], {}, False
        for raw in sessions or []:
            if not isinstance(raw, dict):
                repaired = True
                continue
            s = dict(raw)
            sid = str(s.get("id") or "").strip()
            if not sid:
                sid = uuid.uuid4().hex[:12]
                s["id"] = sid
                repaired = True
            if sid in seen:
                if seen[sid] == s:
                    repaired = True       # 完全重复，直接丢弃
                    continue
                new_id = uuid.uuid4().hex[:12]
                s["id"] = new_id         # 内容不同，换 id 保留
                sid = new_id
                repaired = True
            seen[sid] = s
            out.append(s)
        return out, repaired

    def _seed(self):
        demo1 = new_session("LocalShell", "本地 PowerShell")
        demo1["folder"] = "示例"
        demo1["shell_path"] = "powershell.exe"
        demo1["description"] = "本机命令行，双击即可连接"
        demo2 = new_session("LocalShell", "本地 CMD")
        demo2["folder"] = "示例"
        demo2["shell_path"] = "cmd.exe"
        self.sessions = [demo1, demo2]
        self.folders = ["示例"]
        self.save()

    def save(self):
        """写盘并返回是否成功；失败原因留在 LAST_SESSION_SAVE_ERROR。

        1.4.19 及以前这里是 except: pass，磁盘写满、目录被删、文件被占用
        时新建/修改的会话会静默丢失，界面完全无感（测试报告 BUG-04）。
        """
        global LAST_SESSION_SAVE_ERROR
        with self._lock:
            tmp = self.file + ".tmp"
            try:
                folder = os.path.dirname(self.file)
                if folder and not os.path.isdir(folder):
                    # 数据目录由用户/软件运行目录决定，这里不擅自新建，
                    # 直接报错让用户看到原因
                    LAST_SESSION_SAVE_ERROR = "会话目录不存在：%s" % folder
                    self._notify_error(LAST_SESSION_SAVE_ERROR)
                    return False
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"version": 1, "folders": self.folders,
                               "sessions": self.sessions}, f, ensure_ascii=False, indent=1)
                os.replace(tmp, self.file)
                LAST_SESSION_SAVE_ERROR = ""
                return True
            except Exception as e:
                LAST_SESSION_SAVE_ERROR = "%s: %s" % (type(e).__name__, e)
                try:
                    if os.path.isfile(tmp):
                        os.remove(tmp)
                except Exception:
                    pass
                self._notify_error(LAST_SESSION_SAVE_ERROR)
                return False

    def _notify_error(self, msg):
        cb = getattr(self, "on_save_error", None)
        if cb:
            try:
                cb(msg)
            except Exception:
                pass

    def last_error(self):
        """最近一次保存失败的原因（空串表示上次保存成功）。"""
        return LAST_SESSION_SAVE_ERROR

    def all(self):
        with self._lock:
            return [dict(s) for s in self.sessions]

    def get(self, sid):
        with self._lock:
            for s in self.sessions:
                if s.get("id") == sid:
                    return dict(s)
        return None

    def add(self, s):
        with self._lock:
            s = dict(s)
            sid = s.get("id")
            if sid:                       # 同 id 视为覆盖，杜绝重复树项
                for i, old in enumerate(self.sessions):
                    if old.get("id") == sid:
                        self.sessions[i] = s
                        break
                else:
                    self.sessions.append(s)
            else:
                self.sessions.append(s)
            if s.get("folder") and s["folder"] not in self.folders:
                self.folders.append(s["folder"])
            self.save()
        return s

    def update(self, s):
        with self._lock:
            for i, old in enumerate(self.sessions):
                if old.get("id") == s.get("id"):
                    self.sessions[i] = s
                    break
            else:
                self.sessions.append(s)
            if s.get("folder") and s["folder"] not in self.folders:
                self.folders.append(s["folder"])
            self.save()

    def remove(self, sid):
        with self._lock:
            self.sessions = [s for s in self.sessions if s.get("id") != sid]
            self.save()

    def duplicate(self, sid):
        s = self.get(sid)
        if not s:
            return None
        s = dict(s)
        s["id"] = uuid.uuid4().hex[:12]
        s["name"] = s.get("name", "") + " - 副本"
        s["created"] = int(time.time())
        self.add(s)
        return s

    def touch(self, sid):
        with self._lock:
            for s in self.sessions:
                if s.get("id") == sid:
                    s["last_used"] = int(time.time())
                    s["use_count"] = s.get("use_count", 0) + 1
                    break
            self.save()

    def folder_tree(self):
        tree = {}
        for f in self.folders:
            node = tree
            for part in [p for p in f.split("/") if p]:
                node = node.setdefault(part, {})
        return tree

    def add_folder(self, folder):
        if folder and folder not in self.folders:
            self.folders.append(folder)
            self.save()

    def rename_folder(self, old, new):
        with self._lock:
            self.folders = [new if f == old else f for f in self.folders]
            for s in self.sessions:
                f = s.get("folder", "")
                if f == old:
                    s["folder"] = new
                elif f.startswith(old + "/"):
                    s["folder"] = new + f[len(old):]
            self.save()

    def remove_folder(self, folder):
        base = (folder or "").strip("/")
        if not base:
            return
        with self._lock:
            self.folders = [f for f in self.folders
                            if f != base and not f.startswith(base + "/")]
            for s in self.sessions:
                f = (s.get("folder") or "").strip("/")
                # 必须按“完整路径段”比较：旧写法 startswith(base) 会把
                # “核心备份”里的会话一并清空归属（测试报告 BUG-03）
                if f == base or f.startswith(base + "/"):
                    s["folder"] = ""
            self.save()

    # ------------------------------------------------------------ 导入 / 导出
    def export_json(self, path, sessions=None):
        data = {"app": "TermX", "version": 1,
                "folders": self.folders, "sessions": sessions or self.sessions}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)

    def import_json(self, path):
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        added = 0
        for s in d.get("sessions", []):
            base = new_session(s.get("protocol", "SSH2"))
            base.update({k: v for k, v in s.items() if k in SESSION_DEFAULTS})
            base["id"] = uuid.uuid4().hex[:12]
            self.add(base)
            added += 1
        return added

    def import_crt_ini(self, path, folder="SecureCRT 导入"):
        """解析 SecureCRT 会话 .ini（S: 字符串 / D: DWORD / B: 布尔）。"""
        try:
            raw = open(path, "rb").read()
            for enc in ("utf-8-sig", "utf-16", "gbk", "latin-1"):
                try:
                    text = raw.decode(enc)
                    break
                except Exception:
                    continue
            else:
                return 0
        except Exception:
            return 0
        kv = {}
        for line in text.splitlines():
            m = re.match(r'^[SDB]:"?([^"=]+)"?=(.*)$', line.strip())
            if m:
                kv[m.group(1).strip()] = m.group(2).strip().strip('"')
        proto = kv.get("Protocol Name", "SSH2").upper()
        proto = "SSH2" if "SSH2" in proto else ("SSH1" if "SSH1" in proto else
                ("TELNET" if "TELNET" in proto else ("SERIAL" if "SERIAL" in proto else
                ("RLOGIN" if "RLOGIN" in proto else "SSH2"))))
        proto = {"TELNET": "Telnet", "SERIAL": "Serial", "RLOGIN": "Rlogin"}.get(proto, "SSH2")
        name = os.path.splitext(os.path.basename(path))[0]
        s = new_session(proto, name)
        s["folder"] = folder
        s["host"] = kv.get("Hostname", kv.get("Host", ""))
        s["username"] = kv.get("Username", "")
        # ---- 端口（BUG-01）：SecureCRT 把端口写在 "Port Number" 键，
        # DWORD 值形如 00000016（十六进制 8 位），而手写的老文件可能直接写
        # "22"/"2222"（十进制）。旧代码一律 int(value, 16)，于是 "22" 变成
        # 0x22=34、"2222" 变成 8738，导入后连不上。
        raw_port = kv.get("Port Number") or kv.get("Port") or ""
        port = _parse_crt_port(raw_port)
        s["port"] = port if port else PROTO_DEFAULT_PORT.get(proto, 22)
        if proto == "Telnet" and s["port"] == 22:
            s["port"] = 23
        emu = kv.get("Emulation", "VT100").upper()
        if "XTERM" in emu:
            s["term_type"] = "xterm"
        elif "VT220" in emu:
            s["term_type"] = "vt220"
        elif "LINUX" in emu:
            s["term_type"] = "linux"
        elif "ANSI" in emu:
            s["term_type"] = "ansi"
        else:
            s["term_type"] = "vt100"
        s["keyfile"] = kv.get("Identity Filename V2", kv.get("Identity Filename", ""))
        s["description"] = "从 SecureCRT 导入：" + kv.get("Description", "")
        s["login_script"] = kv.get("Login Script", "")
        s["script"] = kv.get("Script Filename", "")
        s["log_file"] = kv.get("Log Filename", "")
        s["log_enabled"] = bool(s["log_file"])
        s["shell_path"] = kv.get("Shell Command", "")
        self.add(s)
        return 1

    def import_crt_dir(self, folder, target="SecureCRT 导入"):
        n = 0
        for root, _dirs, files in os.walk(folder):
            for fn in files:
                if fn.lower().endswith(".ini"):
                    n += self.import_crt_ini(os.path.join(root, fn), target)
        return n

    def export_csv(self, path):
        cols = ["name", "folder", "protocol", "host", "port", "username", "description"]
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for s in self.sessions:
                w.writerow({k: s.get(k, "") for k in cols})


class AccountStore(object):
    """全局账号库：显示名 + 用户名 + 密码（DPAPI 加密），保存到 accounts.json。

    与会话库分开存放，新建会话 / 快速连接 / 登录时可统一选择账号调用。
    """
    def __init__(self):
        self.file = path_of("accounts.json")
        self.accounts = []
        self.load()

    def load(self):
        try:
            with open(self.file, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.accounts = [a for a in d.get("accounts", []) if isinstance(a, dict)]
        except Exception:
            self.accounts = []

    def save(self):
        """写盘并返回是否成功（账号密码丢失比会话丢失更危险，见 BUG-04）。"""
        global LAST_ACCOUNT_SAVE_ERROR
        try:
            folder = os.path.dirname(self.file)
            if folder and not os.path.isdir(folder):
                LAST_ACCOUNT_SAVE_ERROR = "账号库目录不存在：%s" % folder
                return False
            tmp = self.file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "accounts": self.accounts},
                          f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.file)
            LAST_ACCOUNT_SAVE_ERROR = ""
            return True
        except Exception as e:
            LAST_ACCOUNT_SAVE_ERROR = "%s: %s" % (type(e).__name__, e)
            try:
                if os.path.isfile(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            return False

    def last_error(self):
        return LAST_ACCOUNT_SAVE_ERROR

    def all(self):
        return [dict(a) for a in self.accounts]

    def get(self, aid):
        for a in self.accounts:
            if a.get("id") == aid:
                return dict(a)
        return None

    def add(self, account):
        a = dict(account)
        a["id"] = a.get("id") or uuid.uuid4().hex[:12]
        a.setdefault("name", "")
        a.setdefault("username", "")
        a.setdefault("password_enc", "")
        self.accounts.append(a)
        self.save()
        return dict(a)

    def update(self, account):
        a = dict(account)
        for i, old in enumerate(self.accounts):
            if old.get("id") == a.get("id"):
                self.accounts[i] = a
                break
        else:
            self.accounts.append(a)
        self.save()
        return dict(a)

    def remove(self, aid):
        self.accounts = [a for a in self.accounts if a.get("id") != aid]
        self.save()

    @staticmethod
    def label(account):
        """下拉显示文本：名称 · 用户名（名称为空时只显示用户名）。"""
        name = (account.get("name") or "").strip()
        user = (account.get("username") or "").strip()
        return ("%s · %s" % (name, user)) if (name and name != user) else (name or user)

    @staticmethod
    def password(account):
        return _dpapi(account.get("password_enc", "") or "", False)

    def by_label(self, label):
        for a in self.accounts:
            if self.label(a) == label:
                return dict(a)
        return None


def unbind_account(aid, store=None):
    """账号被删除时，解绑所有引用它的会话。

    绑定关系是"会话 → account_id"单向引用，所以删账号不会让会话失效，
    但必须清掉会话里的 account_id，否则会话会指向一个不存在的账号，
    密码也就取不到了（表现为连不上或用了旧密码）。

    返回被解绑的会话数量。
    """
    if not aid:
        return 0
    try:
        st = store or SessionStore()
    except Exception:
        return 0
    n = 0
    try:
        for s in st.all():
            if s.get("account_id") == aid:
                s["account_id"] = ""
                # 账号没了，旧的密码副本也不再可靠，清掉
                s["password"] = ""
                s["password_enc"] = ""
                try:
                    st.update(s)
                except Exception:
                    pass
                n += 1
    except Exception:
        return n
    return n


class VariableTable(object):
    """批量下发用的变量表：CSV 首行为列名，每行对应一台设备。"""
    def __init__(self):
        self.headers = []
        self.rows = []

    def load(self, path):
        """读变量表 CSV，兼容 Excel 编辑后的 GBK / UTF-16 / 分号分隔。"""
        with open(path, "rb") as f:
            raw = f.read()
        encodings = ("utf-16", "utf-8-sig", "gbk", "latin-1") \
            if raw.startswith((b"\xff\xfe", b"\xfe\xff")) \
            else ("utf-8-sig", "gbk", "utf-16", "latin-1")
        parsed = None
        for enc in encodings:
            try:
                text = raw.decode(enc)
            except Exception:
                continue
            delimiters = [","]
            try:
                d = csv.Sniffer().sniff(text[:4096],
                                        delimiters=",;\t|").delimiter
                if d not in delimiters:
                    delimiters.append(d)
            except Exception:
                pass
            for d in ",;\t|":
                if d not in delimiters:
                    delimiters.append(d)
            # 变量表列名不固定，用"拆出的列数"选最优分隔符：
            # 整行挤成一列（分隔符选错）时列数最少，会被自然淘汰。
            best_rows, best_cols = None, 0
            for sep in delimiters:
                try:
                    rows = list(csv.reader(text.splitlines(), delimiter=sep))
                except Exception:
                    continue
                if not rows:
                    continue
                cols = len([c for c in rows[0] if (c or "").strip()])
                if any(c.strip() for c in rows[0]) and cols > best_cols:
                    best_rows, best_cols = rows, cols
            parsed = best_rows or parsed
            if parsed:
                break
        r = parsed or []
        if not r:
            return 0
        self.headers = [h.strip() for h in r[0]]
        self.rows = [dict(zip(self.headers, row)) for row in r[1:]
                     if any(x.strip() for x in row)]
        return len(self.rows)

    def row_for(self, index, session):
        base = {
            "name": session.get("name", ""), "host": session.get("host", ""),
            "ip": session.get("host", ""), "user": session.get("username", ""),
            "port": str(session.get("port", "")), "index": str(index),
            "no": str(index + 1), "protocol": session.get("protocol", ""),
            "folder": session.get("folder", ""),
        }
        if index < len(self.rows):
            base.update({k: v for k, v in self.rows[index].items() if v != ""})
        return base


def expand_vars(text, mapping):
    """替换 {var} / ${var} / %var% 三种写法的变量。

    1.4.19 的正则用 ``\\w``，只认 ASCII 单词字符（对带空格、短横线、
    “机房A-设备”这类中文列头一律不匹配），而且 ``{...}`` 分支排在
    ``${...}`` 前面，导致 ``${name}`` 被替换成 ``$sw1``（测试报告
    BUG-14）。这里放宽键名字符集并修正分支顺序。
    """
    if not text:
        return ""
    mapping = mapping or {}

    def repl(m):
        key = m.group(1) or m.group(2) or m.group(3) or ""
        if key in mapping:
            return str(mapping[key])
        k2 = key.strip()
        if k2 in mapping:
            return str(mapping[k2])
        return m.group(0)

    return _VAR_RE.sub(repl, text)


# ${name} 必须排在 {name} 之前，否则 "$" 会被留在结果里
_VAR_RE = re.compile(r"\$\{([^{}]+)\}|\{([^{}]+)\}|%([^%]+)%")