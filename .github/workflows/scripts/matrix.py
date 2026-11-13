"""构建矩阵的唯一来源。

矩阵数据只存放在 config/matrix.json；本模块负责读取、排序和生成
GitHub Actions 需要的扁平/分类格式，避免脚本之间各存一份副本。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

MATRIX_PATH = Path(__file__).resolve().parent.parent / "config" / "matrix.json"


def load_matrix(path: Optional[str] = None) -> dict:
    with open(path or MATRIX_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _split_key(key: str) -> tuple:
    android, kernel = key.split("-", 1)
    return android, kernel


def _android_order(android: str) -> int:
    return int(android.replace("android", ""))


def _kernel_order(kernel: str) -> float:
    return float(kernel)


def _sub_level_order(sub_level: str) -> str:
    # X（LTS）排在数字版本之后
    return "ZZZZ" if sub_level == "X" else sub_level.zfill(8)


def _sub_level_label(cfg: dict) -> str:
    label = str(cfg["sub_level"])
    if cfg.get("page_size") == "16k":
        label += "(16k)"
    return label


def generate_build_matrix(matrix: Optional[dict] = None) -> list:
    """生成扁平矩阵，供 workflow 的 strategy.matrix 使用。"""
    matrix = matrix if matrix is not None else load_matrix()
    builds = []
    for key, configs in matrix.items():
        android, kernel = _split_key(key)
        for cfg in configs:
            build = {
                "android": android,
                "kernel": kernel,
                "sub_level": cfg["sub_level"],
                "os_patch": cfg["os_patch_level"],
            }
            if cfg.get("revision"):
                build["revision"] = cfg["revision"]
            if cfg.get("page_size"):
                build["page_size"] = cfg["page_size"]
            build["label"] = (
                f"{cfg['sub_level']}-16k"
                if cfg.get("page_size") == "16k"
                else cfg["sub_level"]
            )
            builds.append(build)

    builds.sort(key=lambda x: (
        _android_order(x["android"]),
        _kernel_order(x["kernel"]),
        _sub_level_order(x["sub_level"]),
    ))
    return builds


def generate_classified_matrix(matrix: Optional[dict] = None) -> dict:
    """生成 android -> kernel -> configs 的分类矩阵。"""
    matrix = matrix if matrix is not None else load_matrix()
    classified: dict = {}
    for key, configs in matrix.items():
        android, kernel = _split_key(key)
        classified.setdefault(android, {}).setdefault(kernel, []).extend(configs)

    sorted_classified: dict = {}
    for android in sorted(classified, key=_android_order):
        sorted_classified[android] = {}
        for kernel in sorted(classified[android], key=_kernel_order):
            sorted_classified[android][kernel] = sorted(
                classified[android][kernel],
                key=lambda x: _sub_level_order(x["sub_level"]),
            )
    return sorted_classified


def matrix_summary(classified: Optional[dict] = None) -> list:
    """一行一个 android-kernel 组合的摘要。"""
    classified = classified if classified is not None else generate_classified_matrix()
    summary = []
    for android, kernels in classified.items():
        for kernel, configs in kernels.items():
            sub_levels = [_sub_level_label(c) for c in configs]
            summary.append(f"{android}-{kernel}: {', '.join(sub_levels)}")
    return summary


def matrix_summary_markdown(classified: Optional[dict] = None) -> str:
    classified = classified if classified is not None else generate_classified_matrix()
    lines = ["### 构建矩阵摘要", ""]
    for android, kernels in classified.items():
        lines.append(f"**{android.upper()}**")
        lines.append("")
        for kernel, configs in kernels.items():
            sub_levels = ", ".join(_sub_level_label(c) for c in configs)
            lines.append(f"- {kernel}: {sub_levels}")
        lines.append("")
    return "\n".join(lines)
