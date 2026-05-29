from dataclasses import dataclass, field
from typing import Optional
from enum import Enum
import re


class AndroidVersion(Enum):
    """支持的 Android 版本"""
    ANDROID12 = "android12"
    ANDROID13 = "android13"
    ANDROID14 = "android14"
    ANDROID15 = "android15"
    ANDROID16 = "android16"


class KernelVersion(Enum):
    """支持的 Kernel 版本"""
    KERNEL_5_10 = "5.10"
    KERNEL_5_15 = "5.15"
    KERNEL_6_1 = "6.1"
    KERNEL_6_6 = "6.6"
    KERNEL_6_12 = "6.12"


class KSUVersion(Enum):
    """KernelSU 版本选项"""
    STABLE = "Stable(标准)"
    DEV = "Dev(开发)"


# 内核版本与 Android 版本的映射关系
ANDROID_KERNEL_MAP = {
    AndroidVersion.ANDROID12: [KernelVersion.KERNEL_5_10],
    AndroidVersion.ANDROID13: [KernelVersion.KERNEL_5_10, KernelVersion.KERNEL_5_15],
    AndroidVersion.ANDROID14: [KernelVersion.KERNEL_5_15, KernelVersion.KERNEL_6_1],
    AndroidVersion.ANDROID15: [KernelVersion.KERNEL_6_6],
    AndroidVersion.ANDROID16: [KernelVersion.KERNEL_6_12],
}

# KernelSU 仓库配置
KSU_REPO_CONFIG = {
    "repo_url": "https://github.com/SukiSU-Ultra/SukiSU-Ultra.git",
    "branch": "main",
    "setup_script": "https://raw.githubusercontent.com/SukiSU-Ultra/SukiSU-Ultra/main/kernel/setup.sh",
}

# SUSFS 仓库配置
SUSFS_REPO_CONFIG = {
    "repo_url": "https://github.com/ShirkNeko/susfs4ksu.git",
}

# SukiSU Patch 仓库配置
SUKISU_PATCH_REPO_CONFIG = {
    "repo_url": "https://github.com/ShirkNeko/SukiSU_patch.git",
}

# AnyKernel3 仓库配置
ANYKERNEL_CONFIG = {
    "repo_url": "https://github.com/WildPlusKernel/AnyKernel3.git",
    "branch": "gki-2.0",
}

# Kernel Patches 仓库配置
KERNEL_PATCHES_CONFIG = {
    "repo_url": "https://github.com/Tools-cx-app/kernel_patches.git",
}

# Baseband-guard 配置
BBG_CONFIG = {
    "repo_url": "https://github.com/vc-teahouse/Baseband-guard.git",
    "setup_script": "https://github.com/vc-teahouse/Baseband-guard/raw/main/setup.sh",
}

# 工具链配置
TOOLCHAIN_CONFIG = {
    "aosp_mirror": "https://android.googlesource.com",
    "build_tools_branch": "main-kernel-build-2024",
    "mkbootimg_branch": "main-kernel-build-2024",
}

# 旧版内核兼容补丁
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

# OnePlus 8E 支持补丁
OP8E_PATCH_URL = "https://github.com/zzh20188/GKI_KernelSU_SUSFS/raw/refs/heads/dev/hmbird_patch.c"

# KPM Image 补丁
KPM_PATCH_URL = "https://raw.githubusercontent.com/ShirkNeko/SukiSU_patch/refs/heads/main/kpm/patch_linux"


