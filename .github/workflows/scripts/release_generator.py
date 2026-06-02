#!/usr/bin/env python3
import json
import urllib.request
import ssl
import sys
from pathlib import Path
import sys as _sys
_sys.path.insert(0, str(Path(__file__).parent))
from config import KERNEL_VERSION


class ReleaseGenerator:
    def __init__(self):
        self.matrix_path = Path(__file__).parent.parent / "config" / "matrix.json"
        self.ssl_ctx = ssl.create_default_context()
        self.ssl_ctx.check_hostname = False
        self.ssl_ctx.verify_mode = ssl.CERT_NONE

    def load_matrix(self) -> dict:
        with open(self.matrix_path, 'r') as f:
            return json.load(f)

    def _fetch_json(self, url: str) -> dict:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Python'})
            with urllib.request.urlopen(req, context=self.ssl_ctx) as response:
                return json.loads(response.read())
        except Exception:
            return {}

    def get_ksu_info(self) -> tuple:
        ksu_tag, ksu_commit = "latest", "unknown"
        tags = self._fetch_json("https://api.github.com/repos/SukiSU-Ultra/SukiSU-Ultra/git/refs/tags")
        if tags:
            ksu_tag = tags[-1]['ref'].split('/')[-1]
        ref = self._fetch_json("https://api.github.com/repos/SukiSU-Ultra/SukiSU-Ultra/git/ref/heads/main")
        if ref:
            ksu_commit = ref['object']['sha'][:7]
        return ksu_tag, ksu_commit

    def generate_body(self) -> str:
        matrix = self.load_matrix()
        ksu_tag, ksu_commit = self.get_ksu_info()
        configs = [f"- Android {k.split('-')[0].replace('android', '')} (Kernel {k.split('-')[1]})" for k in sorted(matrix.keys())]
        return '\n'.join([
            f"## GKI Kernel with SukiSU & SUSFS {KERNEL_VERSION}", "",
            "### SukiSU Info",
            f"- Tag: `{ksu_tag}`",
            f"- Commit: `{ksu_commit}`", "",
            "### Supported Configurations",
            *configs, "",
            "### Features",
            f"- SUSFS {KERNEL_VERSION}", "- Manual Syscall Hooks", "- Magic Mount Support", "- BBR Support", "- LZ4KD Support",
        ])

    def save_body(self, output_path: str = "RELEASE_BODY.md"):
        body = self.generate_body()
        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)
        with open(output_file, 'w') as f:
            f.write(body)
        print(body)


if __name__ == '__main__':
    ReleaseGenerator().save_body(sys.argv[1] if len(sys.argv) > 1 else "RELEASE_BODY.md")
