# -*- coding: utf-8 -*-
"""开启联网搜索：web_search = "live"（顶层键）+ 控制上下文大小的 [tools.web_search]

为什么之前没有：`resolve_web_search_mode`（core/src/config/mod.rs:2741）在没有
`web_search` 键、也没开 feature 时返回 None —— 工具根本不出现。不是百炼不支持：
实测同一线路（wire_api="responses"）下模型调了 web_search，并引用到中国气象网。

代价：一次搜索实测 input 22k tokens（结果文本很大）。所以
  search_context_size = "low"  把注入结果压小
  只在真需要时用（模型自己决定；我们不写"每次都要搜"的规则）
"""
import io
import os

ROOT = os.environ.get("JL_ROOT", "/home/baize/jl-service")

TOP = 'web_search = "live"'
TOOLS = '''
[tools.web_search]
search_context_size = "low"
'''


def patch_text(text):
    """把 web_search 插到顶层键区（第一个 [table] 之前），并补 [tools.web_search]。"""
    lines = text.splitlines()
    if any(l.strip().startswith("web_search") for l in lines):
        pass
    else:
        # 找第一个表头
        at = next((i for i, l in enumerate(lines) if l.startswith("[")), len(lines))
        lines.insert(at, TOP)
    if "[tools.web_search]" not in text:
        # 放在 [features] 之前（都是合法表，位置只影响可读性）
        at = next((i for i, l in enumerate(lines) if l.startswith("[features]")), len(lines))
        lines[at:at] = TOOLS.strip("\n").splitlines() + [""]
    return "\n".join(lines).rstrip() + "\n"


def main():
    # 1) 主配置（新用户 home 的模板来自它）
    main_cfg = os.path.join(os.path.expanduser("~"), ".easycode", "config.toml")
    if os.path.exists(main_cfg):
        s = io.open(main_cfg, encoding="utf-8").read()
        new = patch_text(s)
        if new != s:
            io.open(main_cfg, "w", encoding="utf-8", newline="\n").write(new)
            print("  主配置已开 web_search")

    # 2) 所有用户 home
    homes = os.path.join(ROOT, "data", "homes")
    n = 0
    if os.path.isdir(homes):
        for uid in sorted(os.listdir(homes)):
            cfg = os.path.join(homes, uid, "config.toml")
            if not os.path.isfile(cfg):
                continue
            s = io.open(cfg, encoding="utf-8").read()
            new = patch_text(s)
            if new != s:
                io.open(cfg, "w", encoding="utf-8", newline="\n").write(new)
                n += 1
    print("  用户家目录已开 web_search：%d 个" % n)


if __name__ == "__main__":
    main()
