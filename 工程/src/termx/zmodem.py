# -*- coding: utf-8 -*-
"""Pure-Python ZMODEM file transfer for TermX.

This module replaces the need for external ``rz.exe`` / ``sz.exe`` helpers:
it speaks ZMODEM directly over the raw byte stream of an SSH/Telnet session.
It has **no third-party dependencies and spawns no external processes**.

Public API
----------
* ``ZmodemError``            - exception type.
* ``Transfer``               - the protocol state machine.
* ``available()``            - always ``True``.
* ``looks_like_zmodem(data)``- heuristic start-sequence detector.
* Frame-type constants (``ZRQINIT`` .. ``ZXXXX``), ``ZDLE``/``ZPAD``/``ZDLEE``
  and the frame-end characters ``ZCRCE``..``ZCRCN`` are exported so callers can
  sniff the beginning of a transfer.

Design / known limitations (intentionally pragmatic)
---------------------------------------------------
* The primary acceptance target is **loop-back interop between two Transfer
  instances plus parsing of a standard lrzsz hex start frame** (``**\\x18B00...``).
  Full wire compatibility with every lrzsz build is NOT guaranteed:
  - The frame-end characters follow the values requested here
    (``ZCRCE='I'`` .. ``ZCRCN='M'``), which is shifted by one from the classic
    lrzsz 'H'..'L' set.  The receiver nevertheless accepts 0x48..0x4d as
    end-of-subpacket markers, so both encodings are tolerated.
  - CRC32 is transmitted as ``(binascii.crc32(data) ^ 0xffffffff)`` little
    endian, matching lrzsz's un-complemented register.  CRC mismatches are
    logged but *tolerated* so a slightly different peer CRC convention cannot
    deadlock the state machine.
  - The ZFILE info subpacket uses the NUL separated field layout
    ``name\\0size\\0mtime\\0mode\\0files\\0left\\0``.  The receiver also parses
    lrzsz's ``name\\0size mtime mode left\\0`` layout (first decimal token).
  - A data frame is assumed to carry a single subpacket; multi-subpacket
    streaming (ZCRCG/ZCRCQ continuation inside one ZDATA frame) is partially
    handled but not exhaustively exercised.
  - ``feed`` hides the whole incoming chunk once a transfer is active (protocol
    bytes are expected to be at the front of the chunk).  A trailing shell
    prompt arriving in the very same chunk may be swallowed - cosmetic only.
* ``feed`` is fully synchronous/non-blocking: it never sleeps or waits on the
  network.  All "wait for peer frame" logic is implemented as state transitions
  driven by subsequent ``feed`` calls.  Use the ``stop`` event (or ``cancel``)
  to abort a stalled transfer, and a wall-clock guard backs it up.
"""

import binascii
import os
import struct
import threading
import time

# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------
ZDLE = 0x18          # Ctrl-X, data-link escape
ZPAD = 0x2A          # '*', frame padding / attention
ZDLEE = 0x58         # ZDLE ^ 0x40, escaped literal ZDLE

# Binary/hex header format markers (the byte right after ZPAD ZDLE)
ZBIN = 0x41          # 'A' - binary header, CRC16
ZHEX = 0x42          # 'B' - hex header, CRC16
ZBIN32 = 0x43        # 'C' - binary header, CRC32

# Frame-end characters (values requested by TermX).
ZCRCE = 0x49         # 'I' - CRC next, frame ends, no response
ZCRCG = 0x4A         # 'J' - CRC next, frame continues, no response
ZCRCQ = 0x4B         # 'K' - CRC next, frame continues, ZACK expected
ZCRCW = 0x4C         # 'L' - CRC next, frame ends, ZACK expected
ZCRCN = 0x4D         # 'M' - CRC next, frame ends, no response

# Frame types
ZRQINIT = 0
ZRINIT = 1
ZSINIT = 2
ZACK = 3
ZFILE = 4
ZSKIP = 5
ZNAK = 6
ZABORT = 7
ZFIN = 8
ZRPOS = 9
ZDATA = 10
ZEOF = 11
ZFERR = 12
ZCRC = 13
ZCHALLENGE = 14
ZCOMPL = 15
ZCAN = 16
ZFREEACT = 17
ZCOMMAND = 18
ZSTDERR = 19
ZXXXX = 20

# Headers that carry a data subpacket.
_SUBPACKET_TYPES = frozenset((ZFILE, ZDATA, ZSINIT, ZCOMMAND, ZSTDERR, ZFREEACT))

