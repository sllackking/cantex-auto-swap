from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import json
import logging
import os
import random
import sys
import time
from dataclasses import asdict, is_dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
SECRETS_DIR = ROOT / "secrets"
GAS_GATE_PATH = ROOT / "runtime_gas_gate.json"
GAS_GATE_LOCK_PATH = ROOT / "runtime_gas_gate.lock"


def load_dotenv(dotenv_path: Path) -> None:
    if not dotenv_path.exists():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def to_decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    if value is None:
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return default


def as_dict(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        return data
    if is_dataclass(data):
        return asdict(data)
    if hasattr(data, "model_dump") and callable(getattr(data, "model_dump")):
        return data.model_dump()
    if hasattr(data, "dict") and callable(getattr(data, "dict")):
        return data.dict()
    if hasattr(data, "__dict__"):
        return dict(data.__dict__)
    return {}


def setup_logger(level: str, log_file: str | None) -> logging.Logger:
    wallet_name = os.getenv("WALLET_NAME", "default")
    logger = logging.getLogger("cantex_auto_swap")
    logger.setLevel(level.upper())
    logger.handlers.clear()
    fmt = logging.Formatter(
        f"%(asctime)s | %(levelname)s | [{wallet_name}] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    return logger


class GasOracle:
    def __init__(self, config: dict[str, Any]) -> None:
        self.provider = str(config.get("provider", "none")).lower()
        self.max_gwei = to_decimal(config.get("max_gwei", "10"))
        self.fixed_gwei = to_decimal(config.get("fixed_gwei", "5"))
        self.eth_rpc_url = str(config.get("eth_rpc_url", ""))

    def current_gwei(self) -> Decimal:
        if self.provider in {"none", "disabled", "off"}:
            return Decimal("0")
        if self.provider == "fixed":
            return self.fixed_gwei
        if self.provider == "eth_rpc":
            payload = {"jsonrpc": "2.0", "method": "eth_gasPrice", "params": [], "id": 1}
            req = Request(
                self.eth_rpc_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=10) as response:
                body = response.read().decode("utf-8")
            result = (json.loads(body) if body else {}).get("result")
            if not result:
                raise RuntimeError("eth_gasPrice returned empty result")
            wei = int(result, 16)
            return Decimal(wei) / Decimal(10**9)
        raise ValueError(f"Unsupported gas provider: {self.provider}")


class CantexAdapter:
    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger
        self.sdk = None
        self.InstrumentId = None
        self._setup()

    def _setup(self) -> None:
        sdk_module = importlib.import_module("cantex_sdk")
        CantexSDK = getattr(sdk_module, "CantexSDK")
        OperatorKeySigner = getattr(sdk_module, "OperatorKeySigner")
        IntentTradingKeySigner = getattr(sdk_module, "IntentTradingKeySigner")
        self.InstrumentId = getattr(sdk_module, "InstrumentId")

        operator_hex = os.getenv("CANTEX_OPERATOR_KEY") or os.getenv("OPERATOR_PRIVATE_KEY_HEX")
        intent_hex = os.getenv("CANTEX_TRADING_KEY") or os.getenv("INTENT_TRADING_PRIVATE_KEY_HEX")
        if not operator_hex:
            raise RuntimeError("Missing operator key. Set CANTEX_OPERATOR_KEY or OPERATOR_PRIVATE_KEY_HEX")
        if not intent_hex:
            raise RuntimeError("Missing trading key. Set CANTEX_TRADING_KEY or INTENT_TRADING_PRIVATE_KEY_HEX")

        operator = OperatorKeySigner.from_hex(operator_hex)
        intent = IntentTradingKeySigner.from_hex(intent_hex)
        base_url = (
            os.getenv("CANTEX_BASE_URL")
            or os.getenv("CANTEX_API_BASE_URL")
            or "https://api.cantex.io"
        )
        # Always authenticate per bot process, avoid local cached API key mix-ups.
        self.sdk = CantexSDK(operator, intent, base_url=base_url, api_key_path=None)

    async def start(self) -> None:
        await self.sdk.authenticate()

    async def close(self) -> None:
        if self.sdk:
            await self.sdk.close()

    def _build_instrument(self, data: Any, default_admin: Any = None, default_id: Any = None) -> Any:
        if isinstance(data, self.InstrumentId):
            return data
        if isinstance(data, dict):
            inst_admin = data.get("admin")
            inst_id = data.get("id")
            if inst_admin and inst_id:
                return self.InstrumentId(admin=str(inst_admin), id=str(inst_id))
        if default_admin and default_id:
            return self.InstrumentId(admin=str(default_admin), id=str(default_id))
        raise ValueError("Instrument must be {'admin':'...','id':'...'}")

    def _normalize_trade_params(self, params: dict[str, Any]) -> dict[str, Any]:
        out = dict(params)
        if "sell_amount" in out:
            out["sell_amount"] = to_decimal(out["sell_amount"])

        if "sell_instrument" not in out:
            out["sell_instrument"] = self._build_instrument(
                None,
                default_admin=out.get("sell_instrument_admin"),
                default_id=out.get("sell_instrument_id"),
            )
        else:
            out["sell_instrument"] = self._build_instrument(out["sell_instrument"])

        if "buy_instrument" not in out:
            out["buy_instrument"] = self._build_instrument(
                None,
                default_admin=out.get("buy_instrument_admin"),
                default_id=out.get("buy_instrument_id"),
            )
        else:
            out["buy_instrument"] = self._build_instrument(out["buy_instrument"])
        return out

    async def _invoke(self, method_name: str, params: dict[str, Any]) -> Any:
        method = getattr(self.sdk, method_name, None)
        if method is None:
            raise RuntimeError(f"SDK client has no method: {method_name}")
        sig = inspect.signature(method)
        kwargs = {k: v for k, v in params.items() if k in sig.parameters}
        return await method(**kwargs)

    async def get_swap_quote(self, params: dict[str, Any]) -> Any:
        return await self._invoke("get_swap_quote", self._normalize_trade_params(params))

    async def swap(self, params: dict[str, Any]) -> Any:
        return await self._invoke("swap", self._normalize_trade_params(params))

    async def get_account_info(self) -> Any:
        return await self._invoke("get_account_info", {})

    async def pair_exists_in_pool(self, sell_instrument: Any, buy_instrument: Any) -> tuple[bool, str]:
        pools = await self.sdk.get_pool_info()
        for pool in pools.pools:
            direct = (
                pool.token_a.id == sell_instrument.id
                and pool.token_a.admin == sell_instrument.admin
                and pool.token_b.id == buy_instrument.id
                and pool.token_b.admin == buy_instrument.admin
            )
            reverse = (
                pool.token_b.id == sell_instrument.id
                and pool.token_b.admin == sell_instrument.admin
                and pool.token_a.id == buy_instrument.id
                and pool.token_a.admin == buy_instrument.admin
            )
            if direct or reverse:
                return True, pool.contract_id
        lines = []
        for p in pools.pools[:30]:
            lines.append(
                f"{p.token_a.id} ({p.token_a.admin}) <-> {p.token_b.id} ({p.token_b.admin})"
            )
        return False, "; ".join(lines) if lines else "no pools returned"


class AutoSwapBot:
    def __init__(self, config: dict[str, Any], logger: logging.Logger) -> None:
        self.config = config
        self.logger = logger
        self.gas = GasOracle(config.get("gas", {}))
        self.adapter = CantexAdapter(logger)
        self.wallet_address = (os.getenv("WALLET_ADDRESS") or "").strip()
        self.executed_trades = 0
        self.waiting_fee_logged = False
        self.waiting_fee_last = Decimal("-1")

    def _read_gas_gate(self) -> dict[str, Any] | None:
        try:
            if not GAS_GATE_PATH.exists():
                return None
            data = json.loads(GAS_GATE_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _write_gas_gate(self, data: dict[str, Any]) -> None:
        GAS_GATE_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    async def _detect_network_fee(self, quote_params: dict[str, Any], trade_cfg: dict[str, Any]) -> Decimal:
        probe = dict(quote_params)
        probe_amount = trade_cfg.get("sell_amount_min", quote_params.get("sell_amount", "1"))
        probe["sell_amount"] = str(probe_amount)
        quote = await self.adapter.get_swap_quote(probe)
        return to_decimal(
            getattr(getattr(getattr(quote, "fees", None), "network_fee", None), "amount", None),
            Decimal("0"),
        )

    async def _wait_gas_gate(self, quote_params: dict[str, Any], trade_cfg: dict[str, Any], interval: int) -> bool:
        max_network_fee = to_decimal(trade_cfg.get("max_network_fee", "999999999"))
        gate_ttl = max(3, min(interval, 15))
        now = time.time()
        gate = self._read_gas_gate()
        if gate and (now - float(gate.get("ts", 0))) <= gate_ttl:
            allow = bool(gate.get("allow", False))
            fee = to_decimal(gate.get("fee", "0"), Decimal("0"))
            if allow:
                self.waiting_fee_logged = False
                self.waiting_fee_last = Decimal("-1")
                return True
            if (not self.waiting_fee_logged) or (abs(fee - self.waiting_fee_last) >= Decimal("0.01")):
                self.logger.info(
                    "TRADE_RESULT | %s -> %s | FAIL | wallet_address=%s | wait_gas | network_fee=%s",
                    self._inst_label(quote_params.get("sell_instrument")),
                    self._inst_label(quote_params.get("buy_instrument")),
                    self.wallet_address,
                    fee,
                )
                self.waiting_fee_logged = True
                self.waiting_fee_last = fee
            return False

        lock_fd = None
        try:
            lock_fd = os.open(str(GAS_GATE_LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            fee = await self._detect_network_fee(quote_params, trade_cfg)
            allow = fee <= max_network_fee
            self._write_gas_gate(
                {
                    "ts": now,
                    "allow": allow,
                    "fee": str(fee),
                    "max_network_fee": str(max_network_fee),
                }
            )
            if allow:
                self.waiting_fee_logged = False
                self.waiting_fee_last = Decimal("-1")
                return True
            if (not self.waiting_fee_logged) or (abs(fee - self.waiting_fee_last) >= Decimal("0.01")):
                self.logger.info(
                    "TRADE_RESULT | %s -> %s | FAIL | wallet_address=%s | wait_gas | network_fee=%s",
                    self._inst_label(quote_params.get("sell_instrument")),
                    self._inst_label(quote_params.get("buy_instrument")),
                    self.wallet_address,
                    fee,
                )
                self.waiting_fee_logged = True
                self.waiting_fee_last = fee
            return False
        except FileExistsError:
            await asyncio.sleep(1.0)
            return False
        finally:
            if lock_fd is not None:
                try:
                    os.close(lock_fd)
                except Exception:
                    pass
                try:
                    os.remove(str(GAS_GATE_LOCK_PATH))
                except Exception:
                    pass

    def _quote_ok(self, quote: Any) -> tuple[bool, str]:
        trade_cfg = self.config.get("trade", {})
        min_out = to_decimal(trade_cfg.get("min_expected_out", "0"))
        max_slippage_bps = to_decimal(trade_cfg.get("max_price_impact_bps", "999999"))
        max_network_fee = to_decimal(trade_cfg.get("max_network_fee", "999999999"))

        returned_amount = to_decimal(getattr(quote, "returned_amount", None))
        slippage = to_decimal(getattr(getattr(quote, "prices", None), "slippage", None))
        network_fee_amount = to_decimal(
            getattr(getattr(getattr(quote, "fees", None), "network_fee", None), "amount", None)
        )
        slippage_bps = slippage * Decimal(10000) if slippage <= Decimal("1") else slippage * Decimal(100)

        if returned_amount < min_out:
            return False, f"returned_amount={returned_amount} < min_expected_out={min_out}"
        if slippage_bps > max_slippage_bps:
            return False, f"slippage_bps={slippage_bps} > max={max_slippage_bps}"
        if network_fee_amount > max_network_fee:
            return False, f"network_fee={network_fee_amount} > max_network_fee={max_network_fee}"
        return True, (
            f"returned_amount={returned_amount}, slippage_bps={slippage_bps}, "
            f"network_fee={network_fee_amount}"
        )

    def _build_reverse_params(self, forward_quote: Any, forward_swap_params: dict[str, Any]) -> dict[str, Any]:
        out_amount = to_decimal(getattr(forward_quote, "returned_amount", None))
        if out_amount <= Decimal("0"):
            raise RuntimeError("Round-trip cannot continue: forward quote returned_amount <= 0")
        return {
            "sell_amount": str(out_amount),
            "sell_instrument": forward_swap_params["buy_instrument"],
            "buy_instrument": forward_swap_params["sell_instrument"],
        }

    def _inst_id(self, raw: Any) -> str:
        if isinstance(raw, dict):
            return str(raw.get("id", "UNKNOWN"))
        val = getattr(raw, "id", None)
        return str(val) if val is not None else "UNKNOWN"

    def _inst_label(self, raw: Any) -> str:
        token = self._inst_id(raw)
        return "CC" if token == "Amulet" else token

    async def _pick_forward_amount(
        self,
        trade_cfg: dict[str, Any],
        default_amount: Any,
        sell_instrument_raw: Any,
    ) -> Decimal:
        use_max_balance = bool(trade_cfg.get("use_max_balance", False))
        if use_max_balance:
            reserve_amount = to_decimal(trade_cfg.get("reserve_amount", "0"), Decimal("0"))
            normalized = self.adapter._normalize_trade_params(
                {
                    "sell_amount": "1",
                    "sell_instrument": sell_instrument_raw,
                    "buy_instrument": sell_instrument_raw,
                }
            )
            target = normalized["sell_instrument"]
            account = await self.adapter.get_account_info()
            balance = Decimal("0")
            for token in getattr(account, "tokens", []):
                inst = getattr(token, "instrument", None)
                if (
                    getattr(inst, "id", None) == target.id
                    and getattr(inst, "admin", None) == target.admin
                ):
                    balance = to_decimal(getattr(token, "unlocked_amount", "0"), Decimal("0"))
                    break
            sellable = balance - reserve_amount
            if sellable <= Decimal("0"):
                raise RuntimeError(
                    f"use_max_balance enabled but sellable balance <= 0 "
                    f"(balance={balance}, reserve_amount={reserve_amount})"
                )
            return sellable

        raw_min = trade_cfg.get("sell_amount_min", default_amount)
        raw_max = trade_cfg.get("sell_amount_max", None)
        amount_min = to_decimal(raw_min, Decimal("0"))
        amount_max = to_decimal(raw_max, amount_min) if raw_max not in (None, "", "null") else amount_min
        if amount_min <= Decimal("0") or amount_max <= Decimal("0"):
            raise RuntimeError("sell_amount_min/sell_amount_max must be > 0")
        if amount_max < amount_min:
            raise RuntimeError("sell_amount_max must be >= sell_amount_min")
        if amount_max == amount_min:
            return amount_min
        sampled = Decimal(str(random.uniform(float(amount_min), float(amount_max))))
        return sampled.quantize(Decimal("0.00000001"))

    async def _execute_leg(
        self,
        *,
        quote_params: dict[str, Any],
        swap_params: dict[str, Any],
        dry_run: bool,
        label: str,
    ) -> tuple[bool, Any]:
        sell_id = self._inst_label(swap_params.get("sell_instrument"))
        buy_id = self._inst_label(swap_params.get("buy_instrument"))
        sell_amount = to_decimal(swap_params.get("sell_amount"), Decimal("0"))
        quote = await self.adapter.get_swap_quote(quote_params)
        ok, reason = self._quote_ok(quote)
        network_fee_amount = to_decimal(
            getattr(getattr(getattr(quote, "fees", None), "network_fee", None), "amount", None)
        )
        max_network_fee = to_decimal(self.config.get("trade", {}).get("max_network_fee", "999999999"))
        waiting_gas = (not ok) and (network_fee_amount > max_network_fee)
        should_log_wait = (not self.waiting_fee_logged) or (abs(network_fee_amount - self.waiting_fee_last) >= Decimal("0.01"))
        if (not waiting_gas) or should_log_wait:
            self.logger.info("%s quote check: %s", label, reason)
        if not ok:
            if waiting_gas:
                # Suppress repetitive logs while waiting for fee(gas) to drop.
                if should_log_wait:
                    self.logger.info(
                        "TRADE_RESULT | %s -> %s | FAIL | wallet_address=%s | wait_gas | network_fee=%s",
                        sell_id,
                        buy_id,
                        self.wallet_address,
                        network_fee_amount,
                    )
                    self.waiting_fee_last = network_fee_amount
                    self.waiting_fee_logged = True
            else:
                self.waiting_fee_logged = False
                self.waiting_fee_last = Decimal("-1")
                self.logger.info(
                    "TRADE_RESULT | %s -> %s | FAIL | wallet_address=%s | sell_amount=%s | %s",
                    sell_id,
                    buy_id,
                    self.wallet_address,
                    sell_amount,
                    reason,
                )
            return False, quote
        returned_amount = to_decimal(getattr(quote, "returned_amount", None))
        self.waiting_fee_logged = False
        self.waiting_fee_last = Decimal("-1")

        if dry_run:
            self.logger.info("DRY RUN -> %s swap params: %s", label, swap_params)
            self.logger.info(
                "TRADE_RESULT | %s -> %s | SUCCESS_DRY_RUN | wallet_address=%s | sell_amount=%s | returned_amount=%s | network_fee=%s",
                sell_id,
                buy_id,
                self.wallet_address,
                sell_amount,
                returned_amount,
                network_fee_amount,
            )
        else:
            result = await self.adapter.swap(swap_params)
            self.logger.info("%s swap executed: %s", label, as_dict(result) or result)
            self.logger.info(
                "TRADE_RESULT | %s -> %s | SUCCESS | wallet_address=%s | sell_amount=%s | returned_amount=%s | network_fee=%s",
                sell_id,
                buy_id,
                self.wallet_address,
                sell_amount,
                returned_amount,
                network_fee_amount,
            )
        self.executed_trades += 1
        return True, quote

    async def run_forever(self) -> None:
        loop_cfg = self.config.get("loop", {})
        trade_cfg = self.config.get("trade", {})
        interval = int(loop_cfg.get("interval_seconds", 15))
        dry_run = bool(trade_cfg.get("dry_run", True))
        roundtrip_enabled = bool(trade_cfg.get("roundtrip_enabled", False))
        max_trades = int(trade_cfg.get("max_trades", 0))
        quote_params = dict(trade_cfg.get("quote_params", {}))
        swap_params = dict(trade_cfg.get("swap_params", {}))

        if not quote_params or not swap_params:
            raise RuntimeError("trade.quote_params and trade.swap_params are required")

        await self.adapter.start()
        if not self.wallet_address:
            try:
                acct = await self.adapter.get_account_info()
                self.wallet_address = str(getattr(acct, "address", "") or "").strip()
            except Exception:
                pass
        self.logger.info(
            "Bot started. dry_run=%s, roundtrip_enabled=%s, max_trades=%s",
            dry_run,
            roundtrip_enabled,
            max_trades if max_trades > 0 else "unlimited",
        )

        # Preflight: avoid endless 404 loop when pair isn't in any pool.
        normalized_quote = self.adapter._normalize_trade_params(quote_params)
        exists, detail = await self.adapter.pair_exists_in_pool(
            normalized_quote["sell_instrument"], normalized_quote["buy_instrument"]
        )
        if not exists:
            self.logger.error(
                "Configured pair not found in pool. Please update config.json instruments. "
                "Available pairs: %s",
                detail,
            )
            await self.adapter.close()
            return
        self.logger.info("Pair validated in pool: %s", detail)

        try:
            while True:
                try:
                    if self.gas.provider not in {"none", "disabled", "off"}:
                        gas_gwei = self.gas.current_gwei()
                        self.logger.info(
                            "Current gas: %.4f gwei (threshold <= %.4f)",
                            float(gas_gwei),
                            float(self.gas.max_gwei),
                        )
                        if gas_gwei > self.gas.max_gwei:
                            await asyncio.sleep(interval)
                            continue
                    else:
                        self.logger.info("Gas check disabled (Canton/Cantex mode)")

                    if max_trades > 0 and self.executed_trades >= max_trades:
                        self.logger.info(
                            "Reached max_trades=%s, stopping bot.",
                            max_trades,
                        )
                        return

                    try:
                        gate_ok = await self._wait_gas_gate(quote_params, trade_cfg, interval)
                        if not gate_ok:
                            await asyncio.sleep(interval)
                            continue

                        forward_amount = await self._pick_forward_amount(
                            trade_cfg,
                            quote_params.get("sell_amount"),
                            quote_params.get("sell_instrument"),
                        )
                    except Exception as exc:
                        sell_id = self._inst_id(quote_params.get("sell_instrument"))
                        buy_id = self._inst_id(quote_params.get("buy_instrument"))
                        self.logger.info(
                            "TRADE_RESULT | %s -> %s | FAIL | wallet_address=%s | %s",
                            sell_id,
                            buy_id,
                            self.wallet_address,
                            str(exc),
                        )
                        await asyncio.sleep(interval)
                        continue
                    cycle_quote_params = dict(quote_params)
                    cycle_swap_params = dict(swap_params)
                    cycle_quote_params["sell_amount"] = str(forward_amount)
                    cycle_swap_params["sell_amount"] = str(forward_amount)
                    self.logger.info("Forward sell_amount selected: %s", forward_amount)

                    ok, forward_quote = await self._execute_leg(
                        quote_params=cycle_quote_params,
                        swap_params=cycle_swap_params,
                        dry_run=dry_run,
                        label="Forward",
                    )
                    if not ok:
                        await asyncio.sleep(interval)
                        continue

                    if roundtrip_enabled:
                        reverse_swap_params = self._build_reverse_params(forward_quote, cycle_swap_params)
                        reverse_quote_params = dict(reverse_swap_params)
                        ok_reverse, _ = await self._execute_leg(
                            quote_params=reverse_quote_params,
                            swap_params=reverse_swap_params,
                            dry_run=dry_run,
                            label="Reverse",
                        )
                        if not ok_reverse:
                            self.logger.warning("Reverse leg skipped due to quote constraints.")

                    self.logger.info("Swap cycle completed. Starting next cycle immediately.")
                    continue
                except asyncio.CancelledError:
                    self.logger.info("Stop signal received. Shutting down bot loop.")
                    break
                except Exception as exc:
                    self.logger.exception("Loop error: %s", exc)
                await asyncio.sleep(interval)
        finally:
            await self.adapter.close()


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Missing config file: {path}")
    with path.open("r", encoding="utf-8") as fp:
        data = json.load(fp)
    if not isinstance(data, dict):
        raise ValueError("config root must be a JSON object")
    return data


async def async_main(args: argparse.Namespace) -> int:
    load_dotenv(Path(args.dotenv))
    cfg = load_config(Path(args.config))
    logger = setup_logger(
        level=str(cfg.get("logging", {}).get("level", "INFO")),
        log_file=cfg.get("logging", {}).get("log_file"),
    )
    bot = AutoSwapBot(cfg, logger)
    await bot.run_forever()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Cantex auto swap bot")
    parser.add_argument("--config", default="config.json", help="Path to config json")
    parser.add_argument("--dotenv", default=".env", help="Path to env file")
    args = parser.parse_args()
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
