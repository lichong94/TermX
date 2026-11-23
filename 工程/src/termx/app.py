# -*- coding: utf-8 -*-
"""程序入口与内置自检。打包后执行 TermX.exe --selftest 可验证核心模块。"""
import os
import sys
import threading
import time
import traceback


def selftest(emit=None):
    """不启动界面的核心自检，返回失败数量。"""
    emit = emit or (lambda s: print(s))
    failed = []
    passed = []

    def check(name, fn):
        try:
            fn()
            passed.append(name)
        except Exception as e:
            failed.append("%s: %s" % (name, e))
            emit("  [FAIL] %s -> %s" % (name, e))

    from .ansi import ScreenBuffer, REVERSE, BOLD
    from .config import Config, SCHEMES, color_256
    from .sessions import new_session, set_password, get_password, VariableTable, expand_vars
    from .scripting import Capture, BatchDispatcher, BatchResult, parse_login_script
    from .conpty import conpty_available
    from .connections import build_ssh_args, ANSI_RE

    def t_ansi_basic():
        b = ScreenBuffer(10, 3, SCHEMES["Dark+"])
        b.feed(b"hello\r\nworld")
        assert b.line_text(0) == "hello", b.line_text(0)
        assert b.line_text(1) == "world", b.line_text(1)
        assert b.cursor_snapshot() == (2, 6), b.cursor_snapshot()

    def t_ansi_sgr():
        b = ScreenBuffer(10, 2, SCHEMES["Dark+"])
        b.feed(b"\x1b[1;31mRED\x1b[0m\x1b[32mGREEN")
        assert b.lines[0][0].ch == "R"
        assert b.lines[0][0].attrs & BOLD
        assert b.lines[0][0].fg == SCHEMES["Dark+"]["ansi"][1]
        assert b.lines[0][3].fg == SCHEMES["Dark+"]["ansi"][2], b.lines[0][3].fg

    def t_ansi_cursor():
        b = ScreenBuffer(20, 5, SCHEMES["Dark+"])
        b.feed(b"ABCDE\x1b[3;10HXY")
        assert b.lines[2][9].ch == "X"
        assert b.lines[2][10].ch == "Y"
        b.feed(b"\x1b[2K")
        assert b.line_text(2) == "", repr(b.line_text(2))
        b.feed(b"\x1b[2J")
        assert b.plain_text().strip() == ""

    def t_ansi_scroll_alt():
        b = ScreenBuffer(5, 2, SCHEMES["Dark+"], scrollback=100)
        b.feed(b"one\r\ntwo\r\nthree")
        assert len(b.scrollback) >= 1, len(b.scrollback)
        assert b.line_text(1) == "three"
        b.feed(b"\x1b[?1049hALT")
        assert b.alt_active and b.line_text(0) == "ALT"
        b.feed(b"\x1b[?1049l")
        assert not b.alt_active and b.line_text(1) == "three"

    def t_ansi_utf8_and_wide():
        b = ScreenBuffer(20, 2, SCHEMES["Dark+"])
        b.feed("中文 abc".encode("utf-8"))
        assert b.line_text(0).startswith("中文"), repr(b.line_text(0))
        assert b.lines[0][1].ch == "", repr(b.lines[0][1].ch)
        b.feed(b"\r\n\x1b[38;5;196mX\x1b[38;2;1;2;3mY")
        assert b.lines[1][0].fg.startswith("#")
        assert b.lines[1][1].fg == "#010203"

    def t_ansi_osc():
        titles = []
        b = ScreenBuffer(10, 2, SCHEMES["Dark+"], title_cb=titles.append)
        b.feed(b"\x1b]0;myhost\x07")
        assert b.title == "myhost" and titles, (b.title, titles)

    def t_ansi_reply():
        out = []
        b = ScreenBuffer(10, 2, SCHEMES["Dark+"], reply_cb=out.append)
        b.feed(b"\x1b[6n")
        assert out and out[0] == b"\x1b[1;1R", out

    def t_ansi_resize():
        b = ScreenBuffer(10, 4, SCHEMES["Dark+"])
        b.feed(b"abcdefghij")
        b.resize(5, 2)
        assert b.cols == 5 and b.rows == 2
        assert b.line_text(0) == "abcde", b.line_text(0)

    def t_config():
        c = Config()
        assert c.get("font_family")
        assert color_256(196, c.scheme()).startswith("#")
        assert len(SCHEMES) >= 5

    def t_theme_registry():
        from . import ui_theme
        assert len(ui_theme.THEMES) >= 6
        for key, theme in ui_theme.THEMES.items():
            assert theme["scheme"] in SCHEMES, (key, theme["scheme"])
            for field in ("bg", "fg", "toolbar", "panel_hdr", "status", "accent", "ref"):
                assert field in theme, (key, field)
        for key, label in ui_theme.THEME_LABELS:
            assert ui_theme.normalize(label) == key, (label, ui_theme.normalize(label))
            assert ui_theme.normalize(key) == key

    def t_theme_schemes():
        for name in ("Windows Terminal 午夜", "MobaXterm 晴空", "Solarized Dark"):
            s = SCHEMES[name]
            assert len(s["ansi"]) == 16
            assert all(c.startswith("#") and len(c) == 7
                       for c in [s["fg"], s["bg"], s["cursor"]] + s["ansi"])

    def t_session_password():
        s = new_session("SSH2")
        set_password(s, "p@ssw0rd 中文")
        assert get_password(s) == "p@ssw0rd 中文", repr(get_password(s))
        assert s.get("password") == ""

    def t_session_defaults():
        for p in ("SSH2", "Telnet", "Serial", "LocalShell", "SFTP", "Rlogin", "Raw"):
            s = new_session(p)
            assert s["protocol"] == p and s["id"]

    def t_expand_vars():
        m = {"host": "10.0.0.1", "no": "3", "user": "root"}
        assert expand_vars("ping {host} -n ${no} %user%", m) == "ping 10.0.0.1 -n 3 root"

    def t_var_table():
        import tempfile
        fd, p = tempfile.mkstemp(suffix=".csv")
        os.write(fd, "host,user,vlan\r\n1.1.1.1,admin,10\r\n2.2.2.2,root,20\r\n".encode())
        os.close(fd)
        v = VariableTable()
        assert v.load(p) == 2
        assert v.row_for(1, new_session("SSH2"))["vlan"] == "20"
        os.unlink(p)

    def t_capture():
        cap = Capture()
        cap.push(b"\x1b[31mhello\x1b[0m world")
        assert cap.text() == "hello world", repr(cap.text())
        import threading
        def later():
            import time
            time.sleep(0.05)
            cap.push(b"ready# ")
        threading.Thread(target=later, daemon=True).start()
        idx, out, pos = cap.wait_for(["#"], 2)
        assert idx == 0 and out.endswith("#"), (idx, out)
        idx2, _o, _p = cap.wait_for(["NEVER"], 0.2)
        assert idx2 == -1

    def t_login_script():
        rules = parse_login_script(
            'expect "login:"\nsend "admin\\r"\nexpect "Password:"\nsend "secret\\r"')
        assert len(rules) == 2 and rules[0][1] == "admin\r", rules

    def t_ssh_args():
        cfg = Config()
        s = new_session("SSH2", "测试")
        s.update({"host": "1.2.3.4", "username": "root", "port": 2222,
                  "keyfile": r"C:\keys\id_ed25519"})
        args = build_ssh_args(s, cfg)
        joined = " ".join(args)
        assert "2222" in joined and "root@1.2.3.4" in joined and "-tt" in joined
        assert "StrictHostKeyChecking" in joined

    def t_strip_ansi():
        assert ANSI_RE.sub(b"", b"\x1b[31mred\x1b[0m") == b"red"

    def t_batch_export():
        import tempfile
        results = [BatchResult("sw1", "show ver", "output", True, 0.12)]
        fd, p = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        BatchDispatcher.export(results, p, "csv")
        assert os.path.getsize(p) > 20
        os.unlink(p)

    def t_conpty():
        assert isinstance(conpty_available(), bool)

    def t_import_all():
        import termx.main_window  # noqa
        import termx.dialogs  # noqa
        import termx.terminal  # noqa
        import termx.connections  # noqa

    # ---- 脚本引擎 / 批量下发的端到端模拟测试 ----
    class FakeTab(object):
        def __init__(self, name, host):
            self.name = name
            self.session = {"name": name, "host": host, "username": "admin",
                            "port": 22, "protocol": "SSH2", "folder": ""}
            self.cols, self.rows = 80, 24
            self.connected = True
            self.sent = []
            self.cap = Capture()
            self.log_path = ""

        def is_connected(self):
            return self.connected

        def send_text(self, data):
            if isinstance(data, bytes):
                data = data.decode("utf-8", "replace")
            self.sent.append(data)
            self.cap.push(("\r\n" + data).encode("utf-8"))
            self.cap.push(b"OK\r\n# ")

        def start_capture(self):
            return self.cap

        def stop_capture(self, cap):
            pass

        def wait_for(self, patterns, timeout=30, stop=None, regex=False, since=None):
            return self.cap.wait_for(patterns, timeout, since, regex, stop)

        def screen_text(self, *a):
            return "fake screen"

        def clear_screen(self):
            pass

        def set_caption(self, text):
            self.name = text

    class FakeController(object):
        root = None

        def __init__(self):
            self.logs = []
            self._tabs = []

        def ui_call(self, func, *args):
            return func(*args)

        def send_to_tab(self, tab, data):
            if tab:
                tab.send_text(data)

        def wait_for(self, tab, patterns, timeout):
            return tab.wait_for(patterns, timeout)

        def script_log(self, text):
            self.logs.append(str(text))

        def set_status(self, text):
            pass

        def tabs(self):
            return self._tabs

        def tab_count(self):
            return len(self._tabs)

        def tab_at(self, i):
            return self._tabs[i] if 0 <= i < len(self._tabs) else None

        def active_tab(self):
            return self._tabs[0] if self._tabs else None

        def activate_tab(self, tab):
            pass

        def script_connect(self, *a):
            return True

        def disconnect_tab(self, tab):
            if tab:
                tab.connected = False

        def toggle_log(self, *a):
            pass

    def _run_engine(ctl, tab, code, language, timeout=12):
        from .scripting import Engine
        eng = Engine(ctl)
        done = threading.Event()
        out = {}
        eng.run(code, "test", tab, language,
                on_done=lambda ok, msg: (out.update(ok=ok, msg=msg), done.set()),
                on_log=ctl.logs.append)
        return done.wait(timeout), out

    def t_python_script():
        import threading
        ctl = FakeController()
        tab = FakeTab("sw1", "10.0.0.1")
        ctl._tabs = [tab]
        ok, out = _run_engine(ctl, tab,
                              'crt.Screen.Send("show version\\r")\n'
                              'r = crt.Screen.WaitForString("#", 2)\n'
                              'crt.Log("PY_OK=%s" % r)\n', "python")
        assert ok, out
        assert out.get("ok"), out.get("msg")
        assert tab.sent and "show version" in tab.sent[0]
        assert any("PY_OK=True" in x for x in ctl.logs), ctl.logs

    def t_jscript_builtin():
        ctl = FakeController()
        tab = FakeTab("sw1", "10.0.0.1")
        ctl._tabs = [tab]
        ok, out = _run_engine(ctl, tab,
                              'function main(){crt.Screen.Send("show js\\r");'
                              'crt.Sleep(10);crt.Screen.Send("JS_OK\\r");} main();',
                              "jscript", 15)
        assert ok, "JScript 内置引擎执行超时"
        assert out.get("ok"), out.get("msg")
        assert tab.sent and "show js" in tab.sent[0], (out, ctl.logs)

    def t_vbscript_builtin():
        ctl = FakeController()
        tab = FakeTab("sw1", "10.0.0.1")
        ctl._tabs = [tab]
        ok, out = _run_engine(ctl, tab,
                              'Sub Main\n crt.Screen.Send "show vbs" & vbCr\n'
                              ' crt.Sleep 10\n crt.Screen.Send "VBS_OK" & vbCr\n'
                              'End Sub',
                              "vbscript", 15)
        assert ok, "VBScript 内置引擎执行超时"
        assert out.get("ok"), out.get("msg")
        assert tab.sent and "show vbs" in tab.sent[0], (out, ctl.logs)

    def t_bundle_status():
        from . import bundle
        rows = bundle.status()
        for name, ok, path in rows:
            emit("  组件 %-34s %s" % (name, "内置 OK" if ok else ("缺失 " + str(path))))
        critical = ("OpenSSH 全套 (SSH/SFTP/SCP/密钥)", "VC++ 运行库")
        missing = [n for n, ok, _ in rows if n in critical and not ok]
        assert not missing, "核心内置组件缺失: " + ", ".join(missing)

    def t_bundle_isolated_ssh():
        from . import bundle
        assert bundle.verify_isolated(emit=emit), "内置 OpenSSH 无法在隔离 PATH 下运行"

    def t_zmodem_builtin():
        """纯 Python 协议栈内存对接：分片喂入 + 字节级比对。"""
        import shutil
        import tempfile
        from collections import deque
        from . import zmodem
        assert zmodem.available() is True
        assert zmodem.looks_like_zmodem(b"**\x18B00000000000000\r\n\x11") is True
        assert zmodem.looks_like_zmodem(b"just plain terminal text") is False
        assert zmodem._crc16(b"123456789") == 0x31C3, "CRC16 不符合 Zmodem 标准"
        work = tempfile.mkdtemp(prefix="termx_zm_")
        try:
            src = os.path.join(work, "payload.bin")
            inbox = os.path.join(work, "inbox")
            os.makedirs(inbox, exist_ok=True)
            data = bytes((i * 131 + 7) % 256 for i in range(2048))
            with open(src, "wb") as f:
                f.write(data)
            q = deque()
            a = zmodem.Transfer(lambda d: q.append(("B", bytes(d))))
            b = zmodem.Transfer(lambda d: q.append(("A", bytes(d))),
                                directory=inbox)
            a.start_upload([src])
            steps = 0
            while q:
                steps += 1
                assert steps < 20000, "Zmodem 回环未收敛"
                target, chunk = q.popleft()
                node = a if target == "A" else b
                for off in range(0, len(chunk), 7):
                    node.feed(chunk[off:off + 7])
            assert not a.active and not b.active, "传输未正常结束"
            got = os.path.join(inbox, "payload.bin")
            assert os.path.isfile(got), "接收方未写出文件"
            with open(got, "rb") as f:
                assert f.read() == data, "传输内容不一致"
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def t_batch_dispatch():
        import threading
        ctl = FakeController()
        tabs = [FakeTab("sw1", "1.1.1.1"), FakeTab("sw2", "2.2.2.2")]
        ctl._tabs = tabs
        results = []
        done = threading.Event()
        d = BatchDispatcher(ctl, tabs, ["uname -a", "echo {host}"], None,
                            {"parallel": True, "wait_prompt": True,
                             "prompt": r"#\s*$", "timeout": 3, "delay": 0},
                            on_result=results.append,
                            on_done=lambda rs: done.set())
        d.run()
        assert done.wait(12), "批量下发超时"
        assert len(results) == 4, len(results)
        assert all(r.ok for r in results), [(r.command, r.error) for r in results]
        assert any("1.1.1.1" in r.command for r in results)
        assert any("2.2.2.2" in r.command for r in results)

    def t_batch_paging():
        """关闭分页：登录确认后、执行用户命令前，先下发厂商命令。"""
        import threading
        ctl = FakeController()
        tab = FakeTab("hw1", "3.3.3.3")
        ctl._tabs = [tab]
        results = []
        done = threading.Event()
        d = BatchDispatcher(
            ctl, [tab], ["display version"], None,
            {"parallel": True, "wait_prompt": True, "prompt": r"#\s*$",
             "timeout": 3, "delay": 0,
             "paging_cmd": "screen-length 0 temporary", "paging_name": "华为"},
            on_result=results.append,
            on_done=lambda rs: done.set())
        d.run()
        assert done.wait(20), "关闭分页 + 批量下发超时"
        assert len(results) == 2, len(results)
        first = results[0]
        assert "关闭分页" in first.command, first.command
        assert "screen-length 0 temporary" in first.command, first.command
        assert first.ok, first.error
        assert "screen-length 0 temporary\r" in tab.sent, tab.sent[:3]
        assert (tab.sent.index("screen-length 0 temporary\r")
                < tab.sent.index("display version\r")), tab.sent[:4]
        assert results[1].command == "display version", results[1].command
        assert results[1].ok, results[1].error
        assert d.total == 2, d.total

    # ---------------- 测试报告 BUG-01～BUG-14 回归（v1.4.20） ----------------
    def _bare_store(tmp):
        from .sessions import SessionStore
        st = SessionStore.__new__(SessionStore)
        st.file = os.path.join(tmp, "sessions.json")
        st.sessions = []
        st.folders = []
        st._lock = threading.RLock()
        st.on_save_error = None
        return st

    def t_bug01_crt_port():
        """SecureCRT 导入端口：Port Number / Port 两种键名，十进制与 DWORD 十六进制都认。"""
        import shutil
        import tempfile
        from .sessions import _parse_crt_port
        assert _parse_crt_port("22") == 22, _parse_crt_port("22")
        assert _parse_crt_port("2222") == 2222
        assert _parse_crt_port("00000016") == 22, "SecureCRT DWORD 00000016 应为 22"
        assert _parse_crt_port("000006C0") == 1728
        assert _parse_crt_port("") == 0 and _parse_crt_port("abc") == 0
        tmp = tempfile.mkdtemp(prefix="termx_crt_")
        try:
            st = _bare_store(tmp)
            def put(name, body):
                p = os.path.join(tmp, name)
                with open(p, "w", encoding="utf-8") as f:
                    f.write(body)
                return p
            p1 = put("a.ini", 'S:"Protocol Name"=SSH2\nD:"Port Number"=00000016\n'
                              'S:"Hostname"=10.0.0.1\nS:"Username"=admin\n')
            assert st.import_crt_ini(p1, "导入") == 1
            assert st.sessions[0]["port"] == 22, st.sessions[0]["port"]
            p2 = put("b.ini", 'S:"Protocol Name"=SSH2\nS:"Port"=22\nS:"Hostname"=10.0.0.2\n')
            assert st.import_crt_ini(p2, "导入") == 1
            assert st.sessions[1]["port"] == 22, "十进制 22 被当成十六进制解析"
            p3 = put("c.ini", 'S:"Protocol Name"=Telnet\nS:"Hostname"=10.0.0.3\n')
            assert st.import_crt_ini(p3, "导入") == 1
            assert st.sessions[2]["port"] == 23, st.sessions[2]["port"]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def t_bug02_local_shell_quote():
        """含空格的 shell 路径必须被引号包住，否则 CreateProcess 找不到程序。"""
        from .conpty import quote_cmdline
        from .config import Config
        from .connections import _split_cmd_args, create_connection
        assert _split_cmd_args('-NoProfile -Directory="D:\\a b"') == \
            ["-NoProfile", "-Directory=D:\\a b"], _split_cmd_args('-NoProfile -Directory="D:\\a b"')
        assert _split_cmd_args("") == []
        s = new_session("LocalShell")
        s["shell_path"] = r"C:\Program Files\PowerShell\7\pwsh.exe"
        s["shell_args"] = "-NoProfile"
        conn, info = create_connection(s, Config(), {})
        assert info.startswith('"C:\\Program Files\\PowerShell\\7\\pwsh.exe"'), info
        assert conn.cmdline == info
        assert info == quote_cmdline([s["shell_path"], "-NoProfile"]), info
        conn.stop()

    def t_bug03_remove_folder():
        """删除分组只清该分组及其子目录，不能波及“核心备份”这类同前缀分组。"""
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp(prefix="termx_dir_")
        try:
            st = _bare_store(tmp)
            st.folders = ["核心", "核心备份", "核心/子目录"]
            a = new_session("SSH2", "A")
            a["folder"] = "核心"
            b = new_session("SSH2", "B")
            b["folder"] = "核心备份"
            c = new_session("SSH2", "C")
            c["folder"] = "核心/子目录"
            d = new_session("SSH2", "D")
            d["folder"] = "生产"
            st.sessions = [a, b, c, d]
            st.remove_folder("核心")
            assert st.folders == ["核心备份"], st.folders
            assert st.sessions[0]["folder"] == "", "被删分组内的会话未移出"
            assert st.sessions[1]["folder"] == "核心备份", "同前缀分组被误清空"
            assert st.sessions[2]["folder"] == "", "子目录会话未移出"
            assert st.sessions[3]["folder"] == "生产"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def t_bug04_save_reports_error():
        """写盘失败必须返回 False、记录原因并回调界面，不能静默丢会话。"""
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp(prefix="termx_save_")
        try:
            st = _bare_store(tmp)
            st.sessions = [new_session("SSH2", "A")]
            errs = []
            st.on_save_error = errs.append
            assert st.save() is True, "正常保存应返回 True"
            assert st.last_error() == ""
            st.file = os.path.join(tmp, "no", "such", "dir", "sessions.json")
            assert st.save() is False, "目录不存在时保存却返回成功"
            assert st.last_error(), "保存失败必须记录原因"
            assert errs, "保存失败没有通知界面"
            assert "不存在" in errs[0], errs[0]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def t_bug05_faint():
        """SGR 2 是弱化，不能当粗体；SGR 22 同时清粗体与弱化。"""
        from .ansi import FAINT
        b = ScreenBuffer(10, 2, SCHEMES["Dark+"])
        b.feed(b"\x1b[2mFAINT\x1b[22mNORMAL")
        assert b.lines[0][0].attrs & FAINT, b.lines[0][0].attrs
        assert not (b.lines[0][0].attrs & BOLD), "SGR 2 仍被当成粗体"
        assert not (b.lines[0][5].attrs & (FAINT | BOLD)), b.lines[0][5].attrs
        b2 = ScreenBuffer(10, 1, SCHEMES["Dark+"])
        b2.feed(b"\x1b[1mB\x1b[22mN")
        assert b2.lines[0][0].attrs & BOLD
        assert not (b2.lines[0][1].attrs & BOLD), "SGR 22 未清除粗体"

    def t_bug06_color256_robust():
        """非法 256 色号（None/负数/超范围）回退默认前景色，不能抛异常或取负索引。"""
        scheme = SCHEMES["Dark+"]
        for bad in (None, "", "abc", -1, -100, 256, 300):
            got = color_256(bad, scheme)
            assert got == scheme["fg"], (bad, got)
        assert color_256(15, scheme) == scheme["ansi"][15]
        assert color_256("196", scheme).startswith("#")
        assert color_256(196, scheme) == "#FF0000", color_256(196, scheme)

    def t_bug07_serial_flow():
        """串口流控必须写进 DCB：none/xonxoff/hardware 三种方式位各不相同。"""
        from .connections import (apply_serial_flow, DCB_FLOW_MASK,
                                  DCB_F_OUTX, DCB_F_INX, DCB_F_OUTX_CTS_FLOW)
        base = 0xFFFFFFFF
        none = apply_serial_flow(base, "none")
        assert none & DCB_FLOW_MASK == (1 << 4) | (1 << 12), hex(none & DCB_FLOW_MASK)
        xon = apply_serial_flow(base, "xonxoff")
        assert xon & DCB_F_OUTX and xon & DCB_F_INX, hex(xon)
        assert not (xon & DCB_F_OUTX_CTS_FLOW), "软件流控不应开 CTS"
        hw = apply_serial_flow(base, "hardware")
        assert hw & DCB_F_OUTX_CTS_FLOW, "RTS/CTS 未置 fOutxCtsFlow"
        assert (hw >> 12) & 3 == 2, "RTS_CONTROL 未设为握手"
        assert not (hw & DCB_F_OUTX), "硬件流控不应开 XON/XOFF"
        assert apply_serial_flow(0, "hardware") & (1 << 4), "DTR 未使能"

    def t_bug08_proxy_error():
        """代理隧道运行期失败必须上报，且同一隧道只报一次。"""
        import socket as _socket
        from .connections import ProxyTunnel
        got = []
        t = ProxyTunnel("10.0.0.9", 22, {}, {}, on_error=got.append)
        t._error("SOCKS5 认证失败")
        assert got and "认证失败" in got[0], got
        t._error("第二次")
        assert len(got) == 1, "同一隧道重复上报：%r" % (got,)
        # 端到端：代理端口不可达时走 _serve 也要触发回调
        s = new_session("SSH2")
        s["proxy"] = "custom"
        s["proxy_type"] = "socks5"
        s["proxy_host"] = "127.0.0.1"
        s["proxy_port"] = 1
        got2 = []
        t2 = ProxyTunnel("10.0.0.9", 22, s, {}, on_error=got2.append)
        cli = _socket.socket()
        try:
            t2._serve(cli)
        finally:
            cli.close()
        assert got2, "代理连接失败时界面收不到任何错误"

    def t_bug09_conpty_handles():
        """ConPTY 启动失败的各分支必须把 4 个管道句柄全部关掉。"""
        import ctypes as _ct
        from ctypes import wintypes as _wt
        from .conpty import _close_handles, k32
        k32.GetHandleInformation.argtypes = [_wt.HANDLE, _ct.POINTER(_wt.DWORD)]
        k32.GetHandleInformation.restype = _wt.BOOL
        pairs = []
        for _ in range(2):
            r, w = _wt.HANDLE(), _wt.HANDLE()
            assert k32.CreatePipe(_ct.byref(r), _ct.byref(w), None, 0), "无法创建测试管道"
            pairs.append((r, w))
        handles = [h for pair in pairs for h in pair]
        _close_handles(*(handles + [_wt.HANDLE()]))
        flags = _wt.DWORD()
        for h in handles:
            assert not k32.GetHandleInformation(h, _ct.byref(flags)), "有句柄未关闭"

    def t_bug10_ssh1_mask():
        """SSH-1 展示用命令行必须遮住 -pw 密码；随包 plink 无 -pwfile 已实测确认。"""
        from .connections import _mask_cmdline
        argv = ["plink.exe", "-1", "-ssh", "-P", "22", "-l", "root",
                "-pw", "sec ret中文", "10.0.0.1"]
        shown = _mask_cmdline(argv)
        assert "sec ret中文" not in shown, shown
        assert "******" in shown, shown
        assert "10.0.0.1" in shown and "-pw" in shown, shown

    def t_bug11_obf_weak_protect():
        """DPAPI 不可用时的降级保护必须是机器相关混淆，且兼容旧 OBF: 数据。"""
        import base64 as _b64
        from .sessions import _obf_encode, _obf_decode
        enc = _obf_encode("密码abc123")
        assert enc.startswith("OBF2:"), enc[:10]
        assert "密码abc123" not in enc, "混淆结果仍能直接读明文"
        assert _obf_decode(enc) == "密码abc123"
        old = "OBF:" + _b64.b64encode("旧格式密码".encode("utf-8")).decode("ascii")
        assert _obf_decode(old) == "旧格式密码", "旧版 OBF: 数据无法继续读取"

    def t_bug12_anti_idle_encoding():
        """防挂起字符串必须按会话编码发送，GBK 会话不能变成 UTF-8 乱码。"""
        from .config import Config
        from .connections import RawConnection
        s = new_session("Raw")
        s["encoding"] = "gbk"
        s["anti_idle_string"] = "中文保活"
        c = RawConnection(s, Config(), {})
        assert c.encoding.upper() == "GBK", c.encoding
        c.alive = True
        c.send_anti_idle()
        data = c._q.get_nowait()
        assert data == "中文保活".encode("gbk"), data

    def t_bug13_recv_exact():
        """SOCKS5 定长读取必须循环读满，短读不能当成完整报文。"""
        import socket as _socket
        from .connections import _recv_exact
        a, b = _socket.socketpair()
        a.settimeout(8)                  # 防止测试自身挂死
        try:
            def writer():
                time.sleep(0.05)
                b.sendall(b"A")
                time.sleep(0.05)
                b.sendall(b"BCD")
            th = threading.Thread(target=writer, daemon=True)
            th.start()
            assert _recv_exact(a, 4) == b"ABCD", "短读被当成完整报文"
            th.join(2)
            b.close()
            try:
                _recv_exact(a, 2)
                raise AssertionError("对端关闭时 _recv_exact 没有报错")
            except RuntimeError:
                pass
        finally:
            try:
                a.close()
            except Exception:
                pass

    def t_bug14_expand_vars_unicode():
        """变量名放宽到中文/空格/短横线，且 ${var} 不能被替换成 $值。"""
        m = {"机房A-设备": "sw1", "name": "核心交换机", "带空格 键": "ok"}
        assert expand_vars("{机房A-设备}", m) == "sw1", expand_vars("{机房A-设备}", m)
        assert expand_vars("${name}", m) == "核心交换机", expand_vars("${name}", m)
        assert expand_vars("%name%", m) == "核心交换机"
        assert expand_vars("{带空格 键}", m) == "ok"
        assert expand_vars(" connect {机房A-设备}\r", m) == " connect sw1\r"
        assert expand_vars("{不存在}", m) == "{不存在}"
        assert expand_vars("$name", m) == "$name", "裸 $ 不应被替换"

    tests = [
        ("Python 脚本引擎 (crt API)", t_python_script),
        ("JScript 内置引擎 (零外部依赖)", t_jscript_builtin),
        ("VBScript 内置引擎 (零外部依赖)", t_vbscript_builtin),
        ("内置组件清单", t_bundle_status),
        ("隔离 PATH 下 OpenSSH 独立运行", t_bundle_isolated_ssh),
        ("内置 Zmodem 协议栈回环传输", t_zmodem_builtin),
        ("批量下发 (并发/变量/提示符/汇总)", t_batch_dispatch),
        ("批量下发·登录后关闭分页", t_batch_paging),
        ("BUG-01 SecureCRT 端口导入", t_bug01_crt_port),
        ("BUG-02 含空格 Shell 路径引号", t_bug02_local_shell_quote),
        ("BUG-03 删除分组不误伤同前缀", t_bug03_remove_folder),
        ("BUG-04 会话保存失败可见", t_bug04_save_reports_error),
        ("BUG-05 SGR 2 弱化与粗体分离", t_bug05_faint),
        ("BUG-06 256 色号非法值兜底", t_bug06_color256_robust),
        ("BUG-07 串口流控写入 DCB", t_bug07_serial_flow),
        ("BUG-08 代理隧道错误上报", t_bug08_proxy_error),
        ("BUG-09 ConPTY 失败句柄全关", t_bug09_conpty_handles),
        ("BUG-10 SSH-1 命令行密码遮罩", t_bug10_ssh1_mask),
        ("BUG-11 DPAPI 降级弱保护", t_bug11_obf_weak_protect),
        ("BUG-12 防挂起按会话编码", t_bug12_anti_idle_encoding),
        ("BUG-13 SOCKS5 短读处理", t_bug13_recv_exact),
        ("BUG-14 变量名支持中文", t_bug14_expand_vars_unicode),
        ("ANSI 基本输出与光标", t_ansi_basic),
        ("ANSI SGR 颜色/粗体", t_ansi_sgr),
        ("ANSI 光标定位/擦除", t_ansi_cursor),
        ("ANSI 回滚/备用屏幕", t_ansi_scroll_alt),
        ("UTF-8/宽字符/256色/真彩", t_ansi_utf8_and_wide),
        ("OSC 标题", t_ansi_osc),
        ("终端查询响应", t_ansi_reply),
        ("窗口尺寸变化", t_ansi_resize),
        ("全局配置与配色", t_config),
        ("界面主题注册表 (6 套)", t_theme_registry),
        ("主题终端配色表", t_theme_schemes),
        ("会话密码 DPAPI 加解密", t_session_password),
        ("全协议会话模板", t_session_defaults),
        ("变量替换", t_expand_vars),
        ("批量变量 CSV", t_var_table),
        ("输出捕获/等待提示符/超时", t_capture),
        ("登录脚本解析", t_login_script),
        ("SSH 命令行构造", t_ssh_args),
        ("日志 ANSI 剥离", t_strip_ansi),
        ("批量结果导出", t_batch_export),
        ("ConPTY 可用性", t_conpty),
        ("全部模块可导入", t_import_all),
    ]
    emit("TermX 核心自检 (%d 项)" % len(tests))
    for name, fn in tests:
        check(name, fn)
    emit("通过 %d / %d" % (len(passed), len(tests)))
    if failed:
        for f in failed:
            emit("  失败: " + f)
    return len(failed)


