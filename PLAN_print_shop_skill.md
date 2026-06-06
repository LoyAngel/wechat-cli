# 打印店 Skill — 实施方案（修订版）

## Context

打印店每天收到大量微信消息（图片、文件），人工整理耗时且易遗漏。目标是创建一个 OpenClaw Skill，自动检测多个微信账号的新消息，用 LLM 判断是否为打印资料，自动归类到客户文件夹，并同步创建飞书文档记录。操作人员无需编程知识。

**运行环境**：OpenClaw 调度执行，cron 定时触发，lark-cli 已在 OpenClaw 中可用。

---

## 一、核心工作流

```
cron 定时触发
    │
    ▼
┌─────────────────────────────────────────────────┐
│ OpenClaw Skill                                  │
│                                                  │
│  1. 遍历所有微信账号配置                           │
│     └─ wechat-cli --config <acc> new-messages     │
│        --detail --state-file <acc_state>          │
│                                                  │
│  2. 对每个有新消息的会话，LLM 判断：                │
│     - 是否包含打印资料（文件/图片）？               │
│     - 客户名是什么？                               │
│     - 是否有打印要求（份数/纸张等）？               │
│                                                  │
│  3. 创建目录 & 复制文件                           │
│     └─ wechat-cli copy-media --chat <客户>        │
│        --out-dir 打印店/<日期>/<账号>/<客户>/      │
│        --type image,file                          │
│        --prefer-hd  (优先 _d.dat 高清图)          │
│                                                  │
│  4. 飞书文档同步                                  │
│     └─ lark-cli 检查/创建当日文档，追加记录        │
│                                                  │
│  5. 输出本轮摘要                                   │
└─────────────────────────────────────────────────┘
```

---

## 二、目录与文件结构

### 客户文件目录

```
打印店/
└── 2026-05-30/                ← 日期
    ├── 店长号/                 ← 微信账号名（用户配置的别名）
    │   ├── 张三/               ← 客户微信名
    │   │   ├── 海报.png
    │   │   ├── 报价单.pdf
    │   │   └── ...
    │   └── 李四/
    │       └── 照片.jpg
    └── 客服号/
        └── 王五/
            └── 资料.docx
```

### 账号映射配置（由用户维护）

```yaml
# print_shop_config.yaml
accounts:
  - name: "店长号"              # 用户自定义别名
    wxid: "wxid_abc123"        # 微信原始 ID
    config: "~/.wechat-cli/config_shop1.json"
  - name: "客服号"
    wxid: "wxid_def456"
    config: "~/.wechat-cli/config_shop2.json"

poll_interval_minutes: 5
output_base_dir: "D:/打印店/"
```

说明：
- `config` 是 wechat-cli 的配置文件路径（不是目录）。
- 与该配置文件同目录下会生成 `all_keys.json`、`image_keys.json`、`decoded_images/` 等状态文件。
- 每个账号需要用对应的 `--config` 执行 `init` 与 `decode-images --scan-key`。
 - 若微信切换登录或需要重新选择账号，执行：
   ```bash
   wechat-cli --config ~/.wechat-cli/config_shop1.json change-account
   ```

### 本地状态文件

```
~/.print_shop/
├── last_check_店长号.json     # 每个账号独立的 new-messages 状态
├── last_check_客服号.json
└── feishu_docs.json           # { "2026-05-30": "doc_url", ... }
```

---

## 三、wechat-cli 需要的三项调整

### 调整 1：`new-messages --detail`（高优先级）

**文件**：`wechat_cli/commands/new_messages.py`

**新增参数**：
- `--detail N`：对每个有新消息的会话，自动调用 `history --limit N --media`，将最近 N 条消息详情（含媒体路径）嵌入返回结果
- `--state-file PATH`：指定状态文件路径，替代默认的 `last_check.json`，实现多账号状态隔离

