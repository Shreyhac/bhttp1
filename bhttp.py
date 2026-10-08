"""BHTTP/1 shared framing helpers, used by bserve and bcurl.

Wire format (see SPEC.md): one long-lived TCP connection, 4-byte preface
"BHT1", then frames. Frame header is 8 bytes: Length (24 bits, payload size)
| Type (8) | Flags (8) | Stream ID (24), then Length payload bytes.
All integers are unsigned big-endian. Strings are UTF-8 with explicit lengths.
"""
import math, os, socket, struct, time

PREFACE = b"BHT1"
HDR_LEN = 8
MAX_PAYLOAD = 16384
MAX_STREAM = 0xFFFFFF
MAX_PATH = MAX_PAYLOAD - 4          # method (1) + path length (2) + count (1)

HEADERS, DATA, GOAWAY = 0x0, 0x1, 0x2
F_END_STREAM = 0x01

M_GET, M_HEAD = 0x01, 0x02
METHODS = {M_GET: "GET", M_HEAD: "HEAD"}

E_NONE, E_PROTOCOL, E_FRAME_SIZE, E_PREFACE, E_INTERNAL = 0, 1, 2, 3, 4
GOAWAY_TEXT = {E_NONE: "normal", E_PROTOCOL: "protocol error",
               E_FRAME_SIZE: "frame too large", E_PREFACE: "bad preface",
               E_INTERNAL: "internal error"}

# After sending GOAWAY: read and discard input for at most this long / this
# many bytes before closing, so unread input does not turn the close into RST.
DRAIN_SECONDS = 1.0
DRAIN_LIMIT = 65536

STATIC = ["host", "user-agent", "accept", "content-type", "content-length",
          "server", "date", "etag", "last-modified", "cache-control"]
IDX = {n: i + 1 for i, n in enumerate(STATIC)}
NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")


class Malformed(Exception):
    """Bad payload, framing intact: the server answers 400 on that stream
    and keeps the connection open; a client treats it as a protocol error."""


class FramingError(Exception):
    """Broken framing or a protocol error: send GOAWAY with .code and close."""
    def __init__(self, msg, code=E_PROTOCOL):
        super().__init__(msg)
        self.code = code


class PeerGoaway(FramingError):
    """The peer sent a well-formed GOAWAY. .code is the peer's code (unknown
    codes read as 1). Do not answer it: close and stop using the connection."""
    def __init__(self, code):
        if code not in GOAWAY_TEXT:
            code = E_PROTOCOL
        super().__init__("peer sent GOAWAY(%d): %s" % (code, GOAWAY_TEXT[code]), code)


def env_seconds(name, default):
    """A positive, finite number of seconds from the environment."""
    raw = os.environ.get(name)
    if raw is None:
        return float(default)
    try:
        val = float(raw)
    except ValueError:
        raise ValueError("%s must be a number of seconds, got %r" % (name, raw))
    if not math.isfinite(val) or val <= 0:
        raise ValueError("%s must be positive and finite, got %r" % (name, raw))
    return val


def env_int(name, default, lo, hi):
    raw = os.environ.get(name)
    if raw is None:
        return default
    if not (raw.isascii() and raw.isdigit()) or not lo <= int(raw) <= hi:
        raise ValueError("%s must be an integer in %d..%d, got %r" % (name, lo, hi, raw))
    return int(raw)


# Seconds allowed to receive one whole frame once its first byte arrived.
FRAME_TIMEOUT = env_seconds("BHTTP_FRAME_TIMEOUT", 10)


def recv_exact(sock, n, deadline=None):
    """Read exactly n bytes. EOFError: clean close. FramingError: too slow."""
    buf = bytearray()
    while len(buf) < n:
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0:
                raise FramingError("frame not completed in time")
            sock.settimeout(left)
        try:
            chunk = sock.recv(n - len(buf))
        except socket.timeout:
            raise FramingError("frame not completed in time")
        if not chunk:
            raise EOFError("closed")
        buf += chunk
    return bytes(buf)