def smoke_test(emit=None):
    """真实界面 + ConPTY 冒烟测试：自动开关窗口并验证终端有输出。"""
    emit = emit or (lambda s: print(s))
    import tkinter as tk
    from .config import Config
    from .main_window import MainWindow
    from .sessions import new_session
    result = {"ok": False, "text": ""}
    root = tk.Tk()
    app = MainWindow(root)
    s = new_session("LocalShell", "冒烟测试")
    s["shell_path"] = "cmd.exe"
    tab = app.open_session(s)

    def finish():
        result["ok"] = ("TERMX_SMOKE_OK" in result["text"] and
                        bool(result["text"].strip()))
        emit("SMOKE connected=%s size=%dx%d conpty=%s" %
             (tab.is_connected(), tab.cols, tab.rows, tab.conn is not None))
        emit("SMOKE output: %r" % result["text"][:220])
        emit("SMOKE %s" % ("OK" if result["ok"] else "FAIL"))
        app._closing = True
        try:
            tab.disconnect()
        except Exception as e:
            emit("SMOKE disconnect: %s" % e)
        try:
            root.destroy()
        except Exception:
            pass

    def check():
        try:
            tab.send_text("echo TERMX_SMOKE_OK\r")
        except Exception as e:
            emit("SMOKE send error: %s" % e)
        root.after(1800, lambda: (result.update(text=tab.view.buffer.all_text()), finish()))

    root.after(5000, check)
    root.mainloop()
    return 0 if result["ok"] else 1


