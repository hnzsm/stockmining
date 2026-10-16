"""模板内联脚本语法检查：把 Jinja 的 {{ }} / {% %} 中和掉再交给 node --check。

用法：$PY tools/_tpl_syntax_check.py templates/stock.html templates/screen.html ...
"""
import re
import subprocess
import sys
import tempfile
import os

NODE = r"C:\Users\ming\.workbuddy\binaries\node\versions\22.22.2-3\node.exe"
if not os.path.exists(NODE):
    NODE = "node"

SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)


def neutralize(src: str) -> str:
    src = re.sub(r"\{\{.*?\}\}", "0", src, flags=re.S)
    src = re.sub(r"\{%.*?%\}", "", src, flags=re.S)
    src = re.sub(r"\{#.*?#\}", "", src, flags=re.S)
    return src


def main(paths):
    bad = 0
    for p in paths:
        html = open(p, encoding="utf-8").read()
        blocks = SCRIPT_RE.findall(html)
        if not blocks:
            print("SKIP %s（没有内联脚本）" % p)
            continue
        js = "\n;\n".join(neutralize(b) for b in blocks)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(js)
            tmp = fh.name
        r = subprocess.run([NODE, "--check", tmp], capture_output=True, text=True)
        os.unlink(tmp)
        if r.returncode == 0:
            print("OK   %s（%d 段内联脚本）" % (p, len(blocks)))
        else:
            bad += 1
            print("FAIL %s\n%s" % (p, (r.stderr or "").strip()[:800]))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
