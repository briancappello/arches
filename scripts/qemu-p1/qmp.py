"""Minimal QMP client for scripts/qemu-p1/vm.sh.

Usage: qmp.py SOCKET quit | key KEY | type TEXT | shot FILE.png
"""

from __future__ import annotations

import json
import socket
import sys
import time

SHIFTED = {
    "!": "1",
    "@": "2",
    "#": "3",
    "$": "4",
    "%": "5",
    "^": "6",
    "&": "7",
    "*": "8",
    "(": "9",
    ")": "0",
    "_": "minus",
    "+": "equal",
    "{": "bracket_left",
    "}": "bracket_right",
    ":": "semicolon",
    '"': "apostrophe",
    "<": "comma",
    ">": "dot",
    "?": "slash",
    "|": "backslash",
    "~": "grave_accent",
}
PLAIN = {
    " ": "spc",
    "-": "minus",
    "=": "equal",
    "[": "bracket_left",
    "]": "bracket_right",
    ";": "semicolon",
    "'": "apostrophe",
    ",": "comma",
    ".": "dot",
    "/": "slash",
    "\\": "backslash",
    "`": "grave_accent",
    "\n": "ret",
}


class Conn:
    def __init__(self, path: str) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.sock.settimeout(30)
        self._f = self.sock.makefile("rw")
        json.loads(self._f.readline())  # greeting
        send(self, self._f, {"execute": "qmp_capabilities"})


def connect(path: str) -> Conn:
    return Conn(path)


def send(s, f, msg) -> dict:
    f.write(json.dumps(msg) + "\n")
    f.flush()
    while True:
        reply = json.loads(f.readline())
        if "return" in reply or "error" in reply:
            if "error" in reply:
                raise SystemExit(f"QMP error: {reply['error']}")
            return reply


def keys_for(ch: str) -> list[str]:
    if ch.isalpha():
        return ["shift", ch.lower()] if ch.isupper() else [ch]
    if ch.isdigit():
        return [ch]
    if ch in PLAIN:
        return [PLAIN[ch]]
    if ch in SHIFTED:
        return ["shift", SHIFTED[ch]]
    raise SystemExit(f"cannot type {ch!r}")


def press(s, combo: list[str]) -> None:
    send(
        s,
        s._f,
        {
            "execute": "send-key",
            "arguments": {"keys": [{"type": "qcode", "data": k} for k in combo]},
        },
    )
    time.sleep(0.05)


def main() -> None:
    sock, cmd, *rest = sys.argv[1:]
    s = connect(sock)
    if cmd == "quit":
        send(s, s._f, {"execute": "quit"})
    elif cmd == "key":
        press(s, rest[0].split("-") if rest[0] not in ("minus",) else [rest[0]])
    elif cmd == "type":
        for ch in rest[0]:
            press(s, keys_for(ch))
    elif cmd == "shot":
        send(
            s,
            s._f,
            {
                "execute": "screendump",
                "arguments": {"filename": rest[0], "format": "png"},
            },
        )
    else:
        raise SystemExit(f"unknown command {cmd}")


if __name__ == "__main__":
    main()
