"""change-account 命令 — 切换微信账号"""

import json
import os
import shutil
import sys
import tempfile

import click

from ..core.config import (
    account_config_path,
    auto_detect_db_dir,
    resolve_state_paths,
    save_current_config_path,
)


@click.command("change-account")
@click.option("--db-dir", default=None, help="直接指定微信数据目录路径（应为 db_storage 目录）")
@click.pass_context
def change_account(ctx, db_dir):
    """切换微信账号，更新配置并重新提取密钥"""
    config_path = None
    if ctx is not None:
        root_ctx = ctx.find_root()
        config_path = root_ctx.params.get("config_path")

    # 1. 确定目标 db_dir
    if db_dir is None:
        db_dir = auto_detect_db_dir()
        if db_dir is None:
            click.echo("[!] 未能自动检测到微信账号", err=True)
            click.echo("请通过 --db-dir 参数指定，例如:", err=True)
            click.echo("  wechat-cli change-account --db-dir ~/path/to/db_storage", err=True)
            sys.exit(1)
        wxid = os.path.basename(os.path.dirname(db_dir))
        click.echo(f"[+] 已选择账号: {wxid}")
    else:
        db_dir = os.path.abspath(db_dir)
        if not os.path.isdir(db_dir):
            click.echo(f"[!] 目录不存在: {db_dir}", err=True)
            sys.exit(1)
        if os.path.basename(db_dir) != "db_storage":
            click.echo(f"警告: 目录名不是 'db_storage'，请确认路径是否正确: {db_dir}", err=True)
        wxid = os.path.basename(os.path.dirname(db_dir))
        if not wxid.startswith("wxid_"):
            click.echo(f"警告: 账号标识 '{wxid}' 不像有效的微信 wxid，可能无法正常工作", err=True)
        click.echo(f"[+] 使用指定数据目录: {db_dir}")

    if not config_path:
        config_path = account_config_path(wxid)
    config_path, state_dir, keys_file = resolve_state_paths(config_path)

    # 2. 加载/创建配置
    cfg = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, encoding="utf-8") as f:
                cfg = json.load(f)
        except json.JSONDecodeError:
            pass

    old_db_dir = cfg.get("db_dir", "")
    if old_db_dir == db_dir:
        save_current_config_path(config_path)
        click.echo("[+] 已是当前账号，已更新当前配置指向")
        return

    # 3. 更新 db_dir 并写入配置
    cfg["db_dir"] = db_dir
    os.makedirs(state_dir, exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

    # 4. 重新提取密钥
    click.echo("\n开始提取密钥...")
    try:
        from ..keys import extract_keys
        key_map = extract_keys(db_dir, keys_file)
    except RuntimeError as e:
        click.echo(f"\n[!] 密钥提取失败: {e}", err=True)
        if "sudo" not in str(e).lower():
            click.echo("提示: macOS/Linux 可能需要 sudo 权限", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"\n[!] 密钥提取出错: {e}", err=True)
        sys.exit(1)

    save_current_config_path(config_path)

    # 5. 清理旧的解密数据库缓存
    cache_dir = os.path.join(tempfile.gettempdir(), "wechat_cli_cache")
    if os.path.isdir(cache_dir):
        for item in os.listdir(cache_dir):
            item_path = os.path.join(cache_dir, item)
            try:
                if os.path.isfile(item_path):
                    os.unlink(item_path)
                elif os.path.isdir(item_path):
                    shutil.rmtree(item_path)
            except OSError:
                pass

    click.echo(f"\n[+] 账号切换完成!")
    click.echo(f"    当前账号: {wxid}")
    click.echo(f"    数据目录: {db_dir}")
    click.echo(f"    提取到 {len(key_map)} 个数据库密钥")
    click.echo("\n现在可以使用:")
    click.echo("  wechat-cli sessions")
    click.echo("  wechat-cli history \"联系人\"")
