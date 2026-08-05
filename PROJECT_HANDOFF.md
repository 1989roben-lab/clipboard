# Memory 项目交接文档

## 1. 项目概况

- 项目名称：Memory
- GitHub：`https://github.com/1989roben-lab/clipboard`
- 生产地址：`https://memory.s1242624.org`
- 生产端口：`8016`
- 技术栈：Python 标准库、SQLite、原生 HTML/CSS/JavaScript、Docker Compose
- 运行依赖：仅 Docker；前端没有构建步骤，Python 没有第三方运行时依赖

Memory 是部署在私人服务器上的个人记忆库，用于保存文字、图片和普通文件。
当前界面以移动端和 PWA 为主要使用场景，并针对 iPhone Safari、独立窗口、
安全区域和 Android 主屏幕安装进行了适配。

## 2. 目录与核心文件

| 文件 | 作用 |
| --- | --- |
| `server.py` | HTTP 服务、SQLite 数据访问、上传、下载、编辑和删除接口 |
| `static/index.html` | 单页界面、全部 CSS 与前端 JavaScript |
| `static/manifest.webmanifest` | PWA 名称、启动地址与图标配置 |
| `static/service-worker.js` | 应用外壳离线缓存；不缓存用户内容 |
| `static/icons/` | iOS、PWA、maskable 图标和图标母版 |
| `tests/integration_test.py` | 后端、上传、PWA 与持久化集成测试 |
| `compose.yaml` | 通用 Docker Compose 配置 |
| `Dockerfile` | Python 3.13 Alpine 运行镜像 |

## 3. 功能与限制

- 文字最大 200 KB。
- 图片支持 PNG、JPEG、WebP 和 GIF，单张最大 10 MB。
- 普通附件最大 100 MB，按照最大 8 MB 分片上传。
- 图片和附件说明最大 12 KB。
- 文字、待办、图片和附件合计最多保留 100 条；超出后自动删除最旧记录及关联数据。
- 支持文字、图片说明和附件说明的卡片内编辑。
- 支持多项目待办记忆；紧密项目行使用无数字渐变色块切换 Stage 1–5，并可在对话框中增删改。
- “我的记忆”按最后编辑时间倒序排列，刚编辑的记录会移到最前。
- 支持粘贴图片、拖入文件、文件选择和移动端快速拍照。
- “添加文件”会自动识别：支持的图片进入预览/压缩流程，其他文件进入附件流程。
- “清空全部”会永久删除记录、上传文件和未完成上传。

## 4. 数据与持久化

- 容器内数据库：`/data/clipboard.db`
- 容器内上传目录：`/data/uploads`
- Docker 数据卷：`lan-clipboard-8016-data`
- 数据表：`entries`、`todo_items`、`file_uploads`
- 未完成的附件上传有效期为 24 小时，服务会清理过期分片。

日常停止可以执行 `docker compose down`，数据卷会保留。不要执行
`docker compose down -v`，也不要手动删除 `lan-clipboard-8016-data`。

## 5. 主要 HTTP 接口

| 方法与路径 | 作用 |
| --- | --- |
| `GET /health` | 健康检查 |
| `GET /api/entries` | 获取记录列表 |
| `POST /api/entries` | 保存文字或待办清单 |
| `PATCH /api/entries/<id>` | 编辑文字、说明或待办清单 |
| `PATCH /api/todo-items/<id>` | 修改单个待办项目的 Stage |
| `DELETE /api/entries/<id>` | 删除单条记录 |
| `DELETE /api/entries` | 清空全部记录与上传 |
| `POST /api/images` | 上传图片 |
| `GET /api/images/<id>` | 查看图片 |
| `GET /api/images/<id>/download` | 下载图片 |
| `POST /api/file-uploads` | 初始化附件分片上传 |
| `POST /api/file-uploads/<upload_id>` | 上传一个分片 |
| `POST /api/file-uploads/<upload_id>/complete` | 完成附件上传 |
| `DELETE /api/file-uploads/<upload_id>` | 取消未完成上传 |
| `GET /api/files/<id>/download` | 下载附件 |

