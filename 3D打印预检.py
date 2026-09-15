#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
3D 打印预检 + 修复

为什么需要这一步：
  AI 生成（图生3D / 多视角重建）出来的网格几乎都**不能直接打印**，典型毛病：
    · 非流形边（一条边接 3 个以上面）→ 切片软件报错
    · 破洞（边界边）→ 模型不是封闭体，切片出来是空壳
    · 零厚度面片 → 打印出来一碰就碎
    · 多个不连通的碎块 → 打印时零件乱飞
    · 自相交 → 切片路径错乱
    · 没有平底 → 打出来站不住
  本脚本先体检、再按需修复，最后导出 STL。

用法（Blender 后台运行）：
  blender --background --python 3D打印预检.py -- 模型.glb
  blender --background --python 3D打印预检.py -- 模型.glb --fix --out 修好的.stl
  blender --background --python 3D打印预检.py -- 模型.glb --fix --solidify 1.5 --base
"""
import bpy, bmesh, sys, os, math
from mathutils import Vector

EPS_AREA = 1e-9


def argv():
    a = sys.argv
    return a[a.index("--") + 1:] if "--" in a else []


def parse():
    a = argv()
    if not a:
        print("用法: blender --background --python 3D打印预检.py -- 模型文件 [--fix] [--out x.stl]")
        print("     额外: --solidify 毫米数   --base   --keep-largest")
        print("           --scale-to 毫米（按最长边缩放）  --hollow 壁厚mm（掏空）")
        print("     切片: --slice --printer \"Bambu Lab A1\" [--nozzle 0.4] [--layer 0.20]")
        print("           [--filament \"Bambu PLA Basic\"] [--gcode-out 目录]")
        sys.exit(1)
    o = {"src": a[0], "fix": False, "out": None, "solidify": None,
         "base": False, "scale_to": None, "hollow": None, "keep_largest": False,
         "slice": False, "printer": "Bambu Lab A1", "nozzle": "0.4",
         "layer": "0.20", "filament": "Bambu PLA Basic", "gcode_out": None}
    i = 1
    while i < len(a):
        t = a[i]
        if t == "--fix": o["fix"] = True
        elif t == "--base": o["base"] = True
        elif t == "--keep-largest": o["keep_largest"] = True
        elif t == "--slice": o["slice"] = True
        elif t == "--out": i += 1; o["out"] = a[i]
        elif t == "--solidify": i += 1; o["solidify"] = float(a[i])
        elif t == "--scale-to": i += 1; o["scale_to"] = float(a[i])
        elif t == "--hollow": i += 1; o["hollow"] = float(a[i])
        elif t == "--printer": i += 1; o["printer"] = a[i]
        elif t == "--nozzle": i += 1; o["nozzle"] = a[i]
        elif t == "--layer": i += 1; o["layer"] = a[i]
        elif t == "--filament": i += 1; o["filament"] = a[i]
        elif t == "--gcode-out": i += 1; o["gcode_out"] = a[i]
        i += 1
    return o


def import_model(path):
    """按扩展名导入模型，返回所有网格对象"""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    ext = os.path.splitext(path)[1].lower()
    if ext in (".glb", ".gltf"):   bpy.ops.import_scene.gltf(filepath=path)
    elif ext == ".obj":            bpy.ops.wm.obj_import(filepath=path)
    elif ext == ".stl":            bpy.ops.wm.stl_import(filepath=path)
    elif ext == ".fbx":            bpy.ops.import_scene.fbx(filepath=path)
    elif ext == ".ply":            bpy.ops.wm.ply_import(filepath=path)
    else: raise SystemExit(f"不支持的格式: {ext}")
    return [o for o in bpy.context.scene.objects if o.type == "MESH"]


def join_all(objs):
    """多碎块合并成一个对象，便于统一处理"""
    bpy.ops.object.select_all(action="DESELECT")
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]
    if len(objs) > 1:
        bpy.ops.object.join()
    return bpy.context.view_layer.objects.active


def analyse(obj):
    """用 bmesh 直接体检。返回指标字典。"""
    me = obj.data
    bm = bmesh.new()
    bm.from_mesh(me)
    bm.edges.ensure_lookup_table(); bm.faces.ensure_lookup_table()

    r = {}
    r["顶点"] = len(bm.verts); r["面"] = len(bm.faces); r["边"] = len(bm.edges)

    # 非流形边：连接的face数 != 2
    nonman = [e for e in bm.edges if len(e.link_faces) not in (2,)]
    r["非流形边"] = len([e for e in nonman if len(e.link_faces) > 2])
    # 边界边（=1个面）：破洞
    r["边界边(破洞)"] = len([e for e in bm.edges if len(e.link_faces) == 1])
    # 退化面 —— 必须用「相对」阈值，不能用绝对面积。
    # 踩过的坑：原来固定用 1e-9，模型缩放后面积整体变小，正常三角面也被判成退化面
    #（实测缩放到 100mm 后误报 4208 个，而 Bambu Studio 判定 manifold = yes）。
    vs = [v.co for v in bm.verts]
    if vs:
        xs = [v.x for v in vs]; ys = [v.y for v in vs]; zs = [v.z for v in vs]
        diag = ((max(xs)-min(xs))**2 + (max(ys)-min(ys))**2 + (max(zs)-min(zs))**2) ** 0.5
    else:
        diag = 1.0
    rel = max(diag * diag * 1e-12, 1e-18)     # 相对 1e-12 倍对角线平方
    r["退化面"] = len([f for f in bm.faces if f.calc_area() < rel])
    if bm.faces:
        r["极小面"] = len([f for f in bm.faces if f.calc_area() < rel * 1e3])

    # 连通块数量
    seen = set(); shells = 0
    for v in bm.verts:
        if v.index in seen: continue
        shells += 1
        stack = [v]
        while stack:
            cur = stack.pop()
            if cur.index in seen: continue
            seen.add(cur.index)
            for e in cur.link_edges:
                o = e.other_vert(cur)
                if o.index not in seen: stack.append(o)
    r["连通块"] = shells

    # 体积（闭合网格才有意义）
    try:
        vol = bm.calc_volume(signed=False)
        r["体积"] = vol
        r["体积(mm³)"] = vol * 1e9 if obj.scale.x == 1 else vol * 1e9
    except Exception:
        r["体积"] = 0.0
    r["封闭"] = (r["边界边(破洞)"] == 0 and r["非流形边"] == 0)

    bm.free()

    # 尺寸（世界坐标，假设 1 blender unit = 1 m）
    bb = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    xs = [p.x for p in bb]; ys = [p.y for p in bb]; zs = [p.z for p in bb]
    r["尺寸mm"] = (round((max(xs)-min(xs))*1000, 1),
                   round((max(ys)-min(ys))*1000, 1),
                   round((max(zs)-min(zs))*1000, 1))
    return r


def report(r, title):
    print(f"\n{'='*56}\n{title}\n{'='*56}")
    print(f"  规模        : {r['顶点']} 顶点 / {r['边']} 边 / {r['面']} 面")
    print(f"  尺寸(宽×深×高): {r['尺寸mm'][0]} × {r['尺寸mm'][1]} × {r['尺寸mm'][2]} mm")
    print(f"  封闭(可打印) : {'✓ 是' if r['封闭'] else '✗ 否'}")
    print(f"  非流形边     : {r['非流形边']}  {'✓' if r['非流形边']==0 else '✗ 需清理'}")
    print(f"  破洞(边界边) : {r['边界边(破洞)']}  {'✓' if r['边界边(破洞)']==0 else '✗ 需补洞'}")
    _dr = r['退化面'] / max(r['面'], 1)
    print(f"  退化面       : {r['退化面']}  "
          f"{'✓' if r['退化面']==0 else ('✓ 占比低' if _dr <= 0.001 else '✗ 需删除')}")
    print(f"  连通块       : {r['连通块']}  {'✓' if r['连通块']<=1 else '✗ 有碎块需合并/删除'}")
    v = r.get("体积", 0)
    if v > 0:
        # 1 blender unit = 1m → m³ → mm³
        print(f"  体积         : {v*1e9:,.1f} mm³  (实体约 {v*1e9*0.001:.1f} cm³)")
    # 退化面按「占比」判断：AI 生成的网格几乎总有几个零面积三角面（<0.1%），
    # 切片软件能正常处理。只有占比明显偏高才当成阻塞问题。
    ratio = r["退化面"] / max(r["面"], 1)
    issues = []
    if r["非流形边"]: issues.append("非流形边")
    if r["边界边(破洞)"]: issues.append("破洞")
    if ratio > 0.001: issues.append(f"退化面({ratio*100:.2f}%)")
    elif r["退化面"]:
        print(f"  注            : 退化面 {r['退化面']} 个（占 {ratio*100:.3f}%，低于阈值，切片可正常处理）")
    if r["连通块"] > 1: issues.append(f"{r['连通块']}个碎块")
    print(f"  → 结论      : {'✗ 不能直接打印：' + '、'.join(issues) if issues else '✓ 通过预检'}")
    return issues


def fix_all(obj, opt):
    """按需修复：清理 → 补洞 → 加厚 → 加底座"""
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="EDIT")
    bm = bmesh.from_edit_mesh(obj.data)

    # 1) 删退化面（相对阈值）。迭代多轮 —— 删面会让相邻面变成新的退化面。
    def _diag():
        vs = [v.co for v in bm.verts]
        if not vs: return 1.0
        return (((max(v.x for v in vs)-min(v.x for v in vs))**2 +
                 (max(v.y for v in vs)-min(v.y for v in vs))**2 +
                 (max(v.z for v in vs)-min(v.z for v in vs))**2) ** 0.5)
    total = 0
    for _ in range(4):
        lim = max(_diag()**2 * 1e-12, 1e-18)
        degen = [f for f in bm.faces if f.calc_area() < lim]
        if not degen:
            break
        bmesh.ops.delete(bm, geom=degen, context="FACES")
        total += len(degen)
    if total:
        print(f"  · 已删除 {total} 个退化面（迭代清理）")

    # 2) 删重复顶点
    before = len(bm.verts)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-5)
    if len(bm.verts) != before:
        print(f"  · 已合并重复顶点 {before} → {len(bm.verts)}")

    # 3) 补洞（把边界边围成的环填上）
    bm.edges.ensure_lookup_table()
    boundary = [e for e in bm.edges if len(e.link_faces) == 1]
    if boundary:
        try:
            bmesh.ops.holes_fill(bm, edges=boundary, sides=0)
            print(f"  · 已尝试补洞（{len(boundary)} 条边界边）")
        except Exception as e:
            print(f"  · 补洞失败: {e}")

    # 4) 重算法线
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bmesh.update_edit_mesh(obj.data)
    bpy.ops.object.mode_set(mode="OBJECT")

    # 5) 加厚（零厚度面片 → 实体）
    if opt["solidify"]:
        m = obj.modifiers.new("Solidify", "SOLIDIFY")
        m.thickness = opt["solidify"] / 1000.0   # mm → m
        m.offset = 0
        print(f"  · 已加厚 {opt['solidify']} mm")

    # 6) 掏空
    if opt["hollow"]:
        m = obj.modifiers.new("Hollow", "SOLIDIFY")
        m.thickness = -opt["hollow"] / 1000.0
        m.offset = 1
        print(f"  · 已掏空，壁厚 {opt['hollow']} mm")

    # 逐个应用修改器（注意：bpy.ops 是动态命名空间，hasattr 永远为 True，
    # 不能用 hasattr 探测操作符是否存在）
    for m in list(obj.modifiers):
        try:
            bpy.ops.object.modifier_apply(modifier=m.name)
        except Exception as e:
            print(f"  · 应用修改器 {m.name} 失败: {e}")

    # 7) 只保留最大实体（去掉漂浮碎屑——AI 网格常见的"零件乱飞"）
    if opt.get("keep_largest"):
        bpy.ops.object.mode_set(mode="EDIT")
        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        seen, groups = set(), []
        for v in bm.verts:
            if v.index in seen:
                continue
            grp, stack = [], [v]
            while stack:
                cur = stack.pop()
                if cur.index in seen:
                    continue
                seen.add(cur.index); grp.append(cur)
                for e in cur.link_edges:
                    o = e.other_vert(cur)
                    if o.index not in seen:
                        stack.append(o)
            groups.append(grp)
        if len(groups) > 1:
            # 用「包围盒体积」判断哪块最大。
            # 不要用 bmesh.ops.volume：它对非封闭/非流形几何会抛异常，
            # 一旦 fallback 成顶点数，顶点密集的小球就会被误判成最大块（实测踩过）。
            def bbox_vol(g):
                xs = [v.co.x for v in g]; ys = [v.co.y for v in g]; zs = [v.co.z for v in g]
                return (max(xs) - min(xs)) * (max(ys) - min(ys)) * (max(zs) - min(zs))

            groups.sort(key=bbox_vol, reverse=True)
            dropped = [round(bbox_vol(g) ** (1/3) * 1000, 1) for g in groups[1:]]
            kill = [vv for g in groups[1:] for vv in g]
            bmesh.ops.delete(bm, geom=kill, context="VERTS")
            print(f"  · 已移除 {len(groups)-1} 个碎块（边长 "
                  f"{'/'.join(str(d) for d in dropped)} mm），保留最大实体")
        bmesh.update_edit_mesh(obj.data)
        bpy.ops.object.mode_set(mode="OBJECT")

    # 8) 加平底（打出来能站稳）
    if opt["base"]:
        bpy.ops.object.mode_set(mode="EDIT")
        bm = bmesh.from_edit_mesh(obj.data)
        zs = [v.co.z for v in bm.verts]
        zmin = min(zs)
        bottom = [f for f in bm.faces if all(abs(v.co.z - zmin) < 1e-4 for v in f.verts)]
        if bottom:
            r = bmesh.ops.extrude_face_region(bm, geom=bottom)
            nv = [e for e in r["geom"] if isinstance(e, bmesh.types.BMVert)]
            bmesh.ops.translate(bm, verts=nv, vec=(0, 0, -0.002))  # 下延 2mm 底座
            print(f"  · 已加 {2} mm 平底座")
        bmesh.update_edit_mesh(obj.data)
        bpy.ops.object.mode_set(mode="OBJECT")


def export(obj, path, unit_scale=True):
    """
    导出。
    注意单位：STL 本身不带单位，切片软件（Bambu Studio / PrusaSlicer）一律按 **毫米** 解释坐标。
    Blender 默认 1 单位 = 1 米，所以 20mm 的物体坐标只有 0.02 —— 直接导出会被切片软件
    当成 0.02mm（肉眼不可见的灰尘）。因此导出前统一 ×1000，把「米」换算成「毫米」。
    """
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj

    if unit_scale:
        s = 1000.0
        obj.scale = (obj.scale.x * s, obj.scale.y * s, obj.scale.z * s)
        bpy.ops.object.transform_apply(scale=True)
        d = obj.dimensions
        print(f"  · 单位换算 ×1000（米→毫米），导出尺寸 "
              f"{d.x:.1f} × {d.y:.1f} × {d.z:.1f} mm")

    ext = os.path.splitext(path)[1].lower()
    if ext == ".stl":
        bpy.ops.wm.stl_export(filepath=path, export_selected_objects=True)
    elif ext in (".obj",):
        bpy.ops.wm.obj_export(filepath=path, export_selected_objects=True)
    elif ext in (".glb", ".gltf"):
        bpy.ops.export_scene.gltf(filepath=path, use_selection=True)
    else:
        bpy.ops.wm.stl_export(filepath=path)
    return path


def slice_with_bambu(stl_path, opt):
    """
    调用 Bambu Studio 命令行切片。
    配置来自 App 自带资源目录：profiles/BBL/{machine,process,filament}/*.json
    """
    import subprocess, glob, shutil
    # 路径从 config.py 读（可用 config.json / 环境变量覆盖）
    try:
        import config as CFG
        BS, RES = CFG.BAMBU_BIN, CFG.BAMBU_RES
    except ImportError:
        BS = os.environ.get("BAMBU_BIN",
                            "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio")
        RES = os.environ.get("BAMBU_RESOURCES",
                             "/Applications/BambuStudio.app/Contents/Resources/profiles/BBL")
    if not os.path.exists(BS):
        print(f"\n✗ 没找到 Bambu Studio：{BS}")
        print("  请确认已安装（不能从 DMG 直接运行），或改路径。")
        return None

    def pick(sub, want):
        d = os.path.join(RES, sub)
        if not os.path.isdir(d):
            return None
        exact = os.path.join(d, want + ".json")
        if os.path.exists(exact):
            return exact
        # 模糊：找包含关键字的第一个
        key = want.split("@")[0].strip()
        cands = sorted(glob.glob(os.path.join(d, f"*{key}*{opt['printer'].replace('Bambu Lab ','')}*.json")))
        return cands[0] if cands else None

    mach = pick("machine", f"{opt['printer']} {opt['nozzle']} nozzle")
    proc = pick("process", f"{opt['layer']}mm Standard @BBL {opt['printer'].replace('Bambu Lab ','')}")
    fil  = pick("filament", f"{opt['filament']} @BBL {opt['printer'].replace('Bambu Lab ','')}")

    print(f"\n{'='*56}\nBambu Studio 切片\n{'='*56}")
    for label, p in (("机型", mach), ("层高", proc), ("材料", fil)):
        print(f"  {label}: {os.path.basename(p) if p else '✗ 未找到'}")

    outdir = opt["gcode_out"] or os.path.join(os.path.dirname(os.path.abspath(stl_path)), "切片输出")
    os.makedirs(outdir, exist_ok=True)
    args = [BS, "--load-settings", f"{mach};{proc}", "--load-filaments", fil,
            "--slice", "0", "--outputdir", outdir, stl_path]
    r = subprocess.run(args, capture_output=True, text=True, timeout=900)
    gcodes = glob.glob(os.path.join(outdir, "*.gcode"))
    if not gcodes:
        print("  ✗ 切片失败，输出末尾：")
        for ln in (r.stdout or "").splitlines()[-12:]:
            if "[trace]" not in ln and "[debug]" not in ln:
                print("   ", ln)
        return None
    for g in gcodes:
        # 从 G-code 头部提取打印信息
        info = {}
        with open(g, encoding="utf-8", errors="ignore") as f:
            for i, ln in enumerate(f):
                if i > 60: break
                for k in ("model printing time", "total estimated time", "filament_type",
                          "layer_height", "nozzle_diameter", "printer_model"):
                    if ln.startswith(f"; {k}") and k not in info:
                        info[k] = ln.split("=", 1)[-1].strip() if "=" in ln else ln.split(":", 1)[-1].strip()
        print(f"  ✓ {os.path.basename(g)}  ({os.path.getsize(g):,} bytes)")
        for k, v in info.items():
            print(f"      {k}: {v}")
    return gcodes


def main():
    opt = parse()
    if not os.path.exists(opt["src"]):
        raise SystemExit(f"找不到文件: {opt['src']}")
    objs = import_model(opt["src"])
    if not objs:
        raise SystemExit("导入后没有网格对象")
    obj = join_all(objs)
    obj.name = "模型"
    if len(objs) > 1:
        print(f"已合并 {len(objs)} 个对象")

    before = analyse(obj)
    issues = report(before, "预检结果（修复前）")

    if opt["fix"] and issues:
        print(f"\n{'='*56}\n开始修复\n{'='*56}")
        fix_all(obj, opt)
        after = analyse(obj)
        report(after, "修复后复检")
    elif opt["fix"]:
        print("\n预检无问题，无需修复")

    if opt["scale_to"]:
        # 按最长边缩放到指定毫米。
        # 必须用「当前」尺寸——修复/去碎块之后尺寸会变，
        # 拿修复前的尺寸当基准会导致缩放完全错（实测踩过：想缩到 40mm 结果成了 4.1mm）。
        cur = analyse(obj)
        d = cur["尺寸mm"]
        cur_max = max(d)
        if cur_max > 0:
            s = opt["scale_to"] / cur_max
            obj.scale = (obj.scale.x * s, obj.scale.y * s, obj.scale.z * s)
            bpy.ops.object.transform_apply(scale=True)
            print(f"\n已缩放：最长边 {cur_max} mm → {opt['scale_to']} mm")
            report(analyse(obj), "缩放后")

    # 切片时若没指定导出路径，自动导出一个临时 STL（单位已换算成毫米）
    if opt["slice"] and not opt["out"]:
        opt["out"] = os.path.join(os.path.dirname(os.path.abspath(opt["src"])),
                                  os.path.splitext(os.path.basename(opt["src"]))[0] + "_待切片.stl")

    if opt["out"]:
        p = os.path.abspath(opt["out"])
        export(obj, p)
        print(f"\n✓ 已导出: {p}  ({os.path.getsize(p):,} bytes)")

    if opt["slice"]:
        # 切片前必须已导出为 STL
        final = opt["out"]
        if not os.path.exists(final):
            print("\n✗ 切片需要先导出 STL")
        else:
            # 模型是封闭的才值得切片
            a = analyse(obj) if opt["fix"] else before
            if not a["封闭"]:
                print("\n⚠️ 模型不是封闭实体，切片可能失败或切出空壳——建议先加 --fix --solidify")
            slice_with_bambu(final, opt)


if __name__ == "__main__":
    main()
