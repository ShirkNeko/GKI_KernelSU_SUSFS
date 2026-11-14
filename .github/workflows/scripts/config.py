"""构建系统共享配置。

仓库地址、版本枚举和构建参数校验集中在这里。模块导入时不产生网络请求，
SUSFS 版本号在首次调用 get_susfs_version() 时才去仓库读取。
"""
from __future__ import annotations

import logging
import re
import ssl
import urllib.request
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

_logger = logging.getLogger("config")


# ---------------------------------------------------------------------------
# SUSFS 版本
# ---------------------------------------------------------------------------

SUSFS_VERSION_FALLBACK = "v2.4.0"
SUSFS_VERSION_BRANCHES = (
    "gki-android17-6.18",
    "gki-android16-6.12",
    "gki-android15-6.6",
    "gki-android14-6.1",
    "gki-android13-5.15",
    "gki-android12-5.10",
    "main",
)
_SUSFS_VERSION_PATTERN = re.compile(r'#define\s+SUSFS_VERSION\s+"([^"]+)"')
_susfs_version_cache: Optional[str] = None


def get_susfs_version() -> str:
    """从 ShirkNeko/susfs4ksu 读取 SUSFS_VERSION，结果缓存，读取失败用已知版本兜底。"""
    global _susfs_version_cache
    if _susfs_version_cache is not None:
        return _susfs_version_cache

    context = ssl.create_default_context()
    for branch in SUSFS_VERSION_BRANCHES:
        url = (
            f"https://raw.githubusercontent.com/ShirkNeko/susfs4ksu/{branch}"
            "/kernel_patches/include/linux/susfs.h"
        )
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "GKI-Kernel-Builder"}
            )
            with urllib.request.urlopen(request, context=context, timeout=10) as response:
                match = _SUSFS_VERSION_PATTERN.search(response.read().decode("utf-8"))
        except Exception:
            continue
        if match:
            _susfs_version_cache = match.group(1)
            return _susfs_version_cache

    _susfs_version_cache = SUSFS_VERSION_FALLBACK
    return _susfs_version_cache


# ---------------------------------------------------------------------------
# 版本枚举
# ---------------------------------------------------------------------------

class AndroidVersion(Enum):
    ANDROID12 = "android12"
    ANDROID13 = "android13"
    ANDROID14 = "android14"
    ANDROID15 = "android15"
    ANDROID16 = "android16"
    ANDROID17 = "android17"


class KernelVersion(Enum):
    KERNEL_5_10 = "5.10"
    KERNEL_5_15 = "5.15"
    KERNEL_6_1 = "6.1"
    KERNEL_6_6 = "6.6"
    KERNEL_6_12 = "6.12"
    KERNEL_6_18 = "6.18"


class KSUVersion(Enum):
    STABLE = "Stable(标准)"
    DEV = "Dev(开发)"
    CUSTOM = "Custom(自定义)"


ANDROID_KERNEL_MAP = {
    AndroidVersion.ANDROID12: [KernelVersion.KERNEL_5_10],
    AndroidVersion.ANDROID13: [KernelVersion.KERNEL_5_10, KernelVersion.KERNEL_5_15],
    AndroidVersion.ANDROID14: [KernelVersion.KERNEL_5_15, KernelVersion.KERNEL_6_1],
    AndroidVersion.ANDROID15: [KernelVersion.KERNEL_6_6],
    AndroidVersion.ANDROID16: [KernelVersion.KERNEL_6_12],
    AndroidVersion.ANDROID17: [KernelVersion.KERNEL_6_18],
}

# Stable / Dev 固定映射到 SukiSU-Ultra 的构建分支，Custom 使用用户输入的分支。
KSU_BRANCH_BY_VERSION = {
    KSUVersion.STABLE.value: "builtin",
    KSUVersion.DEV.value: "builtin-new",
}
KSU_CUSTOM_FALLBACK_BRANCH = KSU_BRANCH_BY_VERSION[KSUVersion.DEV.value]

PAGE_SIZES = ("4k", "16k")
# Kleaf --page_size 只在这些构建分支上可用
PAGE_SIZE_16K_KERNELS = ("6.1", "6.6", "6.12", "6.18")


# ---------------------------------------------------------------------------
# 仓库配置
# ---------------------------------------------------------------------------

KSU_REPO_CONFIG = {
    "repo_url": "https://github.com/SukiSU-Ultra/SukiSU-Ultra.git",
    # setup.sh 固定从 main 获取；脚本本身接受任意分支 / tag / commit 作为第一个参数
    "setup_script": "https://raw.githubusercontent.com/SukiSU-Ultra/SukiSU-Ultra/main/kernel/setup.sh",
}

