#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hunyuan3D 输入整理

为什么必须做这一步：
  Hunyuan3D 会在条件化之前剥离背景。如果喂进去的图背景不干净，
  失败方式非常剧烈 —— 社区实测：同一张平灰底蘑菇参考图，
  20 步出来「漂浮的薄片」，30 步出来「一个实心方块」。
  抠图后贴纯白并裁切，同一张图就正常了。

  另一个原因：输入里的背景会被当成几何重建出来。

本脚本做的事（顺序照抄 blender-hunyuan3d-mac 的 prep_image.py）：
  1. 抠出主体（已有 alpha 就直接用，否则跑 rembg）
  2. 合成到纯白背景
  3. 裁到主体包围盒
  4. 补成正方形，四边留 ~8% 边距
  5. 缩放到 768×768

用法：
  python3 hy3d准备.py 输入.png 输出.png
"""
import os
import sys


def tight_bbox(alpha_img, thresh=25):
    """稳健的主体包围盒（阈值化，避免 alpha 里的零星噪点撑大边框）"""
    import numpy as np
    a = np.array(alpha_img)[:, :, 3]
    ys, xs = np.where(a > thresh)
    if len(xs) == 0:
        return None
    return (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)


def prepare(src, dst, size=768, margin=0.08):
    from PIL import Image
    import numpy as np

    img = Image.open(src)
    # 1. 取主体 alpha —— 已有就直用（合成图/已抠图），否则跑 rembg
    alpha = None
    if img.mode in ("RGBA", "LA"):
        cand = np.array(img.convert("RGBA"))[:, :, 3].astype(np.float32) / 255.0
        if cand.min() < 0.5 and cand.max() > 0.5 and 0.005 < cand.mean() < 0.995:
            alpha = img.convert("RGBA")
            how = "已有 alpha"
    if alpha is None:
        from rembg import remove, new_session
        global _S
        try:
            s = _S
        except NameError:
            s = _S = new_session("u2net")
        alpha = remove(img.convert("RGBA"), session=s)
        how = "rembg 抠图"

    # 2. 合成到纯白
    white = Image.new("RGBA", alpha.size, (255, 255, 255, 255))
    white.alpha_composite(alpha)
    flat = white.convert("RGB")

    # 3. 裁到主体
    bb = tight_bbox(alpha)
    if bb:
        flat = flat.crop(bb)
        subj_w, subj_h = flat.size
    else:
        subj_w, subj_h = flat.size

    # 4. 补成正方形，四边留 8% 边距
    side = int(round(max(subj_w, subj_h) / (1 - 2 * margin)))
    canvas = Image.new("RGB", (side, side), (255, 255, 255))
    canvas.paste(flat, ((side - subj_w) // 2, (side - subj_h) // 2))

    # 5. 缩放到 768
    canvas = canvas.resize((size, size), Image.LANCZOS)
    canvas.save(dst)

    nonwhite = float((np.array(canvas).min(axis=2) < 245).mean())
    return {"方式": how, "原尺寸": img.size, "主体尺寸": (subj_w, subj_h),
            "输出": dst, "输出尺寸": canvas.size, "主体占比": round(nonwhite, 4)}


def main():
    if len(sys.argv) < 3:
        print("用法: python3 hy3d准备.py 输入 输出 [尺寸]")
        sys.exit(1)
    src, dst = sys.argv[1], sys.argv[2]
    size = int(sys.argv[3]) if len(sys.argv) > 3 else 768
    if not os.path.exists(src):
        raise SystemExit(f"找不到输入：{src}")
    info = prepare(src, dst, size=size)
    print(f"整理完成：{info['方式']} | 主体 {info['主体尺寸']} → 输出 {info['输出尺寸']} "
          f"| 主体占比 {info['主体占比']*100:.1f}%")
    print(f"  {dst}")


if __name__ == "__main__":
    main()
