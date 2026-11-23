# -*- coding: utf-8 -*-
"""内置 VBScript / JScript 兼容执行器（纯 Python，零外部依赖）。

用途：替代 SecureCRT / 本软件原先依赖的 Windows WSH(cscript.exe)。把常见
CRT 自动化脚本转译成 Python 后执行，所有 crt.* 调用统一走 rpc(method, args)。

实现要点：源码先在「字符串掩码域」里做逐行转译，所有字面量用占位符保护，
最后一次性还原成 Python 字符串字面量，避免反斜杠被重复转义。

覆盖范围（SecureCRT 脚本实际使用的绝大多数语法）：
  VBScript: Sub/Function、Dim/Const、赋值、单行与块 If/ElseIf/Else、
            For/For Each/Next、Do While/Loop、While/Wend、Exit、
            注释与续行、: 多语句、& 拼接、Mod/\\/^、And/Or/Not、
            常用内置函数与 vbCr 等常量。
  JScript : var/function、if/else、for(;;)、while、break/continue/return、
            数组字面量、Math/parseInt/String 常用方法、=== / && / || 等。

限制：不是完整的 VBScript/JScript 语言实现；COM/CreateObject、With、
Class、正则字面量、原型链等高级语法不支持。需要完整语言能力时可在
全局选项把脚本引擎切回系统 WSH。
"""
import datetime
import math
import random
import re
import time

__all__ = ["BasicEngineError", "available", "run_basic"]


class BasicEngineError(Exception):
    pass


def available():
    return True


# ------------------------------------------------------------------ 字符串掩码
_VB_STR = re.compile(r'"(?:[^"]|"")*"')
_JS_STR = re.compile(r'"(?:[^"\\]|\\.|"")*"|\'(?:[^\'\\]|\\.)*\'')


def _mask_into(s, parts, js=False):
    """把字符串字面量替换成占位符，原串追加到 parts（编号跨行唯一）。"""
    rx = _JS_STR if js else _VB_STR

    def repl(m):
        parts.append(m.group(0))
        return "\x01S%d\x01" % (len(parts) - 1)

    return rx.sub(repl, s)


def _finalize(py, parts, js=False):
    """一次性把占位符还原成正确的 Python 字符串字面量。"""
    for i, raw in enumerate(parts):
        q, body = raw[0], raw[1:-1]
        if js:
            # 源码里若出现真实控制字符，转成转义序列，避免生成未终止字符串
            body = (body.replace("\r", "\\r").replace("\n", "\\n")
                        .replace("\t", "\\t"))
            val = q + body + q          # JS 转义序列与 Python 基本一致
        else:
            val = repr(body.replace('""', '"'))
        py = py.replace("\x01S%d\x01" % i, val)
    return py


# ------------------------------------------------------------------ crt 代理
class _Node(object):
    """把 crt.Screen.Send(...) 之类调用映射到 rpc；未知成员安全降级。"""

    def __init__(self, rpc, path, state):
        object.__setattr__(self, "_rpc", rpc)
        object.__setattr__(self, "_path", path)
        object.__setattr__(self, "_state", state)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        st = object.__getattribute__(self, "_state")
        if name == "MatchIndex":
            return st.get("MatchIndex", 0)
        if name in ("Rows", "CurrentRow"):
            return st.get(name, 24)
        if name in ("Columns", "CurrentColumn"):
            return st.get(name, 80)
        if name in ("Synchronous", "Connected", "Logging"):
            return st.get(name, False)
        if name == "Version":
            return "TermX 1.1.0 (内置脚本引擎)"
        return _Node(object.__getattribute__(self, "_rpc"),
                     object.__getattribute__(self, "_path") + "." + name, st)

    def __call__(self, *args):
        method = object.__getattribute__(self, "_path").rsplit(".", 1)[-1]
        st = object.__getattribute__(self, "_state")
        vals = []
        for a in args:
            if isinstance(a, (list, tuple)):
                vals.append("\x01".join(_vbs_str(x) for x in a))
            else:
                vals.append(_vbs_str(a))
        try:
            value = object.__getattribute__(self, "_rpc")(method, vals)
        except RuntimeError as e:
            if "未实现" in str(e) or "Unknown" in str(e):
                return ""
            raise
        value = "" if value is None else str(value)
        if method == "WaitForString":
            return value not in ("0", "", "False", "false", "None")
        if method == "WaitForStrings":
            try:
                idx = int(value)
            except Exception:
                idx = 0
            st["MatchIndex"] = idx
            return idx
        if method in ("GetTabCount", "Connected", "MessageBox", "ActivateTab"):
            try:
                return int(value) if value.strip().lstrip("-").isdigit() else value
            except Exception:
                return value
        return value


