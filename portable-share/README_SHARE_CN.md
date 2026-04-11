# Cantex Auto Swap - 便携分享版

这个文件夹可以直接发给别人使用，不依赖固定盘符（不要求 D 盘）。

## 包内已包含
- `cantex-auto-swap` 主程序
- 本地 `cantex_sdk` 源码（同目录 `cantex_sdk`）
- 一键启动脚本：`start.bat`

## 对方电脑首次使用（Windows）
1. 安装 Python 3.11 或 3.12（勾选 Add Python to PATH）
   - 下载：[Python for Windows](https://www.python.org/downloads/windows/)
2. 双击 `start.bat`
   - 脚本会自动创建 `.venv312`
   - 自动安装依赖与本地 `cantex_sdk`
   - 自动启动 UI

## 访问地址
- 本机：`http://127.0.0.1:39087`
- 局域网：终端会打印 `LAN URL`

## 启用与关闭
- 启用：双击 `start.bat`
- 关闭：在黑色终端窗口按 `Ctrl + C`，或直接关闭终端窗口

## 用户必须配置
- `.env` 中填自己的私钥（不要分享给任何人）
- 或在 UI 的“钱包管理”里批量添加钱包

## 重要安全提醒
- 不要把真实私钥发给他人
- 不要把 `.env`、`wallets.json`、`secrets/` 上传公开仓库
- 先开“测试-开（dry_run）”观察日志，再做真实交易

## 常见问题
- 提示脚本被策略阻止：
  在 PowerShell 使用
  `powershell -ExecutionPolicy Bypass -File .\run-ui.ps1`
- 提示缺依赖：
  重新双击 `start.bat`，等待自动安装完成

## cantex_sdk 官方地址
- GitHub: [caviarnine/cantex_sdk](https://github.com/caviarnine/cantex_sdk)
