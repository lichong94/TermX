# -*- coding: utf-8 -*-
"""脚本与批量下发。

1) 批量命令窗口：多会话并发/顺序下发、变量替换、等待提示符、超时、结果汇总导出
2) Python 脚本：内嵌 SecureCRT 风格 crt 对象（Screen / Session / Dialog / Tab ...）
3) VBScript / JScript 脚本：默认走内置转译引擎（零外部依赖），
   可选切换系统 WSH(cscript.exe) 以获得完整语法
4) 命令脚本(.txt)、SecureCRT expect/send 登录脚本、宏与录制
"""
import json
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
import traceback

from .config import path_of
from .connections import ANSI_RE
from .sessions import expand_vars

STRIP = re.compile(
    r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[PX^_][^\x1b]*(?:\x1b\\)?|\x1b[@-Z\\-_]|[\x00-\x08\x0b\x0c\x0e-\x1f]')


def clean(data, encoding="utf-8"):
    if isinstance(data, bytes):
        for enc in (encoding, "utf-8", "gbk", "latin-1"):
            try:
                data = data.decode(enc, "replace")
                break
            except Exception:
                continue
        else:
            data = ""
    return STRIP.sub("", data)


class Capture(object):
    """线程安全的输出捕获器：脚本等待提示符、批量结果收集都基于它。"""

    def __init__(self, encoding="utf-8", max_chars=2000000):
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._buf = ""
        self._cursor = 0
        self._closed = False
        self.encoding = encoding
        self.max_chars = max_chars

    def push(self, data):
        text = clean(data, self.encoding)
        if not text:
            return
        with self._cond:
            self._buf += text
            if len(self._buf) > self.max_chars:
                self._buf = self._buf[-self.max_chars:]
            self._cond.notify_all()

    def text(self):
        with self._lock:
            return self._buf

    def since(self, pos):
        with self._lock:
            return self._buf[pos:]

    def position(self):
        with self._lock:
            return len(self._buf)

    def wait_for(self, patterns, timeout=30, since=None, regex=False, stop=None):
        """返回 (匹配序号 0-based, 命中文本, 新游标)。

        since=None 时从上次读取游标继续（兼容 SecureCRT WaitForString 语义），
        避免刚发送命令后立刻等待时漏掉已经到达的回显。
        """
        if isinstance(patterns, str):
            patterns = [patterns]
        if not patterns:
            return -1, "", self._cursor
        start = self._cursor if since is None else since
        deadline = time.time() + max(0.1, timeout)
        with self._cond:
            while True:
                if stop is not None and stop.is_set():
                    return -1, self._buf[start:], len(self._buf)
                seg = self._buf[start:]
                for i, p in enumerate(patterns):
                    try:
                        hit = re.search(p, seg, re.I) if regex else \
                              (p.lower() in seg.lower())
                    except re.error:
                        hit = p in seg
                    if hit:
                        end = (re.search(p, seg, re.I).end() if regex
                               else seg.lower().find(p.lower()) + len(p))
                        self._cursor = start + end
                        return i, seg[:end], self._cursor
                left = deadline - time.time()
                if left <= 0:
                    self._cursor = len(self._buf)
                    return -1, seg, self._cursor
                self._cond.wait(min(0.08, left))

    def wait_quiet(self, seconds=0.4, since=0, stop=None):
        """等待输出静止一段时间（用于无提示符的设备）。"""
        deadline = time.time() + seconds
        with self._cond:
            last = len(self._buf)
            while time.time() < deadline:
                if stop is not None and stop.is_set():
                    break
                self._cond.wait(0.08)
                if len(self._buf) != last:
                    last = len(self._buf)
                    deadline = time.time() + seconds
        return self._buf[since:]

    def close(self):
        with self._cond:
            self._closed = True
            self._cond.notify_all()


# ------------------------------------------------------------------ crt 对象
class ScreenAPI(object):
    def __init__(self, engine, tab):
        self._engine = engine
        self._tab = tab
        self.MatchIndex = 0
        self.Synchronous = True
        self.IgnoreCase = True
        self.IgnoreEscape = True
        self.CurrentRow = 1
        self.CurrentColumn = 1

    @property
    def Rows(self):
        return self._tab.rows if self._tab else 24

    @property
    def Columns(self):
        return self._tab.cols if self._tab else 80

    def Send(self, text):
        self._engine.send(self._tab, str(text))
        return True

    def SendSpecial(self, name):
        special = {
            "MENU_ENTER": b"\r", "MENU_ESCAPE": b"\x1b", "MENU_TAB": b"\t",
            "MENU_CTRL_C": b"\x03", "MENU_CTRL_D": b"\x04", "MENU_CTRL_Z": b"\x1a",
            "MENU_BREAK": b"\x03", "MENU_UP": b"\x1b[A", "MENU_DOWN": b"\x1b[B",
            "MENU_F1": b"\x1bOP", "MENU_F2": b"\x1bOQ",
        }
        self._engine.send(self._tab, special.get(str(name).upper(), b""))
        return True

    def WaitForString(self, text, timeout=30):
        idx, _out, _pos = self._engine.wait_for(self._tab, [str(text)], timeout)
        self.MatchIndex = idx + 1
        return idx >= 0

    def WaitForStrings(self, strings, timeout=30):
        idx, _out, _pos = self._engine.wait_for(self._tab, list(strings), timeout)
        self.MatchIndex = idx + 1
        return self.MatchIndex

    def ReadString(self, chars, timeout=30):
        patterns = list(chars) if not isinstance(chars, str) else [chars]
        idx, out, _pos = self._engine.wait_for(self._tab, patterns, timeout)
        self.MatchIndex = idx + 1
        return out

    def WaitForCursor(self, timeout=30):
        return True

    def Get(self, row1, col1, row2, col2):
        return self._engine.ui_call(self._tab.screen_text, int(row1), int(col1),
                                    int(row2), int(col2))

    def Get2(self, row1, col1, row2, col2):
        return self.Get(row1, col1, row2, col2)

    def Clear(self):
        self._engine.ui_call(self._tab.clear_screen)

    def Print(self, text):
        self._engine.log(str(text))

    def SendKeys(self, text):
        return self.Send(text)


class SessionAPI(object):
    def __init__(self, engine, tab):
        self._engine = engine
        self._tab = tab

    @property
    def Connected(self):
        return bool(self._tab and self._tab.is_connected())

    @property
    def RemoteAddress(self):
        return self._tab.session.get("host", "") if self._tab else ""

    @property
    def RemotePort(self):
        return self._tab.session.get("port", 0) if self._tab else 0

    @property
    def LocalAddress(self):
        return "127.0.0.1"

    @property
    def Path(self):
        return self._tab.session.get("folder", "") if self._tab else ""

    @property
    def LogFileName(self):
        return self._tab.log_path if self._tab else ""

    @property
    def Logging(self):
        return bool(self._tab and self._tab.log_path)

    def Connect(self, arg=None, **kwargs):
        return self._engine.connect(self._tab, arg, kwargs)

    def ConnectInTab(self, arg=None, **kwargs):
        return self._engine.connect(self._tab, arg, kwargs, new_tab=True)

    def Disconnect(self):
        self._engine.disconnect(self._tab)

    def Log(self, enable=True, path=""):
        self._engine.log_session(self._tab, bool(enable), path)


class DialogAPI(object):
    def __init__(self, engine):
        self._engine = engine

    def MessageBox(self, message, title="TermX", buttons=0):
        return self._engine.ui_call(self._engine.message_box, str(message), str(title))

    def Prompt(self, message, title="TermX", default=""):
        return self._engine.ui_call(self._engine.prompt, str(message), str(title), str(default)) or ""

    def FileOpenDialog(self, title="打开文件", filter="*.*", default=""):
        return self._engine.ui_call(self._engine.file_dialog, str(title)) or ""


class ClipboardAPI(object):
    def __init__(self, engine):
        self._engine = engine

    @property
    def Text(self):
        return self._engine.ui_call(self._engine.clipboard_get) or ""

    @Text.setter
    def Text(self, value):
        self._engine.ui_call(self._engine.clipboard_set, str(value))


class TabAPI(object):
    def __init__(self, engine, tab):
        self._engine = engine
        self._tab = tab
        self.Screen = ScreenAPI(engine, tab)
        self.Session = SessionAPI(engine, tab)

    @property
    def Caption(self):
        return self._tab.name if self._tab else ""

    @Caption.setter
    def Caption(self, value):
        self._engine.ui_call(self._tab.set_caption, str(value))

    @property
    def Index(self):
        return self._engine.tab_index(self._tab)

    def Activate(self):
        self._engine.ui_call(self._engine.activate_tab, self._tab)

    def Close(self):
        self._engine.disconnect(self._tab)


class CrtAPI(object):
    """SecureCRT 兼容的顶层对象。"""
    Version = "TermX 1.0.0 (SecureCRT 兼容层)"

    def __init__(self, engine, tab):
        self._engine = engine
        self._tab = tab
        self.Screen = ScreenAPI(engine, tab)
        self.Session = SessionAPI(engine, tab)
        self.Dialog = DialogAPI(engine)
        self.Clipboard = ClipboardAPI(engine)
        self.Window = self
        self.Arguments = []
        self.MatchIndex = 0

    def Sleep(self, ms):
        time.sleep(max(0, float(ms)) / 1000.0)

    def Quit(self):
        raise SystemExit(0)

    def GetScriptTab(self):
        return TabAPI(self._engine, self._tab)

    def GetTabCount(self):
        return self._engine.tab_count()

    def GetTab(self, index):
        return TabAPI(self._engine, self._engine.tab_at(index))

    def GetActiveTab(self):
        return TabAPI(self._engine, self._engine.active_tab())

    def ActiveTab(self, index):
        self._engine.ui_call(self._engine.activate_tab, self._engine.tab_at(index))

    def GetSessionCount(self):
        return self._engine.tab_count()

    def AddSession(self, *args):
        return ""

    def Show(self, value=True):
        pass

    def Log(self, text):
        self._engine.log(str(text))

    def MessageBox(self, text, title="TermX"):
        return self.Dialog.MessageBox(text, title)


class Engine(object):
    """把脚本 API 绑定到主窗口的标签页集合。所有 UI 调用经 ui_call 回主线程。"""

    def __init__(self, controller):
        self.controller = controller

    # ------------------------------------------------------------ 基础桥接
    def send(self, tab, data):
        if tab is None:
            return
        controller = self.controller
        controller.send_to_tab(tab, data)

    def wait_for(self, tab, patterns, timeout):
        if tab is None:
            return -1, "", 0
        return self.controller.wait_for(tab, patterns, float(timeout or 30))

    def ui_call(self, func, *args):
        return self.controller.ui_call(func, *args)

    def log(self, text):
        self.controller.script_log(text)

    def tab_count(self):
        return self.controller.tab_count()

    def tab_at(self, index):
        return self.controller.tab_at(int(index) - 1 if int(index) > 0 else int(index))

    def tab_index(self, tab):
        tabs = self.controller.tabs()
        return tabs.index(tab) + 1 if tab in tabs else 0

    def active_tab(self):
        return self.controller.active_tab()

    def activate_tab(self, tab):
        self.controller.activate_tab(tab)

    def connect(self, tab, arg, kwargs, new_tab=False):
        return self.ui_call(self.controller.script_connect, tab, arg, kwargs, new_tab)

    def disconnect(self, tab):
        self.ui_call(self.controller.disconnect_tab, tab)

    def log_session(self, tab, enable, path):
        self.ui_call(self.controller.toggle_log, tab, enable, path)

    def message_box(self, msg, title):
        import tkinter.messagebox as mb
        mb.showinfo(title or "TermX", msg)
        return 1

    def prompt(self, msg, title, default):
        import tkinter.simpledialog as sd
        return sd.askstring(title or "TermX", msg, initialvalue=default)

    def file_dialog(self, title):
        import tkinter.filedialog as fd
        return fd.askopenfilename(title=title)

    def clipboard_get(self):
        return self.controller.root.clipboard_get()

    def clipboard_set(self, text):
        r = self.controller.root
        r.clipboard_clear()
        r.clipboard_append(text)

    # ------------------------------------------------------------ 执行入口
    def run(self, code, filename="", tab=None, language="python", on_done=None,
            on_log=None, stop_event=None, args=None):
        t = threading.Thread(target=self._run_thread,
                             args=(code, filename, tab, language, on_done, on_log,
                                   stop_event, args or []),
                             daemon=True, name="script-runner")
        t.start()
        return t

    def _run_thread(self, code, filename, tab, language, on_done, on_log, stop, args):
        ok, msg = True, ""
        try:
            if language == "python":
                self._run_python(code, filename, tab, on_log, args)
            elif language in ("vbscript", "jscript", "js", "vbs"):
                if self._prefer_wsh():
                    self._run_wsh(code, filename, tab, language, on_log, stop)
                else:
                    self._run_basic(code, filename, tab, language, on_log, stop)
            elif language == "commands":
                self._run_commands(code, tab, on_log, stop)
            else:
                raise RuntimeError("未知脚本类型: %s" % language)
        except SystemExit:
            pass
        except Exception as e:
            ok = False
            msg = "%s\n%s" % (e, traceback.format_exc(limit=6))
            if on_log:
                on_log("[错误] %s" % e)
        if on_done:
            try:
                on_done(ok, msg)
            except Exception:
                pass

    def _run_python(self, code, filename, tab, on_log, args):
        crt = CrtAPI(self, tab)
        crt.Arguments = args
        glb = {"crt": crt, "__name__": "__main__", "__builtins__": __builtins__,
               "Sleep": lambda ms: time.sleep(ms / 1000.0),
               "Log": lambda s: self.log(str(s))}
        exec(compile(code, filename or "<script>", "exec"), glb)

    def _run_commands(self, code, tab, on_log, stop):
        for raw in code.splitlines():
            if stop and stop.is_set():
                break
            line = raw.strip()
            # 注释行跳过；空行也跳过（脚本里的空行只是排版，不代表要发命令）
            if not line or line.startswith("#") or line.startswith("'"):
                continue
            upper = line.upper()
            m = re.match(r'^SLEEP\s+(\d+)$', upper)
            if m:
                time.sleep(int(m.group(1)) / 1000.0)
                continue
            m = re.match(r'^WAIT\s+"(.+?)"(?:\s+(\d+))?$', line, re.I)
            if m:
                self.wait_for(tab, [m.group(1)], int(m.group(2) or 30))
                continue
            m = re.match(r'^SEND\s+"(.*)"$', line, re.I)
            if m:
                # SEND "" 即"只发一个回车"——用于需要敲空行确认的场景
                text = m.group(1).replace("\\r", "\r").replace("\\n", "\r").replace("\\t", "\t")
            else:
                text = line + "\r"
            self.send(tab, text)
            if on_log:
                on_log("> " + line)
            time.sleep(0.05)

    # ------------------------------------------------------------ 内置脚本引擎
    def _prefer_wsh(self):
        cfg = getattr(self.controller, "cfg", None)
        if cfg is None or cfg.get("script_engine", "builtin") != "wsh":
            return False
        p = os.path.join(os.environ.get("WINDIR", r"C:\Windows"),
                         "System32", "cscript.exe")
        return os.path.isfile(p)

    def _run_basic(self, code, filename, tab, language, on_log, stop):
        """纯 Python 的 VBScript/JScript 兼容执行器，不调用任何外部进程。"""
        try:
            from . import vbscript
        except ImportError:
            self._run_wsh(code, filename, tab, language, on_log, stop)
            return

        def rpc(method, args):
            return self._rpc(method, list(args), tab)

        def printer(text):
            s = str(text)
            self.log(s)
            if on_log:
                on_log("[脚本] " + s)

        vbscript.run_basic(code, language, rpc, on_log=on_log, stop=stop,
                           on_print=printer)

    # ------------------------------------------------------------ WSH 桥（可选）
    def _run_wsh(self, code, filename, tab, language, on_log, stop):
        is_js = language in ("jscript", "js")
        prelude = JS_PRELUDE if is_js else VBS_PRELUDE
        ext = ".js" if is_js else ".vbs"
        full = prelude + "\r\n" + code.replace("\r\n", "\n").replace("\n", "\r\n")
        fd, path = tempfile.mkstemp(suffix=ext, prefix="termx_script_")
        os.close(fd)
        # WSH 支持带 BOM 的 UTF-16LE Unicode 脚本，避免中文乱码
        with open(path, "wb") as f:
            f.write(b"\xff\xfe" + full.encode("utf-16-le"))
        engine = "//E:jscript" if is_js else "//E:vbscript"
        try:
            cmd = ["cscript.exe", "//nologo", engine, path]
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                 text=True, encoding="utf-8", errors="replace", bufsize=1)
            while True:
                if stop and stop.is_set():
                    p.terminate()
                    break
                line = p.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line or "|" not in line:
                    if on_log and line and not line.startswith("Microsoft"):
                        on_log("[脚本] " + line)
                    continue
                method, rest = line.split("|", 1)
                params = [_hexdec(x) for x in rest.split("|")] if rest else []
                try:
                    value = self._rpc(method, params, tab)
                    resp = "OK|" + _hexenc("" if value is None else str(value))
                except SystemExit:
                    p.terminate()
                    break
                except Exception as e:
                    resp = "ERR|" + _hexenc(str(e))
                try:
                    p.stdin.write(resp + "\n")
                    p.stdin.flush()
                except Exception:
                    break
            try:
                p.wait(timeout=3)
            except Exception:
                p.terminate()
        finally:
            try:
                os.unlink(path)
            except Exception:
                pass

    def _rpc(self, method, a, tab):
        s = lambda i, d="": a[i] if i < len(a) else d
        if method == "Send":
            self.send(tab, s(0)); return ""
        if method == "SendSpecial":
            ScreenAPI(self, tab).SendSpecial(s(0)); return ""
        if method == "WaitForString":
            idx, _o, _p = self.wait_for(tab, [s(0)], float(s(1, 30) or 30))
            return "1" if idx >= 0 else "0"
        if method == "WaitForStrings":
            raw = s(0, "[]")
            try:
                patterns = json.loads(raw) if raw.lstrip().startswith("[") else raw.split("\x01")
            except Exception:
                patterns = raw.split("\x01")
            idx, _o, _p = self.wait_for(tab, patterns, float(s(1, 30) or 30))
            return str(idx + 1)
        if method == "ReadString":
            _i, out, _p = self.wait_for(tab, [s(0)], float(s(1, 30) or 30))
            return out
        if method == "Get":
            return self.ui_call(tab.screen_text, int(s(0, 1)), int(s(1, 1)),
                                int(s(2, 24)), int(s(3, 80)))
        if method == "Clear":
            self.ui_call(tab.clear_screen); return ""
        if method == "Sleep":
            time.sleep(float(s(0, 0) or 0) / 1000.0); return ""
        if method == "MessageBox":
            self.ui_call(self.message_box, s(0), s(1, "TermX")); return "1"
        if method == "Prompt":
            return self.ui_call(self.prompt, s(0), s(1, "TermX"), s(2)) or ""
        if method == "FileOpen":
            return self.ui_call(self.file_dialog, s(0)) or ""
        if method == "ClipboardGet":
            try:
                return self.ui_call(self.clipboard_get) or ""
            except Exception:
                return ""
        if method == "ClipboardSet":
            self.ui_call(self.clipboard_set, s(0)); return ""
        if method == "Connected":
            return "1" if (tab and tab.is_connected()) else "0"
        if method == "Disconnect":
            self.disconnect(tab); return ""
        if method == "Connect":
            self.connect(tab, s(0), {}, s(1, "0") == "1"); return ""
        if method == "GetTabCount":
            return str(self.tab_count())
        if method == "GetTabCaption":
            t = self.tab_at(int(s(0, 1)))
            return t.name if t else ""
        if method == "ActivateTab":
            self.ui_call(self.activate_tab, self.tab_at(int(s(0, 1)))); return ""
        if method == "SendToTab":
            t = self.tab_at(int(s(0, 1)))
            self.send(t, s(1)); return ""
        if method == "Log":
            self.log(s(0)); return ""
        if method == "Version":
            return CrtAPI.Version
        if method == "Quit":
            raise SystemExit(0)
        raise RuntimeError("脚本 API 未实现: %s" % method)


