# -*- coding: utf-8 -*-
"""验收报告：一条命令跑完核心链路，输出一份人能读懂的报告。

和 smoke 的分工：
  · smoke 是"门禁"（非 0 退出就拦部署），重在断言全不全
  · 这个脚本是"给人看的"：每项一行、带数字、最后写一份 Markdown 到 runtime/

只做一件事会改动数据：生成一份巡视记录（用于校验表式），跑完即删。

用法：python3 scripts/acceptance.py [projectId]
"""
import io
import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.environ.get("BFF_URL") or "http://127.0.0.1:8081"
PROJECT = sys.argv[1] if len(sys.argv) > 1 else "test-sandbox"
# 硬守卫：验收会生成/删除导出文件，只允许在测试项目上跑
if PROJECT != "test-sandbox":
    print("拒绝运行：PROJECT=%r，必须是 test-sandbox" % PROJECT)
    sys.exit(2)
rows = []


def call(method, path, body=None, timeout=60):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except Exception as e:
        return 0, {"error": repr(e)}


def sql(query):
    pw = ""
    for line in io.open(os.path.join(ROOT, ".env"), encoding="utf-8"):
        if line.startswith("MYSQL_PASSWORD="):
            pw = line.split("=", 1)[1].strip()
    r = subprocess.run(["docker", "exec", "jl-mysql", "mysql", "-ujl", "-p" + pw,
                        "--default-character-set=utf8mb4", "-N", "-e", query, "jl"],
                       capture_output=True, text=True, timeout=60)
    return r.stdout.strip()


def item(name, ok, detail=""):
    rows.append((name, ok, detail))
    print(("  ✓ " if ok else "  ✗ ") + name + (("  " + detail) if detail else ""))


def main():
    t0 = time.time()
    st, b = call("GET", "/healthz")
    item("服务健康", st == 200 and (b.get("ok") or b.get("engine")),
         "engine=%s" % b.get("engine"))

    n_doc = sql("SELECT COUNT(*) FROM doc")
    n_todo = sql("SELECT COUNT(*) FROM doc WHERE state='待补'")
    n_mem = sql("SELECT COUNT(*) FROM memory WHERE status=1")
    item("数据可读", n_doc.isdigit(), "文书 %s（待补 %s）· 记忆 %s" % (n_doc, n_todo, n_mem))

    st, b = call("GET", "/api/v1/metrics/memory?projectId=" + PROJECT)
    tot = ((b.get("data") or {}).get("totals") or {})
    item("记忆指标可用", st == 200 and "revokeRate" in tot,
         "条目 %s · 复用 %s" % (tot.get("entries"), tot.get("recallUses")))

    st, b = call("GET", "/api/v1/photos/index?projectId=" + PROJECT)
    d = b.get("data") or {}
    item("影像索引可用", st == 200, "%s 张 / %s 个部位" % (d.get("total"), len(d.get("places") or [])))

    st, b = call("GET", "/api/v1/rule-proposals?projectId=" + PROJECT + "&status=pending")
    item("待确认规矩可查", st == 200, "%s 条待确认" % len(b.get("data") or []))

    # 表式：生成一份巡视记录并校验结构
    today = time.strftime("%Y-%m-%d")
    st, b = call("POST", "/api/v1/reports/patrol",
                 {"projectId": PROJECT, "from": today, "to": today, "inspector": "验收"})
    got = b.get("data") or {}
    ok_form = st == 200 and bool(got.get("url"))
    detail = "%s 字节" % got.get("bytes") if ok_form else (b.get("message") or "")
    tables = 0
    if ok_form:
        try:
            with urllib.request.urlopen(BASE + got["url"], timeout=60) as r:
                raw = r.read()
            fp = os.path.join(ROOT, "runtime", "acceptance-patrol.docx")
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            with open(fp, "wb") as f:
                f.write(raw)
            from docx import Document
            doc = Document(fp)
            tables = len(doc.tables)
            shapes = {(len(t.rows), len(t.columns)) for t in doc.tables}
            ok_form = raw[:2] == b"PK" and shapes == {(4, 4)}
            detail = "%d 张表 %s" % (tables, shapes)
            # 清掉导出与临时文件
            name = os.path.basename(got["url"])
            for pat in ("data/exports/*/" + name, "data/exports/*/" + name + ".meta.json"):
                import glob
                for cand in glob.glob(os.path.join(ROOT, pat)):
                    try:
                        os.remove(cand)
                    except OSError:
                        pass
            os.remove(fp)
        except Exception as e:
            ok_form, detail = False, repr(e)
    item("省统表出表（巡视记录）", ok_form, detail)

    # 备份
    bdir = os.path.join(ROOT, "backups")
    files = [f for f in os.listdir(bdir)] if os.path.isdir(bdir) else []
    export_tars = [f for f in files if f.startswith("exports-")]
    item("备份在跑", bool(files) and bool(export_tars),
         "%d 个包（含导出 %d 个）" % (len(files), len(export_tars)))

    # 引擎记忆：按 D-记忆-04 应当是关闭的（MEMORY.md 只作候选，不参与注入）
    import glob as _glob
    cfgs = _glob.glob(os.path.join(ROOT, "data", "homes", "*", "config.toml"))
    on = [f for f in cfgs if "memories = true" in io.open(f, encoding="utf-8").read()]
    item("引擎记忆已关闭（D-记忆-04）", len(on) == 0,
         "%d/%d 户仍开着" % (len(on), len(cfgs)))

    bad = [r for r in rows if not r[1]]
    out = os.path.join(ROOT, "runtime", "acceptance-%s.md" % time.strftime("%Y%m%d-%H%M"))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    lines = ["# 验收报告 %s" % time.strftime("%Y-%m-%d %H:%M"), "",
             "项目：%s　耗时 %.1fs　结论：%s" % (PROJECT, time.time() - t0,
             "全部通过" if not bad else "有 %d 项未过" % len(bad)), "",
             "| 项 | 结果 | 细节 |", "|---|---|---|"]
    for name, ok, detail in rows:
        lines.append("| %s | %s | %s |" % (name, "✓" if ok else "✗", detail))
    io.open(out, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print("\n  报告: %s" % out)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
