---
name: check_hardcoded_secrets
description: 检查代码中硬编码的密钥、密码、token、私钥等敏感凭证。Invoke when审计任何仓库,这是必查项,适用所有语言。
---

# 硬编码密钥审计

## 适用场景

**所有仓库必查**,无论语言。硬编码凭证是最常见的低门槛漏洞之一。

## 执行步骤

### 1. 搜密钥关键字

调用 `search_code` 搜以下模式(大小写不敏感,分开搜;示例省略必填参数 `repo_path`,实际调用须带上任务工作区根路径——仓库任务为 `clone_repo` 返回值,上传任务由系统注入工作区、见任务上下文):

**通用关键字:**
```
search_code(pattern="password\\s*[:=]\\s*['\"]", case_sensitive=false)
search_code(pattern="passwd\\s*[:=]\\s*['\"]", case_sensitive=false)
search_code(pattern="pwd\\s*[:=]\\s*['\"]", case_sensitive=false)
search_code(pattern="secret\\s*[:=]\\s*['\"]", case_sensitive=false)
search_code(pattern="api[_-]?key\\s*[:=]\\s*['\"]", case_sensitive=false)
search_code(pattern="access[_-]?key\\s*[:=]\\s*['\"]", case_sensitive=false)
search_code(pattern="token\\s*[:=]\\s*['\"]", case_sensitive=false)
search_code(pattern="auth\\s*[:=]\\s*['\"]", case_sensitive=false)
```

**云服务凭证:**
```
search_code(pattern="AKIA[0-9A-Z]{16}", case_sensitive=false)  # AWS Access Key
search_code(pattern="aws_secret_access_key\\s*[:=]", case_sensitive=false)
search_code(pattern="stripe_sk_(test_)?[a-zA-Z0-9]+", case_sensitive=false)
search_code(pattern="xoxb-[a-zA-Z0-9-]+", case_sensitive=false)  # Slack bot token
search_code(pattern="ghp_[a-zA-Z0-9]+", case_sensitive=false)  # GitHub PAT
search_code(pattern="sk-[a-zA-Z0-9]{20,}", case_sensitive=false)  # OpenAI 等
```

**私钥:**
```
search_code(pattern="-----BEGIN (RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")
```

**数据库连接串(含密码):**
```
search_code(pattern="(postgres|mysql|mongodb)://[^:\\s]+:[^@\\s]+@", case_sensitive=false)
search_code(pattern="redis://:[^@\\s]+@", case_sensitive=false)
```

### 2. 验证上下文

对每个命中点,调用 `read_file` 看上下文:

**真正的硬编码(应报):**
```python
password = "admin123"
api_key = "sk-abc123..."
DATABASE_URL = "postgres://user:Passw0rd!@db.example.com/mydb"
```

**可接受的(不报或标 info):**
- 环境变量引用:`password = os.getenv("DB_PASSWORD")` ✓ 安全
- 配置文件示例:`# password = "your_password_here"` ✓ 注释
- 测试用 fixture(看是否明显假):`password = "test123"` → 标 `info`
- 字典 key 名:`{"password": ...}` → 看值是不是真的密钥

### 3. 检查 .env / 配置文件被提交

调用 `list_files`(带 `repo_path`)查根目录,看是否有 `.env`、`.env.local`、`config.json`、`config.yml`、`secrets.yml` 等文件:
- 若有,`read_file` 检查是否含真实凭证(非占位符)
- 真实凭证被提交到仓库:严重漏洞

### 4. 输出结果

每个硬编码密钥问题在最终总结里按下列字段给出:

- **位置**:文件:行号
- **类别**:CWE-798 硬编码密钥 + 具体类型(AWS Access Key / API Token / 数据库密码 / 私钥)
- **严重度**:high / info(判定见下表;平台严重度枚举只有 high/medium/low/info,无更高档)
- **证据**:真实值前 4 位打码(如 `sk-1***...`)+ 所在代码片段
- **建议**:改用环境变量 / 密钥管理服务,并立即吊销已泄露的凭证

## 严重度判定

| 类型 | severity |
|------|----------|
| AWS / 云服务 access key | high(最高危,优先报) |
| 数据库密码(明文连接串) | high(最高危,优先报) |
| OpenAI / Stripe 等付费 API key | high |
| GitHub PAT / Slack token | high |
| 通用 password / secret | high |
| 测试用的明显假值(如 "test123") | info |

## 避免误报

- 占位符(`xxx`、`your_key_here`、`<token>`、`changeme`):不报
- 注释行:不报或标 info
- 测试文件:标 info
- 值明显是变量名而非密钥(`password = password_value`):不报

## 避免漏报

- 一定要搜 `-----BEGIN ... PRIVATE KEY-----`,私钥泄露按最高档 high 报
- 数据库连接串 `postgres://user:pass@host` 经常被忽略,要专门搜
- 多语言统一处理,不要只搜 Python 风格
