#!/usr/bin/env python3
import argparse
import os
import sys
import json
import logging
from pathlib import Path
from typing import Optional, List
from datetime import datetime

# 添加当前目录到 path
sys.path.insert(0, str(Path(__file__).parent))

from config import (
    BuildConfig,
    AndroidVersion,
    KernelVersion,
    ANDROID_KERNEL_MAP,
    KSUVersion,
    validate_commit_hash,
)
from kernel_builder import KernelBuilder, BuildResult


# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='\033[92m[%(levelname)s]\033[0m %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


def load_build_matrix() -> dict:
    """从 JSON 文件加载构建矩阵"""
    matrix_file = Path(__file__).parent.parent / "config" / "matrix.json"
    
    if matrix_file.exists():
        try:
            with open(matrix_file, "r", encoding="utf-8") as f:
                matrix = json.load(f)
            logger.info(f"从 {matrix_file} 加载构建矩阵")
            return matrix
        except json.JSONDecodeError as e:
            logger.warning(f"JSON 解析失败: {e}，使用内置矩阵")
    else:
        logger.warning(f"矩阵文件不存在: {matrix_file}，使用内置矩阵")
    
    # 内置默认矩阵
    return {
        "android12-5.10": [
            {"sub_level": "136", "os_patch_level": "2022-11", "revision": "r15"},
            {"sub_level": "198", "os_patch_level": "2024-01", "revision": "r17"},
            {"sub_level": "209", "os_patch_level": "2024-05", "revision": "r13"},
            {"sub_level": "236", "os_patch_level": "2025-05", "revision": "r1"},
            {"sub_level": "X", "os_patch_level": "lts", "revision": "r1"},
        ],
        "android13-5.15": [
            {"sub_level": "74", "os_patch_level": "2023-01"},
            {"sub_level": "123", "os_patch_level": "2023-11"},
            {"sub_level": "148", "os_patch_level": "2024-05"},
            {"sub_level": "170", "os_patch_level": "2025-01"},
            {"sub_level": "178", "os_patch_level": "2025-03"},
            {"sub_level": "180", "os_patch_level": "2025-05"},
            {"sub_level": "189", "os_patch_level": "2025-09"},
        ],
        "android14-6.1": [
            {"sub_level": "78", "os_patch_level": "2024-06"},
            {"sub_level": "90", "os_patch_level": "2024-08"},
            {"sub_level": "99", "os_patch_level": "2024-10"},
            {"sub_level": "124", "os_patch_level": "2025-02"},
            {"sub_level": "145", "os_patch_level": "2025-09"},
        ],
        "android15-6.6": [
            {"sub_level": "50", "os_patch_level": "2024-10"},
            {"sub_level": "66", "os_patch_level": "2025-02"},
            {"sub_level": "102", "os_patch_level": "2025-10"},
        ],
        "android16-6.12": [
            {"sub_level": "23", "os_patch_level": "2025-06"},
            {"sub_level": "30", "os_patch_level": "2025-07"},
            {"sub_level": "58", "os_patch_level": "2025-12"},
            {"sub_level": "69", "os_patch_level": "2026-03"},
        ],
    }


# 加载构建矩阵
DEFAULT_BUILD_MATRIX = load_build_matrix()


