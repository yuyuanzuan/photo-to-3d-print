# Hunyuan3D-Swift 安装笔记（只装形状，不装贴图）

> 2026-09-15 实测于 MacBook Pro M1 Pro / 16GB / macOS 26.6.2

## 为什么选它

| 方案 | 能否在 M1 Pro 16GB 跑 |
|---|---|
| microsoft/TRELLIS (13.6k★) | ❌ 必须 NVIDIA 16GB 显存 + CUDA 编译 |
| AiuniAI/Unique3D | ❌ Ubuntu 22.04 + CUDA 12.1 |
| xxlong0/Wonder3D | ❌ 依赖 tiny-cuda-nn（CUDA 专用）|
| Hunyuan3D-2.1 原版 | △ 官方称支持 macOS，但安装步骤全是 CUDA torch |
| **ZimengXiong/Hunyuan3D-Swift** | ✅ **MLX/Metal 原生，实测 59.5 秒 / 2.16GB** |

## 实测性能

| 指标 | 实测 | README 宣称 |
|---|---|---|
| 耗时 | **59.5 秒** | 20.9 秒 |
| 峰值内存 (max RSS) | **2.16 GB** | ~5.6 GB |
| 峰值内存占用 | 5.06 GB | — |
| 输出网格 | 118,618 顶点 / 237,232 面 | — |

**内存只用了 2.16GB，远低于 16GB 上限。**

## 安装步骤（含三个坑）

```bash
mkdir -p ~/AI && cd ~/AI
git clone --depth 1 https://github.com/ZimengXiong/Hunyuan3D-Swift.git
cd Hunyuan3D-Swift
swift build -c release          # 174 秒
```

### 坑 1：SwiftPM 不编译 MLX 的 Metal 内核

不处理会直接报 `Failed to load the default metallib`。`mlx-swift` 锁的是 MLX **0.31.4**，
而 Python 包只有 0.31.0/1/2，取最接近的 **0.31.2**：

```bash
mkdir -p ~/AI/_mlxwheel && cd ~/AI/_mlxwheel
# mlx-metal 是独立依赖包，metallib 在里面（mlx 本体轮子只有 584KB，没有 metallib）
curl -sSL -o mlx_metal.whl "https://files.pythonhosted.org/packages/3f/69/fe3b783ebe999f3118234e1e940feb622518bfb1dea6ac5d13b1d36a8449/mlx_metal-0.31.2-py3-none-macosx_14_0_arm64.whl"
unzip -q -o mlx_metal.whl -d x
cp x/mlx/lib/mlx.metallib ~/AI/Hunyuan3D-Swift/.build/release/
```

### 坑 2：权重分四个槽位，打印只需 shape

```bash
cd ~/AI/Hunyuan3D-Swift
# 只下 shape-small 就够了（推荐档位用它）；要跑大模型再加 shape-large（4.93 GB）
for repo in hunyuan3d-mlx-shape-small; do
  d="weights/${repo#hunyuan3d-mlx-}"
  mkdir -p "$d" && cd "$d"
  for f in config.yaml model.fp16.safetensors; do
    curl -sSL -C - --retry 5 --retry-all-errors -O \
      "https://hf-mirror.com/zimengxiong/$repo/resolve/main/$f"
  done
  cd ~/AI/Hunyuan3D-Swift
done
```

**只下 shape-small。** paint（贴图）峰值要 38GB，16GB 跑不了 —— 而且**贴图对 3D 打印毫无用处**。

### 坑 3：下载会被中断，必须校验完整性

不能只看文件在不在。实测 `shape-large` 下到 83% 时目录检查通过，但文件**缺 1.44 GB**，
拿它推理会崩溃或产出垃圾。用 safetensors 头部声明的张量总长核对：

```python
import struct, json, os
p = "model.fp16.safetensors"
sz = os.path.getsize(p)
with open(p, "rb") as f:
    n = struct.unpack("<Q", f.read(8))[0]
    hdr = json.loads(f.read(n).decode("utf-8"))
mx = max(v["data_offsets"][1] for k, v in hdr.items() if k != "__metadata__")
need = 8 + n + mx          # 正确文件大小
print(sz, need, "完整" if sz >= need else f"缺 {need-sz:,} bytes")
```

服务端（`pipeline_ui/server.py`）里已内置这个校验（`safetensors_complete()`），
权重不完整时会**自动回退到可用的那个**而不是让整条链路失败。

**网络**：huggingface.co 国内直连会超时。两个办法：
- 用镜像 `https://hf-mirror.com/<repo>/resolve/main/<file>`（`curl -C -` 可续传）
- 或启动代理后直连（实测 FlClash 起来后 HF 返回 HTTP 200）

---

## 精细度档位实测（同一张输入图，M1 Pro 16GB）

