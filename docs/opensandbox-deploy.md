# OpenSandbox 部署指南(Linux 服务器)

本文档指导如何在 Linux 服务器上部署 OpenSandbox Server,并让 SecondLook 后端连接到它。

> 配置项以 OpenSandbox 官方 `server/configuration.md` 为准。本文只覆盖 SecondLook 接入需要的最小配置。

## 前置条件

- **Linux 服务器**(Ubuntu 20.04+ / CentOS 7+ / Debian 11+ 推荐;macOS / Windows WSL2 也支持)
- **Docker Engine 20.10+** 已安装并运行(`docker --version` 能输出版本号)
- **Python 3.10+**(`python3 --version`)
- **pip / uv** 任一即可
- **服务器对外开放端口 8080**(或你自定义的端口),供后端连接

## 一、安装 OpenSandbox Server

### 1.1 安装 Server

`opensandbox-server` 是 CLI 应用(不是库),Debian/Ubuntu 12+ 的 Python 是 PEP 668 externally-managed,直接 `pip install` 会被拦。正确做法是用 `uv tool install` 或 `pipx`:它自动建独立 venv,把命令放到 `~/.local/bin/opensandbox-server`。

```bash
# 方式 A(推荐):uv tool install —— 需先装 uv
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
uv tool install opensandbox-server

# 方式 B:pipx —— 需先装 pipx
sudo apt install -y pipx
pipx ensurepath
pipx install opensandbox-server

# 让当前 shell 能找到 ~/.local/bin(两种方式装完都要执行一次,或重登)
export PATH="$HOME/.local/bin:$PATH"

# 验证:能列出子命令即装好(此 CLI 没有 --version)
opensandbox-server --help
```

> 不要用 `uvx opensandbox-server` 长期运行:`uvx` 是临时执行,每次启动都可能重新拉包,systemd 托管时路径也不好定位。装成正式命令更稳。
> 也不要用 `pip install --break-system-packages`:会污染系统 Python,可能破坏 apt 管理的包。

### 1.2 生成配置文件

```bash
# 生成 Docker runtime 配置模板(推荐)
opensandbox-server init-config ~/.sandbox.toml --example docker

# 覆盖已有配置
opensandbox-server init-config ~/.sandbox.toml --example docker --force
```

生成的模板包含 `[runtime]`、`[docker]`、`[egress]` 等全部必要字段。**必须检查/修改以下两项**:

```toml
[server]
# 模板默认是 127.0.0.1,只能本机访问。要让后端远程连接,必须改成 0.0.0.0
host = "0.0.0.0"
port = 8080

# 鉴权:留空则不鉴权,但非交互启动需设环境变量 OPENSANDBOX_INSECURE_SERVER=YES(见 1.4)
# 生产环境强烈建议设一个随机长字符串:
# api_key = "your-secret-api-key"

[runtime]
type = "docker"
# init-config 会自动填入当前版本的 execd 镜像,手写时不能省,否则启动失败
execd_image = "opensandbox/execd:v1.0.21"

[storage]
# 允许挂载到沙箱的宿主机路径前缀。空列表 = 禁止任何 host 挂载(安全默认)
# 要把 SSH key 挂载进沙箱,需放行对应路径前缀,例如:
# allowed_host_paths = ["/home"]
```

完整配置参考:https://github.com/opensandbox-group/OpenSandbox/blob/main/server/configuration.md

### 1.3 启动 Server

```bash
# 前台运行,看日志
opensandbox-server

# 后台运行 + 日志
nohup opensandbox-server > ~/opensandbox.log 2>&1 &

# 验证:返回 JSON 即成功
curl http://localhost:8080/health
```

### 1.4 关于鉴权的重要说明

`[server].api_key` 留空时,Server 仍然可以启动,但**非交互环境**(systemd / nohup / Docker / CI)下必须显式确认风险,否则启动会卡住:

```bash
# 方式 A:设环境变量(推荐用于 systemd / nohup)
export OPENSANDBOX_INSECURE_SERVER=YES

# 方式 B:在配置里设 api_key(生产推荐)
# [server]
# api_key = "your-secret-api-key"
```

设了 `api_key` 后,所有 API 请求(除 `/health`、`/docs`、`/redoc`)必须带 header `OPEN-SANDBOX-API-KEY: your-secret-api-key`。SecondLook 后端通过 `SANDBOX_API_KEY` 环境变量传入。

### 1.5 配置 systemd(可选,生产推荐)

