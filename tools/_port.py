# -*- coding: utf-8 -*-
"""查清并释放 8777 端口：列出监听进程 PID → 强制结束 → 复查。"""
import re
import socket
import subprocess
import sys
import time

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8777


def listeners(port):
    out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                         encoding="utf-8", errors="replace").stdout
    pids = []
    for line in out.splitlines():
        if f":{port}" not in line or "LISTENING" not in line.upper():
            continue
        m = re.search(r"(\d+)\s*$", line.strip())
        if m:
            pids.append((line.strip()[:60], int(m.group(1))))
    return pids


def port_open(port):
    s = socket.socket()
    s.settimeout(1)
    r = s.connect_ex(("127.0.0.1", port))
    s.close()
    return r == 0


print(f"--- 端口 {PORT} ---")
items = listeners(PORT)
if not items:
    print("无 LISTENING 记录；connect 检测:", "OPEN" if port_open(PORT) else "CLOSED")
else:
    for line, pid in items:
        print(f"  {line}  PID={pid}")
    for _, pid in items:
        if pid == 0:
            continue
        r = subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, text=True, errors="replace")
        print(f"  taskkill {pid}: rc={r.returncode} {(r.stdout or r.stderr).strip()[:80]}")
    time.sleep(2)
    print("  复查:", "OPEN（仍在）" if port_open(PORT) else "CLOSED（已释放）")
