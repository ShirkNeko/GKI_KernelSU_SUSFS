#!/usr/bin/env python3
"""
Telegram 通知脚本
用于发送构建完成/发布通知到 Telegram
"""
import os
import sys
import ssl
import json
import hashlib
import urllib.request
from pathlib import Path


class TelegramNotifier:
    def __init__(self, bot_token: str = None, chat_id: str = None, thread_id: str = None):
        self.bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
        self.thread_id = thread_id or os.environ.get("TELEGRAM_MESSAGE_THREAD_ID", "")

        self.ssl_ctx = ssl.create_default_context()
        self.ssl_ctx.check_hostname = False
        self.ssl_ctx.verify_mode = ssl.CERT_NONE

    def send_message(self, message: str, parse_mode: str = "HTML", disable_web_page_preview: bool = True) -> bool:
        """发送消息到 Telegram"""
        if not self.bot_token or not self.chat_id:
            print("错误: 缺少 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID")
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

        data = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_web_page_preview,
        }

        if self.thread_id:
            data["message_thread_id"] = self.thread_id

        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(data).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, context=self.ssl_ctx) as response:
                result = json.loads(response.read())
                if result.get("ok"):
                    print(f"消息发送成功 (message_id: {result['result']['message_id']})")
                    return True
                else:
                    print(f"发送失败: {result.get('description')}")
                    return False
        except Exception as e:
            print(f"请求失败: {e}")
            return False

    def send_document(self, file_path: str, caption: str = None) -> bool:
        """发送文件到 Telegram"""
        if not self.bot_token or not self.chat_id:
            print("错误: 缺少 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID")
            return False

        if not os.path.exists(file_path):
            print(f"文件不存在: {file_path}")
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendDocument"

        try:
            with open(file_path, "rb") as f:
                import multipart

                form = multipart.MultipartForm()
                form.add_field("chat_id", self.chat_id)
                if self.thread_id:
                    form.add_field("message_thread_id", self.thread_id)
                if caption:
                    form.add_field("caption", caption)
                    form.add_field("parse_mode", "HTML")
                form.add_file("document", os.path.basename(file_path), f)

                req = urllib.request.Request(
                    url,
                    data=form.get_binary(),
                    headers={"Content-Type": form.get_content_type()},
                )
                with urllib.request.urlopen(req, context=self.ssl_ctx) as response:
                    result = json.loads(response.read())
                    if result.get("ok"):
                        print(f"文件发送成功 (message_id: {result['result']['message_id']})")
                        return True
                    else:
                        print(f"文件发送失败: {result.get('description')}")
                        return False
        except ImportError:
            print("需要安装 multipart 库: pip install python-multipart")
            return False
        except Exception as e:
            print(f"文件发送失败: {e}")
            return False


def calculate_file_hashes(file_path: str) -> dict:
    """计算文件的 SHA256 和 MD5 hash"""
    hashes = {"sha256": "", "md5": ""}
    try:
        sha256_hash = hashlib.sha256()
        md5_hash = hashlib.md5()
        with open(file_path, "rb") as f:
            for byte_block in iter(lambda: f.read(4096), b""):
                sha256_hash.update(byte_block)
                md5_hash.update(byte_block)
        hashes["sha256"] = sha256_hash.hexdigest()
        hashes["md5"] = md5_hash.hexdigest()
    except Exception as e:
        print(f"计算 hash 失败: {e}")
    return hashes


