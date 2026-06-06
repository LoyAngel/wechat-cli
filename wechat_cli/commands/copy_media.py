"""copy-media 命令 — 将聊天中的图片/文件复制到目标目录"""

import hashlib
import os
import shutil

import click

from ..core.contacts import get_contact_names
from ..core.messages import (
    MSG_TYPE_FILTERS,
    collect_chat_history,
    parse_time_range,
    resolve_chat_context,
    validate_pagination,
)
from ..output.formatter import output


def _ensure_image_keys(app):
    """确保有图片解密密钥，返回 (aes_key, xor_key) 或 (None, None)。"""
    try:
        return app.image_key_manager.get_key()
    except Exception:
        return None, None


def _collect_dat_files_from_dir(chat_username, db_dir, start_ts, end_ts, limit=None):
    """直接扫描联系人的 attach 目录，收集所有标准 .dat 图片文件。

    绕过逐条消息匹配的不准确性（XML md5 ≠ .dat 文件名），直接从目录扫描。
    排除缩略图（_t.dat / _t_W.dat）和辅助文件（_h.dat）。

    Returns:
        set of .dat file paths
    """
    wechat_base = os.path.dirname(db_dir)
    attach_dir = os.path.join(wechat_base, "msg", "attach")
    if not os.path.isdir(attach_dir):
        return set()

    h = hashlib.md5(chat_username.encode()).hexdigest()
    hash_dir = os.path.join(attach_dir, h)
    if not os.path.isdir(hash_dir):
        return set()

    from datetime import datetime

    dat_entries = []

    for month_dir in sorted(os.listdir(hash_dir)):
        # 检查月份是否在时间范围内
        if not (len(month_dir) == 7 and month_dir[4] == '-'):
            continue
        try:
            month_start = int(datetime.strptime(month_dir, "%Y-%m").timestamp())
        except ValueError:
            continue
        # 下个月第一天作为该月的结束
        y, m = int(month_dir[:4]), int(month_dir[5:7])
        if m == 12:
            next_month_start = int(datetime(y + 1, 1, 1).timestamp())
        else:
            next_month_start = int(datetime(y, m + 1, 1).timestamp())

        if start_ts and next_month_start <= start_ts:
            continue
        if end_ts and month_start > end_ts:
            continue

        img_dir = os.path.join(hash_dir, month_dir, "Img")
        if not os.path.isdir(img_dir):
            continue

        for fname in os.listdir(img_dir):
            if not fname.lower().endswith(".dat"):
                continue
            # 排除缩略图和辅助文件
            name_part = fname.rsplit(".", 1)[0]
            if fname.endswith("_h.dat"):
                continue
            if name_part.endswith("_t") or "_t_" in name_part:
                continue
            fpath = os.path.join(img_dir, fname)
            if not os.path.isfile(fpath):
                continue
            # 文件时间戳精确过滤（月目录只是粗筛）
            mtime = int(os.path.getmtime(fpath))
            if start_ts and mtime < start_ts:
                continue
            if end_ts and mtime > end_ts:
                continue
            dat_entries.append((mtime, fpath))

    dat_entries.sort(key=lambda x: x[0], reverse=True)
    if limit and limit > 0:
        dat_entries = dat_entries[:limit]

    return [p for _, p in dat_entries]


def _copy_image_files(dat_paths, out_dir, aes_key, xor_key, prefer_hd=False):
    """解码 .dat 图片并写入到 out_dir。

    Returns:
        {"success": int, "failed": int, "hd_count": int,
         "files": [...], "failed_files": [...]}
    """
    from ..core.image_decrypt import process_file

    os.makedirs(out_dir, exist_ok=True)
    success = 0
    failed = 0
    hd_count = 0
    out_files = []
    failed_files = []

    for dat_path in sorted(dat_paths):
        actual_path = dat_path
        if prefer_hd:
            base = os.path.splitext(dat_path)[0]
            if base.endswith("_d"):
                hd_candidate = dat_path
            else:
                hd_candidate = base + "_d.dat"
            if os.path.isfile(hd_candidate):
                actual_path = hd_candidate
                hd_count += 1

        try:
            data, ext = process_file(actual_path, aes_key, xor_key)
        except Exception as e:
            failed += 1
            failed_files.append({
                'source': dat_path,
                'filename': os.path.basename(dat_path),
                'error': 'decrypt_failed',
                'detail': str(e),
            })
            continue

        if data is None or ext is None or len(data) <= 100:
            failed += 1
            failed_files.append({
                'source': dat_path,
                'filename': os.path.basename(dat_path),
                'error': 'decrypt_empty',
                'detail': '解密结果为空或无效 (%d bytes)' % len(data) if data else '解密结果为 None',
            })
            continue

        name = os.path.splitext(os.path.basename(dat_path))[0]
        if actual_path != dat_path:
            name = os.path.splitext(os.path.basename(actual_path))[0]
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
            out_files.append({
                'source': dat_path,
                'actual_source': actual_path if actual_path != dat_path else dat_path,
                'output': out_path,
                'size': len(data),
                'is_hd': actual_path != dat_path,
            })
            success += 1
        except Exception as e:
            failed += 1
            failed_files.append({
                'source': dat_path,
                'filename': os.path.basename(dat_path),
                'error': 'write_failed',
                'detail': str(e),
            })

    return {
        'success': success, 'failed': failed, 'hd_count': hd_count,
        'files': out_files, 'failed_files': failed_files,
    }