`opensandbox-server` 装在 `~/.local/bin/`,systemd 的非登录 shell 默认不加载它,所以 ExecStart 必须用**绝对路径**,不能用 `$(which ...)`(在 `sudo tee` 的 heredoc 里 PATH 往往不含 `~/.local/bin`,会展开成空串)。

```bash
# 先确认绝对路径(应该是 ~/.local/bin/opensandbox-server)
which opensandbox-server
# 例如输出:/home/admin/.local/bin/opensandbox-server

sudo tee /etc/systemd/system/opensandbox.service > /dev/null <<EOF
[Unit]
Description=OpenSandbox Server
After=docker.service network.target
Requires=docker.service

[Service]
Type=simple
User=$(whoami)
# 用绝对路径!不要用 $(which ...),sudo 上下文里 PATH 可能不含 ~/.local/bin
ExecStart=/home/$(whoami)/.local/bin/opensandbox-server
Restart=on-failure
RestartSec=5
Environment=PATH=/usr/local/bin:/usr/bin:/bin:/home/$(whoami)/.local/bin
# 若 [server].api_key 留空,必须加这一行,否则非交互启动会卡住
Environment=OPENSANDBOX_INSECURE_SERVER=YES

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now opensandbox
sudo systemctl status opensandbox
```

如果 `which opensandbox-server` 输出的不是 `/home/<user>/.local/bin/opensandbox-server`(比如用了 pipx 装在别的位置),把 ExecStart 和 PATH 里的路径换成实际输出。

## 二、准备沙箱镜像

SecondLook 在沙箱里执行 `git` / `rg`(ripgrep)/ `python3` / `awk` / `find` 等命令。官方 `ubuntu` 镜像不含 `git` 和 `rg`,需构建自定义镜像。

### 2.1 一键构建(推荐)

仓库提供了 `scripts/build-sandbox-image.sh`,自动生成 Dockerfile、构建镜像、验证必要工具都在:

```bash
# 在服务器上,进入 SecondLook 仓库根目录
# 默认同时预装三款 CLI:Qoder + DeepSeek Harness CLI (dsh) + Codex(另有 --no-semgrep 可省略 Semgrep)
bash scripts/build-sandbox-image.sh

# 不装 Qoder
bash scripts/build-sandbox-image.sh --no-qoder-cli

# 不装 DeepSeek Harness CLI (dsh)
bash scripts/build-sandbox-image.sh --no-deepseek-cli

# 不装 Codex
bash scripts/build-sandbox-image.sh --no-codex-cli

# 仅基础工具(不装任何 CLI)
bash scripts/build-sandbox-image.sh --no-qoder-cli --no-deepseek-cli --no-codex-cli

# 服务器在国内时加 --cn-mirror 一键国内加速(避免 docker.io 拉取 ubuntu 超时)
bash scripts/build-sandbox-image.sh --cn-mirror

# 或仅换 Docker 基础镜像源(apt/npm 仍用官方源)
bash scripts/build-sandbox-image.sh --registry docker.m.daocloud.io
```

脚本会:
1. 检查 docker 可用(含 daemon 是否运行、当前用户是否在 docker 组)
2. 按参数生成 `Dockerfile.sandbox`(已存在且配置一致则跳过;配置变更会备份原文件后重新生成)
3. `docker build -t secondlook-sandbox:latest`
4. 逐个验证镜像内 `git` / `rg` / `python3` / `awk` / `find` / `curl` 及所选 CLI 都能找到

**国内镜像加速**(服务器在国内时推荐):`--cn-mirror` 一键启用三项国内源:
- Docker 基础镜像:`docker.m.daocloud.io/ubuntu:24.04`(DaoCloud 镜像,路径与 Docker Hub 一致)
- apt 源:阿里云 `mirrors.aliyun.com`(ubuntu 24.04 DEB822 格式 + 旧 sources.list 兼容)
- npm 源:`registry.npmmirror.com`(加速 qodercli/dsh/codex 的 `npm install -g`)

若只换 Docker 基础镜像源(apt/npm 保持官方),用 `--registry <prefix>`,如 `--registry docker.m.daocloud.io`。注意阿里云容器镜像服务需带 `library/` 前缀,如 `--registry registry.cn-hangzhou.aliyuncs.com/library`。镜像源变更会触发 Dockerfile 重新生成(检测 `# @registry:` 标记)。

