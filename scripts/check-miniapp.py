# -*- coding: utf-8 -*-
"""小程序静态自检：真机验收前先过这一遍（不需要开微信）。

它查的都是"没有它真机一定出问题"的东西：
  · 页面登记与文件是否对得上（缺文件 = 白屏）
  · 相机/定位的隐私声明（缺了审核不过、定位会被拒）
  · 上传字段与后端要求是否一致（缺 projectId = 后端 400）
  · 报告页是否真的在调 /reports/patrol 并打开 docx
  · API_BASE 是否还是本机隧道地址（真机必须换成 https 域名）

用法：python3 scripts/check-miniapp.py
退出码 0 = 全过；1 = 有问题（逐条打印）
"""
import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MP = os.environ.get("JL_MINIAPP") or r"C:\Users\NJHTR\WeChatProjects\jl"

problems = []


def need(cond, msg):
    if not cond:
        problems.append(msg)


def read(rel):
    fp = os.path.join(MP, rel)
    if not os.path.isfile(fp):
        problems.append("缺文件: %s" % rel)
        return ""
    return io.open(fp, encoding="utf-8").read()


def main():
    if not os.path.isdir(MP):
        print("  找不到小程序目录: %s" % MP)
        return 1

    app = json.loads(read("app.json") or "{}")
    pages = app.get("pages") or []
    for p in pages:
        for ext in (".js", ".wxml"):
            need(os.path.isfile(os.path.join(MP, p + ext)),
                 "页面登记了但文件不存在: %s%s" % (p, ext))

    # 隐私声明：相机/定位
    rpi = app.get("requiredPrivateInfos") or []
    need("getLocation" in rpi, "app.json 缺 requiredPrivateInfos: getLocation")
    need("chooseLocation" in rpi, "app.json 缺 requiredPrivateInfos: chooseLocation")
    need("scope.userLocation" in (app.get("permission") or {}),
         "app.json 缺 permission.scope.userLocation（定位会被拒）")

    # 拍照页
    cap = read("pages/capture/capture.wxml")
    cap_js = read("pages/capture/capture.js")
    need("<camera" in cap, "拍照页没有 <camera>")

    # 报告页：调后端 + 打开 docx
    rep = read("pages/report/report.js")
    need("/api/v1/reports/patrol" in rep, "报告页没有调 /api/v1/reports/patrol")
    need("openDocument" in rep, "报告页没有 wx.openDocument（下载了打不开）")
    need("downloadFile" in rep, "报告页没有 wx.downloadFile")

    # 上传字段：后端要 projectId
    up = read("utils/upload.js")
    need("projectId" in up, "上传没有带 projectId（后端会 400）")

    # 离线拍照队列（F5）：失败要入队、回到页面要补传
    need(os.path.isfile(os.path.join(MP, "utils", "offline.js")),
         "缺 utils/offline.js（离线拍照队列）")
    need("offline.enqueue" in cap_js, "拍照失败没有入队（断网=照片丢失）")
    need("offline.drain" in cap_js, "回到拍照页没有补传")
    need("pending" in cap, "取景页没有待传提示")

    # API 地址：本机隧道只适合开发者工具
    cfg = read("utils/config.js")
    m = re.search(r'API_BASE\s*:\s*["\']([^"\']*)["\']', cfg)
    api_base = m.group(1) if m else ""
    device_ready = api_base.startswith("https://")
    print("  API_BASE = %r（%s）" % (api_base,
          "可上真机" if device_ready else "仅开发者工具/隧道，真机需换 https 域名"))

    print("  页面 %d 个 · requiredPrivateInfos=%s" % (len(pages), rpi))
    if problems:
        print("发现 %d 个问题：" % len(problems))
        for p in problems:
            print("  X " + p)
        return 1
    print("全部通过（真机验收清单见 docs/CHECKLIST-DEVICE.md）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
