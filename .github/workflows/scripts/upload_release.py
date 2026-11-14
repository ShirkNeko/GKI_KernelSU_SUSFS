#!/usr/bin/env python3
"""并发上传 GitHub Release 资产，避开 secondary rate limit。

一次完整发布会生成 250+ 个文件。softprops/action-gh-release 一次性并发
上传全部文件，几百个“删除+上传”请求瞬间打满 GitHub 的内容创建类限流
（约 80 次/分钟），表现为 “You have exceeded a secondary rate limit”。

这里的做法：

- 并发上传（默认 6 路），每个文件一次 `gh release upload --clobber`
- 已存在且大小和 sha256 都一致的文件直接跳过，重跑时不再重复删除+上传
- 触发 429/403 时所有线程一起冷却（Retry-After 或指数退避），再继续
- 不额外限速：正常网络下 250 个文件约 1–3 分钟

用法::

    python3 upload_release.py --tag v2.4.0 --repo owner/repo \\
        --dir release-files --notes-file release-files/RELEASE_BODY.md

需要环境变量 GH_TOKEN（或 GITHUB_TOKEN）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("upload-release")

_RATE_LIMIT_HINTS = (
    "secondary rate limit",
    "rate limit",
    "429",
    "abuse",
    "exceeded a secondary",
)

_cooldown_lock = threading.Lock()
_cooldown_until = 0.0
_cooldown_step = 0


def _gh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if "GH_TOKEN" not in env and env.get("GITHUB_TOKEN"):
        env["GH_TOKEN"] = env["GITHUB_TOKEN"]
    result = subprocess.run(
        ["gh", *args],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"gh {' '.join(args)} 失败: {(result.stderr or result.stdout).strip()}"
        )
    return result


def _is_rate_limited(message: str) -> bool:
    lowered = message.lower()
    return any(hint in lowered for hint in _RATE_LIMIT_HINTS)


def _retry_after(message: str) -> Optional[int]:
    match = re.search(r"retry[- ]after[:\s]+(\d+)", message, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def _wait_for_cooldown() -> None:
    while True:
        with _cooldown_lock:
            remaining = _cooldown_until - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 1.0))


def _enter_cooldown(seconds: float) -> None:
    """限流时让所有线程一起退避，避免别的线程继续撞墙。"""
    global _cooldown_until, _cooldown_step
    with _cooldown_lock:
        _cooldown_step += 1
        target = time.monotonic() + max(seconds, 15)
        if target > _cooldown_until:
            _cooldown_until = target
    logger.warning("触发 GitHub 限流，全部上传暂停 %.0fs", max(seconds, 15))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_release(repo: str, tag: str, title: str, notes_file: Optional[Path]) -> None:
    existing = None
    for attempt in range(1, 4):
        result = _gh("release", "view", tag, "--repo", repo, "--json", "id", check=False)
        if result.returncode == 0:
            existing = result
            break
        message = f"{result.stderr or ''}\n{result.stdout or ''}".strip()
        if "not found" in message.lower():
            break
        if _is_rate_limited(message) and attempt < 3:
            wait = _retry_after(message) or 60
            logger.warning("查询 Release 触发限流，%ss 后重试", wait)
            time.sleep(wait)
            continue
        raise RuntimeError(f"无法查询 Release {tag}: {message.splitlines()[-1:]}")

    if existing is not None:
        args = ["release", "edit", tag, "--repo", repo]
        if title:
            args += ["--title", title]
        if notes_file:
            args += ["--notes-file", str(notes_file)]
        _gh(*args)
        logger.info("更新已有 Release: %s", tag)
        return

    args = ["release", "create", tag, "--repo", repo]
    if title:
        args += ["--title", title]
    if notes_file:
        args += ["--notes-file", str(notes_file)]
    _gh(*args)
    logger.info("创建 Release: %s", tag)


def list_assets(repo: str, tag: str) -> dict:
    result = _gh(
        "api",
        f"repos/{repo}/releases/tags/{tag}",
        "--jq",
        ".assets | map({name: .name, size: .size, digest: (.digest // \"\")})",
        check=False,
    )
    if result.returncode != 0:
        return {}
    try:
        assets = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return {}
    return {asset["name"]: asset for asset in assets}


def is_unchanged(path: Path, asset: Optional[dict]) -> bool:
    if not asset:
        return False
    if asset.get("size") != path.stat().st_size:
        return False
    digest = (asset.get("digest") or "").strip()
    if not digest.startswith("sha256:"):
        # 没有 digest 时不能确认内容一致，保守重新上传
        return False
    return digest[len("sha256:"):].lower() == _sha256(path)


def upload_one(path: Path, repo: str, tag: str, retries: int) -> bool:
    delay = 30
    for attempt in range(1, retries + 1):
        _wait_for_cooldown()
        result = _gh(
            "release", "upload", tag, str(path), "--repo", repo, "--clobber",
            check=False,
        )
        if result.returncode == 0:
            return True

        message = f"{result.stderr or ''}\n{result.stdout or ''}".strip()
        if _is_rate_limited(message):
            if attempt >= retries:
                break
            wait = _retry_after(message) or delay
            _enter_cooldown(wait)
            delay = min(delay * 2, 180)
            continue
        logger.error("上传失败 %s: %s", path.name, message.splitlines()[-1:])
        return False
    logger.error("上传失败 %s: 重试 %d 次后仍被限流", path.name, retries)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="并发上传 Release 资产")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--repo", required=True, help="owner/repo")
    parser.add_argument("--dir", required=True, help="包含待上传文件的目录")
    parser.add_argument("--title", default=None)
    parser.add_argument("--notes-file", default=None)
    parser.add_argument("--concurrency", type=int, default=6, help="并发上传数")
    parser.add_argument("--retries", type=int, default=6)
    args = parser.parse_args()

    files_dir = Path(args.dir)
    if not files_dir.is_dir():
        logger.error("目录不存在: %s", files_dir)
        return 1

    files = sorted(p for p in files_dir.iterdir() if p.is_file())
    if not files:
        logger.warning("没有需要上传的文件: %s", files_dir)
        return 0

    notes_file = Path(args.notes_file) if args.notes_file else None
    if notes_file and not notes_file.is_file():
        notes_file = None

    started = time.monotonic()
    ensure_release(
        args.repo, args.tag, args.title or f"GKI Kernel {args.tag}", notes_file
    )
    assets = list_assets(args.repo, args.tag)
    logger.info(
        "共 %d 个文件，Release 已有 %d 个资产，并发 %d",
        len(files), len(assets), args.concurrency,
    )

    uploaded = skipped = failed = 0
    counter_lock = threading.Lock()
    done = 0

    def handle(path: Path) -> str:
        nonlocal done
        if is_unchanged(path, assets.get(path.name)):
            outcome = "skip"
        elif upload_one(path, args.repo, args.tag, args.retries):
            outcome = "upload"
        else:
            outcome = "fail"
        with counter_lock:
            done += 1
            logger.info("[%d/%d] %s: %s", done, len(files), path.name,
                        {"skip": "跳过未变化", "upload": "已上传", "fail": "失败"}[outcome])
        return outcome

    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futures = [pool.submit(handle, path) for path in files]
        for future in as_completed(futures):
            outcome = future.result()
            if outcome == "skip":
                skipped += 1
            elif outcome == "upload":
                uploaded += 1
            else:
                failed += 1

    logger.info(
        "上传完成: 成功 %d, 跳过 %d, 失败 %d, 耗时 %.0fs",
        uploaded, skipped, failed, time.monotonic() - started,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
