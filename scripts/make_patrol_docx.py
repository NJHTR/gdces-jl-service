# -*- coding: utf-8 -*-
"""生成《监理巡视记录》docx（2026-11-05 ~ 11-06，数据取自当日现场文书与影像）"""
import os

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

# 产物放到用户文档目录：仓库只放生成脚本，避免交付物混进部署
# Documents 在本机是只读挂载（写会 FileNotFoundError），放桌面
OUT_DIR = os.path.join(os.path.expanduser("~"), "Desktop")
os.makedirs(OUT_DIR, exist_ok=True)
OUT = os.path.join(OUT_DIR, "监理巡视记录-20261105-06.docx")

CN_BODY = "宋体"
CN_HEAD = "黑体"


def set_cn(run, font=CN_BODY, size=10.5, bold=False, color=None):
    run.font.name = font
    run.font.size = Pt(size)
    run.bold = bold
    if color:
        run.font.color.rgb = RGBColor(*color)
    # 中文字体必须单独指定 eastAsia，否则 Word 里会回落到默认字体
    run._element.rPr.rFonts.set(qn("w:eastAsia"), font)


def para(doc, text="", size=10.5, bold=False, align=None, font=CN_BODY,
         space_after=4, color=None):
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(space_after)
    p.paragraph_format.line_spacing = 1.35
    if align is not None:
        p.alignment = align
    if text:
        set_cn(p.add_run(text), font=font, size=size, bold=bold, color=color)
    return p


def cell_text(cell, text, bold=False, size=9.5, align=None, font=CN_BODY):
    cell.text = ""
    p = cell.paragraphs[0]
    p.paragraph_format.space_after = Pt(2)
    p.paragraph_format.line_spacing = 1.25
    if align is not None:
        p.alignment = align
    set_cn(p.add_run(text), font=font, size=size, bold=bold)


def make_table(doc, rows, widths=None, header=False):
    t = doc.add_table(rows=0, cols=len(rows[0]))
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    for ri, row in enumerate(rows):
        cells = t.add_row().cells
        for ci, val in enumerate(row):
            cell_text(cells[ci], val,
                      bold=(header and ri == 0) or (ci == 0 and not header),
                      size=9.5)
    if widths:
        for ci, w in enumerate(widths):
            for row in t.rows:
                row.cells[ci].width = Cm(w)
    return t


doc = Document()

# 页面：A4 纵向，窄一点的页边距，表格能放下
sec = doc.sections[0]
sec.orientation = WD_ORIENT.PORTRAIT
sec.page_width = Cm(21.0)
sec.page_height = Cm(29.7)
sec.left_margin = Cm(2.0)
sec.right_margin = Cm(2.0)
sec.top_margin = Cm(2.0)
sec.bottom_margin = Cm(2.0)

# 默认样式字体
normal = doc.styles["Normal"]
normal.font.name = CN_BODY
normal.font.size = Pt(10.5)
normal.element.rPr.rFonts.set(qn("w:eastAsia"), CN_BODY)

# ---------------------------------------------------------------- 标题
para(doc, "监 理 巡 视 记 录", size=20, bold=True, font=CN_HEAD,
     align=WD_ALIGN_PARAGRAPH.CENTER, space_after=2)
para(doc, "广东建设工程监理有限公司", size=10.5,
     align=WD_ALIGN_PARAGRAPH.CENTER, space_after=2)
para(doc, "记录期间：2026 年 11 月 5 日 — 2026 年 11 月 6 日      编号：JL-XS-20261105-06",
     size=9, align=WD_ALIGN_PARAGRAPH.CENTER, space_after=10)

# ---------------------------------------------------------------- 工程基本信息
make_table(doc, [
    ["工程名称", "云谷智造园 · 3 期（深圳市南山区科技园北区 A-3-2）",
     "监理单位", "广东建设工程监理有限公司"],
    ["合同编号", "GZ-2026-031", "施工单位", "中建八局华南公司"],
    ["记录人", "李工（监理员）", "记录日期", "2026-11-05 至 2026-11-06"],
    ["表式依据", "《建设工程监理规范》GB/T 50319 巡视要求；常用《巡视记录》表式（监表 37）", "", ""],
], widths=[2.6, 6.6, 2.4, 5.4])

para(doc, "", space_after=6)