class _Echo(object):
    def __init__(self, emit):
        self._emit = emit

    def Echo(self, *args):
        self._emit(" ".join(_vbs_str(a) for a in args))

    def __getattr__(self, name):
        return lambda *a, **k: ""


# ------------------------------------------------------------------ 运行环境
def _vbs_str(v):
    if v is None:
        return ""
    if v is True:
        return "True"
    if v is False:
        return "False"
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return str(v)


def _environment(rpc, on_log, stop, on_print):
    state = {"MatchIndex": 0}
    env = {"__builtins__": __builtins__,
           "crt": _Node(rpc, "crt", state),
           "WScript": _Echo(on_print or (lambda s: None)),
           "True": True, "False": False, "None": None,
           "vbCr": "\r", "vbLf": "\n", "vbCrLf": "\r\n", "vbTab": "\t",
           "vbNullChar": "\0", "vbBack": "\b", "vbFormFeed": "\f",
           "vbVerticalTab": "\v", "vbYes": 6, "vbNo": 7, "vbOK": 1,
           "vbCancel": 2, "vbAbort": 3, "vbRetry": 4, "vbIgnore": 5,
           "Nothing": None, "Null": None, "Empty": "",
           "vbTrue": True, "vbFalse": False}

    def emit(s):
        if on_print:
            try:
                on_print(str(s))
            except Exception:
                pass

    def tick():
        if stop is not None and stop.is_set():
            raise BasicEngineError("脚本已被中止")
        return 1

    def rng(a, b, step=1):
        try:
            a, b, step = int(a), int(b), int(step or 1)
        except Exception:
            a, b, step = 0, 0, 1
        if step == 0:
            step = 1
        tick()
        return range(a, b + 1, step) if step > 0 else range(a, b - 1, step)

    def cat(*parts):
        return "".join(_vbs_str(p) for p in parts)

    def jsadd(*parts):
        if len(parts) == 1:
            return parts[0]
        if any(isinstance(p, str) for p in parts):
            return "".join(_vbs_str(p) for p in parts)
        out = parts[0]
        for p in parts[1:]:
            try:
                out = out + p
            except Exception:
                out = _vbs_str(out) + _vbs_str(p)
        return out

    def num(v, default=0):
        try:
            if isinstance(v, bool):
                return int(v)
            s = _vbs_str(v).strip()
            if not s:
                return default
            return int(float(s))
        except Exception:
            try:
                return float(_vbs_str(v))
            except Exception:
                return default

    def mid(s, start, length=None):
        s = _vbs_str(s)
        i = max(0, num(start) - 1)
        return s[i:] if length is None else s[i:i + max(0, num(length))]

    def instr(*a):
        if len(a) >= 3 and isinstance(a[0], int):
            start, hay, needle = num(a[0]) - 1, _vbs_str(a[1]), _vbs_str(a[2])
        else:
            start = 0
            hay = _vbs_str(a[0]) if a else ""
            needle = _vbs_str(a[1]) if len(a) > 1 else ""
        return hay.lower().find(needle.lower(), max(0, start)) + 1

    class _Math(object):
        PI = math.pi
        E = math.e

        @staticmethod
        def floor(x): return math.floor(num(x))

        @staticmethod
        def ceil(x): return math.ceil(num(x))

        @staticmethod
        def round(x): return round(num(x))

        @staticmethod
        def abs(x): return abs(num(x))

        @staticmethod
        def max(*a): return max(num(x) for x in a) if a else 0

        @staticmethod
        def min(*a): return min(num(x) for x in a) if a else 0

        @staticmethod
        def pow(a, b): return num(a) ** num(b)

        @staticmethod
        def sqrt(x): return math.sqrt(abs(num(x)))

        @staticmethod
        def random(): return random.random()

    class _JsString(object):
        @staticmethod
        def fromCharCode(*codes):
            return "".join(chr(num(c) & 0xFFFF) for c in codes)

    env.update({
        "__tick": tick, "__range": rng, "__CAT": cat, "__JSADD": jsadd,
        "__jslen": lambda x: (len(x) if hasattr(x, "__len__") else 0),
        "__push": lambda arr, *items: (arr.extend(items), len(arr))[1]
                  if isinstance(arr, list) else 0,
        "Math": _Math, "String": _JsString,
        "__CStr": _vbs_str, "__CInt": num, "__CLng": num,
        "__CBool": lambda v: bool(v),
        "__CDbl": lambda v: float(num(v, 0)), "__CSng": lambda v: float(num(v, 0)),
        "__Len": lambda s: len(_vbs_str(s)), "__Mid": mid,
        "__Left": lambda s, n: _vbs_str(s)[:max(0, num(n))],
        "__Right": lambda s, n: (_vbs_str(s)[-max(0, num(n)):] if num(n) else ""),
        "__InStr": instr,
        "__InStrRev": lambda s, n, start=-1: (
            _vbs_str(s).lower().rfind(_vbs_str(n).lower(), 0,
                                      num(start) or len(_vbs_str(s))) + 1),
        "__Trim": lambda s: _vbs_str(s).strip(),
        "__LTrim": lambda s: _vbs_str(s).lstrip(),
        "__RTrim": lambda s: _vbs_str(s).rstrip(),
        "__UCase": lambda s: _vbs_str(s).upper(),
        "__LCase": lambda s: _vbs_str(s).lower(),
        "__Replace": lambda s, a, b: _vbs_str(s).replace(_vbs_str(a), _vbs_str(b)),
        "__Split": lambda s, d=" ": _vbs_str(s).split(_vbs_str(d))
                   if _vbs_str(d) else list(_vbs_str(s)),
        "__Join": lambda a, d=" ": _vbs_str(d).join(_vbs_str(x) for x in a),
        "__Chr": lambda c: chr(num(c) & 0xFFFF),
        "__ChrW": lambda c: chr(num(c) & 0xFFFF),
        "__Asc": lambda s: ord(_vbs_str(s)[0]) if _vbs_str(s) else 0,
        "__AscW": lambda s: ord(_vbs_str(s)[0]) if _vbs_str(s) else 0,
        "__Val": lambda s: num((re.match(r"[\d.+-]+", _vbs_str(s).strip()) or
                                re.match(r"", "")).group(0) or 0),
        "__IsNumeric": lambda s: bool(re.match(r"^\s*[+-]?\d+(\.\d+)?\s*$", _vbs_str(s))),
        "__Int": lambda x: math.floor(num(x)),
        "__Fix": lambda x: math.trunc(num(x)),
        "__Abs": lambda x: abs(num(x)),
        "__Sgn": lambda x: (num(x) > 0) - (num(x) < 0),
        "__Round": lambda x, n=0: round(num(x), num(n)),
        "__Array": lambda *a: list(a),
        "__UBound": lambda a, *d: (len(a) - 1 if hasattr(a, "__len__") else -1),
        "__LBound": lambda a, *d: 0,
        "__Space": lambda n: " " * max(0, num(n)),
        "__String": lambda n, c: _vbs_str(c)[:1] * max(0, num(n)),
        "__IIf": lambda c, a, b: a if c else b,
        "__MsgBox": lambda m, *a: (emit(m), 1)[1],
        "__InputBox": lambda m, *a: (emit(m), "")[1],
        "__IsEmpty": lambda v: v in (None, ""),
        "__IsNull": lambda v: v is None,
        "__TypeName": lambda v: type(v).__name__,
        "__Hex": lambda v: format(num(v), "X"),
        "__Oct": lambda v: format(num(v), "o"),
        "__Timer": lambda: time.time() % 86400,
        "__Now": lambda: datetime.datetime.now(),
        "__Date": lambda: datetime.date.today().isoformat(),
        "__Time": lambda: datetime.datetime.now().strftime("%H:%M:%S"),
        "__Rnd": lambda *a: random.random(),
        "__FormatNumber": lambda v, *a: _vbs_str(v),
        "__CDate": lambda v: _vbs_str(v),
        "__parseInt": lambda s, *a: num(s),
        "__parseFloat": lambda s: float(num(s, 0)),
        "__isNaN": lambda v: not bool(re.match(r"^\s*[+-]?\d+(\.\d+)?\s*$", _vbs_str(v))),
    })
    return env


