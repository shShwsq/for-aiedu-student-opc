---
name: check_ssrf
description: 检查 SSRF(服务端请求伪造)漏洞,覆盖 requests/urllib/httpx/aiohttp 接收用户 URL 的场景。Invoke when审计涉及外部 HTTP 请求的代码,特别是 webhook、抓取、代理类功能。
---

# SSRF 审计

## 适用场景

仓库里有任何发起外部 HTTP 请求的代码都应执行本技能。
重点查:webhook 回调、URL 抓取/爬虫、代理转发、图片/文件远程加载、SSO/OAuth 回调处理。

## 执行步骤

### 1. 定位外部请求点

调用 `search_code` 搜以下模式(示例省略必填参数 `repo_path`,实际调用须带上任务工作区根路径——仓库任务为 `clone_repo` 返回值,上传任务由系统注入工作区、见任务上下文):

**Python:**
```
search_code(pattern="requests\\.(get|post|put|delete|head|patch|request)\\s*\\(", file_glob="*.py")
search_code(pattern="urllib\\.request\\.urlopen\\s*\\(", file_glob="*.py")
search_code(pattern="httpx\\.(get|post|request)\\s*\\(", file_glob="*.py")
search_code(pattern="aiohttp\\.ClientSession", file_glob="*.py")
search_code(pattern="urlopen\\s*\\(", file_glob="*.py")
```

**Node.js:**
```
search_code(pattern="axios\\.(get|post|request)\\s*\\(", file_glob="*.js")
search_code(pattern="fetch\\s*\\(", file_glob="*.js")
search_code(pattern="http\\.request\\s*\\(|https\\.request\\s*\\(", file_glob="*.js")
search_code(pattern="got\\s*\\(|node-fetch", file_glob="*.js")
```

**Go:**
```
search_code(pattern="http\\.(Get|Post|Do|NewRequest)\\s*\\(", file_glob="*.go")
```

**PHP:**
```
search_code(pattern="curl_setopt.*CURLOPT_URL|file_get_contents\\s*\\(", file_glob="*.php")
```

### 2. 判断 URL 是否用户可控

对每个命中点,调用 `read_file` 读取命中行附近的内容(用 `offset` / `max_lines` 定位),追 URL 来源,常见用户输入入口:
- `request.args` / `request.form` / `request.json` / `request.query`(Flask/FastAPI)
- `req.body` / `req.query` / `req.params`(Express)
- `@PathVariable` / `@RequestParam`(Spring)
- `$_GET` / `$_POST`(PHP)
- 从数据库读 URL 但 URL 是用户写入的(也属于可控)

### 3. 检查防护是否存在

**安全的特征(看是否同时满足):**
- URL scheme 白名单(只允许 `http` / `https`)
- 域名白名单(只允许业务域名)
- IP 解析后拦截内网(`127.0.0.0/8`、`10.0.0.0/8`、`172.16.0.0/12`、`192.168.0.0/16`、`169.254.0.0/16`、`::1`、`fc00::/7`)
- 解析后 DNS rebinding 防护(二次解析校验)

**危险的特征:**
- 直接 `requests.get(user_url)` 无任何校验
- 只校验了 `http://` / `https://` 前缀(可被 `http://127.0.0.1` 绕过)
- 跟随重定向但未在重定向后再次校验

### 4. 检查文件协议与元数据接口

```
search_code(pattern="file://|gopher://|dict://|ftp://", case_sensitive=false)
```

若用户可控 URL,且未屏蔽这些协议,属于 SSRF 高危。

特别关注云环境元数据接口(常被 SSRF 利用来偷云凭证):
- AWS / GCP: `http://169.254.169.254/`
- 阿里云: `http://100.100.100.200/`

### 5. 输出结果

每个 SSRF 问题在最终总结里按下列字段给出:

- **位置**:文件:行号
- **类别**:CWE-918 SSRF + 可控入口类型(webhook / 抓取 / 代理 / 文件远程加载)
- **严重度**:high / medium(判定见下表;平台严重度枚举只有 high/medium/low/info,无更高档)
- **证据**:用户可控入口 + 缺失的防护 + 危害(可访问内网 / 元数据接口 / 端口扫描)
- **建议**:scheme 白名单 + 域名白名单 + 解析后 IP 校验 + 禁用重定向或重定向后再次校验

## 严重度判定

| 情形 | severity |
|------|----------|
| 无任何防护,可访问云元数据接口 | high(最高危,优先报) |
| 无任何防护,可访问内网 | high |
| 只校验 scheme 前缀,可被绕过 | high |
| 有域名白名单但解析前校验(DNS rebinding) | medium |

## 避免误报

- 调用同业务内部 API(域名固定、URL 不来自用户):不报
- 第三方 SDK 内部的请求(如 `openai.ChatCompletion.create`):不报
- 测试文件里的 mock 请求:标 info

## 避免漏报

- 不要只搜 `requests.get`,Web 框架自带的请求库也要搜
- 重定向跟踪是常见绕过点,`read_file` 看是否 `allow_redirects=True` 且无二次校验
- 文件协议 `file://` 和元数据接口 `169.254.169.254` 是 SSRF 的关键利用路径,要专门检查