def _hexenc(s):
    # WSH 的 AscW/ChrW 按 UTF-16 码元（大端十六进制文本）交换
    return s.encode("utf-16-be", "replace").hex()


def _hexdec(h):
    try:
        return bytes.fromhex(h).decode("utf-16-be", "replace")
    except Exception:
        return ""


JS_PRELUDE = r'''
function __enc(s){var o="",i,c;s=String(s);for(i=0;i<s.length;i++){c=s.charCodeAt(i).toString(16);while(c.length<4)c="0"+c;o+=c;}return o;}
function __dec(h){var s="",i;for(i=0;i+3<h.length;i+=4)s+=String.fromCharCode(parseInt(h.substr(i,4),16));return s;}
function __rpc(m,a){var line=m,i,p,r;for(i=0;i<a.length;i++)line+="|"+__enc(a[i]);WScript.StdOut.WriteLine(line);r=WScript.StdIn.ReadLine();p=r.split("|");if(p[0]=="ERR")throw new Error(p.length>1?__dec(p[1]):"脚本错误");return p.length>1?__dec(p[1]):"";}
var crt={
 Version:"TermX 1.0.0 (SecureCRT 兼容层)", Arguments:[], MatchIndex:0,
 Sleep:function(ms){__rpc("Sleep",[ms]);},
 Quit:function(){__rpc("Quit",[]);},
 Log:function(s){__rpc("Log",[s]);},
 GetScriptTab:function(){return __tab(0);},
 GetTabCount:function(){return parseInt(__rpc("GetTabCount",[]));},
 GetTab:function(n){return __tab(n);},
 GetActiveTab:function(){return __tab(0);},
 ActiveTab:function(n){__rpc("ActivateTab",[n]);},
 Screen:{
  Send:function(s){__rpc("Send",[s]);},
  SendSpecial:function(s){__rpc("SendSpecial",[s]);},
  WaitForString:function(s,t){var v=__rpc("WaitForString",[s,t||30]);return v=="1";},
  WaitForStrings:function(a,t){var v=parseInt(__rpc("WaitForStrings",[a.join(String.fromCharCode(1)),t||30]));this.MatchIndex=v;return v;},
  ReadString:function(s,t){return __rpc("ReadString",[s,t||30]);},
  Get:function(r1,c1,r2,c2){return __rpc("Get",[r1,c1,r2,c2]);},
  Get2:function(r1,c1,r2,c2){return __rpc("Get",[r1,c1,r2,c2]);},
  Clear:function(){__rpc("Clear",[]);},
  CurrentRow:1, CurrentColumn:1, Rows:24, Columns:80, Synchronous:true
 },
 Session:{
  Connect:function(s){__rpc("Connect",[s||"","0"]);},
  ConnectInTab:function(s){__rpc("Connect",[s||"","1"]);},
  Disconnect:function(){__rpc("Disconnect",[]);},
  Connected:false, RemoteAddress:"", RemotePort:0, Path:"", Logging:false
 },
 Dialog:{
  MessageBox:function(s,t){return __rpc("MessageBox",[s,t||"TermX"]);},
  Prompt:function(s,t,d){return __rpc("Prompt",[s,t||"TermX",d||""]);},
  FileOpenDialog:function(t){return __rpc("FileOpen",[t||"打开文件"]);}
 },
 Clipboard:{
  Text:"",
  GetText:function(){return __rpc("ClipboardGet",[]);},
  SetText:function(v){__rpc("ClipboardSet",[v]);},
  get_Text:function(){return __rpc("ClipboardGet",[]);},
  set_Text:function(v){__rpc("ClipboardSet",[v]);}
 }
};
function __tab(n){var self={Index:n,Activate:function(){__rpc("ActivateTab",[n]);},
 Caption:"",Screen:crt.Screen,Session:crt.Session,
 Send:function(s){__rpc("SendToTab",[n,s]);}};return self;}
'''

