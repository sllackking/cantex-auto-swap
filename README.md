# Cantex 自动交易面板（开源版）

这是一个面向 Cantex 的自动交易工具，支持：
- 多钱包批量管理（启用/停用/删除）
- UI 可视化参数配置
- 自动轮询与自动下单
- 往返交易（A->B 后再 B->A）
- 日志归档（清空时自动备份历史日志）

## 1. 环境要求
- Windows 10/11
- Python 3.11 或 3.12
- 可访问 Cantex API 的网络环境

## 2. 安装 Python（新手）
1. 下载并安装 Python 3.11/3.12：
   https://www.python.org/downloads/windows/
2. 安装时勾选 `Add Python to PATH`
3. 验证：
```powershell
python --version
pip --version
```

## 3. 安装 cantex_sdk（必须）
官方仓库：
https://github.com/caviarnine/cantex_sdk

推荐：
```powershell
cd D:\CCnetwork
git clone https://github.com/caviarnine/cantex_sdk.git
cd D:\CCnetwork\cantex-auto-swap
python -m pip install -e D:\CCnetwork\cantex_sdk
```

## 4. 下载本项目
```powershell
git clone https://github.com/sllackking/cantex-auto-swap.git D:\CCnetwork\cantex-auto-swap
```

## 5. 一键启动
```powershell
cd D:\CCnetwork\cantex-auto-swap
powershell -ExecutionPolicy Bypass -File .\run-ui.ps1
```

访问：
- 本机：`http://127.0.0.1:39087`
- 局域网：终端打印 `LAN URL`

## 6. 首次配置
1. 在 UI 的“钱包管理”里批量添加钱包（每行：`operator_key trading_key [备注]`）
2. 刷新地址与余额
3. 配置交易参数
4. 先用“测试-开（dry_run）”观察日志
5. 确认无误后再切真实交易

## 7. 日志与归档
- 实时日志：`bot.log`
- 归档目录：`log_archive/`
- UI 点击“清空日志”会先归档再清空

## 8. 安全建议
- 私钥只保存在本地
- 不要提交 `.env`、`wallets.json`、`secrets/`
- 先小额测试再放大

## 9. 常见问题
- `No module named cantex_sdk`：先安装 `cantex_sdk`，再运行 `run-ui.ps1`
- 页面打不开：检查端口、防火墙、代理

## 10. 开源贡献
欢迎 PR / Issue。提交前请确认不包含任何真实私钥。
