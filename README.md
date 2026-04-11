# Cantex 自动交易面板（开源版）

这是一个面向 Cantex 的自动交易工具，支持：
- 多钱包批量管理（启用/停用/删除）
- UI 可视化参数配置
- 自动轮询 + 自动下单
- 往返交易（A->B 后再 B->A）
- 日志归档（清空时自动备份历史日志）

## 便携分享版
- 仓库内提供可直接分发的版本：`portable-share/`
- 给朋友使用时，直接把 `portable-share` 整个文件夹发给对方即可
- 对方双击 `portable-share/start.bat` 就能自动初始化并启动 UI

## 1. 环境要求
- Windows 10/11
- Python 3.11 或 3.12
- 能访问 Cantex API 的网络环境

## 2. 安装 Python（新手步骤）
1. 打开 Python 官网下载页：  
   https://www.python.org/downloads/windows/
2. 下载并安装 `Python 3.11.x` 或 `Python 3.12.x`
3. 安装时务必勾选：`Add Python to PATH`
4. 安装完成后，在 PowerShell 验证：

```powershell
python --version
pip --version
```

## 3. 关于 cantex_sdk（默认无需单独下载）
- 分享版已内置 `cantex_sdk`，`run-ui.ps1` 会自动安装。
- 只有当你自己删掉了 `cantex_sdk` 目录，才需要手动安装官方 SDK：
  https://github.com/caviarnine/cantex_sdk

## 4. 下载项目
```powershell
git clone https://github.com/sllackking/cantex-auto-swap.git D:\CCnetwork\cantex-auto-swap
```

## 5. 一键启动（推荐）
```powershell
cd D:\CCnetwork\cantex-auto-swap
powershell -ExecutionPolicy Bypass -File .\run-ui.ps1
```

启动后访问：
- 本机：`http://127.0.0.1:39087`
- 局域网：终端会打印 `LAN URL`

## 6. 首次配置步骤（给新手）
1. 打开 UI 后，先在「钱包管理」里批量添加钱包（每行：`操作员私钥 空格/Tab 交易私钥 [可选备注]`）
2. 点击「刷新地址与余额」
3. 在「策略设置」里选择交易参数
4. 先保持“测试-开（dry_run）”跑模拟
5. 点击「启动」

## 7. 日志与历史记录
- 实时日志文件：`bot.log`
- 归档目录：`log_archive/`
- UI 点击“清空日志”会先归档再清空

## 8. 安全建议
- 私钥只保存在本地，不要发给任何人
- 不要把 `.env`、`wallets.json` 上传到 GitHub
- 先小金额测试，再逐步放大

## 9. 常见问题
- `No module named cantex_sdk`：先确认项目目录里有 `cantex_sdk` 文件夹，再运行 `run-ui.ps1`
- 页面打不开：检查终端端口输出、代理和防火墙

## 10. 开源与贡献
欢迎 PR / Issue。提交前请确认不包含真实私钥。
