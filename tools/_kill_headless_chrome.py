# -*- coding: utf-8 -*-
"""清掉探针跑挂时残留的 headless Chrome 进程。

为什么需要：探针在 headless 里挂住（超时被杀）时，`chrome.exe` 的子进程可能留在
后台不退；累积多了会拖慢后续每次探针启动。排查 `verify_*` 的浏览器段卡住时先跑这个。

只杀**命令行里带 --headless** 的进程，用户日常在用的浏览器一个不碰。

用法：$PY tools/_kill_headless_chrome.py
"""
import subprocess
import sys

PS = (
    "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
    "Where-Object { $_.CommandLine -like '*--headless*' } | "
    "ForEach-Object { $_.ProcessId }"
)


def main() -> int:
    r = subprocess.run(["powershell", "-NoProfile", "-Command", PS],
                       capture_output=True)
    pids = [x.strip() for x in r.stdout.decode("utf-8", "replace").split() if x.strip().isdigit()]
    if not pids:
        print("没有残留的 headless Chrome")
        return 0
    killed = 0
    for pid in pids:
        k = subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
        if k.returncode == 0:
            killed += 1
    print("清掉 %d/%d 个 headless Chrome 进程" % (killed, len(pids)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