# ------------------------------------------------------------------ 表达式转译
VB_FUNCS = {
    "cstr": "__CStr", "cint": "__CInt", "clng": "__CLng", "cdbl": "__CDbl",
    "csng": "__CSng", "cbool": "__CBool", "len": "__Len", "mid": "__Mid",
    "left": "__Left", "right": "__Right", "instr": "__InStr",
    "instrrev": "__InStrRev", "trim": "__Trim", "ltrim": "__LTrim",
    "rtrim": "__RTrim", "ucase": "__UCase", "lcase": "__LCase",
    "replace": "__Replace", "split": "__Split", "join": "__Join",
    "chr": "__Chr", "chrw": "__ChrW", "chrb": "__Chr", "asc": "__Asc",
    "ascw": "__AscW", "ascb": "__Asc", "val": "__Val",
    "isnumeric": "__IsNumeric", "int": "__Int", "fix": "__Fix",
    "abs": "__Abs", "sgn": "__Sgn", "round": "__Round", "array": "__Array",
    "ubound": "__UBound", "lbound": "__LBound", "space": "__Space",
    "string": "__String", "iif": "__IIf", "msgbox": "__MsgBox",
    "inputbox": "__InputBox", "isempty": "__IsEmpty", "isnull": "__IsNull",
    "typename": "__TypeName", "hex": "__Hex", "oct": "__Oct",
    "timer": "__Timer", "now": "__Now", "date": "__Date", "time": "__Time",
    "rnd": "__Rnd", "formatnumber": "__FormatNumber", "cdate": "__CDate",
}


