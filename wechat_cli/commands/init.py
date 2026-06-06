"""init 命令 — 交互式初始化，提取密钥并生成配置"""

import json
import os
import sys

import click

from ..core.config import (
    account_config_path,
    auto_detect_db_dir,
    resolve_state_paths,
    save_current_config_path,
)


@click.command()
@click.option("--db-dir", default=None, help="微信数据目录路径（默认自动检测）")
@click.option("--force", is_flag=True, help="强制重新提取密钥")
@click.pass_context
def init(ctx, db_dir, force):
    """初始化 wechat-cli：提取密钥并生成配置"""
    click.echo("WeChat CLI 初始化")
    click.echo("=" * 40)

    config_path = None
    if ctx is not None:
        root_ctx = ctx.find_root()
        config_path = root_ctx.params.get("config_path")

    # 1. 确定 db_dir
    if db_dir is None:
        db_dir = auto_detect_db_dir()
        if db_dir is None:
            click.echo("[!] 未能自动检测到微信数据目录", err=True)
            click.echo("请通过 --db-dir 参数指定，例如:", err=True)
            click.echo("  wechat-cli init --db-dir ~/path/to/db_storage", err=True)
            sys.exit(1)
        click.echo(f"[+] 检测到微信数据目录: {db_dir}")
    else:
        db_dir = os.path.abspath(db_dir)
        if not os.path.isdir(db_dir):
            click.echo(f"[!] 目录不存在: {db_dir}", err=True)
            sys.exit(1)
        click.echo(f"[+] 使用指定数据目录: {db_dir}")

    current_wxid = os.path.basename(os.path.dirname(db_dir))
    if not config_path:
        config_path = account_config_path(current_wxid)
    config_path, state_dir, keys_file = resolve_state_paths(config_path)

    # 2. 检查是否已初始化
    if os.path.exists(config_path) and os.path.exists(keys_file) and not force:
        save_current_config_path(config_path)
        click.echo(f"已初始化（配置: {config_path}）")
        click.echo("使用 --force 重新提取密钥")
        return

    # 3. 创建状态目录
    os.makedirs(state_dir, exist_ok=True)

    # 4. 提取密钥
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

    # 5. 写入配置
    cfg = {
        "db_dir": db_dir,
    }
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

    save_current_config_path(config_path)

    click.echo(f"\n[+] 初始化完成!")
    click.echo(f"    配置: {config_path}")
    click.echo(f"    密钥: {keys_file}")
    click.echo(f"    提取到 {len(key_map)} 个数据库密钥")
    click.echo("\n现在可以使用:")
    click.echo("  wechat-cli sessions")
    click.echo("  wechat-cli history \"联系人\"")
    click.echo("\n提示: 运行以下命令可启用图片查看/导出功能（需先在微信中打开聊天大图）:")
    click.echo("  wechat-cli decode-images --scan-key")
