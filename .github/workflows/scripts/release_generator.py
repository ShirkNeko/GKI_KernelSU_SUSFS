#!/usr/bin/env python3
"""生成 GitHub Release 的发布说明。

优先读取构建 job 上传的 build-info-*.json，用实际构建出来的分支和 commit；
没有构建信息时才回退到 GitHub API。
"""
import argparse
import json
import ssl
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import (  # noqa: E402
    KPM_PATCH_SDK_VERSION,
    KPM_SUPPORTED_KERNELS,
    KPM_TOOLS_VERSION,
    get_susfs_version,
)
from matrix import load_matrix  # noqa: E402


def _unique(values) -> list:
    return sorted({v for v in values if v})


def load_build_infos(directory) -> list:
    if not directory:
        return []
    infos = []
    for path in sorted(Path(directory).glob("build-info-*.json")):
        try:
            infos.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return infos


class ReleaseGenerator:
    def __init__(self, build_info_dir=None, use_zram: bool = False,
                 set_default_bbr: bool = False, use_susfs: bool = True,
                 use_kpm: bool = True):
        self.build_infos = load_build_infos(build_info_dir)
        self.ssl_ctx = ssl.create_default_context()
        self.use_zram = bool(use_zram)
        self.set_default_bbr = bool(set_default_bbr)
        self.use_susfs = bool(use_susfs)
        self.use_kpm = bool(use_kpm)

    def _fetch_json(self, url: str) -> dict:
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "GKI-Kernel-Builder"})
            with urllib.request.urlopen(request, context=self.ssl_ctx, timeout=15) as response:
                return json.loads(response.read())
        except Exception:
            return {}

    def _fallback_ksu_info(self) -> dict:
        info = {"version": "", "ref": "main", "commit": "unknown"}
        ref = self._fetch_json(
            "https://api.github.com/repos/SukiSU-Ultra/SukiSU-Ultra/git/ref/heads/main"
        )
        if ref:
            info["commit"] = ref["object"]["sha"][:7]
        tags = self._fetch_json(
            "https://api.github.com/repos/SukiSU-Ultra/SukiSU-Ultra/git/refs/tags"
        )
        if tags:
            info["version"] = tags[-1]["ref"].split("/")[-1]
        return info

    def _build_info_summary(self) -> dict:
        versions = _unique(
            i.get("config", {}).get("kernelsu_version") for i in self.build_infos
        )
        refs = _unique(i.get("ksu", {}).get("resolved_ref") for i in self.build_infos)
        commits = _unique(i.get("ksu", {}).get("commit") for i in self.build_infos)
        return {
            "version": ", ".join(versions),
            "ref": ", ".join(refs),
            "commit": ", ".join(commits),
        }

    def get_ksu_info(self) -> dict:
        if self.build_infos:
            info = self._build_info_summary()
            if info["ref"] or info["commit"]:
                return info
        return self._fallback_ksu_info()

    def generate_body(self) -> str:
        matrix = load_matrix()
        ksu = self.get_ksu_info()

        use_susfs = self.use_susfs
        if self.build_infos:
            infos = [i.get("config", {}).get("use_susfs") for i in self.build_infos]
            if any(value is not None for value in infos):
                use_susfs = any(value for value in infos if value is not None)
        susfs_version = get_susfs_version() if use_susfs else ""
        title = "## GKI Kernel with SukiSU"
        if use_susfs:
            title += f" & SUSFS {susfs_version}"

        configurations = [
            f"- Android {key.split('-', 1)[0].replace('android', '')} "
            f"(Kernel {key.split('-', 1)[1]})"
            + (" + 16K" if any(c.get("page_size") == "16k" for c in rows) else "")
            for key, rows in sorted(matrix.items())
        ]

        lines = [
            title,
            "",
            "### SukiSU Info",
        ]
        if ksu.get("version"):
            lines.append(f"- Version: `{ksu['version']}`")
        if ksu.get("ref"):
            lines.append(f"- Ref: `{ksu['ref']}`")
        lines.append(f"- Commit: `{ksu['commit']}`")
        use_zram = self.use_zram or any(
            i.get("config", {}).get("use_zram") for i in self.build_infos
        )
        set_default_bbr = self.set_default_bbr or any(
            i.get("config", {}).get("set_default_bbr") for i in self.build_infos
        )
        features = []
        if use_susfs:
            features.append(f"- SUSFS {susfs_version}")
        use_kpm = self.use_kpm
        if self.build_infos:
            infos = [i.get("config", {}).get("use_kpm") for i in self.build_infos]
            if any(value is not None for value in infos):
                use_kpm = any(value for value in infos if value is not None)
        if use_kpm:
            kernels = [
                kernel
                for kernel in KPM_SUPPORTED_KERNELS
                if any(key.endswith(f"-{kernel}") for key in matrix)
            ] or list(KPM_SUPPORTED_KERNELS)
            features.append(
                f"- KPM {KPM_TOOLS_VERSION} "
                f"(SDK {KPM_PATCH_SDK_VERSION}, Kernel {' / '.join(kernels)})"
            )
        if set_default_bbr:
            features.append("- BBR Support")
        if use_zram:
            features.append("- LZ4KD Support")

        lines.extend([
            "",
            "### Supported Configurations",
            *configurations,
        ])
        if features:
            lines.extend([
                "",
                "### Features",
                *features,
            ])
        return "\n".join(lines)

    def save_body(self, output_path: str = "RELEASE_BODY.md"):
        body = self.generate_body()
        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(body, encoding="utf-8")
        print(body)


def _bool_arg(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 Release 发布说明")
    parser.add_argument("output", nargs="?", default="RELEASE_BODY.md")
    parser.add_argument("--build-info-dir", default=None, help="包含 build-info-*.json 的目录")
    parser.add_argument("--zram", nargs="?", const=True, default=False,
                        help="本次构建启用 ZRAM(LZ4KD)")
    parser.add_argument("--bbr", nargs="?", const=True, default=False,
                        help="本次构建启用 BBR")
    parser.add_argument("--susfs", nargs="?", const=True, default=True,
                        help="本次构建启用 SUSFS")
    parser.add_argument("--kpm", nargs="?", const=True, default=True,
                        help="本次构建启用 KPM")
    args = parser.parse_args()

    ReleaseGenerator(
        args.build_info_dir,
        use_zram=_bool_arg(args.zram),
        set_default_bbr=_bool_arg(args.bbr),
        use_susfs=_bool_arg(args.susfs),
        use_kpm=_bool_arg(args.kpm),
    ).save_body(args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
