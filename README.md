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
左侧手柄拖动或通过键盘方向键调整项目顺序。
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

- 点击待办框的文字或空白区域可直接修改，离开输入框或按回车自动保存，Esc 取消；进度色块仍用于修改进度。
- 电脑按住待办框上下拖动排序；手机长按约半秒后拖动，普通滑动仍可滚动页面。正在编辑文字时不触发拖动。
- 键盘聚焦待办框后，Enter 编辑，Alt＋上下方向键排序；原“编辑”按钮继续支持批量修改。
- 保存失败会保留草稿，按回车或再次离开输入框可重试；排序失败恢复原顺序。

独立数据验证：`python3 tests/todo_inline_test.py`、`python3 tests/integration_test.py`。
安装 Playwright 和 Chromium 后可运行 `node tests/todo_browser_test.cjs`；也可通过
`PLAYWRIGHT_MODULE` 指定已有 Playwright 安装位置。浏览器测试使用临时数据库，覆盖电脑操作与 Chromium 手机触摸模拟，不代表 iOS Safari 或 Android 真机验证。

### 原文字编辑（v40）

待办事项和普通文字卡片都直接在原文字上编辑，保留原字体、字号和背景，不新增输入框。
普通文字回车换行，点击外部或 Ctrl／⌘＋回车保存，Esc 取消；待办仍按回车保存。
校验或保存失败会保留草稿，修正后再保存。图片和附件说明继续使用原有编辑方式。
