# Cantex 自动交易面板（开源版）

这是一个面向 Cantex 的自动交易工具，支持：

- 多钱包批量管理（启用/停用/删除）
- UI 可视化参数配置
- 自动轮询 + 自动下单
- 往返交易（A->B 后再 B->A）
- 日志归档（清空时自动备份历史日志）

## 1. 环境要求

- Windows 10/11
- Python 3.11 或 3.12
- 能访问 Cantex API 的网络环境

## 2. 下载项目

### 方式 A：直接下载 ZIP
在仓库页面点 `Code` -> `Download ZIP`，解压到例如：

`D:\CCnetwork\cantex-auto-swap`

### 方式 B：Git 克隆

```powershell
git clone https://github.com/sllackking/cantex-auto-swap.git D:\CCnetwork\cantex-auto-swap
```

## 3. 一键启动（推荐）

```powershell
cd D:\CCnetwork\cantex-auto-swap
powershell -ExecutionPolicy Bypass -File .\run-ui.ps1
```

启动后访问：

- 本机：`http://127.0.0.1:39087`
- 局域网：终端会打印 `LAN URL`

## 4. 首次配置步骤（给新手）

1. 打开 UI 后，先在「钱包管理」里批量添加钱包  
每行格式：

`操作员私钥 空格/Tab 交易私钥 [可选备注]`

2. 点击「刷新地址与余额」

3. 在「策略设置」里选择交易对、方向、数量区间

4. 保持 `演练模式(dry_run)=开启` 先跑模拟

5. 点击「启动」

6. 确认日志正常后，再决定是否关闭 `dry_run` 做真实交易

## 5. 参数说明（核心）

- `交易数量（随机区间）`  
  在最小值和最大值之间随机；只填最小值则固定数量。

- `自动交易次数 max_trades`  
  `0` 表示无限循环；`1` 表示仅执行一轮。

- `并发钱包数 concurrent_wallets`  
  `0` 表示所有启用钱包同时参与；`N` 表示最多同时用 N 个钱包。

- `往返交易 roundtrip_enabled`  
  开启后：先 A->B，再立即 B->A。

- `全仓模式 use_max_balance`  
  开启后按余额可卖上限交易（会扣除 `reserve_amount` 保留量）。

- `最大网络费 max_network_fee`  
  当前网络费高于此值时，会等待，不执行交易。

- `最大滑点(%)`  
  允许的最大价格影响，超过则跳过本次。

- `轮询间隔秒 interval_seconds`  
  每隔多少秒检查一次交易条件。

## 6. 日志与历史记录

- 实时日志文件：`bot.log`
- 归档目录：`log_archive/`
- 在 UI 点击「清空日志」时，实际会：
  1) 先把当前 `bot.log` 归档到 `log_archive/bot_YYYYMMDD_HHMMSS.log`
  2) 再清空 `bot.log`

所以不会丢历史记录，支持跨天查询。

## 7. 交易记录查询（本地）

UI 顶部有「交易记录查询」按钮：

1. 输入钱包地址
2. 系统会从 `bot.log + log_archive/*.log` 检索最近 20 条
3. 结果弹窗显示

## 8. 安全建议（务必看）

- 私钥只保存在本地，不要发给任何人
- 不要把 `.env`、`wallets.json` 上传到 GitHub
- 首次务必用 `dry_run=true`
- 先小金额测试，再逐步放大

## 9. 常见问题

- `No module named cantex_sdk`  
  请用 `run-ui.ps1` 启动，或确认本地已安装 `cantex_sdk`。

- 页面打不开  
  检查终端打印端口；防火墙/代理是否拦截。

- 复制地址失败  
  已内置兼容复制逻辑，若仍失败请手动复制。

- 查询无交易记录  
  可能是该地址还未产生 `TRADE_RESULT`，或日志刚清空且无新交易。

## 10. 开源与贡献

欢迎 PR / Issue。提交前请确认：

- 不包含真实私钥
- 不提交 `.env`、`wallets.json`、`secrets/`、`log_archive/`
- 新功能有最基本自测说明
