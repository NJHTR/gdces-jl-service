# -*- coding: utf-8 -*-
"""记忆引用标记：[m:12]

为什么用标记而不是后端去猜：
- 模型决定"这次要不要用某条记忆"，用标记写出来是可核对的
- 后端只做机械解析（去掉标记、加引用、hits+1），不做语义判断
- 没有标记就当没用过，不虚报"依据你的习惯"
"""
import re

MEM_RE = re.compile(r"\[m:(\d+)\]")


def strip_mem_refs(text):
    """把 [m:12] 从正文里摘掉，返回 (干净正文, 用到的 id 列表[保序去重])。"""
    ids = []

    def _take(m):
        i = int(m.group(1))
        if i not in ids:
            ids.append(i)
        return ""

    clean = MEM_RE.sub(_take, text or "")
    clean = re.sub(r"[ \t]{2,}", " ", clean)
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    return clean.strip(), ids
