# -*- coding: utf-8 -*-
"""连接层：SSH1/2、Telnet、Rlogin、Raw TCP、串口、本地 Shell、SFTP。

统一 Connection 接口，后台线程 IO，绝不直接触碰 UI 对象。
代理（SOCKS5 / HTTP CONNECT）由内置隧道实现，不需要外部工具。
"""
import base64
import ctypes
import os
import queue
import re
import socket
import struct
import threading
import time
from ctypes import wintypes

from . import bundle
from .config import path_of
from .conpty import PtyProcess, quote_cmdline, conpty_available
from .sessions import get_password, get_username

ANSI_RE = re.compile(
    rb'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[PX^_][^\x1b]*(?:\x1b\\)?|\x1b[@-Z\\-_]')

IAC = 0xff
DONT, DO, WONT, WILL, SB, GA, EL, EC = 0xfe, 0xfd, 0xfc, 0xfb, 0xfa, 0xf9, 0xf8, 0xf7
SE, NOP, DM, BRK, IP, AO, AYT, EOR = 0xf0, 0xf1, 0xf2, 0xf3, 0xf4, 0xf5, 0xf6, 0xef
OPT_ECHO, OPT_SGA, OPT_STATUS, OPT_TTYPE, OPT_NAWS, OPT_LINEMODE, OPT_NEWENV = 1, 3, 5, 24, 31, 34, 39


class Connection(object):
    """所有连接的基类。回调运行在 IO 线程。"""

    def __init__(self, session, cfg, callbacks):
        self.session = session
        self.cfg = cfg
        self.cb = callbacks or {}
        self.alive = False
        self._closed = False
        self._q = queue.Queue()
        self._log_lock = threading.Lock()
        self._log_file = None
        self.log_path = ""
        self.encoding = session.get("encoding") or cfg.get("encoding") or "UTF-8"
        self.title = session.get("name", "")
        self.description = ""
        self.auto_command = session.get("auto_command", "")
        self.anti_idle = bool(session.get("anti_idle", cfg.get("anti_idle")))
        self.anti_idle_interval = int(session.get("anti_idle_interval") or
                                      cfg.get("anti_idle_interval") or 60)
        self.anti_idle_string = session.get("anti_idle_string") or cfg.get("anti_idle_string") or ""
        self._prepare_log()

    # -------------------------------------------------------------- 日志
    def _prepare_log(self):
        if not (self.session.get("log_enabled") or self.cfg.get("auto_log")):
            return
        path = self.session.get("log_file") or ""
        if not path:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            safe = re.sub(r'[\\/:*?"<>|]', "_", self.title or "session")
            path = os.path.join(path_of("logs"), "%s_%s.log" % (safe, stamp))
        if not os.path.isabs(path):
            path = os.path.join(path_of(""), path)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self._log_file = open(path, "ab" if self.session.get("log_append", True) else "wb")
            self.log_path = path
        except Exception:
            self._log_file = None
            self.log_path = ""
        self._log_ts = bool(self.session.get("log_timestamp", self.cfg.get("log_timestamp")))
        self._log_strip = bool(self.session.get("log_strip_ansi", self.cfg.get("log_strip_ansi")))

    def _emit(self, data, log=True):
        if not data:
            return
        if log and self._log_file:
            try:
                out = ANSI_RE.sub(b"", data) if self._log_strip else data
                if self._log_ts:
                    prefix = ("\r\n[%s] " % time.strftime("%Y-%m-%d %H:%M:%S")).encode("ascii")
                    out = prefix + out
                with self._log_lock:
                    self._log_file.write(out)
                    self._log_file.flush()
            except Exception:
                pass
        cb = self.cb.get("on_data")
        if cb:
            try:
                cb(data)
            except Exception:
                pass

    def _state(self, text):
        cb = self.cb.get("on_state")
        if cb:
            try:
                cb(text)
            except Exception:
                pass

    def _error(self, text):
        cb = self.cb.get("on_error")
        if cb:
            try:
                cb(text)
            except Exception:
                pass

    def _fire_close(self):
        if self._closed:
            return
        self._closed = True
        self.alive = False
        try:
            self._q.put(None)
        except Exception:
            pass
        if self._log_file:
            try:
                with self._log_lock:
                    self._log_file.close()
            except Exception:
                pass
            self._log_file = None
        cb = self.cb.get("on_close")
        if cb:
            try:
                cb()
            except Exception:
                pass

    def write(self, data):
        if isinstance(data, str):
            try:
                data = data.encode(self.encoding, "replace")
            except Exception:
                data = data.encode("utf-8", "replace")
        if self.alive and data:
            self._q.put(data)

    def write_str(self, text):
        self.write(text)

    def send_anti_idle(self):
        if not self.alive:
            return
        s = self.anti_idle_string
        if s == "":
            self.write(b"\x00")          # SSH 协议层忽略 NUL，最安全
        else:
            # 这里必须传字符串：write() 会按会话 encoding 编码。若先按 UTF-8
            # 编成 bytes，write() 见 bytes 就不再转码，GBK 会话上的中文
            # 防挂起字符串会变成乱码（测试报告 BUG-12）。
            self.write(s.replace("\\n", "\n").replace("\\r", "\r"))

    def resize(self, cols, rows):
        pass

    def start(self):
        raise NotImplementedError

    def stop(self):
        self._fire_close()

    def startup_info(self):
        return ""