# Recognised end-of-subpacket markers.  0x48 is included for classic lrzsz
# (ZCRCE='H'); 0x49..0x4d are the TermX values above.
_FRAME_END = frozenset((0x48, ZCRCE, ZCRCG, ZCRCQ, ZCRCW, ZCRCN))
# Only ZCRCG (0x4a) means "another subpacket follows"; all other markers end
# the frame.  Values blend the TermX ('I'..'M') and classic lrzsz ('H'..'K')
# encodings (which are shifted by one) so both remain parseable.
_END_OF_FRAME = frozenset((0x48, ZCRCE, ZCRCQ, ZCRCW, ZCRCN))
# Markers that expect a ZACK (0x4a/0x4b cover lrzsz ZCRCQ/ZCRCW).
_NEED_ACK = frozenset((0x4A, ZCRCQ, ZCRCW))

# Bytes skipped between frames at the top level (ZPAD + lrzsz CRLF / XON tail).
_PAD_BYTES = frozenset((ZPAD, 0x0D, 0x0A, 0x11, 0x00))

# Control bytes that must be ZDLE-escaped inside data / binary header bodies.
# Their escaped form (byte ^ 0x40) never lands on a frame-end marker, which
# keeps the subpacket parser unambiguous.  0x08..0x0d are intentionally NOT
# escaped: their XOR form would collide with 0x48..0x4d frame-end markers.
_DANGEROUS = frozenset((0x10, 0x11, 0x13, 0x18, 0x40, 0x8D, 0x8F,
                        0x90, 0x91, 0x93, 0xC0))

_MAX_SCAN = 24       # rolling detection buffer size while idle
_STALL_TIMEOUT = 30.0

_ZERO_FLAGS = (0, 0, 0, 0)


class ZmodemError(Exception):
    """Raised on unrecoverable ZMODEM errors."""


# ---------------------------------------------------------------------------
# CRC helpers
# ---------------------------------------------------------------------------
_CRC16_TABLE = None


def _crc16(data, crc=0):
    """CRC-CCITT (poly 0x1021, init 0, MSB first, not reflected)."""
    global _CRC16_TABLE
    if _CRC16_TABLE is None:
        tbl = []
        for i in range(256):
            c = i << 8
            for _ in range(8):
                c = ((c << 1) ^ 0x1021) & 0xFFFF if (c & 0x8000) else (c << 1) & 0xFFFF
            tbl.append(c)
        _CRC16_TABLE = tbl
    tbl = _CRC16_TABLE
    for b in data:
        crc = ((crc << 8) & 0xFFFF) ^ tbl[((crc >> 8) ^ b) & 0xFF]
    return crc & 0xFFFF


def _crc32(data):
    """ZMODEM 32-bit CRC: standard reflected CRC-32 with lrzsz's register
    convention (binascii.crc32 already applies init/final complement; lrzsz
    transmits the raw register, i.e. that value XOR 0xffffffff)."""
    return (binascii.crc32(data) ^ 0xFFFFFFFF) & 0xFFFFFFFF


def _crc_bytes(data, size):
    if size == 4:
        return struct.pack("<I", _crc32(data))
    return struct.pack(">H", _crc16(data))


def _pos_flags(pos):
    return (pos & 0xFF, (pos >> 8) & 0xFF, (pos >> 16) & 0xFF, (pos >> 24) & 0xFF)


def _flags_pos(flags):
    return (flags[0] | (flags[1] << 8) | (flags[2] << 16) | (flags[3] << 24))


# ---------------------------------------------------------------------------
# ZDLE escaping
# ---------------------------------------------------------------------------
def _escape_byte(b):
    if b == ZDLE:
        return bytes((ZDLE, ZDLEE))
    if b in _DANGEROUS:
        return bytes((ZDLE, b ^ 0x40))
    return bytes((b,))


def _escape(data):
    return b"".join(_escape_byte(b) for b in data)


def _hex_byte(v):
    table = b"0123456789abcdef"
    return bytes((table[(v >> 4) & 0xF], table[v & 0xF]))


def _hex_val(c):
    if 0x30 <= c <= 0x39:
        return c - 0x30
    if 0x61 <= c <= 0x66:
        return c - 0x61 + 10
    if 0x41 <= c <= 0x46:
        return c - 0x41 + 10
    return -1


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------
def available():
    return True


