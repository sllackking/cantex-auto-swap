# Cantex 自动交易 - 便携分享版

这个文件夹可以直接发给别人使用，不依赖固定盘符。

## 使用步骤（给朋友）
1. 安装 Python 3.11 或 3.12（勾选 Add Python to PATH）
   - https://www.python.org/downloads/windows/
2. 双击 `start.bat`
3. 打开浏览器访问 `http://127.0.0.1:39087`

## 说明
- 已内置 `cantex_sdk`
- 启动脚本会自动安装依赖
- 首次请先用“测试-开（dry_run）”

## 启用与关闭
- 启用：双击 `start.bat`
- 关闭：终端窗口按 `Ctrl + C`

## 安全提醒
- 私钥仅保存在本地
- 不要把 `.env`、`wallets.json`、`secrets/` 公开