def parse_args() -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="GKI Kernel 构建系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s --android android12 --kernel 5.10 --sub-level 236 --os-patch 2025-05
  %(prog)s --android android14 --kernel 6.1 --sub-level 124 --os-patch 2025-02 --ksu-commit abc1234
  %(prog)s --matrix android14-6.1
  %(prog)s --all
        """
    )
    
    # 基本参数
    parser.add_argument(
        "--android", "-a",
        choices=[v.value for v in AndroidVersion],
        help="Android 版本"
    )
    parser.add_argument(
        "--kernel", "-k",
        choices=[v.value for v in KernelVersion],
        help="Kernel 版本"
    )
    parser.add_argument(
        "--sub-level", "-s",
        help="Sub level 版本 (如 124 或 X 表示 LTS)"
    )
    parser.add_argument(
        "--os-patch",
        help="OS Patch Level (如 2025-02)"
    )
    
    # KernelSU 参数
    parser.add_argument(
        "--ksu-version",
        choices=[v.value for v in KSUVersion],
        default=KSUVersion.STABLE.value,
        help="SukiSU 版本 (默认: Stable)"
    )
    parser.add_argument(
        "--ksu-commit",
        help="指定 SukiSU 的 commit hash (可选，将构建指定版本)"
    )
    parser.add_argument(
        "--susfs-commit",
        help="指定 SUSFS 的 commit hash 或相对偏移 (如 abc1234 或 HEAD~1)"
    )
    
    # 功能开关
    parser.add_argument(
        "--zram",
        action="store_true",
        help="启用 ZRAM (LZ4KD)"
    )
    parser.add_argument(
        "--no-kpm",
        action="store_true",
        help="禁用 KPM"
    )
    parser.add_argument(
        "--bbg",
        action="store_true",
        help="启用 Baseband-guard"
    )
    parser.add_argument(
        "--op8e",
        action="store_true",
        help="启用 OnePlus 8E 支持"
    )
    parser.add_argument(
        "--bbr",
        action="store_true",
        help="设置 BBR 为默认拥塞算法"
    )
    
    # 发布配置
    parser.add_argument(
        "--release",
        action="store_true",
        default=True,
        help="创建发布版本 (默认启用)"
    )
    parser.add_argument(
        "--no-release",
        action="store_true",
        help="不创建发布版本"
    )
    parser.add_argument(
        "--version",
        help="自定义版本名称"
    )
    parser.add_argument(
        "--revision",
        help="Android 12 特定的 revision"
    )
    
    # 矩阵构建
    parser.add_argument(
        "--matrix", "-m",
        help="使用预定义矩阵 (如 android14-6.1)"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="构建所有配置的组合"
    )
    
    # 信息选项
    parser.add_argument(
        "--list-configs",
        action="store_true",
        help="列出所有支持的配置"
    )
    parser.add_argument(
        "--list-matrix",
        action="store_true",
        help="列出所有预定义矩阵"
    )
    
    # 其他选项
    parser.add_argument(
        "--workspace", "-w",
        default=os.environ.get("GKI_WORKSPACE", "/tmp/gki-build"),
        help="工作目录 (默认: /tmp/gki-build)"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="详细输出"
    )
    parser.add_argument(
        "--output-json",
        help="将结果输出为 JSON 文件"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅验证配置，不执行构建"
    )
    
    return parser.parse_args()


def load_from_env() -> dict:
    """从环境变量加载配置"""
    config = {}
    
    env_mappings = {
        "ANDROID_VERSION": "android_version",
        "KERNEL_VERSION": "kernel_version",
        "SUB_LEVEL": "sub_level",
        "OS_PATCH_LEVEL": "os_patch_level",
        "KSU_VERSION": "kernelsu_version",
        "KSU_COMMIT": "kernelsu_commit",
        "SUSFS_COMMIT": "susfs_commit",
        "CUSTOM_VERSION": "custom_version",
        "REVISION": "revision",
    }
    
    for env_key, config_key in env_mappings.items():
        if env_key in os.environ:
            config[config_key] = os.environ[env_key]
    
    # 布尔值
    if os.environ.get("USE_ZRAM", "").lower() in ("false", "0", "no"):
        config["use_zram"] = False
    elif os.environ.get("USE_ZRAM", "").lower() in ("true", "1", "yes"):
        config["use_zram"] = True
    if os.environ.get("USE_KPM", "").lower() in ("false", "0", "no"):
        config["use_kpm"] = False
    if os.environ.get("USE_BBG", "").lower() in ("true", "1", "yes"):
        config["use_bbg"] = True
    if os.environ.get("SUPPORT_OP8E", "").lower() in ("true", "1", "yes"):
        config["support_op8e"] = True
    if os.environ.get("SET_DEFAULT_BBR", "").lower() in ("true", "1", "yes"):
        config["set_default_bbr"] = True
    
    return config


def create_build_config(args: argparse.Namespace) -> BuildConfig:
    """从参数创建构建配置"""
    # 优先使用命令行参数，其次使用环境变量
    env_config = load_from_env()
    
    config_dict = {
        "android_version": args.android or env_config.get("android_version", "android14"),
        "kernel_version": args.kernel or env_config.get("kernel_version", "6.1"),
        "sub_level": args.sub_level or env_config.get("sub_level", "124"),
        "os_patch_level": args.os_patch or env_config.get("os_patch_level", "2025-02"),
        "kernelsu_version": args.ksu_version,
        "kernelsu_commit": args.ksu_commit or env_config.get("kernelsu_commit"),
        "susfs_commit": args.susfs_commit or env_config.get("susfs_commit"),
        "use_zram": args.zram,
        "use_kpm": not args.no_kpm,
        "use_bbg": args.bbg,
        "support_op8e": args.op8e,
        "set_default_bbr": args.bbr,
        "make_release": not args.no_release,
        "custom_version": args.version or env_config.get("custom_version"),
        "revision": args.revision or env_config.get("revision"),
    }
    
    # 验证 commit hash
    if config_dict["kernelsu_commit"] and not validate_commit_hash(config_dict["kernelsu_commit"]):
        raise ValueError(
            f"无效的 SukiSU commit hash: {config_dict['kernelsu_commit']}. "
            "Commit hash 必须是 7-40 位的十六进制字符串."
        )
    
    return BuildConfig(**config_dict)


def list_configs() -> None:
    """列出所有支持的配置"""
    print("\n" + "=" * 60)
    print("支持的 Android/Kernel 版本组合")
    print("=" * 60)
    
    for android, kernels in ANDROID_KERNEL_MAP.items():
        print(f"\n{android.value}:")
        for kernel in kernels:
            configs = DEFAULT_BUILD_MATRIX.get(f"{android.value}-{kernel.value}", [])
            sub_levels = [c["sub_level"] for c in configs]
            print(f"  - {kernel.value}: {', '.join(sub_levels) if sub_levels else 'N/A'}")
    
    print("\n" + "=" * 60)
    print("KernelSU 版本选项")
    print("=" * 60)
    for v in KSUVersion:
        print(f"  - {v.value}")


def list_matrix() -> None:
    """列出所有预定义矩阵"""
    print("\n" + "=" * 60)
    print("预定义构建矩阵")
    print("=" * 60)
    
    for combo, configs in sorted(DEFAULT_BUILD_MATRIX.items()):
        print(f"\n{combo}:")
        for cfg in configs:
            rev = f" (rev: {cfg.get('revision', 'N/A')})" if cfg.get('revision') else ""
            print(f"  - {cfg['sub_level']:>4} | {cfg['os_patch_level']}{rev}")


def get_matrix_configs(matrix_key: str) -> List[dict]:
    """获取指定矩阵的配置"""
    return DEFAULT_BUILD_MATRIX.get(matrix_key, [])


def build_single(config: BuildConfig, workspace: str, dry_run: bool = False) -> BuildResult:
    """构建单个配置"""
    if dry_run:
        logger.info(f"[DRY RUN] 验证配置: {config.config_name}")
        logger.info(f"  Android: {config.android_version}")
        logger.info(f"  Kernel: {config.kernel_version}")
        logger.info(f"  Sub Level: {config.sub_level}")
        logger.info(f"  OS Patch: {config.os_patch_level}")
        logger.info(f"  KSU Version: {config.kernelsu_version}")
        logger.info(f"  KSU Commit: {config.kernelsu_commit or 'latest'}")
        logger.info(f"  ZRAM: {config.use_zram}")
        logger.info(f"  KPM: {config.use_kpm}")
        logger.info(f"  BBG: {config.use_bbg}")
        logger.info(f"  OnePlus 8E: {config.support_op8e}")
        return BuildResult(success=True, config=config, message="配置验证通过 (dry run)")
    
    # 创建构建器
    builder = KernelBuilder(config, workspace)
    
    # 执行构建
    return builder.build()


def build_matrix(matrix_key: str, args: argparse.Namespace, workspace: str) -> List[BuildResult]:
    """构建整个矩阵"""
    logger.info(f"\n{'=' * 60}")
    logger.info(f"开始构建矩阵: {matrix_key}")
    logger.info(f"{'=' * 60}\n")
    
    configs_data = get_matrix_configs(matrix_key)
    if not configs_data:
        logger.error(f"未知的矩阵: {matrix_key}")
        return []
    
    results = []
    
    for cfg_data in configs_data:
        try:
            # 创建配置
            config = BuildConfig(
                android_version=matrix_key.split("-")[0],
                kernel_version=matrix_key.split("-")[1],
                sub_level=cfg_data["sub_level"],
                os_patch_level=cfg_data["os_patch_level"],
                kernelsu_version=args.ksu_version,
                kernelsu_commit=args.ksu_commit,
                use_zram=args.zram,
                use_kpm=not args.no_kpm,
                use_bbg=args.bbg,
                support_op8e=args.op8e,
                set_default_bbr=args.bbr,
                make_release=not args.no_release,
                custom_version=args.version,
                revision=cfg_data.get("revision"),
            )
            
            logger.info(f"\n{'=' * 60}")
            logger.info(f"构建配置: {config.config_name}")
            logger.info(f"{'=' * 60}")
            
            result = build_single(config, workspace, args.dry_run)
            results.append(result)
            
            if result.success:
                logger.info(f"✓ {config.config_name} 构建成功")
            else:
                logger.error(f"✗ {config.config_name} 构建失败: {result.message}")
            
        except Exception as e:
            logger.error(f"配置 {cfg_data} 出错: {e}")
            continue
    
    return results


def build_all(args: argparse.Namespace, workspace: str) -> List[BuildResult]:
    """构建所有矩阵"""
    logger.info(f"\n{'#' * 60}")
    logger.info("# 构建所有配置")
    logger.info(f"{'#' * 60}\n")
    
    all_results = []
    
    for matrix_key in sorted(DEFAULT_BUILD_MATRIX.keys()):
        results = build_matrix(matrix_key, args, workspace)
        all_results.extend(results)
    
    return all_results


def print_summary(results: List[BuildResult], output_json: Optional[str] = None) -> None:
    """打印构建摘要"""
    total = len(results)
    success = sum(1 for r in results if r.success)
    failed = total - success
    
    print("\n" + "=" * 60)
    print("构建摘要")
    print("=" * 60)
    print(f"总数: {total}")
    print(f"成功: \033[92m{success}\033[0m")
    print(f"失败: \033[91m{failed}\033[0m")
    
    if success > 0:
        avg_time = sum(r.build_time or 0 for r in results if r.success) / success
        print(f"平均构建时间: {avg_time:.2f} 秒")
    
    if failed > 0:
        print("\n失败的配置:")
        for r in results:
            if not r.success:
                print(f"  - {r.config.config_name}: {r.message}")
    
    print("=" * 60)
    
    # 输出 JSON
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
            ]
        }
        
        with open(output_json, "w", encoding="utf-8") as f:
            json.dump(json_data, f, indent=2, ensure_ascii=False)
        
        logger.info(f"结果已保存到: {output_json}")


def main():
    """主函数"""
    args = parse_args()
    
    # 设置日志级别
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # 列出配置
    if args.list_configs:
        list_configs()
        return 0
    
    if args.list_matrix:
        list_matrix()
        return 0
    
    # 验证参数
    if not args.all and not args.matrix and not args.android:
        logger.error("请指定 --all, --matrix 或 --android")
        return 1
    
    # 确定工作目录
    workspace = args.workspace
    logger.info(f"工作目录: {workspace}")
    
    # 创建工作目录
    os.makedirs(workspace, exist_ok=True)
    
    results = []
    
    if args.all:
        results = build_all(args, workspace)
    elif args.matrix:
        results = build_matrix(args.matrix, args, workspace)
    else:
        # 单个配置
        try:
            config = create_build_config(args)
            result = build_single(config, workspace, args.dry_run)
            results.append(result)
        except Exception as e:
            logger.error(f"配置错误: {e}")
            return 1
    
    # 打印摘要
    if results:
        print_summary(results, args.output_json)
    
    # 返回状态码
    if results and all(r.success for r in results):
        return 0
    elif results and any(r.success for r in results):
        return 2  # 部分成功
    else:
        return 1  # 全部失败


if __name__ == "__main__":
    sys.exit(main())
