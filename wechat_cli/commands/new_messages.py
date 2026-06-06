"""get-new-messages 命令 — 增量消息查询，状态持久化到磁盘"""

import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime

import click

from ..core.config import STATE_DIR
from ..core.contacts import get_contact_names
from ..core.messages import (
    decompress_content,
    format_msg_type,
    collect_chat_history,
    resolve_chat_context,
)
from ..output.formatter import output

STATE_FILE = os.path.join(STATE_DIR, "last_check.json")


def _load_last_state(state_path):
    if not os.path.exists(state_path):
        return {}
    try:
        with open(state_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_last_state(state, state_path):
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    with open(state_path, 'w', encoding="utf-8") as f:
        json.dump(state, f)


def _format_summary(summary, is_group=False):
    """格式化消息摘要：解压 + 去除群聊发送者前缀"""
    if isinstance(summary, bytes):
        summary = decompress_content(summary, 4) or '(压缩内容)'
    if isinstance(summary, str) and ':\n' in summary:
        summary = summary.split(':\n', 1)[1]
    return str(summary or '')


def _fetch_detail_messages(username, is_group, app, detail_count):
    """为单个聊天获取最近 N 条消息详情（含媒体路径）。"""
    chat_ctx = resolve_chat_context(username, app.msg_db_keys, app.cache, app.decrypted_dir)
    if not chat_ctx or not chat_ctx.get('db_path'):
        return []

    names = get_contact_names(app.cache, app.decrypted_dir)
    lines, _ = collect_chat_history(
        chat_ctx, names, app.display_name_fn,
        start_ts=None, end_ts=None, limit=detail_count, offset=0,
        msg_type_filter=None, resolve_media=True, db_dir=app.db_dir,
    )

    details = []
    for line in lines:
        detail = {'text': line}
        m = re.search(r'\[(图片)\]\s*(\S+\.dat)', line)
        if m:
            dat_path = m.group(2)
            if os.path.isfile(dat_path):
                detail['dat_path'] = dat_path
                # 检查 _d.dat 高清版本
                base = os.path.splitext(dat_path)[0]
                hd_candidate = base + "_d.dat" if not base.endswith("_d") else dat_path
                detail['has_hd'] = os.path.isfile(hd_candidate)
        details.append(detail)

    return details


@click.command("new-messages")
@click.option("--format", "fmt", default="json", type=click.Choice(["json", "text"]), help="输出格式")
@click.option("--state-file", default=None, help="状态文件路径（用于多账号隔离）")
@click.option("--detail", "detail_count", default=0, type=int, help="获取每个新消息会话的最近 N 条消息详情")
@click.option("--customers-only", is_flag=True, help="仅返回 username 以 wxid_ 开头的个人会话（过滤群聊/公众号等）")
@click.option("--dry-run", is_flag=True, help="预览新消息但不更新状态文件（可重复执行）")
@click.pass_context
def new_messages(ctx, fmt, state_file, detail_count, customers_only, dry_run):
    """获取自上次调用以来的新消息

    \b
    示例:
      wechat-cli new-messages                        # 首次: 返回未读消息并记录状态
      wechat-cli new-messages                        # 再次: 仅返回新增消息
      wechat-cli new-messages --format text           # 纯文本输出
      wechat-cli new-messages --detail 5              # 含最近 5 条消息详情
      wechat-cli new-messages --state-file acc1.json  # 多账号隔离
      wechat-cli new-messages --customers-only        # 仅返回个人客户（过滤群聊/公众号）
      wechat-cli new-messages --dry-run               # 预览模式，不更新状态文件

    \b
    状态文件: ~/.wechat-cli/last_check.json (删除此文件可重置)
    多账号时通过 --state-file 指定不同文件实现状态隔离
    --customers-only: 仅保留 username 以 wxid_ 开头的会话，过滤群聊、公众号等非客户消息
    --dry-run: 预览新消息但不更新状态文件，可反复执行得到相同结果
    """
    app = ctx.obj

    state_path = state_file or STATE_FILE

    path = app.cache.get(os.path.join("session", "session.db"))
    if not path:
        click.echo("错误: 无法解密 session.db", err=True)
        ctx.exit(3)

    names = get_contact_names(app.cache, app.decrypted_dir)
    with closing(sqlite3.connect(path)) as conn:
        rows = conn.execute("""
            SELECT username, unread_count, summary, last_timestamp,
                   last_msg_type, last_msg_sender, last_sender_display_name
            FROM SessionTable
            WHERE last_timestamp > 0
            ORDER BY last_timestamp DESC
        """).fetchall()

    curr_state = {}
    for r in rows:
        username, unread, summary, ts, msg_type, sender, sender_name = r
        curr_state[username] = {
            'unread': unread, 'summary': summary, 'timestamp': ts,
            'msg_type': msg_type, 'sender': sender or '', 'sender_name': sender_name or '',
        }

    last_state = _load_last_state(state_path)

    if not last_state:
        # 首次调用：保存状态，返回未读
        if not dry_run:
            _save_last_state({u: s['timestamp'] for u, s in curr_state.items()}, state_path)

        unread_msgs = []
        for username, s in curr_state.items():
            if customers_only and not username.startswith('wxid_'):
                continue
            if s['unread'] and s['unread'] > 0:
                display = names.get(username, username)
                is_group = '@chatroom' in username
                summary = _format_summary(s['summary'], is_group)
                time_str = datetime.fromtimestamp(s['timestamp']).strftime('%H:%M')
                msg = {
                    'chat': display,
                    'username': username,
                    'is_group': is_group,
                    'unread': s['unread'],
                    'last_message': summary,
                    'msg_type': format_msg_type(s['msg_type']),
                    'time': time_str,
                    'timestamp': s['timestamp'],
                }
                if detail_count > 0:
                    msg['detail'] = _fetch_detail_messages(username, is_group, app, detail_count)
                unread_msgs.append(msg)

        if fmt == 'json':
            output({'first_call': True, 'unread_count': len(unread_msgs), 'messages': unread_msgs}, 'json')
        else:
            if unread_msgs:
                lines = []
                for m in unread_msgs:
                    tag = " [群]" if m['is_group'] else ""
                    lines.append(f"[{m['time']}] {m['chat']}{tag} ({m['unread']}条未读): {m['last_message']}")
                output(f"当前 {len(unread_msgs)} 个未读会话:\n\n" + "\n".join(lines), 'text')
            else:
                output("当前无未读消息（已记录状态，下次调用将返回新消息）", 'text')
        return

    # 后续调用：对比差异
    new_msgs = []
    for username, s in curr_state.items():
        if customers_only and not username.startswith('wxid_'):
            continue
        prev_ts = last_state.get(username, 0)
        if s['timestamp'] > prev_ts:
            display = names.get(username, username)
            is_group = '@chatroom' in username
            summary = _format_summary(s['summary'], is_group)

            sender_display = ''
            if is_group and s['sender']:
                sender_display = names.get(s['sender'], s['sender_name'] or s['sender'])

            msg = {
                'chat': display,
                'username': username,
                'is_group': is_group,
                'last_message': summary,
                'msg_type': format_msg_type(s['msg_type']),
                'sender': sender_display,
                'time': datetime.fromtimestamp(s['timestamp']).strftime('%H:%M:%S'),
                'timestamp': s['timestamp'],
            }
            if detail_count > 0:
                msg['detail'] = _fetch_detail_messages(username, is_group, app, detail_count)
            new_msgs.append(msg)

    if not dry_run:
        _save_last_state({u: s['timestamp'] for u, s in curr_state.items()}, state_path)

    new_msgs.sort(key=lambda m: m['timestamp'])

    if fmt == 'json':
        output({'first_call': False, 'new_count': len(new_msgs), 'messages': new_msgs}, 'json')
    else:
        if not new_msgs:
            output("无新消息", 'text')
        else:
            lines = []
            for m in new_msgs:
                entry = f"[{m['time']}] {m['chat']}"
                if m['is_group']:
                    entry += " [群]"
                entry += f": {m['msg_type']}"
                if m['sender']:
                    entry += f" ({m['sender']})"
                entry += f" - {m['last_message']}"
                lines.append(entry)
            output(f"{len(new_msgs)} 条新消息:\n\n" + "\n".join(lines), 'text')
