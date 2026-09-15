# 照片 → 3D → 打印

**Photo → 3D → 3D-Printable STL**，全本地跑在 Apple Silicon 上，不需要云端图生 3D 服务。

拍一张照片，约 **100 秒**后拿到一个**封闭流形、可直接切片**的 STL 文件。

```
📤上传 → 🔍质量门 → 🎨AI修复(可选) → 🖼️整理输入 → 🧊Hunyuan3D重建 → 🩺打印预检 → 📦交付STL
```

有一个带实时进度的 Web UI，也能纯命令行跑。

---

## 为什么是这个方案

做这个之前，我先把主流路线都试过一遍，最后留下的这条不是"看起来最先进"的，而是**唯一能在消费级 Mac 上产出可打印文件**的。

### 试过并放弃的：AI 生成多视角 + 空间雕刻

思路是：用图像模型生成转台多视角图，再用剪影交集（visual hull）雕出几何。

**算法本身没问题** —— 我用 Blender 渲染的**几何一致**多视角做真值验证，重建自洽 IoU 达到 **0.992**。

**但 AI 生成的视角之间几何不一致**，实测：

| 输入 | 高度偏差 | 重建 IoU | 结果 |
|---|---|---|---|
| 几何一致的真实视角 | 0.0% | **0.992** | 可直接打印 |
| AI 生成的视角 | **50.2%** | **0.644** | 碎成 11 块 |

三轮提示词工程（英文 → 中文 → 加"比例尺锁定"）＋ 换横向画幅，**都改不动**：
生成模型把每张图当独立构图，不保留跨视角的比例关系。输入矛盾时，任何重建算法都救不回来。

**更要紧的是"多视角这一步本身没必要"** —— 单图重建模型直接出网格，根本不存在视角一致性问题。

### 现在用的：Hunyuan3D-Swift（MLX/Metal）

| | 多视角+空间雕刻 | **Hunyuan3D 单图重建** |
|---|---|---|
| 耗时 | ~20 分钟 | **100 秒** |
| API 费用 | ~$1.2/次 | **$0** |
| 网格 | IoU 0.644，形状失真 | **封闭流形，直接可打印** |
| 依赖 | 云端额度 | **纯本地** |

