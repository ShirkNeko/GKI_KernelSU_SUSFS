from dataclasses import dataclass
from typing import Optional
from enum import Enum
import re
import urllib.request
import ssl


def get_susfs_version() -> str:
    """从 susfs 仓库获取版本号"""
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    # 尝试多个分支获取版本号
    branches = ["gki-android15-6.6", "gki-android14-6.1", "gki-android13-5.15", "gki-android12-5.10", "main"]
    version_pattern = re.compile(r'#define\s+SUSFS_VERSION\s+"([^"]+)"')

    for branch in branches:
        try:
            url = f"https://raw.githubusercontent.com/ShirkNeko/susfs4ksu/{branch}/kernel_patches/include/linux/susfs.h"
            req = urllib.request.Request(url, headers={'User-Agent': 'Python'})
            with urllib.request.urlopen(req, context=ssl_ctx, timeout=10) as response:
                content = response.read().decode('utf-8')
                match = version_pattern.search(content)
                if match:
                    return match.group(1)
        except Exception:
            continue

    # 如果获取失败，返回默认值
    return "v2.1.0"


# 内核版本号 - 从 susfs 仓库自动获取
KERNEL_VERSION = get_susfs_version()
print(f"SUSFS Version: {KERNEL_VERSION}")


class AndroidVersion(Enum):
    ANDROID12 = "android12"
    ANDROID13 = "android13"
    ANDROID14 = "android14"
    ANDROID15 = "android15"


class KernelVersion(Enum):
    KERNEL_5_10 = "5.10"
    KERNEL_5_15 = "5.15"
    KERNEL_6_1 = "6.1"
    KERNEL_6_6 = "6.6"


class KSUVersion(Enum):
    STABLE = "Stable(标准)"
    DEV = "Dev(开发)"


ANDROID_KERNEL_MAP = {
    AndroidVersion.ANDROID12: [KernelVersion.KERNEL_5_10],
    AndroidVersion.ANDROID13: [KernelVersion.KERNEL_5_10, KernelVersion.KERNEL_5_15],
    AndroidVersion.ANDROID14: [KernelVersion.KERNEL_5_15, KernelVersion.KERNEL_6_1],
    AndroidVersion.ANDROID15: [KernelVersion.KERNEL_6_6],
}

# 仓库配置
KSU_REPO_CONFIG = {"repo_url": "https://github.com/SukiSU-Ultra/SukiSU-Ultra.git",
                    "branch": "main",
                    "setup_script": "https://raw.githubusercontent.com/SukiSU-Ultra/SukiSU-Ultra/main/kernel/setup.sh"}

# SUSFS 仓库配置
SUSFS_REPO_CONFIG = {"repo_url": "https://github.com/ShirkNeko/susfs4ksu.git"}

# SukiSU Patch 仓库配置
SUKISU_PATCH_REPO_CONFIG = {"repo_url": "https://github.com/ShirkNeko/SukiSU_patch.git"}

# AnyKernel3 仓库配置
ANYKERNEL_CONFIG = {"repo_url": "https://github.com/WildPlusKernel/AnyKernel3.git", "branch": "gki-2.0"}

# Kernel Patches 仓库配置
KERNEL_PATCHES_CONFIG = {"repo_url": "https://github.com/Tools-cx-app/kernel_patches.git"}

# Baseband-guard 配置
BBG_CONFIG = {"repo_url": "https://github.com/vc-teahouse/Baseband-guard.git",
              "setup_script": "https://github.com/vc-teahouse/Baseband-guard/raw/main/setup.sh"}

# 工具链配置
TOOLCHAIN_CONFIG = {"aosp_mirror": "https://android.googlesource.com",
                    "build_tools_branch": "main-kernel-build-2024",
                    "mkbootimg_branch": "main-kernel-build-2024"}
LEGACY_FIXES = {
    "android13-5.15-below-123": {"url": "https://github.com/zzh20188/GKI_KernelSU_SUSFS/raw/refs/heads/legacy/fix_5.15.legacy", "min_sub_level": 123},
    "android12-5.10-below-136": {"url": "https://github.com/zzh20188/GKI_KernelSU_SUSFS/raw/refs/heads/legacy/fdinfo.c.patch", "min_sub_level": 136},
}
OP8E_PATCH_URL = "https://github.com/zzh20188/GKI_KernelSU_SUSFS/raw/refs/heads/dev/hmbird_patch.c"
KPM_PATCH_URL = "https://raw.githubusercontent.com/ShirkNeko/SukiSU_patch/refs/heads/main/kpm/patch_linux"


@dataclass
class BuildConfig:
    android_version: str
    kernel_version: str
    sub_level: str
    os_patch_level: str
    kernelsu_version: str = "Stable(标准)"
    kernelsu_commit: Optional[str] = None
    susfs_commit: Optional[str] = None
    use_zram: bool = False
    use_kpm: bool = True
    use_bbg: bool = False
    support_op8e: bool = False
    set_default_bbr: bool = False
    make_release: bool = True
    custom_version: Optional[str] = None
    revision: Optional[str] = None
    build_id: Optional[str] = None

    def __post_init__(self):
        self._validate_android_version()
        self._validate_kernel_version()
        self._validate_kernel_android_compat()
        self._validate_sub_level()
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

    def _set_build_id(self):
        if self.build_id is None:
            self.build_id = f"{self.android_version}-{self.kernel_version}-{self.sub_level}-{self.os_patch_level}"

    @property
    def config_name(self) -> str:
        return f"{self.android_version}-{self.kernel_version}-{self.sub_level}"

    @property
    def formatted_branch(self) -> str:
        return f"{self.android_version}-{self.kernel_version}-{self.os_patch_level}"

    @property
    def kernel_branch(self) -> str:
        return f"gki-{self.android_version}-{self.kernel_version}"

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
            "kernelsu_commit": self.kernelsu_commit,
            "use_zram": self.use_zram,
            "use_kpm": self.use_kpm,
            "use_bbg": self.use_bbg,
            "support_op8e": self.support_op8e,
            "set_default_bbr": self.set_default_bbr,
            "make_release": self.make_release,
            "custom_version": self.custom_version,
            "revision": self.revision,
            "build_id": self.build_id,
        }


def validate_commit_hash(commit_hash: str) -> bool:
    return bool(re.match(r'^[0-9a-f]{7,40}$', commit_hash, re.IGNORECASE))