三款 CLI 的差异:
- **Qoder CLI**(`qodercli`):npm 包,需 Node.js >= 20.0.0,账号在 qoder.com
- **DeepSeek Harness CLI**(`dsh`):npm 包,需 Node.js >= 20,DeepSeek 开源(https://github.com/deepseek-ai/deepseek-harness),凭证为 `DEEPSEEK_API_KEY`(+ 可选 `DEEPSEEK_BASE_URL` 自部署端点)
- **Codex CLI**(`codex`):npm 包,需 Node.js >= 16,OpenAI 官方,支持自定义 OpenAI 兼容端点

> Node 版本策略:qodercli 要求 >= 20.0.0,dsh 要求 >= 20,codex 要求 >= 16。只要 qoder_cli / deepseek_cli / codex_cli 任一启用,统一装 Node 22.x(三者都兼容)。

完成后在 SecondLook 的 `.env` 里设 `SANDBOX_IMAGE=secondlook-sandbox:latest`。

### 2.2 手动构建(了解脚本做了什么)

脚本生成的 `Dockerfile.sandbox` 内容(基础工具部分,各 CLI 的安装块见 2.3-2.5):

```dockerfile
FROM ubuntu:24.04

# 避免 tzdata 等交互式安装卡住
ENV DEBIAN_FRONTEND=noninteractive

# 基础工具:curl 用于 NodeSource 脚本(Node 类 CLI 的 Node.js 安装)
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        ripgrep \
        python3 \
        python3-pip \
        ca-certificates \
        openssh-client \
        coreutils \
        findutils \
        gawk \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3 /usr/bin/python

# 沙箱默认非 root 用户 user,确保 home 目录存在
RUN useradd -m -s /bin/bash user
USER user
WORKDIR /home/user
```

手动构建命令:

```bash
docker build -f Dockerfile.sandbox -t secondlook-sandbox:latest .
# 验证
docker run --rm secondlook-sandbox:latest rg --version
docker run --rm secondlook-sandbox:latest git --version
```

Server 直接用本地 Docker daemon,无需推到 registry。

### 2.3 可选:Qoder CLI 执行器依赖

SecondLook 支持 Qoder CLI 执行器(`task.executor=qoder_cli`):

| 执行器 | task.executor | CLI 命令 | 账号 | 依赖 | PAT 环境变量 |
|--------|---------------|----------|------|------|--------------|
| Qoder CLI | `qoder_cli` | `qodercli` | qoder.com | Node.js >= 20.0.0 + npm | `QODER_PERSONAL_ACCESS_TOKEN` |

[acp_bridge.py](../backend/app/agents/acp_bridge.py) 用 Python 标准库实现,需 **Python3**(2.2 的镜像已含)。安装方式二选一:

#### 方式 A:镜像预装(推荐,启动快、无网络依赖)

在切到 `user` 之前用 root 安装:

```dockerfile
# 装 Node.js 22.x(qodercli 要求 >= 20.0.0;dsh/codex 也兼容 Node 22,统一一个版本)
USER root
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# 全局安装 Qoder CLI(官方 npm 包 @qoder-ai/qodercli)
RUN npm install -g @qoder-ai/qodercli
```

构建后验证:

```bash
docker run --rm secondlook-sandbox:latest qodercli --version
```

#### 方式 B:运行时自动安装(首次启动慢,需沙箱能访问外网)

不在镜像里预装,让 [qoder_cli_agent.py](../backend/app/agents/qoder_cli_agent.py) 在首次启动时执行 `QODER_CLI_INSTALL_CMD` 安装。前提是镜像已装 Node.js >= 20.0.0(否则 npm 不存在)。对应 `.env` 配置(默认值已可用):

```bash
QODER_CLI_BIN=qodercli
QODER_CLI_INSTALL_CMD=npm install -g @qoder-ai/qodercli
```

> 注意:[BRIDGE_STARTUP_TIMEOUT 默认 30 秒](../backend/app/agents/qoder_cli_agent.py),首次自动安装可能超时。生产环境建议用方式 A 预装,避免每次任务都拉包。

### 2.4 可选:DeepSeek CLI(dsh)执行器依赖

SecondLook 还支持 DeepSeek 开源的 [DeepSeek Harness CLI](https://github.com/deepseek-ai/deepseek-harness)(简称 dsh)作为执行器(`task.executor=deepseek_cli`),原生支持 ACP 协议(`dsh --profile acp`),模型经 `session/set_config_option` 在 session/new 后设置。

| 执行器 | task.executor | CLI 命令 | 账号 | 依赖 | 凭证环境变量 |
|--------|---------------|----------|------|------|--------------|
| DeepSeek CLI | `deepseek_cli` | `dsh --profile acp` | DeepSeek 开放平台(或自部署端点) | Node.js >= 20 + npm | `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL`(可选) |

与 Qoder CLI 的关键差异:
- **ACP 启动命令**:`dsh --profile acp`(随附的 ACP profile,非 `--acp` 标志)
- **权限模式**:无 `--yolo` 启动参数,经 `DSH_PERMISSION_MODE` 环境变量注入(由 [deepseek_cli_agent.py](../backend/app/agents/deepseek_cli_agent.py) 自动完成):默认 `workspace-write`(危险命令发 `request_permission` 弹窗);`always_approve` 时注入 `danger-full-access` 跳过审批
- **模型选择**:不支持 `--model` CLI 参数,session/new 后经 `set_config_option` 设置(configId: `model` / `reasoning_effort`),模型如 `deepseek-v4-flash` / `deepseek-v4-pro`
- **凭证字段**:不是 PAT,而是 `api_key` + `base_url`(可选)两字段(用户在「智能体配置」填写)
- **Node 版本**:要求 >= 20(与 Qoder 的 20.0.0 对齐,统一装 Node 22.x)

安装方式二选一:

#### 方式 A:镜像预装(推荐,启动快、无网络依赖)

```dockerfile
# 装 Node.js 22.x(dsh 要求 >= 20;qodercli/codex 也兼容 Node 22,统一一个版本)
USER root
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# 全局安装 DeepSeek Harness CLI(官方 npm 包 @deepseek-ai/dsh,bin 名 dsh)
RUN npm install -g @deepseek-ai/dsh \
    && dsh --version
```

构建后验证:

```bash
docker run --rm secondlook-sandbox:latest dsh --version
docker run --rm secondlook-sandbox:latest node --version   # 应输出 v22.x
```

#### 方式 B:运行时自动安装(首次启动慢,需沙箱能访问外网)

不在镜像里预装,让 [deepseek_cli_agent.py](../backend/app/agents/deepseek_cli_agent.py) 在首次启动时执行 `DEEPSEEK_CLI_INSTALL_CMD` 安装。前提是镜像已含 Node.js >= 20。对应 `.env` 配置(默认值已可用):

```bash
DEEPSEEK_CLI_BIN=dsh
DEEPSEEK_CLI_INSTALL_CMD=npm install -g @deepseek-ai/dsh
```

> 注意:[BRIDGE_STARTUP_TIMEOUT 默认 30 秒](../backend/app/agents/acp_base.py),首次自动安装可能超时。生产环境建议用方式 A 预装。

#### 凭证配置

dsh 从 `DEEPSEEK_API_KEY` 环境变量读取凭证(与 e2e 测试同一套约定)。用户在「智能体配置」→ DeepSeek CLI 中填写两个字段,后端按 [registry.py](../backend/app/agents/registry.py) 的 `credential_env` 映射:

| 用户填写字段 | 注入的环境变量 | 必填 | 默认值 |
|--------------|----------------|------|--------|
| API Key | `DEEPSEEK_API_KEY` | 是 | — |
| API Base URL | `DEEPSEEK_BASE_URL` | 否 | 留空用 DeepSeek 官方端点(https://api.deepseek.com) |

两种典型场景:
- **DeepSeek 官方 API**:在 [platform.deepseek.com/api_keys](https://platform.deepseek.com/api_keys) 申请 API Key 填入,base_url 留空(用官方端点)
- **自部署 / 代理端点**(vLLM / 中转等 OpenAI 兼容端点):base_url 填完整 URL。沙箱需能访问该端点

### 2.5 可选:Codex CLI 执行器依赖

SecondLook 还支持 [OpenAI Codex CLI](https://github.com/openai/codex)(Apache-2.0 开源)作为执行器(`task.executor=codex_cli`)。与 Qoder/DeepSeek 不同,Codex **不原生支持 ACP 协议**,而是通过 [codex_bridge.py](../backend/app/agents/codex_bridge.py) 翻译 `codex exec --json` 的 JSONL 事件流为 ACP 协议。

| 执行器 | task.executor | CLI 命令 | 账号 | 依赖 | 凭证注入方式 |
|--------|---------------|----------|------|------|--------------|
| Codex | `codex_cli` | `codex exec --json`(经 codex_bridge.py 翻译为 ACP) | OpenAI 或任意 OpenAI 兼容端点 | Node.js >= 16 + npm | 环境变量(`CODEX_API_KEY`)+ config.toml(模型/provider/wire_api) |

与 Qoder/DeepSeek 的关键差异:
- **不原生支持 ACP**:Qoder 与 dsh(`dsh --profile acp`)都原生支持 ACP,Codex 没有,改用 `codex exec --json` 非交互模式输出 JSONL 事件,由 [codex_bridge.py](../backend/app/agents/codex_bridge.py) 翻译为 ACP 通知
- **多轮会话**:Codex 用 `codex exec resume <thread_id>` 恢复之前的会话(首次调用提取 thread_id,后续轮次复用),而非 ACP 的 `session/load`
- **配置文件**:模型/provider 配置写入 `~/.codex/config.toml`(TOML 格式),而非环境变量注入
- **审批策略**:`approval_policy = "full-auto"` 跳过所有审批(非交互模式必须)
- **沙箱模式**:`sandbox_mode = "danger-full-access"` 关闭 Codex 内部沙箱(我们用 OpenSandbox 隔离)
- **通信协议**:支持 `wire_api` 选择(Responses API / Chat Completions API),第三方端点推荐 `chat`
- **凭证字段**:`api_key` + `base_url`(可选)+ `model`(可选)+ `wire_api`(可选)四字段
- **运行时依赖**:Node.js >= 16(与 Qoder/DeepSeek 同属 Node 类 npm CLI)

安装方式二选一:

#### 方式 A:镜像预装(推荐,启动快、无网络依赖)

```dockerfile
# Codex CLI 是 Node.js 包,需先装 Node.js >= 16(与 qodercli / dsh 共享 Node 22.x 运行时)
# 见 2.3 / 2.4 的 Node.js 安装块,统一用 setup_22.x(codex 要求 >= 16,Node 22 兼容)
USER root
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# 全局安装 Codex CLI(官方 npm 包 @openai/codex,bin 名 codex)
RUN npm install -g @openai/codex \
    && codex --version
```

构建后验证:

```bash
docker run --rm secondlook-sandbox:latest codex --version
```

#### 方式 B:运行时自动安装(首次启动慢,需沙箱能访问外网)

不在镜像里预装,让 [codex_cli_agent.py](../backend/app/agents/codex_cli_agent.py) 在首次启动时执行 `CODEX_CLI_INSTALL_CMD` 安装。前提是镜像已含 Node.js >= 16。对应 `.env` 配置(默认值已可用):

```bash
CODEX_CLI_BIN=codex
CODEX_CLI_INSTALL_CMD=npm install -g @openai/codex
```

> 注意:[BRIDGE_STARTUP_TIMEOUT 默认 30 秒](../backend/app/agents/acp_base.py),首次自动安装 `@openai/codex` 可能超时。生产环境建议用方式 A 预装。

#### 凭证配置

Codex 从 `~/.codex/config.toml` 读取模型/provider 配置,API Key 经 `CODEX_API_KEY` 环境变量注入(config.toml 的 `env_key` 指向它)。用户在「智能体配置」→ Codex CLI 中填写四个字段,后端按 [registry.py](../backend/app/agents/registry.py) 的 `credential_fields` 动态渲染表单:

| 用户填写字段 | 注入方式 | 必填 | 默认值 |
|--------------|----------|------|--------|
| API Key | `CODEX_API_KEY` 环境变量 | 是 | — |
| API Base URL | config.toml `model_providers.secondlook.base_url` | 否 | 留空用 OpenAI 官方端点 |
| 模型名 | config.toml `model` | 否 | `gpt-5` |
| Wire API | config.toml `model_providers.secondlook.wire_api` | 否 | `responses`(Responses API) |

后端注入流程(由 [codex_cli_agent.py](../backend/app/agents/codex_cli_agent.py) 的 `pre_bridge_hook` 自动完成):
1. **`credential_env`**(registry 静态映射):`api_key` → `CODEX_API_KEY` 环境变量
2. **`pre_bridge_hook`**:向沙箱写入 `~/.codex/config.toml`,含:
   - `model`(模型名)
   - `approval_policy = "full-auto"`(跳过审批)
   - `sandbox_mode = "danger-full-access"`(关闭 Codex 内部沙箱)
   - 若填了 `base_url`:额外写 `[model_providers.secondlook]` 表(base_url + wire_api + env_key),并设 `model_provider = "secondlook"`
   - 若 `base_url` 留空:不写自定义 provider,Codex 用默认 OpenAI provider

`wire_api` 两种取值:
- `responses`(默认):OpenAI Responses API,Codex 0.81.0+ 默认,GPT-5/o 系列推荐
- `chat`:Chat Completions API,大多数第三方/本地模型支持(自部署端点推荐)

典型场景:
- **OpenAI 官方 API**:在 [platform.openai.com/api-keys](https://platform.openai.com/api-keys) 申请 API Key,base_url 留空,模型用 `gpt-5` 或 `o4-mini`,wire_api 用默认 `responses`
- **自部署 LLM 端点**(vLLM / Xinference / Ollama 等 OpenAI 兼容端点):三个字段都填,base_url 含 `/v1` 后缀,wire_api 选 `chat`(兼容性更好),沙箱需能访问该端点

## 三、配置 SSH Key(给沙箱用,可选)

沙箱里执行 `git clone git@github.com:...` 需要 SSH 凭证。如果你只用 HTTPS+token 方式 clone(后端 `clone_repo_with_fallback` 会优先用 token),可以跳过本节。

### 3.1 生成专用 SSH Key

```bash
ssh-keygen -t ed25519 -C "opensandbox@your-server" -f ~/.ssh/id_ed25519_opensandbox -N ""
cat ~/.ssh/id_ed25519_opensandbox.pub
```

### 3.2 添加到 GitHub

- 打开 https://github.com/settings/keys
- 点 "New SSH key",把上一步输出的公钥粘贴进去
- Title 随意,比如 `OpenSandbox Server`

### 3.3 配置 SSH 自动用这个 key

```bash
cat >> ~/.ssh/config <<EOF

Host github.com
    HostName github.com
    User git
    IdentityFile ~/.ssh/id_ed25519_opensandbox
    StrictHostKeyChecking no
EOF

chmod 600 ~/.ssh/config

# 测试
ssh -T git@github.com
# 看到 "Hi xxx! You've successfully authenticated" 即成功
```

### 3.4 让沙箱能读到 SSH Key(关键)

OpenSandbox 的 `[docker]` 段**没有** `volumes` 字段。挂载宿主机目录到沙箱的正确方式是:

1. **Server 端**:在 `~/.sandbox.toml` 的 `[storage].allowed_host_paths` 放行 SSH 目录所在路径前缀
2. **后端**:通过 SDK 的 `volumes` 参数挂载(已在 `sandbox/client.py` 实现)

修改 `~/.sandbox.toml`:

```toml
[storage]
# 放行 /home 前缀,允许挂载 ~/.ssh 到沙箱
allowed_host_paths = ["/home"]
```

重启 Server:

```bash
sudo systemctl restart opensandbox
```

在 SecondLook 后端的 `.env` 里设:

```bash
# 挂载宿主机 ~/.ssh 到沙箱 /home/user/.ssh(只读)
SANDBOX_SSH_KEY_HOST_PATH=~/.ssh
```

后端 `client.py` 会自动把这个路径作为只读 Volume 挂载到每个沙箱的 `/home/user/.ssh`。

### 3.5 仓库缓存挂载(可选,sandbox 模式加速)

SecondLook 支持同一仓库跨任务复用克隆(bare 仓库缓存 + `fetch --prune` 增量更新)。
sandbox 模式下,后端会把**本任务仓库自己的 bare 缓存子目录**以只读 Volume 挂载进容器,
任务克隆从挂载路径秒级完成,不再每次全量远程 clone。

安全设计:

- 只挂载本任务仓库的 bare 子目录(非缓存根),容器读不到其他用户/其他仓库
- 挂载只读(容器内不可写,无法污染缓存);bare 的 remote URL 是匿名形态,无 token 落盘
- 默认关闭,需显式开启

开启步骤(后端与 Server 须能访问同一份缓存目录):

1. **Server 端**:在 `~/.sandbox.toml` 的 `[storage].allowed_host_paths` 放行缓存目录
   **专用窄前缀**(建议专用目录,勿直接放行 `/home` 或 `/`):

```toml
[storage]
# SSH 目录与仓库缓存目录分别放行
allowed_host_paths = ["/home", "/data/secondlook/repo_cache"]
```

2. **把缓存目录放到 Server 可访问的路径**:后端与 Server 同机时,把后端
   `REPO_CACHE_DIR` 直接指向该目录即可;跨机部署时需通过 NFS/共享盘等方式
   让 Server 宿主机能读到同一份缓存(缓存由后端维护写入,Server 只需只读挂载)。

3. **后端** `.env`:

```bash
# sandbox 模式缓存总开关(默认关)
REPO_CACHE_SANDBOX_ENABLED=true
# 缓存在 Server 宿主机上的绝对路径(与 SANDBOX_SSH_KEY_HOST_PATH 同语义,
# 是 Server 机器路径,不是后端本地路径)
REPO_CACHE_SANDBOX_HOST_DIR=/data/secondlook/repo_cache
```

已知限制:LLM 运行中克隆**其他**仓库时(容器无法追加挂载),自动降级为全量远程
克隆(日志记 `[clone_fallback] 会话未挂载该仓库的 bare 缓存,走远程克隆`)。
缓存任何失败(目录未放行/磁盘/网络)一律降级原克隆链,不会阻塞任务。

## 四、后端连接配置

在 SecondLook 后端的 `backend/.env` 里配置:

```bash
# 切换到真实沙箱模式
SANDBOX_MODE=sandbox

# OpenSandbox Server 地址(远程服务器填 IP)
SANDBOX_SERVER_URL=http://your-server-ip:8080

# API Key(对应 Server 的 [server].api_key;Server 留空则这里也留空)
SANDBOX_API_KEY=

# 沙箱镜像(第二节构建的自定义镜像)
SANDBOX_IMAGE=secondlook-sandbox:latest

# 沙箱超时(分钟)
SANDBOX_TIMEOUT_MINUTES=30

# 可选:CLI(ACP)prompt 等长阻塞段的后台续期间隔(分钟,默认 5)
# 那段时间命令由 CLI 自己在沙箱里跑,不触发后端会话访问,只能靠 auto_renew 撑 TTL
# (普通访问路径的续期已并入探活:sandbox_tools._SANDBOX_PROBE_INTERVAL,默认 60s)
SANDBOX_RENEW_INTERVAL_MINUTES=5

# 可选:挂载宿主机 SSH key(第三节)
SANDBOX_SSH_KEY_HOST_PATH=~/.ssh

# 可选:资源限制
SANDBOX_CPU=2
SANDBOX_MEMORY=4Gi
```

确保后端安装了 OpenSandbox SDK:

```bash
cd backend
pip install opensandbox
```

重启后端:

```bash
uvicorn app.main:app --reload
```

## 五、验证

提交一个审计任务,看后端日志里是否出现 `[sandbox] git clone` 而不是 `[local]`:

```
[sandbox] git clone: git@github.com:xxx/xxx.git
[sandbox] search: rg --line-number ...
```

如果看到 `[local]`,说明 `SANDBOX_MODE` 没切到 `sandbox`。

## 六、常见问题

### 6.1 Server 启动卡住 / 无输出

**原因**:`[server].api_key` 留空且未设 `OPENSANDBOX_INSECURE_SERVER=YES`,非交互环境会等待 TTY 确认。

**解决**:要么在 `[server].api_key` 设一个值,要么设环境变量 `OPENSANDBOX_INSECURE_SERVER=YES`。

### 6.2 后端连不上 Server

**排查**:
1. 确认 `~/.sandbox.toml` 里 `[server].host = "0.0.0.0"`(模板默认 127.0.0.1,只能本机访问)
2. 确认防火墙放行 8080 端口:`sudo ufw allow 8080` 或 `firewall-cmd --add-port=8080/tcp`
3. 在后端机器上 `curl http://your-server-ip:8080/health` 验证连通性

### 6.3 沙箱里 git clone 失败:Permission denied (publickey)

**原因**:沙箱没读到 SSH key,或 key 没添加到 GitHub。

**排查**:
1. 确认 `.env` 里 `SANDBOX_SSH_KEY_HOST_PATH` 已设
2. 确认 Server 的 `[storage].allowed_host_paths` 放行了对应路径前缀
3. 后端日志看 clone 失败的 stderr

### 6.4 沙箱里 rg 命令不存在

**原因**:用了 `ubuntu` 官方镜像,没装 ripgrep。

**解决**:按第二节构建 `secondlook-sandbox:latest` 自定义镜像,并在 `.env` 设 `SANDBOX_IMAGE=secondlook-sandbox:latest`。

### 6.5 沙箱创建失败:image pull 超时

`ubuntu` / `secondlook-sandbox` 镜像在 Server 本地。若用了远程 registry 镜像,国内拉取可能慢:
- 配置 Docker 镜像加速器(阿里云 ACR 等)
- 或预先 `docker pull` 到本地

### 6.6 沙箱执行命令超时

`SANDBOX_TIMEOUT_MINUTES` 是整个沙箱的生命周期超时。单个命令超时在 `sandbox_tools.py` 里:
- `git clone`:120s
- 其他命令:60s(默认)
- `semgrep`:300s

如需调整,改 `sandbox_tools.py` 对应调用的 `timeout` 参数。

### 6.7 沙箱内存/CPU 不够

在 SecondLook 的 `.env` 配置:

```bash
SANDBOX_CPU=2
SANDBOX_MEMORY=4Gi
```

后端会通过 SDK 的 `resource` 参数传给 Server。注意:**不要**在 `~/.sandbox.toml` 的 `[docker]` 段找 `memory` / `cpus` 字段——官方配置没有这两项,资源限制只能通过 SDK 在创建沙箱时传入。

### 6.8 报错 `[DOCKER::SANDBOX_NOT_FOUND] Sandbox <uuid> not found`

含义:**Server 本身是活的**(它能回结构化错误 + `request_id`),但它手上已经没这个
沙箱实例了 —— 而后端内存里的会话还带着完好的 `repo_path`,于是每条命令都撞 404。

常见成因(按概率):1. **容器 TTL 到期被回收**(`SANDBOX_TIMEOUT_MINUTES`,默认 30min;
后端会话却保留 `WORKSPACE_TTL_AFTER_COMPLETE`,默认 24h,两个时限不一致);
2. **opensandbox.service 重启过**(内存里的沙箱注册表丢了);
3. **容器被 docker 侧抹掉**:`docker container prune` / 磁盘压力回收 / OOM 后被清
(第七节第 5 条的定期 prune 就属于这一类)/ 手动 `docker rm -f`;
4. 后端 `SANDBOX_SERVER_URL` 指向了另一台 Server(沙箱建在 A,请求打到 B)。

排查:比对后端日志里最后一条 `TTL 已续期 +N 分钟` 与报错时间(`+N` ≈ 报错时刻即 TTL
回收)、`systemctl status opensandbox` 的启动时间、`docker ps -a` 里容器是否还在。

系统现在的处理(无需人工干预):浏览端点会探活发现回收→丢弃会话并回 **410**,前端
展示"工作区已过期"并亮出「重新克隆」;会话复用时发现已回收会重建新容器。
想减少发生频率就调大 `SANDBOX_TIMEOUT_MINUTES`(活跃阅读会探活续期,闲置的不受影响)。

## 七、生产环境注意事项

1. **API Key 鉴权**:生产环境一定要给 Server 设 `[server].api_key`,否则任何人都能创建沙箱
2. **网络隔离**:Server 端口只对后端服务开放,不要暴露到公网
3. **资源配额**:用 `SANDBOX_CPU` / `SANDBOX_MEMORY` 限制单沙箱资源,防恶意消耗
4. **日志留存**:Server 日志要收集,便于排查沙箱执行问题
5. **定期清理**:沙箱意外退出可能留下 dangling 容器,定期 `docker container prune`。
   注意它会一并抹掉崩溃过(如 OOM)的沙箱容器,使 Server 回 `[DOCKER::SANDBOX_NOT_FOUND]`
   (见 6.8);跑着长任务时先 `docker ps` 确认再清

## 配置项对照表

| SecondLook `.env` | OpenSandbox Server 配置 | 说明 |
|---|---|---|
| `SANDBOX_SERVER_URL` | `[server].host` + `[server].port` | 后端解析出 `host:port` 传给 SDK 的 `domain` |
| `SANDBOX_API_KEY` | `[server].api_key` | 两边必须一致,或都留空 |
| `SANDBOX_IMAGE` | — | 沙箱容器镜像,Server 本地需存在 |
| `SANDBOX_SSH_KEY_HOST_PATH` | `[storage].allowed_host_paths` | 后端挂载,Server 放行路径前缀 |
| `REPO_CACHE_SANDBOX_ENABLED` + `REPO_CACHE_SANDBOX_HOST_DIR` | `[storage].allowed_host_paths` | 仓库缓存只读挂载(3.5 节),Server 放行缓存目录专用窄前缀 |
| `SANDBOX_CPU` / `SANDBOX_MEMORY` | — | 通过 SDK `resource` 参数传入 |

## 参考链接

- 官方仓库:https://github.com/opensandbox-group/OpenSandbox
- Server 配置参考:https://github.com/opensandbox-group/OpenSandbox/blob/main/server/configuration.md
- Python SDK 文档:https://open-sandbox.ai/sdks/python
- 安装指南:https://open-sandbox.ai/getting-started/installation