VBS_PRELUDE = r'''
Function Enc(s)
 Dim i,c,o
 s=CStr(s):o=""
 For i=1 To Len(s)
  c=AscW(Mid(s,i,1))
  If c<0 Then c=c+65536
  c=Hex(c)
  If Len(c)<4 Then c=String(4-Len(c),"0") & c
  o=o & c
 Next
 Enc=o
End Function
Function Dec(h)
 Dim i,s
 s=""
 For i=1 To Len(h) Step 4
  s=s & ChrW(CLng("&H" & Mid(h,i,4)))
 Next
 Dec=s
End Function
Function RPC(m,a)
 Dim line,i,r,p
 line=m
 For i=0 To UBound(a)
  line=line & "|" & Enc(a(i))
 Next
 WScript.StdOut.WriteLine line
 r=WScript.StdIn.ReadLine
 p=Split(r,"|",2)
 If p(0)="ERR" Then Err.Raise vbObjectError+1,,Dec(p(1))
 If UBound(p)>0 Then RPC=Dec(p(1)) Else RPC=""
End Function
Class ScreenClass
 Public MatchIndex,CurrentRow,CurrentColumn,Rows,Columns,Synchronous
 Private Sub Class_Initialize
  MatchIndex=0:CurrentRow=1:CurrentColumn=1:Rows=24:Columns=80:Synchronous=True
 End Sub
 Public Sub Send(s): RPC "Send",Array(s): End Sub
 Public Sub SendSpecial(s): RPC "SendSpecial",Array(s): End Sub
 Public Function WaitForString(s,t)
  WaitForString=(RPC("WaitForString",Array(s,t))="1")
 End Function
 Public Function WaitForStrings(a,t)
  Dim v: v=CInt(RPC("WaitForStrings",Array(Join(a,Chr(1)),t)))
  MatchIndex=v: WaitForStrings=v
 End Function
 Public Function ReadString(s,t): ReadString=RPC("ReadString",Array(s,t)): End Function
 Public Function GetText(r1,c1,r2,c2): GetText=RPC("Get",Array(r1,c1,r2,c2)): End Function
 Public Function Get2(r1,c1,r2,c2): Get2=RPC("Get",Array(r1,c1,r2,c2)): End Function
 Public Sub Clear(): RPC "Clear",Array(): End Sub
End Class
Class SessionClass
 Public Sub Connect(s): RPC "Connect",Array(s,"0"): End Sub
 Public Sub ConnectInTab(s): RPC "Connect",Array(s,"1"): End Sub
 Public Sub Disconnect(): RPC "Disconnect",Array(): End Sub
 Public Property Get Connected(): Connected=(RPC("Connected",Array())="1"): End Property
End Class
Class DialogClass
 Public Function MessageBox(s,t): MessageBox=RPC("MessageBox",Array(s,t)): End Function
 Public Function Prompt(s,t,d): Prompt=RPC("Prompt",Array(s,t,d)): End Function
 Public Function FileOpenDialog(t): FileOpenDialog=RPC("FileOpen",Array(t)): End Function
End Class
Class CrtClass
 Public Version,Arguments,MatchIndex,Screen,Session,Dialog
 Private Sub Class_Initialize
  Version="TermX 1.0.0 (SecureCRT 兼容层)":Arguments=Array():MatchIndex=0
  Set Screen=New ScreenClass
  Set Session=New SessionClass
  Set Dialog=New DialogClass
 End Sub
 Public Sub Sleep(ms): RPC "Sleep",Array(ms): End Sub
 Public Sub Log(s): RPC "Log",Array(s): End Sub
 Public Sub Quit(): RPC "Quit",Array(): End Sub
 Public Function GetScriptTab(): Set GetScriptTab=Nothing: End Function
 Public Function GetTabCount(): GetTabCount=CInt(RPC("GetTabCount",Array())): End Function
 Public Function GetActiveTab(): Set GetActiveTab=Nothing: End Function
 Public Sub ActiveTab(n): RPC "ActivateTab",Array(n): End Sub
End Class
Dim crt
Set crt=New CrtClass
'''