**输出格式**（JSON）：
```json
{
  "first_call": false,
  "new_count": 2,
  "messages": [
    {
      "chat": "张三",
      "username": "wxid_xxx",
      "is_group": false,
      "last_message": "帮我打印这个",
      "msg_type": "图片",
      "time": "14:30:00",
      "detail": [                           // --detail 新增
        {"type": "image", "dat_path": "...", "decoded_path": "...", "time": "14:29:55"},
        {"type": "text", "content": "帮我打印这个", "time": "14:30:00"}
      ]
    }
  ]
}
```

### 调整 2：新增 `copy-media` 命令（高优先级）

**新文件**：`wechat_cli/commands/copy_media.py`

**功能**：
```bash
wechat-cli copy-media --chat "张三" \
  --out-dir "D:/打印店/2026-05-30/客服号/张三/" \
  --type image,file \
  --since "2026-05-30 00:00" \
  --prefer-hd
```

**行为**：
- 获取指定聊天的图片和文件消息
- 图片：解码 `.dat` → `.jpg/.png`；若 `--prefer-hd` 且存在对应 `_d.dat` 文件，优先解码高清版
- 文件：从 `msg/file/YYYY-MM/` 复制到目标目录
- 支持 `--since` / `--until` 时间范围过滤
- 返回复制/解码结果清单

**`_d.dat` 说明**：微信图片缓存中 `xxx.dat` 是缩略图，`xxx_d.dat`（如果存在）是原图/高清版。优先使用 `_d.dat` 解码即可获得高清图。

### 调整 3：密钥扫描提醒（文档层面）

在 `new-messages --detail` 或 `copy-media` 执行图片解码前，检查 `image_keys.json` 是否存在对应账号的密钥。若不存在，输出清晰的指引：

```
错误: 账号 "店长号" (wxid_abc123) 未找到图片解密密钥。

请按以下步骤操作：
  1. 确认微信已登录并在运行
  2. 在微信中打开任意聊天窗口的大图（点开图片查看原图）
  3. 运行: wechat-cli --config ~/.wechat-cli/config_shop1.json decode-images --scan-key
  4. 重新运行本命令
```

---

## 四、lark-cli（已安装，无需构建）

lark-cli 已在 OpenClaw 环境可用，Skill 中直接调用：

```bash
# 创建文档
lark-cli doc create --title "打印店日报 2026-05-30" --folder "打印店"

# 追加内容
lark-cli doc append --doc-id <id> --content "## 张三\n- 海报.png (A4, 10份)\n- 报价单.pdf\n"
```

具体命令格式以 OpenClaw 中 lark-cli 的实际 help 输出为准。

---

## 五、OpenClaw Skill 编写方式

Skill 文件存放于 OpenClaw 的 skills 目录，格式为 Markdown：

```markdown
---
name: print-shop
description: 打印店微信消息自动分类、文件整理与飞书同步
triggers:
  - cron: "*/5 * * * *"    # 每 5 分钟
---

# 打印店助手

## 触发时执行

1. 读取 ~/.print_shop/print_shop_config.yaml 获取所有账号配置
2. 对每个账号执行：
   wechat-cli --config {{config}} new-messages --detail 5 --state-file ~/.print_shop/last_check_{{name}}.json
3. 分析返回的新消息，判断是否为打印资料...
4. ...
```

---

## 六、实施步骤

### 阶段 A：wechat-cli 增强
1. [ ] `new-messages` 增加 `--detail` 和 `--state-file` 参数
2. [ ] 新增 `copy-media` 命令（支持 `--prefer-hd`）
3. [ ] 密钥缺失时的友好提示

### 阶段 B：OpenClaw Skill 编写
4. [ ] 配置文件模板 `print_shop_config.yaml`
5. [ ] OpenAI Skill 文件（Markdown）
6. [ ] 飞书文档地址本地存储 (`feishu_docs.json`) 读写逻辑

### 阶段 C：测试
7. [ ] 单账号端到端测试
8. [ ] 多账号并发测试
9. [ ] LLM 分类准确性验证

---

## 七、验证方式

1. 准备测试微信账号，发送模拟打印消息（图片 + "帮我打印"）
2. 手动执行 Skill，观察是否正确创建目录、解码图片、复制文件
3. 等待 cron 第二次触发，验证增量检测是否只处理新消息
4. 检查飞书文档是否正确追加记录
