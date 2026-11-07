#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""独立验证器：只读 DoD + git diff，跑命令，签回执。

Builder 不能用它给自己盖章：--sign 必须由 Verifier 传入，且不能以 builder 开头。
缺 commit / files 为空 → 直接 fail（防"改了个空提交"）。

用法：
  python3 scripts/loop-verify.py --item P0-perf --dod dod.json \
          --sign "verifier:independent" [--commit HEAD] [--gates gates.json]
"""
import argparse
import io
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECEIPTS = os.path.join(ROOT, "docs", "receipts")
STATE = os.path.join(ROOT, "docs", "state", "loop.json")


def run(cmd, timeout=900):
    t0 = time.time()
    r = subprocess.run(cmd, shell=True, cwd=ROOT, capture_output=True,
                       text=True, timeout=timeout)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    return r.returncode, out[-400:], round(time.time() - t0, 1)


def git_out(cmd):
    return subprocess.run(cmd, shell=True, cwd=ROOT, capture_output=True,
                          text=True).stdout.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--item", required=True)
    ap.add_argument("--dod", required=True, help="json: [{check,cmd,expect}]")
    ap.add_argument("--sign", default="", help="verifier:<name>")
    ap.add_argument("--commit", default="HEAD")
    ap.add_argument("--gates", default="", help="json: {preflight:0,...}")
    ap.add_argument("--round", type=int, default=0)
    a = ap.parse_args()

    if not a.sign or a.sign.lower().startswith("builder"):
        print("拒绝：必须由 Verifier 签名（--sign verifier:<name>），Builder 不能自签")
        return 2

    commit = git_out("git rev-parse --short %s" % a.commit)
    files = [f for f in git_out(
        "git show --name-only --pretty=format: %s" % a.commit).splitlines() if f.strip()]
    if not commit or not files:
        print("失败：commit=%r files=%r（没有实际改动不能算完成）" % (commit, files))
        return 1

    dod = json.load(io.open(a.dod, encoding="utf-8"))
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
            good = (code == 0 and expect in out)
        ok = ok and good
        print(("  ✓ " if good else "  ✗ ") + "%s（%ss）" % (it["check"], secs))
        if not good:
            print("    尾部: " + out.replace("\n", " ")[-260:])
        evidence.append({"check": it["check"], "cmd": it["cmd"], "exit": code,
                         "expect": expect, "pass": good, "secs": secs,
                         "out_tail": out})

    gates = {}
    if a.gates and os.path.isfile(a.gates):
        gates = json.load(io.open(a.gates, encoding="utf-8"))
    gates = dict(gates)
    gates["negative"] = "pass" if ok else "fail"

    os.makedirs(RECEIPTS, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M")
    fp = os.path.join(RECEIPTS, "%s-%s.json" % (ts, a.item))
    rec = {
        "round": a.round,
        "item": a.item,
        "type": "IMPL",
        "commit": commit,
        "files": files,
        "dod": [{k: it.get(k) for k in ("check", "cmd", "expect")} for it in dod],
        "gates": gates,
        "verdict": "pass" if ok else "fail",
        "evidence": evidence,
        "decisions_needed": [],
        "risks": [],
        "duration_s": round(time.time() - t0, 1),
        "verified_by": a.sign,
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    io.open(fp, "w", encoding="utf-8").write(
        json.dumps(rec, ensure_ascii=False, indent=2) + "\n")
    print("  回执: docs/receipts/%s（%s）" % (os.path.basename(fp), rec["verdict"]))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
