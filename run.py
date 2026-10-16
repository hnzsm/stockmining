# -*- coding: utf-8 -*-
"""启动脚本：初始化数据库 -> 启动 Flask -> 自动打开浏览器。"""
from __future__ import annotations

import os
import sys
import threading
import time
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from werkzeug.serving import is_running_from_reloader  # noqa: E402

from app import app, bootstrap  # noqa: E402
from config import FLASK_DEBUG, FLASK_HOST, FLASK_PORT  # noqa: E402

URL = f"http://{FLASK_HOST}:{FLASK_PORT}"

_open_lock = threading.Lock()
_opened = False


def open_browser():
    """等端口起来后打开主页；同一进程内最多开一次。"""
    global _opened
    time.sleep(1.4)
    with _open_lock:
        if _opened:
            return
        _opened = True
    try:
        webbrowser.open(URL)
    except Exception:  # noqa: BLE001
        pass


def should_open_browser() -> bool:
    """要不要由**本进程**来开浏览器 —— 只让「非 reloader 进程」开，避免弹两次。

    ⚠ debug=True 时 Werkzeug reloader 是「父进程看门狗 + 子进程真干活」两个进程，
    两边都会跑一遍 main()（细节见 CONVENTIONS.md C22）→ 各开一次浏览器 = 两个窗口。
    is_running_from_reloader() 为 True 的**只有那个子进程**；父进程、以及「没开 reloader
    时的唯一进程」都是 False，所以交给 False 的那边开：debug 开/关都恰好一次。
    附带好处：改 .py 只重起子进程、父进程不动 ⇒ 热重载不会再弹新窗口。
    """
    if os.getenv("STOCKMINING_NO_BROWSER") == "1":
        return False
    return not is_running_from_reloader()


def main():
    print("=" * 62)
    print("  短线选股工作台  StockMining")
    print("=" * 62)
    try:
        bootstrap()
        print("  [OK] 数据库结构就绪，内置策略已安装")
    except Exception as e:  # noqa: BLE001
        print(f"  [!!] 初始化失败：{e}")
        print("       请检查 config.py 中的 MySQL 配置，以及 MySQL 服务是否启动。")
    print(f"  访问地址: {URL}")
    print("=" * 62)
    if should_open_browser():
        threading.Thread(target=open_browser, daemon=True).start()
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=FLASK_DEBUG, threaded=True)


if __name__ == "__main__":
    main()
