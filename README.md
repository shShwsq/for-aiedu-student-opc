# SecondLook

> A dual-agent collaborative code analysis platform

**English** · [简体中文](./README.zh-CN.md)

SecondLook runs every task with two collaborating agents:

- **Agent 1 — Executor (shown as "AI Assistant" in the UI)**: performs the actual code analysis. Either the built-in LLM-driven `react_agent` or an external CLI executor (Qoder CLI / DeepSeek Harness CLI / Codex CLI).
- **Agent 2 — Inspector (shown as "Inspector" in the UI)**: a rigorous quality reviewer, verify-first and review-only (it never modifies code). It ① runs a single complete background review once Agent 1 finishes (verifying findings against real source code, defining its own review dimensions from the task description), ② runs PoC verifications when a test environment is configured (verification actions: `http_request` / `run_python_code`, with `per_action` / `direct` authorization modes), ③ spot-checks external references cited by Agent 1 (CVE / advisories / official docs — existence, source authority, SSRF-hardened fetching), ④ tags the most learning-worthy findings (`practice_worthy`) so practice generation prioritizes them, and ⑤ turns genuine gaps it cannot resolve itself into "suggested deep-dive directions" (suggestions) the user can trigger with one click.

Agent 1 starts executing directly on the user's task description; the task is marked complete as soon as Agent 1 finishes. Agent 2 then runs a single complete review in the background (reading code, running PoCs, checking references), producing "key points & knowledge" plus "suggested deep-dive directions" — it verifies on its own and never asks follow-up questions, turning genuine gaps into suggestions the user can trigger with one click ("deep dive"). The Settings page (a single shell with two-level navigation — account / model settings / CLI credentials / collaboration policy / practice settings) configures the inspector toggle, verification permissions, and more; disabling Agent 2 falls back to single-agent mode (no background review). Multiple rounds are user-driven (follow-up messages / clicking a suggestion).

## Key Features