def _split_top(s, pat):
    """只在括号外层切分；字符串字面量已被掩码，不会干扰。"""
    out, buf, depth, i = [], "", 0, 0
    while i < len(s):
        c = s[i]
        if c in "([":
            depth += 1
            buf += c
            i += 1
            continue
        if c in ")]":
            depth = max(0, depth - 1)
            buf += c
            i += 1
            continue
        if depth == 0:
            m = pat.match(s, i)
            if m:
                out.append(buf)
                buf = ""
                i = m.end()
                continue
        buf += c
        i += 1
    out.append(buf)
    return out


def _lex(expr, cond, js):
    """词法级替换：运算符、常量、内置函数名（对所有层级生效）。"""
    expr = re.sub(r"(?i)\bMod\b", "%", expr)
    if not js:
        expr = expr.replace("\\", "//").replace("^", "**")
    expr = re.sub(r"(?i)\bAnd\b", "and", expr)
    expr = re.sub(r"(?i)\bOr\b", "or", expr)
    expr = re.sub(r"(?i)\bNot\b", "not", expr)
    expr = re.sub(r"(?i)\bXor\b", "!=", expr)
    expr = re.sub(r"(?i)\bIs\b", "is", expr)
    expr = expr.replace("<>", "!=")
    if js:
        expr = expr.replace("===", "==").replace("!==", "!=")
        expr = expr.replace("&&", " and ").replace("||", " or ")
        expr = re.sub(r"!(?!=)", " not ", expr)
        expr = re.sub(r"\btrue\b", "True", expr)
        expr = re.sub(r"\bfalse\b", "False", expr)
        expr = re.sub(r"\bnull\b", "None", expr)
        expr = re.sub(r"\bundefined\b", "None", expr)
        expr = re.sub(r"([\w\)\]]+)\.length\b", r"__jslen(\1)", expr)
        expr = re.sub(r"([\w\)\]]+)\.push\(", r"__push(\1, ", expr)
        expr = re.sub(r"\bnew\s+Array\b", "", expr)
        expr = re.sub(r"\bparseInt\b", "__parseInt", expr)
        expr = re.sub(r"\bparseFloat\b", "__parseFloat", expr)
        expr = re.sub(r"\bisNaN\b", "__isNaN", expr)
    elif cond:
        # VBScript 的 = 既用于赋值也用于比较；条件里转成 ==
        expr = re.sub(r"(?<![<>!=:])=(?!=)", "==", expr)
    expr = re.sub(r"(?i)\bTrue\b", "True", expr)
    expr = re.sub(r"(?i)\bFalse\b", "False", expr)
    expr = re.sub(r"(?i)\bNothing\b|\bNull\b", "None", expr)
    expr = re.sub(r"(?i)\bEmpty\b", '""', expr)
    for name, mapped in VB_FUNCS.items():
        expr = re.sub(r"(?i)\b%s(?=\s*\()" % re.escape(name), mapped, expr)
    return expr