选 [Hunyuan3D-Swift](https://github.com/ZimengXiong/Hunyuan3D-Swift) 的原因：**它是 MLX/Metal 原生移植**，
不是 CUDA 转译。其余主流方案在 Mac 上都跑不起来——

| 方案 | 能否在 M1 Pro 16GB 跑 |
|---|---|
| microsoft/TRELLIS | ❌ 必须 NVIDIA 16GB 显存 + CUDA 编译 |
| AiuniAI/Unique3D | ❌ Ubuntu + CUDA 12.1 |
| xxlong0/Wonder3D | ❌ 依赖 tiny-cuda-nn（CUDA 专用）|
| Hunyuan3D-2.1 原版 | △ 官方称支持 macOS，但安装步骤全是 CUDA torch |
| **Hunyuan3D-Swift** | ✅ **实测 100 秒 / 2.4GB** |

---

## 实测数据（MacBook Pro M1 Pro / 16GB）

### 精细度档位

**关键：精细度由 `--octree`（SDF 网格分辨率）控制，而内存几乎不涨。**

| 配置 | 顶点 | 面数 | 耗时 | 峰值内存 | **连通块** | 预检 |
|---|---|---|---|---|---|---|
| small + octree 256 | 157,688 | 315,372 | 59s | 2.63 GB | 1 | ✓ |
| small + octree 384 | 355,362 | 710,720 | 84s | 2.29 GB | 1 | ✓ |
| **small + octree 512** ⭐ | **632,482** | **1,264,960** | **102s** | 2.41 GB | **1** | **✓** |
| large + octree 384 | 311,194 | 622,108 | 190s | 2.4 GB | **70** ✗ | ✗ |
| large + octree 512 | 558,800 | 1,117,320 | 211s | 2.43 GB | **70** ✗ | ✗ |

### ⚠️ 一个反直觉的结论：更大的模型对 3D 打印反而更差

`shape-large` 是 **8 步 turbo** 变体，`shape-small` 是 **30 步 CFG**。实测结果：

- `shape-large` 无论 octree 设 384 还是 512，**输出都碎成 70 个连通块**
- `shape-small` 始终是 **1 个完整实体**，直接通过打印预检
- `shape-large` 面数还更少（112 万 vs 126 万）、耗时翻倍（211s vs 102s）

所以**默认推荐 `shape-small` + octree 512**。这条是跑实测得出的，不是照搬文档——
上游文档只列了 small/large 的速度内存，没说碎块问题。

### 全链路耗时

| 步骤 | 耗时 |
|---|---|
| 质量门检测 | 1~2s |
| AI 修复（按需） | 跳过或 ~60s |
| 整理输入 | 1~2s |
| **Hunyuan3D 重建** | **59~102s** |
| 打印预检与定尺 | 6~11s |
| **合计** | **约 70~120 秒** |

---

## 前置依赖

| 依赖 | 用途 | 必须 |
|---|---|---|
| **macOS 14+ / Apple Silicon** | MLX 只在 Apple Silicon 上跑 | ✅ |
| [Hunyuan3D-Swift](https://github.com/ZimengXiong/Hunyuan3D-Swift) | 单图生 3D（MLX） | ✅ |
| [Blender](https://www.blender.org/) 4.x/5.x | 网格体检、修复、定尺、导出 | ✅ |
| Xcode / Swift 工具链 | 编译 MLX 引擎 | ✅ |
| [Bambu Studio](https://bambulab.com/download) | 切片（也可以在界面里手动做） | 可选 |
| 任意图像模型 API | 仅「AI 修复」用（主体被裁切时补全） | 可选 |

**Hunyuan3D-Swift 的安装有个大坑**：SwiftPM 不编译 MLX 的 Metal 内核，
直接 `swift build` 出来的二进制会报 `Failed to load the default metallib`。
完整绕过方法见 **[docs/hunyuan3d-setup.md](docs/hunyuan3d-setup.md)**（含 metallib 提取、权重下载、完整性校验）。

---

## 快速开始

```bash
git clone <this-repo> && cd photo-to-3d-print

# 1. 配置本机路径
cp config.example.json config.json
$EDITOR config.json              # 按需改 Blender / Hunyuan3D 路径
python3 config.py                # 自检：所有依赖是否就位

# 2. 装 Python 依赖（只用图像处理部分）
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. 装 Hunyuan3D-Swift（见 docs/hunyuan3d-setup.md）
#    简版：
#    git clone https://github.com/ZimengXiong/Hunyuan3D-Swift ~/AI/Hunyuan3D-Swift
#    cd ~/AI/Hunyuan3D-Swift && swift build -c release
#    curl -sSL -O https://huggingface.co/zimengxiong/hunyuan3d-mlx-shape-small/resolve/main/{config.yaml,model.fp16.safetensors}
#    # 别忘了补 mlx.metallib，否则跑不起来

# 4. 起服务
.venv/bin/python pipeline_ui/server.py
# 浏览器打开 http://127.0.0.1:8765
```

### 命令行用法

```bash
# 只跑重建 + 打印预检
~/AI/Hunyuan3D-Swift/.build/release/hy3d shape 照片.png -o mesh.glb \
  --weights ~/AI/Hunyuan3D-Swift/weights/shape-small --octree 512

python3 3D打印预检.py mesh.glb --fix --keep-largest --scale-to 100 --out 可打印.stl

# 体检报告 + 直接切片
python3 3D打印预检.py mesh.glb --fix --scale-to 100 --out out.stl \
  --slice --printer "Bambu Lab A1" --gcode-out 切片/
```

参数：`--fix` 修复 · `--solidify 毫米` 加厚 · `--keep-largest` 只留最大实体
· `--base` 加平底 · `--hollow 毫米` 掏空 · `--scale-to 毫米` 按最长边缩放

---

## Web UI

- **上传图片 → 填「主体是什么」→ 开始**，8 步流程图实时显示状态、耗时、详情
- **重建精细度**可选四档（默认「超高」= 126 万面 / 100 秒）
- 提示词面板可读可改（「主体是什么」会注入到所有提示词）
- **历史任务持久化**：刷新页面、重启服务都不丢，点任意一条可回看
- 产物可随时下载，STL 可一键在 Bambu Studio 中打开
- **API Key 只在服务端读取**，前端只知道「有没有」

---

## 项目结构

```
photo-to-3d-print/
├── config.py                  # 集中配置（环境变量 > config.json > 默认值）
├── config.example.json
├── pipeline_ui/
│   ├── server.py              # 流水线编排 + Web UI 后端（纯标准库 HTTP）
│   ├── blender_clean.py       # 体素重构 + 平滑 + 去碎块 + 平底
│   └── static/index.html      # 前端（零构建，原生 JS）
├── hy3d准备.py                # 抠图 → 纯白底 → 裁主体 → 补方 → 768
├── 3D打印预检.py              # 流形体检 + 修复 + 定尺 + 切片
├── 图像预处理.py              # 质量门（本地判定，不调 API）
├── 提示词模板.py              # 形象锁 / 转台约束 / 修复指令模板
└── docs/
    └── hunyuan3d-setup.md     # Hunyuan3D-Swift 安装笔记（含三个坑）
```

---

## 设计要点

### 1. 「整理输入」这步不能省

Hunyuan3D 会在条件化前剥离背景。喂脏背景图**失败得非常剧烈**——
社区实测：同一张平灰底蘑菇参考图，20 步出「漂浮薄片」，30 步出「一个实心方块」。
另一个原因：**输入里的背景会被当成几何一起重建出来**。

所以固定做：抠图 → 贴纯白 → 裁主体 → 补方形 8% 边距 → 768。
实测效果：网格从 23.7 万面涨到 31.5 万面，细节明显更多。

### 2. AI 修复只在必要时调

质量门检出「背景脏 / 背景未去除 / 抠图有碎块」时**不调 API** —— 本地抠图就解决了。
只有「主体被裁切」「主体过小」这种生成式才能修的问题才调用图像模型。

```
跳过 AI 修复：检出「背景脏」，这些由「整理输入」本地处理，不消耗 API
```

### 3. 尺寸单位必须交叉验证

STL 不带单位，切片软件一律按**毫米**解释坐标。Blender 默认 1 单位 = 1 米，
所以 20mm 的物体坐标只有 0.02 —— 直接导出会被当成 **0.02mm**（肉眼不可见的灰尘）。

导出时统一 ×1000，并**用 Bambu Studio 反查确认**（`--info` 报的尺寸必须和预期一致）。
同理也不能重复换算：已导出的 STL 再导入再 ×1000 会变成 90 米、超出打印体积。

### 4. 每个"看起来合理"的假设都验过

举几个踩过的坑：

- **退化面判据不能用绝对面积**：模型缩放后正常三角面会被误判（实测误报 4208 个，
  而 Bambu Studio 判定 `manifold = yes`）。改成相对包围盒对角线的阈值后是 15 个（真实值）。
- **判断"哪块最大"不能用 `bmesh.ops.volume`**：它对非封闭几何会抛异常，
  fallback 成顶点数后，顶点密集的小球会被误判为最大块（症状：想缩到 40mm 结果成了 4.1mm）。
  改用包围盒体积。
- **`hasattr` 对 `bpy.ops` 永远返回 True**（动态命名空间），不能用它探测操作符是否存在。

---

## 已知限制

- **单图重建看不到背面**，深度靠推测 —— 成品比例会有偏差（实测高度偏大 ~30%）。
  要更准需多角度输入或专业摄影测量。
- **凹面丢失**：单图重建对杯口内腔、腋下空隙这类结构还原不了。
- **只支持 Apple Silicon**：MLX 依赖 Metal，Intel Mac / Windows / Linux 跑不了
  （可改用 Hunyuan3D 官方 PyTorch 版 + CUDA）。
- 贴图（paint）部分**未集成**：峰值需 38GB 内存，且对 3D 打印无用。
- 面数越高切片越慢：126 万面的模型切片约 10 秒、G-code 可达几十 MB。

---

## 许可

MIT。Hunyuan3D 模型权重与算法移植遵循其各自许可，见上游仓库。

## 致谢

- [ZimengXiong/Hunyuan3D-Swift](https://github.com/ZimengXiong/Hunyuan3D-Swift) — MLX/Metal 移植
- [Tencent-Hunyuan/Hunyuan3D-2](https://github.com/Tencent-Hunyuan/Hunyuan3D-2) — 原模型
- [danielgatis/rembg](https://github.com/danielgatis/rembg) — 抠图
- [Blender](https://www.blender.org/) · [Bambu Studio](https://bambulab.com/download)