# ------------------------------------------------------------------ 代理隧道
def _recv_exact(sock, n):
    """从套接字精确读满 n 字节。

    TCP 上 ``recv(n)`` 只保证“至多 n 字节”，可能短读甚至返回 0（对端关闭）。
    旧代码直接 ``recv(2)``/``recv(4)`` 当完整报文用，还会对空返回取下标
    ``recv(1)[0]``，于是 SOCKS5 握手在慢网络/分片下随机失败或抛
    IndexError（测试报告 BUG-13）。
    """
    buf = b""
    while len(buf) < n:
        d = sock.recv(n - len(buf))
        if not d:
            raise RuntimeError("代理连接被对端提前关闭")
        buf += d
    return buf


def _socks5_connect(sock, host, port, user, passwd):
    methods = [0x00] if not user else [0x00, 0x02]
    sock.sendall(bytes([0x05, len(methods)]) + bytes(methods))
    r = _recv_exact(sock, 2)
    if r[0] != 5:
        raise RuntimeError("SOCKS5 握手失败")
    if r[1] == 0x02:
        u = user.encode(); p = (passwd or "").encode()
        sock.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
        if _recv_exact(sock, 2)[1] != 0:
            raise RuntimeError("SOCKS5 认证失败")
    elif r[1] != 0:
        raise RuntimeError("SOCKS5 不支持的认证方式")
    h = host.encode("idna", "replace")
    req = b"\x05\x01\x00\x03" + bytes([min(255, len(h))]) + h + struct.pack(">H", port)
    sock.sendall(req)
    head = _recv_exact(sock, 4)
    if head[1] != 0:
        raise RuntimeError("SOCKS5 连接目标失败")
    atyp = head[3]
    if atyp == 1:
        _recv_exact(sock, 6)                       # IPv4 + 端口
    elif atyp == 3:
        n = _recv_exact(sock, 1)[0]
        _recv_exact(sock, n + 2)
    elif atyp == 4:
        _recv_exact(sock, 18)                      # IPv6 + 端口
    else:
        raise RuntimeError("SOCKS5 返回了未知地址类型")


def _http_connect(sock, host, port, user, passwd):
    auth = ""
    if user:
        token = base64.b64encode(("%s:%s" % (user, passwd or "")).encode()).decode()
        auth = "Proxy-Authorization: Basic %s\r\n" % token
    req = ("CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n%s\r\n" %
           (host, port, host, port, auth)).encode()
    sock.sendall(req)
    buf = b""
    while b"\r\n\r\n" not in buf and len(buf) < 8192:
        part = sock.recv(1024)
        if not part:
            break
        buf += part
    if b" 200 " not in buf.split(b"\r\n", 1)[0]:
        raise RuntimeError("HTTP 代理 CONNECT 失败: %s" % buf[:120].decode("latin-1", "replace"))


def open_proxied_socket(target, port, session, cfg, timeout=12):
    """按会话/全局代理设置建立到目标的 socket。"""
    mode = session.get("proxy", "inherit")
    if mode == "none":
        ptype, phost, pport = "none", "", 0
        puser = ppass = ""
    elif mode == "custom":
        ptype = session.get("proxy_type", "none")
        phost, pport = session.get("proxy_host", ""), int(session.get("proxy_port") or 0)
        puser, ppass = session.get("proxy_user", ""), session.get("proxy_pass", "")
    else:
        ptype = cfg.get("proxy_type", "none")
        phost, pport = cfg.get("proxy_host", ""), int(cfg.get("proxy_port") or 0)
        puser, ppass = cfg.get("proxy_user", ""), cfg.get("proxy_pass", "")
    if ptype in ("none", "", None) or not phost:
        return socket.create_connection((target, port), timeout)
    s = socket.create_connection((phost, pport), timeout)
    s.settimeout(timeout)
    try:
        if ptype.startswith("socks"):
            _socks5_connect(s, target, port, puser, ppass)
        elif ptype.startswith("http"):
            _http_connect(s, target, port, puser, ppass)
        else:
            raise RuntimeError("未知代理类型: %s" % ptype)
    except Exception:
        s.close()
        raise
    s.settimeout(None)
    return s