SUSFS_REPO_CONFIG = {
    # 5.10 / 5.15 / 6.1 / 6.6 使用 ShirkNeko fork；6.12 目前只有 simonpunk 分支。
    "default_url": "https://github.com/ShirkNeko/susfs4ksu.git",
    "android16_url": "https://gitlab.com/simonpunk/susfs4ksu.git",
}
SUSFS_618_BRANCH = "gki-android17-6.18"
SUSFS_618_BUNDLE = Path(__file__).resolve().parent.parent / "vendor" / "susfs618" / "kernel_patches"
SUSFS_COMPAT_SCRIPTS = Path(__file__).resolve().parent.parent / "vendor" / "scripts"

SUKISU_PATCH_REPO_CONFIG = {"repo_url": "https://github.com/ShirkNeko/SukiSU_patch.git"}

ANYKERNEL_CONFIG = {
    "repo_url": "https://github.com/WildPlusKernel/AnyKernel3.git",
    "branch": "gki-2.0",
}

KERNEL_PATCHES_CONFIG = {"repo_url": "https://github.com/Tools-cx-app/kernel_patches.git"}

BBG_CONFIG = {
    "repo_url": "https://github.com/vc-teahouse/Baseband-guard.git",
    "setup_script": "https://github.com/vc-teahouse/Baseband-guard/raw/main/setup.sh",
}

TOOLCHAIN_CONFIG = {
    "aosp_mirror": "https://android.googlesource.com",
    "build_tools_branch": "main-kernel-build-2024",
    "mkbootimg_branch": "main-kernel-build-2024",
}

LEGACY_FIXES = {
    "android13-5.15-below-123": {
        "url": "https://github.com/zzh20188/GKI_KernelSU_SUSFS/raw/refs/heads/legacy/fix_5.15.legacy",
        "min_sub_level": 123,
    },
    "android12-5.10-below-136": {
        "url": "https://github.com/zzh20188/GKI_KernelSU_SUSFS/raw/refs/heads/legacy/fdinfo.c.patch",
        "min_sub_level": 136,
    },
}
OP8E_PATCH_URL = "https://github.com/zzh20188/GKI_KernelSU_SUSFS/raw/refs/heads/dev/hmbird_patch.c"

# KPM 修补工具（SukiSU_KernelPatch_patch releases）
KPM_TOOLS_VERSION = "0.14.0"
KPM_TOOLS_BASE = (
    "https://github.com/SukiSU-Ultra/SukiSU_KernelPatch_patch/releases/download/"
    f"{KPM_TOOLS_VERSION}"
)
KPM_KPIMG_URL = f"{KPM_TOOLS_BASE}/kpimg"
KPM_KPTOOLS_URL = f"{KPM_TOOLS_BASE}/kptools-linux"
KPM_PATCH_SDK_VERSION = "123"
# KPM 修补只在这些内核线上生效（6.6+ 不支持，16k 也会跳过）
KPM_SUPPORTED_KERNELS = ("5.10", "5.15", "6.1")


# ---------------------------------------------------------------------------
# 构建配置
# ---------------------------------------------------------------------------

_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/~^-]*$")
_OS_PATCH_PATTERN = re.compile(r"^\d{4}-\d{2}$")


def validate_ref(ref: str) -> bool:
    """校验 git 分支 / tag / commit，拒绝会被 shell 解释的字符。"""
    return bool(_REF_PATTERN.match(ref)) and ".." not in ref