def _struct(expr, cond, js):
    """结构级处理：函数调用参数递归 + 顶层字符串拼接。"""
    m = re.match(r"^([A-Za-z_][\w.]*)\((.*)\)$", expr, re.S)
    if m:
        head, args = m.group(1), m.group(2)
        parts = _split_top(args, re.compile(r"\s*,\s*"))
        return "%s(%s)" % (head, ", ".join(
            _struct(p, cond, js) for p in parts))
    # JS 的 + 排除 ++；VB 的 & 排除 &H 十六进制前缀
    pat = (re.compile(r"(?<!\+)\s*\+\s*(?!\+)") if js
           else re.compile(r"\s*&\s*(?![Hh])"))
    segs = _split_top(expr, pat)
    if len(segs) > 1:
        return (("__JSADD(" if js else "__CAT(") + ", ".join(
            _struct(x, cond, js) for x in segs) + ")")
    return expr


def _vb_expr(expr, cond=False, js=False):
    """输入是掩码域的表达式，输出仍是掩码域的 Python 表达式。"""
    return _struct(_lex(expr, cond, js), cond, js)


def _vb_call(stmt, js=False):
    """把 `crt.Screen.Send "x"` 这类无括号过程调用补上括号。"""
    m = re.match(r"^([A-Za-z_][\w.]*(?:\([^)]*\))?)\s+(.+)$", stmt)
    if not m:
        return _vb_expr(stmt, js=js)
    head, rest = m.group(1), m.group(2)
    if head.lower() in ("dim", "const", "sub", "function", "if", "elseif",
                        "for", "next", "do", "loop", "while", "wend",
                        "rem", "exit", "select", "case", "set", "on"):
        return _vb_expr(stmt, js=js)
    args = [a.strip() for a in rest.split(",")]
    return "%s(%s)" % (head, ", ".join(_vb_expr(a, js=js) for a in args))


def _vb_line(line, cur_func=None):
    m = re.match(r"^\s*([A-Za-z_][\w.]*(?:\([^)]*\))?)\s*=\s*(.+)$", line)
    if m:
        lhs, rhs = m.group(1), _vb_expr(m.group(2).strip())
        if cur_func and lhs == cur_func:
            lhs = "__ret"
        return "%s = %s" % (lhs, rhs)
    return _vb_call(line)