class ProxyTunnel(object):
    """把代理包装成本地端口，供 ssh.exe / sftp.exe 等外部程序使用。"""

    def __init__(self, target, port, session, cfg, on_error=None):
        self.target, self.port = target, port
        self.session, self.cfg = session, cfg
        self.server = None
        self.port_local = 0
        self._stop = threading.Event()
        self._workers = []
        self.on_error = on_error          # 运行期错误回调（BUG-08）
        self._err_reported = False

    def start(self):
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(8)
        self.server.settimeout(0.5)
        self.port_local = self.server.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True, name="proxy-tunnel").start()
        return self.port_local

    def active(self):
        return self.port_local > 0

    def _accept(self):
        while not self._stop.is_set():
            try:
                c, _ = self.server.accept()
            except socket.timeout:
                continue
            except Exception:
                break
            t = threading.Thread(target=self._serve, args=(c,), daemon=True)
            t.start()
            self._workers.append(t)

    def _serve(self, client):
        try:
            upstream = open_proxied_socket(self.target, self.port, self.session, self.cfg)
        except Exception as e:
            self._error(str(e))
            client.close()
            return
        self._pump(client, upstream)

    def _error(self, msg):
        """隧道转发失败上报界面。

        旧实现是 ``pass``，连接建立之后的代理故障（账号错、目标拒绝、
        代理掉线）完全不可见，用户只看到终端一片空白（测试报告 BUG-08）。
        同一隧道只报第一次，避免每条转发连接都弹一次窗。
        """
        if self._err_reported:
            return
        self._err_reported = True
        cb = self.on_error
        if not cb:
            return
        try:
            cb("代理隧道失败（%s:%s）：%s" % (self.target, self.port, msg))
        except Exception:
            pass

    @staticmethod
    def _pump(a, b):
        def copy(src, dst):
            try:
                while True:
                    d = src.recv(8192)
                    if not d:
                        break
                    dst.sendall(d)
            except Exception:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except Exception:
                    pass
        t1 = threading.Thread(target=copy, args=(a, b), daemon=True)
        t2 = threading.Thread(target=copy, args=(b, a), daemon=True)
        t1.start(); t2.start()
        t1.join(); t2.join()
        a.close(); b.close()

    def stop(self):
        self._stop.set()
        try:
            self.server.close()
        except Exception:
            pass


# ------------------------------------------------------------------ 伪终端连接
class PtyConnection(Connection):
    def __init__(self, session, cfg, callbacks, cmdline, cwd=None, env=None):
        Connection.__init__(self, session, cfg, callbacks)
        self.cmdline = cmdline if isinstance(cmdline, str) else quote_cmdline(cmdline)
        self.cwd = cwd or None
        # 子进程一律使用软件自带的工具环境，不依赖系统 PATH 里的组件
        self.env = bundle.bundled_env()
        if env:
            self.env.update({str(k): str(v) for k, v in env.items()})
        self.pty = None
        self.description = self.cmdline

    def start(self):
        self._state("正在启动 ...")
        self.pty = PtyProcess(self.cmdline, cwd=self.cwd, env=self.env,
                              cols=max(2, int(self.session.get("cols") or self.cfg.get("cols") or 80)),
                              rows=max(1, int(self.session.get("rows") or self.cfg.get("rows") or 24)),
                              on_data=self._on_data, on_exit=self._on_exit)
        try:
            ok = self.pty.start()
        except Exception as e:
            self._error("启动失败: %s" % e)
            self._fire_close()
            return False
        if not ok:
            self._error("无法启动进程：%s" % self.cmdline[:120])
            self._fire_close()
            return False
        self.alive = True
        self._state("已连接 · " + ("ConPTY" if conpty_available() else "兼容管道"))
        threading.Thread(target=self._writer, daemon=True, name="pty-writer").start()
        return True

    def _on_data(self, data):
        self._emit(data)

    def _on_exit(self, code):
        self._fire_close()

    def _writer(self):
        while self.alive:
            try:
                data = self._q.get(timeout=0.15)
            except queue.Empty:
                continue
            if data is None:
                break
            if self.pty:
                self.pty.write(data)

    def resize(self, cols, rows):
        if self.pty:
            self.pty.resize(cols, rows)

    def stop(self):
        if self.pty:
            try:
                self.pty.close()
            except Exception:
                pass
        Connection.stop(self)

    def startup_info(self):
        return self.cmdline


