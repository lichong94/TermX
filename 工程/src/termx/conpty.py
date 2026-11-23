# -*- coding: utf-8 -*-
"""Windows ConPTY（伪控制台）封装，纯 ctypes，无第三方依赖。

ConPTY 让本地 Shell / OpenSSH / sftp 等子程序以为自己连着真实控制台，
因此彩色输出、交互式密码、vim/htop 等全屏程序都能正常工作。
不支持 ConPTY 的老系统自动降级为普通管道。
"""
import ctypes
import os
import queue
import subprocess
import threading
from ctypes import wintypes

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

HPCON = wintypes.HANDLE
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
STARTF_USESTDHANDLES = 0x00000100
CREATE_NO_WINDOW = 0x08000000


class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
                ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
                ("hStdError", wintypes.HANDLE)]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


def conpty_available():
    try:
        return bool(getattr(k32, "CreatePseudoConsole", None)) and \
               bool(getattr(k32, "ResizePseudoConsole", None))
    except Exception:
        return False


k32.CreatePipe.argtypes = [ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE),
                           ctypes.c_void_p, wintypes.DWORD]
k32.CreatePipe.restype = wintypes.BOOL
k32.CloseHandle.argtypes = [wintypes.HANDLE]
k32.CloseHandle.restype = wintypes.BOOL
k32.ClosePseudoConsole.argtypes = [HPCON]
k32.ResizePseudoConsole.argtypes = [HPCON, COORD]
k32.CreatePseudoConsole.argtypes = [COORD, wintypes.HANDLE, wintypes.HANDLE,
                                    wintypes.DWORD, ctypes.POINTER(HPCON)]
k32.CreatePseudoConsole.restype = ctypes.c_long
k32.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                                                  wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t)]
k32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
k32.UpdateProcThreadAttribute.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t,
                                          ctypes.c_void_p, ctypes.c_size_t,
                                          ctypes.c_void_p, ctypes.c_void_p]
k32.UpdateProcThreadAttribute.restype = wintypes.BOOL
k32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
k32.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p,
                               ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD,
                               ctypes.c_void_p, wintypes.LPCWSTR,
                               ctypes.POINTER(STARTUPINFOEXW),
                               ctypes.POINTER(PROCESS_INFORMATION)]
k32.CreateProcessW.restype = wintypes.BOOL
k32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                         ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
k32.ReadFile.restype = wintypes.BOOL
k32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                          ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
k32.WriteFile.restype = wintypes.BOOL
k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
k32.TerminateProcess.restype = wintypes.BOOL
k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
k32.WaitForSingleObject.restype = wintypes.DWORD


def quote_cmdline(args):
    return subprocess.list2cmdline([str(a) for a in args if a is not None and a != ""])


def _close_handles(*handles):
    """尽力关闭一组内核句柄（None / 空句柄自动跳过）。

    _start_conpty 的每条失败分支此前只关了部分管道句柄：属性列表初始化
    失败、UpdateProcThreadAttribute 失败、CreateProcess 失败这三条路径都
    漏了 h_in_read / h_out_write，反复重连（或反复开关标签）后句柄数持续
    增长，最终 CreatePipe 失败、所有终端都起不来（测试报告 BUG-09）。
    """
    for h in handles:
        if not h:
            continue
        try:
            k32.CloseHandle(h)
        except Exception:
            pass


