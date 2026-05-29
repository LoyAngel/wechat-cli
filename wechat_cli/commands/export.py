"""export 命令 — 导出聊天记录为 markdown 或 txt，可选导出图片"""

import os
import re
from datetime import datetime

import click

from ..core.contacts import get_contact_names
from ..core.messages import (
    collect_chat_history,
    parse_time_range,
    resolve_chat_context,
    validate_pagination,
)
from ..output.formatter import output


@click.command("export")
@click.argument("chat_name")
@click.option("--format", "fmt", default="markdown", type=click.Choice(["markdown", "txt"]), help="导出格式")
@click.option("--output", "output_path", default=None, help="输出文件路径（默认输出到 stdout）")
@click.option("--start-time", default="", help="起始时间 YYYY-MM-DD [HH:MM[:SS]]")
@click.option("--end-time", default="", help="结束时间 YYYY-MM-DD [HH:MM[:SS]]")
@click.option("--limit", default=500, help="导出消息数量")
@click.option("--images", is_flag=True, help="解密并导出聊天中的图片（需先运行 decode-images --scan-key）")
@click.pass_context
def export(ctx, chat_name, fmt, output_path, start_time, end_time, limit, images):
    """导出聊天记录为 markdown 或纯文本，可选导出图片

    \b
    示例:
      wechat-cli export "张三" --format markdown
      wechat-cli export "张三" --images                  # 导出含图片
      wechat-cli export "AI交流群" --format txt --output chat.txt
      wechat-cli export "张三" --start-time "2026-04-01" --limit 1000
    """
    app = ctx.obj

    try:
        validate_pagination(limit, 0, limit_max=None)
        start_ts, end_ts = parse_time_range(start_time, end_time)
    except ValueError as e:
        click.echo(f"错误: {e}", err=True)
        ctx.exit(2)

    chat_ctx = resolve_chat_context(chat_name, app.msg_db_keys, app.cache, app.decrypted_dir)
    if not chat_ctx:
        click.echo(f"找不到聊天对象: {chat_name}", err=True)
        ctx.exit(1)
    if not chat_ctx['db_path']:
        click.echo(f"找不到 {chat_ctx['display_name']} 的消息记录", err=True)
        ctx.exit(1)

    # 图片解密准备
    image_map = {}  # {dat_path: decoded_path}
    images_dir = None
    if images:
        try:
            aes_key, xor_key = app.image_key_manager.get_key()
        except Exception:
            click.echo("警告: 未找到图片解密密钥，将跳过图片导出。", err=True)
            click.echo("请先运行: wechat-cli decode-images --scan-key", err=True)
            images = False

        if images and output_path:
            base = os.path.splitext(output_path)[0]
            images_dir = base + "_images"
        elif images:
            images_dir = os.path.join(app.decoded_image_dir, "export_images")

    names = get_contact_names(app.cache, app.decrypted_dir)
    lines, failures = collect_chat_history(
        chat_ctx, names, app.display_name_fn,
        start_ts=start_ts, end_ts=end_ts, limit=limit, offset=0,
        resolve_media=images, db_dir=app.db_dir if images else None,
    )

    if not lines:
        click.echo(f"{chat_ctx['display_name']} 无消息记录", err=True)
        ctx.exit(0)

    # 解密图片
    if images and images_dir:
        image_map = _decode_export_images(lines, aes_key, xor_key, images_dir)

    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    chat_type = "群聊" if chat_ctx['is_group'] else "私聊"
    time_range = f"{start_time or '最早'} ~ {end_time or '最新'}"

    if fmt == 'markdown':
        content = _format_markdown(
            chat_ctx['display_name'], chat_type, time_range, now, lines,
            image_map, images_dir,
        )
    else:
        content = _format_txt(
            chat_ctx['display_name'], chat_type, time_range, now, lines,
            image_map, images_dir,
        )

    if output_path:
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(content)
            if not content.endswith('\n'):
                f.write('\n')
        msg = f"已导出到: {output_path}（{len(lines)} 条消息）"
        if image_map:
            msg += f"，{len(image_map)} 张图片 -> {images_dir}"
        click.echo(msg, err=True)
    else:
        output(content, 'text')


def _decode_export_images(lines, aes_key, xor_key, images_dir):
    """从导出行中提取 .dat 路径并解码，返回 {dat_path: rel_path} 映射。"""
    from ..core.image_decrypt import process_file

    os.makedirs(images_dir, exist_ok=True)

    dat_paths = set()
    for line in lines:
        for m in re.finditer(r'\[(图片|文件)\]\s*(\S+\.dat)', line):
            p = m.group(2)
            if os.path.isfile(p):
                dat_paths.add(p)

    if not dat_paths:
        return {}

    image_map = {}
    for dat_path in sorted(dat_paths):
        try:
            data, ext = process_file(dat_path, aes_key, xor_key)
        except Exception:
            continue

        if data is None or ext is None or len(data) <= 100:
            continue

        name = os.path.splitext(os.path.basename(dat_path))[0]
        out_name = name + ext
        out_path = os.path.join(images_dir, out_name)

        # 避免重名
        counter = 1
        while os.path.exists(out_path):
            out_name = f"{name}_{counter}{ext}"
            out_path = os.path.join(images_dir, out_name)
            counter += 1

        with open(out_path, "wb") as f:
            f.write(data)
        image_map[dat_path] = out_name

    return image_map


def _replace_image_ref(line, image_map, images_dir):
    """将行中的 .dat 路径替换为图片引用。"""
    for m in re.finditer(r'\[(图片|文件)\]\s*(\S+\.dat)', line):
        tag = m.group(1)
        dat_path = m.group(2)
        if dat_path in image_map:
            rel = f"images/{image_map[dat_path]}" if images_dir else image_map[dat_path]
            replacement = f"![{tag}]({rel})"
            line = line.replace(m.group(0), replacement)
    return line


def _format_markdown(display_name, chat_type, time_range, export_time, lines, image_map=None, images_dir=None):
    header = (
        f"# 聊天记录: {display_name}\n\n"
        f"**时间范围:** {time_range}\n\n"
        f"**导出时间:** {export_time}\n\n"
        f"**消息数量:** {len(lines)}\n\n"
        f"**类型:** {chat_type}\n\n---\n"
    )
    if image_map:
        body_lines = [_replace_image_ref(line, image_map, images_dir) for line in lines]
    else:
        body_lines = lines
    body = "\n".join(f"- {line}" for line in body_lines)
    return header + body


def _format_txt(display_name, chat_type, time_range, export_time, lines, image_map=None, images_dir=None):
    header = (
        f"聊天记录: {display_name}\n"
        f"类型: {chat_type}\n"
        f"时间范围: {time_range}\n"
        f"导出时间: {export_time}\n"
        f"消息数量: {len(lines)}\n"
        f"{'=' * 60}"
    )
    if image_map:
        body_lines = [_replace_image_ref(line, image_map, images_dir) for line in lines]
    else:
        body_lines = lines
    body = "\n".join(body_lines)
    return header + "\n" + body
