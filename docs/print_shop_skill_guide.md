# 打印店 Skill — OpenClaw 创建指南

## 背景

打印店顾客通过微信发来图片、文件等打印资料，消息繁杂众多。此 Skill 利用 wechat-cli 自动检测多个微信账号的客户消息，通过 LLM 判断是否为打印资料，自动分类整理到本地文件夹，并同步创建飞书文档记录。

操作人员无需编程知识，配置完成后全自动运行。

---

## 一、前置准备

### 1.1 确认 wechat-cli 已安装并初始化

```bash
wechat-cli --version   # 应显示 0.2.4+
```

每个微信账号需要：
- 微信保持登录运行
- 已完成 `wechat-cli init`（提取数据库密钥）
- 已完成 `wechat-cli decode-images --scan-key`（提取图片密钥，需先打开任意聊天大图）

### 1.2 多账号配置

每个微信账号需要独立的 wechat-cli 配置文件：

```bash
# 账号 1: 店长号
wechat-cli --config ~/.wechat-cli/config_shop1.json init

# 账号 2: 客服号
wechat-cli --config ~/.wechat-cli/config_shop2.json init
```

注意：
- `--config` 是配置文件路径（不是目录）。
- 与该配置文件同目录下会生成 `all_keys.json`、`image_keys.json`、`decoded_images/` 等状态文件。
- 每个账号都需要用对应的 `--config` 再执行一次：
  ```bash
  wechat-cli --config ~/.wechat-cli/config_shop1.json decode-images --scan-key
  wechat-cli --config ~/.wechat-cli/config_shop2.json decode-images --scan-key
  ```

如需重新选择账号或微信登录发生变化，可执行：

```bash
wechat-cli --config ~/.wechat-cli/config_shop1.json change-account
```

### 1.3 lark-cli（已在 OpenClaw 中可用）

确认 lark-cli 已在 OpenClaw 环境中配置好飞书应用的认证信息。

---

## 二、配置文件

### 2.1 打印店配置 `~/.print_shop/config.yaml`

```yaml
# 微信账号映射
accounts:
  - name: "店长号"                    # 别名（用于目录名和显示）
    wxid: "wxid_abc123def456"        # 微信原始 wxid
    config: "~/.wechat-cli/config_shop1.json"

  - name: "客服号"
    wxid: "wxid_xyz789ghi012"
    config: "~/.wechat-cli/config_shop2.json"

# 输出根目录
output_base_dir: "D:/打印店/"

# 轮询间隔（cron 表达式，此处仅作记录，实际 cron 独立设置）
poll_cron: "*/5 * * * *"

# 每次获取的最近消息数（用于 LLM 判断）
detail_count: 5

# LLM 打印意图判断提示词（可自定义）
classify_prompt: |
  你是一个打印店助手。请判断以下微信消息是否包含打印资料（文件、图片）。

  打印资料的典型特征：
  - 发送了图片或文件
  - 消息内容涉及打印、复印、制作等关键词
  - 请求制作海报、名片、传单、标书等

  非打印资料的特征：
  - 纯文字闲聊
  - 表情包（非原图级别的图片）
  - 语音、视频通话
  - 链接分享

  对每条消息，返回 JSON：
  {
    "is_print_job": true/false,
    "customer_name": "客户称呼（从备注/昵称/消息推断）",
    "notes": "打印要求备注（份数、纸张、大小等，如无可为空）"
  }
```

### 2.2 飞书文档映射 `~/.print_shop/feishu_docs.json`

```json
{
  "2026-05-30": {
    "doc_id": "doxcnXXXXXX",
    "doc_url": "https://xxx.feishu.cn/docx/XXXXXX"
  }
}
```

每日文档由 Skill 自动创建并记录在此文件中。

---

## 三、目录结构

```
D:/打印店/                        ← output_base_dir
├── 2026-05-30/                   ← 日期
│   ├── 店长号/                   ← 微信账号别名
│   │   ├── 张三/                 ← 客户微信名
│   │   │   ├── 海报.png
│   │   │   └── 报价单.pdf
│   │   └── 李四/
│   │       └── 证件照.jpg
│   └── 客服号/
│       └── 王五/
│           └── 资料.docx
└── 2026-05-31/
    └── ...
```

```
~/.print_shop/
├── config.yaml                   ← 配置文件
├── last_check_店长号.json        ← wechat-cli 状态（自动维护）
├── last_check_客服号.json
└── feishu_docs.json              ← 飞书文档 ID 映射
```

---

## 四、Skill 工作流

```
┌─ cron 每 N 分钟触发 ───────────────────────────────────────┐
│                                                              │
│  1. 读取 ~/.print_shop/config.yaml                          │
│                                                              │
│  2. 遍历每个账号:                                            │
│     ┌─────────────────────────────────────────────────┐     │
│     │ wechat-cli --config <config>                     │     │
│     │   new-messages                                   │     │
│     │   --customers-only    ← 仅个人客户               │     │
│     │   --detail 5          ← 含最近 5 条消息详情      │     │
│     │   --state-file ~/.print_shop/last_check_<name>.json │  │
│     └─────────────────────────────────────────────────┘     │
│                                                              │
│  3. 如果没有新消息 → 结束                                    │
│                                                              │
│  4. LLM 分析每条新消息:                                      │
│     - 是否为打印资料？                                      │
│     - 客户名称？                                            │
│     - 打印要求？                                            │
│                                                              │
│  5. 对确认为打印资料的客户:                                  │
│     ┌─────────────────────────────────────────────────┐     │
│     │ wechat-cli copy-media --chat "<客户>"            │     │
│     │   --out-dir "<output_base>/<日期>/<账号>/<客户>/" │     │
│     │   --prefer-hd         ← 优先高清图片             │     │
│     │   --since "<今天 00:00>"                         │     │
│     └─────────────────────────────────────────────────┘     │
│                                                              │
│  6. 飞书文档同步:                                            │
│     - 检查 feishu_docs.json 是否有当日文档                  │
│     - 无则创建: lark-cli doc create --title "打印日报 <日期>"│
│     - 追加记录: lark-cli doc append --doc-id <id>           │
│       --content "## <客户名>\n- 文件列表\n- 打印要求"       │
│                                                              │
│  7. 输出本轮摘要（供操作人员查看）                           │
└──────────────────────────────────────────────────────────────┘
```