def parse_sha256sums(file_path: str) -> list:
    """解析 SHA256SUMS 文件"""
    files = []
    if os.path.exists(file_path):
        with open(file_path, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    parts = line.split(None, 1)
                    if len(parts) == 2:
                        hash_value = parts[0]
                        filename = parts[1]
                        files.append({"hash": hash_value, "filename": filename})
    return files


def build_single_notify_message(
    android_version: str,
    kernel_version: str,
    sub_level: str,
    os_patch_level: str,
    kernelsu_version: str,
    use_zram: bool,
    use_kpm: bool,
    hashes_file: str = None,
) -> str:
    """生成单版本构建完成通知消息"""
    message = f"""✅ <b>内核构建成功</b>

<b>📱 Android:</b> {android_version}
<b>🔧 Kernel:</b> {kernel_version}
<b>📌 Sub Level:</b> {sub_level}
<b>📅 OS Patch:</b> {os_patch_level}

<b>💾 SukiSU 版本:</b> {kernelsu_version}
<b>🔧 ZRAM:</b> {"启用" if use_zram else "禁用"}
<b>📦 KPM:</b> {"启用" if use_kpm else "禁用"}"""

    # 添加文件 hash 信息
    if hashes_file and os.path.exists(hashes_file):
        files = parse_sha256sums(hashes_file)
        if files:
            message += "\n\n<b>📋 文件校验 (SHA256):</b>"
            for file_info in files:
                filename = os.path.basename(file_info["filename"])
                file_hash = file_info["hash"]
                message += f"\n<code>{filename}</code>"
                message += f"\n<code>{file_hash}</code>"

    return message


def build_release_notify_message(
    release_tag: str,
    release_url: str,
    release_notes: str = None,
    hashes_file: str = None,
    status_file: str = None,
    success_count: str = None,
    failed_count: str = None,
) -> str:
    """生成新版本发布通知消息"""
    message = f"""🎉 <b>构建完成!</b>

📦 <b>版本:</b> {release_tag}
🔗 <b>下载:</b> <a href="{release_url}">点击下载</a>"""

    # 添加构建统计
    success = int(success_count) if success_count and success_count.isdigit() else 0
    failed = int(failed_count) if failed_count and failed_count.isdigit() else 0
    total = success + failed

    if total > 0:
        success_emoji = "✅" if success == total else "⚠️"
        message += f"""

{success_emoji} <b>构建统计:</b>
✅ 成功: {success}/{total}
❌ 失败: {failed}/{total}"""

    # 添加完整的发布说明
    if release_notes:
        message += f"""

📝 <b>更新内容:</b>
{release_notes}"""

    # 添加文件 hash 信息
    if hashes_file and os.path.exists(hashes_file):
        files = parse_sha256sums(hashes_file)
        if files:
            message += "\n\n<b>📋 文件校验 (SHA256):</b>"
            for file_info in files:
                filename = os.path.basename(file_info["filename"])
                file_hash = file_info["hash"]
                message += f"\n<code>{filename}</code>"
                message += f"\n<code>{file_hash}</code>"

    return message


def main():
    if len(sys.argv) < 2:
        print("用法:")
        print("  python telegram_notify.py single <android> <kernel> <sub_level> <os_patch> <ksu_version> <zram> <kpm> [hashes_file]")
        print("  python telegram_notify.py release <tag> <url> [notes_file] [hashes_file]")
        sys.exit(1)

    notifier = TelegramNotifier()
    action = sys.argv[1]

    if action == "single":
        if len(sys.argv) < 8:
            print("错误: single 需要 7 个参数")
            sys.exit(1)

        hashes_file = sys.argv[8] if len(sys.argv) > 8 else None
        if hashes_file and not os.path.exists(hashes_file):
            hashes_file = None

        message = build_single_notify_message(
            android_version=sys.argv[2],
            kernel_version=sys.argv[3],
            sub_level=sys.argv[4],
            os_patch_level=sys.argv[5],
            kernelsu_version=sys.argv[6],
            use_zram=sys.argv[7].lower() == "true",
            use_kpm=True,
            hashes_file=hashes_file,
        )
        success = notifier.send_message(message)

        if hashes_file:
            notifier.send_document(hashes_file, "SHA256SUMS")

    elif action == "release":
        if len(sys.argv) < 4:
            print("错误: release 需要 <tag> <url> [notes_file] [hashes_file] [status_file] [success_count] [failed_count]")
            sys.exit(1)

        tag = sys.argv[2]
        url = sys.argv[3]
        notes = ""
        hashes_file = None
        status_file = None
        success_count = None
        failed_count = None

        # 解析可选参数
        for arg in sys.argv[4:]:
            if os.path.exists(arg):
                if arg.endswith(".md"):
                    with open(arg, "r", encoding="utf-8") as f:
                        notes = f.read()
                elif arg.endswith(".txt"):
                    if "status" in arg:
                        status_file = arg
                    else:
                        hashes_file = arg
            elif arg.isdigit():
                if success_count is None:
                    success_count = arg
                else:
                    failed_count = arg

        message = build_release_notify_message(tag, url, notes, hashes_file, status_file, success_count, failed_count)
        success = notifier.send_message(message)

        if hashes_file:
            notifier.send_document(hashes_file, "SHA256SUMS")

    else:
        print(f"未知操作: {action}")
        sys.exit(1)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