# ---------------------------------------------------------------- 11-05
para(doc, "一、2026 年 11 月 5 日（星期四）", size=12, bold=True, font=CN_HEAD)
para(doc, "天气：未记录（当天现场未留天气记录）　|　巡视时段：上午、下午各一次",
     size=9, space_after=4)

make_table(doc, [
    ["巡视范围\n及主要部位", "3# 楼 5 层顶板，4#、5# 楼外脚手架，10# 楼南侧模板支撑，"
     "13# 楼地下室顶板后浇带，14# 楼外墙脚手架，15#、5# 西侧、7# 东侧临边防护，"
     "16# 楼塔吊，17# 楼砌体，21# 楼西侧临时用电，22# 楼北侧物料提升机，"
     "19#、32# 楼卸料平台，23#、25#、26#、27#、30#、31# 楼外架，29# 楼南侧基坑，24# 楼三层砌体"],
    ["主要施工情况", "主体结构顶板钢筋绑扎与模板支撑搭设；砌体砌筑；外脚手架搭设与拆除;"
     "基坑土方作业；塔吊运行；现场临时用电及垂直运输设备使用"],
    ["质量、安全、环保等情况",
     "质量：3# 楼 5 层顶板钢筋保护层厚度复查，结论符合要求（实测值待补）；"
     "17# 楼砌体灰缝饱满度不足，要求加强砌筑质量控制；24# 楼三层砌体拉结筋漏设 2 处。\n"
     "安全：外架连墙件缺失（4# 楼东侧 2 处、5# 楼南侧 1 处）；26#、31# 楼剪刀撑未连续设置；"
     "27# 楼东侧连墙件间距实测超过 4m；25# 楼立杆底部无垫板；30# 楼作业层未设挡脚板；"
     "23# 楼密目网破损 3 处；14# 楼外架堆料超载；16# 楼塔吊附着螺栓松动；"
     "21# 楼西侧配电箱未做二级漏电保护；22# 楼物料提升机停层门未关闭；"
     "19#、32# 楼卸料平台未挂限载牌；15# 楼临边踢脚板缺失、7# 楼临边栏杆高度不足；"
     "28# 楼模板支撑扫地杆缺失；29# 楼南侧基坑边堆土过高；13# 楼后浇带未及时封闭。\n"
     "环保：现场道路扬尘控制、裸土覆盖基本到位，未见明显遗撒。"],
    ["发现问题及处理情况",
     "共发现问题 22 项（质量 3 项、安全 18 项、其他 1 项）。\n"
     "处理：当场向施工单位指出并要求立即整改；其中 4# 楼东侧外架连墙件缺失、"
     "26#/31# 楼剪刀撑、27# 楼连墙件间距、28# 楼模板支撑扫地杆、29# 楼基坑堆土等"
     "已下发《监理通知单》，明确整改期限（11-06、11-07、11-08），"
     "要求整改回复附整改前后对比影像，整改完成后报监理复查。\n"
     "复查要求：连墙件应从底层第一步纵向水平杆处开始设置，宜靠近主节点，"
     "偏离主节点距离不应大于 300mm；剪刀撑应由底至顶连续设置，"
     "斜杆与地面倾角 45°~60°。"],
    ["巡视人", "李工"],
], widths=[3.0, 13.5])

para(doc, "备注：本日文书编号见附表一（doc 40—79 区间），同一问题存在多份重复稿，"
          "以编号最大者为准。", size=8.5, color=(0x55, 0x55, 0x55), space_after=10)

# ---------------------------------------------------------------- 11-06
doc.add_page_break()
para(doc, "二、2026 年 11 月 6 日（星期五）", size=12, bold=True, font=CN_HEAD)
para(doc, "天气：晴 26℃（现场照片水印记录）　|　单位工程：大兴路（沙盛四路等 4 条道路工程）",
     size=9, space_after=4)

make_table(doc, [
    ["巡视范围\n及主要部位", "大兴路 T 字平交路口，碎石铺设作业面"],
    ["主要施工情况", "路基碎石铺设，机械摊铺配合人工找平；现场已设置围挡与交通疏导"],
    ["质量、安全、环保等情况",
     "质量：检查碎石铺设厚度、平整度与压实情况；实测值（厚度、压实度）尚未记录，"
     "验收结论暂缺，待补充实测数据后判定。\n"
     "安全：作业区围挡完好，机械作业半径内无闲杂人员；运输车辆进出有专人指挥。\n"
     "环保：洒水降尘到位，出场车辆冲洗设施可用。"],
    ["发现问题及处理情况",
     "问题 1 项：碎石铺设实测数据未记录，无法形成验收结论。\n"
     "处理：要求施工单位补测厚度与压实度并提交记录；监理同步补充现场影像与实测值，"
     "完成后在《监理检查记录》（doc 194）中闭合。"],
    ["巡视人", "李工"],
], widths=[3.0, 13.5])

