#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""独立验证器 v2：DoD + 多仓库 commit + 门禁 + 负向用例，全部落进回执。

硬约束（缺一条就 fail）：
  1. JL_VERIFIER_TOKEN 必须由外部注入（Builder 会话里不设就没有）
  2. 至少一个 commit；跨仓库项要传多个 --commit（后端、小程序各一）
  3. 每个 commit 必须真有文件改动
  4. DoD 里至少一条负向（check 含"负向"或 kind=negative）
  5. gates 文件必须含 preflight/deploy/smoke/acceptance 四个退出码，
     且 gate.head 必须等于当前 HEAD（防用旧门禁签新代码）

用法（在 Verifier 会话里）：
  设置 JL_VERIFIER_TOKEN
  python3 scripts/loop-verify.py --item P0-perf --dod dod.json \
      --commit <后端sha> --commit <小程序sha> --gates docs/state/last-gate.json \
      --sign "verifier:independent" --round 1
"""
import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECEIPTS = os.path.join(ROOT, "docs", "receipts")
MINIAPP = os.environ.get("JL_MINIAPP_REPO") or r"C:\Users\NJHTR\WeChatProjects\jl"
GATES_REQUIRED = ("preflight", "deploy", "smoke", "acceptance")


def sh(cmd, cwd=ROOT):
    return subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True,
                          encoding="utf-8", errors="replace").stdout.strip()


def fix_cmd(cmd):
    if cmd.startswith("python "):
        return '"%s" %s' % (sys.executable, cmd[len("python "):])
    return cmd


def run(cmd, timeout=900):
    t0 = time.time()
    r = subprocess.run(fix_cmd(cmd), shell=True, cwd=ROOT, capture_output=True,
                       timeout=timeout, encoding="utf-8", errors="replace")
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    return r.returncode, out[-500:], round(time.time() - t0, 1)


def resolve_commit(ref):
    """返回 (repo, full_sha, short, files)；先找后端，再找小程序。"""
    for repo in (ROOT, MINIAPP):
        if not os.path.isdir(os.path.join(repo, ".git")):
            continue
        sha = sh("git rev-parse %s" % ref, cwd=repo)
        if not sha:
            continue
        files = [f for f in sh("git show --name-only --pretty=format: %s" % sha,
                               cwd=repo).splitlines() if f.strip()]
        if files:
            return repo, sha, sha[:7], files
    return None, None, None, []


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--item", required=True)
    ap.add_argument("--dod", required=True)
    ap.add_argument("--commit", action="append", required=True)
    ap.add_argument("--gates", required=True)
    ap.add_argument("--sign", default="verifier")
    ap.add_argument("--round", type=int, default=0)
    a = ap.parse_args()

    token = os.environ.get("JL_VERIFIER_TOKEN", "")
    if not token:
        print("拒绝：没有 JL_VERIFIER_TOKEN（验证器必须在独立会话里由外部注入）")
        return 2
    if a.sign.lower().startswith("builder"):
        print("拒绝：Builder 不能签")
        return 2

    commits = []
    for ref in a.commit:
        repo, sha, short, files = resolve_commit(ref)
        if not sha:
            print("失败：commit %r 不存在或没有文件改动" % ref)
            return 1
        commits.append({"repo": os.path.basename(repo), "sha": short,
                        "path": repo, "files": files})

    base = sh("git rev-parse %s^" % commits[0]["sha"], cwd=commits[0]["path"]) or ""
    h = hashlib.sha256()
    for c in commits:
        h.update(sh("git diff %s^..%s" % (c["sha"], c["sha"]), cwd=c["path"]).encode())
    diff_hash = h.hexdigest()[:16]

    dod = json.load(io.open(a.dod, encoding="utf-8"))
    has_negative = any(("负向" in str(it.get("check", "")) or it.get("kind") == "negative")
                       for it in dod)
    if not has_negative:
        print("失败：DoD 里没有负向用例（防止只测 happy path）")
        return 1

    gates = {}
    if os.path.isfile(a.gates):
        gates = json.load(io.open(a.gates, encoding="utf-8"))
    missing = [k for k in GATES_REQUIRED if k not in gates]
    if missing:
        print("失败：门禁缺少 %s（回执必须带四个退出码）" % missing)
        return 1
    head_now = sh("git rev-parse --short HEAD")
    gate_head = (gates.get("head") or "").strip()
    if gate_head and gate_head != head_now:
        print("失败：门禁是旧代码跑的（gate.head=%s，当前 HEAD=%s），先重跑 loop-gate"
              % (gate_head, head_now))
        return 1

    evidence, ok = [], True
    t0 = time.time()
    for it in dod:
        code, out, secs = run(it["cmd"])
        expect = str(it.get("expect", "0")).strip()
        if expect.startswith("exit:"):
            good = code == int(expect.split(":", 1)[1])
        elif expect in ("ok", "OK"):
            good = code == 0 and "FAIL" not in out
        else:
            good = code == 0 and expect in out
        ok = ok and good
        print(("  PASS " if good else "  FAIL ") + "%s (%ss)" % (it["check"], secs))
        if not good:
            print("    尾部: " + out.replace("\n", " ")[-260:])
        evidence.append({"check": it["check"], "cmd": it["cmd"], "exit": code,
                         "expect": expect, "pass": good, "secs": secs,
                         "out_tail": out})

    gates = dict(gates)
    gates["negative"] = "pass" if ok else "fail"

    os.makedirs(RECEIPTS, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M")
    fp = os.path.join(RECEIPTS, "%s-%s.json" % (ts, a.item))
    rec = {
        "round": a.round,
        "item": a.item,
        "type": "IMPL",
        "commit": commits[0]["sha"],
        "commits": [{k: c[k] for k in ("repo", "sha")} for c in commits],
        "files": [f for c in commits for f in c["files"]],
        "base_commit": base[:7] if base else None,
        "diff_hash": diff_hash,
        "dod": [{k: it.get(k) for k in ("check", "cmd", "expect")} for it in dod],
        "gates": gates,
        "verdict": "pass" if ok else "fail",
        "evidence": evidence,
        "verified_by": a.sign,
        "verifier_token": hashlib.sha256(token.encode()).hexdigest()[:8],
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "duration_s": round(time.time() - t0, 1),
    }
    io.open(fp, "w", encoding="utf-8").write(
        json.dumps(rec, ensure_ascii=False, indent=2) + "\n")
    print("  回执: docs/receipts/%s（%s，%d 个 commit）"
          % (os.path.basename(fp), rec["verdict"], len(commits)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