def _make_emitter(argv):
    """GUI 单文件版没有控制台，--out 可把自检/冒烟结果写入文件。"""
    path = None
    if "--out" in argv:
        i = argv.index("--out")
        if i + 1 < len(argv):
            path = argv[i + 1]
    buf = []

    def emit(line):
        if path:
            buf.append(str(line))
        else:
            try:
                print(line)
            except Exception:
                pass

    def flush():
        if path:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write("\n".join(buf))
            except Exception:
                pass
    return emit, flush


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    emit, flush = _make_emitter(argv)
    if "--version" in argv or "-v" in argv:
        from .config import APP_NAME, APP_VER, APP_BUILD
        emit("%s %s (%s)" % (APP_NAME, APP_VER, APP_BUILD))
        flush()
        return 0
    if "--selftest" in argv:
        try:
            rc = 1 if selftest(emit=emit) else 0
        except Exception:
            emit(traceback.format_exc())
            rc = 2
        flush()
        return rc
    if "--smoke" in argv:
        try:
            rc = smoke_test(emit=emit)
        except Exception:
            emit(traceback.format_exc())
            rc = 2
        flush()
        return rc
    if "--data-dir" in argv:
        from .config import data_dir
        emit(data_dir())
        flush()
        return 0
    if "--data-dir-set" in argv:
        from .config import set_configured_data_dir
        i = argv.index("--data-dir-set")
        p = argv[i + 1] if i + 1 < len(argv) else ""
        emit(set_configured_data_dir(p or None))
        flush()
        return 0
    if "--verify-bundle" in argv:
        from . import bundle
        emit("TermX 内置组件清单  根目录: %s" % bundle.root())
        all_ok = True
        for name, ok, path in bundle.status():
            all_ok &= bool(ok)
            emit("  [%s] %-34s %s" % ("OK" if ok else "缺失", name, path))
        iso = bundle.verify_isolated(emit=emit)
        emit("隔离 PATH 独立运行: %s" % ("通过" if iso else "失败"))
        emit("结论: %s" % ("全部组件随包可用，无需下载/安装" if all_ok and iso
                          else "存在缺失项，请检查 TermX_Bin 目录"))
        flush()
        return 0 if (all_ok and iso) else 1
    from .main_window import launch
    launch()
    return 0
