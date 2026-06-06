"""图片密钥管理 — 持久化 AES/XOR 密钥到配置目录下的 image_keys.json"""

import json
import os
import sys

from .config import STATE_DIR

DEFAULT_IMAGE_KEYS_FILE = os.path.join(STATE_DIR, "image_keys.json")
_DEFAULT_XOR_KEY = 0xBD


class MissingKeyError(Exception):
    """图片密钥未就绪时抛出。"""


class ImageKeyManager:
    """管理微信图片解密密钥（AES + XOR）的持久化和查询。"""

    def __init__(self, wechat_files_root, image_keys_file=None):
        self._wechat_files_root = wechat_files_root
        self._keys_file = image_keys_file or DEFAULT_IMAGE_KEYS_FILE
        self._keys = {}  # {account: {"aes_key": str, "xor_key": int}}
        self._load()

    def _load(self):
        if os.path.exists(self._keys_file):
            try:
                with open(self._keys_file, encoding="utf-8") as f:
                    self._keys = json.load(f)
            except (json.JSONDecodeError, IOError):
                self._keys = {}

    def _save(self):
        key_dir = os.path.dirname(self._keys_file)
        if key_dir:
            os.makedirs(key_dir, exist_ok=True)
        with open(self._keys_file, "w", encoding="utf-8") as f:
            json.dump(self._keys, f, indent=2, ensure_ascii=False)

    # ---- public API ----

    def has_key(self, account=None):
        """检查是否有可用密钥。"""
        if account:
            return account in self._keys
        return len(self._keys) > 0

    def get_key(self, account=None):
        """获取 (aes_key_bytes, xor_key_int)。

        Args:
            account: 指定账号 wxid，多账号时建议指定

        Returns:
            (aes_key_bytes, xor_key_int)

        Raises:
            MissingKeyError: 无可用密钥
        """
        if account and account in self._keys:
            info = self._keys[account]
            return info["aes_key"].encode("ascii"), info.get("xor_key", _DEFAULT_XOR_KEY)

        if not account and self._keys:
            # 单账号 / 取第一个
            acct = next(iter(self._keys))
            info = self._keys[acct]
            return info["aes_key"].encode("ascii"), info.get("xor_key", _DEFAULT_XOR_KEY)

        raise MissingKeyError(
            "未找到图片解密密钥。\n"
            "请运行: wechat-cli decode-images --scan-key\n"
            "注意: 扫描前需先打开微信聊天大图！"
        )

    def set_key_manual(self, account, aes_key_hex, xor_key=None):
        """手动设置密钥（来自 decode-images --key）。"""
        self._keys[account] = {"aes_key": aes_key_hex, "xor_key": xor_key}
        self._save()

    def scan_and_save(self, account_filter=None, on_status=None):
        """扫描微信进程内存并持久化密钥。"""
        if sys.platform != "win32":
            raise RuntimeError("内存扫描仅支持 Windows")

        from .image_decrypt import scan_dat_key_from_memory

        root = self._wechat_files_root
        if not root or not os.path.isdir(root):
            raise RuntimeError(f"微信数据根目录不可用: {root}")

        result = scan_dat_key_from_memory(
            wechat_base_dir=root,
            account_filter=account_filter,
            on_status=on_status,
        )

        if not result:
            return 0

        if isinstance(result, dict):
            count = 0
            for acct, info in result.items():
                key_str = info["aes_key"].decode("ascii")
                xor_key = info.get("xor_key")
                self._keys[acct] = {"aes_key": key_str, "xor_key": xor_key}
                count += 1
            self._save()
            return count
        else:
            aes_key, xor_key = result
            # 单账号 — 需要确定账号名
            # 从 wechat_files_root 下找唯一 wxid_ 目录
            acct = account_filter or self._detect_single_account()
            if acct:
                self._keys[acct] = {
                    "aes_key": aes_key.decode("ascii"),
                    "xor_key": xor_key,
                }
                self._save()
                return 1
            return 0

    def _detect_single_account(self):
        """从 wechat_files_root 找唯一 wxid 目录。"""
        root = self._wechat_files_root
        if not root or not os.path.isdir(root):
            return None
        wxid_dirs = [
            d for d in os.listdir(root)
            if d.startswith("wxid_") and os.path.isdir(os.path.join(root, d))
        ]
        return wxid_dirs[0] if len(wxid_dirs) == 1 else None

    @property
    def accounts(self):
        """返回所有已知账号的 wxid 列表。"""
        return list(self._keys.keys())

    @property
    def keys_file(self):
        """返回密钥文件路径。"""
        return self._keys_file