@dataclass
class BuildConfig:
    """构建配置数据类"""
    
    # 必需参数
    android_version: str
    kernel_version: str
    sub_level: str
    os_patch_level: str
    
    # KernelSU 配置
    kernelsu_version: str = "Stable(标准)"
    kernelsu_commit: Optional[str] = None  # 支持指定 commit hash
    
    # SUSFS 配置
    susfs_commit: Optional[str] = None  # 支持指定 commit hash
    
    # 可选功能开关
    use_zram: bool = False
    use_kpm: bool = True
    use_bbg: bool = False
    support_op8e: bool = False
    set_default_bbr: bool = False
    
    # 发布配置
    make_release: bool = True
    custom_version: Optional[str] = None
    
    # Android 12 特定参数
    revision: Optional[str] = None
    
    # 构建标识
    build_id: Optional[str] = None
    
    def __post_init__(self):
        """验证配置有效性"""
        self._validate_android_version()
        self._validate_kernel_version()
        self._validate_kernel_android_compat()
        self._validate_sub_level()
        self._set_build_id()
    
    def _validate_android_version(self):
        """验证 Android 版本"""
        valid_versions = [v.value for v in AndroidVersion]
        if self.android_version not in valid_versions:
            raise ValueError(
                f"无效的 Android 版本: {self.android_version}. "
                f"支持的版本: {', '.join(valid_versions)}"
            )
    
    def _validate_kernel_version(self):
        """验证 Kernel 版本"""
        valid_versions = [v.value for v in KernelVersion]
        if self.kernel_version not in valid_versions:
            raise ValueError(
                f"无效的 Kernel 版本: {self.kernel_version}. "
                f"支持的版本: {', '.join(valid_versions)}"
            )
    
    def _validate_kernel_android_compat(self):
        """验证 Kernel 版本与 Android 版本的兼容性"""
        av = AndroidVersion(self.android_version)
        kv = KernelVersion(self.kernel_version)
        
        if kv not in ANDROID_KERNEL_MAP.get(av, []):
            raise ValueError(
                f"Android {self.android_version} 不支持 Kernel {self.kernel_version}. "
                f"支持的 Kernel 版本: {[v.value for v in ANDROID_KERNEL_MAP.get(av, [])]}"
            )
    
    def _validate_sub_level(self):
        """验证 sub_level"""
        if self.sub_level != "X" and not self.sub_level.isdigit():
            raise ValueError(
                f"无效的 sub_level: {self.sub_level}. "
                f"必须是数字或 'X' (LTS)"
            )
    
    def _set_build_id(self):
        """设置构建 ID"""
        if self.build_id is None:
            self.build_id = (
                f"{self.android_version}-{self.kernel_version}-{self.sub_level}-"
                f"{self.os_patch_level}"
            )
    
    @property
    def config_name(self) -> str:
        """获取配置名称 (用于环境和目录)"""
        return f"{self.android_version}-{self.kernel_version}-{self.sub_level}"
    
    @property
    def formatted_branch(self) -> str:
        """获取格式化后的分支名称"""
        return f"{self.android_version}-{self.kernel_version}-{self.os_patch_level}"
    
    @property
    def kernel_branch(self) -> str:
        """获取 SUSFS 使用的内核分支名称"""
        return f"gki-{self.android_version}-{self.kernel_version}"
    
    @property
    def ksu_branch_arg(self) -> str:
        """获取 KernelSU 的分支参数"""
        if self.kernelsu_version == KSUVersion.STABLE.value:
            return "-s builtin"
        else:  # Dev
            return "-s builtin"
    
    def get_susfs_patch_filename(self) -> str:
        """获取 SUSFS 补丁文件名"""
        return f"50_add_susfs_in_gki-{self.android_version}-{self.kernel_version}.patch"
    
    def is_lts(self) -> bool:
        """是否是 LTS 版本"""
        return self.sub_level == "X"
    
    def get_sub_level_int(self) -> Optional[int]:
        """获取 sub_level 的整数值"""
        if self.sub_level == "X":
            return None
        return int(self.sub_level)
    
    def to_dict(self) -> dict:
        """转换为字典"""
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
    
    @classmethod
    def from_dict(cls, data: dict) -> "BuildConfig":
        """从字典创建配置"""
        return cls(**data)


@dataclass
class SubLevelConfig:
    """Sub level 配置"""
    sub_level: str
    os_patch_level: str
    revision: Optional[str] = None
    
    @classmethod
    def from_string(cls, s: str) -> "SubLevelConfig":
        """从字符串解析 (格式: sub_level:os_patch_level[:revision])"""
        parts = s.split(":")
        return cls(
            sub_level=parts[0],
            os_patch_level=parts[1],
            revision=parts[2] if len(parts) > 2 else None,
        )


@dataclass
class KernelMatrix:
    """内核矩阵配置"""
    android_version: str
    kernel_version: str
    sub_levels: list = field(default_factory=list)
    
    @classmethod
    def from_yaml_dict(cls, data: dict, android_version: str, kernel_version: str) -> "KernelMatrix":
        """从 YAML 字典创建"""
        sub_levels = []
        for item in data.get("include", []):
            sub_levels.append(SubLevelConfig(
                sub_level=str(item["sub_level"]),
                os_patch_level=item["os_patch_level"],
                revision=item.get("revision"),
            ))
        return cls(
            android_version=android_version,
            kernel_version=kernel_version,
            sub_levels=sub_levels,
        )
    
    def generate_configs(self, base_config: dict) -> list:
        """生成所有 BuildConfig"""
        configs = []
        for sl in self.sub_levels:
            config_dict = base_config.copy()
            config_dict.update({
                "sub_level": sl.sub_level,
                "os_patch_level": sl.os_patch_level,
                "revision": sl.revision,
            })
            configs.append(BuildConfig(**config_dict))
        return configs


def validate_commit_hash(commit_hash: str) -> bool:
    """验证 commit hash 格式"""
    # Git commit hash 应该是 40 位十六进制字符串 (完整) 或至少 7 位
    if re.match(r'^[0-9a-f]{7,40}$', commit_hash, re.IGNORECASE):
        return True
    return False


def get_ksu_branch_from_version(version: str) -> str:
    """根据版本类型获取 KSU 分支"""
    if version == KSUVersion.STABLE.value:
        return "main"  # 或者获取最新 tag
    elif version == KSUVersion.DEV.value:
        return "main"
    return "main"
