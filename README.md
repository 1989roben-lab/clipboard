# Memory

Memory 是一个运行在私人服务器上的个人记忆库。随时随地记录想法、保存
图片与文件，把每一个值得留下的此刻放在同一个安全、简单的空间里。

支持上传任意附件，单文件最大 100 MB，并使用分片上传提升网络稳定性。
PNG、JPEG、WebP 和 GIF 图片会保留预览与图片压缩能力，每张图片最大
10 MB。文字、待办、图片和附件合计最多保留 100 条；超过上限后优先删除
最旧的非置顶记录及其关联数据，置顶记录显示在列表最前方。

图片和普通文件都可以通过“添加文件”选择，也可以直接粘贴或拖入编辑器。
系统会自动识别图片并启用压缩与预览；其他文件使用附件分片上传。图片会
显示在当前光标位置，前后都可以继续输入文字。
加入内容后点击“存入 Memory”，即可保存为一条记忆。图片或附件说明最多
12 KB。

点击“今日待办事项”可把输入区切换为待办模式，每个非空行会保存为一个
待办项目。同一张待办记忆可以包含多个项目，每项支持从浅绿到深绿的
Stage 1–5；Stage 5 表示已完成。待办卡片仍显示在“我的记忆”中，可直接
通过五段渐变绿色块切换阶段，也可在编辑对话框中新增、修改项目，并使用
直接拖动项目或通过 Alt＋上下方向键调整项目顺序，手机长按后拖动。
所有记忆卡片都可置顶或取消置顶；多条置顶记忆按最近置顶时间排列。

“快速拍照上传”可以使用后置摄像头拍照并加入同一个
内嵌草稿；拍摄完成后同样需要点击保存才会上传。

## 手机与 PWA

页面会自动适配不同尺寸的 iPhone、iPad 和安卓手机，支持安全区域、动态
视口、单列布局、可用的触控区域及统一的 13px 内容字号。页面会显示
“安装 Memory”：浏览器支持原生安装提示时会直接唤起，否则显示对应的
添加到主屏幕步骤。

Safari 中可通过“分享 → 添加到主屏幕”安装；在 iQOO / vivo 的 OriginOS
上，可使用 Chrome 右上角菜单中的“添加到主屏幕”。通过 HTTPS 打开时可
安装为完整的独立窗口应用。独立窗口已针对 Safari 状态栏、刘海与灵动岛、
底部手势区、窄屏换行和输入法视口进行适配。

PWA 离线时只保留应用界面，不缓存历史文字、图片、附件或下载响应。断网
期间会显示“服务器未连接”，恢复联网后自动重新读取历史记录。

## 管理

```sh
docker compose up -d --build
docker compose ps
docker compose logs -f
docker compose restart
docker compose down
```

历史记录、原始图片和附件保存在 Docker 数据卷
`lan-clipboard-8016-data` 中。网页中的“清空全部”会永久删除全部
记录、上传文件和未完成上传；普通的 `docker compose down` 不会删除
记录。`docker compose down -v` 会删除整个数据卷，请勿用于日常停止
服务。

## memory1 独立实例（已停用）

自 2026-10-20 起，服务器仅保留 `https://memory.s1242624.org/`，对应
`lan-clipboard-8016` 应用和 `clipboard-cloudflared` 隧道容器。
`memory1-8019`、`memory1-cloudflared` 及其专用网络已移除；原数据卷
`memory1-8019-data` 和停用前备份保留，未与主实例合并。
以下配置仅供恢复独立实例时参考，不作为日常部署步骤。

`memory1.s1242624.org` 使用 `compose.memory1.yaml` 单独部署，不与原实例
共享容器、网络、端口、配置或数据卷：

- 应用容器：`memory1-8019`
- Tunnel 容器：`memory1-cloudflared`
- 本机地址：`http://127.0.0.1:8019`
- 数据卷：`memory1-8019-data`（数据库 `/data/memory1.db`）
- Compose 网络：`memory1-8019_default`
- Cloudflare Tunnel：`memory1-8019`

Tunnel token 保存在被 Git 忽略且权限为 `0600` 的 `.env`，与原实例部署
目录 `/home/aa/apps/clipboard` 中的配置完全分开。管理新实例时必须显式指定
它的 Compose 文件：