@dataclass
class BuildConfig:
    android_version: str
    kernel_version: str
    sub_level: str
    os_patch_level: str
    kernelsu_version: str = KSUVersion.STABLE.value
    kernelsu_branch: Optional[str] = None
    kernelsu_commit: Optional[str] = None
    susfs_commit: Optional[str] = None
    use_zram: bool = False
    use_kpm: bool = True
    use_susfs: bool = True
    use_bbg: bool = False
    support_op8e: bool = False
    set_default_bbr: bool = False
    custom_version: Optional[str] = None
    revision: Optional[str] = None
    page_size: str = "4k"
    build_id: Optional[str] = None

    def __post_init__(self):
        self._validate_android_version()
        self._validate_kernel_version()
        self._validate_kernel_android_compat()
        self._validate_sub_level()
        self._validate_os_patch_level()
        self._validate_ksu_config()
        self._validate_page_size()
        self._set_build_id()

    def _validate_android_version(self):
        valid = [v.value for v in AndroidVersion]
        if self.android_version not in valid:
            raise ValueError(f"无效的 Android 版本: {self.android_version}. 支持: {', '.join(valid)}")

    def _validate_kernel_version(self):
        valid = [v.value for v in KernelVersion]
        if self.kernel_version not in valid:
            raise ValueError(f"无效的 Kernel 版本: {self.kernel_version}. 支持: {', '.join(valid)}")

    def _validate_kernel_android_compat(self):
        av = AndroidVersion(self.android_version)
        kv = KernelVersion(self.kernel_version)
        if kv not in ANDROID_KERNEL_MAP.get(av, []):
            raise ValueError(f"Android {self.android_version} 不支持 Kernel {self.kernel_version}")

    def _validate_sub_level(self):
        if self.sub_level != "X" and not self.sub_level.isdigit():
            raise ValueError(f"无效的 sub_level: {self.sub_level}")

    def _validate_os_patch_level(self):
        if self.os_patch_level != "lts" and not _OS_PATCH_PATTERN.match(self.os_patch_level):
            raise ValueError(f"无效的 os_patch_level: {self.os_patch_level}")

    def _validate_ksu_config(self):
        valid = [v.value for v in KSUVersion]
        if self.kernelsu_version not in valid:
            raise ValueError(
                f"无效的 SukiSU 版本: {self.kernelsu_version}. 支持: {', '.join(valid)}"
            )
        if self.kernelsu_commit and not validate_ref(self.kernelsu_commit):
            raise ValueError(f"无效的 SukiSU commit/ref: {self.kernelsu_commit}")
        if self.kernelsu_branch and not validate_ref(self.kernelsu_branch):
            raise ValueError(f"无效的 SukiSU 分支: {self.kernelsu_branch}")
        if not self.use_susfs and self.kernelsu_version == KSUVersion.STABLE.value:
            # builtin 分支没有 CONFIG_KSU_HACK_ARM64_BRANCH_LINK，无 SUSFS 时必须用 builtin-new
            _logger.warning(
                "未启用 SUSFS：builtin 分支没有 branch link 钩子，自动改用 Dev(builtin-new)"
            )
            self.kernelsu_version = KSUVersion.DEV.value

    def _validate_page_size(self):
        if self.page_size not in PAGE_SIZES:
            raise ValueError(f"无效的 page_size: {self.page_size}. 支持: {', '.join(PAGE_SIZES)}")
        if self.page_size == "16k" and self.kernel_version not in PAGE_SIZE_16K_KERNELS:
            supported = ", ".join(PAGE_SIZE_16K_KERNELS)
            raise ValueError(
                f"Kernel {self.kernel_version} 不支持 16k page size. 支持: {supported}"
            )

    def _set_build_id(self):
        if self.build_id is None:
            self.build_id = f"{self.android_version}-{self.kernel_version}-{self.sub_level}-{self.os_patch_level}"

    @property
    def config_name(self) -> str:
        name = f"{self.android_version}-{self.kernel_version}-{self.sub_level}"
        if self.page_size == "16k":
            name += "-16k"
        return name

    @property
    def formatted_branch(self) -> str:
        return f"{self.android_version}-{self.kernel_version}-{self.os_patch_level}"

    @property
    def kernel_branch(self) -> str:
        return f"gki-{self.android_version}-{self.kernel_version}"

    @property
    def artifact_prefix(self) -> str:
        base = f"{self.android_version}-{self.kernel_version}.{self.sub_level}-{self.os_patch_level}"
        if self.page_size == "16k":
            base += "-16k"
        return base

    @property
    def kernelsu_ref(self) -> str:
        """实际传给 SukiSU setup.sh 的分支 / tag / commit。"""
        if self.kernelsu_commit:
            return self.kernelsu_commit.strip()
        if self.kernelsu_version == KSUVersion.CUSTOM.value:
            branch = (self.kernelsu_branch or "").strip()
            return branch or KSU_CUSTOM_FALLBACK_BRANCH
        return KSU_BRANCH_BY_VERSION[self.kernelsu_version]

    @property
    def ignored_kernelsu_branch(self) -> Optional[str]:
        """Stable / Dev 模式下填写了不会生效的分支时返回该值，用于提示。"""
        if not self.kernelsu_branch or self.kernelsu_version == KSUVersion.CUSTOM.value:
            return None
        if self.kernelsu_branch == KSU_BRANCH_BY_VERSION.get(self.kernelsu_version):
            # 与版本映射一致（build-wsl 以前会自动带上），不算被忽略
            return None
        return self.kernelsu_branch

    def get_susfs_patch_filename(self) -> str:
        return f"50_add_susfs_in_gki-{self.android_version}-{self.kernel_version}.patch"

    def is_lts(self) -> bool:
        return self.sub_level == "X"

    def get_sub_level_int(self) -> Optional[int]:
        return None if self.sub_level == "X" else int(self.sub_level)

    def to_dict(self) -> dict:
        return {
            "android_version": self.android_version,
            "kernel_version": self.kernel_version,
            "sub_level": self.sub_level,
            "os_patch_level": self.os_patch_level,
            "kernelsu_version": self.kernelsu_version,
            "kernelsu_branch": self.kernelsu_branch,
            "kernelsu_ref": self.kernelsu_ref,
            "kernelsu_commit": self.kernelsu_commit,
            "susfs_commit": self.susfs_commit,
            "use_zram": self.use_zram,
            "use_kpm": self.use_kpm,
            "use_susfs": self.use_susfs,
            "use_bbg": self.use_bbg,
            "support_op8e": self.support_op8e,
            "set_default_bbr": self.set_default_bbr,
            "custom_version": self.custom_version,
            "revision": self.revision,
            "page_size": self.page_size,
            "build_id": self.build_id,
        }