# ------------------------------------------------------------------ socket 基类
class SocketConnection(Connection):
    def __init__(self, session, cfg, callbacks, default_port):
        Connection.__init__(self, session, cfg, callbacks)
        self.host = session.get("host", "")
        self.port = int(session.get("port") or default_port)
        self.sock = None

    def start(self):
        if not self.host:
            self._error("主机地址为空")
            self._fire_close()
            return False
        self._state("正在连接 %s:%d ..." % (self.host, self.port))
        try:
            self.sock = open_proxied_socket(self.host, self.port, self.session, self.cfg)
        except Exception as e:
            self._error("连接失败: %s" % e)
            self._fire_close()
            return False
        self.sock.settimeout(None)
        self.alive = True
        self._state("已连接 %s:%d" % (self.host, self.port))
        threading.Thread(target=self._reader, daemon=True, name="sock-reader").start()
        threading.Thread(target=self._writer, daemon=True, name="sock-writer").start()
        self.on_connected()
        return True

    def on_connected(self):
        pass

    def _reader(self):
        while self.alive:
            try:
                data = self.sock.recv(16384)
            except Exception:
                break
            if not data:
                break
            self._emit(data)
        self._fire_close()

    def _writer(self):
        while self.alive:
            try:
                data = self._q.get(timeout=0.15)
            except queue.Empty:
                continue
            if data is None:
                break
            try:
                self.sock.sendall(data)
            except Exception:
                break

    def stop(self):
        try:
            if self.sock:
                self.sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        Connection.stop(self)

    def startup_info(self):
        return "%s://%s:%d" % (self.session.get("protocol"), self.host, self.port)


class TelnetConnection(SocketConnection):
    IAC_SUPPORT_WILL = {OPT_ECHO, OPT_SGA, OPT_STATUS, OPT_TTYPE, OPT_NAWS}
    IAC_SUPPORT_DO = {OPT_SGA, OPT_TTYPE, OPT_NAWS}

    def __init__(self, session, cfg, callbacks):
        SocketConnection.__init__(self, session, cfg, callbacks, 23)
        self._iac = 0
        self._sb = bytearray()
        self.term = session.get("term_type") or "xterm"

    def _send_iac(self, *args):
        try:
            self.sock.sendall(bytes([IAC]) + bytes(list(args)))
        except Exception:
            pass

    def on_connected(self):
        self.resize(self.session.get("cols") or self.cfg.get("cols") or 80,
                    self.session.get("rows") or self.cfg.get("rows") or 24)

    def resize(self, cols, rows):
        if not self.alive:
            return
        try:
            self.sock.sendall(bytes([IAC, SB, OPT_NAWS]) +
                              struct.pack(">HH", cols & 0xffff, rows & 0xffff) +
                              bytes([IAC, SE]))
        except Exception:
            pass

    def send_anti_idle(self):
        if self.alive:
            self._send_iac(NOP)

    def _emit(self, data, log=True):
        """剥离 IAC 协商，只把纯文本交给终端解析器。"""
        plain = bytearray()
        for b in data:
            if self._iac == 1:
                if b == IAC:
                    plain.append(IAC); self._iac = 0
                elif b in (DO, DONT, WILL, WONT):
                    self._iac = b
                elif b == SB:
                    self._iac = 2; self._sb = bytearray()
                elif b in (GA, NOP, DM, BRK, IP, AO, AYT, EOR, EL, EC, SE):
                    self._iac = 0
                else:
                    self._iac = 0
            elif self._iac == 2:
                if b == IAC:
                    self._iac = 3
                else:
                    self._sb.append(b)
            elif self._iac == 3:
                if b == SE:
                    self._handle_sb(bytes(self._sb))
                    self._sb = bytearray()
                    self._iac = 0
                else:
                    self._sb.append(IAC); self._sb.append(b); self._iac = 2
            elif self._iac in (DO, DONT, WILL, WONT):
                cmd, opt = self._iac, b
                if cmd == WILL:
                    self._send_iac(DO if opt in self.IAC_SUPPORT_WILL else DONT, opt)
                elif cmd == DO:
                    if opt in self.IAC_SUPPORT_DO:
                        self._send_iac(WILL, opt)
                        if opt == OPT_NAWS:
                            self.resize(self.session.get("cols") or 80,
                                        self.session.get("rows") or 24)
                    else:
                        self._send_iac(WONT, opt)
                self._iac = 0
            elif b == IAC:
                self._iac = 1
            else:
                plain.append(b)
        Connection._emit(self, bytes(plain), log)

    def _handle_sb(self, buf):
        if buf and buf[0] == OPT_TTYPE:
            name = self.term.encode("ascii", "replace")[:40]
            try:
                self.sock.sendall(bytes([IAC, SB, OPT_TTYPE, 0]) + name + bytes([IAC, SE]))
            except Exception:
                pass