# ------------------------------------------------------------------ 登录脚本
def parse_login_script(text):
    """解析 SecureCRT expect/send 登录脚本为 [(expect, send)]。"""
    rules = []
    cur = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r'(?i)^expect\s+"(.*)"\s*$', line)
        if m:
            cur = [m.group(1), ""]
            rules.append(cur)
            continue
        m = re.match(r'(?i)^send\s+"(.*)"\s*$', line)
        if m and cur is not None:
            cur[1] = m.group(1).replace("\\r", "\r").replace("\\n", "\r").replace("\\t", "\t")
    return rules


class LoginScriptRunner(object):
    def __init__(self, tab, text, log=None, rules=None, quiet=False):
        self.tab = tab
        self.rules = rules if rules is not None else parse_login_script(text)
        self.log = None if quiet else log
        self.quiet = quiet
        self._stop = threading.Event()

    def start(self):
        if self.rules:
            threading.Thread(target=self._run, daemon=True, name="login-script").start()

    def stop(self):
        self._stop.set()

    def _sub_vars(self, send):
        try:
            from .sessions import get_username
            user = get_username(self.tab.session)
        except Exception:
            user = self.tab.session.get("username", "")
        out = send.replace("{{USERNAME}}", user) \
                  .replace("{{HOST}}", self.tab.session.get("host", ""))
        if "{{PASSWORD}}" in out:
            try:
                from .sessions import get_password
                out = out.replace("{{PASSWORD}}", get_password(self.tab.session) or "")
            except Exception:
                pass
        return out

    def _run(self):
        for expect, send in self.rules:
            if self._stop.is_set():
                return
            idx, _out, _pos = self.tab.wait_for([expect], 25, stop=self._stop)
            if idx < 0:
                if self.log:
                    self.log("[登录脚本] 等待超时: %s" % expect)
                continue
            data = self._sub_vars(send)
            self.tab.send_text(data)
            if self.log:
                self.log("[登录脚本] 匹配 %s -> 发送应答" % expect[:20])


