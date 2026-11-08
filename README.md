# MiMo 配音台


本地网页配音工具，基于小米 MiMo TTS。支持预置音色、音色克隆、长稿分段、音频清晰化和合并。仅需 Python 3.8+，无需安装第三方依赖。

## 启动

1. 将 `config.example.json` 复制为 `config.json`，填入自己的 MiMo API Key；也可以启动后在页面中设置。不要提交 `config.json`。
2. Windows 双击 `启动.bat`，或运行 `python server.py`。
3. 打开 http://127.0.0.1:8765。

生成音频保存在本地 `output/`，克隆音色样本保存在 `samples/`。两者及错误日志均不纳入 Git。详细架构和接口说明见 [开发文档](开发文档.md)。
