#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Blender 侧几何清理：空间雕刻结果 → 干净的可打印实体

空间雕刻的输出本身已是封闭流形，但表面带有体素台阶感（marching cubes 的锯齿）。
这一步在 Blender 里做：
  1. 体素重构（Voxel Remesh）—— 把台阶面重建成均匀三角面，同时并掉重叠/自交
  2. 平滑（Smooth）—— 消除锯齿，保留整体造型
  3. 只保留最大实体 —— 去掉可能出现的孤立碎块
  4. 平底 —— 保证打印时能站稳

用法：
  blender --background --python blender_clean.py -- 输入.obj 输出.obj [体素尺寸]
"""
import bpy, bmesh, sys, os


def argv():
    a = sys.argv
    return a[a.index("--") + 1:] if "--" in a else []


def main():
    a = argv()
    if len(a) < 2:
        print("用法: blender -b --python blender_clean.py -- 输入.obj 输出.obj [体素尺寸]")
        sys.exit(1)
    src, dst = a[0], a[1]
    voxel = float(a[2]) if len(a) > 2 else 0.004

    bpy.ops.wm.read_factory_settings(use_empty=True)
    ext = os.path.splitext(src)[1].lower()
    if ext == ".obj":
        bpy.ops.wm.obj_import(filepath=src)
    elif ext == ".stl":
        bpy.ops.wm.stl_import(filepath=src)
    else:
        raise SystemExit(f"不支持的格式: {ext}")

    objs = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    if not objs:
        raise SystemExit("导入后没有网格")
    bpy.ops.object.select_all(action="DESELECT")
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]
    if len(objs) > 1:
        bpy.ops.object.join()
    obj = bpy.context.view_layer.objects.active
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)

    d0 = obj.dimensions
    print(f"输入：{len(obj.data.vertices):,} 顶点  尺寸 {d0.x:.4f}×{d0.y:.4f}×{d0.z:.4f}")

    # ① 体素重构
    m = obj.modifiers.new("Remesh", "REMESH")
    m.mode = "VOXEL"
    m.voxel_size = voxel
    m.use_smooth_shade = True
    bpy.ops.object.modifier_apply(modifier=m.name)
    print(f"  ① 体素重构（体素 {voxel}）→ {len(obj.data.vertices):,} 顶点")

    # ② 平滑（保留整体造型，只压掉锯齿）
    sm = obj.modifiers.new("Smooth", "SMOOTH")
    sm.factor = 0.5
    sm.iterations = 4
    bpy.ops.object.modifier_apply(modifier=sm.name)
    print(f"  ② 平滑 → {len(obj.data.vertices):,} 顶点")

    # ③ 只保留最大实体
    bpy.ops.object.mode_set(mode="EDIT")
    bm = bmesh.from_edit_mesh(obj.data)
    bm.verts.ensure_lookup_table()
    seen, groups = set(), []
    for v in bm.verts:
        if v.index in seen:
            continue
        g, stack = [], [v]
        while stack:
            c = stack.pop()
            if c.index in seen:
                continue
            seen.add(c.index); g.append(c)
            for e in c.link_edges:
                n = e.other_vert(c)
                if n.index not in seen:
                    stack.append(n)
        groups.append(g)
    if len(groups) > 1:
        def bbox_vol(g):
            xs = [v.co.x for v in g]; ys = [v.co.y for v in g]; zs = [v.co.z for v in g]
            return (max(xs)-min(xs)) * (max(ys)-min(ys)) * (max(zs)-min(zs))
        groups.sort(key=bbox_vol, reverse=True)
        kill = [vv for g in groups[1:] for vv in g]
        bmesh.ops.delete(bm, geom=kill, context="VERTS")
        print(f"  ③ 去掉 {len(groups)-1} 个碎块，保留最大实体")
    else:
        print("  ③ 只有 1 个连通块，无需处理")
    bmesh.update_edit_mesh(obj.data)
    bpy.ops.object.mode_set(mode="OBJECT")

    # ④ 平底（可选，把最低处切平让模型能立住）
    bpy.ops.object.mode_set(mode="EDIT")
    bm = bmesh.from_edit_mesh(obj.data)
    zmin = min(v.co.z for v in bm.verts)
    zmax = max(v.co.z for v in bm.verts)
    cut = zmin + (zmax - zmin) * 0.01          # 削掉最低 1%
    below = [v for v in bm.verts if v.co.z < cut]
    for v in below:
        v.co.z = cut
    print(f"  ④ 削平底部 1%（{len(below)} 个顶点）")
    bmesh.update_edit_mesh(obj.data)
    bpy.ops.object.mode_set(mode="OBJECT")

    d1 = obj.dimensions
    print(f"输出：{len(obj.data.vertices):,} 顶点  尺寸 {d1.x:.4f}×{d1.y:.4f}×{d1.z:.4f}")

    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    if os.path.splitext(dst)[1].lower() == ".stl":
        bpy.ops.wm.stl_export(filepath=dst, export_selected_objects=True)
    else:
        bpy.ops.wm.obj_export(filepath=dst, export_selected_objects=True)
    print(f"✓ 已输出 {dst}  ({os.path.getsize(dst):,} bytes)")


if __name__ == "__main__":
    main()