class AutoLoginRunner(object):
    """免重复登录：并发等待所有登录提示，命中即自动应答保存的凭据。

    与 LoginScriptRunner 的顺序 expect 不同，这里一次 wait_for 同时监听
    用户名/密码等全部提示，避免前一个提示超时阻塞后一个的应答。
    """

    def __init__(self, tab, rules, total_timeout=30, log=None):
        self.tab = tab
        self.rules = list(rules)          # [(prompt, send)]
        self.total_timeout = total_timeout
        self.log = log
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self.rules:
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="auto-login")
            self._thread.start()

    def busy(self):
        """自动登录是否仍在应答中（批量下发据此判断"登录是否已完成"）。"""
        t = self._thread
        return bool(t and t.is_alive())

    def stop(self):
        self._stop.set()

    def _run(self):
        prompts = [p for p, _ in self.rules]
        mapping = dict(self.rules)
        deadline = time.time() + self.total_timeout
        answered = 0
        while not self._stop.is_set():
            left = deadline - time.time()
            if left <= 0:
                break
            try:
                idx, _out, _pos = self.tab.wait_for(prompts, min(left, 2.0),
                                                    stop=self._stop)
            except Exception:
                break
            if idx < 0:
                if not self.tab.connected:
                    break
                continue
            try:
                self.tab.send_text(mapping[prompts[idx]])
                answered += 1
                # 首次应答后再监听一段时间，覆盖 sudo/su 的二次密码提示
                if answered == 1:
                    deadline = max(deadline, time.time() + 8)
            except Exception:
                break


