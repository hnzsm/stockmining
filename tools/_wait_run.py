# -*- coding: utf-8 -*-
"""临时：等一条回测跑完，打印进度与结果。用完可删。

    python tools/_wait_run.py 69
"""
import json
import sys
import time
import urllib.request

rid = sys.argv[1] if len(sys.argv) > 1 else "69"
base = "http://127.0.0.1:8777"
t0 = time.time()
for _ in range(240):
    d = json.loads(urllib.request.urlopen(f"{base}/api/backtest/runs/{rid}", timeout=30)
                   .read().decode())["data"]
    print(f"{time.time()-t0:6.0f}s  {d['status']:<8}{d['progress']:.2f}  {d['progress_msg'][:60]}")
    if d["status"] in ("done", "failed", "error"):
        print(json.dumps(d.get("metrics") or {}, ensure_ascii=False)[:600])
        break
    time.sleep(15)