def _copy_file_attachments(chat_ctx, names, display_name_fn, db_dir, out_dir, start_ts=None, end_ts=None, limit=200):
    """收集聊天中的文件附件并复制到 out_dir。

    先从消息格式化文本中提取文件路径，若路径不存在则在 msg/file/ 目录下
    跨月份搜索同名文件。

    Returns:
        {"success": int, "failed": int,
         "files": [...], "failed_files": [...]}
    """
    import re

    os.makedirs(out_dir, exist_ok=True)
    success = 0
    failed = 0
    out_files = []
    failed_files = []

    lines, _ = collect_chat_history(
        chat_ctx, names, display_name_fn,
        start_ts=start_ts, end_ts=end_ts, limit=limit, offset=0,
        msg_type_filter=MSG_TYPE_FILTERS['file'],
        resolve_media=True, db_dir=db_dir,
    )

    # 预扫描 msg/file/ 所有月份目录，供回退查找
    wechat_base = os.path.dirname(db_dir)
    file_base = os.path.join(wechat_base, "msg", "file")
    all_file_dirs = []
    if os.path.isdir(file_base):
        all_file_dirs = sorted(
            [os.path.join(file_base, d) for d in os.listdir(file_base)
             if os.path.isdir(os.path.join(file_base, d))],
            reverse=True,
        )

    for line in lines:
        m = re.search(r'\[文件\]\s*(.+?)\n\s+(.+)', line)
        if m:
            filename = m.group(1)
            source_path = m.group(2)
        else:
            m = re.search(r'\[文件\]\s*(.+)', line)
            filename = m.group(1) if m else None
            source_path = None

        if not filename:
            continue

        # 若路径不存在，跨月份目录搜索
        searched_dirs = []
        if not source_path or not os.path.isfile(source_path):
            source_path = None
            for d in all_file_dirs:
                candidate = os.path.join(d, filename)
                if os.path.isfile(candidate):
                    source_path = candidate
                    break
                searched_dirs.append(d)
                # 模糊匹配
                for f in os.listdir(d):
                    if filename in f or f in filename:
                        source_path = os.path.join(d, f)
                        break
                if source_path:
                    break

        if not source_path or not os.path.isfile(source_path):
            failed += 1
            hint = (
                "文件未下载到本地。微信不会自动下载所有文件附件，"
                "请在微信聊天中手动点击该文件下载后再试。"
            )
            failed_files.append({
                'filename': filename,
                'error': 'file_not_downloaded',
                'detail': hint,
                'searched_dirs': searched_dirs or [os.path.join(file_base, '*')],
            })
            continue

        out_name = filename
        out_path = os.path.join(out_dir, out_name)
        counter = 1
        while os.path.exists(out_path):
            base, ext = os.path.splitext(filename)
            out_name = f"{base}_{counter}{ext}"
            out_path = os.path.join(out_dir, out_name)
            counter += 1

        try:
            shutil.copy2(source_path, out_path)
            out_files.append({
                'source': source_path,
                'filename': filename,
                'output': out_path,
                'size': os.path.getsize(out_path),
            })
            success += 1
        except Exception as e:
            failed += 1
            failed_files.append({
                'filename': filename,
                'source_path': source_path,
                'error': 'copy_failed',
                'detail': str(e),
            })

    return {
        'success': success, 'failed': failed,
        'files': out_files, 'failed_files': failed_files,
    }


@click.command("copy-media")
@click.option("--chat", required=True, help="联系人名称或备注")
@click.option("--out-dir", required=True, help="输出目录")
@click.option("--type", "media_types", default="image,file",
              help="媒体类型: image, file, video, voice (逗号分隔，默认 image,file)")