# ------------------------------------------------------------------ 批量下发
class BatchResult(object):
    def __init__(self, target, command, output, ok, elapsed, error="", var_row=0):
        self.target = target
        self.command = command
        self.output = output
        self.ok = ok
        self.elapsed = elapsed
        self.error = error
        # 变量表行号（>0 表示该结果来自变量展开的第 N 行）
        self.var_row = var_row


class BatchDispatcher(object):
    """批量下发核心：多会话、变量替换、等待提示符、超时、并发、结果汇总。"""

    def __init__(self, controller, targets, lines, variables=None, options=None,
                 on_progress=None, on_result=None, on_done=None, on_log=None):
        self.controller = controller
        self.targets = list(targets)
        # 命令列表：保留空行（= 只发一个回车，用户要求支持空行命令），
        # 只过滤注释行。
        self.lines = [l for l in lines if not l.strip().startswith("#")]
        self.variables = variables
        self.opt = {
            "parallel": False, "delay": 0.0, "timeout": 30.0,
            "wait_prompt": True, "prompt": r"[$#>]\s*$", "stop_on_error": False,
            "capture": True, "send_newline": True, "strip_echo": False,
            # var_expand:
            #   "all"  = 每台设备执行变量表的全部行（默认，1.4.17 起）
            #            —— 500 行变量 × N 台设备 = 每台设备 500 条命令
            #   "index"= 旧行为：第 i 台设备只用第 i 行变量
            "var_expand": "all",
            # 关闭分页：连接并确认登录后、执行用户命令前，先发这一条命令
            # （每条目标设备只发一次，不随变量行重复）
            "paging_cmd": "",       # 空=不执行
            "paging_name": "",      # 厂商名，仅用于结果列表与日志展示
        }
        self.opt.update(options or {})
        self.on_progress = on_progress
        self.on_result = on_result
        self.on_done = on_done
        self.on_log = on_log
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._results = []
        self._lock = threading.Lock()
        # 变量表行数：all 模式下每台设备都要跑这么多遍
        self.var_rows = len(getattr(self.variables, "rows", []) or []) \
            if self.variables else 0
        repeats = max(1, self.var_rows) \
            if self.opt.get("var_expand") == "all" else 1
        self.total = len(self.targets) * repeats * max(1, len(self.lines))
        if (self.opt.get("paging_cmd") or "").strip():
            # 关闭分页每台设备各计一条
            self.total += len(self.targets)
        self.done = 0

    def stop(self):
        self._stop.set()

    def pause(self, value=True):
        if value:
            self._pause.set()
        else:
            self._pause.clear()

    def _log(self, text):
        if self.on_log:
            try:
                self.on_log(text)
            except Exception:
                pass

    def _bump(self, target, cmd, status):
        with self._lock:
            self.done += 1
            n = self.done
        if self.on_progress:
            try:
                self.on_progress(n, self.total, target, cmd, status)
            except Exception:
                pass

    def run(self):
        t = threading.Thread(target=self._run, daemon=True, name="batch-dispatch")
        t.start()
        return t

    def _run(self):
        results = []
        try:
            # 顺序执行：一次只处理一个目标，跑完（并关掉临时标签）再下一个。
            # 目标多时并行会同时创建几十个标签/连接线程把界面卡死，
            # 因此这里统一走串行路径，"多会话并发"选项不再生效。
            for i, tgt in enumerate(self.targets):
                if self._stop.is_set():
                    break
                self._run_target(i, tgt, results)
        except Exception as e:
            self._log("[批量] 异常: %s" % e)
        finally:
            if self.on_done:
                try:
                    self.on_done(results)
                except Exception:
                    pass

    def _ensure_tab(self, target):
        """把目标变成可用的标签。

        目标可能是已打开的标签对象，也可能是**会话描述**（未打开的会话）。
        后者在执行时才临时开一个标签——避免一次性创建几十个窗口卡死界面。
        开/关标签属于 UI 操作，必须回到主线程执行。
        返回 (标签, 是否是本次临时开的)。
        """
        if not isinstance(target, dict):
            return target, False
        try:
            controller = self.controller
            # 已有同会话的标签直接复用
            for t in controller.tabs():
                try:
                    from .dialogs import BatchWindow
                    if BatchWindow._sess_key(t.session) == BatchWindow._sess_key(target):
                        return t, False
                except Exception:
                    pass
            # 开标签要走 UI 线程（tkinter 不能跨线程操作）
            opener = getattr(controller, "ui_call", None)
            if opener:
                tab = opener(controller.open_session, dict(target), False)
            else:
                tab = controller.open_session(dict(target), activate=False)
            # 临时开的标签是异步连接的，必须等它连上再执行，
            # 否则会因"未连接"被直接跳过
            if tab is not None:
                self._wait_connected(tab)
            return tab, True
        except Exception as e:
            self._log("[批量] 打开会话失败: %s" % e)
            return None, False

    def _wait_connected(self, tab, timeout=None):
        """等待标签连接建立（后台线程内轮询，不阻塞 UI）。"""
        if timeout is None:
            try:
                timeout = float(self.opt.get("connect_timeout", 20))
            except Exception:
                timeout = 20.0
        deadline = time.time() + timeout
        while time.time() < deadline and not self._stop.is_set():
            try:
                if tab.is_connected():
                    return True
            except Exception:
                pass
            time.sleep(0.2)
        try:
            return bool(tab.is_connected())
        except Exception:
            return False

    def _close_tab(self, tab, opened):
        """关闭本次临时打开的标签；用户自己打开的不动。"""
        if not opened or tab is None:
            return
        try:
            controller = self.controller
            closer = getattr(controller, "ui_call", None)
            if closer:
                closer(controller.close_tab, tab)
            else:
                controller.close_tab(tab)
        except Exception:
            pass

    def _mapping_list(self, index, session, name):
        """返回本设备要执行的 (映射, 变量行号) 列表。

        - var_expand="all"（默认）：变量表每一行都生成一套映射，
          于是每台设备都会把命令对每一行变量各执行一次；
        - var_expand="index"（旧行为）：第 i 台设备只用第 i 行变量。
        没有变量表时两种模式都只返回一套（仅内置变量）。
        """
        base = {
            "name": name, "host": session.get("host", ""),
            "ip": session.get("host", ""), "user": session.get("username", ""),
            "port": str(session.get("port", "")), "index": str(index),
            "no": str(index + 1), "protocol": session.get("protocol", ""),
            "folder": session.get("folder", ""),
        }
        rows = getattr(self.variables, "rows", []) or [] if self.variables else []
        headers = getattr(self.variables, "headers", []) or [] if self.variables else []
        if not rows:
            return [(base, 0)]
        if self.opt.get("var_expand") == "all":
            out = []
            for ri, row in enumerate(rows):
                m = dict(base)
                m.update({k: v for k, v in row.items() if v != ""})
                # 变量行号，便于在 500 行里定位
                m["var_row"] = str(ri + 1)
                m["row"] = str(ri + 1)
                out.append((m, ri + 1))
            return out
        # 旧行为：按设备序号取对应行
        m = dict(base)
        if 0 <= index < len(rows):
            m.update({k: v for k, v in rows[index].items() if v != ""})
        return [(m, index + 1)]

    def _run_target(self, index, target, results):
        tab, opened = self._ensure_tab(target)
        if tab is None:
            name = target.get("name", "会话%d" % (index + 1)) if isinstance(target, dict) \
                else getattr(target, "name", "会话%d" % (index + 1))
            maps = self._mapping_list(
                index, target if isinstance(target, dict) else {}, name)
            for mapping, _ri in maps:
                for cmd in self.lines:
                    r = BatchResult(name, cmd, "", False, 0, "打开失败")
                    results.append(r)
                    self._bump(name, cmd, "打开失败")
                    if self.on_result:
                        self.on_result(r)
            return
        try:
            self._run_on_tab(index, tab, results)
        finally:
            self._close_tab(tab, opened)

    def _run_on_tab(self, index, tab, results):
        name = getattr(tab, "name", "会话%d" % (index + 1))
        if not tab.is_connected():
            # 目标可能是刚打开的会话：等待其连接建立（自动登录并行进行）
            deadline = time.time() + float(self.opt.get("connect_timeout", 20))
            while not tab.is_connected() and time.time() < deadline \
                    and not self._stop.is_set():
                time.sleep(0.2)
        if not tab.is_connected():
            self._log("[%s] 未连接，跳过" % name)
            maps = self._mapping_list(index, getattr(tab, "session", {}) or {}, name)
            for _mapping, _ri in maps:
                for cmd in self.lines:
                    r = BatchResult(name, cmd, "", False, 0, "未连接")
                    results.append(r)
                    self._bump(name, cmd, "未连接")
                    if self.on_result:
                        self.on_result(r)
            return

        maps = self._mapping_list(index, getattr(tab, "session", {}) or {}, name)
        cap = tab.start_capture() if self.opt["capture"] else None
        try:
            # 连接并确认登录完成后，先下发"关闭分页"（每台设备一次）
            self._apply_paging(tab, name, results, cap)
            for mapping, var_row in maps:
                if self._stop.is_set():
                    break
                if var_row:
                    self._log("[%s] === 变量行 %d/%d ===" %
                              (name, var_row, max(1, self.var_rows)))
                self._run_line_set(index, tab, name, mapping, var_row,
                                   results, cap)
        finally:
            if cap:
                try:
                    tab.stop_capture(cap)
                except Exception:
                    pass

    def _wait_login_done(self, tab, timeout=None):
        """等待自动登录应答线程结束，避免把命令发到 Password:/login: 提示上。

        没有自动登录（未保存凭据或用户手工登录）时立即返回，不做猜测。
        """
        al = getattr(tab, "_auto_login", None)
        busy = getattr(al, "busy", None) if al is not None else None
        if not callable(busy):
            return
        try:
            limit = float(timeout if timeout is not None
                          else self.opt.get("login_timeout", 25) or 25)
        except Exception:
            limit = 25.0
        deadline = time.time() + limit
        while time.time() < deadline and not self._stop.is_set():
            try:
                if not busy():
                    return
            except Exception:
                return
            time.sleep(0.2)

    def _apply_paging(self, tab, name, results, cap):
        """连接并确认登录后，先下发"关闭分页"命令（每台设备只一次）。

        登录确认：先等自动登录应答结束，再等命令行提示符出现；
        已登录会话的本次捕获里没有历史提示符，此时补发一个回车唤起提示符
        再等待（登录流程已结束，回车是安全的）。
        关闭分页失败只记结果与日志，不阻断后续命令。
        """
        cmd = (self.opt.get("paging_cmd") or "").strip()
        if not cmd or self._stop.is_set():
            return
        vendor = (self.opt.get("paging_name") or "").strip()
        label = ("[关闭分页·%s] %s" % (vendor, cmd)) if vendor \
            else ("[关闭分页] " + cmd)
        prompt = self.opt.get("prompt") or r"[$#>]\s*$"
        try:
            timeout = float(self.opt.get("timeout", 30))
        except Exception:
            timeout = 30.0
        t0 = time.time()
        ok, err, output = False, "", ""
        try:
            self._wait_login_done(tab)
            idx, out, _pos = tab.wait_for([prompt], min(0.8, timeout),
                                          stop=self._stop, regex=True)
            if idx < 0:                   # 已登录会话：补一个回车唤起提示符
                start = cap.position() if cap else 0
                tab.send_text("\r")
                idx, out, _pos = tab.wait_for([prompt], min(6.0, timeout),
                                              stop=self._stop, regex=True,
                                              since=start)
            # 下发关闭分页命令并等回显提示符
            start2 = cap.position() if cap else 0
            tab.send_text(cmd + "\r")
            idx2, out2, _pos = tab.wait_for([prompt], timeout, stop=self._stop,
                                            regex=True, since=start2)
            ok = idx2 >= 0
            err = "" if ok else "等待提示符超时"
            output = cap.since(start2) if cap else out2
        except Exception as e:
            ok, err = False, str(e)
        r = BatchResult(name, label, output, ok, time.time() - t0, err)
        results.append(r)
        self._bump(name, label, "完成" if ok else "失败")
        if self.on_result:
            try:
                self.on_result(r)
            except Exception:
                pass
        self._log("[%s] %s -> %s" % (name, label,
                                     "完成" if ok else ("失败: " + err)))

    def _run_line_set(self, index, tab, name, mapping, var_row, results, cap):
        """对一套变量映射执行全部命令行（空行/SLEEP/WAIT/普通命令）。"""
        for raw in self.lines:
                while self._pause.is_set() and not self._stop.is_set():
                    time.sleep(0.2)
                if self._stop.is_set():
                    break
                line = raw.strip()
                low = line.upper()
                if not line:
                    # 空行 = 只发一个回车（用户要求支持空行命令/会回车）
                    cmd = ""
                    start = cap.position() if cap else 0
                    t0 = time.time()
                    try:
                        tab.send_text("\r")
                        if self.opt["wait_prompt"]:
                            idx, out, _p = tab.wait_for([self.opt["prompt"]],
                                                        float(self.opt["timeout"]),
                                                        stop=self._stop, regex=True,
                                                        since=start)
                            output = cap.since(start) if cap else out
                            ok = idx >= 0
                            err = "" if ok else "等待提示符超时"
                        else:
                            time.sleep(max(0.0, float(self.opt["delay"])))
                            output = cap.since(start) if cap else ""
                            ok, err = True, ""
                    except Exception as e:
                        output, ok, err = "", False, str(e)
                    r = BatchResult(name, "(回车)", output, ok, time.time() - t0, err,
                                          var_row=var_row)
                    results.append(r)
                    self._bump(name, "(回车)", "完成" if ok else "失败")
                    if self.on_result:
                        self.on_result(r)
                    self._log("[%s] (回车) -> %s" % (name, "完成" if ok else "失败"))
                    if float(self.opt["delay"]) > 0:
                        time.sleep(float(self.opt["delay"]))
                    continue
                if low.startswith("SLEEP ") and low[6:].strip().isdigit():
                    time.sleep(int(low[6:].strip()) / 1000.0)
                    self._bump(name, line, "等待")
                    continue
                m = re.match(r'(?i)^WAIT\s+"(.+?)"(?:\s+(\d+))?$', line)
                if m:
                    since = cap.position() if cap else 0
                    idx, out, _p = tab.wait_for([m.group(1)],
                                                float(m.group(2) or self.opt["timeout"]),
                                                stop=self._stop, since=since)
                    r = BatchResult(name, line, out, idx >= 0, 0,
                                    "" if idx >= 0 else "等待超时",
                                    var_row=var_row)
                    results.append(r)
                    self._bump(name, line, "完成" if idx >= 0 else "超时")
                    if self.on_result:
                        self.on_result(r)
                    continue
                cmd = expand_vars(line, mapping)
                start = cap.position() if cap else 0
                t0 = time.time()
                payload = cmd + ("\r" if self.opt["send_newline"] else "")
                try:
                    tab.send_text(payload)
                    if self.opt["wait_prompt"]:
                        idx, out, _p = tab.wait_for([self.opt["prompt"]],
                                                    float(self.opt["timeout"]),
                                                    stop=self._stop, regex=True,
                                                    since=start)
                        output = cap.since(start) if cap else out
                        ok = idx >= 0
                        err = "" if ok else "等待提示符超时"
                    else:
                        time.sleep(max(0.0, float(self.opt["delay"])))
                        if cap:
                            cap.wait_quiet(float(self.opt["delay"]) or 0.2)
                        output = cap.since(start) if cap else ""
                        ok, err = True, ""
                    if self.opt["strip_echo"] and output.startswith(cmd):
                        output = output[len(cmd):]
                except Exception as e:
                    output, ok, err = "", False, str(e)
                r = BatchResult(name, cmd, output, ok, time.time() - t0, err,
                                var_row=var_row)
                results.append(r)
                self._bump(name, cmd, "完成" if ok else "失败")
                if self.on_result:
                    self.on_result(r)
                self._log("[%s] %s -> %s (%.2fs)" % (name, cmd[:60], "完成" if ok else "失败",
                                                     r.elapsed))
                if not ok and self.opt["stop_on_error"]:
                    self._stop.set()
                    break
                if float(self.opt["delay"]) > 0:
                    time.sleep(float(self.opt["delay"]))

    @staticmethod
    def export(results, path, fmt="csv"):
        import csv
        if fmt == "csv":
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(["会话", "变量行", "命令", "状态", "耗时(秒)", "错误", "输出"])
                for r in results:
                    w.writerow([r.target, getattr(r, "var_row", 0) or "", r.command,
                                "成功" if r.ok else "失败",
                                "%.2f" % r.elapsed, r.error, r.output[:32000]])
        else:
            with open(path, "w", encoding="utf-8") as f:
                for r in results:
                    vr = getattr(r, "var_row", 0) or 0
                    mark = " [变量行%d]" % vr if vr else ""
                    f.write("=" * 70 + "\n[%s%s] %s  (%s, %.2fs)\n" %
                            (r.target, mark, r.command,
                             "成功" if r.ok else "失败", r.elapsed))
                    if r.error:
                        f.write("错误: %s\n" % r.error)
                    f.write(r.output + "\n")
        return path