**关键：精细度由 `--octree`（SDF 网格分辨率）控制，而内存几乎不涨。**

| 配置 | 顶点 | 面数 | 耗时 | 峰值内存 | **连通块** | 打印预检 |
|---|---|---|---|---|---|---|
| small + octree 256 | 157,688 | 315,372 | 59s | 2.63 GB | **1** | ✓ |
| small + octree 384 | 355,362 | 710,720 | 84s | 2.29 GB | **1** | ✓ |
| **small + octree 512** ⭐ | **632,482** | **1,264,960** | **102s** | 2.41 GB | **1** | **✓ 最佳** |
| large + octree 384 | 311,194 | 622,108 | 190s | 2.4 GB | **70** ✗ | ✗ 有碎块 |
| large + octree 512 | 558,800 | 1,117,320 | 211s | 2.43 GB | **70** ✗ | ✗ 有碎块 |

### ⚠️ 最重要的结论：shape-large 对 3D 打印反而更差

**不是"更大的模型更好"。** 实测：

- `shape-large` 无论 octree 设 384 还是 512，**输出都碎成 70 个连通块**；
  `shape-small` 始终是 **1 个完整实体**
- `shape-large` 面数还更少（112万 vs 126万）、耗时翻倍（211s vs 102s）

**原因**：`shape-large` 是 **8 步 turbo** 变体，`shape-small` 是 **30 步 CFG**。
turbo 输出更平滑简洁，但会产生大量孤立的漂浮碎块——这对 3D 打印是灾难
（要么用 `--keep-largest` 丢掉，损失细节；要么切出来一堆散件）。

**所以推荐 `shape-small` + octree 512**（126万面 / 102秒 / 单一实体 / 直接通过预检）。
这条结论是**跑实测得出的，不是照搬文档**——文档只列了 small/large 的速度内存，没说碎块问题。

### 内存从来不是瓶颈

四组配置峰值都在 **2.3~2.6 GB**，16GB 上限余量极大。可以放心拉高 octree。


## 生成 + 打印

```bash
cd ~/AI/Hunyuan3D-Swift
./.build/release/hy3d shape 输入图.png -o out/mesh.glb --weights weights/shape-small

# 接打印预检（我的脚本）
/Applications/Blender.app/Contents/MacOS/Blender --background \
  --python 3D打印预检.py -- \
  out/mesh.glb --fix --keep-largest --scale-to 100 --out out/可打印.stl
```

## 关键结果：输出网格天生可打印

| 指标 | Hunyuan3D 输出 | 视觉外壳方案（AI 多视角） |
|---|---|---|
| 封闭（可打印） | **✓ 是** | ✓（但形状失真）|
| 非流形边 | **0** | 0 |
| 破洞 | **0** | 0 |
| 连通块 | **1** | 1 |
| 退化面 | 15 / 237,232（0.006%）| — |
| 重建自洽 IoU | — | **0.644**（不合格）|
| 耗时 | **59.5 秒** | ~20 分钟 |
| API 费用 | **$0** | ~$1.2（6 个角度）|

**Hunyuan3D 的输出直接就是水密网格**——这是视觉外壳方案完全比不了的。
视觉外壳那套三轮提示词工程都没解决的"多视角几何不一致"，在单图重建这里**根本不存在**。

## 输入图片要求（重要）

Hunyuan3D 会在条件化前剥离背景。输入必须是**主体+干净背景**：
- 主体完整、居中
- 背景纯白或已抠图
- 用我流水线里的 `图像预处理.py` 先过一遍质量门正好

用脏背景的原图会失败得很惨（社区实测：平灰底上同一张蘑菇参考图，
20 步出来"漂浮薄片"，30 步出来"一个实心方块"）。

## 第三方 API 备选（本地跑不动或要更高精度时）

不需要贴图的话很便宜：

| 服务 | 模型 | 价格（无贴图）| 打印友好功能 |
|---|---|---|---|
| fal.ai | Tripo H3.1 | $0.20 | 四边面 +$0.05 |
| fal.ai | Hunyuan3D v3.1 Rapid | $0.225 | 自定义面数 +$0.15 |
| fal.ai | Meshy v6 | $0.80 | — |
| PoYo（中转）| Tripo H3.1 | $0.075 | — |
| **Tripo 直连** | 图片转 3D | **$0.20**（20 积分）| **部件补全/快速封口 $0.30**、四边面 $0.05、智能低模 $0.10 |

**Tripo 的「部件补全（快速封口）」是专门为 3D 打印做的**——本地方案如果遇到不封闭的网格，
这个功能能兜底。Tripo 也没有免费额度（"100 积分 = $1.00"是换算说明，不是赠送）。

注意：你现有的 apiyi 和 cherry 两个中转**都没有图生 3D 模型**（apiyi 293 个模型、cherry 168 个，均无）。