# ------------------------------------------------------------------ VBScript
def _vb_to_py(code):
    parts = []
    raw_lines = []
    pending = ""
    for ln in code.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        s = ln.rstrip()
        if s.endswith("_"):
            pending += s[:-1] + " "
            continue
        full = pending + s
        pending = ""
        masked = _mask_into(full, parts, js=False)
        cut = len(masked)
        q = masked.find("'")
        if q >= 0:
            cut = min(cut, q)
        m = re.search(r"(?i)\bREM\s", masked)
        if m:
            cut = min(cut, m.start())
        for seg in masked[:cut].split(":"):
            if seg.strip():
                raw_lines.append(seg.strip())
    if pending.strip():
        raw_lines.append(_mask_into(pending.strip(), parts, js=False))

    out = []
    indent = 0
    stack = []
    cur_func = None
    has_main = False

    def emit(line):
        out.append("    " * indent + line)

    for line in raw_lines:
        low = line.lower()
        if low in ("end sub", "end function"):
            if cur_func and stack and stack[-1] == "func":
                emit("return __ret")
            if stack:
                stack.pop()
            indent = max(0, indent - 1)
            cur_func = None
            continue
        if low in ("end if", "next", "wend", "loop") or low.startswith("loop "):
            if stack:
                stack.pop()
            indent = max(0, indent - 1)
            continue
        m = re.match(r"(?i)^(sub|function)\s+([A-Za-z_]\w*)\s*(?:\((.*)\))?\s*$", line)
        if m:
            kind, name, params = m.group(1).lower(), m.group(2), m.group(3) or ""
            plist = [p.strip().split("=")[0].strip() for p in params.split(",") if p.strip()]
            emit("def %s(%s):" % (name, ", ".join(plist)))
            indent += 1
            stack.append("func" if kind == "function" else "sub")
            if kind == "function":
                cur_func = name
                emit('__ret = ""')
            elif name.lower() == "main":
                has_main = True
            continue
        m = re.match(r"(?i)^if\s+(.+?)\s+then\s*(.*)$", line)
        if m:
            emit("if %s:" % _vb_expr(m.group(1), cond=True))
            indent += 1
            emit("__tick()")
            stack.append("if")
            tail = m.group(2).strip()
            if tail:
                emit(_vb_line(tail, cur_func))
                indent -= 1
                stack.pop()
            continue
        m = re.match(r"(?i)^elseif\s+(.+?)\s+then\s*$", line)
        if m:
            indent = max(0, indent - 1)
            emit("elif %s:" % _vb_expr(m.group(1), cond=True))
            indent += 1
            emit("__tick()")
            continue
        if low == "else":
            indent = max(0, indent - 1)
            emit("else:")
            indent += 1
            emit("__tick()")
            continue
        m = re.match(r"(?i)^for\s+each\s+(\w+)\s+in\s+(.+)$", line)
        if m:
            emit("for %s in %s:" % (m.group(1), _vb_expr(m.group(2))))
            indent += 1
            emit("__tick()")
            stack.append("for")
            continue
        m = re.match(r"(?i)^for\s+(\w+)\s*=\s*(.+?)\s+to\s+(.+?)(?:\s+step\s+(.+))?$", line)
        if m:
            emit("for %s in __range(%s, %s, %s):" % (
                m.group(1), _vb_expr(m.group(2)), _vb_expr(m.group(3)),
                _vb_expr(m.group(4) or "1")))
            indent += 1
            emit("__tick()")
            stack.append("for")
            continue
        m = re.match(r"(?i)^do\s+while\s+(.+)$", line)
        if m:
            emit("while %s:" % _vb_expr(m.group(1), cond=True))
            indent += 1
            emit("__tick()")
            stack.append("while")
            continue
        m = re.match(r"(?i)^do\s+until\s+(.+)$", line)
        if m:
            emit("while not (%s):" % _vb_expr(m.group(1), cond=True))
            indent += 1
            emit("__tick()")
            stack.append("while")
            continue
        if low == "do":
            emit("while True:")
            indent += 1
            emit("__tick()")
            stack.append("while")
            continue
        m = re.match(r"(?i)^while\s+(.+)$", line)
        if m:
            emit("while %s:" % _vb_expr(m.group(1), cond=True))
            indent += 1
            emit("__tick()")
            stack.append("while")
            continue
        if low.startswith("exit for") or low.startswith("exit do"):
            emit("break")
            continue
        if low.startswith("exit sub") or low.startswith("exit function"):
            emit("return __ret" if cur_func else "return")
            continue
        if low.startswith("dim "):
            for v in re.split(r"[\s,]+", line[4:]):
                v = v.strip().rstrip("()")
                if re.match(r"^[A-Za-z_]\w*$", v):
                    emit("%s = 0" % v)
            continue
        m = re.match(r"(?i)^const\s+([A-Za-z_]\w*)\s*=\s*(.+)$", line)
        if m:
            emit("%s = %s" % (m.group(1), _vb_expr(m.group(2))))
            continue
        emit(_vb_line(line, cur_func))
    if has_main:
        out.append('if "Main" in globals(): Main()')
    return _finalize("\n".join(out), parts, js=False)


# ------------------------------------------------------------------ JScript
def _js_split(code):
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    lines, buf, i, quote, depth = [], "", 0, None, 0
    while i < len(code):
        c = code[i]
        if quote:
            buf += c
            if c == "\\" and i + 1 < len(code):
                buf += code[i + 1]
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "\"'":
            quote = c
            buf += c
            i += 1
            continue
        if c == "/" and i + 1 < len(code) and code[i + 1] == "/":
            while i < len(code) and code[i] not in "\r\n":
                i += 1
            continue
        if c in "\r\n":
            if buf and not buf.endswith(" "):
                buf += " "
            i += 1
            continue
        if c == "(":
            depth += 1
            buf += c
            i += 1
            continue
        if c == ")":
            depth = max(0, depth - 1)
            buf += c
            i += 1
            continue
        if c == ";" and depth == 0:
            if buf.strip():
                lines.append(buf.strip())
            buf = ""
            i += 1
            continue
        if c in "{}" and depth == 0:
            if buf.strip():
                lines.append(buf.strip())
            lines.append(c)
            buf = ""
            i += 1
            continue
        buf += c
        i += 1
    if buf.strip():
        lines.append(buf.strip())
    return [l for l in lines if l]