def read_frame(sock, tap=None, idle=None):
    """Return (type, flags, stream, payload).

    idle: seconds to wait for the first byte (None waits forever). It is set
    on every call, so a deadline left over from an earlier frame never leaks.
    EOFError: clean close on a frame boundary.
    socket.timeout: nothing arrived for `idle` seconds.
    FramingError: cut mid-frame, frame too slow, or Length over the cap.
    The Length check happens before the Type is looked at, so an oversized
    frame of any type, known or unknown, is a framing error, never a skip.
    At most 8 + 16384 bytes are ever buffered for one frame.
    """
    sock.settimeout(idle)
    first = sock.recv(1)
    if not first:
        raise EOFError("closed")
    deadline = time.monotonic() + FRAME_TIMEOUT
    try:
        head = first + recv_exact(sock, 7, deadline)
    except EOFError:
        raise FramingError("cut mid-header")
    length = int.from_bytes(head[0:3], "big")
    if length > MAX_PAYLOAD:
        if tap:
            tap("<-", head)
        raise FramingError("length %d over limit" % length, E_FRAME_SIZE)
    try:
        payload = recv_exact(sock, length, deadline) if length else b""
    except EOFError:
        raise FramingError("cut mid-frame")
    if tap:
        tap("<-", head + payload)
    return head[3], head[4], int.from_bytes(head[5:8], "big"), payload


def pack_frame(ftype, flags, stream, payload=b""):
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("payload over the 16384 cap")
    if not 0 <= stream <= MAX_STREAM:
        raise ValueError("stream ID out of range")
    return len(payload).to_bytes(3, "big") + bytes((ftype, flags)) + \
        stream.to_bytes(3, "big") + payload


def pack_goaway(code):
    return pack_frame(GOAWAY, 0, 0, struct.pack(">H", code))


def goaway_code(stream, payload):
    """Validate a received GOAWAY. Returns its code (unknown codes read as 1)."""
    if stream != 0 or len(payload) != 2:
        raise FramingError("malformed GOAWAY")
    (code,) = struct.unpack(">H", payload)
    return code if code in GOAWAY_TEXT else E_PROTOCOL


def goaway_close(sock, code, tap=None, linger=None):
    """Send GOAWAY(code), half-close, drain input briefly, then close.

    Closing a socket that still has unread input makes the kernel send RST
    instead of FIN, and an RST can destroy the GOAWAY before the peer reads
    it. Half-closing first (FIN after the GOAWAY) and reading until the peer
    closes, or DRAIN_SECONDS / DRAIN_LIMIT run out, avoids that.
    """
    frame = pack_goaway(code)
    try:
        sock.sendall(frame)
        if tap:
            tap("->", frame)
    except OSError:
        sock.close()                    # peer already gone: nothing to save
        return
    linger_close(sock, linger)


def linger_close(sock, linger=None):
    """Half-close, read and discard input until the peer closes (at most
    `linger` seconds / DRAIN_LIMIT bytes), then close: FIN, never RST."""
    linger = DRAIN_SECONDS if linger is None else linger
    try:
        sock.shutdown(socket.SHUT_WR)
        end = time.monotonic() + linger
        drained = 0
        while drained < DRAIN_LIMIT:
            left = end - time.monotonic()
            if left <= 0:
                break
            sock.settimeout(left)
            chunk = sock.recv(4096)
            if not chunk:
                break
            drained += len(chunk)
    except OSError:
        pass                            # peer already gone: nothing to save
    finally:
        sock.close()


def _utf8(raw, what):
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise Malformed("%s is not valid UTF-8" % what)


def _take(buf, pos, n, what):
    """Return buf[pos:pos+n]; a field running past the payload is malformed."""
    if pos + n > len(buf):
        raise Malformed("%s runs past the end of the frame" % what)
    return buf[pos:pos + n]