```sh
docker compose -f compose.memory1.yaml ps
docker compose -f compose.memory1.yaml logs -f
docker compose -f compose.memory1.yaml up -d --build
docker compose -f compose.memory1.yaml restart
docker compose -f compose.memory1.yaml down
```

请勿对新实例执行 `docker compose -f compose.memory1.yaml down -v`，该命令会
删除 `memory1-8019-data` 中的独立数据库及上传文件。

## 待办事项直接操作

- 点击待办框的文字或空白区域可直接修改，离开文字或 Ctrl／⌘＋回车保存，Esc 取消；进度色块仍用于修改进度。
- 回车在光标处分成两项，新项目默认 Stage 1；在开头或结尾回车会出现空白草稿，填写后才保存。清空已有项目后离开或按回车直接删除，最后一项删除时同时移除空清单。
- 电脑按住待办框上下拖动排序；手机长按约半秒后拖动，普通滑动仍可滚动页面。正在编辑文字时不触发拖动。
- 键盘聚焦待办框后，Enter 编辑，Alt＋上下方向键排序；原“编辑”按钮继续支持批量修改。
- 保存失败会保留草稿，按回车或再次离开输入框可重试；排序失败恢复原顺序。

独立数据验证：`python3 tests/todo_inline_test.py`、`python3 tests/integration_test.py`。
安装 Playwright 和 Chromium 后可运行 `node tests/todo_browser_test.cjs`；也可通过
`PLAYWRIGHT_MODULE` 指定已有 Playwright 安装位置。浏览器测试使用临时数据库，覆盖电脑操作与 Chromium 手机触摸模拟，不代表 iOS Safari 或 Android 真机验证。

### 原文字编辑（v40）

待办事项和普通文字卡片都直接在原文字上编辑，保留原字体、字号和背景，不新增输入框。
普通文字回车换行，点击外部或 Ctrl／⌘＋回车保存，Esc 取消。
校验或保存失败会保留草稿，修正后再保存。图片和附件说明继续使用原有编辑方式。

v42 的批量编辑弹窗与主页面使用相同的拖动方式，不再显示拖动手柄；弹窗修改在点击“保存修改”后提交，取消会放弃草稿。长清单独立滚动，底部保存按钮保持可见。

## v43：MongoDB 架构与搜索（2026-11-23）

线上实例已从 SQLite 迁至家里共享 MongoDB 的独立 `memory` 数据库。
`server.py` 负责 HTTP 接口，`memory/storage.py` 负责 MongoDB 持久化，
`memory/entries.py` 处理记忆和待办，`memory/attachments.py` 处理附件。
待办嵌入对应记忆文档，使用版本条件进行原子替换，冲突返回 409。
运行一个应用进程，通过进程内锁串行执行写入和 100 条淘汰；不支持多实例同时写同一个 Memory 库。
置顶、排序、进度、历史数字 ID、创建/修改时间和附件链接均保留。

### 数据连接

`.secrets/memory.env` 保存专用账号的 `MONGO_URI`，只授权读写 `memory`。
应用同时加入原网络和外部 `mongodb-shared` 网络，通过 `mongodb:27017` 连接。
不提供 SQLite 自动回退；数据库不可用时 API 返回 503。
`DATA_DIR=/data`，附件继续放在 `lan-clipboard-8016-data` 卷的 `/data/uploads`。
旧 `/data/clipboard.db` 仅保留为历史文件，线上不再写入或读取它。
切勿对共享 MongoDB 使用 `down -v`，也不要删除 Memory 文件卷。

### 搜索

“我的记忆”上方可搜索正文、待办文字、图片/附件说明和原始文件名。
中文按子串匹配，英文不区分大小写；空白分隔的多个词必须全部命中，
可以分别命中不同字段。关键词作为普通文本，不执行正则表达式，最多 200 字符。
接口为 `GET /api/entries?q=关键词`，不传 q 保持原行为。
命中待办会展示完整清单，搜索不改变置顶和更新时间排序。
输入延迟 250ms 查询，支持清除、无结果提示，搜索时隐藏“清空全部”。
搜索状态和内容不写入浏览器持久缓存；本版不解析附件正文或识别图片文字。

### 备份与恢复

用户 cron 每天 **03:25（Asia/Shanghai）** 运行 `scripts/backup_memory.py`：
短暂停止 Memory 写入后，成套备份 `memory` 数据库（含用户角色定义）和附件，
写入 SHA256 清单，再恢复应用。最近 7 天备份保存在：
`/home/aa/Vibecoding/shanghai-migration/backups/memory-daily/`。
原共享 MongoDB 每日 03:15 全库备份仍然执行，它只保护数据库，不替代附件配套备份。
每 5 分钟巡检现在包含 Memory 域名和容器健康状态。

