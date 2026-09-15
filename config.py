#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
集中配置 —— 所有外部依赖路径与 API 凭据都从这里读。

优先级：环境变量 > config.json > 默认值

为什么要这一层：
  最初这些路径是硬编码的（/Applications/Blender.app/... 、~/AI/Hunyuan3D-Swift/...），
  换台机器就跑不起来。抽出来之后，别人只改 config.json 或设环境变量即可。

用法：
  cp config.example.json config.json   # 然后按需修改
  # 或直接设环境变量
  export BLENDER_BIN=/path/to/blender
  export HY3D_BIN=/path/to/hy3d
"""
import json
import os

ROOT = os.path.dirname(os.path.abspath(__file__))

# ── 读 config.json（可选）────────────────────────────────
_CFG = {}
_cfg_path = os.path.join(ROOT, "config.json")
if os.path.exists(_cfg_path):
    try:
        with open(_cfg_path, encoding="utf-8") as f:
            _CFG = json.load(f) or {}
    except Exception as e:
        print(f"[config] config.json 解析失败，忽略：{e}")


def get(key, default=None, env=None):
    """按 环境变量 > config.json > 默认值 取值"""
    if env and os.environ.get(env):
        return os.environ[env]
    if key in _CFG and _CFG[key] not in (None, ""):
        return _CFG[key]
    return default


def path(key, default, env=None):
    v = get(key, default, env)
    return os.path.expanduser(v) if v else v


# ── 外部程序路径 ─────────────────────────────────────────
BLENDER_BIN = path("blender_bin",
                   "/Applications/Blender.app/Contents/MacOS/Blender", "BLENDER_BIN")
BAMBU_BIN = path("bambu_bin",
                 "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio", "BAMBU_BIN")
BAMBU_RES = path("bambu_resources",
                 "/Applications/BambuStudio.app/Contents/Resources/profiles/BBL",
                 "BAMBU_RESOURCES")

# Hunyuan3D-Swift（MLX/Metal，单图生 3D）
# 安装见 docs/hunyuan3d-setup.md
HY3D_BIN = path("hy3d_bin",
                "~/AI/Hunyuan3D-Swift/.build/release/hy3d", "HY3D_BIN")
HY3D_W_SMALL = path("hy3d_weights_small",
                    "~/AI/Hunyuan3D-Swift/weights/shape-small", "HY3D_WEIGHTS_SMALL")
HY3D_W_LARGE = path("hy3d_weights_large",
                    "~/AI/Hunyuan3D-Swift/weights/shape-large", "HY3D_WEIGHTS_LARGE")

# 跑 rembg / scikit-image 的 Python 解释器（默认用仓库内 .venv）
VENV_PY = path("venv_python", os.path.join(ROOT, ".venv", "bin", "python"),
               "PIPELINE_PYTHON")

# ── 图像生成中转（仅「AI 修复」用得到，可选）──────────────
# 结构：{ "provider名": {"base": "...", "key": "..."} }
# key 也可以只写环境变量名，由环境变量提供
PROVIDERS = get("providers", {
    "openai": {"base": "https://api.openai.com/v1", "key_env": "OPENAI_API_KEY"},
})

# 兼容旧写法：从 config.json 的 keys 段取
_KEYS = _CFG.get("keys", {})


def provider_key(name):
    """
    取某个 provider 的 API Key。
    顺序：config.json 的 providers.<name>.key → keys.<name> → 对应环境变量
    """
    p = PROVIDERS.get(name) or {}
    if p.get("key"):
        return str(p["key"]).strip()
    if _KEYS.get(name):
        return str(_KEYS[name]).strip()
    env_name = p.get("key_env") or f"{name.upper()}_API_KEY"
    return (os.environ.get(env_name) or "").strip()


def provider_base(name):
    p = PROVIDERS.get(name) or {}
    return p.get("base", "")


def provider_has_key(name):
    return bool(provider_key(name))


# ── 自检 ────────────────────────────────────────────────
def doctor():
    """打印各依赖是否就绪，方便排查「为什么跑不起来」"""
    checks = [
        ("Blender", BLENDER_BIN),
        ("Bambu Studio", BAMBU_BIN),
        ("Bambu 配置目录", BAMBU_RES),
        ("Hunyuan3D 可执行文件", HY3D_BIN),
        ("shape-small 权重", HY3D_W_SMALL),
        ("shape-large 权重", HY3D_W_LARGE),
        ("Python 环境", VENV_PY),
    ]
    print(f"仓库根目录: {ROOT}")
    for label, p in checks:
        ok = os.path.exists(p)
        print(f"  {'✓' if ok else '✗'} {label:<22} {p}")
    for name in PROVIDERS:
        print(f"  {'✓' if provider_has_key(name) else '✗'} API Key: {name}")
    return all(os.path.exists(p) for _, p in checks[:2])


if __name__ == "__main__":
    doctor()
