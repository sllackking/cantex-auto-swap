# Cantex Auto Swap Bot

When `gas <= threshold`, the bot fetches a quote and executes swap automatically.

## Files

- `src/main.py`: main loop
- `config.json.example`: strategy template
- `.env.example`: key template
- `run.ps1`: one-click launcher for Windows

## Quick Start

```powershell
cd D:\CCnetwork\cantex-auto-swap
powershell -ExecutionPolicy Bypass -File .\run.ps1
```

## Web UI

```powershell
cd D:\CCnetwork\cantex-auto-swap
powershell -ExecutionPolicy Bypass -File .\run-ui.ps1
```

Then open:

- http://127.0.0.1:8787
- if occupied, it auto-falls back to `18787` then `28080`

UI functions:

- Start/Stop bot
- View live `bot.log`
- Realtime gas panel (or Canton mode note + latest network fee)
- Wallet management in bottom area: auto-numbered wallets (钱包1,2,3...), current-wallet display, enable/disable, delete, address copy, balances (CC/USDC/CBTC), batch add by space/tab
- Select trading pair and direction
- Set trade amount
- `use_max_balance=true` to use full available sell-token balance (MAX mode), with optional `reserve_amount`
- Toggle round-trip (trade out then trade back immediately)
- Set max auto trade count (`0` means unlimited)
- Edit and save key strategy fields in `config.json`

On first run, it will auto-create:

- `config.json` from `config.json.example`
- `.env` from `.env.example`

## Key Config

`config.json`:

```json
{
  "gas": {
    "provider": "eth_rpc",
    "max_gwei": 8.0,
    "eth_rpc_url": "https://rpc.ankr.com/eth"
  },
  "trade": {
    "dry_run": true,
    "quote_params": {
      "sell_amount": "10",
      "sell_instrument": {
        "admin": "REPLACE_WITH_ADMIN_PARTY",
        "id": "REPLACE_WITH_INSTRUMENT_ID"
      },
      "buy_instrument": {
        "admin": "REPLACE_WITH_ADMIN_PARTY",
        "id": "REPLACE_WITH_INSTRUMENT_ID"
      }
    },
    "swap_params": {
      "sell_amount": "10",
      "sell_instrument": {
        "admin": "REPLACE_WITH_ADMIN_PARTY",
        "id": "REPLACE_WITH_INSTRUMENT_ID"
      },
      "buy_instrument": {
        "admin": "REPLACE_WITH_ADMIN_PARTY",
        "id": "REPLACE_WITH_INSTRUMENT_ID"
      }
    },
    "max_price_impact_bps": 200,
    "min_expected_out": "0",
    "cooldown_seconds": 120
  }
}
```

- `dry_run=true`: no real order, only simulation logs.
- `quote_params` and `swap_params` now follow `cantex_sdk v0.4.0` method signature.

## Env

`.env`:

```dotenv
CANTEX_OPERATOR_KEY=replace_me
CANTEX_TRADING_KEY=replace_me
CANTEX_BASE_URL=https://api.cantex.io
```

The bot still accepts legacy env names:
- `OPERATOR_PRIVATE_KEY_HEX`
- `INTENT_TRADING_PRIVATE_KEY_HEX`
- `CANTEX_API_BASE_URL`

## Notes

- No third-party Python dependency is required now (standard library only).
- The only external runtime dependency is `cantex_sdk` itself.
- Launcher uses `.venv312` (Python 3.12) by default.
- Offline install is supported:
  - put SDK source in `D:\CCnetwork\cantex_sdk`, or
  - put SDK source in `D:\CCnetwork\cantex-auto-swap\vendor\cantex_sdk`
  - then rerun `run.ps1`