- **Dual-agent collaboration**: Agent 1 executor (`react_agent` or an external CLI, shown as "AI Assistant") completes the task on finish, then Agent 2 inspector (verify-first background review, reference checking and learning-point tagging, shown as "Inspector") produces key knowledge points and suggested deep-dive directions; multiple rounds are user-driven (follow-up messages / clicking a suggestion), and disabling Agent 2 degrades to single-agent mode
- **Executor abstraction layer**: Pluggable executors — built-in react_agent / Qoder CLI / DeepSeek Harness CLI (dsh) / Codex CLI — unified via the ACP protocol
- **Deliverable uploads**: The task creation page offers three deliverable source tabs — Git repository / upload ZIP / upload single file. `upload_id` and `repo_url` are mutually exclusive; the task description is required in upload mode; ZIPs are protected against zip-slip with size limits. Uploading is optional — plain-text tasks work without one
- **Scenario templating**: General, code review, and document review as quick templates (preset prompts + recommended skills); the inspector defines its own review dimensions per task
- **Sandbox isolation**: Containerized execution based on [OpenSandbox](https://github.com/opensandbox/opensandbox); all tool calls run in an isolated environment
- **Multi Git platform**: Unified abstraction layer supporting GitHub / Gitee — OAuth login + private repo binding + automatic cloning (Gitee access tokens auto-refresh via refresh_token)
- **Practice questions & adaptive drills**: Turn real task findings into LLM-generated objective questions with SM-2 spaced repetition, weak-point reinforcement, and difficulty-matched session composition. The knowledge board groups points into collapsible learning-topic sections (built-in security / architecture / coding / contract plus user-defined topics), each card carrying a mastery badge; both topic-level ("practice this topic") and per-point focused drills are one click away. Learning topics form a user-managed vocabulary — built-ins can be disabled (stops only new questions, existing ones unaffected) and custom topics are driven by a user-supplied name + perspective description
- **Streaming output**: Reasoning, tool calls, and plan checklists pushed to the frontend in real time
- **Skill system**: Loadable expert SKILL.md directives, selectively enabled per task

## Tech Stack

| Layer | Technologies |
|---|---|
| Backend | FastAPI · SQLAlchemy 2.0 · PostgreSQL (psycopg3) · Pydantic v2 · OpenAI SDK |
| Frontend | Vue 3 · TypeScript · Vite · Pinia · Vue Router |
| Sandbox | OpenSandbox (containerized code execution environment) |
| Auth | JWT · OAuth 2.0 (GitHub / Gitee) · Fernet symmetric encryption (token storage) |
| LLM | OpenAI-protocol compatible (defaults to Alibaba Cloud DashScope / Tongyi Qianwen) |

## Project Structure

```
SecondLook/
├── backend/                  # FastAPI backend
│   ├── app/
│   │   ├── agents/           # Agents (agent1 react_agent / agent2 inspector / orchestrator / verifier / CLI wrappers)
│   │   ├── llm/              # LLM client wrappers
│   │   ├── models/           # SQLAlchemy data models (incl. practice / agent_policy / task_artifact)
│   │   ├── routers/          # API routes (auth / tasks / git_provider / uploads / practice / ...)
│   │   ├── sandbox/          # OpenSandbox client wrapper
│   │   ├── scenarios/        # Scenario templates (general / code review / document review)
│   │   ├── schemas/          # Pydantic request/response models
│   │   ├── services/         # Practice engine (SM-2 / selector / generator) + memory / workspace diff
│   │   ├── skills/           # Skill loader + skill registry
│   │   ├── tools/            # ReAct tools (clone_repo / search_code / run_lint / ...)
│   │   ├── agent_policy.py    # Collaboration policy (defaults + user/task-level merge)
│   │   ├── config.py         # Environment variable config (pydantic-settings)
│   │   ├── git_provider.py   # Git platform abstraction layer (GitHub / Gitee)
│   │   └── main.py           # FastAPI entry point
│   ├── skills/               # SKILL.md skill definition files
│   ├── requirements.txt
│   └── .env.example
├── frontend/                 # Vue 3 frontend
│   ├── src/
│   │   ├── api/              # API clients (incl. practice / practiceStream)
│   │   ├── components/       # Reusable components
│   │   ├── composables/      # Composables (theme / onboarding / feature flags)
│   │   ├── stores/           # Pinia state management
│   │   ├── views/            # Page views (incl. PracticeView)
│   │   └── types/            # TypeScript type definitions
│   ├── package.json
│   └── .env.example
├── docs/                     # Documentation (spec / Roadmap / sandbox deploy)
├── scripts/                  # Helper scripts (sandbox image build, etc.)
└── references/               # Reference materials
```

## Requirements

- **Python** ≥ 3.13 (dev environment uses 3.14)
- **Node.js** ≥ 22.0.0 (dev environment uses 24.x)
- **PostgreSQL** (Alibaba Cloud RDS or self-hosted)
- **OpenSandbox Server** (optional; when not deployed, set `SANDBOX_MODE=local` to use local mode (no sandbox))

## Quick Start

### 1. Clone the repository

```bash
git clone <repo-url> SecondLook
cd SecondLook
```

### 2. Backend setup & startup

```bash
cd backend

# Create a virtual environment (Python 3.13+)
python -m venv .venv
# Windows
.venv\Scripts\Activate.ps1
# Linux/macOS
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Copy the env template and fill it in
cp .env.example .env
# Edit .env — at minimum set DATABASE_URL / LLM_API_KEY / JWT_SECRET / GITHUB_TOKEN_SECRET

# Start the dev server (defaults to 0.0.0.0:8000)
uvicorn app.main:app --reload
```

Once running, visit `http://localhost:8000/docs` for the API docs (Swagger UI).

### 3. Frontend setup & startup

```bash
cd frontend

# Install dependencies
npm install

# Copy the env template and fill it in
cp .env.example .env
# Edit .env — fill in VITE_GITHUB_OAUTH_CLIENT_ID, etc.

# Start the dev server (defaults to http://localhost:5173)
npm run dev
```

The frontend proxies `/api/*` requests to the backend at `http://localhost:8000` via Vite (automatically stripping the `/api` prefix).

### 4. Open the app

Open `http://localhost:5173`, register an account (or log in via GitHub / Gitee OAuth), and start using it.

---

## Environment Variables

### Backend (`backend/.env`)

#### Database

| Variable | Description | Default |
|---|---|---|
| `DATABASE_URL` | PostgreSQL connection string, format `postgresql+psycopg://user:password@host:port/dbname` | **required** |
| `DB_REBUILD_ON_START` | Whether to drop_all + create_all on startup (wipes data; set `true` temporarily during schema changes) | `false` |

#### Application

| Variable | Description | Default |
|---|---|---|
| `APP_ENV` | Runtime environment (`development` / `production`) | `development` |
| `APP_DEBUG` | Debug mode | `true` |
| `APP_HOST` | Listen address | `0.0.0.0` |
| `APP_PORT` | Listen port | `8000` |
| `LOG_LEVEL` | Log level (empty → determined by `APP_DEBUG`) | empty |
| `APP_BASE_URL` | App base URL (used in email verification/reset links) | `http://localhost:5173` |
| `PRACTICE_ENABLED` | Master switch for the practice feature (`false` = `/practice/*` routes not registered, no auto-generation on task completion; existing data is kept) | `true` |

#### Auth & Encryption

| Variable | Description | Default |
|---|---|---|
| `JWT_SECRET` | JWT signing secret (**must change in production**) | `change_me_in_production` |
| `JWT_ALGORITHM` | JWT algorithm | `HS256` |
| `JWT_EXPIRE_MINUTES` | JWT expiry (minutes) | `1440` |
| `GITHUB_TOKEN_SECRET` | Git access_token encryption key (Fernet, 32-byte base64). **If empty, a random key is generated on every restart, making stored tokens undecryptable; must be fixed in production.** Generate with: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` | empty |

#### Git Platform OAuth

Both GitHub and Gitee are supported; configure as needed. Platforms left empty will return errors on their corresponding routes.

| Variable | Description |
|---|---|
| `GITHUB_OAUTH_CLIENT_ID` | GitHub OAuth App Client ID (create at https://github.com/settings/developers) |
| `GITHUB_OAUTH_CLIENT_SECRET` | GitHub OAuth App Client Secret |
| `GITHUB_OAUTH_REDIRECT_URI` | GitHub callback URL, defaults to `http://localhost:5173/auth/github/callback` |
| `GITEE_OAUTH_CLIENT_ID` | Gitee third-party app Client ID (create at https://gitee.com/oauth/applications; check `user_info` + `projects` scopes) |
| `GITEE_OAUTH_CLIENT_SECRET` | Gitee third-party app Client Secret |
| `GITEE_OAUTH_REDIRECT_URI` | Gitee callback URL, defaults to `http://localhost:5173/auth/gitee/callback` |
| `GIT_OAUTH_MAX_RETRIES` | Extra retries when the transport layer fails against the platform (unreachable / timeout / 5xx); `0` disables retries. Only requests that are safe to repeat are retried — a single-use authorization code is never re-sent |
| `GIT_OAUTH_PROXY` | Proxy for Git platform calls only (GitHub/Gitee OAuth + API), e.g. `http://127.0.0.1:7890`. Set it when the TLS handshake to github.com stalls; do **not** set a global `HTTPS_PROXY` — that would also reroute LLM / sandbox / ACP traffic. Empty falls back to the system proxy env vars |

> **Error code contract** (implementation: [backend/app/git_errors.py](backend/app/git_errors.py))
> - `400`: the platform clearly rejected the request — authorization code expired or already used (refreshing the callback page / navigating back onto a `?code=` URL is the usual cause), callback URL differing from the one used at authorize time, wrong credentials or insufficient scope. Gitee collapses all of these into one 401, so the detail now appends the platform's own reason text (credentials redacted) instead of requiring a manual curl repro
> - `401`: `/git/{provider}/repos` and `/refresh` — the platform considers the token dead, re-bind required
> - `409`: `/git/{provider}/bind` — that Git account is already bound to another user; on login, the matching e-mail account is already bound to a different platform account
> - `502` / `504`: cannot reach GitHub/Gitee, TLS handshake reset, read timeout, platform 5xx — unrelated to your authorization code or `.env`, just retry later
> Both classes log a backend `warning` with the root cause and `user_id`, so troubleshooting reads the console/log instead of guessing from an access-log status code.
> Logs are credential-redacted: Gitee passes `access_token` in the URL query and httpx logs the full URL at INFO, so values are filtered to `***` at the handler level (see [backend/app/log_redaction.py](backend/app/log_redaction.py)).

> **OAuth app configuration notes**
> - GitHub: set the Authorization callback URL to the value of `GITHUB_OAUTH_REDIRECT_URI`
> - Gitee: set the app callback URL to the value of `GITEE_OAUTH_REDIRECT_URI`; check at least `user_info` (login) + `projects` (repo binding) scopes
> - The same callback URL serves both "login" and "binding" scenarios, dispatched by current login state

**Gitee scope selection** (app creation page https://gitee.com/oauth/applications — request only what's needed; excessive scopes may cause users to deny authorization):

| Scope | Check? | Purpose |
|---|:---:|---|
| `user_info` | ✅ required | Login: fetch user ID / username / avatar / display name (matches `scope_login`) |
| `projects` | ✅ required | Repo binding: list private repos + HTTPS clone auth (matches `scope_bind`) |
| `emails` | ❌ skip | Gitee has no verified-email endpoint; the `email` field from `user_info` is enough |
| `pull_requests` / `issues` / `notes` | ❌ skip | This system only reads/clones code, not PRs / Issues / comments |
| `keys` / `hook` / `groups` / `gists` / `enterprises` | ❌ skip | Unused |

> Scopes map 1:1 to the code (`user_info` = `scope_login`, `user_info projects` = `scope_bind`) — see `GiteeProvider` in [backend/app/git_provider.py](backend/app/git_provider.py).

#### LLM Config (dev default provider)

| Variable | Description | Default |
|---|---|---|
| `LLM_PROVIDER` | Provider id, corresponds to `llmProviders[].id` in `models_catalog.json` | `dashscope` |
| `LLM_API_KEY` | LLM API Key (from the provider's console) | **required** |
| `LLM_MODEL` | Model id | `qwen3.6-flash` |
| `LLM_ENABLE_THINKING` | Enable thinking (toggleable for hybrid-thinking models) | `true` |
| `LLM_RATE_LIMIT_MAX_RETRIES` | Backoff retries after a 429 rate-limit error (exponential backoff + jitter; 0 = no retry) | `3` |

> In production / multi-user scenarios, LLM config is managed by users on the Settings → Models page (`/settings/models`); these env vars serve only as a dev fallback.

#### Repo Cloning

| Variable | Description | Default |
|---|---|---|
| `REPO_CLONE_DIR` | Local clone temp directory (used when `SANDBOX_MODE=local`) | `./data/repos` |
| `REPO_CLONE_DEPTH` | Clone depth: `0` = full clone (default, keeps git history for `git log`/`git blame`); `>0` = shallow clone `--depth N` (speed up huge repos) | `0` |
| `REPO_CLONE_TIMEOUT` | Clone timeout in seconds (full clone is slower than shallow; raise for huge repos) | `600` |

#### Task Deliverable Uploads

Task creation offers three deliverable source tabs: Git repository / upload ZIP / upload single file. `upload_id` and `repo_url` are mutually exclusive (providing both returns 422); in upload mode the task description (`user_input`) is required. ZIP archives are checked against zip-slip, per-file, total-size, and file-count limits; uploads are kept on the server for task retries and post-completion resume.

| Variable | Description | Default |
|---|---|---|
| `UPLOADS_DIR` | Storage directory for task deliverable uploads | `./data/uploads` |
| `UPLOAD_MAX_FILE_MB` | Max upload size (MB) | `100` |
| `UPLOAD_MAX_EXTRACT_MB` | Max total extracted size for a ZIP (MB) | `300` |
| `UPLOAD_MAX_SINGLE_FILE_MB` | Max single file inside a ZIP (MB) | `50` |
| `UPLOAD_MAX_FILES` | Max file count inside a ZIP | `2000` |
| `WORKSPACE_DOWNLOAD_MAX_MB` | Max size of a single workspace/uploaded file served through the download endpoint (binary files are not previewed, only downloaded; over the limit → 413) | `50` |

#### Sandbox (OpenSandbox)

| Variable | Description | Default |
|---|---|---|
| `SANDBOX_MODE` | `local` (no sandbox, host filesystem) / `sandbox` (real OpenSandbox Server) | `local` |
| `SANDBOX_SERVER_URL` | OpenSandbox Server URL, e.g. `http://your-server:8080` | `http://localhost:8080` |
| `SANDBOX_API_KEY` | Server auth API Key (corresponds to server `[server].api_key`; empty = no auth) | empty |
| `SANDBOX_IMAGE` | Sandbox image (must have git / ripgrep / python3 / awk / coreutils preinstalled) | `ubuntu` |
| `SANDBOX_TIMEOUT_MINUTES` | Sandbox timeout (minutes) | `30` |
| `SANDBOX_RENEW_INTERVAL_MINUTES` | Session TTL renew interval: renews when a session was last accessed more than this long ago — prevents long tasks from being recycled by the Server | `5` |
| `SANDBOX_USE_SERVER_PROXY` | Whether to go through the Server proxy (`true` required for cross-machine deployment) | `true` |
| `SANDBOX_SSH_KEY_HOST_PATH` | SSH key directory on the Server host (read-only mounted into the sandbox for SSH cloning; absolute path) | empty |
| `SANDBOX_CPU` | Sandbox CPU limit (e.g. `2`) | empty |
| `SANDBOX_MEMORY` | Sandbox memory limit (e.g. `4Gi`) | empty |

> The official `ubuntu` image lacks git and ripgrep — build a custom image per [docs/opensandbox-deploy.md](docs/opensandbox-deploy.md), or use `scripts/build-sandbox-image.sh`.

#### CLI Executors (optional, effective per `task.executor`)

Each CLI executor has two config values: binary name/path + install command (auto-installed in the sandbox when not detected).

**Hang protection** (applies to all CLI executors):

| Variable | Description | Default |
|---|---|---|
| `ACP_IDLE_TIMEOUT_OUTPUT_SECONDS` | Idle timeout with no active tool (waiting for model output); on expiry the session is cancelled and the round wraps up with accumulated output (`0` = off) | `300` |
| `ACP_IDLE_TIMEOUT_TOOL_SECONDS` | Last-resort idle timeout while a tool is running (git clone / long builds produce no output) — guards against CLI crashes without a `completed` event (`0` = off) | `1800` |

**Async sub-agent recovery** (signature comes from Qoder CLI):

Qoder's `Agent` tool is fire-and-forget: the tool call immediately returns an "Async agent launched…background" ack and the model often ends the turn (`end_turn`) before the sub-agents report back — the platform then shows "completed" while the work isn't done. When a round launched background sub-agents and its closing text hasn't delivered substantive results yet, the backend re-sends a follow-up prompt on the same live ACP session with backoff so the CLI injects the finished sub-agent results into the next turn. If a follow-up is truncated by hang protection, ends with a `stopReason` other than `end_turn`, or exhausts its attempts, the summary and the UI are annotated "results may be incomplete" instead of falsely claiming completion.

| Variable | Description | Default |
|---|---|---|
| `ACP_ASYNC_AGENT_AUTOCONTINUE` | Master switch (`false` = never continue) | `true` |
| `ACP_ASYNC_AGENT_TYPES` | Executors this applies to (comma-separated) | `qoder_cli` |
| `ACP_ASYNC_AGENT_MAX_CONTINUE` | Max follow-up prompts per round (`0` = none). Each follow-up is additionally bounded by the idle timeouts above, so the worst case is tens of minutes | `3` |
| `ACP_ASYNC_AGENT_CONTINUE_WAIT_SECONDS` | Wait before each follow-up (gives sub-agents time to finish) | `20` |
| `ACP_ASYNC_AGENT_CONTINUE_BACKOFF_FACTOR` | Growth factor for that wait | `1.5` |
| `ACP_ASYNC_AGENT_DELIVERED_MIN_CHARS` | Threshold for "a substantive report arrived": stop re-asking once the follow-up output is at least this long (asking again only stacks duplicate reports into the summary); `0` = disable this criterion | `1500` |

**Qoder CLI** — `task.executor=qoder_cli`

| Variable | Default |
|---|---|
| `QODER_CLI_BIN` | `qodercli` |
| `QODER_CLI_INSTALL_CMD` | `npm install -g @qoder-ai/qodercli` |

**DeepSeek CLI (dsh)** — `task.executor=deepseek_cli`

| Variable | Default |
|---|---|
| `DEEPSEEK_CLI_BIN` | `dsh` |
| `DEEPSEEK_CLI_INSTALL_CMD` | `npm install -g @deepseek-ai/dsh` |

> DeepSeek Harness CLI (short name `dsh`, open source: <https://github.com/deepseek-ai/deepseek-harness>). `dsh --profile acp` starts its bundled stdio ACP service, so it reuses the common `acp_bridge` natively. Credentials (`api_key` + optional `base_url`) are filled in on the "Agent Settings" page and injected via the `DEEPSEEK_API_KEY` (+ optional `DEEPSEEK_BASE_URL`) env vars. Model / reasoning effort are set at runtime via ACP `session/set_config_option` (configId: `model` / `reasoning_effort`; models such as `deepseek-v4-flash` / `deepseek-v4-pro`). Permission mode is controlled by the `DSH_PERMISSION_MODE` env var: default `workspace-write` — dangerous commands raise a `request_permission` prompt; in `always_approve` mode `danger-full-access` is injected to skip prompts. The npm package `@deepseek-ai/dsh` requires Node.js >= 20 (the sandbox image ships Node 22.x).

**Codex CLI** — `task.executor=codex_cli`

| Variable | Default |
|---|---|
| `CODEX_CLI_BIN` | `codex` |
| `CODEX_CLI_INSTALL_CMD` | `npm install -g @openai/codex` |

> Codex CLI is OpenAI's official coding CLI (Apache-2.0, requires Node.js >= 16). Unlike Qoder/DeepSeek which support ACP natively, Codex uses `codex exec --json` (JSONL event stream) translated to ACP by `codex_bridge.py`. Credentials (`api_key` / `base_url` / `model` / `wire_api`) are filled in on the "Agent Settings" page. The backend injects the API key via the `CODEX_API_KEY` env var (static `credential_env` mapping), and writes `~/.codex/config.toml` (model/provider/base_url/wire_api + `approval_policy=full-auto` + `sandbox_mode=danger-full-access`) via `pre_bridge_hook`. Defaults: model `gpt-5`, wire_api `responses` (Responses API). Supports custom OpenAI-compatible endpoints (vLLM / Ollama etc., use `wire_api=chat`).

### Frontend (`frontend/.env`)

The frontend stores only the Client ID (no secret), used to build OAuth authorization URLs.

| Variable | Description |
|---|---|
| `VITE_GITHUB_OAUTH_CLIENT_ID` | GitHub OAuth Client ID |
| `VITE_GITHUB_OAUTH_REDIRECT_URI` | GitHub callback URL (must match backend `GITHUB_OAUTH_REDIRECT_URI`) |
| `VITE_GITEE_OAUTH_CLIENT_ID` | Gitee OAuth Client ID |
| `VITE_GITEE_OAUTH_REDIRECT_URI` | Gitee callback URL (must match backend `GITEE_OAUTH_REDIRECT_URI`) |

---

## Sandbox Deployment

Production requires deploying an OpenSandbox Server to provide an isolated code execution environment. Full steps are in [docs/opensandbox-deploy.md](docs/opensandbox-deploy.md); key points:

1. Deploy OpenSandbox Server on a Linux server, listening on `0.0.0.0:8080`
2. Explicitly configure `[runtime].execd_image`, and allow mount path prefixes in `[storage].allowed_host_paths`
3. Build a custom image (with git / ripgrep / python3 / Node.js preinstalled) to avoid reinstalling on every task
4. Set `SANDBOX_MODE=sandbox`, `SANDBOX_SERVER_URL`, and `SANDBOX_USE_SERVER_PROXY=true` on the backend

When the sandbox is not deployed, set `SANDBOX_MODE=local` to use local mode (no sandbox) (dev/debug only — tool calls execute on the host machine).

## Build & Deployment

### Frontend build

```bash
cd frontend
npm run build      # vue-tsc type check + vite build, output in dist/
npm run type-check # type check only
```

### Backend run

```bash
cd backend
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

For production, consider `gunicorn -k uvicorn.workers.UvicornWorker` (Linux) or containerized deployment.

> **Note**: The backend holds in-process state (practice generation jobs, task SSE event streams). **Do not run multiple workers**; use `--workers 1` with gunicorn, or the Docker deployment below.

### Docker one-click deployment (recommended)

Use the Docker Compose setup under `deploy/`: 2 containers (`backend` + `frontend/nginx`). External dependencies (Alibaba Cloud RDS PostgreSQL, OpenSandbox on another server) stay outside the compose stack and are connected via environment variables.

```bash
# On a Linux server (Docker + Compose plugin required)
git clone <repo-url> SecondLook && cd SecondLook/deploy
cp .env.production.example .env.production   # fill in DB / secrets / sandbox URL / OAuth
bash deploy.sh                               # build + start
```

Key points:

- **Backend is forced to a single worker** (uvicorn `--workers 1`): practice jobs and task SSE streams are in-process state; multiple workers break event delivery
- **nginx disables `proxy_buffering`**: task/practice streams are realtime SSE, buffering freezes the frontend
- **`/api` prefix is stripped when proxying**: frontend baseURL is `/api`, nginx proxies to the backend without the prefix
- **Persistent volumes**: `backend/logs` (practice_generate.log / perf.log / acp), `backend/data` (unified data root: uploads / user skills / local clones / repo cache; legacy `uploads_data`, `user_skills`, `_repos` layouts are migrated into it automatically at startup)
- After changing `VITE_*` variables (e.g. OAuth callbacks), re-run `bash deploy.sh` (build-time injection)

See [deploy/.env.production.example](deploy/.env.production.example) for per-variable comments.

#### Upgrading to the unified data root (legacy Docker deployments only)

Since Oct 2026 runtime data (uploaded deliverables / user skills / local clones / repo cache) lives under a single `backend/data/` volume. The legacy `uploads_data` / `user_skills` / `backend_repos` volumes are no longer mounted; migrate them manually before upgrading (named volume data is never moved automatically):

```bash
cd deploy && docker compose down
# 1) Remove UPLOADS_DIR=.../uploads_data from .env.production (fall back to the default inside the data volume)
# 2) Start once so the backend_data volume is created and inherits ownership from the image, then stop
docker compose up -d backend && sleep 10 && docker compose down
# 3) Copy old volume data into the new volume (volume names carry the secondlook_ prefix;
#    cp -a preserves uid 1000 ownership; _repos local clones are temp data, safe to skip)
docker run --rm -v secondlook_uploads_data:/old -v secondlook_backend_data:/new alpine \
  sh -c 'cp -a /old/. /new/uploads/'
docker run --rm -v secondlook_user_skills:/old -v secondlook_backend_data:/new alpine \
  sh -c 'cp -a /old/. /new/user_skills/'
docker compose up -d
```

> Non-Docker dev environments need no action: at startup the backend moves legacy layout directories under the working directory into `data/` automatically; any location explicitly configured via `UPLOADS_DIR` (and friends) is always respected and never touched.

## Documentation

- [Specification](docs/spec.md) — Full product spec and architecture design (in Chinese)
- [Roadmap](docs/Roadmap.md) — Phased plan and progress (in Chinese)
- [Sandbox Deployment Guide](docs/opensandbox-deploy.md) — OpenSandbox Server deployment and image build (in Chinese)
- [Agent Architecture](docs/agent-architecture.md) — agent1 (executor) / agent2 (inspector) / CLI agent internals, context passing, collaboration policy (in Chinese)
- [Task Detail View Structure](docs/task-detail-view-structure.md) — TaskDetailView layout and rendering pipeline (in Chinese)

## Development Notes

- **Database schema changes**: Temporarily set `DB_REBUILD_ON_START=true` and restart to rebuild tables (wipes data); remember to set it back to `false`. Production should use Alembic migrations.
- **Add a scenario template**: Register it under `backend/app/scenarios/`, providing `preset_prompt` and `recommended_skills`.
- **Add a skill**: Drop a `SKILL.md` under `backend/skills/<category>/<name>/`; it's auto-scanned and loaded on startup.
- **Add a Git platform**: Add an implementation class to the `PROVIDERS` registry in `git_provider.py`; routes and UI are already parameterized.
- **Add a CLI executor**: Register an `agent_type` in the `backend/app/agents/` registry, implementing ACP protocol communication.
- **Practice feature**: `PRACTICE_ENABLED=false` disables `/practice/*` routes and auto-generation; question-generation logs are written to `backend/logs/practice_generate.log`.

## License

[MIT License](./LICENSE) © 2026 shShwsq