class RawConnection(SocketConnection):
    def __init__(self, session, cfg, callbacks):
        SocketConnection.__init__(self, session, cfg, callbacks, 23)


class RloginConnection(SocketConnection):
    def __init__(self, session, cfg, callbacks):
        SocketConnection.__init__(self, session, cfg, callbacks, 513)

    def on_connected(self):
        import getpass
        local = self.session.get("rlogin_local_user") or getpass.getuser()
        remote = (self.session.get("rlogin_remote_user")
                  or get_username(self.session) or getpass.getuser())
        term = "%s/38400" % (self.session.get("term_type") or "xterm")
        try:
            self.sock.sendall(b"\x00" + local.encode() + b"\x00" + remote.encode() +
                              b"\x00" + term.encode() + b"\x00")
        except Exception:
            pass


# ------------------------------------------------------------------ 串口
# DCB 里与流控相关的位（Win32 dcb.h 的 fBinary..fAbortOnError 位域）
DCB_F_OUTX_CTS_FLOW = 1 << 2
DCB_F_OUTX_DSR_FLOW = 1 << 3
DCB_F_DTR_CONTROL = 3 << 4
DCB_F_TX_CONTINUE_ON_XOFF = 1 << 7
DCB_F_OUTX = 1 << 8
DCB_F_INX = 1 << 9
DCB_F_RTS_CONTROL = 3 << 12
DCB_FLOW_MASK = (DCB_F_OUTX_CTS_FLOW | DCB_F_OUTX_DSR_FLOW | DCB_F_DTR_CONTROL |
                 DCB_F_TX_CONTINUE_ON_XOFF | DCB_F_OUTX | DCB_F_INX | DCB_F_RTS_CONTROL)


def apply_serial_flow(bits, flow):
    """按 serial_flow 设置 DCB 的 fBits 位，返回新的 fBits。

    flow 取值：none（默认）/ xonxoff（软件）/ hardware（RTS-CTS）。
    旧实现从来没把 serial_flow 写进 DCB，界面上选了流控也不生效
    （测试报告 BUG-07）。
    """
    f = str(flow or "none").strip().lower()
    bits = (int(bits) & ~DCB_FLOW_MASK) & 0xFFFFFFFF
    if f in ("xonxoff", "xon/xoff", "xon-xoff", "software", "soft"):
        # 软件流控：收发都用 XON/XOFF，且不因 XOFF 暂停发送
        bits |= DCB_F_TX_CONTINUE_ON_XOFF | DCB_F_OUTX | DCB_F_INX
    elif f in ("hardware", "rtscts", "rts/cts", "rts-cts", "hard"):
        # 硬件流控：CTS 控制发送，RTS 由驱动自动握手
        bits |= DCB_F_OUTX_CTS_FLOW | (2 << 12)
    else:
        bits |= (1 << 12)                           # 无流控：RTS 常开（RTS_CONTROL_ENABLE）
    bits |= (1 << 4)                                # DTR_CONTROL_ENABLE
    return bits


class DCB(ctypes.Structure):
    _fields_ = [("DCBlength", wintypes.DWORD), ("BaudRate", wintypes.DWORD),
                ("fBits", wintypes.DWORD), ("wReserved", wintypes.WORD),
                ("XonLim", wintypes.WORD), ("XoffLim", wintypes.WORD),
                ("ByteSize", ctypes.c_byte), ("Parity", ctypes.c_byte),
                ("StopBits", ctypes.c_byte), ("XonChar", ctypes.c_char),
                ("XoffChar", ctypes.c_char), ("ErrorChar", ctypes.c_char),
                ("EofChar", ctypes.c_char), ("EvtChar", ctypes.c_char),
                ("wReserved1", wintypes.WORD)]


class COMMTIMEOUTS(ctypes.Structure):
    _fields_ = [("ReadIntervalTimeout", wintypes.DWORD),
                ("ReadTotalTimeoutMultiplier", wintypes.DWORD),
                ("ReadTotalTimeoutConstant", wintypes.DWORD),
                ("WriteTotalTimeoutMultiplier", wintypes.DWORD),
                ("WriteTotalTimeoutConstant", wintypes.DWORD)]