para(doc, "现场影像：1 张（sha e5a2f060…7ce38，2026-10-27 09:06 拍摄，"
          "水印含工程名称、单位工程、部位、施工内容、天气）",
     size=8.5, color=(0x55, 0x55, 0x55), space_after=10)

# ---------------------------------------------------------------- 汇总
para(doc, "三、本期汇总", size=12, bold=True, font=CN_HEAD)
make_table(doc, [
    ["日期", "巡视部位", "发现问题", "已出文书", "处理状态"],
    ["2026-11-05", "3#—32# 楼及地下室", "22 项（质量 3 / 安全 18 / 其他 1）",
     "监理通知单 6 份（其中 4#楼连墙件、26#/31# 剪刀撑、27# 连墙件间距、"
     "28# 模板支撑、29# 基坑堆土）", "整改中，期限 11-06～11-08"],
    ["2026-11-06", "大兴路 T 字平交路口", "1 项（实测数据缺失）", "监理检查记录 1 份（待补实测值）",
     "待补实测后闭合"],
], widths=[2.6, 4.6, 4.6, 4.8, 3.2], header=True)

para(doc, "", space_after=6)

# ---------------------------------------------------------------- 附表
para(doc, "附表一　巡视相关文书索引（节选）", size=11, bold=True, font=CN_HEAD)
make_table(doc, [
    ["文书号", "类型", "对象", "状态"],
    ["79", "监理通知单", "32# 楼南侧卸料平台（限载牌未挂）", "待补"],
    ["78", "监理通知单", "31# 楼西侧外架（剪刀撑缺失两道）", "待补"],
    ["76/77", "监理通知单", "30# 楼东侧外架（未设挡脚板）", "待补"],
    ["73/75", "监理通知单", "29# 楼南侧基坑（边堆土过高）", "待补"],
    ["72/74", "监理通知单", "28# 楼西侧模板支撑（扫地杆缺失）", "待补"],
    ["66/65", "监理通知单", "26# 楼西侧外架（剪刀撑未连续）", "待签"],
    ["62/63", "监理通知单", "24# 楼三层砌体（拉结筋漏设）", "待签"],
    ["194", "监理检查记录", "大兴路 T 字平交路口碎石铺设", "待补"],
], widths=[2.4, 3.6, 9.0, 2.4], header=True)

para(doc, "", space_after=4)
para(doc, "说明：", size=9.5, bold=True)
for line in [
    "1. 本表依据《建设工程监理规范》GB/T 50319 关于巡视的规定编制：监理人员对施工现场"
    "每天巡视不少于一次，记录巡视部位、施工情况、发现的问题及处理结果；当日未处理完毕的，"
    "在处理完成之日补记。",
    "2. 数据来源：2026-11-05、11-06 已出具的监理通知单 / 检查记录及现场影像，"
    "文书编号与数据库一致，可追溯。",
    "3. 表中“待补”为现场未取得的数据（如实测值、签发人、整改期限），按要求由责任人补齐后闭合，"
    "不得事后倒填。",
    "4. 影像与文书分别存于项目工作区 docs/ 与 records/photos.md，按 git 版本留痕。",
]:
    para(doc, line, size=9, space_after=2)

doc.save(OUT)
print("已生成:", OUT)

# 复核：重新打开，确认段落与表格数量、关键文字存在
d2 = Document(OUT)
texts = [p.text for p in d2.paragraphs]
all_text = "\n".join(texts) + "\n" + "\n".join(
    c.text for t in d2.tables for r in t.rows for c in r.cells)
print("段落数:", len(d2.paragraphs), "表格数:", len(d2.tables), "字符数:", len(all_text))
for key in ("监 理 巡 视 记 录", "2026 年 11 月 5 日", "连墙件间距", "大兴路", "附表一"):
    print("  含 %-12s %s" % (key, key in all_text))
