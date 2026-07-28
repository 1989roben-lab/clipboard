# 局域网内容中转站

在 Mac 上通过 Docker 运行，并让同一局域网中的 Windows 电脑通过
`http://Mac局域网IP:8016` 复制文本或下载图片。

支持 PNG、JPEG、WebP 和 GIF，每张图片最大 10 MB。文字和图片合计最多
保留 100 条；超过上限后会删除最旧记录及其原始图片文件。

图片可以直接粘贴或拖入输入框，也可以通过“选择图片”加入草稿。图片会
直接显示在当前光标位置，不会出现附件卡片；图片前后都可以继续输入文字。
加入图片后不会立即上传，最后点击“保存到中转站”才会将图片和文字按当前
顺序保存为同一条记录。图片附带文字最多 12 KB。

手机浏览器还会显示“拍摄照片”按钮，使用后置摄像头拍照并加入同一个
内嵌草稿；拍摄完成后同样需要点击保存才会上传。

## 管理

```sh
docker compose up -d --build
docker compose ps
docker compose logs -f
docker compose restart
docker compose down
```

历史记录和原始图片保存在 Docker 数据卷 `lan-clipboard-8016-data`
中。普通的 `docker compose down` 不会删除记录。`docker compose
down -v` 会删除整个数据卷，请勿用于日常停止服务。