class SerialConnection(Connection):
    def __init__(self, session, cfg, callbacks):
        Connection.__init__(self, session, cfg, callbacks)
        self.port_name = session.get("serial_port") or "COM1"
        self.handle = None
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def start(self):
        k = self._k32
        k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.HANDLE]
        k.CreateFileW.restype = wintypes.HANDLE
        path = "\\\\.\\" + self.port_name
        h = k.CreateFileW(path, 0xC0000000, 0, None, 3, 0, None)
        if not h or h in (-1, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF):
            self._error("无法打开串口 %s" % self.port_name)
            self._fire_close()
            return False
        self.handle = h
        dcb = DCB()
        dcb.DCBlength = ctypes.sizeof(DCB)
        spec = "baud=%d parity=%s data=%d stop=%d" % (
            int(self.session.get("serial_baud") or 9600),
            str(self.session.get("serial_parity") or "N")[:1],
            int(self.session.get("serial_data") or 8),
            int(self.session.get("serial_stop") or 1))
        k.BuildCommDCBW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(DCB)]
        k.BuildCommDCBW.restype = wintypes.BOOL
        if not k.BuildCommDCBW(spec, ctypes.byref(dcb)):
            self._error("串口参数无效: %s" % spec)
            self.stop()
            return False
        # ---- 流控（BUG-07）：serial_flow 一直没有写进 DCB，界面上选
        # “XON/XOFF”或“RTS/CTS”其实仍按无流控跑，大数据量下发时丢字节。
        flow = str(self.session.get("serial_flow") or "none").strip().lower()
        dcb.fBits = apply_serial_flow(dcb.fBits, flow)
        k.SetCommState.argtypes = [wintypes.HANDLE, ctypes.POINTER(DCB)]
        k.SetCommState.restype = wintypes.BOOL
        if not k.SetCommState(h, ctypes.byref(dcb)):
            self._error("设置串口失败")
            self.stop()
            return False
        to = COMMTIMEOUTS(20, 0, 30, 0, 100)
        k.SetCommTimeouts.argtypes = [wintypes.HANDLE, ctypes.POINTER(COMMTIMEOUTS)]
        k.SetCommTimeouts.restype = wintypes.BOOL
        k.SetCommTimeouts(h, ctypes.byref(to))
        k.PurgeComm.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.PurgeComm(h, 0x000F)
        self.alive = True
        self._state("串口已打开 %s %s 流控=%s" % (self.port_name, spec, flow))
        threading.Thread(target=self._reader, daemon=True, name="com-reader").start()
        threading.Thread(target=self._writer, daemon=True, name="com-writer").start()
        return True

    def _reader(self):
        k = self._k32
        k.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                              ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        k.ReadFile.restype = wintypes.BOOL
        buf = ctypes.create_string_buffer(4096)
        n = wintypes.DWORD(0)
        while self.alive:
            if not k.ReadFile(self.handle, buf, 4096, ctypes.byref(n), None):
                continue
            if n.value:
                self._emit(buf.raw[:n.value])
        self._fire_close()

    def _writer(self):
        k = self._k32
        k.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                               ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        k.WriteFile.restype = wintypes.BOOL
        n = wintypes.DWORD(0)
        while self.alive:
            try:
                data = self._q.get(timeout=0.15)
            except queue.Empty:
                continue
            if data is None:
                break
            b = ctypes.create_string_buffer(data, len(data))
            k.WriteFile(self.handle, b, len(data), ctypes.byref(n), None)

    def stop(self):
        if self.handle:
            try:
                ctypes.windll.kernel32.CloseHandle(self.handle)
            except Exception:
                pass
            self.handle = None
        Connection.stop(self)

    def startup_info(self):
        return "%s @ %d" % (self.port_name, self.session.get("serial_baud") or 9600)


# ------------------------------------------------------------------ 工厂
_SSH1_WARNED = False          # SSH-1 密码明文前提只在首次连接时提示一次


def _mask_cmdline(args):
    """展示用命令行：把 -pw 后面的密码替换成 ******。

    plink 0.67 不支持 -pwfile（实测 unknown option），SSH-1 只能用 -pw 传
    密码。这个命令行原本会被原样当成“连接说明”显示在标签/状态栏，
    等于把密码摆在界面上（测试报告 BUG-10 的另一半）。
    """
    out, hide = [], False
    for a in args:
        if hide:
            out.append("******")
            hide = False
            continue
        out.append(str(a))
        if str(a) in ("-pw", "--password"):
            hide = True
    return quote_cmdline(out)


def _split_cmd_args(text):
    """把“附加参数”字符串切成参数列表（支持双引号包住含空格的参数）。

    只实现 Windows 命令行最常用的一层规则：引号内空格不分段，
    引号可出现在参数中间（如 -Directory="D:\a b"）。
    """
    out, cur, quote = [], [], False
    for ch in text or "":
        if ch == '"':
            quote = not quote
            continue
        if ch in (" ", "\t") and not quote:
            if cur:
                out.append("".join(cur))
                cur = []
            continue
        cur.append(ch)
    if cur:
        out.append("".join(cur))
    return out


