#!/usr/bin/env python3
"""为 GitHub Actions 生成构建矩阵和 SUSFS 版本输出。"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import get_susfs_version
from matrix import (
    generate_build_matrix,
    generate_classified_matrix,
    matrix_summary,
    matrix_summary_markdown,
)


def write_output(name: str, value: str) -> None:
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
        f.write(f"{name}={value}\n")


def main() -> int:
    builds = generate_build_matrix()
    classified = generate_classified_matrix()
    version = get_susfs_version()

    write_output("build_matrix", json.dumps(builds))
    write_output("kernel_version", version)
    write_output("classified_matrix", json.dumps(classified))
    write_output("matrix_summary", json.dumps(matrix_summary(classified)))

    with open("matrix_summary.md", "w", encoding="utf-8") as f:
        f.write(matrix_summary_markdown(classified))

    print(f"Matrix generated: {len(builds)} builds")
    print(f"SUSFS version: {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