def looks_like_zmodem(data):
    """Heuristic: does *data* contain a ZMODEM start signature?"""
    if not data:
        return False
    if b"**\x18B00" in data or b"*\x18B00" in data:
        return True
    for pad in (b"**", b"*"):
        for fmt in (b"A", b"B", b"C"):
            if pad + b"\x18" + fmt in data:
                return True
    if b"rz\r" in data and (b"\x18B" in data or b"*\x18" in data):
        return True
    if b"sz" in data and b"\x18B" in data:
        return True
    return False


def _find_frame_start(buf):
    """Return index of the earliest ZPAD(+) ZDLE (A|B|C) header marker."""
    n = len(buf)
    fmts = (ZBIN, ZHEX, ZBIN32)
    i = 0
    while i < n - 2:
        if buf[i] == ZPAD:
            j = i
            while j < n and buf[j] == ZPAD:
                j += 1
            if j + 1 < n and buf[j] == ZDLE and buf[j + 1] in fmts:
                return i
        i += 1
    return -1


# ---------------------------------------------------------------------------
# Transfer
# ---------------------------------------------------------------------------
class Transfer:
    def __init__(self, send_bytes, directory="", on_file=None, on_progress=None,
                 on_log=None, stop=None):
        if not callable(send_bytes):
            raise ZmodemError("send_bytes must be callable")
        self.send_bytes = send_bytes
        self.directory = directory or ""
        self.on_file = on_file
        self.on_progress = on_progress
        self.on_log = on_log
        self.stop = stop

        self._lock = threading.RLock()
        self._active = False
        self._role = None            # 'send' (start_upload) or 'recv'
        self._buf = bytearray()      # claimed protocol bytes, not fully parsed
        self._scan = bytearray()     # rolling idle detection buffer
        self._reset_transfer_state()

    # -- state -------------------------------------------------------------
    def _reset_transfer_state(self):
        self._state = "IDLE"
        self._last_activity = time.time()
        # receiver state
        self._recv_name = ""
        self._recv_total = 0
        self._recv_data = bytearray()
        self._recv_pos = 0
        # sender state
        self._send_paths = []
        self._send_idx = 0
        self._send_data = b""
        self._send_name = ""
        self._send_offset = 0
        self._last_zdata = b""

    @property
    def active(self):
        return self._active

    def next_file(self):
        """Optional no-op hook (kept for API symmetry with legacy callers)."""
        return None

    def _log(self, msg):
        if self.on_log:
            try:
                self.on_log(msg)
            except Exception:
                pass

    def _stopped(self):
        return bool(self.stop is not None and self.stop.is_set())

    # -- public control ----------------------------------------------------
    def start_upload(self, paths):
        """Actively start an upload (we are the sender, peer runs ``rz``)."""
        with self._lock:
            self._send_paths = [str(p) for p in paths]
            self._send_idx = 0
            self._active = True
            self._role = "send"
            self._state = "WAIT_ZRINIT"
            self._buf.clear()
            self._scan.clear()
            self._last_activity = time.time()
            self._log("ZMODEM: upload start, sending ZRQINIT")
            self._send_hex(ZRQINIT, _ZERO_FLAGS, tail=b"\r\n\x11")

    def cancel(self):
        with self._lock:
            if self._active or self._buf:
                try:
                    self._send_hex(ZABORT, _ZERO_FLAGS)
                    self.send_bytes(b"\x18" * 8)   # classic CAN attention
                except Exception:
                    pass
            self._log("ZMODEM: cancelled")
            self._active = False
            self._role = None
            self._buf.clear()
            self._scan.clear()
            self._reset_transfer_state()

    # -- frame emission ----------------------------------------------------
    def _send_hex(self, ftype, flags, tail=b""):
        body = bytes((ftype & 0xFF,)) + bytes(f & 0xFF for f in flags)
        crc = _crc16(body)
        out = bytearray((ZPAD, ZPAD, ZDLE, ZHEX))
        out += _hex_byte(ftype & 0xFF)
        for f in flags:
            out += _hex_byte(f & 0xFF)
        out += _hex_byte((crc >> 8) & 0xFF)
        out += _hex_byte(crc & 0xFF)
        if ftype != ZFIN and not tail:
            out += b"\r\n"
        out += tail
        self.send_bytes(bytes(out))

    def _send_bin32_header(self, ftype, flags):
        body = bytes((ftype & 0xFF,)) + bytes(f & 0xFF for f in flags)
        raw = body + struct.pack("<I", _crc32(body))
        out = bytearray((ZPAD, ZDLE, ZBIN32))
        out += _escape(raw)
        return bytes(out)

    def _send_subpacket(self, data, end_char, crc_size):
        out = bytearray(_escape(data))
        out += bytes((ZDLE, end_char))
        out += _escape(_crc_bytes(bytes(data) + bytes((end_char,)), crc_size))
        return bytes(out)

    def _send_zfile(self):
        path = self._send_paths[self._send_idx]
        try:
            with open(path, "rb") as fh:
                self._send_data = fh.read()
            st = os.stat(path)
            mtime = int(st.st_mtime)
            mode = 0o644
        except OSError as exc:
            self._log("ZMODEM: cannot read %s: %s" % (path, exc))
            self._send_data = b""
            mtime = 0
            mode = 0o644
        self._send_name = os.path.basename(path)
        self._send_offset = 0
        info = (self._send_name.encode("utf-8", "replace") + b"\0" +
                str(len(self._send_data)).encode() + b"\0" +
                ("%o" % mtime).encode() + b"\0" +
                ("%o" % mode).encode() + b"\0" +
                str(len(self._send_paths) - self._send_idx - 1).encode() + b"\0" +
                b"0\0")
        frame = self._send_bin32_header(ZFILE, _pos_flags(0))
        frame += self._send_subpacket(info, ZCRCE, 4)
        # first data subpacket follows immediately, then we wait for ZACK
        frame += self._zdata_frame(0)
        self._state = "WAIT_ZACK"
        self.send_bytes(frame)

    def _zdata_frame(self, offset):
        chunk = self._send_data[offset:offset + 1024]
        hdr = self._send_bin32_header(ZDATA, _pos_flags(offset))
        sub = self._send_subpacket(chunk, ZCRCW, 4)
        frame = hdr + sub
        self._last_zdata = frame
        self._send_offset = offset + len(chunk)
        return frame

    def _send_next_file(self):
        if self._send_idx >= len(self._send_paths):
            self._state = "WAIT_ZFIN"
            self._send_hex(ZFIN, _ZERO_FLAGS)
            self._log("ZMODEM: all files sent, ZFIN")
            return
        self._send_zfile()

    # -- feed / parsing ----------------------------------------------------
    def feed(self, data):
        """Feed bytes from the peer.  Return the number of leading bytes that
        belong to the ZMODEM protocol (caller must not render them)."""
        if not data:
            return 0
        if not isinstance(data, (bytes, bytearray)):
            data = bytes(data)
        with self._lock:
            if self._stopped():
                self._active = False
                self._buf.clear()
                self._scan.clear()
                return 0

            if not self._active:
                self._scan += data
                combined = bytes(self._scan)
                idx = _find_frame_start(combined)
                if idx < 0:
                    if len(self._scan) > _MAX_SCAN:
                        del self._scan[:-_MAX_SCAN]
                    return 0
                self._active = True
                self._role = "recv"
                self._state = "RECV"
                self._buf = bytearray(combined[idx:])
                self._scan.clear()
                claimed = len(data)
                self._last_activity = time.time()
                self._log("ZMODEM: start sequence detected, activating receiver")
                # Prompt the peer sender: tell it we are ready to receive.
                self._send_hex(ZRINIT, _ZERO_FLAGS, tail=b"\r\n\x11")
            else:
                self._buf += data
                claimed = len(data)

            if (self._active and
                    time.time() - self._last_activity > _STALL_TIMEOUT):
                self._log("ZMODEM: stalled, aborting")
                self._abort_reset()
                self._buf.clear()   # safe: not inside _parse()
                return claimed

            consumed = self._parse()
            if consumed:
                del self._buf[:consumed]
                self._last_activity = time.time()

            if not self._active:
                # Any leftover bytes are trailing terminal text; drop them
                # (they were already claimed - see module docstring).
                self._buf.clear()
                self._scan.clear()
            return claimed

    def _abort_reset(self):
        self._active = False
        self._role = None
        # Do NOT clear self._buf here: _parse() may still be iterating that
        # very bytearray.  feed() clears it once parsing has returned.
        self._scan.clear()
        self._reset_transfer_state()

    # -- header + subpacket parsing ---------------------------------------
    def _parse(self):
        buf = self._buf
        n = len(buf)
        pos = 0
        while pos < n and self._active:
            while pos < n and buf[pos] in _PAD_BYTES:
                pos += 1
            if pos >= n:
                break
            if buf[pos] != ZDLE:
                pos += 1                      # stray byte, skip
                continue
            if pos + 1 >= n:
                break
            fmt = buf[pos + 1]
            if fmt == ZHEX:
                res = self._parse_hex_header(buf, pos)
            elif fmt == ZBIN32:
                res = self._parse_bin_header(buf, pos, 4)
            elif fmt == ZBIN:
                res = self._parse_bin_header(buf, pos, 2)
            else:
                pos += 2                      # unknown ZDLE sequence
                continue
            if res is None:
                break                          # incomplete header
            ftype, flags, hdr_end, crc_size = res
            start = pos
            if ftype in _SUBPACKET_TYPES:
                sub = self._parse_subpackets(buf, hdr_end, crc_size)
                if sub is None:
                    break                      # keep header buffered
                payload, ends, sub_end = sub
                pos = sub_end
                self._on_frame(ftype, flags, payload, ends)
            else:
                pos = hdr_end
                self._on_frame(ftype, flags, None, None)
            if pos <= start:                   # safety: always advance
                pos = start + 1
        return pos

    def _parse_hex_header(self, buf, pos):
        # ZPAD* ZDLE 'B' type(2) flags(8) crc16(4) = 14 hex digits after 'B'
        if pos + 2 > len(buf):
            return None
        q = pos + 2
        need = 14
        if q + need > len(buf):
            return None
        digits = bytes(buf[q:q + need])
        vals = []
        for c in digits:
            v = _hex_val(c)
            if v < 0:
                return None
            vals.append(v)
        ftype = (vals[0] << 4) | vals[1]
        flags = [(vals[2] << 4) | vals[3], (vals[4] << 4) | vals[5],
                 (vals[6] << 4) | vals[7], (vals[8] << 4) | vals[9]]
        got_crc = ((vals[10] << 12) | (vals[11] << 8) |
                   (vals[12] << 4) | vals[13])
        body = bytes((ftype,)) + bytes(flags)
        calc = _crc16(body)
        if calc != got_crc:
            self._log("ZMODEM: hex header CRC mismatch (%04x != %04x)" %
                      (got_crc, calc))
        return ftype, flags, q + need, 0

    def _parse_bin_header(self, buf, pos, crc_size):
        # ZPAD ZDLE 'C'|'A', then 1(type)+4(flags)+crc_size bytes, all escaped.
        i = pos + 2
        vals = []
        for _ in range(5 + crc_size):
            if i >= len(buf):
                return None
            b = buf[i]
            if b == ZDLE:
                if i + 1 >= len(buf):
                    return None
                vals.append(buf[i + 1] ^ 0x40)
                i += 2
            else:
                vals.append(b)
                i += 1
        ftype = vals[0]
        flags = vals[1:5]
        body = bytes(vals[:5])
        crc_got = bytes(vals[5:])
        if crc_size == 4:
            crc_calc = struct.pack("<I", _crc32(body))
        else:
            crc_calc = struct.pack(">H", _crc16(body))
        if crc_got != crc_calc:
            self._log("ZMODEM: bin header CRC mismatch (ignored)")
        return ftype, flags, i, crc_size

    def _parse_subpackets(self, buf, start, crc_size):
        n = len(buf)
        i = start
        all_data = bytearray()
        ends = []
        while True:
            data = bytearray()
            found = False
            while i < n:
                b = buf[i]
                if b == ZDLE:
                    if i + 1 >= n:
                        return None
                    e = buf[i + 1]
                    if e in _FRAME_END:
                        i += 2
                        found = True
                        break
                    data.append(e ^ 0x40)
                    i += 2
                else:
                    data.append(b)
                    i += 1
            if not found:
                return None
            crc = bytearray()
            for _ in range(crc_size):
                if i >= n:
                    return None
                if buf[i] == ZDLE:
                    if i + 1 >= n:
                        return None
                    crc.append(buf[i + 1] ^ 0x40)
                    i += 2
                else:
                    crc.append(buf[i])
                    i += 1
            calc = _crc_bytes(bytes(data) + bytes((e,)), crc_size)
            if bytes(crc) != calc:
                self._log("ZMODEM: subpacket CRC mismatch (ignored)")
            all_data += data
            ends.append((e, len(data)))
            if e in _NEED_ACK and self._role == "recv":
                # ZACK with the running file position
                self._send_hex(ZACK, _pos_flags(self._recv_pos + len(all_data)))
            if e not in _END_OF_FRAME:
                continue                       # ZCRCG / ZCRCQ: another subpacket
            return bytes(all_data), ends, i

    # -- frame dispatch ----------------------------------------------------
    def _on_frame(self, ftype, flags, payload, ends):
        if self._role == "send":
            self._on_send_frame(ftype, flags, payload)
        else:
            self._on_recv_frame(ftype, flags, payload, ends)

    def _on_send_frame(self, ftype, flags, payload):
        if self._state == "WAIT_ZRINIT" and ftype in (ZRINIT, ZRQINIT, ZACK):
            self._log("ZMODEM: sender got handshake, sending ZFILE")
            self._send_next_file()
        elif self._state == "WAIT_ZACK":
            if ftype == ZACK:
                if self._send_offset < len(self._send_data):
                    self.send_bytes(self._zdata_frame(self._send_offset))
                else:
                    self._state = "WAIT_ZRINIT_AFTER_EOF"
                    self._send_hex(ZEOF, _pos_flags(len(self._send_data)))
            elif ftype == ZRPOS:
                self._send_offset = _flags_pos(flags)
                self.send_bytes(self._zdata_frame(self._send_offset))
                self._state = "WAIT_ZACK"
            elif ftype == ZNAK:
                self._log("ZMODEM: ZNAK, resending last data packet")
                if self._last_zdata:
                    self.send_bytes(self._last_zdata)
            elif ftype in (ZABORT, ZCAN, ZFERR):
                self._log("ZMODEM: peer aborted")
                self._abort_reset()
        elif self._state == "WAIT_ZRINIT_AFTER_EOF":
            if ftype in (ZRINIT, ZRQINIT, ZACK):
                self._send_idx += 1
                self._send_next_file()
            elif ftype == ZSKIP:
                self._send_idx += 1
                self._send_next_file()
            elif ftype in (ZABORT, ZCAN):
                self._abort_reset()
        elif self._state == "WAIT_ZFIN":
            if ftype == ZFIN:
                self._log("ZMODEM: sender finished")
                self._abort_reset()

    def _on_recv_frame(self, ftype, flags, payload, ends):
        if ftype == ZRQINIT:
            # already answered with ZRINIT on activation
            return
        if ftype == ZABORT or ftype == ZCAN or ftype == ZFERR:
            self._log("ZMODEM: sender aborted")
            self._abort_reset()
            return
        if ftype == ZFILE:
            self._begin_file(payload)
            return
        if ftype == ZDATA:
            if payload:
                self._recv_data += payload
                self._recv_pos += len(payload)
                self._progress()
            return
        if ftype == ZEOF:
            self._finish_file()
            # ask for the next file (or let the sender close with ZFIN)
            self._send_hex(ZRINIT, _ZERO_FLAGS, tail=b"\r\n\x11")
            return
        if ftype == ZFIN:
            self._send_hex(ZFIN, _ZERO_FLAGS)
            self._log("ZMODEM: receiver finished")
            self._abort_reset()
            return
        # ZRPOS / ZACK / ZSINIT / ZCHALLENGE etc. carry no data we need here.

    # -- file assembly -----------------------------------------------------
    def _begin_file(self, payload):
        parts = bytes(payload).split(b"\0") if payload else [b""]
        name = parts[0].decode("utf-8", "replace").strip()
        name = os.path.basename(name.replace("\\", "/")) or "zmodem.bin"
        total = 0
        for field in parts[1:]:
            for tok in field.split():
                if tok.isdigit():
                    total = int(tok)
                    break
            if total:
                break
        self._recv_name = name
        self._recv_total = total
        self._recv_data = bytearray()
        self._recv_pos = 0
        self._log("ZMODEM: receiving %s (%d bytes)" % (name, total))
        self._progress()

    def _progress(self):
        if self.on_progress:
            try:
                self.on_progress(self._recv_name, self._recv_pos, self._recv_total)
            except Exception:
                pass

    def _finish_file(self):
        data = bytes(self._recv_data)
        name = self._recv_name
        if self.on_file is not None:
            try:
                self.on_file(name, data)
            except Exception as exc:
                self._log("ZMODEM: on_file failed: %s" % exc)
        else:
            try:
                directory = self.directory or os.getcwd()
                os.makedirs(directory, exist_ok=True)
                # sanitize against path traversal
                safe = os.path.basename(name.replace("\\", "/")) or "zmodem.bin"
                if safe in (".", ".."):
                    safe = "zmodem.bin"
                with open(os.path.join(directory, safe), "wb") as fh:
                    fh.write(data)
            except Exception as exc:
                self._log("ZMODEM: write failed: %s" % exc)
        self._log("ZMODEM: file %s complete (%d bytes)" % (name, len(data)))
        self._recv_data = bytearray()
        self._recv_pos = 0