---

## 五、OpenClaw Skill 文件

在 OpenClaw 的 skills 目录下创建 `print-shop.md`：

```markdown
---
name: print-shop
description: >
  打印店微信消息自动整理。定时检测多个微信账号的客户消息，
  LLM 判断打印意图，自动归类文件，同步飞书文档。
triggers:
  - cron: "*/5 * * * *"
---

# 打印店助手

你是打印店的智能助手，负责自动检测微信客户消息并整理打印资料。

## 配置文件

所有配置在 `~/.print_shop/config.yaml`，首次使用需创建。
飞书文档映射在 `~/.print_shop/feishu_docs.json`。

## 每次触发时执行

### Step 1 — 读取配置

读取 `~/.print_shop/config.yaml` 获取账号列表和输出目录。
如果配置文件不存在，输出提示并退出。

### Step 2 — 检查所有账号的新消息

对每个账号，执行（注意：如果微信未登录或密钥过期会报错，此时跳过该账号并通知）：

```bash
wechat-cli --config {{account.config}} new-messages \
  --customers-only \
  --detail {{detail_count}} \
  --state-file ~/.print_shop/last_check_{{account.name}}.json \
  --format json
```

### Step 3 — LLM 判断

将返回的 JSON 中 `new_count > 0` 的消息，使用以下提示词判断：

"""
{{classify_prompt}}

输入消息:
{{messages_json}}
"""

返回每个会话的判断结果。

### Step 4 — 文件整理

对每个 `is_print_job: true` 的会话，确定客户名（优先用备注，否则用昵称），然后：

```bash
wechat-cli copy-media \
  --chat "{{customer_username}}" \
  --out-dir "{{output_base_dir}}/{{today_date}}/{{account_name}}/{{customer_name}}/" \
  --prefer-hd \
  --since "{{today_date}} 00:00" \
  --format json
```

### Step 5 — 飞书文档同步

1. 读取 `~/.print_shop/feishu_docs.json`
2. 检查是否存在 `{{today_date}}` 键
3. 若不存在，调用 lark-cli 创建文档：
   ```bash
   lark-cli doc create --title "打印日报 {{today_date}}" --folder "打印店"
   ```
   将返回的 doc_id 和 url 写入 `feishu_docs.json`
4. 对每个客户的打印订单，追加到文档：
   ```bash
   lark-cli doc append --doc-id {{doc_id}} --content "## {{customer_name}}
   - 文件: {{file_list}}
   - 要求: {{notes}}"
   ```

### Step 6 — 输出摘要

用文本格式列出本轮处理结果：
- 检查了几个账号
- 发现几个新消息会话
- 确认几个打印订单
- 复制了多少文件
- 飞书文档链接

## 错误处理

- **微信未登录/密钥过期**：跳过该账号，在摘要中标注 "(密钥缺失，请打开微信大图后运行 decode-images --scan-key)"
- **copy-media 报错**：在摘要中标注失败原因
- **lark-cli 报错**：文件仍会整理，仅飞书同步失败，在摘要中提示

## 手动触发

用户也可以直接让你执行单次检查：
- "检查微信打印消息"
- "看看有没有新的打印订单"
```

---

## 六、Cron 配置

在 OpenClaw 中设置 cron 触发器（或在系统 crontab 中）：

```
# 每 10 分钟检查一次（避免整点高峰）
*/10 * * * * openclaw run print-shop
```

推荐在非整点分钟执行（如 2,7,12,17...），避免与其他定时任务竞争。

---

## 七、首次使用检查清单

- [ ] 所有微信账号已登录并在运行
- [ ] 每个账号已执行 `wechat-cli init`
- [ ] 每个账号已在微信中打开过聊天大图，并执行 `wechat-cli decode-images --scan-key`
- [ ] 已创建 `~/.print_shop/config.yaml` 并填写正确的账号映射
- [ ] 已确认 lark-cli 可用（`lark-cli --help`）
- [ ] 已创建输出根目录 `output_base_dir`
- [ ] OpenClaw Skill 文件已就位
- [ ] 手动触发一次测试：在 OpenClaw 中输入 "检查微信打印消息"

---

## 八、故障排查

| 现象 | 原因 | 解决 |
|---|---|---|
| `new-messages` 无输出 | 微信未登录或数据库加密 | 确认微信运行中，重新 `init` |
| 图片解密失败 | 密钥过期（微信重启后可能变化） | 打开微信大图后运行 `decode-images --scan-key` |
| `copy-media` 报密钥错误 | 同上 | 同上 |
| 客户名显示为 wxid | 联系人未在通讯录中 | 正常现象，LLM 会尝试从消息上下文推断 |
| 飞书文档创建失败 | lark-cli 认证过期 | 检查飞书应用 token 是否有效 |
| 高清图片不存在 | 客户发送的不是原图 | `_d.dat` 仅对原图存在，缩略图无高清版 |
