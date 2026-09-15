#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
图像质量门 + image2 修复 + 多角度素材生成

解决的问题：
  上传的图片经常「背景没去掉」或「主体不完整（被裁切/被遮挡）」，
  这种图直接进 3D 重建链路会得到残缺或错误的模型。
  本脚本先本地判定（不花钱、不联网），只有确实不合格才调用 image2 修复，
  并且强制约束「保留主体形象细节」。

链路位置：
  照片 →【质量门】→ 不合格则 image2 修复 → image2 生成多角度 → 3D 重建 → 打印预检 → 切片

用法：
  # 只体检，不调用任何 API（本地 rembg，免费）
  python3 图像预处理.py 照片.jpg

  # 体检 + 不合格时调 image2 修复
  python3 图像预处理.py 照片.jpg --repair

  # 生成 4 个角度，作为 3D 重建的中间链路素材
  python3 图像预处理.py 照片.jpg --repair --angles 4 --out-dir 输出/

环境：本脚本用 .venv-rembg（含 rembg / pillow / numpy / fal_client）
  需要 fal.ai API Key 才能用 --repair / --angles：
    改 ~/.fal_key（本脚本会读）或设环境变量 FAL_KEY
"""
import argparse, base64, io, json, os, sys

# ── 阈值（本地判定用，可按你的素材调整） ──────────────────
TH = {
    "alpha_coverage_max": 0.92,   # 主体占画面超过这个比例 → 背景几乎没去掉
    "alpha_coverage_min": 0.06,   # 主体太小 → 细节不足，3D 重建会糊
    "bg_std_max": 0.055,          # 背景区域标准差超过这个值 → 背景有内容（没抠干净）
    "bg_white_min": 0.88,         # 背景亮度高于此值视为"白底"
    "border_frac_max": 0.02,      # 主体触边像素占比超过此值 → 主体被裁切
    "specks_max": 3,              # 连通碎块数超过此值 → 抠图脏
}

ANGLES = [
    ("front",    "a straight-on front view"),
    ("left45",   "a 45-degree view from the subject's left front"),
    ("right45",  "a 45-degree view from the subject's right front"),
    ("left90",   "a full left side profile view"),
    ("right90",  "a full right side profile view"),
    ("back",     "a straight-on back view"),
]

# 保形象的强制约束（放在每条指令最前面，模型对开头更敏感）
IDENTITY_LOCK = (
    "Keep the subject's identity and appearance EXACTLY unchanged: same facial features, "
    "same head shape, same hair style and color, same skin tone, same body proportions, "
    "same clothing design and colors, same materials and textures. "
    "Do NOT beautify, do NOT restyle, do NOT change age, do NOT change identity. "
    "Only perform the requested operation."
)


def load_fal_key():
    for p in (os.path.expanduser("~/.fal_key"), os.path.expanduser("~/.config/fal/key")):
        if os.path.exists(p):
            k = open(p).read().strip()
            if k:
                return k
    return os.environ.get("FAL_KEY", "").strip()


def to_data_uri(path):
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp"}.get(ext, "png")
    with open(path, "rb") as f:
        return f"data:image/{mime};base64," + base64.b64encode(f.read()).decode()


# ══════════════════════════════════════════════════════════
#  质量门：本地判定（免费、离线）
# ══════════════════════════════════════════════════════════
def quality_gate(path):
    import numpy as np
    from PIL import Image
    from rembg import remove, new_session

    img = Image.open(path)
    has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
    rgb = img.convert("RGB")
    a = np.array(rgb).astype(np.float32) / 255.0

    session = new_session("u2net")
    fg = remove(img.convert("RGBA"), session=session)
    alpha = np.array(fg)[:, :, 3].astype(np.float32) / 255.0
    H, W = alpha.shape
    mask = alpha > 0.5
    cov = float(mask.mean())

    # 背景区域（非主体）在原图里的"内容量"
    bgm = ~mask
    bg_region = a[bgm] if bgm.sum() > 100 else np.zeros((1, 3), np.float32)
    bg_std = float(bg_region.std()) if len(bg_region) else 0.0
    bg_bright = float(bg_region.mean()) if len(bg_region) else 1.0

    # 主体是否触边（被裁切）
    # 注意：不要用「触边像素占比」——掩膜沿边缘只有很窄一条，占比天然很低，
    # 实测一个被裁掉三边的主体才 0.53%，远低于任何合理阈值，会漏判。
    # 稳定判据是「掩膜包围盒是否顶到画面边界」。
    ys, xs = np.where(mask)
    touch = []
    if len(xs):
        if xs.min() <= 2:         touch.append("左")
        if xs.max() >= W - 3:     touch.append("右")
        if ys.min() <= 2:         touch.append("上")
        if ys.max() >= H - 3:     touch.append("下")
    b = 2
    border_px = 0
    for sl in (np.s_[:b, :], np.s_[-b:, :], np.s_[:, :b], np.s_[:, -b:]):
        border_px += int(mask[sl].sum())
    border_frac = border_px / max(1, int(mask.sum()))

    # 碎块数量
    try:
        from scipy import ndimage
        lbl, n = ndimage.label(mask)
        sizes = ndimage.sum(mask, lbl, range(1, n + 1)) if n else []
        specks = int(sum(1 for s in sizes if s < mask.sum() * 0.01))
    except Exception:
        specks = 0

    problems = []
    if cov > TH["alpha_coverage_max"]:
        problems.append(("背景未去除", f"主体占画面 {cov*100:.0f}%，几乎没有独立背景区域"))
    if cov < TH["alpha_coverage_min"]:
        problems.append(("主体过小", f"主体仅占 {cov*100:.1f}%，重建细节会不足"))
    if bg_std > TH["bg_std_max"] and bg_bright < TH["bg_white_min"]:
        problems.append(("背景脏", f"背景区域标准差 {bg_std:.3f}（阈值 {TH['bg_std_max']}），说明背景仍有内容"))
    if touch:
        problems.append(("主体被裁切", f"主体顶到画面「{'/'.join(touch)}」边，说明被裁掉了一部分"))
    if specks > TH["specks_max"]:
        problems.append(("抠图有碎块", f"检出 {specks} 个零散碎块"))

    return {
        "输入有 alpha 通道": has_alpha,
        "尺寸": f"{W}×{H}",
        "主体占比": round(cov, 4),
        "背景标准差": round(bg_std, 4),
        "背景平均亮度": round(bg_bright, 4),
        "主体触边比例": round(border_frac, 4),
        "触边侧": touch,
        "零散碎块": specks,
        "问题": problems,
        "通过": len(problems) == 0,
    }


def print_gate(r):
    print(f"\n{'='*58}\n质量门（本地判定，未调用任何 API）\n{'='*58}")
    for k in ("尺寸", "输入有 alpha 通道", "主体占比", "背景标准差",
              "背景平均亮度", "触边侧", "零散碎块"):
        v = r[k]
        print(f"  {k:<14}: {'（无，主体完整）' if k == '触边侧' and not v else v}")
    if r["通过"]:
        print("  → 结论        : ✓ 通过，可直接进入下一步")
    else:
        print(f"  → 结论        : ✗ 不合格，需要 image2 接管")
        for name, detail in r["问题"]:
            print(f"      · {name}：{detail}")


# ══════════════════════════════════════════════════════════
#  image2 调用（fal.ai  openai/gpt-image-2/edit）
# ══════════════════════════════════════════════════════════
def call_image2(prompt, image_paths, size="1024x1024", timeout=600):
    key = load_fal_key()
    if not key:
        raise RuntimeError(
            "没有 fal.ai API Key。请把 Key 写入 ~/.fal_key，或设置环境变量 FAL_KEY。\n"
            "  申请地址：https://fal.ai/dashboard/keys"
        )
    try:
        import fal_client
    except ImportError:
        raise RuntimeError("缺少 fal_client。请用本目录的 .venv-rembg 运行本脚本。")

    client = fal_client.SyncClient(key=key)
    urls = []
    for p in image_paths:
        urls.append(client.upload_file(p))   # 上传到 fal CDN 换成 URL
    args = {"prompt": prompt, "image_urls": urls, "image_size": size}
    res = client.subscribe("openai/gpt-image-2/edit", arguments=args)
    return res


def extract_urls(res):
    """从 fal 返回里取出图片 URL，兼容几种返回结构"""
    out = []
    if isinstance(res, dict):
        for k in ("images", "image", "output"):
            v = res.get(k)
            if isinstance(v, list):
                for it in v:
                    if isinstance(it, dict) and it.get("url"):
                        out.append(it["url"])
                    elif isinstance(it, str):
                        out.append(it)
            elif isinstance(v, dict) and v.get("url"):
                out.append(v["url"])
            elif isinstance(v, str):
                out.append(v)
    return out


def download(url, dest):
    import urllib.request
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    urllib.request.urlretrieve(url, dest)
    return dest


def build_repair_prompt(problems, r):
    """根据质量门检出的问题，拼一条保形象 + 针对性修复的指令"""
    tasks = []
    names = [p[0] for p in problems]
    if "背景未去除" in names or "背景脏" in names or "抠图有碎块" in names:
        tasks.append(
            "Remove the background COMPLETELY and place the subject on a plain, "
            "uniform pure white background (RGB 255,255,255). No shadows, no props, "
            "no scenery, no text."
        )
    if "主体被裁切" in names:
        tasks.append(
            "The subject is cut off by the image border. Naturally extend and complete "
            "the missing parts (head, body, limbs, edges of the object) so the whole "
            "subject is fully visible with clear margin around it. The extension must "
            "match the visible parts exactly in style, color and proportion."
        )
    if "主体过小" in names:
        tasks.append(
            "Reframe so the subject fills most of the frame, while keeping the entire "
            "subject visible with a small margin."
        )
    if not tasks:
        tasks.append("Clean up the image: remove the background and make the subject complete.")
    return IDENTITY_LOCK + "\n\nTask:\n- " + "\n- ".join(tasks) + \
        "\n\nOutput a single square image, subject centered."


def build_angle_prompt(angle_desc):
    return (
        IDENTITY_LOCK + "\n\nTask:\n"
        f"Render the SAME subject from {angle_desc}. This is a multi-view turnaround for 3D "
        "reconstruction, so the geometry must stay consistent with the input across all views. "
        "Plain pure white background, even lighting, no shadows on the background, "
        "full subject visible and centered, same scale as the input view."
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src", help="输入图片")
    ap.add_argument("--repair", action="store_true", help="不合格时调用 image2 修复")
    ap.add_argument("--force-repair", action="store_true", help="无论是否合格都调 image2 修复")
    ap.add_argument("--angles", type=int, default=0, help="生成几个角度的中间链路素材（0=不生成）")
    ap.add_argument("--size", default="1024x1024", help="输出尺寸，如 1024x1024")
    ap.add_argument("--out-dir", default=None, help="输出目录（默认与输入同级的 <名字>_预处理/）")
    ap.add_argument("--gate-only", action="store_true", help="只做本地质量门判定")
    args = ap.parse_args()

    if not os.path.exists(args.src):
        raise SystemExit(f"找不到文件：{args.src}")

    out_dir = args.out_dir or os.path.join(
        os.path.dirname(os.path.abspath(args.src)),
        os.path.splitext(os.path.basename(args.src))[0] + "_预处理")
    os.makedirs(out_dir, exist_ok=True)

    # ── 1. 本地质量门 ──
    r = quality_gate(args.src)
    print_gate(r)

    if args.gate_only:
        print(json.dumps({"通过": r["通过"], "问题": [p[0] for p in r["问题"]]},
                         ensure_ascii=False))
        return

    key = load_fal_key()
    if not key:
        print("\n⚠️ 没有 fal.ai API Key —— 跳过 image2 的修复与多角度生成。")
        print("   写入方式：  echo '你的key' > ~/.fal_key")
        print("   申请地址：  https://fal.ai/dashboard/keys")
        print("   （质量门是纯本地的，上面的判定结果有效）")
        return

    source = args.src

    # ── 2. 不合格 → image2 修复 ──
    if (args.repair and not r["通过"]) or args.force_repair:
        prompt = build_repair_prompt(r["问题"], r)
        print(f"\n{'='*58}\n调用 image2 修复（openai/gpt-image-2/edit）\n{'='*58}")
        print("  指令要点：先锁死形象一致性，再执行针对性修复")
        try:
            res = call_image2(prompt, [args.src], size=args.size)
            urls = extract_urls(res)
            if not urls:
                print("  ✗ 返回里没找到图片 URL：", json.dumps(res, ensure_ascii=False)[:300])
            else:
                dest = os.path.join(out_dir, "修复后.png")
                download(urls[0], dest)
                print(f"  ✓ 已下载：{dest}  ({os.path.getsize(dest):,} bytes)")
                # 复检
                r2 = quality_gate(dest)
                print_gate(r2)
                source = dest
        except Exception as e:
            print(f"  ✗ 调用失败：{e}")
            return

    # ── 3. 生成多角度中间链路素材 ──
    if args.angles > 0:
        n = min(args.angles, len(ANGLES))
        print(f"\n{'='*58}\n生成 {n} 个角度（3D 重建的中间链路素材）\n{'='*58}")
        print(f"  基准图：{os.path.basename(source)}")
        print("  注意：每个角度都从【同一张基准图】生成，而不是链式迭代——")
        print("        链式生成会让误差累积，越往后越不像本人。")
        for name, desc in ANGLES[:n]:
            prompt = build_angle_prompt(desc)
            try:
                res = call_image2(prompt, [source], size=args.size)
                urls = extract_urls(res)
                if not urls:
                    print(f"  ✗ {name}: 未返回图片")
                    continue
                dest = os.path.join(out_dir, f"角度_{name}.png")
                download(urls[0], dest)
                print(f"  ✓ {name:<8} {os.path.getsize(dest):>9,} bytes  → {dest}")
            except Exception as e:
                print(f"  ✗ {name}: {e}")

    # ── 4. 汇总清单 ──
    files = sorted(f for f in os.listdir(out_dir) if f.lower().endswith((".png", ".jpg")))
    if files:
        manifest = os.path.join(out_dir, "素材清单.json")
        with open(manifest, "w", encoding="utf-8") as f:
            json.dump({"基准图": os.path.basename(source),
                       "角度素材": files,
                       "质量门结果": {k: v for k, v in r.items() if k != "问题"},
                       "说明": "角度素材供 3D 重建（图生3D / 多视角重建）使用"},
                      f, ensure_ascii=False, indent=2)
        print(f"\n{'='*58}\n输出目录：{out_dir}\n{'='*58}")
        for f in files:
            print(f"  {f}")
        print(f"  {os.path.basename(manifest)}")


if __name__ == "__main__":
    main()
