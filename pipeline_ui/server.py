#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
照片 → 3D → 打印 流水线 · Web UI 后端

启动：
  python3 pipeline_ui/server.py
  浏览器打开 http://127.0.0.1:8765

依赖路径与 API 凭据全部从 config.py 读（见 config.example.json）。

设计要点：
  · 纯标准库 HTTP 服务，Web 框架零依赖（rembg/PIL/scikit-image 从 .venv 里来）
  · 进度用 SSE 推送，前端实时看到走到哪一步
  · 上传走原始二进制 + query 参数传文件名 —— HTTP 头只能是 latin-1，
    直接塞中文名会变乱码（踩过：测试照片.png → æµ_è__äººç__.png）
  · 每个任务独立目录 outputs/<job_id>/，产物可单独下载
  · 任务状态落盘到 outputs/<id>/job.json，刷新页面/重启服务都不丢
"""
import base64, json, os, re, shutil, struct, subprocess, sys, threading, time, traceback
import urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                 # 仓库根目录
sys.path.insert(0, ROOT)
import config as CFG                         # ⚠ 必须早于任何 CFG.* 的使用（曾因放在
                                             #   第 62 行导致启动即 NameError）
UPLOADS = os.path.join(HERE, "uploads")
OUTPUTS = os.path.join(HERE, "outputs")
PORT = int(os.environ.get("PIPELINE_PORT", "8765"))
# 监听地址：0.0.0.0 = 局域网可访问；127.0.0.1 = 仅本机。
# 可以用 config.json 的 listen_host 或环境变量 PIPELINE_HOST 覆盖。
HOST = CFG.get("listen_host", "0.0.0.0", "PIPELINE_HOST")

# 本地 Hunyuan3D（MLX/Metal，单图生 3D）。路径全部来自 config.py，
# 换机器只改 config.json 或设环境变量即可（见 config.example.json）
HY3D_BIN = CFG.HY3D_BIN
HY3D_W_SMALL = CFG.HY3D_W_SMALL
HY3D_W_LARGE = CFG.HY3D_W_LARGE

# 精细度档位：--octree 是 SDF 网格分辨率，直接决定面数。
# 实测（M1 Pro 16GB，shape-small）：
#   256 → 31.5 万面 / 59 秒 / 2.6GB
#   384 → 71.1 万面 / 84 秒 / 2.3GB
#   512 → 126.5 万面 / 102 秒 / 2.4GB   ← 内存几乎不涨，可以放心拉高
QUALITY_PRESETS = {
    "standard": {"label": "标准", "octree": 256, "model": "small",
                 "faces": "31万", "secs": 60},
    "high":     {"label": "高",   "octree": 384, "model": "small",
                 "faces": "71万", "secs": 85},
    "ultra":    {"label": "超高", "octree": 512, "model": "small",
                 "faces": "126万", "secs": 102},
    # ⚠️ shape-large 是 8 步 turbo 变体：实测无论 octree 384/512，
    #    输出都会碎成 70 个连通块（shape-small 只有 1 个）。
    #    对 3D 打印来说反而更差 —— 要靠 --keep-largest 丢掉碎块，会损失细节。
    #    面数也更少（111万 vs 126万）、耗时翻倍（211s vs 102s）。
    #    保留这个档位是给"要更平滑造型、不在意碎片"的场景用。
    "max":      {"label": "大模型(turbo)", "octree": 512, "model": "large",
                 "faces": "112万(碎70块)", "secs": 211},
}
os.makedirs(UPLOADS, exist_ok=True)
os.makedirs(OUTPUTS, exist_ok=True)
import importlib.util as _ilu
def _load_pt():
    spec = _ilu.spec_from_file_location("提示词模板", os.path.join(ROOT, "提示词模板.py"))
    m = _ilu.module_from_spec(spec); spec.loader.exec_module(m); return m
PT = _load_pt()

# ── 步骤定义（前端流程图按这个渲染） ──────────────────────
STEPS = [
    {"id": "upload",   "name": "上传图片",       "icon": "📤", "desc": "选择要处理的照片"},
    {"id": "gate",     "name": "质量门检测",     "icon": "🔍", "desc": "本地判定背景/主体是否合格"},
    {"id": "fix",      "name": "AI 修复",        "icon": "🎨", "desc": "不合格时保形象修复（可选）"},
    {"id": "prep",     "name": "整理输入",       "icon": "🖼️", "desc": "抠图→纯白底→裁切→补方→768"},
    {"id": "recon",    "name": "Hunyuan3D 重建", "icon": "🧊", "desc": "本地 MLX 单图生 3D（约 60 秒）"},
    {"id": "preflight","name": "打印预检与定尺", "icon": "🩺", "desc": "流形体检 + 尺寸归一"},
    {"id": "deliver",  "name": "交付 STL",       "icon": "📦", "desc": "最终可打印文件（切片由你在 Bambu Studio 完成）"},
]
JOBS = {}          # job_id -> state
LOCK = threading.Lock()


def load_key_for(provider):
    """按 provider 名取 API Key。只在服务端使用，不下发前端。见 config.py"""
    return CFG.provider_key(provider)


def load_system_base(provider=None):
    """provider 的 base URL。见 config.py"""
    if provider:
        return CFG.provider_base(provider)
    return next(iter(CFG.PROVIDERS.values()), {}).get("base", "")


def load_system_key(provider=None):
    """
    取「已配置 Key 的第一个 provider」的 Key（provider 指定时取该 provider）。

    历史说明：这里以前读的是本机的 ~/.dsh 私有凭据文件，属于个人环境耦合。
    现在凭据统一由 config.py 提供（config.json 的 providers.<name>.key，
    或对应环境变量），公开仓库里不含任何个人路径与密钥。
    """
    if provider:
        return load_key_for(provider)
    for name in CFG.PROVIDERS:
        k = CFG.provider_key(name)
        if k:
            return k
    return ""


# provider 与模型清单都从 config 读（公开仓库里保持通用，私人中转放 config.json）
PROVIDERS = {k: {"label": v.get("label", k), "base": v.get("base", "")}
             for k, v in CFG.PROVIDERS.items()}

# 每个模型: (provider名, 模型id, 显示名)
AVAILABLE_MODELS = [tuple(m) for m in CFG.get("models", [
    ("openai", "gpt-image-1", "GPT-Image-1"),
])]


def now():
    return time.strftime("%H:%M:%S")


def new_job(job_id, src_path):
    st = {
        "id": job_id, "src": src_path, "status": "created",
        "name": os.path.basename(src_path),
        "steps": {s["id"]: {"status": "pending", "detail": "", "t0": None, "t1": None}
                  for s in STEPS},
        "log": [], "files": [], "gate": None, "error": None,
        "events": [], "done": False,
    }
    with LOCK:
        JOBS[job_id] = st
    return st


def save_job(job_id):
    """把任务状态写到 outputs/<id>/job.json —— 让刷新页面、甚至重启服务后都还能看到历史"""
    st = JOBS.get(job_id)
    if not st:
        return
    d = os.path.join(OUTPUTS, job_id)
    os.makedirs(d, exist_ok=True)
    try:
        with open(os.path.join(d, "job.json"), "w", encoding="utf-8") as f:
            json.dump({k: v for k, v in st.items() if k != "events"},
                      f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def load_jobs():
    """服务启动时把历史任务读回内存"""
    if not os.path.isdir(OUTPUTS):
        return
    for name in sorted(os.listdir(OUTPUTS)):
        p = os.path.join(OUTPUTS, name, "job.json")
        if not os.path.isfile(p):
            continue
        try:
            st = json.load(open(p, encoding="utf-8"))
            st.setdefault("events", [])
            st["done"] = True          # 磁盘上的都视为已结束
            st["interrupted"] = True   # 标记：不是本次进程跑的
            JOBS.setdefault(name, st)
        except Exception:
            pass


def emit(job_id, step_id=None, status=None, detail=None, log=None, files=None):
    st = JOBS.get(job_id)
    if not st:
        return
    with LOCK:
        if step_id and status:
            s = st["steps"].get(step_id)
            if s is None:
                # 步骤 id 不存在时只记日志，绝不抛异常。曾经的 bug：
                # emit(jid, "import", ...) 引用了不存在的步骤，导致每次点
                # 「在 Bambu Studio 中打开」都 KeyError → 500。
                st["log"].append(f"[{now()}] ⚠ 未知步骤 {step_id}（状态更新已忽略）")
            else:
                s["status"] = status
                if detail is not None:
                    s["detail"] = detail
                if status == "running" and not s.get("t0"):
                    s["t0"] = time.time()
                if status in ("done", "failed", "skipped"):
                    s["t1"] = time.time()
        if log:
            st["log"].append(f"[{now()}] {log}")
        if files is not None:
            st["files"] = files
        st["events"].append({"t": time.time(), "step": step_id, "status": status})
    save_job(job_id)
    return st


def list_files(job_id):
    d = os.path.join(OUTPUTS, job_id)
    out = []
    if os.path.isdir(d):
        for f in sorted(os.listdir(d)):
            p = os.path.join(d, f)
            if os.path.isfile(p):
                out.append({"name": f, "size": os.path.getsize(p),
                            "ext": os.path.splitext(f)[1].lower().lstrip(".")})
    return out


# ══════════════════════════════════════════════════════════
#  各步骤实现
# ══════════════════════════════════════════════════════════
def step_gate(job_id, src, outdir, cfg):
    emit(job_id, "gate", "running", "本地分析中…")
    import 图像预处理 as ip
    r = ip.quality_gate(src)
    with LOCK:
        JOBS[job_id]["gate"] = {k: v for k, v in r.items() if k != "问题"}
        JOBS[job_id]["gate"]["problems"] = [p[0] for p in r["问题"]]
        JOBS[job_id]["gate"]["details"] = [p[1] for p in r["问题"]]
    if r["通过"]:
        emit(job_id, "gate", "done",
             f"通过 · 主体占比 {r['主体占比']*100:.1f}% · 背景标准差 {r['背景标准差']:.3f}",
             log="质量门通过，无需修复")
    else:
        emit(job_id, "gate", "done",
             "不合格 · " + "、".join(p[0] for p in r["问题"]),
             log="质量门判定不合格：" + "；".join(f"{n}({d})" for n, d in r["问题"]))
    return r


# 只有这些问题需要生成式修复；「背景脏 / 背景未去除 / 抠图有碎块」交给本地的
# 整理输入那一步（rembg 抠图 + 纯白底）就够了，不必花 API 钱。
NEEDS_GENERATIVE = {"主体被裁切", "主体过小"}


def step_fix(job_id, src, outdir, gate, cfg):
    problems = {p[0] for p in gate.get("问题", [])}
    need = problems & NEEDS_GENERATIVE
    if not need and not cfg.get("force_fix"):
        if problems:
            emit(job_id, "fix", "skipped",
                 f"本地可解决（{'、'.join(sorted(problems))}）",
                 log=f"跳过 AI 修复：检出「{'、'.join(sorted(problems))}」，"
                     f"这些由「整理输入」本地处理，不消耗 API")
        else:
            emit(job_id, "fix", "skipped", "质量门通过，无需修复", log="跳过 AI 修复")
        return src
    emit(job_id, "fix", "running", f"{'、'.join(sorted(need))} · 调用 {cfg['model']} …")
    if not cfg.get("api_key"):
        emit(job_id, "fix", "failed", "缺少 API Key", log="✗ 未配置 API Key，无法修复")
        return src
    emit(job_id, "fix", "running", f"调用 {cfg['model']} …")
    import 图像预处理 as ip          # 修复后复检要用 quality_gate
    # 提示词由模板组装（可在 Web 页面里改），主体描述由使用者提供
    prompt = PT.build_repair_prompt(cfg.get("prompts") or PT.load(),
                                    gate.get("问题", []),
                                    subject=cfg.get("subject"))
    try:
        img = call_image_api(cfg, prompt, [src])
        dest = os.path.join(outdir, "01_修复后.png")
        open(dest, "wb").write(img)
        r2 = ip.quality_gate(dest)
        emit(job_id, "fix", "done",
             f"已修复 · 复检{'通过' if r2['通过'] else '仍有问题'}",
             log=f"AI 修复完成（{cfg['model']}），复检{'通过' if r2['通过'] else '仍有问题'}",
             files=list_files(job_id))
        return dest
    except Exception as e:
        emit(job_id, "fix", "failed", str(e)[:120], log=f"✗ AI 修复失败：{e}")
        return src


def call_image_api(cfg, prompt, images):
    """调 OpenAI 兼容的图像编辑接口，返回图片二进制"""
    key = cfg["api_key"]
    model = cfg["model"]
    base = cfg.get("base_url") or load_system_base(cfg.get("provider"))
    if not base:
        raise RuntimeError("未配置 provider 的 base_url，见 config.json")

    boundary = "----dshpipeline" + str(int(time.time() * 1000))
    parts = []
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\n{model}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="prompt"\r\n\r\n{prompt}\r\n'.encode("utf-8"))
    if cfg.get("size"):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="size"\r\n\r\n{cfg["size"]}\r\n'.encode())
    for p in images:
        fn = os.path.basename(p)
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{fn}"\r\n'
            f'Content-Type: image/png\r\n\r\n'.encode() + open(p, "rb").read() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)

    req = urllib.request.Request(f"{base.rstrip('/')}/images/edits", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            d = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", "ignore")
        except Exception:
            pass
        msg = raw
        try:
            j = json.loads(raw); err = j.get("error", j)
            msg = err.get("localized_message") or err.get("message") or raw
            if "quota" in str(err.get("code", "")) or "quota" in str(msg):
                mm = re.search(r"quota\s*\[(\d+)\]\s*preConsumedQuota\s*\[(\d+)\]", str(err.get("message", "")))
                msg = (f"额度不足：本次预扣 {mm.group(2)}，当前剩余 {mm.group(1)}。请充值或换额度更低的模型。"
                       if mm else f"额度不足：{msg}")
        except Exception:
            pass
        raise RuntimeError(f"[{e.code}] {msg[:300] or '接口无返回内容'}")
    if "error" in d:
        err = d["error"]
        raise RuntimeError((err.get("localized_message") or err.get("message") or str(err))[:300])
    item = d["data"][0]
    if item.get("b64_json"):
        return base64.b64decode(item["b64_json"])
    if item.get("url"):
        with urllib.request.urlopen(item["url"], timeout=300) as r:
            return r.read()
    raise RuntimeError("接口未返回图片数据")


def safetensors_complete(path):
    """
    校验 safetensors 是否完整。

    不能只看文件存不存在 —— 下载被中断会留下一个"看起来存在"的残缺文件，
    拿它去推理会崩溃或产出垃圾。这里读文件头的 JSON，算出张量数据应有的总长度，
    再和实际文件大小比对。（实测踩过：shape-large 下到 83% 时目录检查通过，文件却缺 1.44GB）
    """
    if not os.path.isfile(path):
        return False, "文件不存在"
    try:
        with open(path, "rb") as f:
            raw = f.read(8)
            if len(raw) < 8:
                return False, "文件过短"
            n = struct.unpack("<Q", raw)[0]
            if n == 0 or n > 100_000_000:
                return False, f"头部长度异常({n})"
            hdr = json.loads(f.read(n).decode("utf-8"))
        mx = max(v["data_offsets"][1] for k, v in hdr.items() if k != "__metadata__")
        need = 8 + n + mx
        have = os.path.getsize(path)
        if have < need:
            return False, f"不完整（缺 {need - have:,} bytes）"
        return True, f"完整（{have:,} bytes / {len(hdr)} 张量）"
    except Exception as e:
        return False, f"校验失败：{e}"


def weights_ready(root):
    """形状权重目录是否可用"""
    if not os.path.isdir(root):
        return False, "目录不存在"
    ck = None
    for f in os.listdir(root):
        if f.endswith(".safetensors"):
            ck = os.path.join(root, f)
            break
    if not ck:
        return False, "没有 .safetensors"
    return safetensors_complete(ck)


def step_prep(job_id, src, outdir, cfg):
    """
    整理输入，喂给 Hunyuan3D。

    Hunyuan3D 会在条件化前剥离背景，喂脏背景图会失败得很惨
    （社区实测：平灰底同一张参考图，20 步出漂浮薄片、30 步出实心方块）。
    另一个原因：输入里的背景会被当成几何一起重建出来。
    所以必须先抠图 → 贴纯白 → 裁主体 → 补方形 8% 边距 → 768。
    """
    emit(job_id, "prep", "running", "抠图 + 纯白底 + 裁切…")
    dst = os.path.join(outdir, "00_整理输入.png")
    venv_py = CFG.VENV_PY
    cmd = [venv_py if os.path.exists(venv_py) else "python3",
           os.path.join(ROOT, "hy3d准备.py"), src, dst]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if not os.path.exists(dst):
        tail = "\n".join((r.stdout or "").splitlines()[-4:] + (r.stderr or "").splitlines()[-4:])
        emit(job_id, "prep", "failed", "整理失败", log=f"✗ 输入整理失败：{tail[:300]}")
        return src
    detail = ""
    for ln in (r.stdout or "").splitlines():
        if "主体占比" in ln:
            detail = ln.strip()
    emit(job_id, "prep", "done", detail or f"{os.path.getsize(dst):,} bytes",
         log=f"输入整理完成：{detail}", files=list_files(job_id))
    return dst


def step_recon(job_id, src, outdir, cfg):
    """
    本地 Hunyuan3D 单图生 3D（MLX/Metal）。

    实测（M1 Pro 16GB）：约 60 秒、峰值内存 2.2GB、输出 24~32 万面。
    关键优势：直接出**封闭流形**网格（破洞 0 / 非流形边 0 / 连通块 1），
    而 AI 多视角 + 空间雕刻那条路会因为视角不一致而形状失真（实测 IoU 仅 0.644）。
    """
    if not src or not os.path.exists(src):
        emit(job_id, "recon", "skipped", "没有输入图", log="跳过 3D 重建")
        return None
    if not os.path.exists(HY3D_BIN):
        emit(job_id, "recon", "failed", "未安装 Hunyuan3D-Swift",
             log=f"✗ 找不到 {HY3D_BIN}，请先按 hunyuan3d安装笔记.md 安装")
        return None
    preset = QUALITY_PRESETS.get(cfg.get("quality", "high"), QUALITY_PRESETS["high"])
    weights = HY3D_W_LARGE if preset["model"] == "large" else HY3D_W_SMALL
    ok, why = weights_ready(weights)
    if not ok:
        # 大模型没下好就退回小模型，不要让整条链路失败
        if preset["model"] == "large":
            ok2, why2 = weights_ready(HY3D_W_SMALL)
            if ok2:
                emit(job_id, None, None, None,
                     log=f"⚠️ shape-large 不可用（{why}），回退到 shape-small")
                weights = HY3D_W_SMALL
            else:
                emit(job_id, "recon", "failed", "形状权重都不可用",
                     log=f"✗ shape-small：{why2}；shape-large：{why}")
                return None
        else:
            emit(job_id, "recon", "failed", "形状权重不完整",
                 log=f"✗ {weights}：{why}")
            return None

    emit(job_id, "recon", "running",
         f"{preset['label']} · octree {preset['octree']} · shape-{preset['model']} …")
    glb = os.path.join(outdir, "05_重建.glb")
    t0 = time.time()
    r = subprocess.run([HY3D_BIN, "shape", src, "-o", glb, "--weights", weights,
                        "--octree", str(preset["octree"])],
                       capture_output=True, text=True, timeout=2400)
    dt = time.time() - t0
    if not os.path.exists(glb):
        tail = "\n".join((r.stdout or "").splitlines()[-6:] + (r.stderr or "").splitlines()[-6:])
        emit(job_id, "recon", "failed", "生成失败", log=f"✗ Hunyuan3D 失败：{tail[:400]}")
        return None

    info = ""
    for ln in (r.stdout or "").splitlines():
        if "verts" in ln and "faces" in ln:
            info = ln.strip()
    emit(job_id, "recon", "done", (info or f"{os.path.getsize(glb):,} bytes") + f" · {dt:.0f}秒",
         log=f"Hunyuan3D 重建完成（{dt:.1f} 秒）：{info}", files=list_files(job_id))
    return glb


def step_preflight(job_id, model_path, outdir, cfg):
    """
    打印预检 + 定尺，产出最终交付文件。

    交付物是 STL（毫米），切片由使用者在 Bambu Studio 里自行完成。
    这一步要做对两件事：
      · 尺寸归一 —— 用 --scale-to 把最长边缩到使用者指定的毫米数
      · 打印体检 —— 封闭性、非流形边、破洞、碎块，并自动修复
    """
    if not model_path:
        emit(job_id, "preflight", "skipped", "无 3D 模型输入", log="跳过打印预检（无模型）")
        return None
    target = cfg.get("scale_to") or 100
    emit(job_id, "preflight", "running", f"体检 + 缩放到 {target} mm …")

    stl = os.path.join(outdir, "交付_可打印.stl")
    cmd = [CFG.BLENDER_BIN, "--background",
           "--python", os.path.join(ROOT, "3D打印预检.py"), "--",
           model_path, "--fix", "--keep-largest", "--scale-to", str(target),
           "--out", stl]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    out = (r.stdout or "")

    if not os.path.exists(stl):
        tail = "\n".join(l for l in out.splitlines()[-6:] if "trace" not in l)
        emit(job_id, "preflight", "failed", "预检失败", log=f"✗ 预检失败：{tail[:300]}")
        return None

    # 从脚本输出里抓关键结论
    dims = ""
    for ln in out.splitlines():
        if "单位换算" in ln:
            dims = ln.split("尺寸", 1)[-1].strip()
    ok = "通过预检" in out
    emit(job_id, "preflight", "done" if ok else "failed",
         (f"{dims}" if dims else f"{os.path.getsize(stl):,} bytes") + ("" if ok else " · 预检未通过"),
         log=("打印预检通过，尺寸 " + dims) if ok else "⚠️ 预检未完全通过，请检查产物",
         files=list_files(job_id))
    return stl


def step_deliver(job_id, stl, cfg):
    """交付：确认文件存在、可下载、可一键送进 Bambu Studio"""
    if not stl or not os.path.exists(stl):
        emit(job_id, "deliver", "failed", "没有可交付的文件",
             log="✗ 没有产出可打印文件")
        return None
    size = os.path.getsize(stl)
    dims = ""
    # 用 Bambu Studio 复核尺寸与流形状态（它按毫米读，正好能验证单位换算对不对）
    bs = CFG.BAMBU_BIN
    manifold = None
    if os.path.exists(bs):
        try:
            q = subprocess.run([bs, "--info", stl], capture_output=True, text=True, timeout=300)
            t = q.stdout or ""
            mm = re.search(r"size_x = ([\d.]+)[\s\S]*?size_y = ([\d.]+)[\s\S]*?size_z = ([\d.]+)", t)
            if mm:
                dims = f"{float(mm.group(1)):.1f} × {float(mm.group(2)):.1f} × {float(mm.group(3)):.1f} mm"
            mf = re.search(r"manifold = (\w+)", t)
            manifold = (mf.group(1) == "yes") if mf else None
        except Exception:
            pass

    detail = dims or f"{size:,} bytes"
    if manifold is True:
        detail += " · 封闭 ✓"
    elif manifold is False:
        detail += " · 非封闭 ✗"
    emit(job_id, "deliver", "done", detail,
         log=f"✓ 交付文件已就绪：{os.path.basename(stl)}（{detail}，{size:,} bytes）"
             f" —— 切片请在 Bambu Studio 里完成",
         files=list_files(job_id))
    with LOCK:
        JOBS[job_id]["deliverable"] = {"name": os.path.basename(stl), "size": size,
                                       "dims": dims, "manifold": manifold}
    return stl


def step_slice(job_id, stl, outdir, cfg):
    """
    直接调 Bambu Studio 切片。

    不要再去跑 3D打印预检.py —— 那个脚本会重新导入 STL 并再做一次米→毫米的 ×1000 换算，
    而已导出的 STL 已经是毫米了，二次换算会变成 90 米、超出打印体积
    （实测报错：Nothing to be sliced ... no object is fully inside the print volume）。
    """
    if not stl:
        emit(job_id, "slice", "skipped", "无可切片模型", log="跳过切片")
        return None
    RES = CFG.BAMBU_RES
    BS = CFG.BAMBU_BIN
    if not os.path.exists(BS):
        emit(job_id, "slice", "failed", "未找到 Bambu Studio", log="✗ 未找到 Bambu Studio")
        return None

    pr = cfg.get("printer", "Bambu Lab A1")
    short = pr.replace("Bambu Lab ", "")
    nozzle = cfg.get("nozzle", "0.4")
    layer = cfg.get("layer", "0.20")
    fil = cfg.get("filament", "Bambu PLA Basic")

    def pick(sub, name):
        p = os.path.join(RES, sub, name + ".json")
        return p if os.path.exists(p) else None

    mach = pick("machine", f"{pr} {nozzle} nozzle")
    proc = pick("process", f"{layer}mm Standard @BBL {short}")
    fl = pick("filament", f"{fil} @BBL {short}")
    missing = [n for n, v in (("机型", mach), ("层高", proc), ("材料", fl)) if not v]
    if missing:
        emit(job_id, "slice", "failed", f"缺少配置：{'/'.join(missing)}",
             log=f"✗ Bambu Studio 缺少配置 {missing}（机型/喷嘴/层高/材料组合不存在）")
        return None

    emit(job_id, "slice", "running", f"{short} · {layer}mm · {fil}")
    out = os.path.join(outdir, "切片")
    os.makedirs(out, exist_ok=True)
    r = subprocess.run([BS, "--load-settings", f"{mach};{proc}", "--load-filaments", fl,
                        "--slice", "0", "--outputdir", out, stl],
                       capture_output=True, text=True, timeout=1800)
    gs = sorted(f for f in os.listdir(out) if f.endswith(".gcode"))
    if gs:
        g = os.path.join(out, gs[0])
        info = ""
        with open(g, encoding="utf-8", errors="ignore") as f:
            for i, ln in enumerate(f):
                if i > 60:
                    break
                if "model printing time" in ln:
                    info = ln.split(":", 1)[-1].strip()
                    break
        emit(job_id, "slice", "done", f"{gs[0]} · {info}",
             log=f"切片完成：{gs[0]}（{os.path.getsize(g):,} bytes）{('· 预计 ' + info) if info else ''}",
             files=list_files(job_id))
        return g
    tail = "\n".join(l for l in (r.stdout or "").splitlines()
                     if "[trace]" not in l and "[debug]" not in l)[-400:]
    emit(job_id, "slice", "failed", "未产出 G-code", log=f"✗ 切片失败：{tail}")
    return None


def run_pipeline(job_id):
    st = JOBS[job_id]
    src = st["src"]
    outdir = os.path.join(OUTPUTS, job_id)
    os.makedirs(outdir, exist_ok=True)
    cfg = st.get("cfg", {})
    try:
        emit(job_id, "upload", "done", os.path.basename(src),
             log=f"已接收图片：{os.path.basename(src)}", files=list_files(job_id))
        gate = step_gate(job_id, src, outdir, cfg)
        fixed = step_fix(job_id, src, outdir, gate, cfg)
        prepared = step_prep(job_id, fixed, outdir, cfg)
        model = step_recon(job_id, prepared, outdir, cfg)
        stl = step_preflight(job_id, model, outdir, cfg)
        step_deliver(job_id, stl, cfg)
        if cfg.get("do_slice"):          # 默认不做；留着开关，将来做 Bambu 全自动化时再启用
            step_slice(job_id, stl, outdir, cfg)
        emit(job_id, None, None, None, files=list_files(job_id))
    except Exception as e:
        traceback.print_exc()
        with LOCK:
            JOBS[job_id]["error"] = str(e)
        emit(job_id, None, None, None, log=f"✗ 流水线异常：{e}")
    finally:
        with LOCK:
            JOBS[job_id]["done"] = True
            JOBS[job_id]["status"] = "finished"
        emit(job_id, None, None, None, files=list_files(job_id))


# ══════════════════════════════════════════════════════════
#  HTTP 处理
# ══════════════════════════════════════════════════════════
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)

        if u.path in ("/", "/index.html"):
            p = os.path.join(HERE, "static", "index.html")
            if os.path.exists(p):
                return self._send(200, open(p, "rb").read(), "text/html; charset=utf-8")
            return self._send(404, "UI 文件缺失", "text/plain; charset=utf-8")

        if u.path == "/api/steps":
            return self._json(STEPS)

        if u.path == "/api/jobs":
            out = []
            for jid, st in JOBS.items():
                steps = st.get("steps", {})
                done_n = sum(1 for v in steps.values() if v.get("status") == "done")
                bad = any(v.get("status") == "failed" for v in steps.values())
                out.append({
                    "id": jid,
                    "name": st.get("name") or os.path.basename(st.get("src", "")),
                    "done": bool(st.get("done")),
                    "failed": bad,
                    "steps_done": done_n,
                    "steps_total": len(steps),
                    "subject": (st.get("cfg") or {}).get("subject", ""),
                    "deliverable": st.get("deliverable"),
                    "nfiles": len(list_files(jid)),
                    "mtime": os.path.getmtime(os.path.join(OUTPUTS, jid))
                             if os.path.isdir(os.path.join(OUTPUTS, jid)) else 0,
                })
            out.sort(key=lambda x: x["id"], reverse=True)
            return self._json(out)

        if u.path == "/api/prompts":
            return self._json(PT.load())

        if u.path == "/api/prompts/default":
            return self._json(PT.DEFAULT)

        if u.path == "/api/prompts/preview":
            subj = q.get("subject", [""])[0]
            tpl = PT.load()
            if subj:
                tpl["subject"] = subj
            out = {
                "修复": PT.build_repair_prompt(tpl, [("主体被裁切", ""), ("背景未去除", "")]),
                "多角度": {s2["name"]: PT.build_angle_prompt(tpl, s2["name"])
                           for s2 in tpl.get("angle_specs", [])},
            }
            return self._json(out)

        if u.path == "/api/config":
            k = load_system_key()
            return self._json({
                "has_key": bool(k),
                "key_hint": (k[:7] + "…" + k[-4:]) if k else "",
                "base_url": load_system_base(),
                "models": [{"id": mid, "label": lab, "provider": prov,
                            "provider_label": PROVIDERS[prov]["label"],
                            "has_key": bool(load_key_for(prov))}
                           for prov, mid, lab in AVAILABLE_MODELS],
                "providers": {k: {"label": v["label"], "has_key": bool(load_key_for(k))}
                              for k, v in PROVIDERS.items()},
                "bambu_installed": os.path.exists(CFG.BAMBU_BIN),
                "quality_presets": [{"id": k, "label": v["label"], "octree": v["octree"],
                                     "model": v["model"], "faces": v.get("faces", ""),
                                     "secs": v.get("secs", 0)}
                                    for k, v in QUALITY_PRESETS.items()],
                "large_weights_ready": weights_ready(HY3D_W_LARGE)[0],
                "large_weights_note": weights_ready(HY3D_W_LARGE)[1],
                "printers": ["Bambu Lab A1", "Bambu Lab A1 mini", "Bambu Lab P1P",
                             "Bambu Lab P1S", "Bambu Lab X1C", "Bambu Lab H2D",
                             "Bambu Lab H2S", "Bambu Lab H2C", "Bambu Lab P2S",
                             "Bambu Lab A2L"],
            })

        if u.path == "/api/state":
            jid = q.get("job_id", [""])[0]
            st = JOBS.get(jid)
            if not st:
                return self._json({"error": "任务不存在"}, 404)
            with LOCK:
                return self._json({k: v for k, v in st.items() if k != "events"})

        if u.path == "/api/stream":
            jid = q.get("job_id", [""])[0]
            if jid not in JOBS:
                return self._json({"error": "任务不存在"}, 404)
            return self.sse(jid)

        if u.path == "/api/download":
            jid = q.get("job_id", [""])[0]
            fn = q.get("f", [""])[0]
            fn = os.path.basename(fn)   # 防目录穿越
            p = os.path.join(OUTPUTS, jid, fn)
            if not os.path.isfile(p):
                return self._json({"error": "文件不存在"}, 404)
            data = open(p, "rb").read()
            return self._send(200, data, "application/octet-stream",
                              {"Content-Disposition": f'attachment; filename="{urllib.parse.quote(fn)}"'})

        return self._send(404, "not found", "text/plain")

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length", 0))

        if u.path == "/api/upload":
            # 文件名走 query 参数（百分号编码），不要放 HTTP 头 ——
            # 头部只能是 latin-1，直接塞 UTF-8 会变乱码（实测「测试人物.png」→「æµ_è__äººç__.png」）
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            fn = (qs.get("name", ["upload.png"])[0]) or "upload.png"
            fn = os.path.basename(fn)
            fn = re.sub(r"[^\w.\-\u4e00-\u9fff]", "_", fn) or "upload.png"
            jid = time.strftime("%Y%m%d_%H%M%S") + "_" + str(int(time.time() * 1000) % 1000)
            os.makedirs(UPLOADS, exist_ok=True)
            p = os.path.join(UPLOADS, f"{jid}_{fn}")
            with open(p, "wb") as f:
                f.write(self.rfile.read(n))
            new_job(jid, p)
            emit(jid, "upload", "pending", os.path.basename(p))
            return self._json({"job_id": jid, "name": fn, "size": os.path.getsize(p)})

        if u.path == "/api/upload-angle":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            jid = qs.get("job_id", [""])[0]
            ang = qs.get("angle", [""])[0]
            if jid not in JOBS:
                return self._json({"error": "任务不存在"}, 404)
            if not re.fullmatch(r"[A-Za-z0-9_]+", ang or ""):
                return self._json({"error": "角度名非法"}, 400)
            d = os.path.join(OUTPUTS, jid)
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, f"02_角度_{ang}.png")
            with open(p, "wb") as f:
                f.write(self.rfile.read(n))
            emit(jid, None, None, None,
                 log=f"已接收上传角度：{ang}（{os.path.getsize(p):,} bytes）",
                 files=list_files(jid))
            return self._json({"ok": True, "angle": ang, "size": os.path.getsize(p)})

        if u.path == "/api/run":
            body = json.loads(self.rfile.read(n) or b"{}")
            jid = body.get("job_id")
            st = JOBS.get(jid)
            if not st:
                return self._json({"error": "任务不存在"}, 404)
            cfg = body.get("config", {})
            prov = cfg.get("provider") or next(iter(CFG.PROVIDERS), "")
            if not cfg.get("api_key"):
                cfg["api_key"] = load_key_for(prov)     # 服务端按 provider 注入
            if not cfg.get("base_url"):
                cfg["base_url"] = PROVIDERS.get(prov, {}).get("base", load_system_base())
            st["cfg"] = cfg
            threading.Thread(target=run_pipeline, args=(jid,), daemon=True).start()
            return self._json({"ok": True, "job_id": jid})

        if u.path == "/api/jobs":
            out = []
            for jid, st in JOBS.items():
                steps = st.get("steps", {})
                done_n = sum(1 for v in steps.values() if v.get("status") == "done")
                bad = any(v.get("status") == "failed" for v in steps.values())
                out.append({
                    "id": jid,
                    "name": st.get("name") or os.path.basename(st.get("src", "")),
                    "done": bool(st.get("done")),
                    "failed": bad,
                    "steps_done": done_n,
                    "steps_total": len(steps),
                    "subject": (st.get("cfg") or {}).get("subject", ""),
                    "deliverable": st.get("deliverable"),
                    "nfiles": len(list_files(jid)),
                    "mtime": os.path.getmtime(os.path.join(OUTPUTS, jid))
                             if os.path.isdir(os.path.join(OUTPUTS, jid)) else 0,
                })
            out.sort(key=lambda x: x["id"], reverse=True)
            return self._json(out)

        if u.path == "/api/prompts":
            body = json.loads(self.rfile.read(n) or b"{}")
            if body.get("reset"):
                PT.save(PT.DEFAULT)
                return self._json({"ok": True, "reset": True})
            cur = PT.load()
            cur.update(body.get("prompts") or {})
            PT.save(cur)
            return self._json({"ok": True})

        if u.path == "/api/import-bambu":
            body = json.loads(self.rfile.read(n) or b"{}")
            jid, fn = body.get("job_id"), os.path.basename(body.get("f", ""))
            p = os.path.join(OUTPUTS, jid, fn)
            if not os.path.isfile(p):
                return self._json({"error": "文件不存在"}, 404)
            app = os.path.dirname(os.path.dirname(CFG.BAMBU_BIN))
            if not os.path.isdir(app):
                return self._json({"error": "未找到 Bambu Studio"}, 404)
            subprocess.Popen(["open", "-a", app, p])
            emit(jid, log=f"已在 Bambu Studio 中打开：{fn}")
            return self._json({"ok": True})

        return self._send(404, "not found", "text/plain")

    def sse(self, jid):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        sent = 0
        last_log = 0
        while True:
            st = JOBS.get(jid)
            if not st:
                break
            with LOCK:
                steps = json.dumps({k: v for k, v in st["steps"].items()}, ensure_ascii=False)
                logs = st["log"][last_log:]
                last_log = len(st["log"])
                files = list_files(jid)
                gate = st.get("gate")
                done = st["done"]
                deliverable = st.get("deliverable")
            payload = {"steps": json.loads(steps), "logs": logs, "files": files,
                       "gate": gate, "done": done, "now": now(),
                       "deliverable": deliverable}
            try:
                self.wfile.write(f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            sent += 1
            if done and not logs:
                return
            time.sleep(0.5)


def lan_ips():
    """取本机所有可用的局域网 IPv4 地址"""
    import socket
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))          # 不会真的发包，只为拿到出口网卡地址
        ips.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except Exception:
        pass
    return ips


def main():
    load_jobs()
    print(f"流水线 UI 已启动")
    print(f"  本机:    http://127.0.0.1:{PORT}")
    if HOST == "0.0.0.0":
        for ip in lan_ips():
            print(f"  局域网:  http://{ip}:{PORT}")
        print("  ⚠️ 局域网可达：同一网络下的设备都能打开，也会共用你的 API 额度")
    print(f"  已载入历史任务 {len(JOBS)} 个")
    print(f"  工作目录：{HERE}")
    print(f"  输出目录：{OUTPUTS}")
    print("  按 Ctrl+C 停止")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
