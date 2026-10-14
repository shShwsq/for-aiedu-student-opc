---
name: review_concurrency
description: 审查并发与线程安全问题,覆盖竞态条件、共享可变状态、锁误用、异步阻塞调用等模式。Invoke when 审查多线程/多进程/异步代码,或发现全局可变变量、缓存、单例被并发访问时。
---

# 并发与线程安全审查

## 适用场景

仓库含多线程 / 多进程 / 异步(async/await)代码,或存在被并发访问的共享状态
(全局缓存、单例、模块级字典/列表、数据库连接池)时应执行本技能。
重点语言:Python、Node.js、Java、Go。

## 执行步骤

### 1. 定位共享可变状态

调用 `search_code`(建议 context_lines=3-5;示例省略必填参数 `repo_path`,实际调用须带上任务工作区根路径——仓库任务为 `clone_repo` 返回值,上传任务由系统注入工作区、见任务上下文):

```
search_code(pattern="^_?[a-z_]+\\s*[:=]\\s*(\\{|\\[|dict\\(|list\\(|set\\()", file_glob="*.py")  # 模块级可变容器
search_code(pattern="global\\s+\\w+", file_glob="*.py")                # global 声明=跨函数共享
search_code(pattern="threading\\.(Thread|Lock|RLock)|multiprocessing", file_glob="*.py")
search_code(pattern="new\\s+Thread|ExecutorService|synchronized", file_glob="*.java")
search_code(pattern="go\\s+func|sync\\.(Mutex|WaitGroup|RWMutex)|chan\\s", file_glob="*.go")
```

对每个共享状态确认三件事(`read_file` 看完整使用链):
1. **谁在写**:有几处代码修改它
2. **谁在读**:读的时候有没有其他线程在写
3. **有没有同步**:锁 / 原子操作 / 消息传递(channel)

三者构成"多写或读写并发 + 无同步"→ 竞态成立。

### 2. 检查锁的正确性

```
search_code(pattern="\\.acquire\\s*\\(|with\\s+\\w*lock|\\.lock\\s*\\(", file_glob="*.py")
search_code(pattern="\\.Lock\\s*\\(|defer\\s+\\w+\\.Unlock", file_glob="*.go")
```

常见错误形态:
- **acquire 后提前 return / 抛异常未 release**(Python 应用 `with lock:` 而不是裸 acquire)
- **锁粒度错误**:锁住的是读取,写入却不在锁内;或锁了 A 数据却改 B 数据
- **双重检查缺陷**:先无锁判空再拿锁创建,两个线程同时过第一道检查 → 创建两次
- **死锁风险**:同函数内按不同顺序获取两把锁(A→B 与 B→A 并存)
- Go:复制含 Mutex 的结构体(按值传参)、defer Unlock 前 panic 路径

### 3. 检查"读-改-写"复合操作

即使单个操作线程安全,复合操作也可能竞态。搜这些形态后逐一确认:

```
search_code(pattern="if\\s+\\w+\\s+not\\s+in\\s+\\w+:", file_glob="*.py")   # 判断后插入(check-then-act)
search_code(pattern="\\+=\\s*1|counter|count\\s*\\+=", file_glob="*.py")    # 计数自增非原子
search_code(pattern="\\.get\\(.*\\)\\s*\\|\\||if\\s*!", file_glob="*.java")  # get 后 put 模式
```

Python 的 `d[k] = d.get(k, 0) + 1`、Java 的 HashMap 并发读写(应换 ConcurrentHashMap)、
Node.js 单线程模型下少见但要警惕 worker_threads / 多实例部署共享内存假设。

### 4. 检查异步代码误用(async 项目)

```
search_code(pattern="async\\s+def|async\\s+function", file_glob="*.py")
search_code(pattern="time\\.sleep|requests\\.(get|post)", file_glob="*.py")   # 协程里的阻塞调用
```

- async 函数里调 `time.sleep` / `requests` / 同步文件 IO → 阻塞整个事件循环
- 共享状态在 `await` 前后被读改写:await 期间其他协程可能改了它(await 点即切换点)
- 未 await 的协程(`create_task` 后丢引用)→ 任务被 GC、异常丢失

### 5. 输出结果

每个问题在最终总结里给出:
- **位置**:文件:行号
- **类别**:竞态条件 / 锁误用 / 复合操作非原子 / 异步阻塞 / 死锁风险
- **严重度**:high(数据损坏/服务卡死)、medium(低概率出错)、low(理论风险)
- **证据**:涉及的共享状态 + 并发访问路径
- **建议**:具体修法(加锁粒度 / 换并发容器 / 改消息传递 / 用 asyncio 版客户端)

## 避免误报

- 只在单线程启动路径(initialization)写入的全局量,运行时只读 → 安全
- 不可变对象(tuple、frozen dataclass、常量)共享 → 安全
- Node.js 主线程内的普通变量不存在线程竞态(除非 worker_threads)
- 测试代码里的并发问题降级为 low

## 避免漏报

- 框架隐式并发最易漏:Web 框架(FastAPI 同步路由跑在线程池、Flask/Django 多 worker)、
  定时任务与请求并发访问同一缓存
- 多进程部署(gunicorn -w N)下"内存里加个 dict 缓存"天然不一致,要指出
- 别只搜 lock 关键字,很多代码根本没写锁——"该有锁而没有锁"才是大头
