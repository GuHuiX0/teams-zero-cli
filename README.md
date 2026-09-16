# Teams 本地缓存 CLI：Python 3.12 零安装依赖源码版

需要预先安装 Python 3.12。解压完整目录即可运行，不需要 pip、MCP SDK、Brotli、zstd 或 C/C++ 编译器。第三方纯 Python 解析器源码已附带在 vendor/，请保留整个目录和 LICENSES/。工具运行不联网；网页只分发工具，不接收消息。

## 使用

在解压目录打开 PowerShell：

```powershell
py -3.12 -S teams_cli.py --help
py -3.12 -S teams_cli.py diagnose
py -3.12 -S teams_cli.py accounts
py -3.12 -S teams_cli.py conversations --limit 20
py -3.12 -S teams_cli.py search "关键词" --limit 20
```

## Dev team monitoring

The monitor supports a database-like bootstrap followed by incremental
collection. First configure one exact group chat, load all messages currently
available in the local cache, and then schedule fingerprint-based updates:

```powershell
py -3.12 -S teams_cli.py monitor configure --config monitor.json `
  --account 'tenantId:userId' --conversation 'Dev Team'
py -3.12 -S teams_cli.py monitor bootstrap --config monitor.json
py -3.12 -S teams_cli.py monitor install-task --config monitor.json --interval-minutes 15
```

Each run with changes writes an immutable JSON message batch to `digests/`. Edits and late
arrivals are detected by message ID and content fingerprint. A pending batch is
not considered delivered until an external destination acknowledges it. Use
`monitor status`, `monitor batches`, `monitor show`, and `monitor ack` to inspect
and acknowledge batches.

The repository also provides a dependency-free stdio MCP server:

```powershell
py -3.12 -S teams_cli.py serve --config monitor.json
```

It exposes cache reads, bootstrap/incremental ingestion, immutable batch pages,
mentions, and checkpoints. All knowledge extraction and workflow analysis are
performed by your external agent using the source messages; no keyword rules or
generated insights are included. The MCP host can publish the agent's findings
through a separate OneNote MCP. This collector never writes
OneNote directly. See [docs/monitoring.md](docs/monitoring.md) for deployment on
another Windows computer and the delivery acknowledgement protocol.

如果没有 py 启动器，用 Python 3.12 的完整路径替代。`-S` 禁用 site-packages，可验证无需安装第三方包。

自动发现失败时，指定 IndexedDB 的 `.leveldb` 文件夹：

```powershell
py -3.12 -S teams_cli.py accounts --leveldb 'D:\cache-copy\https_teams.microsoft.com_0.indexeddb.leveldb'
py -3.12 -S teams_cli.py search '关键词' --leveldb 'D:\cache-copy\https_teams.microsoft.com_0.indexeddb.leveldb' --account 'tenantId:userId' --limit 50
```

`--account` 的值来自 accounts 输出的 key。`--limit` 必须是正整数，默认 50；命令前后均可放公共参数。JSON 输出到 stdout，计数和错误到 stderr。可用 `> results.json` 重定向，旧版 Windows PowerShell 可能改变重定向文件编码。`--debug` 打印完整错误信息；诊断输出可能包含本地用户名和路径。

输入目录会先复制到临时目录，读取后关闭文件并清理副本。复制并非实时数据库的原子快照；如果复制失败或结果不一致，先退出 Teams 再重试。程序不会修改原缓存。它只读取本机已缓存的部分记录，不承诺完整聊天历史。

## 验收与限制

```powershell
py -3.12 -S -m unittest discover -v
py -3.12 -S tools/release.py --verify
```

Python 3.12 下的合成测试覆盖：无 site-packages 导入；Snappy、V8、
IndexedDB/LevelDB 解析；当前记录与删除标记；HTML 正文；全量初始化、
增量更新、崩溃恢复与投递确认；MCP stdio；计划任务 XML；源码打包校验。
测试不读取真实用户缓存，不安装真实计划任务，也不写入 OneNote。
真实 Teams 版本兼容性和 OneNote MCP 联动需要在目标电脑验收。

缓存 schema 随 Teams 更新可能变化，遇到不支持记录可跳过或报错。阅读器
仍然只读取本地缓存，不承诺完整服务器历史。MCP 服务使用 stdio，stdout
只输出 JSON-RPC；调试信息输出到 stderr。大数据库会占用临时磁盘和内存，
纯 Python 解码速度有限。监控、批次和日志目录可能包含私密聊天内容，应
按本机安全要求保护。

## 来源

PROVENANCE.json 记录固定上游提交。upstream/ 保留原 reader、CLI 与许可证供比对；msteams_local_cli/ 是本地改编版。所有上游 MIT 许可保留在 LICENSES/ 和源文件中。CCL 仅附带 IndexedDB 所需模块，包入口省去 HTTP 缓存/profile 导入。reader 增加关闭文件与临时副本清理、异常消息类型处理。CLI 增加参数验证和可诊断错误输出。
