"""decode-images 命令 — 解密微信 .dat 加密图片"""

import os
import sys

import click

from ..core.image_decrypt import (
    _DEFAULT_XOR_KEY,
    decode_batch,
    find_dat_files,
)
from ..core.image_key_manager import MissingKeyError
from ..core.messages import resolve_chat_context
from ..output.formatter import output


@click.command("decode-images")
@click.option("--chat", default=None,
              help="解密特定聊天的图片（推荐，用户名或备注名）")
@click.option("--key", default=None,
              help="AES 密钥（16 位 ASCII，如 9f211f90c4d22ab4）")
@click.option("--scan-key", is_flag=True,
              help="从微信进程内存扫描密钥并保存（仅 Windows，需先打开聊天大图）")
@click.option("--out-dir", default=None,
              help="输出目录（默认使用配置中的 decoded_image_dir）")
@click.option("--limit", type=int, default=50,
              help="最多处理文件数（默认 50，最大 10000）")
@click.option("--all", "all_files", is_flag=True,
              help="处理全部文件")
@click.option("--format", "fmt", default="json",
              type=click.Choice(["json", "text"]),
              help="输出格式")
@click.pass_context
def decode_images(ctx, chat, key, scan_key, out_dir, limit, all_files, fmt):
    """解密微信 .dat 加密图片为标准格式

    \b
    推荐用法（需先设置密钥）:
      wechat-cli decode-images --chat "张三"              # 解密张三的图片
      wechat-cli decode-images --scan-key                  # 扫描并保存密钥

    \b
    首次使用:
      wechat-cli decode-images --scan-key                  # 打开大图后扫描密钥（自动保存）
      wechat-cli decode-images --chat "张三"               # 之后直接使用

    \b
    其他:
      wechat-cli decode-images --key 9f211f90c4d22ab4    # 手动指定密钥
      wechat-cli decode-images --limit 100                # 最多100个

    \b
    注意：使用 --scan-key 前，必须先在微信中打开聊天窗口的大图（点开图片查看原图），
    否则密钥不会加载到内存中，无法扫描到。
    """
    app = ctx.obj
    ikm = app.image_key_manager

    current_wxid = os.path.basename(os.path.dirname(app.db_dir))
    if not current_wxid.startswith("wxid_"):
        click.echo(f"警告: 当前数据目录对应的账号 '{current_wxid}' 不像有效的 wxid，图片解密可能失败", err=True)

    if limit is not None and limit < 1:
        click.echo("错误: --limit 必须 >= 1", err=True)
        sys.exit(1)
    if limit is not None and limit > 10000:
        click.echo("错误: --limit 最大为 10000，如需处理全部文件请使用 --all", err=True)
        sys.exit(1)

    wechat_base = app.cfg.get("wechat_base_dir", "")
    if not wechat_base or not os.path.isdir(wechat_base):
        click.echo("错误: 未找到微信数据目录", err=True)
        sys.exit(1)

    # 1. 确定 AES 密钥和 XOR 密钥
    aes_key = None
    xor_key = None

    if scan_key:
        # --- 从内存扫描 ---
        if sys.platform != "win32":
            click.echo("错误: --scan-key 仅支持 Windows", err=True)
            sys.exit(1)

        def _status(msg):
            click.echo(f"  {msg}")

        click.echo("从微信进程内存扫描密钥...")
        click.echo("提示: 请确认已在微信中打开过聊天大图，否则扫描会失败。\n")

        try:
            count = ikm.scan_and_save(account_filter=current_wxid, on_status=_status)
        except RuntimeError as e:
            click.echo(f"\n错误: {e}", err=True)
            sys.exit(1)

        if count == 0:
            click.echo("\n错误: 未找到 DAT 密钥。", err=True)
            click.echo("请确保:\n"
                       "  1. 微信正在运行\n"
                       "  2. 已在微信中打开聊天窗口的大图（点开图片查看原图）\n"
                       "  3. 重新运行: wechat-cli decode-images --scan-key", err=True)
            sys.exit(1)

        # 扫描成功，列出结果
        click.echo(f"\n已保存 {count} 个账号的密钥到 image_keys.json\n")
        for acct in ikm.accounts:
            info = ikm._keys[acct]
            x_str = f"0x{info['xor_key']:02X}" if info.get("xor_key") is not None else "自动检测"
            click.echo(f"  账号: {acct}")
            click.echo(f"  AES:  {info['aes_key']}")
            click.echo(f"  XOR:  {x_str}")
            click.echo()

        # scan-key 只保存密钥，不继续解密。解密请单独运行 decode-images
        click.echo("密钥已保存。解密图片请运行:")
        click.echo("  wechat-cli decode-images --chat \"联系人名称\"")
        sys.exit(0)

    elif key:
        # --- 用户指定密钥 ---
        try:
            aes_key = key.encode("ascii")
        except UnicodeEncodeError:
            click.echo("错误: --key 必须是 ASCII 字母数字字符串", err=True)
            sys.exit(1)
        if len(aes_key) != 16:
            click.echo(f"错误: --key 必须是 16 字节（当前 {len(aes_key)} 字节）", err=True)
            sys.exit(1)

        # 自动检测 XOR key
        xor_key = _detect_xor_key(ikm._wechat_files_root, current_wxid)
        if xor_key is not None:
            click.echo(f"自动检测 XOR key: 0x{xor_key:02X}")
        else:
            xor_key = _DEFAULT_XOR_KEY
            click.echo(f"使用默认 XOR key: 0x{xor_key:02X}")

    else:
        # --- 尝试读取持久化密钥 ---
        try:
            aes_key, xor_key = ikm.get_key(current_wxid)
            click.echo("使用已保存的图片密钥")
        except MissingKeyError:
            click.echo("错误: 未找到图片解密密钥。\n", err=True)
            click.echo("首次使用请先扫描密钥（需先在微信中打开聊天大图）:", err=True)
            click.echo("  wechat-cli decode-images --scan-key", err=True)
            click.echo("\n或手动指定密钥:", err=True)
            click.echo("  wechat-cli decode-images --key <16位密钥>", err=True)
            sys.exit(1)

    # 2. 确定输出目录
    if out_dir:
        output_dir = os.path.abspath(out_dir)
    else:
        output_dir = app.decoded_image_dir

    # 3. 解析联系人
    chat_username = None
    if chat:
        resolved = resolve_chat_context(
            chat, app.msg_db_keys, app.cache, app.decrypted_dir
        )
        if not resolved:
            click.echo(f"错误: 未找到联系人 '{chat}'", err=True)
            sys.exit(1)
        chat_username = resolved["username"]
        click.echo(f"联系人: {resolved['display_name']} ({chat_username})")

    # 4. 枚举文件
    file_entries = list(find_dat_files(wechat_base, chat_username=chat_username))
    if not file_entries:
        if chat:
            click.echo(f"未找到 {chat} 的 .dat 图片文件")
        else:
            click.echo("未找到 .dat 图片文件")
        if fmt == "json":
            output({"total": 0, "success": 0, "failed": 0, "files": []}, fmt)
        return

    if all_files:
        limit = 0
    if limit and limit > 0:
        file_entries = file_entries[:limit]

    file_paths = [p for p, _, _ in file_entries]
    total_size = sum(s for _, s, _ in file_entries)
    click.echo(f"找到 {len(file_paths)} 个文件 (共 {total_size:,} 字节)")

    # 5. 批量解密
    click.echo(f"输出目录: {output_dir}")
    click.echo()

    def _progress(idx, total, path, status, ext, size):
        name = os.path.basename(path)
        if status == "ok":
            click.echo(f"  [{idx + 1}/{total}] OK  {name} -> {ext} ({size:,} bytes)")
        elif status == "fail":
            click.echo(f"  [{idx + 1}/{total}] FAIL {name}")
        elif status == "skip":
            click.echo(f"  [{idx + 1}/{total}] SKIP {name} (wxgf 需要 ffmpeg)")

    result = decode_batch(file_paths, aes_key, xor_key, output_dir, on_progress=_progress)

    # 6. 输出结果
    click.echo()
    click.echo(f"完成: 成功 {result['success']}, 失败 {result['failed']}, "
               f"跳过 {result['skipped']}, 共 {len(file_paths)} 个文件")

    if result["ffmpeg_missing"]:
        click.echo("提示: wxgf 格式需要 ffmpeg，请安装: winget install ffmpeg")

    if result["failed"] > 0:
        click.echo("提示: 如果密钥不正确，请运行 wechat-cli decode-images --scan-key 扫描新密钥")
        click.echo("      注意：扫描前需在微信中打开聊天大图！")

    if fmt == "json":
        output({
            "total": len(file_paths),
            "success": result["success"],
            "failed": result["failed"],
            "skipped": result["skipped"],
            "output_dir": output_dir,
            "files": result["files"],
        }, fmt)


def _detect_xor_key(wechat_files_root, account=None):
    """自动检测 XOR 密钥。"""
    from ..core.image_decrypt import collect_account_ciphertexts

    accounts = collect_account_ciphertexts(wechat_files_root, account_filter=account)
    if not accounts:
        return None

    if account and account in accounts:
        return accounts[account].get("xorkey")

    for info in accounts.values():
        if info.get("xorkey") is not None:
            return info["xorkey"]
    return None