class PtyProcess(object):
    """ConPTY 子进程。on_data(bytes) / on_exit(code) 在后台线程回调。"""

    def __init__(self, cmdline, cwd=None, cols=80, rows=24, env=None,
                 on_data=None, on_exit=None, force_pipe=False):
        self.cmdline = cmdline
        self.cwd = cwd or None
        self.cols, self.rows = cols, rows
        self.env = env
        self.on_data = on_data
        self.on_exit = on_exit
        self.alive = False
        self._closing = False
        self._closed = False
        self._q = queue.Queue()
        self._hpcon = None
        self._h_in_write = None
        self._h_out_read = None
        self._h_process = None
        self._proc = None
        self._use_conpty = conpty_available() and not force_pipe
        self._threads = []

    # ------------------------------------------------------------------ 启动
    def start(self):
        if self._use_conpty:
            try:
                if self._start_conpty():
                    return True
            except Exception:
                self._cleanup_handles()
        return self._start_pipe()

    def _start_conpty(self):
        h_in_read = wintypes.HANDLE()
        h_out_write = wintypes.HANDLE()
        h_in_write = wintypes.HANDLE()
        h_out_read = wintypes.HANDLE()
        if not k32.CreatePipe(ctypes.byref(h_in_read), ctypes.byref(h_in_write), None, 0):
            return False
        if not k32.CreatePipe(ctypes.byref(h_out_read), ctypes.byref(h_out_write), None, 0):
            k32.CloseHandle(h_in_read)
            k32.CloseHandle(h_in_write)
            return False
        hpcon = HPCON()
        hr = k32.CreatePseudoConsole(COORD(self.cols, self.rows), h_in_read, h_out_write,
                                     0, ctypes.byref(hpcon))
        if hr < 0 or not hpcon:
            for h in (h_in_read, h_in_write, h_out_read, h_out_write):
                k32.CloseHandle(h)
            return False
        size = ctypes.c_size_t(0)
        k32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        attr = ctypes.create_string_buffer(size.value)
        if not k32.InitializeProcThreadAttributeList(attr, 1, 0, ctypes.byref(size)):
            k32.ClosePseudoConsole(hpcon)
            _close_handles(h_in_read, h_in_write, h_out_read, h_out_write)
            return False
        # PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE 按“句柄值”传递（不是指向句柄的指针）
        if not k32.UpdateProcThreadAttribute(attr, 0, PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
                                             ctypes.c_void_p(hpcon.value), ctypes.sizeof(HPCON),
                                             None, None):
            k32.DeleteProcThreadAttributeList(attr)
            k32.ClosePseudoConsole(hpcon)
            _close_handles(h_in_read, h_in_write, h_out_read, h_out_write)
            return False

        si = STARTUPINFOEXW()
        si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
        # 父进程（尤其是 GUI / 管道环境）的标准句柄会被子进程继承，
        # 导致输出串到外部控制台而不是 ConPTY。显式清空标准句柄后，
        # 子进程会打开 CONIN$/CONOUT$，正确接入伪控制台。
        si.StartupInfo.dwFlags = STARTF_USESTDHANDLES
        si.StartupInfo.hStdInput = None
        si.StartupInfo.hStdOutput = None
        si.StartupInfo.hStdError = None
        si.lpAttributeList = ctypes.cast(attr, ctypes.c_void_p)
        pi = PROCESS_INFORMATION()
        cmd = ctypes.create_unicode_buffer(self.cmdline)
        env_buf = None
        if self.env:
            base = dict(os.environ)
            base.update({str(k): str(v) for k, v in self.env.items()})
            env_buf = ctypes.create_unicode_buffer(
                "\0".join("%s=%s" % (k, v) for k, v in base.items()) + "\0\0")
        ok = k32.CreateProcessW(None, cmd, None, None, False,
                                EXTENDED_STARTUPINFO_PRESENT | (0x00000400 if env_buf else 0),
                                ctypes.cast(env_buf, ctypes.c_void_p) if env_buf else None,
                                self.cwd, ctypes.byref(si), ctypes.byref(pi))
        k32.DeleteProcThreadAttributeList(attr)
        if not ok:
            k32.ClosePseudoConsole(hpcon)
            _close_handles(h_in_read, h_in_write, h_out_read, h_out_write)
            return False
        k32.CloseHandle(pi.hThread)
        # CreateProcess 之后释放传给 CreatePseudoConsole 的两个句柄，
        # 这样 conhost 关闭通道时 ReadFile 能正确检测到断开。
        k32.CloseHandle(h_in_read)
        k32.CloseHandle(h_out_write)

        self._hpcon = hpcon
        self._h_in_write = h_in_write
        self._h_out_read = h_out_read
        self._h_process = pi.hProcess
        self.alive = True
        self._start_workers(
            reader=lambda: self._read_handle(self._h_out_read),
            writer=lambda data: self._write_handle(self._h_in_write, data),
            exited=lambda: self._wait_process(),
        )
        return True

    def _start_pipe(self):
        """无 ConPTY 时的降级路径：普通匿名管道。"""
        try:
            self._proc = subprocess.Popen(
                self.cmdline, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, cwd=self.cwd, env=self.env,
                creationflags=CREATE_NO_WINDOW, bufsize=0)
        except Exception:
            if self.on_exit:
                self.on_exit(-1)
            return False
        self.alive = True
        self._start_workers(
            reader=lambda: self._read_stream(self._proc.stdout),
            writer=lambda data: self._write_stream(self._proc.stdin, data),
            exited=lambda: self._wait_subprocess(),
        )
        return True

    def _start_workers(self, reader, writer, exited):
        def rloop():
            try:
                reader()
            except Exception:
                pass
            finally:
                if not self._closing:
                    self._fire_exit()
        def wloop():
            while self.alive or not self._q.empty():
                try:
                    data = self._q.get(timeout=0.15)
                except queue.Empty:
                    continue
                if data is None:
                    break
                try:
                    writer(data)
                except Exception:
                    break
        def xloop():
            try:
                exited()
            except Exception:
                pass
            finally:
                self._fire_exit()
        t1 = threading.Thread(target=rloop, name="pty-read", daemon=True)
        t2 = threading.Thread(target=wloop, name="pty-write", daemon=True)
        t3 = threading.Thread(target=xloop, name="pty-wait", daemon=True)
        self._threads = [t1, t2, t3]
        for t in self._threads:
            t.start()

    def _fire_exit(self):
        if self._closing:
            return
        self._closing = True
        self.alive = False
        if self.on_exit:
            try:
                self.on_exit(0)
            except Exception:
                pass

    # ------------------------------------------------------------------ IO
    def _read_handle(self, h):
        buf = ctypes.create_string_buffer(16384)
        n = wintypes.DWORD(0)
        while self.alive and not self._closing:
            ok = k32.ReadFile(h, buf, 16384, ctypes.byref(n), None)
            if not ok or n.value == 0:
                break
            data = buf.raw[:n.value]
            if self.on_data:
                self.on_data(data)

    def _read_stream(self, stream):
        while self.alive and not self._closing:
            data = stream.read(4096)
            if not data:
                break
            if self.on_data:
                self.on_data(data)

    def _write_handle(self, h, data):
        n = wintypes.DWORD(0)
        pos = 0
        while pos < len(data) and self.alive:
            chunk = data[pos:pos + 8192]
            b = ctypes.create_string_buffer(chunk, len(chunk))
            if not k32.WriteFile(h, b, len(chunk), ctypes.byref(n), None):
                break
            pos += n.value or len(chunk)

    def _write_stream(self, stream, data):
        stream.write(data)
        try:
            stream.flush()
        except Exception:
            pass

    def _wait_process(self):
        if self._h_process:
            k32.WaitForSingleObject(self._h_process, 0xFFFFFFFF)

    def _wait_subprocess(self):
        if self._proc:
            self._proc.wait()

    def write(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8", "replace")
        if self.alive and data:
            self._q.put(data)

    def resize(self, cols, rows):
        self.cols, self.rows = max(2, cols), max(1, rows)
        if self._hpcon:
            try:
                k32.ResizePseudoConsole(self._hpcon, COORD(self.cols, self.rows))
            except Exception:
                pass

    def _cleanup_handles(self):
        for h in (self._h_in_write, self._h_out_read):
            if h:
                try:
                    k32.CloseHandle(h)
                except Exception:
                    pass
        if self._hpcon:
            try:
                k32.ClosePseudoConsole(self._hpcon)
            except Exception:
                pass
        if self._h_process:
            try:
                k32.CloseHandle(self._h_process)
            except Exception:
                pass
        self._h_in_write = self._h_out_read = self._h_process = self._hpcon = None

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._closing = True
        self.alive = False
        try:
            self._q.put(None)
        except Exception:
            pass
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
        # 关闭顺序很关键：先关伪控制台让输出管道断开，读线程才会从
        # 同步 ReadFile 中返回；否则直接 CloseHandle 会与阻塞的同步 I/O 死锁。
        # 同一个 HPCON 只能关闭一次，重复关闭会导致宿主进程异常退出。
        if self._hpcon:
            try:
                k32.ClosePseudoConsole(self._hpcon)
            except Exception:
                pass
            self._hpcon = None
        if self._h_process:
            try:
                k32.TerminateProcess(self._h_process, 0)
            except Exception:
                pass
        if self._threads:
            try:
                self._threads[0].join(0.8)
            except Exception:
                pass
        for h in (self._h_in_write, self._h_out_read):
            if h:
                try:
                    k32.CloseHandle(h)
                except Exception:
                    pass
        if self._h_process:
            try:
                k32.CloseHandle(self._h_process)
            except Exception:
                pass
        self._h_in_write = self._h_out_read = self._h_process = None


CONPTY_OK = conpty_available()
