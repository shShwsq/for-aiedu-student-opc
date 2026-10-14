---
name: review_error_handling
description: 审查异常处理与错误边界,覆盖异常吞没、资源泄漏、边界条件、空值误用等模式。Invoke when 审查任何含业务逻辑的仓库,重点是 IO/网络/数据库交互代码与对外 API 层。
---

# 异常处理与错误边界审查

## 适用场景

任何含业务逻辑的仓库都应执行本技能,尤其是 IO / 网络请求 / 数据库 / 文件操作密集的代码。
重点语言:Python、Node.js(JavaScript/TypeScript)、Java、Go。

## 执行步骤

### 1. 定位异常吞没点

调用 `search_code` 搜以下模式(每次搜一个,建议 context_lines=3-5 看上下文;示例省略必填参数 `repo_path`,实际调用须带上任务工作区根路径——仓库任务为 `clone_repo` 返回值,上传任务由系统注入工作区、见任务上下文):

**Python(空 except / 吞异常 / 裸 pass):**
```
search_code(pattern="except[^:]*:\\s*$", file_glob="*.py")           # 空 except 块起点
search_code(pattern="except.*:\\s*pass", file_glob="*.py")           # except: pass 直接吞掉
search_code(pattern="except Exception", file_glob="*.py")            # 宽泛捕获,可能掩盖具体错误
search_code(pattern="\\.\\.\\.\\s*$|except.*:\\s*\\.\\.\\.", file_glob="*.py")  # except: ...
```

**Node.js(吞 Promise 拒绝 / 空 catch):**
```
search_code(pattern="catch\\s*\\([^)]*\\)\\s*\\{\\s*\\}", file_glob="*.js")   # 空 catch {}
search_code(pattern="\\.catch\\(\\s*\\(\\s*\\)\\s*=>\\s*\\{?\\s*\\}?\\s*\\)", file_glob="*.js")  # .catch(() => {})
search_code(pattern="void\\s+\\w+\\(|\\w+\\(\\);?\\s*$", file_glob="*.ts")    # 未 await/未处理 rejection(需人工确认)
```

**Java(空 catch / printStackTrace 了事):**
```
search_code(pattern="catch\\s*\\([^)]*\\)\\s*\\{\\s*\\}", file_glob="*.java")
search_code(pattern="printStackTrace\\s*\\(", file_glob="*.java")
```

**Go(err 被忽略):**
```
search_code(pattern="_,\\s*_\\s*:?=", file_glob="*.go")              # 双下划线丢弃 error
search_code(pattern="if\\s+err\\s*!=\\s*nil\\s*\\{\\s*\\}", file_glob="*.go")  # 空错误分支
```

对每个命中点 `read_file` 看上下文,判断:
- 吞掉的异常是否可能携带关键失败信息(写库失败、支付回调失败等)→ 严重
- 是否是刻意吞掉(幂等清理、探测性调用)→ 可接受,不报

### 2. 定位资源泄漏

```
search_code(pattern="open\\s*\\(", file_glob="*.py")                 # Python:是否用 with
search_code(pattern="requests\\.(get|post|put)\\(", file_glob="*.py")  # session 是否 close
search_code(pattern="createConnection|createPool|fs\\.open", file_glob="*.js")
search_code(pattern="new\\s+(FileInputStream|Connection|Socket)", file_glob="*.java")  # 是否 try-with-resources
```

判定标准:
- Python `f = open(...)` 后无 `with` 也无配对的 `f.close()`(尤其异常路径)→ 泄漏
- 数据库连接 / HTTP 客户端在循环里反复创建不复用 → 性能+泄漏双重问题
- Java 未用 try-with-resources 的 Closeable → 泄漏

### 3. 检查边界条件与空值

重点看这几类函数(用 `search_code` 定位后 `read_file` 逐个确认):
- 解析外部输入的函数(JSON 解析、表单参数、命令行参数):缺字段/类型错误时是否有防护
- 列表/字典取值:`lst[0]`、`d[key]`(无 get 兜底)、`lst[-1]` 对空集合是否安全
- 除法与取余:除数是否可能为 0
- 字符串 split/slice:对空串是否安全
- 数值转换:`int(s)` / `parseInt(s)` 对非数字输入是否捕获

### 4. 输出结果

每个问题在最终总结里给出:
- **位置**:文件:行号
- **类别**:异常吞没 / 资源泄漏 / 边界缺失 / 空值误用
- **严重度**:high(会导致数据丢失/服务不可用)、medium(特定条件下出错)、low(代码健壮性)
- **证据**:关键代码片段
- **建议**:具体修法(补 with / 缩小 except 范围 / 加兜底值)

## 避免误报

- 测试文件里的 `except: pass` 不算生产问题
- 刻意吞掉的清理代码(如 `finally` 里 close 失败)通常可接受
- 框架层的宽泛捕获(如 Web 框架的全局异常中间件)是设计使然

## 避免漏报

- 不要只看 except 块本身,要看**异常发生后程序状态是否一致**(部分写入、锁未释放)
- 异步代码的异常吞没更隐蔽:未 await 的协程抛错无人知晓
- Go 的 `defer` 关闭在 return 之后才执行,注意提前 return 路径
