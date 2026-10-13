"""应用配置加载"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局配置,从 .env 读取"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # 数据库
    DATABASE_URL: str = "postgresql+psycopg://localhost/secondlook"
    # 显式开启才会 drop_all + create_all 重建表,避免每次启动丢数据
    DB_REBUILD_ON_START: bool = False
    # 建立数据库连接的最长等待(秒):库不可达时快速失败,而不是阻塞到 OS 层 TCP
    # 超时(常见 60–130s)把整轮 pytest / 启动拖成"卡死无输出"。健康库连上远小于此
    # 值(localhost 亚毫秒、远端 RDS 通常 <1s),5s 是宽裕的护栏。测试基建(conftest)
    # 的同名探测也读这一值,保持口径一致。
    DB_CONNECT_TIMEOUT_SECONDS: int = 5

    # 应用
    APP_ENV: str = "development"
    APP_DEBUG: bool = True
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    # 日志级别(DEBUG/INFO/WARNING/ERROR),留空则按 APP_DEBUG 决定(DEBUG 时 DEBUG,否则 INFO)
    LOG_LEVEL: str = ""
    # 邮件链接的基础 URL(开发期指向前端 dev server 或后端)
    APP_BASE_URL: str = "http://localhost:5173"
    # 出题 & 练习功能总开关(false 时 /practice/* 路由不注册、任务完成不自动出题;
    # 已建表与题库数据保留,重新开启后可继续使用;用户级偏好 auto_generate_practice 在此开关之下)
    PRACTICE_ENABLED: bool = True

    # ---- 遗忘曲线调度器与组卷策略(详见 services/practice/{sm2,fsrs,selector}.py)----
    # 调度器分派:sm2=经典 SM-2(默认,行为与升级前一致);fsrs=FSRS-4.5
    # (公开默认参数,无需训练数据)。灰度:先跑 sm2 验证埋点/衰减链路,
    # 回归稳定后翻 fsrs。非法值回落 sm2。
    PRACTICE_SCHEDULER: str = "sm2"
    # FSRS 目标保留率(0-1):由 stability 反解下次复习间隔,越高排期越密。
    # 仅 PRACTICE_SCHEDULER=fsrs 时生效。
    PRACTICE_REQUEST_RETENTION: float = 0.9
    # 难度匹配高斯带宽 SIGMA(越小越严格贴合能力值)与组卷其余权重/阈值
    # 仍为 selector.py 内模块常量(集中可改);如需热调可后续上提为设置项。

    # JWT(阶段 6 用)
    JWT_SECRET: str = "change_me_in_production"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 1440

    # GitHub access_token 加密密钥(Fernet,32 字节 base64)
    # 留空则启动时自动生成(开发期方便,生产必须固定)
    GITHUB_TOKEN_SECRET: str = ""

    # GitHub OAuth(阶段 6 用,留空则 /auth/oauth/github 报错)
    GITHUB_OAUTH_CLIENT_ID: str = ""
    GITHUB_OAUTH_CLIENT_SECRET: str = ""
    GITHUB_OAUTH_REDIRECT_URI: str = "http://localhost:5173/auth/github/callback"

    # Gitee OAuth(留空则 /auth/oauth/gitee 与 /git/gitee/* 报错)
    # 在 https://gitee.com/oauth/applications 创建应用获取,回调地址用 /auth/gitee/callback
    GITEE_OAUTH_CLIENT_ID: str = ""
    GITEE_OAUTH_CLIENT_SECRET: str = ""
    GITEE_OAUTH_REDIRECT_URI: str = "http://localhost:5173/auth/gitee/callback"

    # 调用 Git 平台接口(GitHub/Gitee OAuth 与 API)传输层失败后的额外重试次数。
    # 本机实测到 github.com 偶发 TLS 握手被重置、RTT 2-5s,一次抖动就失败会把
    # 可恢复的网络问题报成用户可见错误。仅幂等请求(GET/撤销 token)重试全部传输层
    # 失败,单次有效的授权码换 token 只在"请求尚未发出"的连接失败上重试
    # (详见 app/git_provider.py request_json)。0=不重试。
    GIT_OAUTH_MAX_RETRIES: int = 2

    # 调用 Git 平台接口时走的代理,如 http://127.0.0.1:7890(Clash/v2rayN 的 HTTP 混合端口)。
    # 只作用于 GitHub/Gitee 的 OAuth 与 API 调用:挂全局 HTTPS_PROXY 会连带影响 LLM /
    # 沙箱 / ACP 的 httpx 客户端,而需要翻出去的往往只有 github.com 这一个域
    # (Gitee 是国内直连,走代理反而变慢)。
    # 留空则沿用系统 HTTP_PROXY/HTTPS_PROXY 环境变量(httpx trust_env 默认开启)。
    GIT_OAUTH_PROXY: str = ""

    # LLM(阶段 1:开发期单 provider 配置)
    LLM_PROVIDER: str = "dashscope"
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "qwen3.6-flash"
    LLM_ENABLE_THINKING: bool = True
    # 429 限流退避重试次数(首次失败后最多再重试的次数,0=不重试)
    # 退避策略见 app/llm/client.py:指数退避+抖动,厂商返回 Retry-After 时优先采用
    LLM_RATE_LIMIT_MAX_RETRIES: int = 3
    # ---- 出题并发上限(系统级天花板) ----
    # 用户可在练习设置里选 1/2/4 并行逐条 finding 出题,实际并发取
    # min(用户设置, 本项, 4)。厂商有「组内并发/RPM」上限时(部分厂商只允 1 并发),
    # 选高了只会多撞 429,因此运维可把本项降到 1 强制全局串行。
    PRACTICE_GENERATE_CONCURRENCY: int = 4
    # 同一厂商(按 baseUrl+model 归组)全局并发天花板,跨出题 job 共享:
    # 防两个 job 各自 4 并行把厂商配额扫光
    PRACTICE_PROVIDER_MAX_CONCURRENCY: int = 4

    # ---- 内置 react_agent 子任务按需派发(主循环内工具驱动扇出) ----
    # 执行器通过 dispatch_review_subtasks 工具**按需**把审查子任务扇出成 N 条有界只读
    # ReAct 子循环 → 主线程串行 fan-in 落库 → 各子任务结论作为 tool_result 回灌主循环
    # 继续集成。派发决策由模型自己做(先理解任务再决定派哪些子任务、各带什么技能子集与
    # 要求),而非编排器在循环开始前无差别全量扇出 —— 轻量输入("hi?")不再为多个子任务
    # 付成倍 LLM 调用。复用出题侧已验证的 provider 闸门 + ThreadPoolExecutor 形状,worker
    # 不碰共享 session/publish(只跑 LLM + 只读工具 + 内存缓冲),fan-in 在主线程逐条做。
    # 真优势:子任务带原生身份 label(ACP 侧不带身份导致并行 chunk 交织乱码)。
    # 默认开;关闭 = 不注册派发工具(纯串行主循环,而非回退全量扇出)。
    REACT_FACET_DISPATCH_ENABLED: bool = True
    # 子任务扇出线程数上限(实际取 min(本项, 本次派发子任务数))
    REACT_PARALLEL_MAX_CONCURRENCY: int = 4
    # 【已废弃】旧"注册派发工具所需最低可见技能数"闸门。子任务可纯 instruction、不再
    # 等同技能,可见技能数不再决定是否注册(现只看 REACT_FACET_DISPATCH_ENABLED,是否派发
    # 始终由模型决定)。键仅保留兼容既有 env,读取后不再参与注册判定。
    REACT_FACET_DISPATCH_MIN_FACETS: int = 2
    # 单次派发可调用的子任务数上限(0/留空 = 不额外限制)
    REACT_FACET_DISPATCH_MAX_FACETS: int = 0
    # 单轮(一次 run_react_agent)内派发调用次数上限:对标 Codex "同一未决线程
    # 避免重复派发",防反复扇出烧配额;超限直接报错让模型基于已有结论集成。
    REACT_FACET_DISPATCH_MAX_CALLS: int = 1
    # 单个子任务子循环的最大迭代次数(有界,防止某一子任务跑飞拖垮整轮)
    REACT_FACET_MAX_ITERATIONS: int = 12
    # 单个子任务子循环的墙钟预算(秒):循环每次迭代边界与收 chunk 时协作式检查,
    # 超时则转入本子任务总结收尾(部分结论)并隔离,不拖垮 fan-in 整轮
    REACT_FACET_TIMEOUT_SECONDS: int = 240
    # 单个子任务结论回灌 tool_result 前的 token 上限(CJK 感知粗估,见 _clip_to_tokens)。
    # 借鉴 codex 对子 agent 回灌消息设上限的做法:并行放大的是"份数",
    # 每份必须有界,否则父上下文会被 N 份长结论撑爆。
    REACT_FACET_SUMMARY_MAX_TOKENS: int = 3000
    # 审查链路同厂商(provider+model 归组)全局并发天花板,跨任务共享:
    # 置于子任务子循环的 LLM 调用外层,防并行放大把配额打爆(本次真凶是 403)
    REACT_PROVIDER_MAX_CONCURRENCY: int = 4

    # ---- 数据统一根 ----
    # 4 个运行时数据目录(克隆/缓存/skill/上传)统一收纳在 data/ 根下,
    # gitignore/备份/卷挂载只需覆盖一处;各子目录仍可独立用 env 重定位。
    # 旧布局(_repos / _repo_cache / user_skills / uploads_data 平铺在运行目录根)
    # 由 app/services/data_dirs.py 启动时自动迁移
    #
    # 仓库克隆临时目录
    REPO_CLONE_DIR: str = "./data/repos"
    # 仓库克隆深度:0=完整克隆(默认,保留 git 历史供 agent 追溯);>0=浅克隆 --depth N(超大仓库可设 1/50 加速)
    REPO_CLONE_DEPTH: int = 0
    # 仓库克隆超时(秒)。完整克隆比浅克隆慢,默认 600s;超大仓库可调大
    REPO_CLONE_TIMEOUT: int = 600

    # ---- 仓库缓存(bare cache) ----
    # 同一仓库跨任务复用:首次全量 clone --bare 入缓存,之后任务秒级本地克隆 +
    # 按需 fetch --prune 增量更新。任何缓存失败一律降级原克隆链,不阻塞任务
    # local 模式缓存开关(默认开;缓存目录不可用时自动降级)
    REPO_CACHE_ENABLED: bool = True
    # 缓存根目录(后端本机路径;Windows 建议短路径,注意 260 字符长路径限制)
    # 生产环境可指向独立可写 volume(如 /data/secondlook/repo_cache)
    REPO_CACHE_DIR: str = "./data/repo_cache"
    # 缓存新鲜度 TTL(秒):命中后距上次 fetch 未超此值直接复用,超了才增量 fetch
    REPO_CACHE_FETCH_TTL: int = 300
    # 缓存总大小上限(GB),超限按最久未用 LRU 淘汰(1h 内用过的不会被淘汰)
    REPO_CACHE_MAX_GB: float = 5.0
    # sandbox 模式缓存开关(默认关!需先在 OpenSandbox Server
    # [storage].allowed_host_paths 放行缓存目录前缀,并把 REPO_CACHE_DIR
    # 对应的 Server 宿主机路径填到 REPO_CACHE_SANDBOX_HOST_DIR)
    REPO_CACHE_SANDBOX_ENABLED: bool = False
    # 缓存在 Server 宿主机上的绝对路径(与 SANDBOX_SSH_KEY_HOST_PATH 同语义,
    # 跨机部署时后端本地 REPO_CACHE_DIR 与它指向同一份缓存)
    REPO_CACHE_SANDBOX_HOST_DIR: str = ""

    # ---- 工作区保留 ----
    # 任务完成后 session(含克隆的工作区)保留秒数,超时后惰性清理
    # (原硬编码 3600;教育场景默认放宽到 24h 便于当天回顾)
    # 这是**后端会话**的寿命,不等于容器还在:容器受 SANDBOX_TIMEOUT_MINUTES 约束。
    # 会话比容器活得久的那段窗口里,浏览端点会探活发现容器已回收 → 丢弃会话 +
    # 回 410 + 工作区信息回 available=false,前端据此引导「重新克隆」
    # (探活顺带续期,正在阅读的工作区不会突然过期)
    WORKSPACE_TTL_AFTER_COMPLETE: int = 86400

    # 用户上传 skill 存储目录(默认相对后端运行目录)
    # 生产环境可指向独立可写 volume(如 /data/secondlook/user_skills);
    # 内置 skill 始终在代码目录 backend/skills/,不经过此配置
    USER_SKILLS_DIR: str = "./data/user_skills"

    # 用户 skill 上传限制(单位 MB / 条,安全边界,详见 app/skills/uploader.py)
    # zip 本体上限(默认 50MB)
    SKILL_MAX_ZIP_SIZE_MB: int = 50
    # 解压后总大小上限(默认 200MB,为 50MB zip 预留约 4 倍解压空间,文本类内容压缩率高)
    SKILL_MAX_EXTRACT_SIZE_MB: int = 200
    # zip 内单文件上限(默认 20MB)
    SKILL_MAX_SINGLE_FILE_SIZE_MB: int = 20
    # 条目数上限(含附加资源,默认 100)
    SKILL_MAX_FILES: int = 100
    # 管理界面单文件内容读取预览上限(默认 20MB,与单文件上传上限对齐)
    SKILL_MAX_READ_SIZE_MB: int = 20
    # 管理界面文件列表条目数上限(防御异常目录,默认 200)
    SKILL_MAX_LISTED_FILES: int = 200
    # 附加资源额外允许的扩展名(逗号分隔,追加到内置白名单之上)
    # 内置白名单见 app/skills/uploader.py 的 _BASE_ALLOWED_EXTENSIONS;
    # 默认放行常见位图(png/jpg/jpeg/webp/gif,无可执行风险);
    # 注意:不建议追加 .svg(可内嵌脚本,有 XSS 风险)
    SKILL_ALLOWED_EXTENSIONS_EXTRA: str = ".png,.jpg,.jpeg,.webp,.gif"

    # 任务交付物上传目录(Stage 1 永久 staging:ZIP 解压存树 / 单文件原样存,带 meta.json;
    # 任务创建后长期保留,供失败重试 / 完成后追问 resume 复用)
    # 仅 STORAGE_BACKEND=local 时使用。默认相对路径便于开发;生产必须用 env 覆盖为绝对路径
    # (相对路径按进程 CWD 解析,存储位置随 uvicorn 启动目录漂移),并挂载持久卷
    UPLOADS_DIR: str = "./data/uploads"

    # ---- agent2 后台审查防跑飞(取证额度放开后的最后防线)----
    # 单次后台审查的最长挂钟时间(秒):只读取证额度 100 次可以让审查跑特别久,
    # 而它在 daemon 线程里无超时——真跑飞了只能用户手动「终止检查」。超时在工具循环
    # 边界按"已发射条目收尾"(保留已落库审查项,标 review_status=stopped)。
    # 设为 0 关闭硬时限(只靠用户终止与额度上限)
    AGENT2_REVIEW_MAX_SECONDS: int = 3600

    # ---- agent2 审查子智能体按需派发(工具驱动,主循环内扇出) ----
    # agent2 发射 submit_review_plan 后,**自行判断**是否调用 dispatch_review_subagents
    # 工具把选定的计划项扇出成 N 条有界只读 ReAct 子循环并行核实(每个子智能体 =
    # 一个计划项),而非"计划一发射就无差别自动扇出" —— 与 agent1 子任务按需派发同构。
    # 派发决策由模型自己做(先规划再决定派哪些项/是否全派/一个都不派),不派则按原
    # 串行流程自己逐项取证。形状复用 react_agent 子任务并行(worker 只跑 LLM + 只读
    # 工具 + 内存缓冲,不碰共享 db session/publish/证据台账;发射、落库、verify 全部
    # fan-in 后留主线程)。子智能体带原生身份(计划项 id),规避 ACP 侧不带身份导致
    # 的并行 chunk 交织乱码。默认开;仍受最低合格项数/厂商闸门(REACT_PROVIDER_MAX_
    # CONCURRENCY 同闸)约束。关闭 = 不注册派发工具(纯串行自核,而非回退自动扇出)。
    AGENT2_SUBAGENT_DISPATCH_ENABLED: bool = True
    # 单轮(一次 run_agent2)内派发调用次数上限:对标 Codex"同一未决线程避免重复
    # 派发",防反复扇出烧配额;超限直接回执 ok=false 让模型基于已有结论集成。
    AGENT2_SUBAGENT_DISPATCH_MAX_CALLS: int = 1
    # 计划项扇出线程数上限(实际取 min(本项, 本次派发合格项数))
    AGENT2_PARALLEL_MAX_CONCURRENCY: int = 4
    # **调用时**可派发的最低合格项数:请求过滤掉未知/verify 项后剩余 < 此数
    # → 整批拒绝(项少时子循环 bootstrap + 上下文复制成本高于串行收益,是工程
    # 事实,模型无法估算,故设硬下限;下界固定 2),回执提示"自行逐项核查"
    AGENT2_PARALLEL_MIN_ITEMS: int = 3
    # 单个计划项子循环的最大迭代次数(有界,防止某一项跑飞拖垮整轮 fan-in)
    AGENT2_PLAN_ITEM_MAX_ITERATIONS: int = 8
    # 单个计划项子循环的墙钟预算(秒):循环边界与收 chunk 时协作式检查,
    # 超时则转入本项总结收尾(部分结论)并隔离,不拖垮 fan-in 整轮
    AGENT2_PLAN_ITEM_TIMEOUT_SECONDS: int = 240
    # 单个计划项子循环的取证额度(读类与引用类各自计数):fan-in 后累计
    # 计入主循环总池(MAX_READ_TOOL_CALLS=100 / MAX_REFERENCE_CALLS=3)
    AGENT2_PLAN_ITEM_MAX_READ_CALLS: int = 25
    # 单项结论注入信封前的 token 上限(CJK 感知粗估,镜像
    # REACT_FACET_SUMMARY_MAX_TOKENS:并行放大的是"份数",每份必须有界)
    AGENT2_PLAN_ITEM_SUMMARY_MAX_TOKENS: int = 3000

    # ---- 动态验证(PoC verifier)额度与授权 ----
    # 单轮审查里调用动态验证的次数上限(下限固定 3,实际额度按审查计划里
    # 声明 planned_evidence=verify 的条目数联动)。每次验证内部最多 10 轮工具
    # 迭代且逐动作可能弹授权,所以要留一个硬上限防单轮审查被拖死。
    # 设为 0 = 回落到旧行为(固定 3 次)
    AGENT2_MAX_VERIFY_CALLS: int = 8
    # per_action 模式下等用户授权的最长秒数(0 = 无限等,旧行为——用户不点弹窗
    # 会把整轮后台审查挂死在工具执行内部,「终止检查」与上面的硬时限都叫不醒它)。
    # 超时按"未授权"处理:动作不执行,结论规规矩矩退回待核实。
    VERIFY_AUTH_TIMEOUT_SECONDS: int = 300

    # ---- 引用复核(check_reference,agent2 用)----
    # 场景命中且任务级 _agent_policy 未显式设置 allow_verify 时,自动开启
    # PoC 验证开关(实际跑 PoC 仍需任务配 test_env_url)。逗号分隔场景 id。
    # (原 code_security_audit 已并入 code_review,旧 id 经别名同样命中)
    VERIFY_DEFAULT_SCENARIOS: str = "code_review"
    # 引用复核抓取超时(秒,单跳 socket 级)
    REFERENCE_CHECK_TIMEOUT: int = 15
    # 引用复核响应体读取上限(字符,超出截断)
    REFERENCE_MAX_BODY_CHARS: int = 50000
    # 引用复核最大重定向跳数(每一跳都过 SSRF 校验)
    REFERENCE_MAX_REDIRECTS: int = 3
    # TIER1 权威域名(逗号分隔;条目可含路径前缀限定,如 github.com/advisories;
    # 纯域名为后缀匹配,带路径条目要求域名精确匹配且路径前缀命中)
    REFERENCE_TIER1_DOMAINS: str = (
        "github.com/advisories,nvd.nist.gov,cve.org,cve.mitre.org,"
        "cisa.gov,cert.org,us-cert.gov,kb.cert.org,owasp.org,"
        "docs.python.org,nodejs.org,php.net,kotlinlang.org,doc.rust-lang.org,"
        "go.dev,django.readthedocs.io,flask.palletsprojects.com,"
        "fastapi.tiangolo.com,expressjs.com,react.dev,vuejs.org,"
        "developer.mozilla.org,java.com,dev.java,openjdk.org,"
        "docs.oracle.com,spring.io,ruby-doc.org,docs.ruby-lang.org,"
        "dart.dev,flutter.dev,docs.gradle.org,maven.apache.org,"
        "nginx.org,httpd.apache.org,postgresql.org,dev.mysql.com,"
        "portswigger.net,snyk.io,gitlab.com/advisories"
    )
    # TIER2 可信域名(逗号分隔;github.com 整域在此,advisories 路径才算 TIER1)
    REFERENCE_TIER2_DOMAINS: str = (
        "github.com,wikipedia.org,stackoverflow.com,reddit.com,"
        "cloud.google.com,aws.amazon.com,azure.microsoft.com,"
        "learn.microsoft.com,docs.microsoft.com"
    )

    # 任务上传限制(安全边界,详见 app/services/uploads.py)
    # 单次上传体上限(zip 本体或单文件,默认 100MB)
    UPLOAD_MAX_FILE_MB: int = 100
    # zip 解压后总大小上限(默认 300MB,防解压炸弹)
    UPLOAD_MAX_EXTRACT_MB: int = 300
    # zip 内单文件上限(默认 50MB)
    UPLOAD_MAX_SINGLE_FILE_MB: int = 50
    # 解压后文件条目数上限(默认 2000)
    UPLOAD_MAX_FILES: int = 2000
    # 单次提交(任务创建 / 一条追问)可关联的上传文件个数上限
    UPLOAD_MAX_FILES_PER_MESSAGE: int = 10

    # ---- 工作区浏览 / 下载 ----
    # 单文件下载上限(MB)。二进制文件不进预览、只能下载,这条同时兜住
    # "沙箱里一个巨型构建产物被整份拉回后端"的内存/带宽风险;超限返回 413,
    # 提示用户回源仓库获取。上传回退路径为缓冲读,该值即单次响应的内存上限。
    WORKSPACE_DOWNLOAD_MAX_MB: int = 50

    # ---- 交付物存储后端(Stage 1 永久层)----
    # 部署级选择(一套部署一个后端):local=本地磁盘 UPLOADS_DIR / s3=S3 兼容对象存储
    # 详见 app/services/upload_storage.py
    STORAGE_BACKEND: str = "local"
    # S3 兼容对象存储(仅 STORAGE_BACKEND=s3 时读取;MinIO / 阿里云 OSS S3 兼容端点 / AWS S3)
    # 端点(自建/云上兼容端点,如 https://oss-cn-hangzhou.aliyuncs.com 或 http://minio:9000);
    # 留空则用 boto3 默认(AWS S3)
    S3_ENDPOINT_URL: str = ""
    S3_BUCKET: str = ""
    S3_REGION: str = ""
    S3_ACCESS_KEY_ID: str = ""
    S3_SECRET_ACCESS_KEY: str = ""
    # 对象 key 前缀(以 / 结尾;upload 对象存于 {prefix}{upload_id}/ 下)
    S3_PREFIX: str = "uploads/"

    # ---- 交付物保留 / GC ----
    # 是否启用后台保留清理任务(lifespan 启动的周期协程)
    UPLOAD_GC_ENABLED: bool = True
    # 保留天数:任务进入终态(completed/failed)且超过此天数,或孤儿上传超过此天数,才清理
    UPLOAD_RETENTION_DAYS: int = 30
    # GC 周期(小时)
    UPLOAD_GC_INTERVAL_HOURS: int = 24

    # 沙箱配置(阶段 2 起)
    # mode: local(本地模式,不用沙箱,在宿主机文件系统直接执行)/ sandbox(连真实 OpenSandbox Server)
    SANDBOX_MODE: str = "local"
    # OpenSandbox Server 地址,形如 http://your-server-ip:8080
    SANDBOX_SERVER_URL: str = "http://localhost:8080"
    # Server 鉴权 API Key(对应 server 配置 [server].api_key,留空则不鉴权)
    SANDBOX_API_KEY: str = ""
    # 沙箱镜像:必须预装 git / ripgrep(rg) / python3 / awk / coreutils
    # 官方 ubuntu 镜像不含 git 和 rg,需按 docs/opensandbox-deploy.md 构建自定义镜像
    SANDBOX_IMAGE: str = "ubuntu"
    # 沙箱超时(分钟):Server 到点就回收容器。与 WORKSPACE_TTL_AFTER_COMPLETE
    # (后端会话保留期)是两套时限,差量靠探活补齐,不要求两者对齐
    SANDBOX_TIMEOUT_MINUTES: int = 30
    # CLI(ACP)prompt 等长阻塞段的后台续期间隔(分钟):这段时间命令由 CLI 自己
    # 在沙箱里跑,不触发后端的会话访问,只能靠 auto_renew 线程撑住 TTL
    # (普通访问路径的续期已并入探活,节流见 sandbox_tools._SANDBOX_PROBE_INTERVAL)
    SANDBOX_RENEW_INTERVAL_MINUTES: int = 5
    # 是否走 Server 代理访问沙箱(跨机部署必须开;本机部署开了也能用)
    # True=所有沙箱请求经 Server 8080 端口转发,后端只需连 Server 一个端口
    # False=SDK 直连沙箱容器端口(需后端能访问 Server 的容器端口范围)
    SANDBOX_USE_SERVER_PROXY: bool = True
    # Server 宿主机上的 SSH key 目录(可选,只读挂载到沙箱 /home/user/.ssh 供 git clone SSH 协议用)
    # 这是 Server 机器上的路径,不是后端本地路径!必须用绝对路径(如 /home/admin/.ssh),不要用 ~
    # 留空不挂载;需在 server [storage].allowed_host_paths 放行该路径前缀
    SANDBOX_SSH_KEY_HOST_PATH: str = ""
    # 沙箱资源限制(可选,传给 SDK resource 参数,如 cpu="2" memory="4Gi")
    SANDBOX_CPU: str = ""
    SANDBOX_MEMORY: str = ""

    # ---- local 模式安全策略(路径权限 + 命令白名单) ----
    # local 模式下 .git 目录写保护(防 LLM 篡改 git 历史),对齐 TRAE 沙箱路径策略
    SANDBOX_LOCAL_PROTECT_GIT: bool = True
    # local 模式下额外的只读路径(逗号分隔,写操作拒绝,读操作允许)
    # 默认保护 .vscode / .trae / .idea 等编辑器配置目录
    SANDBOX_LOCAL_READONLY_PATHS: str = ".vscode,.trae,.idea"
    # local 模式下命令安全策略:
    #   safe: 安全命令前缀列表(逗号分隔,直接执行不拦截)
    #   dangerous: 危险命令正则列表(逗号分隔,匹配时推前端确认)
    #   其他命令: 执行但记录 INFO 日志
    SANDBOX_LOCAL_SAFE_COMMANDS: str = (
        "git status,git diff,git log,git show,git branch,git remote,"
        "ls,cat,head,tail,wc,grep,find,rg,fd,"
        "python,python3,pip,pip3,node,npm,npx,"
        "echo,printf,test,"
        "mkdir -p,touch,cp -r,mv"
    )
    # fork bomb 正则等,逗号在引号内作为分隔符
    SANDBOX_LOCAL_DANGEROUS_COMMANDS: str = (
        r"rm\s+-rf\s+/,"
        r"rm\s+-rf\s+~/,"
        r"rm\s+-rf\s+\*,"
        r"mkfs,dd\s+if=,"
        r":\(\)\{\s*:\|:\s*&\s*\};:,"
        r"curl\s+.*\|\s*(ba)?sh,"
        r"wget\s+.*\|\s*(ba)?sh,"
        r"chmod\s+777\s+/,"
        r"netcat|nc\s+-l,"
        r"sudo\s+,"
        r"shutdown|reboot|halt|poweroff"
    )
    # local 模式下是否启用平台原生隔离(macOS: sandbox-exec / Linux: bwrap)
    # True=检测到工具时自动包装命令(只读系统目录 + 读写工作区 + 禁外网)
    # False=不做原生隔离,仅靠路径策略 + 命令白名单(软隔离)
    # Windows 无原生沙箱,此配置项无效
    SANDBOX_LOCAL_NATIVE_ISOLATION: bool = True
    # local 模式是否允许外部 CLI 执行器(qoder_cli/deepseek_cli/codex_cli)
    # True=允许,bridge/CLI 直接跑在宿主机真实环境(无隔离,仅开发/调试)
    # False=维持禁止,CLI 执行器仅 sandbox 模式可用
    SANDBOX_LOCAL_ALLOW_CLI: bool = True

    # ACP CLI 挂死兜底(session/prompt 无数据 idle 超时,按事件状态分级):
    # - 无活动工具(等待模型输出/最终响应):超过 ACP_IDLE_TIMEOUT_OUTPUT_SECONDS
    #   无任何 data 事件 → 判挂死,cancel + 用已累积输出提前收尾本轮
    # - 有工具在跑(git clone/构建等长命令本就长时间无输出):用
    #   ACP_IDLE_TIMEOUT_TOOL_SECONDS 作最后防线(防 CLI 中途崩溃没发 completed)
    # 设为 0 关闭对应超时
    ACP_IDLE_TIMEOUT_OUTPUT_SECONDS: int = 300
    ACP_IDLE_TIMEOUT_TOOL_SECONDS: int = 1800

    # ---- ACP 冷启动提速(预热)----
    # 冷启动链路(acp_enter→prompt_send)实测 median ≈12.5s:环境准备 → 凭证注入 →
    # 起 bridge → 等就绪 → initialize 全程串行,而它前面的预克隆要 24~235s。
    # 开启后这些步骤在预克隆窗口里由后台线程做完,只剩 session/new(≈6s)留在克隆之后。
    # 预热失败/指纹不符一律静默回落全串行,只损失时间不影响正确性。
    # 背景与实测见 .trae/documents/CLI冷启动提速实施计划.md、app/agents/acp_prewarm.py
    ACP_PREWARM_ENABLED: bool = True
    # 允许预热的 agent 类型(逗号分隔)。只放无 wrapper 钩子的类型:
    # - qoder_cli:无 credential_env_builder / pre_bridge_hook,启动参数与 run_acp_agent 同源
    # - deepseek_cli / codex_cli 需要 per-task 的 env 构建或 bridge 前写配置文件,暂未纳入
    #   (deepseek 会因 envs 不一致被指纹自动作废;codex 由 consume 侧显式拒绝带钩子的预热)
    ACP_PREWARM_AGENTS: str = "qoder_cli"
    # 认领预热成果的最长等待(秒):从 handle 创建起算,扣掉已经跑的秒数
    # (不与 BRIDGE_STARTUP_TIMEOUT=30 叠加成两倍等待)
    ACP_PREWARM_TIMEOUT_SECONDS: int = 30
    # bridge 健康轮询间隔(秒):bridge 侧 HTTP 服务已不再无条件 sleep(1s)才监听,
    # 粒度收到 0.2s;设 0 则回落到 acp_base.BRIDGE_HEALTH_INTERVAL 默认值
    ACP_BRIDGE_HEALTH_INTERVAL_SECONDS: float = 0.2

    # ACP 异步子 Agent(后台派生)提前收尾的结果回收:
    # 部分 CLI(Qoder 的 Agent 工具)以 fire-and-forget 方式派生子 Agent,工具调用
    # 立刻回 "Async agent launched…background" 回执并被标 completed;模型常在子 Agent
    # 尚未回报时就结束本轮(end_turn),平台据此显示"已完成"但业务其实未完成。
    # 检测到该签名后,在同一活跃 ACP session 上带退避地补发续轮 prompt,让 CLI 把已
    # 完成的后台子 Agent 结果注入下一轮并产出完整报告(实测 qwen3.8-flash 可回收)。
    # 触发只看结构化事实:本轮存在"只拿到派生回执、没有后续回报"的 Agent 工具就补发。
    # 曾用收尾文本的口吻/篇幅做否决,实测会漏判:模型把"后台代理仍在运行"写在报告
    # 首句、末尾却是干净的结论,篇幅又远超实质阈值,半份报告因此被当"已交付"。
    # 文本判据只保留为循环内的停止条件与日志线索(见 ACP_ASYNC_AGENT_DELIVERED_MIN_CHARS)。
    ACP_ASYNC_AGENT_AUTOCONTINUE: bool = True
    # 生效的 agent 类型(逗号分隔);仅这些 executor 走异步 Agent 续轮回收
    # 同时决定"后台子任务结论落盘"注入段是否拼进 prompt(仅 sandbox 模式)
    ACP_ASYNC_AGENT_TYPES: str = "qoder_cli"
    # 单轮最多补发几次续轮 prompt(防子 Agent 迟迟不回报时无限循环)。
    # 3 → 2:补发一次的回报增量最有用,再补只会让模型把同一份报告重复堆进 summary
    ACP_ASYNC_AGENT_MAX_CONTINUE: int = 2
    # ---- 暂停真中断:恢复即自动续跑(CLI/ACP) ----
    # 用户点"暂停"后,CLI 本轮已被 session/cancel 真中断(半截结果)。
    # 开启时:点"恢复"→ 在同一活跃 ACP session 自动补发一轮"继续",让 agent1
    # 接着被中断的活往下做(符合用户对"恢复=继续"的直觉),直到某轮正常跑完
    # 才收尾进审查;关闭时退回旧行为(恢复即对半截结果收尾 + 审查)。
    # 仅 CLI/ACP 生效:中断标志 _round_paused_interrupt 只由 acp_base 写,
    # builtin react_agent 走原有"同轮冻结续跑",恒不触发此路径。
    ACP_AUTO_CONTINUE_ON_RESUME: bool = True
    # 单次"运行"内最多自动续跑几轮(防用户反复暂停→恢复时无限起新轮)。
    # 达上限后不补发,直接对最后一轮收尾;每轮续跑都需用户真实点一次恢复,
    # 正常不会触顶,此项仅为兜底护栏。
    ACP_AUTO_CONTINUE_MAX_ROUNDS: int = 8
    # 每次补发前等待秒数(给后台子 Agent 完成时间),按 factor 递增。
    # 20 → 30:实测子 Agent 完成多在 1~5 分钟窗口,20s 首探大概率空手而归白耗一次
    ACP_ASYNC_AGENT_CONTINUE_WAIT_SECONDS: int = 30
    ACP_ASYNC_AGENT_CONTINUE_BACKOFF_FACTOR: float = 1.5
    # 续轮回收的自设预算(秒):第 2 次及以后补发前检查已耗时,超了就收手并标注
    # "结果可能不完整"。注意它不是硬上界:已进入的 prompt 只能等它自己结束,真正的
    # 最坏耗时由上面的 idle 兜底决定(无活动工具 300s / 有工具在跑 1800s),因此
    # MAX_CONTINUE=2 时单轮回收最坏仍可能占用数十分钟。子 Agent 长期不回报请调小
    # MAX_CONTINUE 或关掉 AUTOCONTINUE。设为 0 = 该预算关闭
    ACP_ASYNC_AGENT_CONTINUE_DEADLINE_SECONDS: int = 300
    # 判定"续轮已产出实质报告"的字符阈值:补发拿到不少于该长度的文本就收手
    # (即便末尾还带"等待"字样)。再补发只会让模型把同一份报告重复输出堆进
    # summary,因此停止补发并保留"结果可能不完整"的标注。设为 0 = 该判据关闭
    ACP_ASYNC_AGENT_DELIVERED_MIN_CHARS: int = 1500
    # 后台子任务结论落盘:派生后台子 Agent 时要求每个子任务把完整结论写进
    # 工作区外的独立目录(见 acp_base._async_agent_reports_dir),回合结束前由后端
    # 回捞成 kind="agent_report" 的 artifact。价值有两层:① 沙箱 TTL 回收会让 CLI
    # 的会话磁盘一起消失,跨轮 session/resume 失败后未回报的结果物理不可恢复,
    # 落盘件活在 DB 里;② 下一轮回放可直接读它合并,不必重跑子任务
    ACP_ASYNC_AGENT_REPORTS_COLLECT: bool = True
    # 单份落盘报告回捞的字符上限(超出截断并在 metadata 标 truncated)
    ACP_ASYNC_AGENT_REPORTS_MAX_CHARS: int = 20000
    # 单轮最多回捞的报告文件数(按文件名序,超出只取前 N 并标注)
    ACP_ASYNC_AGENT_REPORTS_MAX_FILES: int = 8

    # 预防性禁止 CLI 派生子智能体:命中白名单的 executor 在首轮 prompt 就要求
    # "不要 fan-out,在主循环里串行完成各分面审查"。开启时同时抑制"后台子任务
    # 结论落盘"注入段并关闭续轮回收(见 acp_base._disallow_subagents_enabled /
    # _async_agent_autoccontinue_enabled)——三者互斥于"教子任务落盘"的旧链路。
    # 背景:ACP 事件不带子智能体身份,并行 chunk 交织成乱码,续轮回收又有截断误判
    # 的高危 bug,收益本就打折;止血优先让 CLI 别派生。默认关,灰度启用。
    ACP_DISALLOW_SUBAGENTS: bool = False
    # 生效的 agent 类型(逗号分隔);仅这些 executor 注入禁止 fan-out 段
    ACP_DISALLOW_SUBAGENT_TYPES: str = "qoder_cli"

    # Qoder CLI 配置(qoder_cli executor 用,国际版)
    # qodercli 可执行文件名/路径(沙箱内 PATH 查找或绝对路径)
    QODER_CLI_BIN: str = "qodercli"
    # qodercli 安装命令(沙箱内未检测到 qodercli 时执行,留空则不自动安装)
    QODER_CLI_INSTALL_CMD: str = "npm install -g @qoder-ai/qodercli"

    # DeepSeek Harness CLI 配置(deepseek_cli executor 用,
    # 开源 https://github.com/deepseek-ai/deepseek-harness)
    # dsh 可执行文件名/路径(沙箱内 PATH 查找或绝对路径)
    DEEPSEEK_CLI_BIN: str = "dsh"
    # dsh 安装命令(沙箱内未检测到 dsh 时执行,需沙箱镜像有 Node.js)
    # 推荐在镜像中预装,避免每次任务都拉 npm 包
    DEEPSEEK_CLI_INSTALL_CMD: str = "npm install -g @deepseek-ai/dsh"

    # Codex CLI 配置(codex_cli executor 用,开源 https://github.com/openai/codex,Apache-2.0)
    # codex 可执行文件名/路径(沙箱内 PATH 查找或绝对路径)
    CODEX_CLI_BIN: str = "codex"
    # codex 安装命令(沙箱内未检测到 codex 时执行,需沙箱镜像有 Node.js >= 16)
    CODEX_CLI_INSTALL_CMD: str = "npm install -g @openai/codex"


settings = Settings()
