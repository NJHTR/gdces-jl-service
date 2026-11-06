#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把组织技能装到引擎真正会扫的作用域（Repo / User）

为什么需要这一步：引擎只在下面这些目录里找 SKILL.md（源码 ext/skills/src/host_roots.rs）：
    Repo  →  <项目工作区>/.easycode/skills/
    User  →  $EASYCODE_HOME/skills/
文件放在 data/knowledge/skills 下，它是看不到的 —— 那只是「源」，
装到这两个位置才叫「生效」。

用法：
    python3 scripts/install-skills.py            # 装到所有项目 + 所有用户家目录
    python3 scripts/install-skills.py --dry      # 只打印会改什么
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bff.bffpkg.core import ROOT, q            # noqa: E402
from bff.bffpkg.knowledge import install_skills, skill_names, HOMES_ROOT  # noqa: E402


def main():
    dry = "--dry" in sys.argv

    names = skill_names()
    print("组织技能（源：data/knowledge/skills）:")
    for n in names:
        print("  - %s" % n)
    if not names:
        print("  （空）—— 没有可装的技能")
        return 1

    projects = [r["id"] for r in q("SELECT id FROM project ORDER BY id")]
    users = []
    if os.path.isdir(HOMES_ROOT):
        for name in sorted(os.listdir(HOMES_ROOT)):
            if name.isdigit():
                users.append(int(name))
    # 至少覆盖库里的用户
    for r in q("SELECT DISTINCT user_id FROM task ORDER BY user_id"):
        uid = r.get("user_id")
        if uid and int(uid) not in users:
            users.append(int(uid))

    print("\n目标：项目 %s · 用户 %s" % (projects, users))
    if dry:
        print("\n--dry：不改任何文件")
        return 0

    res = install_skills(project_ids=projects, user_ids=users, verbose=True)
    print("\n安装完成：写入 %d 个文件，覆盖 %d 个目标目录" % (res["installed"], res.get("targets", 0)))

    # 装完顺手核对一处，别只看返回值
    sample = None
    if projects:
        sample = os.path.join(ROOT, "data", "workspaces", str(projects[0]), ".easycode", "skills")
    if sample:
        got = sorted(os.listdir(sample)) if os.path.isdir(sample) else []
        print("核对 %s → %s" % (sample.replace(ROOT + os.sep, ""), got))
    return 0


if __name__ == "__main__":
    sys.exit(main())
