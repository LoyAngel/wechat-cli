"""微信 .dat 图片解密 — 支持 V1 (XOR) 和 V2 (AES-ECB + XOR) 格式"""

import os
import re
import struct
import tempfile

from Crypto.Cipher import AES

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
MEM_MAPPED = 0x40000
MEM_IMAGE = 0x1000000
PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100

_DEFAULT_XOR_KEY = 0xBD

# 预编译正则：匹配由非字母数字边界包围的 32 字符字母数字串（取前 16 字节作为密钥）
_RE_ASCII32 = re.compile(rb"(?<![a-zA-Z0-9])([a-zA-Z0-9]{32})(?![a-zA-Z0-9])")


# ---------------------------------------------------------------------------
# XOR 密钥计算
# ---------------------------------------------------------------------------
def compute_xor_key(account_dir):
    """从账号目录的 _t.dat 文件计算 XOR 密钥（众数投票）。

    Args:
        account_dir: 账号根目录（如 E:\\xwechat_files\\wxid_xxx）

    Returns:
        XOR 密钥 (int)，失败返回 None
    """
    attach = os.path.join(account_dir, "msg", "attach")
    if not os.path.isdir(attach):
        return None

    tail_counts = {}
    for root, dirs, files in os.walk(attach):
        for fname in files:
            if not fname.lower().endswith("_t.dat"):
                continue
            fpath = os.path.join(root, fname)
            try:
                with open(fpath, "rb") as f:
                    data = f.read()
                if len(data) >= 2:
                    last_two = data[-2:]
                    key = (last_two[0], last_two[1])
                    tail_counts[key] = tail_counts.get(key, 0) + 1
            except Exception:
                continue
            if len(tail_counts) >= 32:
                break

    if not tail_counts:
        return None

    # 取出现次数最多的末尾字节对
    most_common = max(tail_counts, key=lambda k: tail_counts[k])
    x = most_common[0] ^ 0xFF
    y = most_common[1] ^ 0xD9
    if x == y:
        return x
    return None


# ---------------------------------------------------------------------------
# 密文收集（多账号支持）
# ---------------------------------------------------------------------------
def collect_account_ciphertexts(wechat_base_dir, account_filter=None):
    """收集各账号的密文样本和 XOR 密钥。

    Args:
        wechat_base_dir: 微信数据根目录（如 E:\\xwechat_files）
        account_filter: 可选，只处理指定账号（如 wxid_8ovuplni6b6922_b8bd）

    Returns:
        {account_name: {"ciphertexts": [...], "xorkey": int}}
        密文按文件 mtime 降序优先，xorkey 可能为 None（计算失败时）
    """
    if not os.path.isdir(wechat_base_dir):
        return {}

    accounts = {}
    for d in os.listdir(wechat_base_dir):
        dp = os.path.join(wechat_base_dir, d)
        if not os.path.isdir(dp) or not d.startswith("wxid_"):
            continue
        if account_filter and d != account_filter:
            continue

        attach = os.path.join(dp, "msg", "attach")
        if not os.path.isdir(attach):
            continue

        # 直接遍历收集密文，找到 8 个即停（无需 mtime 排序）
        ciphertexts = []
        for root, dirs, files in os.walk(attach):
            for fname in files:
                if not fname.lower().endswith("_t.dat"):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "rb") as f:
                        data = f.read()
                    if len(data) >= 0x1F and data[:2] == b"\x07\x08" and data[2:4] == b"V2":
                        ct = data[0x0F:0x1F]
                        if ct not in ciphertexts:
                            ciphertexts.append(ct)
                    if len(ciphertexts) >= 8:
                        break
                except Exception:
                    pass
            if len(ciphertexts) >= 8:
                break

        if ciphertexts:
            xor_key = compute_xor_key(dp)
            accounts[d] = {"ciphertexts": ciphertexts, "xorkey": xor_key}

    return accounts


# ---------------------------------------------------------------------------
# V2 解密
# ---------------------------------------------------------------------------
def _check_dht_overflow(aes_plain, aes_size):
    i = 0
    while i < len(aes_plain):
        if aes_plain[i] == 0xFF and i + 1 < len(aes_plain) and aes_plain[i + 1] == 0xC4:
            seg_len = (aes_plain[i + 2] << 8) | aes_plain[i + 3]
            dht_end = i + 2 + seg_len
            if dht_end > aes_size:
                return dht_end - aes_size
            i = dht_end
        else:
            i += 1
    return 0