def ssh_executable(cfg):
    """优先用户指定 → 随包内置 OpenSSH → 系统兜底（正常不会走到）。"""
    p = cfg.get("ssh_path", "auto")
    if p and p != "auto" and os.path.isfile(p):
        return p
    exe = bundle.tool("openssh", "ssh.exe")
    if exe:
        return exe
    candidates = [os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "OpenSSH", "ssh.exe"),
                  r"C:\Windows\System32\OpenSSH\ssh.exe"]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return "ssh.exe"


def _ssh_tool(cfg, kind):
    name = {"sftp": "sftp.exe", "scp": "scp.exe"}.get(kind, "ssh.exe")
    custom = cfg.get("ssh_path", "auto")
    if custom and custom != "auto" and os.path.isfile(custom):
        p = os.path.join(os.path.dirname(custom), name)
        return p if os.path.isfile(p) else name
    return bundle.tool("openssh", name) or name


def build_ssh1_args(session, cfg):
    """SSH-1 走随包的 PuTTY plink（OpenSSH 早已移除 SSH-1 支持）。"""
    exe = bundle.tool("ssh1", "plink.exe")
    if not exe:
        return build_ssh_args(session, cfg, "ssh")
    port = int(session.get("port") or 22)
    user = get_username(session)
    host = session.get("host") or ""
    args = [exe, "-1", "-ssh", "-P", str(port)]
    if user:
        args += ["-l", user]
    if session.get("keyfile"):
        args += ["-i", session["keyfile"]]
    try:
        pwd = get_password(session)
        if pwd:
            args += ["-pw", pwd]
    except Exception:
        pass
    args.append(host)
    return args


def build_ssh_args(session, cfg, kind="ssh"):
    """根据会话构造 OpenSSH 命令行。"""
    exe = _ssh_tool(cfg, kind)
    proto = session.get("protocol", "SSH2")
    args = [exe]
    # config / known_hosts 全部收敛到 TermX_Data，随绿色包走，不写用户目录
    args += bundle.ssh_identity_options()
    port = int(session.get("port") or 22)
    user = get_username(session)
    host = session.get("host") or ""
    strict = session.get("ssh_strict") or cfg.get("ssh_strict_host_key") or "accept-new"
    alive = int(session.get("ssh_alive_interval") or cfg.get("ssh_alive_interval") or 30)
    args += ["-o", "StrictHostKeyChecking=%s" % strict,
             "-o", "ServerAliveInterval=%d" % max(1, alive),
             "-o", "ServerAliveCountMax=3",
             "-o", "ConnectTimeout=15"]
    # 老旧网络设备（交换机/路由器）常只提供 ssh-rsa(SHA-1)、SHA-1 KEX 或 CBC，
    # OpenSSH 8.8+ 默认禁用，会报 "no matching host key type found. Their offer: ssh-rsa"。
    # 用 "+" 追加而非覆盖：现代算法仍优先，只在对方仅支持旧算法时回退。
    if cfg.get("ssh_legacy_algo", True):
        args += ["-o", "HostKeyAlgorithms=+ssh-rsa",
                 "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa",
                 "-o", "KexAlgorithms=+diffie-hellman-group14-sha1,diffie-hellman-group1-sha1",
                 "-o", "Ciphers=+aes128-cbc,aes192-cbc,aes256-cbc,3des-cbc"]
    if cfg.get("ssh_compression") or session.get("ssh_compression"):
        args += ["-C"]
    if cfg.get("ssh_agent_forward") or session.get("ssh_agent_forward"):
        args += ["-A"]
    if kind == "ssh" and (cfg.get("ssh_x11") or session.get("ssh_x11")):
        args += ["-X"]
    if session.get("auth") == "publickey" and session.get("keyfile"):
        args += ["-i", session["keyfile"], "-o", "IdentitiesOnly=yes"]
    if proto == "SSH1":
        args += ["-1"]
    for f in session.get("forwards") or []:
        k = (f.get("kind") or "L").upper()
        if k == "D":
            args += ["-D", "%s:%s" % (f.get("addr", "127.0.0.1"), f.get("port", 1080))]
        elif k == "R":
            args += ["-R", "%s:%s:%s:%s" % (f.get("addr", "127.0.0.1"), f.get("port", 0),
                                             f.get("target", "127.0.0.1"), f.get("target_port", 0))]
        else:
            args += ["-L", "%s:%s:%s:%s" % (f.get("addr", "127.0.0.1"), f.get("port", 0),
                                             f.get("target", "127.0.0.1"), f.get("target_port", 0))]
    if kind == "sftp":
        args += ["-P", str(port)]
    elif kind == "scp":
        args += ["-P", str(port)]
    else:
        args += ["-p", str(port)]
        if session.get("no_terminal"):
            args += ["-N"]
        else:
            args += ["-tt", "-o", "SendEnv=TERM"]
    target = ("%s@%s" % (user, host)) if user else host
    args.append(target)
    if kind == "ssh" and session.get("command"):
        args += ["--", session["command"]]
    return args