手工生成配套备份：

```sh
python3 scripts/backup_memory.py
```

迁移前快照、旧 Compose 和最终校验结果在：
`/home/aa/Vibecoding/shanghai-migration/backups/memory-v42-20261123/`。
最终迁移校验为 100 条记忆、194 个待办项、11 个附件，附件合计 2,490,065 字节。
旧镜像标签为 `memory:pre-mongo-v42-20261123`。

完整恢复应停止 Memory 后，先保存当前数据，再将同一时间点的 `memory.archive.gz`
恢复到独立测试库核验，确认后仅恢复 `memory` 命名空间和匹配的附件卷。
不要覆盖共享实例的其他数据库或全局账号。备份目录内有恢复验证结果供参考。

### 回退到 v42

不能只恢复迁移前的 SQLite，否则会丢失上线后新增内容。
先运行配套备份并停止 Memory，再用 `scripts/export_sqlite.py` 导出最新 MongoDB 状态：

```sh
docker stop lan-clipboard-8016
docker run --rm --user 0 --network mongodb-shared \
  --env-file .secrets/memory.env \
  -v /home/aa/Vibecoding/shanghai-migration/backups/memory-v42-20261123:/backup \
  memory:43 python /app/scripts/export_sqlite.py \
  --template /backup/final.db --output /backup/rollback-latest.db
```

导出目标必须尚不存在。`export_sqlite.py` 保留最新记录、待办、上传元数据和 ID 计数器，
并校验 SQLite 完整性。保留当前 MongoDB 和附件备份后，将导出数据库与**当前附件**
放入新的回退数据卷，使用保存的 v42 Compose 与旧镜像启动，验证后再继续使用。
不要直接把旧快照覆盖回当前卷。v42 启动会取消未完成上传，回退前应完成或取消这些上传。

### 迁移工具与测试

`scripts/migrate_sqlite.py` 是显式离线迁移工具，目标库非空时拒绝覆盖。
输入应为 SQLite backup API 生成的独立快照（转换为 DELETE 日志模式以便只读挂载），
并配套完整的 uploads 目录；不直接读取正在写入的生产 SQLite。
`--verify-only` 可逐文档比较迁移结果和历史 ID 计数器。

测试需要安装 `requirements.txt`，并设置有权创建/删除隔离测试库的
`TEST_MONGO_URI`。测试只创建随机 `memory_test_*` 库，退出时清理。
生产 `memory_app` 没有这些权限。迁移演练可额外设置
`MEMORY_MIGRATION_FIXTURE` 指向包含 `preflight.db` 和 `uploads/` 的离线快照目录。

```sh
python3 tests/integration_test.py
python3 -m unittest discover -s tests -p todo_inline_test.py -v
python3 -m unittest discover -s tests -p 'test_*.py' -v
node tests/todo_browser_test.cjs
node tests/search_browser_test.cjs
```

浏览器测试使用 Playwright，可通过 `PLAYWRIGHT_MODULE` 指定模块路径，
`PLAYWRIGHT_CHROMIUM_EXECUTABLE` 指定 Chromium。截图只使用隔离测试数据。
浏览器测试覆盖桌面和 Chromium 手机模拟，不代表 iPhone 真机验证。

## v45：文字与多图混排（2026-11-23）

普通文字卡片保持直接点击原文字编辑；在编辑时粘贴图片，会切换为图文混排编辑。
已有图文卡片可点击文字直接编辑，在光标处继续输入文字或粘贴多张图片，
离开卡片自动保存，Esc 取消。保存失败保留草稿并可重试。
每张图片仍以文件保存在附件卷，MongoDB 文档中的 `images` 列表记录图片顺序、
文字位置和元数据；旧单图文档无需迁移即可继续编辑。
单张图片最大 10 MB，混排文字最大 200 KB。未提交的临时图片超过 24 小时后，
在下次上传或应用启动时清理。
现有备份同时包含 MongoDB 与附件。`export_sqlite.py` 遇到多图记录时拒绝导出到 v42，
避免静默丢失后续图片。浏览器测试增加 `node tests/mixed_browser_test.cjs`。