def _js_stmt(stmt, parts):
    s = _mask_into(stmt.strip(), parts, js=True)
    s = re.sub(r"^var\s+", "", s)
    m = re.match(r"^([A-Za-z_$][\w.$]*)(\+\+|--)$", s)
    if m:
        return "%s %s= 1" % (m.group(1).replace("$", "_"),
                             "+" if m.group(2) == "++" else "-")
    if s.startswith("return "):
        return "return " + _vb_expr(s[7:], js=True)
    if s in ("break", "continue"):
        return s
    m = re.match(r"^([A-Za-z_$][\w.$]*)\s*=\s*(.+)$", s)
    if m:
        return "%s = %s" % (m.group(1).replace("$", "_"),
                            _vb_expr(m.group(2).strip(), js=True))
    return _vb_expr(s, js=True)


def _js_to_py(code):
    parts = []
    lines = []
    for raw in _js_split(code):
        lines.append(_mask_into(raw, parts, js=True) if raw not in ("{", "}") else raw)
    out = []
    indent = 0
    stack = []
    pending = None

    def emit(line):
        out.append("    " * indent + line)

    for line in lines:
        if line == "{":
            if pending:
                header, kind, inc = pending
                emit(header)
                indent += 1
                emit("__tick()")
                stack.append((kind, inc))
                pending = None
            else:
                indent += 1
                stack.append(("block", None))
            continue
        if line == "}":
            kind, inc = stack.pop() if stack else ("block", None)
            if inc:
                emit(inc)
            indent = max(0, indent - 1)
            continue
        m = re.match(r"^function\s+([A-Za-z_$]\w*)\s*\((.*)\)\s*$", line)
        if m:
            args = ", ".join(a.strip().replace("$", "_")
                             for a in m.group(2).split(",") if a.strip())
            pending = ("def %s(%s):" % (m.group(1), args), "func", None)
            continue
        m = re.match(r"^for\s*\((.*);(.*);(.*)\)\s*$", line)
        if m:
            init, cond, inc = m.groups()
            if init.strip():
                emit(_js_stmt(init, parts))
            pending = ("while %s:" % _vb_expr(cond.strip(), cond=True, js=True),
                       "for", _js_stmt(inc, parts))
            continue
        m = re.match(r"^while\s*\((.*)\)\s*$", line)
        if m:
            pending = ("while %s:" % _vb_expr(m.group(1), cond=True, js=True),
                       "while", None)
            continue
        m = re.match(r"^if\s*\((.*)\)\s*$", line)
        if m:
            pending = ("if %s:" % _vb_expr(m.group(1), cond=True, js=True),
                       "if", None)
            continue
        if line.lower() == "else":
            pending = ("else:", "else", None)
            continue
        m = re.match(r"^if\s*\((.*)\)\s*(\S.*)$", line)
        if m:
            emit("if %s:" % _vb_expr(m.group(1), cond=True, js=True))
            indent += 1
            emit(_js_stmt(m.group(2), parts))
            indent -= 1
            continue
        if pending:
            header, kind, inc = pending
            emit(header)
            indent += 1
            emit("__tick()")
            stack.append((kind, inc))
            pending = None
            emit(_js_stmt(line, parts))
            continue
        emit(_js_stmt(line, parts))
    while stack and indent > 0:
        _kind, inc = stack.pop()
        if inc:
            emit(inc)
        indent -= 1
    return _finalize("\n".join(out), parts, js=True)


# ------------------------------------------------------------------ 入口
def run_basic(code, lang, rpc, on_log=None, stop=None, on_print=None):
    """执行 VBScript / JScript 源码；错误统一抛 BasicEngineError。"""
    if not callable(rpc):
        raise BasicEngineError("rpc 回调不可用")
    is_js = str(lang).lower() in ("jscript", "js", "javascript")
    try:
        py = _js_to_py(code) if is_js else _vb_to_py(code)
    except Exception as e:
        raise BasicEngineError("脚本转译失败: %s" % e)
    env = _environment(rpc, on_log, stop, on_print)
    try:
        exec(compile(py, "<TermX-%s>" % ("JScript" if is_js else "VBScript"),
                     "exec"), env)
    except SystemExit:
        raise
    except BasicEngineError:
        raise
    except Exception as e:
        raise BasicEngineError("%s: %s\n转译结果:\n%s" %
                               (type(e).__name__, e, py[:1200]))
    return None