def _aes_ecb_decrypt(key, data):
    cipher = AES.new(key, AES.MODE_ECB)
    aligned = (len(data) // 16) * 16
    result = b""
    if aligned > 0:
        result = cipher.decrypt(data[:aligned])
    if aligned < len(data):
        result += data[aligned:]
    return result


def _detect_format(data):
    if len(data) < 4:
        return None, None

    if data[:2] == b"\xff\xd8":
        pos = data.rfind(b"\xff\xd9")
        return (data[: pos + 2] if pos >= 0 else data), ".jpg"
    elif data[:4] == b"\x89PNG":
        iend = data.find(b"IEND")
        return (data[: iend + 8] if iend > 0 else data), ".png"
    elif data[:4] == b"GIF8":
        return data, ".gif"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return data, ".webp"
    elif data[:4] == b"wxgf":
        jpg = _wxgf_to_jpeg(data)
        if jpg:
            return jpg, ".jpg"
        return data, ".wxgf"
    elif data[:2] == b"BM":
        return data, ".bmp"
    return None, None


def decrypt_v2(data, aes_key, xor_key):
    if len(data) < 15:
        return None, None
    aes_size = struct.unpack("<I", data[6:10])[0]
    xor_size = struct.unpack("<I", data[10:14])[0]
    body = data[15:]
    gap = len(body) - aes_size - xor_size

    aes_plain = _aes_ecb_decrypt(aes_key, body[:aes_size])

    if gap == 16:
        xor_plain = bytes(b ^ xor_key for b in body[aes_size + 16:])
        result = aes_plain + xor_plain
    else:
        raw_data = body[aes_size:aes_size + gap]
        xor_plain = bytes(b ^ xor_key for b in body[aes_size + gap:])
        dht_overflow = _check_dht_overflow(aes_plain, aes_size)
        if dht_overflow > 0:
            raw_data = raw_data[16:]
        result = aes_plain + raw_data + xor_plain

    return _detect_format(result)


def decrypt_v1(data, xor_key):
    dec = bytes(b ^ xor_key for b in data)
    return _detect_format(dec)


def process_file(path, aes_key, xor_key):
    with open(path, "rb") as f:
        data = f.read()
    if len(data) >= 6 and data[:2] == b"\x07\x08" and data[2:4] == b"V2":
        return decrypt_v2(data, aes_key, xor_key)
    else:
        return decrypt_v1(data, xor_key)


# ---------------------------------------------------------------------------
# wxgf（HEVC 编码）转 JPEG
# ---------------------------------------------------------------------------
_FFMPEG_PATH = None


def _find_ffmpeg():
    global _FFMPEG_PATH
    if _FFMPEG_PATH:
        return _FFMPEG_PATH

    import glob as _glob
    for pattern in [
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\*\ffmpeg-*\bin\ffmpeg.exe"),
        r"C:\ffmpeg\bin\ffmpeg.exe",
    ]:
        for p in _glob.glob(pattern):
            if os.path.isfile(p):
                _FFMPEG_PATH = p
                return p

    import subprocess as _sp
    try:
        r = _sp.run(["where", "ffmpeg"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            _FFMPEG_PATH = r.stdout.strip().split("\n")[0].strip()
            return _FFMPEG_PATH
    except Exception:
        pass

    return None


def _wxgf_to_jpeg(wxgf_data):
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        return None

    vps_pos = -1
    for code in [b"\x00\x00\x00\x01", b"\x00\x00\x01"]:
        search_pos = 0
        while True:
            pos = wxgf_data.find(code, search_pos)
            if pos < 0:
                break
            nalu_type = (wxgf_data[pos + len(code)] >> 1) & 0x3F
            if nalu_type == 32:
                vps_pos = pos
                break
            search_pos = pos + len(code)
        if vps_pos >= 0:
            break

    if vps_pos < 0:
        return None

    hevc = wxgf_data[vps_pos:]

    import subprocess as _sp

    tmp_dir = tempfile.mkdtemp()
    hevc_path = os.path.join(tmp_dir, "tmp.hevc")
    jpg_path = os.path.join(tmp_dir, "tmp.jpg")

    try:
        with open(hevc_path, "wb") as f:
            f.write(hevc)

        _sp.run(
            [ffmpeg, "-y", "-i", hevc_path, "-vframes", "1", jpg_path],
            capture_output=True, text=True, timeout=15,
        )

        if os.path.isfile(jpg_path) and os.path.getsize(jpg_path) > 100:
            with open(jpg_path, "rb") as f:
                return f.read()
    except Exception:
        pass
    finally:
        for p in [hevc_path, jpg_path]:
            try:
                os.remove(p)
            except OSError:
                pass
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass

    return None


# ---------------------------------------------------------------------------
# 文件发现
# ---------------------------------------------------------------------------
def find_dat_files(wechat_base_dir, chat_username=None):
    """遍历微信 attach 目录，生成 .dat 图片文件路径。

    Args:
        wechat_base_dir: 微信数据根目录（包含 msg/ 的目录）
        chat_username: 可选，限制只搜索特定联系人的目录（通过 MD5 哈希匹配）

    Yields:
        (file_path, size, mtime) 元组
    """
    msg_dir = os.path.join(wechat_base_dir, "msg")
    attach_dir = os.path.join(msg_dir, "attach")
    if not os.path.isdir(attach_dir):
        return

    target_hash = None
    if chat_username:
        import hashlib
        h = hashlib.md5(chat_username.encode()).hexdigest()
        candidate = os.path.join(attach_dir, h)
        if os.path.isdir(candidate):
            target_hash = h
        else:
            return

    for entry in os.listdir(attach_dir):
        if target_hash and entry != target_hash:
            continue
        hash_dir = os.path.join(attach_dir, entry)
        if not os.path.isdir(hash_dir):
            continue
        for month_dir in os.listdir(hash_dir):
            img_dir = os.path.join(hash_dir, month_dir, "Img")
            if not os.path.isdir(img_dir):
                continue
            for fname in os.listdir(img_dir):
                if not fname.lower().endswith(".dat"):
                    continue
                base = fname.lower()
                if base.endswith("_t.dat") or base.endswith("_h.dat"):
                    continue
                fpath = os.path.join(img_dir, fname)
                try:
                    st = os.stat(fpath)
                    yield fpath, st.st_size, int(st.st_mtime)
                except OSError:
                    continue


# ---------------------------------------------------------------------------
# 批量处理
# ---------------------------------------------------------------------------
def decode_file(path, aes_key, xor_key=_DEFAULT_XOR_KEY):
    """解密单个 .dat 文件，返回 (data, ext) 或 (None, None)。"""
    try:
        return process_file(path, aes_key, xor_key)
    except Exception:
        return None, None


def decode_batch(file_paths, aes_key, xor_key, out_dir, on_progress=None):
    """批量解密 .dat 文件。

    Args:
        file_paths: .dat 文件路径列表
        aes_key: 16 字节 AES 密钥
        xor_key: XOR 密钥字节
        out_dir: 输出目录（自动创建）
        on_progress: 可选回调 (index, total, path, status, ext, size)
                      status: "ok" | "fail" | "skip"

    Returns:
        {"success": int, "failed": int, "skipped": int, "files": [...]}
    """
    os.makedirs(out_dir, exist_ok=True)

    ffmpeg_ok = _find_ffmpeg() is not None
    ffmpeg_warned = False

    paths = list(file_paths)
    total = len(paths)
    success = 0
    failed = 0
    skipped = 0
    out_files = []

    for idx, path in enumerate(paths):
        name = os.path.splitext(os.path.basename(path))[0]

        try:
            result, ext = process_file(path, aes_key, xor_key)
        except Exception:
            result, ext = None, None

        if result is not None and ext is not None and len(result) > 100:
            if ext == ".wxgf":
                if not ffmpeg_ok:
                    if not ffmpeg_warned:
                        ffmpeg_warned = True
                    skipped += 1
                    if on_progress:
                        on_progress(idx, total, path, "skip", ext, len(result))
                    continue

            out_path = os.path.join(out_dir, name + ext)
            with open(out_path, "wb") as f:
                f.write(result)
            success += 1
            out_files.append({"path": out_path, "ext": ext, "size": len(result)})
            if on_progress:
                on_progress(idx, total, path, "ok", ext, len(result))
        else:
            failed += 1
            if on_progress:
                on_progress(idx, total, path, "fail", None, 0)

    return {
        "success": success,
        "failed": failed,
        "skipped": skipped,
        "files": out_files,
        "ffmpeg_missing": not ffmpeg_ok,
    }


# ---------------------------------------------------------------------------
# 内存密钥扫描（仅 Windows）
# ---------------------------------------------------------------------------
def _verify_key(candidate, ciphertexts):
    """用密文样本验证候选密钥。"""
    try:
        c = AES.new(bytes(candidate), AES.MODE_ECB)
        for ct in ciphertexts:
            if c.decrypt(bytes(ct))[:3] == b"\xff\xd8\xff":
                return True
    except Exception:
        pass
    return False


def _scan_pid_for_accounts(pid, account_ciphertexts):
    """扫描单个进程，用各账号密文分别验证。

    Args:
        pid: 进程 PID
        account_ciphertexts: {account_name: {"ciphertexts": [...], "xorkey": int}}

    Returns:
        {account_name: bytes_key} 字典，每个账号最多一个匹配
    """
    import ctypes
    import ctypes.wintypes as wt

    kernel32 = ctypes.windll.kernel32
    h = kernel32.OpenProcess(0x0010 | 0x0400, False, pid)
    if not h:
        return {}

    class MBI(ctypes.Structure):
        _fields_ = [
            ("BaseAddress", ctypes.c_uint64),
            ("AllocationBase", ctypes.c_uint64),
            ("AllocationProtect", wt.DWORD),
            ("PartitionId", wt.DWORD),
            ("RegionSize", ctypes.c_uint64),
            ("State", wt.DWORD),
            ("Protect", wt.DWORD),
            ("Type", wt.DWORD),
        ]

    # 为每个账号准备密文字节列表
    acct_cts = {a: [bytes(ct) for ct in info["ciphertexts"]]
                for a, info in account_ciphertexts.items()}
    found = {}

    try:
        mbi = MBI()
        mbi_sz = ctypes.sizeof(MBI)
        addr = 0
        regions = []
        while addr < 0x7FFFFFFFFFFF:
            if not kernel32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), mbi_sz):
                break
            if (
                mbi.State == MEM_COMMIT
                and mbi.Protect != PAGE_NOACCESS
                and not (mbi.Protect & PAGE_GUARD)
                and mbi.Type in (MEM_PRIVATE, MEM_MAPPED, MEM_IMAGE)
                and mbi.RegionSize <= 100 * 1024 * 1024
                and mbi.RegionSize >= 4096
            ):
                regions.append((mbi.BaseAddress, mbi.RegionSize))
            nxt = mbi.BaseAddress + mbi.RegionSize
            if nxt <= addr:
                break
            addr = nxt

        chunk_sz, overlap = 4 * 1024 * 1024, 66

        for idx, (base, size) in enumerate(regions):
            offset, trailing = 0, b""

            while offset < size:
                remaining = min(size - offset, chunk_sz)
                try:
                    buf = (ctypes.c_char * remaining)()
                    got = ctypes.c_size_t(0)
                    if not kernel32.ReadProcessMemory(
                        h, ctypes.c_void_p(base + offset), buf, remaining, ctypes.byref(got)
                    ):
                        offset += remaining
                        trailing = b""
                        continue
                    chunk_data = bytes(buf[: got.value])
                except Exception:
                    offset += remaining
                    trailing = b""
                    continue

                data = trailing + chunk_data

                # 正则搜索 ASCII 32 字符字母数字串，取前 16 字节作为密钥
                for m in _RE_ASCII32.finditer(data):
                    k16 = m.group(1)[:16]
                    if k16 in found.values():
                        continue
                    for acct, cts in acct_cts.items():
                        if acct in found:
                            continue
                        if _verify_key(k16, cts):
                            found[acct] = k16
                            if len(found) >= len(acct_cts):
                                kernel32.CloseHandle(h)
                                return found

                trailing = data[-overlap:] if len(data) > overlap else data
                offset += remaining
    finally:
        kernel32.CloseHandle(h)

    return found


def _get_wechat_processes():
    """返回所有 Weixin.exe 进程的 (pid, mem_mb) 列表，按内存降序。"""
    import csv as _csv
    import io as _io
    import subprocess as _sp

    pids = []

    # 尝试 wmic
    r = _sp.run(
        ["wmic", "process", "where", "name='Weixin.exe'",
         "get", "ProcessId,WorkingSetSize", "/format:csv"],
        capture_output=True, text=True,
    )
    for line in r.stdout.strip().split("\n"):
        line = line.strip()
        if not line or "ProcessId" in line:
            continue
        parts = line.split(",")
        if len(parts) >= 3:
            try:
                pid = int(parts[2])
                mem = int(parts[3]) // (1024 * 1024)
                pids.append((pid, mem))
            except (ValueError, IndexError):
                pass

    # 后备：tasklist
    if not pids:
        r = _sp.run(
            ["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True,
        )
        for line in r.stdout.strip().split("\n"):
            if "Weixin.exe" not in line:
                continue
            try:
                row = next(_csv.reader(_io.StringIO(line)))
                pid = int(row[1])
                mem = int(row[4].replace(" K", "").replace(",", "")) // 1024
                pids.append((pid, mem))
            except Exception:
                pass

    pids.sort(key=lambda x: x[1], reverse=True)
    return pids


def scan_dat_key_from_memory(wechat_base_dir=None, account_filter=None, on_status=None):
    """扫描微信进程内存提取 DAT AES 密钥，支持多账号。

    Args:
        wechat_base_dir: 微信数据根目录（如 E:\\xwechat_files）
        account_filter: 可选，只处理指定账号
        on_status: 可选回调 (message: str)

    Returns:
        单账号时返回 (aes_key_bytes, xor_key_int) 或 (None, None)
        多账号时返回 {account: {"aes_key": bytes, "xor_key": int}}，无结果返回 {}
    """
    import sys

    if sys.platform != "win32":
        if on_status:
            on_status("内存扫描仅支持 Windows")
        return {} if not account_filter else (None, None)

    def _status(msg):
        if on_status:
            on_status(msg)

    # 1. 收集各账号密文
    base_dir = wechat_base_dir
    if not base_dir:
        base_dir = r"E:\xwechat_files"
    if not os.path.isdir(base_dir):
        _status(f"未找到微信数据目录: {base_dir}")
        return {} if not account_filter else (None, None)

    accounts = collect_account_ciphertexts(base_dir, account_filter=account_filter)
    if not accounts:
        _status("未找到微信账号密文样本。请确认微信中已打开过聊天图片。")
        return {} if not account_filter else (None, None)

    _status(f"收集到 {len(accounts)} 个账号的密文样本")
    for acct, info in accounts.items():
        _status(f"  {acct}: {len(info['ciphertexts'])} 密文, "
                f"XOR=0x{info['xorkey']:02X}" if info['xorkey'] else f"  {acct}: XOR 待定")

    # 2. 获取微信进程列表
    pids = _get_wechat_processes()
    if not pids:
        _status("微信未运行")
        return {} if not account_filter else (None, None)

    _status(f"找到 {len(pids)} 个微信进程")

    # 3. 扫描所有进程
    all_found = {}
    for pid, mem in pids:
        _status(f"扫描 PID={pid} ({mem}MB)...")
        remaining = {a: info for a, info in accounts.items() if a not in all_found}
        if not remaining:
            break
        found = _scan_pid_for_accounts(pid, remaining)
        for acct, key in found.items():
            all_found[acct] = {
                "aes_key": key,
                "xor_key": accounts[acct]["xorkey"],
            }
            _status(f"  找到 {acct} 的密钥: {key.decode('ascii')}")

    # 4. 返回结果
    if not all_found:
        _status("未找到任何密钥。")
        _status("提示: 请先在微信中打开聊天窗口的大图（点开图片查看原图），再重新扫描。")
        return {} if not account_filter else (None, None)

    # 单账号：返回简单元组
    if account_filter and account_filter in all_found:
        info = all_found[account_filter]
        return info["aes_key"], info["xor_key"]

    # 仅当系统只有单一账号时返回元组，多账号时始终返回 dict
    if len(all_found) == 1 and len(accounts) == 1:
        info = list(all_found.values())[0]
        return info["aes_key"], info["xor_key"]

    # 多账号：返回完整字典
    return all_found
