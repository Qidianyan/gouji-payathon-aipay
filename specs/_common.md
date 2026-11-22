## 通用约束（所有任务）
- 工作目录是仓库根（含 pyproject.toml）。Python 3.13，只能用标准库 + `cryptography` + `pytest`（已装在 `.venv`）。**不要联网安装任何包，不要改 pyproject.toml。**
- 运行测试：`.venv/bin/python -m pytest -q`。
- 只改/新建本 SPEC 列出的文件；不要动 `aipay_kit/signing.py`、`aipay_kit/gateway/base.py`、`tests/test_signing.py`、`specs/`。
- 不要写任何真实密钥、token、URL 中的 key；不要访问网络。
- 代码要有类型标注，函数短小，中文或英文注释均可。
- 最终回复格式：1) 改了/新建了哪些文件；2) 测试命令与结果摘要（passed/failed 数）；3) 已知限制。
- **禁止运行任何 git 命令**（git 由 Bot 在外层做）。任务应在 5 分钟内完成：只写要求的文件，跑一次测试就收尾。
