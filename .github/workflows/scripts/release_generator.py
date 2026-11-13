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

from config import get_susfs_version  # noqa: E402
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
    def __init__(self, build_info_dir=None):
        self.build_infos = load_build_infos(build_info_dir)
        self.ssl_ctx = ssl.create_default_context()

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
        susfs_version = get_susfs_version()

        configurations = [
            f"- Android {key.split('-', 1)[0].replace('android', '')} "
            f"(Kernel {key.split('-', 1)[1]})"
            + (" + 16K" if any(c.get("page_size") == "16k" for c in rows) else "")
            for key, rows in sorted(matrix.items())
        ]

        lines = [
            f"## GKI Kernel with SukiSU & SUSFS {susfs_version}",
            "",
            "### SukiSU Info",
        ]
        if ksu.get("version"):
            lines.append(f"- Version: `{ksu['version']}`")
        if ksu.get("ref"):
            lines.append(f"- Ref: `{ksu['ref']}`")
        lines.append(f"- Commit: `{ksu['commit']}`")
        lines.extend([
            "",
            "### Supported Configurations",
            *configurations,
            "",
            "### Features",
            f"- SUSFS {susfs_version}",
            "- Manual Syscall Hooks",
            "- Magic Mount Support",
            "- BBR Support",
            "- LZ4KD Support",
        ])
        return "\n".join(lines)

    def save_body(self, output_path: str = "RELEASE_BODY.md"):
        body = self.generate_body()
        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(body, encoding="utf-8")
        print(body)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 Release 发布说明")
    parser.add_argument("output", nargs="?", default="RELEASE_BODY.md")
    parser.add_argument("--build-info-dir", default=None, help="包含 build-info-*.json 的目录")
    args = parser.parse_args()

    ReleaseGenerator(args.build_info_dir).save_body(args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