@click.option("--since", default="", help="起始时间 YYYY-MM-DD [HH:MM[:SS]]（默认今天 00:00）")
@click.option("--until", default="", help="结束时间 YYYY-MM-DD [HH:MM[:SS]]")
@click.option("--prefer-hd", is_flag=True, help="优先使用 _d.dat 高清图片")
@click.option("--limit", default=200, type=int, help="最多处理的消息/图片数 (默认 200)")
@click.option("--format", "fmt", default="json", type=click.Choice(["json", "text"]), help="输出格式")
@click.pass_context
def copy_media(ctx, chat, out_dir, media_types, since, until, prefer_hd, limit, fmt):
    """复制聊天中的媒体文件到指定目录

    \b
    示例:
      wechat-cli copy-media --chat "张三" --out-dir "D:/打印/张三/"
      wechat-cli copy-media --chat "张三" --out-dir "D:/打印/张三/" --type image --prefer-hd
      wechat-cli copy-media --chat "张三" --out-dir "D:/打印/" --since "2026-05-30"

    \b
    图片解密密钥缺失？
      请先在微信中打开任意聊天大图，然后运行:
        wechat-cli decode-images --scan-key
    """
    app = ctx.obj

    from datetime import datetime as _dt

    try:
        validate_pagination(limit, 0, limit_max=None)
        start_ts, end_ts = parse_time_range(since, until)
    except ValueError as e:
        click.echo(f"错误: {e}", err=True)
        ctx.exit(2)

    # 未指定 --since 时默认今天 00:00，避免复制全部历史图片
    if start_ts is None:
        start_ts = int(_dt.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())

    # 解析媒体类型
    types_wanted = set(t.strip().lower() for t in media_types.split(",") if t.strip())
    valid_types = {'image', 'file', 'video', 'voice'}
    invalid = types_wanted - valid_types
    if invalid:
        click.echo(f"错误: 不支持的媒体类型 {invalid}，支持: {', '.join(sorted(valid_types))}", err=True)
        ctx.exit(2)

    # 解析联系人
    chat_ctx = resolve_chat_context(chat, app.msg_db_keys, app.cache, app.decrypted_dir)
    if not chat_ctx:
        click.echo(f"找不到聊天对象: {chat}", err=True)
        ctx.exit(1)
    if not chat_ctx.get('db_path'):
        click.echo(f"找不到 {chat_ctx['display_name']} 的消息记录", err=True)
        ctx.exit(1)

    out_dir = os.path.abspath(out_dir)
    results = {
        'chat': chat_ctx['display_name'],
        'username': chat_ctx['username'],
        'is_group': chat_ctx['is_group'],
    }

    image_key = None
    xor_key = None

    if 'image' in types_wanted:
        image_key, xor_key = _ensure_image_keys(app)
        if not image_key:
            click.echo(
                "\n错误: 未找到图片解密密钥。\n\n"
                "请按以下步骤操作:\n"
                "  1. 确认微信已登录并在运行\n"
                "  2. 在微信中打开任意聊天窗口的大图（点开图片查看原图）\n"
                "  3. 运行: wechat-cli decode-images --scan-key\n"
                "  4. 重新运行本命令\n",
                err=True,
            )
            ctx.exit(3)

    names = get_contact_names(app.cache, app.decrypted_dir)

    # 收集图片路径
    if 'image' in types_wanted:
        dat_paths = _collect_dat_files_from_dir(
            chat_ctx['username'], app.db_dir, start_ts, end_ts, limit=limit,
        )

        # 回退：私聊 hash 目录不存在时（如群聊），走逐条消息解析
        if not dat_paths:
            import re

            lines, _ = collect_chat_history(
                chat_ctx, names, app.display_name_fn,
                start_ts=start_ts, end_ts=end_ts, limit=limit, offset=0,
                msg_type_filter=MSG_TYPE_FILTERS['image'],
                resolve_media=True, db_dir=app.db_dir,
            )

            seen = set()
            for line in lines:
                for m in re.finditer(r'\[(图片)\]\s*(\S+\.dat)', line):
                    p = m.group(2)
                    if os.path.isfile(p) and p not in seen:
                        dat_paths.append(p)
                        seen.add(p)
                        if limit and len(dat_paths) >= limit:
                            break
                if limit and len(dat_paths) >= limit:
                    break

        if dat_paths:
            img_result = _copy_image_files(
                dat_paths, out_dir, image_key, xor_key, prefer_hd=prefer_hd
            )
            results['images'] = img_result
        else:
            results['images'] = {'success': 0, 'failed': 0, 'hd_count': 0, 'files': []}

    # 收集文件附件
    if 'file' in types_wanted:
        file_result = _copy_file_attachments(
            chat_ctx, names, app.display_name_fn, app.db_dir, out_dir,
            start_ts=start_ts, end_ts=end_ts, limit=limit,
        )
        results['files'] = file_result

    if fmt == 'json':
        output(results, 'json')
    else:
        lines = [f"聊天: {chat_ctx['display_name']}", f"输出目录: {out_dir}"]
        if 'images' in results:
            img = results['images']
            hd_note = f" (含 {img.get('hd_count', 0)} 张高清)" if img.get('hd_count') else ""
            lines.append(f"图片: 成功 {img['success']}, 失败 {img['failed']}{hd_note}")
        if 'files' in results:
            f = results['files']
            lines.append(f"文件: 成功 {f['success']}, 失败 {f['failed']}")
        output("\n".join(lines), 'text')