def create_connection(session, cfg, callbacks):
    """返回 (Connection 实例, 说明文字)。连接对象自行管理代理隧道生命周期。"""
    global _SSH1_WARNED
    proto = session.get("protocol", "SSH2")
    if proto == "SSH1":
        # SSH-1 由随包 plink 提供；PuTTY 自带 TCP 栈，代理隧道改由软件层处理
        args = build_ssh1_args(session, cfg)
        env1 = {"TERM": session.get("term_type") or "xterm"}
        conn = PtyConnection(session, cfg, callbacks, args, env=env1)
        if not _SSH1_WARNED:
            _SSH1_WARNED = True
            conn._state("安全提示：SSH-1 由随包 plink 0.67 提供，该版本只能通过"
                        "命令行参数传密码（本机进程命令行里可见）。安全要求高的"
                        "环境请改用 SSH2 + 密钥认证。")
        return conn, _mask_cmdline(args)
    if proto in ("SSH2", "SFTP"):
        kind = "sftp" if proto == "SFTP" else "ssh"
        args = build_ssh_args(session, cfg, kind)
        tunnel = None
        mode = session.get("proxy", "inherit")
        need_proxy = mode == "custom" or (mode == "inherit" and cfg.get("proxy_type", "none") != "none")
        if need_proxy:
            tunnel = ProxyTunnel(session.get("host", ""), int(session.get("port") or 22),
                                 session, cfg, on_error=callbacks.get("on_error"))
            try:
                lp = tunnel.start()
                # 外部 ssh 连本地隧道端口，HostKeyAlias 保持原始主机名指纹校验
                for i, a in enumerate(args):
                    if a in ("-P", "-p") and i + 1 < len(args):
                        args[i + 1] = str(lp)
                for i, a in enumerate(args):
                    if a and (a.endswith("@" + str(session.get("host", ""))) or
                              a == session.get("host", "")):
                        args[i] = a.replace(str(session.get("host", "")), "127.0.0.1")
                if kind == "ssh":
                    args = [args[0], "-o", "HostKeyAlias=%s" % session.get("host", "")] + args[1:]
            except Exception as e:
                callbacks.get("on_error", lambda x: None)("代理隧道启动失败: %s" % e)
        env = {"TERM": session.get("term_type") or "xterm"}
        if kind == "ssh" and (cfg.get("ssh_x11") or session.get("ssh_x11")):
            disp = bundle.ensure_xserver()
            if disp:
                env["DISPLAY"] = disp
        conn = PtyConnection(session, cfg, callbacks, args, env=env)
        conn.proxy_tunnel = tunnel
        _orig_stop = conn.stop
        def stop_all():
            if tunnel:
                tunnel.stop()
            _orig_stop()
        conn.stop = stop_all
        return conn, quote_cmdline(args)
    if proto == "Telnet":
        return TelnetConnection(session, cfg, callbacks), "Telnet"
    if proto == "Rlogin":
        return RloginConnection(session, cfg, callbacks), "Rlogin"
    if proto == "Raw":
        return RawConnection(session, cfg, callbacks), "Raw TCP"
    if proto == "Serial":
        return SerialConnection(session, cfg, callbacks), "Serial"
    if proto == "LocalShell":
        shell = session.get("shell_path") or cfg.get("local_shell") or "powershell.exe"
        extra = session.get("shell_args") or ""
        # 旧实现 "%s %s" 直接拼字符串：shell_path 落在 C:\Program Files\
        # 下（如 PowerShell 7）时，路径里的空格没被引号包住，CreateProcess
        # 会把 "C:\Program" 当可执行文件（测试报告 BUG-02）。
        # 这里拆成参数列表，由 quote_cmdline 按 Windows 规则统一加引号。
        argv = [shell] + _split_cmd_args(extra)
        cmd = quote_cmdline(argv)
        conn = PtyConnection(session, cfg, callbacks, argv,
                             cwd=session.get("shell_cwd") or None,
                             env={"TERM": session.get("term_type") or "xterm"})
        return conn, cmd
    raise ValueError("不支持的协议: %s" % proto)