def check_name(name):
    if not name or not set(name) <= NAME_CHARS:
        raise Malformed("header name %r is not lowercase ASCII [a-z0-9-]" % name)


def pack_entries(pairs):
    """Encode header entries: 1-byte count, then per entry a 1-byte index
    (static table) or 0x00 + literal name, then Value length (2) + Value."""
    if len(pairs) > 255:
        raise ValueError("too many headers")
    out = bytearray([len(pairs)])
    for name, value in pairs:
        check_name(name)
        v = value.encode("utf-8")
        if len(v) > 0xFFFF:
            raise ValueError("header value too long")
        if name in IDX:
            out.append(IDX[name])
        else:
            nb = name.encode("ascii")
            if len(nb) > 255:
                raise ValueError("header name too long")
            out += bytes([0, len(nb)]) + nb
        out += struct.pack(">H", len(v)) + v
    return bytes(out)


def parse_entries(buf, pos):
    """Decode header entries from buf[pos:]. Returns (pairs, new_pos).
    Every length is checked against the bytes left (underrun is malformed).
    A literal name equal to a static name counts as that name."""
    count = _take(buf, pos, 1, "header count")[0]; pos += 1
    pairs = []
    seen = set()
    for _ in range(count):
        idx = _take(buf, pos, 1, "header index")[0]; pos += 1
        if idx == 0:
            nl = _take(buf, pos, 1, "literal name length")[0]; pos += 1
            if nl < 1:
                raise Malformed("empty literal name")
            name = _utf8(_take(buf, pos, nl, "literal name"), "header name")
            pos += nl
        elif idx <= len(STATIC):
            name = STATIC[idx - 1]
        else:
            raise Malformed("reserved header index 0x%02x" % idx)
        check_name(name)
        if name in seen:
            raise Malformed("duplicate header %r" % name)
        seen.add(name)
        (vl,) = struct.unpack(">H", _take(buf, pos, 2, "value length")); pos += 2
        val = _utf8(_take(buf, pos, vl, "header value"), "header value")
        pos += vl
        pairs.append((name, val))
    return pairs, pos


def check_path(path):
    """Spec section 5 rules. The path is used literally: no percent-decoding."""
    if not path.startswith("/"):
        raise Malformed("path does not start with /")
    if "\x00" in path:
        raise Malformed("NUL in path")
    if any(seg == ".." for seg in path.split("/")):
        raise Malformed('".." segment in path')


def enc_request(method, path, pairs):
    p = path.encode("utf-8")
    return bytes([method]) + struct.pack(">H", len(p)) + p + pack_entries(pairs)


def dec_request(buf):
    method = _take(buf, 0, 1, "method")[0]
    if method not in METHODS:
        raise Malformed("unknown method 0x%02x" % method)
    (pl,) = struct.unpack(">H", _take(buf, 1, 2, "path length"))
    path = _utf8(_take(buf, 3, pl, "path"), "path")
    check_path(path)
    pairs, pos = parse_entries(buf, 3 + pl)
    if pos != len(buf):
        raise Malformed("trailing bytes")      # overrun: bytes after the last field
    return method, path, pairs


def enc_response(status, pairs):
    if not 100 <= status <= 599:
        raise ValueError("status %d outside 100-599" % status)
    return struct.pack(">H", status) + pack_entries(pairs)


def dec_response(buf):
    (status,) = struct.unpack(">H", _take(buf, 0, 2, "status"))
    pairs, pos = parse_entries(buf, 2)
    if pos != len(buf):
        raise Malformed("trailing bytes")
    return status, pairs


def parse_content_length(value):
    """content-length is one or more ASCII decimal digits, nothing else."""
    if not value or len(value) > 18 or not (value.isascii() and value.isdigit()):
        raise Malformed("bad content-length %r" % value)
    return int(value)


def hexdump(data, prefix=""):
    lines = []
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        hx = " ".join("%02x" % b for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append("%s%04x  %-47s  %s" % (prefix, i, hx, asc))
    return "\n".join(lines)
