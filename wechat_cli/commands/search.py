"""search-messages 命令"""

import os
import re

import click

from ..core.contacts import get_contact_names
from ..core.messages import (
    MSG_TYPE_FILTERS,
    MSG_TYPE_NAMES,
    collect_chat_search,
    parse_time_range,
    resolve_chat_context,
    resolve_chat_contexts,
    search_all_messages,
    validate_pagination,
    _candidate_page_size,
    _page_ranked_entries,
)
from ..output.formatter import output


@click.command("search")
@click.argument("keyword")
@click.option("--chat", multiple=True, help="限定聊天对象（可多次指定）")
@click.option("--start-time", default="", help="起始时间")
@click.option("--end-time", default="", help="结束时间")
@click.option("--limit", default=20, help="返回数量（最大500）")
@click.option("--offset", default=0, help="分页偏移量")
@click.option("--format", "fmt", default="json", type=click.Choice(["json", "text"]), help="输出格式")
@click.option("--type", "msg_type", default=None, type=click.Choice(MSG_TYPE_NAMES), help="消息类型过滤")
@click.option("--media", is_flag=True, help="解析并解码媒体文件（图片/文件/视频/语音）")
@click.pass_context
def search(ctx, keyword, chat, start_time, end_time, limit, offset, fmt, msg_type, media):
    """搜索消息内容

    \b
    示例:
      wechat-cli search "Claude"                         # 全局搜索
      wechat-cli search "Claude" --chat "AI交流群"        # 在指定群搜索
      wechat-cli search "开会" --chat "群A" --chat "群B"  # 同时搜多个群
      wechat-cli search "你好" --start-time "2026-04-01" --limit 50
      wechat-cli search "照片" --media                    # 搜索并解析媒体文件
    """
    app = ctx.obj

    try:
        validate_pagination(limit, offset)
        start_ts, end_ts = parse_time_range(start_time, end_time)
    except ValueError as e:
        click.echo(f"错误: {e}", err=True)
        ctx.exit(2)

    # 图片密钥
    image_key = None
    xor_key = None
    if media:
        try:
            image_key, xor_key = app.image_key_manager.get_key()
        except Exception:
            pass

    names = get_contact_names(app.cache, app.decrypted_dir)
    candidate_limit = _candidate_page_size(limit, offset)
    chat_names = list(chat)
    type_filter = MSG_TYPE_FILTERS[msg_type] if msg_type else None

    if len(chat_names) == 1:
        # 单聊搜索
        chat_ctx = resolve_chat_context(chat_names[0], app.msg_db_keys, app.cache, app.decrypted_dir)
        if not chat_ctx:
            click.echo(f"找不到聊天对象: {chat_names[0]}", err=True)
            ctx.exit(1)
        if not chat_ctx['db_path']:
            click.echo(f"找不到 {chat_ctx['display_name']} 的消息记录", err=True)
            ctx.exit(1)
        entries, failures = collect_chat_search(
            chat_ctx, names, keyword, app.display_name_fn,
            start_ts=start_ts, end_ts=end_ts, candidate_limit=candidate_limit,
            msg_type_filter=type_filter,
            resolve_media=media, db_dir=app.db_dir if media else None,
        )
        scope = chat_ctx['display_name']

    elif len(chat_names) > 1:
        # 多聊搜索
        resolved, unresolved, missing = resolve_chat_contexts(chat_names, app.msg_db_keys, app.cache, app.decrypted_dir)
        if not resolved:
            click.echo("错误: 没有可查询的聊天对象", err=True)
            ctx.exit(1)
        entries = []
        failures = []
        for rc in resolved:
            e, f = collect_chat_search(
                rc, names, keyword, app.display_name_fn,
                start_ts=start_ts, end_ts=end_ts, candidate_limit=candidate_limit,
                msg_type_filter=type_filter,
                resolve_media=media, db_dir=app.db_dir if media else None,
            )
            entries.extend(e)
            failures.extend(f)
        if unresolved:
            failures.append("未找到: " + "、".join(unresolved))
        scope = f"{len(resolved)} 个聊天对象"

    else:
        # 全局搜索
        entries, failures = search_all_messages(
            app.msg_db_keys, app.cache, names, keyword, app.display_name_fn,
            start_ts=start_ts, end_ts=end_ts, candidate_limit=candidate_limit,
            msg_type_filter=type_filter,
            resolve_media=media, db_dir=app.db_dir if media else None,
        )
        scope = "全部消息"

    paged = _page_ranked_entries(entries, limit, offset)
    lines = [item[1] for item in paged]

    # 解码图片路径
    media_files = {}
    if media and image_key and lines:
        media_files = _decode_search_media(
            lines, image_key, xor_key, app.decoded_image_dir
        )
        if media_files:
            lines = _replace_media_paths(lines, media_files)

    if fmt == 'json':
        result = {
            'scope': scope,
            'keyword': keyword,
            'count': len(paged),
            'offset': offset,
            'limit': limit,
            'start_time': start_time or None,
            'end_time': end_time or None,
            'type': msg_type or None,
            'results': lines,
            'failures': failures if failures else None,
        }
        if media_files:
            result['media'] = media_files
        output(result, 'json')
    else:
        if not paged:
            output(f"在 {scope} 中未找到包含 \"{keyword}\" 的消息", 'text')
            return
        header = f"在 {scope} 中搜索 \"{keyword}\" 找到 {len(paged)} 条结果（offset={offset}, limit={limit}）"
        if start_time or end_time:
            header += f"\n时间范围: {start_time or '最早'} ~ {end_time or '最新'}"
        if failures:
            header += "\n查询失败: " + "；".join(failures)
        output(header + ":\n\n" + "\n\n".join(lines), 'text')


def _decode_search_media(lines, aes_key, xor_key, out_dir):
    """从搜索结果行中提取 .dat 路径并解码。"""
    from ..core.image_decrypt import process_file

    dat_paths = set()
    for line in lines:
        for m in re.finditer(r'\[(图片)\]\s*(\S+\.dat)', line):
            p = m.group(2)
            if os.path.isfile(p):
                dat_paths.add(p)

    if not dat_paths:
        return {}

    os.makedirs(out_dir, exist_ok=True)

    media_map = {}
    for dat_path in sorted(dat_paths):
        try:
            data, ext = process_file(dat_path, aes_key, xor_key)
        except Exception:
            continue

        if data is None or ext is None or len(data) <= 100:
            continue

        name = os.path.splitext(os.path.basename(dat_path))[0]
        out_name = name + ext
        out_path = os.path.join(out_dir, out_name)
        counter = 1
        while os.path.exists(out_path):
            out_name = f"{name}_{counter}{ext}"
            out_path = os.path.join(out_dir, out_name)
            counter += 1

        try:
            with open(out_path, "wb") as f:
                f.write(data)
            media_map[dat_path] = out_path
        except Exception:
            continue

    return media_map


def _replace_media_paths(lines, media_map):
    """将行中的 .dat 路径替换为解码后的路径。"""
    result = []
    for line in lines:
        for m in re.finditer(r'(\[图片\])\s*(\S+\.dat)', line):
            dat_path = m.group(2)
            if dat_path in media_map:
                decoded = media_map[dat_path]
                line = line.replace(m.group(0), f"{m.group(1)} {decoded}")
        result.append(line)
    return result
