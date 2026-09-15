#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
提示词模板（可编辑、可视化、可校验）

设计原则：
  · 提示词不写死在代码里 —— 抽成 JSON 数据，Web 页面里可读可改
  · 「主体是什么」由使用者填写，用 {subject} 占位符注入到各条提示词
  · 组装结果可实时预览：跑之前就能看到最终发给模型的完整文本

空间雕刻成败取决于三件事，提示词必须钉死：
  ① 旋转轴的画面位置（重建假设「画面中心 = 旋转轴」）
  ② 统一尺度（1 单位 = 同样像素数）
  ③ 垂直位置与高度（转台不改变物体高度）
"""
import json, os

HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(HERE, "pipeline_ui", "prompts.json")

DEFAULT = {
    "subject": "主体",
    "identity_lock": (
        "严格保持「{subject}」的身份与外观完全不变：形状、轮廓、比例、颜色、材质、纹理全部一致；"
        "若是有生命的对象，五官、脸型、发型发色、肤色也必须一致。"
        "禁止美颜、禁止换风格、禁止瘦身、禁止改年龄、禁止做任何改变。只执行下面要求的操作。"
    ),
    "turntable_lock": (
        "相机与构图 —— 这是精确的转台拍摄，不是插画创作：\n"
        "1. 相机绕着「{subject}」水平环绕，始终位于它的半高处平视，不仰视、不俯视、不侧倾、不改变高度。\n"
        "2. 使用正交投影（或极长焦远距离拍摄），「{subject}」不得出现透视缩短变形。\n"
        "3. 旋转轴必须始终位于画面的水平中心。不要按本视角自己的轮廓重新居中、也不要重新构图"
        "—— 「{subject}」的轮廓宽度随视角变化是正常的，不要去「修正」它。\n"
        "4. 【比例尺锁定 —— 最重要的一条】所有视角必须使用同一个比例尺："
        "现实中的同一段长度，在每个视角里都必须对应相同的像素数。"
        "绝对不要为了让「{subject}」塞进画面而整体缩小它。"
        "「{subject}」的高度和垂直位置也必须与输入图一致：顶部和底部落在相同的像素行。\n"
        "   ⚠️ 说明：若某个视角下「{subject}」比较宽（例如细长物体的侧面），"
        "宁可让它靠近画面左右边缘、甚至略有留白不均，也**绝不能缩小**。"
        "保持比例尺正确比保持留白均匀重要得多。\n"
        "5. 「{subject}」四周的留白量与输入图保持一致。\n"
        "6. 背景为纯白均匀背景 RGB(255,255,255)。不要阴影、不要倒影、不要地面、不要场景、"
        "不要道具、不要文字、不要边框。\n"
        "7. 光照方向和强度与输入图一致，不要让明暗显示出换了灯光。\n"
        "8. 「{subject}」必须完整可见、全部位于画面内，任何部分都不得被裁切。"
    ),
    "negative_view": (
        "禁止增加、删除或虚构「{subject}」的任何部分。禁止改变它的比例。"
        "禁止改变缩放或裁剪方式。禁止改变透视。禁止左右镜像「{subject}」。"
        "禁止输出拼图、网格、分屏，或在一张图里放多个视角。"
    ),
    "view_line": (
        "要求的视角：渲染「{subject}」的{angle_desc}。"
        "这张图将作为 3D 重建转台序列中的一帧，因此它的轮廓必须与真实相机在该位置拍到的完全一致。"
    ),
    "angle_specs": [
        {"name": "front",   "az": 0.0,
         "desc": "正视图（0°：相机位于「{subject}」的正前方，与输入图相同）"},
        {"name": "left45",  "az": 45.0,
         "desc": "左前四分之三视图（45°：相机向「{subject}」自身的左侧移动 45°，"
                 "因此能看到它的左侧面和一部分正面）"},
        {"name": "right45", "az": 315.0,
         "desc": "右前四分之三视图（45°，反方向：相机向「{subject}」自身的右侧移动 45°，"
                 "看到它的右侧面和一部分正面）"},
        {"name": "left90",  "az": 90.0,
         "desc": "正左侧视图（90°：相机位于「{subject}」自身的正左方，看到它的左侧面；"
                 "它的正面此时朝向画面右侧）"},
        {"name": "right90", "az": 270.0,
         "desc": "正右侧视图（90°，反方向：相机位于「{subject}」自身的正右方；"
                 "它的正面此时朝向画面左侧）"},
        {"name": "back",    "az": 180.0,
         "desc": "背视图（180°：相机位于「{subject}」的正后方）"},
    ],
    "repair_tasks": {
        "背景": ("彻底移除背景，替换为纯白均匀背景 RGB(255,255,255)。"
                 "不要阴影、不要倒影、不要地面、不要道具、不要场景、不要文字、不要边框。"),
        "裁切": ("「{subject}」被画面边缘裁切了。请自然、完整地重建缺失的部分"
                 "（头部、身体、四肢，或物体的边缘），使「{subject}」完整可见。"
                 "补出来的部分必须与可见部分在风格、颜色、比例、材质上完全一致。"),
        "过小": ("重新构图，让「{subject}」占满画面的大部分区域，"
                 "同时保持完整可见、四周留白均匀。"),
    },
    "repair_framing": (
        "构图：「{subject}」居中于方形画幅，完整可见，不触碰任何边缘，四边留白均匀。"
        "正交/长焦观感，无透视变形。背景保持纯白。"
    ),
}


def load():
    if os.path.exists(STORE):
        try:
            d = json.load(open(STORE, encoding="utf-8"))
            out = json.loads(json.dumps(DEFAULT))     # 深拷贝默认值
            out.update(d)                             # 用已保存的覆盖
            return out
        except Exception:
            pass
    return json.loads(json.dumps(DEFAULT))


def save(d):
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    with open(STORE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    return STORE


def _fill(t, subject):
    """
    只替换 {subject} 占位符，不要用 str.format()。

    原因：同一段文本里可能还有别的占位符（例如 view_line 里既有 {subject}
    又有 {angle_desc}）。format() 遇到没传的参数会整体抛错，
    结果是连 {subject} 都没被替换（实测踩过）。
    另外用户在界面里手写大括号也不会引发异常。
    """
    return (t or "").replace("{subject}", subject)


def build_angle_prompt(tpl, angle_name, subject=None):
    subject = (subject or tpl.get("subject") or "主体").strip()
    spec = next((s for s in tpl["angle_specs"] if s["name"] == angle_name), None)
    if spec is None:
        spec = {"desc": f"the {angle_name} view"}
    # 注意顺序：必须先把 subject 填进 angle_desc，再把它塞进 view_line。
    # 否则 desc 里残留的 {subject} 会原样出现在最终提示词里（踩过）。
    desc = _fill(spec["desc"], subject)
    view = _fill(tpl["view_line"], subject).replace("{angle_desc}", desc)
    return (
        _fill(tpl["identity_lock"], subject) + "\n\n"
        + _fill(tpl["turntable_lock"], subject) + "\n\n"
        + view + "\n\n" + _fill(tpl["negative_view"], subject)
    )


def build_repair_prompt(tpl, problems, subject=None):
    subject = (subject or tpl.get("subject") or "主体").strip()
    names = [p[0] for p in problems] if problems else []
    keys = []
    if {"背景未去除", "背景脏", "抠图有碎块"} & set(names):
        keys.append("背景")
    if "主体被裁切" in names:
        keys.append("裁切")
    if "主体过小" in names:
        keys.append("过小")
    if not keys:
        keys = ["背景"]
    tasks = [_fill(tpl["repair_tasks"][k], subject) for k in keys if k in tpl["repair_tasks"]]
    return (_fill(tpl["identity_lock"], subject) + "\n\nTASK:\n- " + "\n- ".join(tasks)
            + "\n\n" + _fill(tpl["repair_framing"], subject))


if __name__ == "__main__":
    t = load()
    bar = "=" * 74
    print(bar + "\n【修复提示词】主体被裁切 + 背景未去除\n" + bar)
    print(build_repair_prompt(t, [("主体被裁切", ""), ("背景未去除", "")], subject="一个陶瓷马克杯"))
    for s in t["angle_specs"]:
        print("\n" + bar + f"\n【多角度】{s['name']} ({s['az']}°)\n" + bar)
        print(build_angle_prompt(t, s["name"], subject="一个陶瓷马克杯"))
