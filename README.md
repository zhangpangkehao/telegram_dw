# tdl GUI

这是一个给 [iyear/tdl](https://github.com/iyear/tdl) 做的本地可视化操作界面。它不会替换 tdl，只是帮你用网页表单生成并执行常用命令。

## 启动

双击当前目录里的 `start.bat`。

启动后会打开本地网页：

```text
http://127.0.0.1:8765
```

如果 8765 端口被占用，程序会自动尝试后面的端口，具体地址会显示在启动窗口里。

## 默认配置

默认使用你提供的 tdl 路径：

```text
D:\ruanjian\ruanjian\tdl_Windows_64bit\tdl.exe
```

默认使用现有会话目录：

```text
D:\ruanjian\ruanjian\tdl_Windows_64bit\tdl_home
```

后端执行 tdl 时会自动把 `USERPROFILE` 设置为这个目录，因此会继续使用 `tdl_home\.tdl\data` 里的登录会话。

## 常用流程

1. 先到 `Chats` 页面点击 `List Chats`，找到频道或群组。
2. 点击表格中的聊天记录，界面会自动填入 Export / Quick Download 的 chat 字段。
3. 到 `Download` 页面选择：
   - `Message Links`：直接粘贴 Telegram 消息链接下载。
   - `Export File`：使用已导出的 JSON 文件下载。
   - `Quick Export + Download`：先导出指定聊天消息，再自动下载其中的文件。
4. 下方 `Console` 会实时显示 tdl 输出。登录 code/qr 模式需要输入时，可以在 Console 输入框里输入。

## 功能

- Login：支持 desktop / code / qr 登录模式。
- Chats：列出聊天并选择目标聊天。
- Download：支持链接下载、JSON 下载、导出后下载。
- Export：导出消息 JSON。
- Forward：转发消息。
- Upload：上传文件。
- Users：导出频道/群组用户。
- Settings：修改 tdl 路径、home、代理、并发、线程等默认配置。

## 注意

Codex 沙箱测试时无法写入 `D:\ruanjian\...`，所以这里无法真正连接 Telegram 完整跑下载。但你在 Windows 上双击 `start.bat` 正常运行时，不会受到这个沙箱限制。
