#!/usr/bin/env python3
"""GKI Kernel 统一构建入口。

本地用法:
    python3 build.py --android android14 --kernel 6.1 --sub-level 124 \
        --os-patch 2025-02 --ksu-version "Stable(标准)"

CI 用法:
    python3 build.py --from-env
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import (  # noqa: E402
    ANDROID_KERNEL_MAP,
    AndroidVersion,
    BuildConfig,
    KernelVersion,
    KSU_BRANCH_BY_VERSION,
    KSUVersion,
    PAGE_SIZES,
)
from kernel_builder import BuildResult, KernelBuilder  # noqa: E402
from matrix import load_matrix  # noqa: E402

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("build")

_TRUE_VALUES = {"1", "true", "yes", "on", "y"}


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------

def parse_args(argv=None) -> argparse.Namespace:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--from-env" in argv:
        args = args_from_env()
        # 允许在 env 模式上叠加本地调试开关
        if "--dry-run" in argv:
            args.dry_run = True
        if "--verbose" in argv or "-v" in argv:
            args.verbose = True
        return args

    parser = argparse.ArgumentParser(description="GKI Kernel 构建系统")

    parser.add_argument("--android", "-a", choices=[v.value for v in AndroidVersion])
    parser.add_argument("--kernel", "-k", choices=[v.value for v in KernelVersion])
    parser.add_argument("--sub-level", "-s")
    parser.add_argument("--os-patch")
    parser.add_argument(
        "--ksu-version",
        choices=[v.value for v in KSUVersion],
        default=KSUVersion.STABLE.value,
        help="Stable=builtin, Dev=builtin-new, Custom=使用 --ksu-branch",
    )
    parser.add_argument("--ksu-branch", default=None, help="Custom 模式下使用的 SukiSU 分支 / tag / commit")
    parser.add_argument("--ksu-commit", default=None, help="SukiSU commit（优先级高于版本和分支）")
    parser.add_argument("--susfs-commit", default=None)
    parser.add_argument("--zram", action="store_true")
    parser.add_argument("--no-kpm", action="store_true")
    parser.add_argument("--bbg", action="store_true")
    parser.add_argument("--op8e", action="store_true")
    parser.add_argument("--bbr", action="store_true")
    parser.add_argument("--custom-version", dest="custom_version", default=None)
    parser.add_argument("--revision")
    parser.add_argument(
        "--page-size",
        choices=list(PAGE_SIZES),
        default="4k",
        help="ARM64 page size；6.1/6.6/6.12/6.18 支持 16k",
    )
    parser.add_argument("--matrix", "-m")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--list-configs", action="store_true")
    parser.add_argument("--list-matrix", action="store_true")
    parser.add_argument("--workspace", "-w", default=os.environ.get("GKI_WORKSPACE", "/tmp/gki-build"))
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--output-json")
    parser.add_argument("--dry-run", action="store_true")

    return parser.parse_args(argv)


def _env(name: str, default=None):
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in _TRUE_VALUES


def args_from_env() -> argparse.Namespace:
    """从环境变量读取 CI 输入，避免在 workflow 里拼接 shell 参数。"""
    return argparse.Namespace(
        android=_env("ANDROID_VERSION"),
        kernel=_env("KERNEL_VERSION"),
        sub_level=_env("SUB_LEVEL"),
        os_patch=_env("OS_PATCH_LEVEL"),
        ksu_version=_env("KSU_VERSION", KSUVersion.STABLE.value),
        ksu_branch=_env("KSU_BRANCH"),
        ksu_commit=_env("KSU_COMMIT"),
        susfs_commit=_env("SUSFS_COMMIT"),
        zram=_env_bool("USE_ZRAM"),
        no_kpm=not _env_bool("USE_KPM", True),
        bbg=_env_bool("USE_BBG"),
        op8e=_env_bool("USE_OP8E"),
        bbr=_env_bool("SET_DEF_BBR"),
        custom_version=_env("CUSTOM_VERSION"),
        revision=_env("REVISION"),
        page_size=_env("PAGE_SIZE", "4k"),
        matrix=_env("BUILD_MATRIX"),
        all=_env_bool("BUILD_ALL"),
        list_configs=False,
        list_matrix=False,
        workspace=_env("GKI_WORKSPACE", "/tmp/gki-build"),
        verbose=_env_bool("VERBOSE"),
        output_json=_env("OUTPUT_JSON"),
        dry_run=_env_bool("DRY_RUN"),
    )


# ---------------------------------------------------------------------------
# 构建
# ---------------------------------------------------------------------------

def create_build_config(args: argparse.Namespace) -> BuildConfig:
    config = BuildConfig(
        android_version=args.android or "android14",
        kernel_version=args.kernel or "6.1",
        sub_level=args.sub_level or "124",
        os_patch_level=args.os_patch or "2025-02",
        kernelsu_version=args.ksu_version,
        kernelsu_branch=args.ksu_branch,
        kernelsu_commit=args.ksu_commit,
        susfs_commit=args.susfs_commit,
        use_zram=args.zram,
        use_kpm=not args.no_kpm,
        use_bbg=args.bbg,
        support_op8e=args.op8e,
        set_default_bbr=args.bbr,
        custom_version=args.custom_version,
        revision=args.revision,
        page_size=args.page_size,
    )
    if config.ignored_kernelsu_branch:
        logger.warning(
            "已填写 SukiSU 分支 %s，但版本为 %s；该分支不会生效。"
            "如需使用自定义分支，请把版本设为 %s。",
            config.ignored_kernelsu_branch,
            config.kernelsu_version,
            KSUVersion.CUSTOM.value,
        )
    return config


def list_configs():
    matrix = load_matrix()
    print("\n" + "=" * 60)
    print("支持的 Android/Kernel 版本组合")
    print("=" * 60)
    for android, kernels in ANDROID_KERNEL_MAP.items():
        print(f"\n{android.value}:")
        for kernel in kernels:
            configs = matrix.get(f"{android.value}-{kernel.value}", [])
            sub_levels = ", ".join(c["sub_level"] for c in configs) or "N/A"
            print(f"  - {kernel.value}: {sub_levels}")
    print("\n" + "=" * 60)
    print("SukiSU 版本选项")
    print("=" * 60)
    for v in KSUVersion:
        branch = KSU_BRANCH_BY_VERSION.get(v.value)
        suffix = f" -> {branch}" if branch else " -> 使用 --ksu-branch"
        print(f"  - {v.value}{suffix}")


def list_matrix():
    matrix = load_matrix()
    print("\n" + "=" * 60)
    print("预定义构建矩阵")
    print("=" * 60)
    for combo in sorted(matrix):
        print(f"\n{combo}:")
        for cfg in matrix[combo]:
            rev = f" (rev: {cfg['revision']})" if cfg.get("revision") else ""
            page = f" [{cfg['page_size']}]" if cfg.get("page_size") else ""
            print(f"  - {cfg['sub_level']:>4} | {cfg['os_patch_level']}{rev}{page}")


def build_single(config: BuildConfig, workspace: str, dry_run: bool = False) -> BuildResult:
    if dry_run:
        logger.info(
            "[DRY RUN] 验证配置: %s (page_size=%s, SukiSU ref: %s)",
            config.config_name, config.page_size, config.kernelsu_ref,
        )
        return BuildResult(success=True, config=config, message="配置验证通过")

    builder = KernelBuilder(config, workspace)
    return builder.build()


def build_matrix(matrix_key: str, args: argparse.Namespace, workspace: str) -> list:
    logger.info("\n%s\n开始构建矩阵: %s\n%s", "=" * 60, matrix_key, "=" * 60)

    matrix = load_matrix()
    configs_data = matrix.get(matrix_key)
    if not configs_data:
        logger.error("未知的矩阵: %s", matrix_key)
        return []

    android, kernel = matrix_key.split("-", 1)
    results = []
    for cfg_data in configs_data:
        try:
            config = BuildConfig(
                android_version=android,
                kernel_version=kernel,
                sub_level=cfg_data["sub_level"],
                os_patch_level=cfg_data["os_patch_level"],
                kernelsu_version=args.ksu_version,
                kernelsu_branch=args.ksu_branch,
                kernelsu_commit=args.ksu_commit,
                susfs_commit=args.susfs_commit,
                use_zram=args.zram,
                use_kpm=not args.no_kpm,
                use_bbg=args.bbg,
                support_op8e=args.op8e,
                set_default_bbr=args.bbr,
                custom_version=args.custom_version,
                revision=cfg_data.get("revision"),
                page_size=cfg_data.get("page_size", "4k"),
            )
        except ValueError as e:
            logger.error("配置 %s 无效: %s", cfg_data, e)
            continue

        logger.info("\n%s\n构建配置: %s\n%s", "=" * 60, config.config_name, "=" * 60)
        result = build_single(config, workspace, args.dry_run)
        results.append(result)

        if result.success:
            logger.info("✓ %s 构建成功", config.config_name)
        else:
            logger.error("✗ %s 构建失败: %s", config.config_name, result.message)

    return results


def build_all(args: argparse.Namespace, workspace: str) -> list:
    all_results = []
    for matrix_key in sorted(load_matrix()):
        all_results.extend(build_matrix(matrix_key, args, workspace))
    return all_results


def print_summary(results: list, output_json: str = None):
    total = len(results)
    success = sum(1 for r in results if r.success)

    print("\n" + "=" * 60)
    print("构建摘要")
    print("=" * 60)
    print(f"总数: {total}")
    print(f"成功: \033[92m{success}\033[0m")
    print(f"失败: \033[91m{total - success}\033[0m")

    if success:
        avg_time = sum(r.build_time or 0 for r in results if r.success) / success
        print(f"平均构建时间: {avg_time:.2f} 秒")

    failed = total - success
    if failed:
        print("\n失败的配置:")
        for r in results:
            if not r.success:
                print(f"  - {r.config.config_name}: {r.message}")
    print("=" * 60)

    if output_json:
        json_data = {
            "timestamp": datetime.now().isoformat(),
            "total": total,
            "success": success,
            "failed": failed,
            "results": [
                {
                    "config": r.config.to_dict(),
                    "success": r.success,
                    "message": r.message,
                    "artifacts": r.artifacts,
                    "build_time": r.build_time,
                }
                for r in results
            ],
        }
        with open(output_json, "w", encoding="utf-8") as f:
            json.dump(json_data, f, indent=2, ensure_ascii=False)
        logger.info("结果已保存到: %s", output_json)


def main() -> int:
    args = parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.list_configs:
        list_configs()
        return 0

    if args.list_matrix:
        list_matrix()
        return 0

    if not args.all and not args.matrix and not args.android:
        logger.error("请指定 --all, --matrix 或 --android")
        return 1

    workspace = args.workspace
    logger.info("工作目录: %s", workspace)
    if not args.dry_run:
        os.makedirs(workspace, exist_ok=True)

    results = []

    if args.all:
        results = build_all(args, workspace)
    elif args.matrix:
        results = build_matrix(args.matrix, args, workspace)
    else:
        try:
            config = create_build_config(args)
        except ValueError as e:
            logger.error("配置错误: %s", e)
            return 1
        results.append(build_single(config, workspace, args.dry_run))

    if results:
        print_summary(results, args.output_json)

    if results and all(r.success for r in results):
        return 0
    if results and any(r.success for r in results):
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
