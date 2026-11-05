#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探测百炼上的模型：哪些适合做 agent、哪些可能支持多 agent。

要回答的问题
    多 agent 的子 agent 启动失败，报 `encrypted_content` 不兼容。
    推理内容（reasoning）才会带 encrypted_content，所以**不产生推理的模型**
    可能绕过这个问题。

做法
    1. 拉 /models
    2. 对每个对话类模型发一个最小 /responses 请求
    3. 记录：HTTP、是否有 reasoning 块、reasoning_tokens、耗时

用法
    python3 scripts/probe_models.py
"""
import json
import os
import time
import urllib.request

ROOT = "/home/baize/jl-service"


def env(key):
    with open(os.path.join(ROOT, ".env"), encoding="utf-8") as f:
        for line in f:
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    return ""


KEY = env("DASHSCOPE_API_KEY")
BASE = env("DASHSCOPE_BASE_URL").rstrip("/")
SKIP = ("image", "video", "tts", "asr", "realtime", "audio")


def post(url, body, timeout=90):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode()), round(time.time() - t0, 1)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        return e.code, {"_raw": body[:200]}, round(time.time() - t0, 1)
    except Exception as e:
        return 0, {"_err": str(e)[:120]}, round(time.time() - t0, 1)


def main():
    req = urllib.request.Request(BASE + "/models", headers={"Authorization": "Bearer " + KEY})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read().decode())

    ids = [m.get("id") for m in data.get("data", []) if isinstance(m, dict)]
    print("上游共 %d 个模型\n" % len(ids))

    cands = [i for i in ids if i and not any(s in i.lower() for s in SKIP)]
    print("%-26s %-5s %-8s %-8s %-6s %s" % ("模型", "HTTP", "推理块", "推token", "秒", "判定"))
    print("-" * 78)

    results = []
    for m in cands:
        code, body, secs = post(
            BASE + "/responses",
            {"model": m, "input": [{"role": "user",
                                    "content": [{"type": "input_text", "text": "说一个字：好"}]}]},
        )
        out = body.get("output") or [] if isinstance(body, dict) else []
        has = any((it or {}).get("type") == "reasoning" for it in out)
        u = (body.get("usage") or {}).get("output_tokens_details") or {} if isinstance(body, dict) else {}
        rt = int(u.get("reasoning_tokens") or 0)

        if code != 200:
            verdict = "不可用 %s" % (str(body.get("_raw") or body.get("_err") or "")[:40])
        elif has or rt:
            verdict = "有推理"
        else:
            verdict = "✅ 无推理（多 agent 候选）"

        print("%-26s %-5s %-8s %-8s %-6s %s" % (m, code, "有" if has else "无", rt, secs, verdict))
        results.append({"model": m, "code": code, "reasoning": has, "rtokens": rt, "verdict": verdict})

    ok = [r for r in results if r["code"] == 200 and not r["reasoning"] and r["rtokens"] == 0]
    print("\n结论：无推理的模型 %d 个" % len(ok))
    for r in ok:
        print("  -", r["model"])
    print("\n下一步：拿其中一个改 ~/.easycode/config.toml 的 model，再跑 probe_spawn.py")


if __name__ == "__main__":
    main()