## 6. PWA 与图标维护

Service Worker 只缓存 HTML、manifest 和应用图标，不缓存 `/api/`、`/health`、
图片、附件或下载响应。离线时只显示应用外壳，联网恢复后重新读取服务器数据。

修改页面外壳、manifest、图标或缓存策略时，应同时递增以下版本：

1. `static/service-worker.js` 的 `CACHE_NAME`。
2. Service Worker 中的版本化离线启动地址。
3. `static/index.html` 注册 Service Worker 的查询参数。
4. `static/manifest.webmanifest` 的 `start_url` 和图标查询参数。

当前应用外壳版本为 `v33`，图标资源查询参数为 `v14`。Chrome、macOS Dock
和已安装的 PWA 可能长期保留旧图标。
即使服务端图标已经更新，已安装应用也不一定立即刷新。排查时先确认公网 manifest
和图标返回最新内容；仍显示旧图时，应删除旧应用，在 Chrome 的
`chrome://apps` 中清理旧条目，完全退出 Chrome 后重新打开生产地址并安装。

## 7. 本地运行与验证

```sh
docker compose up -d --build
docker compose ps
curl -fsS http://127.0.0.1:8016/health
```

修改代码后至少执行：

```sh
python3 tests/integration_test.py
node -e 'const fs=require("fs"); const html=fs.readFileSync("static/index.html","utf8"); const matches=[...html.matchAll(/<script>([\s\S]*?)<\/script>/g)]; if(matches.length!==1) throw new Error(`expected one script, got ${matches.length}`); new Function(matches[0][1]); console.log("inline JavaScript syntax OK")'
git diff --check
```

涉及布局时，还应使用约 `393 × 852`、`432 × 960` 和 `320px` 宽的视口检查：

- 页面没有横向滚动。
- 输入框在 Safari 聚焦时不自动放大。
- 刘海、状态栏和底部手势区不遮挡内容。
- 安装提示、强制刷新、上传、拍照和编辑均可正常使用。

## 8. 生产部署

生产目录为 `/home/aa/apps/clipboard`。生产环境的 `compose.yaml` 包含额外的
Cloudflare Tunnel 服务，并只把应用端口绑定到 `127.0.0.1:8016`，因此不能
使用仓库中的通用 `compose.yaml` 覆盖生产文件，也不能覆盖生产 `.env`。

推荐流程：

1. 本地运行全部测试。
2. 检查生产容器、健康状态和记录数量。
3. 在生产目录的 `.deploy-backups/<时间戳>/` 备份 `Dockerfile`、`README.md`、
   `server.py`、`static` 和 `tests`。
4. 只更新程序文件，不覆盖 `compose.yaml`、`.env`、部署备份或数据卷。
5. 执行 `docker compose up -d --build clipboard`，只重建应用服务。
6. 等待容器变为 `healthy`，同时验证内部地址和公网 `/health`。
7. 对比部署前后的记录数量，并检查公网 manifest、Service Worker 和图标版本。

Cloudflare、GitHub 或服务器授权信息只保存在服务器的私有配置中，不应写入仓库、
日志、Issue、提交信息或交接文档。

## 9. 回滚

如果新版本异常：

1. 先查看 `docker compose ps` 和 `docker compose logs clipboard`。
2. 从最近的 `.deploy-backups/<时间戳>/` 恢复程序文件。
3. 再次执行 `docker compose up -d --build clipboard`。
4. 验证内部与公网健康检查、记录数量、图片和附件下载。

不要通过删除容器数据卷来排查应用代码问题。

## 10. 当前交接状态

- 生产容器：健康。
- 公网域名：可访问。
- PWA 缓存版本：`v33`。
- 图标：蓝紫色半透明记忆薄片，新图标已用于 iOS、PWA 和 maskable 尺寸。
- 最近一次图标发布前后记录数量一致，持久化数据未受影响。
