from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from decimal import Decimal, ROUND_DOWN
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
UI_VERSION = "2026-04-09-bulk-v2"
CONFIG_PATH = ROOT / "config.json"
DOTENV_PATH = ROOT / ".env"
LOG_PATH = ROOT / "bot.log"
LOG_ARCHIVE_DIR = ROOT / "log_archive"
WALLETS_PATH = ROOT / "wallets.json"
SECRETS_DIR = ROOT / "secrets"
PYTHON_PATH = ROOT / ".venv312" / "Scripts" / "python.exe"
MAIN_PATH = ROOT / "src" / "main.py"

_lock = threading.Lock()
_wallets_lock = threading.Lock()
_procs: dict[str, subprocess.Popen[str]] = {}
_network_fee_cache: dict[str, Any] = {"ts": 0.0, "value": None}


def import_cantex_sdk():
    try:
        return __import__("cantex_sdk")
    except ModuleNotFoundError:
        sdk_src = ROOT.parent / "cantex_sdk" / "src"
        if sdk_src.exists():
            sys.path.insert(0, str(sdk_src))
            try:
                return __import__("cantex_sdk")
            except ModuleNotFoundError as exc:
                raise RuntimeError(
                    "cantex_sdk 依赖不完整（可能缺少 aiohttp 等），请使用 run-ui.ps1 启动。"
                ) from exc
        raise RuntimeError("未找到 cantex_sdk，请检查 D:\\CCnetwork\\cantex_sdk 或虚拟环境安装。")


def load_dotenv() -> None:
    if not DOTENV_PATH.exists():
        return
    for raw in DOTENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"缺少配置文件: {CONFIG_PATH}")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def save_config(data: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_wallets() -> list[dict[str, Any]]:
    if not WALLETS_PATH.exists():
        return []
    data = json.loads(WALLETS_PATH.read_text(encoding="utf-8-sig") or "[]")
    if not isinstance(data, list):
        return []
    return [w for w in data if isinstance(w, dict)]


def save_wallets(wallets: list[dict[str, Any]]) -> None:
    tmp = WALLETS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(wallets, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, WALLETS_PATH)


def load_wallets_locked() -> list[dict[str, Any]]:
    with _wallets_lock:
        return load_wallets()


def wallet_seq(wallet: dict[str, Any], fallback: int) -> int:
    try:
        s = int(wallet.get("seq", fallback))
        return s if s > 0 else fallback
    except Exception:
        return fallback


def next_wallet_seq(wallets: list[dict[str, Any]]) -> int:
    max_seq = 0
    for idx, w in enumerate(wallets, 1):
        max_seq = max(max_seq, wallet_seq(w, idx))
    return max_seq + 1


def keypair_exists(wallets: list[dict[str, Any]], op: str, tr: str) -> bool:
    for w in wallets:
        if str(w.get("operator_key", "")).strip() == op and str(w.get("trading_key", "")).strip() == tr:
            return True
    return False


def mask_key(key: str) -> str:
    s = (key or "").strip()
    if len(s) <= 12:
        return "***"
    return f"{s[:6]}...{s[-4:]}"


def cleanup_procs() -> None:
    dead = []
    for k, p in _procs.items():
        if p.poll() is not None:
            dead.append(k)
    for k in dead:
        _procs.pop(k, None)


def is_running() -> bool:
    with _lock:
        cleanup_procs()
        return bool(_procs)


def running_info() -> list[dict[str, Any]]:
    with _lock:
        cleanup_procs()
        return [{"wallet_id": wid, "pid": p.pid} for wid, p in _procs.items()]


def build_env_for_wallet(wallet: dict[str, Any]) -> dict[str, str]:
    env = os.environ.copy()
    env["CANTEX_OPERATOR_KEY"] = str(wallet.get("operator_key", "")).strip()
    env["CANTEX_TRADING_KEY"] = str(wallet.get("trading_key", "")).strip()
    seq = wallet_seq(wallet, 1)
    env["WALLET_NAME"] = str(seq)
    env["WALLET_ADDRESS"] = str(wallet.get("address", "")).strip()
    return env


def start_bot() -> tuple[bool, str]:
    with _lock:
        cleanup_procs()
        if _procs:
            return False, "机器人已在运行。"
        if not PYTHON_PATH.exists():
            return False, f"未找到 Python: {PYTHON_PATH}"

        wallets = load_wallets_locked()
        wallets = sorted(
            wallets,
            key=lambda w: wallet_seq(w, 10**9),
        )
        active_wallets = [w for w in wallets if not bool(w.get("disabled", False))]
        concurrent_wallets = 0
        try:
            cfg = load_config()
            trade_cfg = cfg.get("trade", {}) if isinstance(cfg, dict) else {}
            concurrent_wallets = int(trade_cfg.get("concurrent_wallets", 0) or 0)
        except Exception:
            concurrent_wallets = 0
        if concurrent_wallets > 0:
            active_wallets = active_wallets[:concurrent_wallets]
        started = 0
        errors: list[str] = []

        if not wallets:
            cmd = [str(PYTHON_PATH), str(MAIN_PATH), "--config", str(CONFIG_PATH), "--dotenv", str(DOTENV_PATH)]
            p = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
            _procs["default"] = p
            started += 1
        else:
            if not active_wallets:
                return False, "所有钱包都已停用，未启动机器人。"
            for w in active_wallets:
                wid = str(w.get("id", "")).strip()
                op = str(w.get("operator_key", "")).strip()
                tr = str(w.get("trading_key", "")).strip()
                if not wid or not op or not tr:
                    errors.append(f"钱包缺少必要字段: {w.get('name', wid or 'unknown')}")
                    continue
                cmd = [str(PYTHON_PATH), str(MAIN_PATH), "--config", str(CONFIG_PATH), "--dotenv", str(DOTENV_PATH)]
                p = subprocess.Popen(
                    cmd,
                    cwd=str(ROOT),
                    env=build_env_for_wallet(w),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
                _procs[wid] = p
                started += 1

        if started == 0:
            return False, "没有成功启动任何机器人。" + ("；" + "；".join(errors) if errors else "")
        # Quick liveness check: catch wallets that crash immediately (bad keys/config).
        time.sleep(1.0)
        cleanup_procs()
        alive = len(_procs)
        if alive == 0:
            return False, "机器人启动后立即退出，请检查钱包私钥格式或配置。"
        msg = f"已启动 {started} 个机器人。"
        if wallets:
            msg += f"（总钱包 {len(wallets)}，本次启动 {len(active_wallets)}，存活 {alive}）"
        if errors:
            msg += " 跳过: " + "；".join(errors)
        return True, msg


def stop_bot() -> tuple[bool, str]:
    with _lock:
        cleanup_procs()
        if not _procs:
            return False, "机器人未运行。"
        n = len(_procs)
        for p in list(_procs.values()):
            p.terminate()
        for p in list(_procs.values()):
            try:
                p.wait(timeout=8)
            except subprocess.TimeoutExpired:
                p.kill()
        _procs.clear()
        return True, f"已停止 {n} 个机器人。"


def read_tail_lines(limit: int = 300) -> str:
    if not LOG_PATH.exists():
        return ""
    lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-limit:])


def archive_and_clear_log() -> str:
    if not LOG_PATH.exists():
        LOG_PATH.write_text("", encoding="utf-8")
        return "日志文件不存在，已创建空日志。"
    content = LOG_PATH.read_text(encoding="utf-8", errors="replace")
    if not content.strip():
        LOG_PATH.write_text("", encoding="utf-8")
        return "日志为空，无需归档。"
    LOG_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    archive_path = LOG_ARCHIVE_DIR / f"bot_{ts}.log"
    archive_path.write_text(content, encoding="utf-8")
    LOG_PATH.write_text("", encoding="utf-8")
    return f"日志已归档到: {archive_path}"


def summarize_trade_results(limit: int = 300) -> str:
    if not LOG_PATH.exists():
        return "暂无交易结果日志。"
    lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    selected = []
    pat = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*\[(?P<wallet>[^\]]+)\]\s+TRADE_RESULT \| "
        r"(?P<sell>[^ ]+) -> (?P<buy>[^ ]+) \| (?P<status>[A-Z_]+)(?: \| (?P<reason>.*))?$"
    )
    def _token_label(v: str) -> str:
        s = (v or "").strip()
        return "CC" if s == "Amulet" else s

    def _to_2dp_down(raw: str) -> str:
        try:
            v = Decimal(str(raw))
            return str(v.quantize(Decimal("0.01"), rounding=ROUND_DOWN))
        except Exception:
            return str(raw)

    amount_pat = re.compile(r"sell_amount=([0-9]+(?:\.[0-9]+)?)\s*\|\s*returned_amount=([0-9]+(?:\.[0-9]+)?)")
    gas_pat = re.compile(r"network_fee=([0-9]+(?:\.[0-9]+)?)")

    for line in lines[-1200:]:
        m = pat.search(line)
        if not m:
            continue
        status = m.group("status")
        if status == "SUCCESS":
            text = "成功"
        elif status == "SUCCESS_DRY_RUN":
            text = "模拟成功"
        else:
            text = "失败"
        reason = (m.group("reason") or "").strip()
        reason = re.sub(r"^\s*wallet_address=[^|]+\|\s*", "", reason)
        reason = re.sub(r"^\s*wallet_address=[^|]+\|\s*", "", reason)
        hhmmss = m.group("ts").split(" ")[-1]
        out = (
            f"{hhmmss} | 钱包 {m.group('wallet')} | "
            f"{_token_label(m.group('sell'))}→{_token_label(m.group('buy'))} | {text}"
        )
        mm = amount_pat.search(reason)
        gm = gas_pat.search(reason)
        if "wait_gas" in reason:
            gas_txt = _to_2dp_down(gm.group(1)) if gm else "-"
            out += f" | gas:{gas_txt}丨等待gas降低中..."
        elif mm:
            out += f" | {_to_2dp_down(mm.group(1))}→{_to_2dp_down(mm.group(2))}"
            if gm:
                out += f" | gas:{_to_2dp_down(gm.group(1))}"
        elif reason:
            out += f" | {reason}"
            if gm:
                out += f" | gas:{_to_2dp_down(gm.group(1))}"
        selected.append(out)
    if not selected:
        return "暂无交易结果日志。"
    return "\n".join(selected[-limit:])


def summarize_wallet_trade_history(wallet_seq: int, limit: int = 20) -> str:
    if wallet_seq <= 0:
        return "钱包序号无效。"
    if not LOG_PATH.exists():
        return "暂无交易记录。"
    lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    selected: list[str] = []
    pat = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*\[(?P<wallet>[^\]]+)\]\s+TRADE_RESULT \| "
        r"(?P<sell>[^ ]+) -> (?P<buy>[^ ]+) \| (?P<status>[A-Z_]+)(?: \| (?P<reason>.*))?$"
    )
    amount_pat = re.compile(r"sell_amount=([0-9]+(?:\.[0-9]+)?)\s*\|\s*returned_amount=([0-9]+(?:\.[0-9]+)?)")
    gas_pat = re.compile(r"network_fee=([0-9]+(?:\.[0-9]+)?)")
    for line in reversed(lines):
        m = pat.search(line)
        if not m:
            continue
        if str(m.group("wallet")).strip() != str(wallet_seq):
            continue
        hhmmss = m.group("ts").split(" ")[-1]
        sell = "CC" if m.group("sell") == "Amulet" else m.group("sell")
        buy = "CC" if m.group("buy") == "Amulet" else m.group("buy")
        status = m.group("status")
        text = "成功" if status == "SUCCESS" else ("模拟成功" if status == "SUCCESS_DRY_RUN" else "失败")
        reason = (m.group("reason") or "").strip()
        item = f"{hhmmss} | 钱包{wallet_seq} | {sell}→{buy} | {text}"
        mm = amount_pat.search(reason)
        gm = gas_pat.search(reason)
        if "wait_gas" in reason:
            gas_txt = gm.group(1) if gm else "-"
            item += f" | gas:{gas_txt}丨等待gas降低中..."
        elif mm:
            item += f" | {mm.group(1)}→{mm.group(2)}"
            if gm:
                item += f" | gas:{gm.group(1)}"
        elif reason:
            item += f" | {reason}"
        selected.append(item)
        if len(selected) >= limit:
            break
    if not selected:
        return f"钱包{wallet_seq} 暂无交易记录。"
    return "\n".join(selected)


def _wallet_history_from_lines(lines: list[str], wallet_seq: int, limit: int) -> list[str]:
    selected: list[str] = []
    pat = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*\[(?P<wallet>[^\]]+)\]\s+TRADE_RESULT \| "
        r"(?P<sell>[^ ]+) -> (?P<buy>[^ ]+) \| (?P<status>[A-Z_]+)(?: \| (?P<reason>.*))?$"
    )
    amount_pat = re.compile(r"sell_amount=([0-9]+(?:\.[0-9]+)?)\s*\|\s*returned_amount=([0-9]+(?:\.[0-9]+)?)")
    gas_pat = re.compile(r"network_fee=([0-9]+(?:\.[0-9]+)?)")
    for line in reversed(lines):
        m = pat.search(line)
        if not m:
            continue
        if str(m.group("wallet")).strip() != str(wallet_seq):
            continue
        hhmmss = m.group("ts").split(" ")[-1]
        sell = "CC" if m.group("sell") == "Amulet" else m.group("sell")
        buy = "CC" if m.group("buy") == "Amulet" else m.group("buy")
        status = m.group("status")
        text = "成功" if status == "SUCCESS" else ("模拟成功" if status == "SUCCESS_DRY_RUN" else "失败")
        reason = (m.group("reason") or "").strip()
        reason = re.sub(r"^\s*wallet_address=[^|]+\|\s*", "", reason)
        item = f"{hhmmss} | 钱包{wallet_seq} | {sell}→{buy} | {text}"
        mm = amount_pat.search(reason)
        gm = gas_pat.search(reason)
        if "wait_gas" in reason:
            gas_txt = gm.group(1) if gm else "-"
            item += f" | gas:{gas_txt}丨等待gas降低中..."
        elif mm:
            item += f" | {mm.group(1)}→{mm.group(2)}"
            if gm:
                item += f" | gas:{gm.group(1)}"
        elif reason:
            item += f" | {reason}"
        selected.append(item)
        if len(selected) >= limit:
            break
    return selected


def _wallet_history_from_lines_by_address(lines: list[str], address: str, limit: int) -> list[str]:
    selected: list[str] = []
    addr_norm = (address or "").strip().lower()
    if not addr_norm:
        return selected
    pat = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*\[(?P<wallet>[^\]]+)\]\s+TRADE_RESULT \| "
        r"(?P<sell>[^ ]+) -> (?P<buy>[^ ]+) \| (?P<status>[A-Z_]+)(?: \| (?P<reason>.*))?$"
    )
    amount_pat = re.compile(r"sell_amount=([0-9]+(?:\.[0-9]+)?)\s*\|\s*returned_amount=([0-9]+(?:\.[0-9]+)?)")
    gas_pat = re.compile(r"network_fee=([0-9]+(?:\.[0-9]+)?)")
    addr_pat = re.compile(r"wallet_address=([^|]+)")
    for line in reversed(lines):
        m = pat.search(line)
        if not m:
            continue
        reason_raw = (m.group("reason") or "").strip()
        am = addr_pat.search(reason_raw)
        if not am:
            continue
        rec_addr = (am.group(1) or "").strip().lower()
        if rec_addr != addr_norm:
            continue
        reason = re.sub(r"^\s*wallet_address=[^|]+\|\s*", "", reason_raw)
        hhmmss = m.group("ts").split(" ")[-1]
        wallet = str(m.group("wallet")).strip()
        sell = "CC" if m.group("sell") == "Amulet" else m.group("sell")
        buy = "CC" if m.group("buy") == "Amulet" else m.group("buy")
        status = m.group("status")
        text = "成功" if status == "SUCCESS" else ("模拟成功" if status == "SUCCESS_DRY_RUN" else "失败")
        item = f"{hhmmss} | 钱包{wallet} | {sell}→{buy} | {text}"
        mm = amount_pat.search(reason)
        gm = gas_pat.search(reason)
        if "wait_gas" in reason:
            gas_txt = gm.group(1) if gm else "-"
            item += f" | gas:{gas_txt}丨等待gas降低中..."
        elif mm:
            item += f" | {mm.group(1)}→{mm.group(2)}"
            if gm:
                item += f" | gas:{gm.group(1)}"
        elif reason:
            item += f" | {reason}"
        selected.append(item)
        if len(selected) >= limit:
            break
    return selected


def summarize_wallet_trade_history_from_archives(wallet_seq: int, limit: int = 20) -> str:
    if wallet_seq <= 0:
        return "钱包序号无效。"
    if limit <= 0:
        limit = 20
    if limit > 500:
        limit = 500

    files: list[Path] = []
    if LOG_PATH.exists():
        files.append(LOG_PATH)
    if LOG_ARCHIVE_DIR.exists():
        archives = sorted(LOG_ARCHIVE_DIR.glob("bot_*.log"), key=lambda p: p.name, reverse=True)
        files.extend(archives)

    if not files:
        return "暂无交易记录。"

    out: list[str] = []
    for fp in files:
        try:
            lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        chunk = _wallet_history_from_lines(lines, wallet_seq, max(1, limit - len(out)))
        out.extend(chunk)
        if len(out) >= limit:
            break

    if not out:
        return f"钱包{wallet_seq} 暂无交易记录。"
    return "\n".join(out[:limit])


def summarize_wallet_trade_history_by_address_from_archives(address: str, limit: int = 20) -> str:
    addr = (address or "").strip()
    if not addr:
        return "地址不能为空。"
    if limit <= 0:
        limit = 20
    if limit > 500:
        limit = 500
    files: list[Path] = []
    if LOG_PATH.exists():
        files.append(LOG_PATH)
    if LOG_ARCHIVE_DIR.exists():
        archives = sorted(LOG_ARCHIVE_DIR.glob("bot_*.log"), key=lambda p: p.name, reverse=True)
        files.extend(archives)
    if not files:
        return "暂无交易记录。"
    out: list[str] = []
    for fp in files:
        try:
            lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        chunk = _wallet_history_from_lines_by_address(lines, addr, max(1, limit - len(out)))
        out.extend(chunk)
        if len(out) >= limit:
            break
    if not out:
        return "该地址暂无交易记录。"
    return "\n".join(out[:limit])


def latest_network_fee_from_log() -> str | None:
    if not LOG_PATH.exists():
        return None
    lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    pat = re.compile(r"network_fee=([0-9]+(?:\.[0-9]+)?)")
    for line in reversed(lines[-400:]):
        m = pat.search(line)
        if m:
            return m.group(1)
    return None


def _extract_chain_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("items", "results", "data", "records", "transfers"):
            val = data.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
    return []


def _pick_first(d: dict[str, Any], keys: list[str], default: str = "-") -> str:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return str(v)
    return default


def _fmt_chain_entry(entry: dict[str, Any], idx: int) -> str:
    ts = _pick_first(
        entry,
        ["ledger_created_at", "created_at", "timestamp", "time", "event_time", "block_time"],
    )
    txid = _pick_first(entry, ["update_id", "transaction_id", "tx_hash", "id", "event_id"])
    typ = _pick_first(entry, ["event_type", "type", "kind", "status"], default="transfer")
    amount = _pick_first(entry, ["amount", "transfer_amount", "quantity"], default="-")
    symbol = _pick_first(entry, ["instrument_symbol", "symbol", "asset", "token"], default="")
    from_party = _pick_first(entry, ["from_party", "sender", "from", "source"], default="-")
    to_party = _pick_first(entry, ["to_party", "receiver", "to", "destination"], default="-")
    tail_txid = txid[-10:] if len(txid) > 10 else txid
    unit = f" {symbol}" if symbol and symbol != "-" else ""
    return f"{idx}. {ts} | {typ} | {amount}{unit} | {from_party} -> {to_party} | tx:{tail_txid}"


def fetch_chain_history_by_address(address: str, limit: int = 20) -> str:
    load_dotenv()
    party = (address or "").strip()
    if not party:
        raise RuntimeError("钱包地址不能为空。")
    api_key = (os.getenv("CCVIEW_API_KEY") or os.getenv("CANTON_EXPLORER_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("未配置链上查询 API Key（请在 .env 添加 CCVIEW_API_KEY）。")
    base = (os.getenv("CCVIEW_BASE_URL") or "https://api.ccview.io").strip().rstrip("/")
    endpoint = os.getenv("CCVIEW_HISTORY_PATH", "/api/v2/token-transfers/by-party").strip()
    if not endpoint.startswith("/"):
        endpoint = "/" + endpoint
    query = urlencode({"party_id": party, "limit": max(1, min(int(limit), 200))})
    req = Request(
        f"{base}{endpoint}?{query}",
        headers={
            "accept": "application/json",
            "x-api-key": api_key,
        },
        method="GET",
    )
    try:
        with urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", "replace")
            payload = json.loads(body) if body else {}
    except Exception as exc:
        msg = str(exc)
        if "403" in msg or "401" in msg:
            raise RuntimeError("链上查询被拒绝（API Key 无效或权限不足）。") from exc
        raise RuntimeError(f"链上查询失败：{msg}") from exc

    items = _extract_chain_items(payload)
    if not items:
        return "链上暂无记录（或地址无匹配交易）。"
    out = [f"地址: {party}", f"最近 {min(limit, len(items))} 笔链上记录："]
    for i, item in enumerate(items[:limit], 1):
        out.append(_fmt_chain_entry(item, i))
    return "\n".join(out)


def _pick_quote_keys() -> tuple[str, str]:
    wallets = sorted(load_wallets_locked(), key=lambda w: wallet_seq(w, 10**9))
    for w in wallets:
        if bool(w.get("disabled", False)):
            continue
        op = str(w.get("operator_key", "")).strip()
        tr = str(w.get("trading_key", "")).strip()
        if op and tr:
            return op, tr
    load_dotenv()
    op = (os.getenv("CANTEX_OPERATOR_KEY") or os.getenv("OPERATOR_PRIVATE_KEY_HEX") or "").strip()
    tr = (os.getenv("CANTEX_TRADING_KEY") or os.getenv("INTENT_TRADING_PRIVATE_KEY_HEX") or "").strip()
    if not op or not tr:
        raise RuntimeError("未找到可用钱包私钥。")
    return op, tr


async def _fetch_live_network_fee_async(cfg: dict[str, Any]) -> str:
    trade_cfg = cfg.get("trade", {}) if isinstance(cfg, dict) else {}
    quote_params = trade_cfg.get("quote_params", {}) if isinstance(trade_cfg, dict) else {}
    sell_amount_raw = quote_params.get("sell_amount", "1")
    sell_inst = quote_params.get("sell_instrument", {})
    buy_inst = quote_params.get("buy_instrument", {})
    sell_id = str(sell_inst.get("id", "")).strip() if isinstance(sell_inst, dict) else ""
    sell_admin = str(sell_inst.get("admin", "")).strip() if isinstance(sell_inst, dict) else ""
    buy_id = str(buy_inst.get("id", "")).strip() if isinstance(buy_inst, dict) else ""
    buy_admin = str(buy_inst.get("admin", "")).strip() if isinstance(buy_inst, dict) else ""
    if not (sell_id and sell_admin and buy_id and buy_admin):
        raise RuntimeError("交易对参数不完整，无法探测网络费。")

    load_dotenv()
    sdk_module = import_cantex_sdk()
    CantexSDK = getattr(sdk_module, "CantexSDK")
    OperatorKeySigner = getattr(sdk_module, "OperatorKeySigner")
    IntentTradingKeySigner = getattr(sdk_module, "IntentTradingKeySigner")
    InstrumentId = getattr(sdk_module, "InstrumentId")
    base_url = os.getenv("CANTEX_BASE_URL") or os.getenv("CANTEX_API_BASE_URL") or "https://api.cantex.io"

    op_hex, tr_hex = _pick_quote_keys()
    operator = OperatorKeySigner.from_hex(op_hex)
    intent = IntentTradingKeySigner.from_hex(tr_hex)
    async with CantexSDK(operator, intent, base_url=base_url, api_key_path=None) as sdk:
        await sdk.authenticate()
        quote = await sdk.get_swap_quote(
            sell_amount=Decimal(str(sell_amount_raw)),
            sell_instrument=InstrumentId(admin=sell_admin, id=sell_id),
            buy_instrument=InstrumentId(admin=buy_admin, id=buy_id),
        )
    fee = getattr(getattr(getattr(quote, "fees", None), "network_fee", None), "amount", None)
    if fee is None:
        raise RuntimeError("quote 未返回 network_fee")
    return str(fee)


def resolve_latest_network_fee(cfg: dict[str, Any], ttl_seconds: float = 10.0) -> str | None:
    now = time.time()
    cached_val = _network_fee_cache.get("value")
    cached_ts = float(_network_fee_cache.get("ts") or 0.0)
    if cached_val and (now - cached_ts) <= ttl_seconds:
        return str(cached_val)
    fee = latest_network_fee_from_log()
    if fee is not None:
        _network_fee_cache["value"] = fee
        _network_fee_cache["ts"] = now
        return fee
    try:
        fee_live = asyncio.run(_fetch_live_network_fee_async(cfg))
        _network_fee_cache["value"] = fee_live
        _network_fee_cache["ts"] = now
        return fee_live
    except Exception:
        return None


def get_live_gas() -> dict[str, Any]:
    cfg = load_config()
    gas_cfg = cfg.get("gas", {}) if isinstance(cfg, dict) else {}
    provider = str(gas_cfg.get("provider", "none")).lower()
    if provider in {"none", "disabled", "off"}:
        return {
            "ok": True,
            "provider": provider,
            "gas_gwei": None,
            "note": "Canton/Cantex 模式下未启用 EVM gas。",
            "latest_network_fee": resolve_latest_network_fee(cfg),
        }
    if provider == "fixed":
        return {
            "ok": True,
            "provider": provider,
            "gas_gwei": str(gas_cfg.get("fixed_gwei", "0")),
            "latest_network_fee": resolve_latest_network_fee(cfg),
        }
    if provider == "eth_rpc":
        rpc = str(gas_cfg.get("eth_rpc_url", "")).strip()
        payload = {"jsonrpc": "2.0", "method": "eth_gasPrice", "params": [], "id": 1}
        req = Request(rpc, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(req, timeout=8) as resp:
            body = resp.read().decode("utf-8")
        result = (json.loads(body) if body else {}).get("result")
        if not result:
            raise RuntimeError("eth_gasPrice 返回为空")
        wei = int(result, 16)
        gwei = wei / (10 ** 9)
        return {
            "ok": True,
            "provider": provider,
            "gas_gwei": f"{gwei:.6f}",
            "latest_network_fee": resolve_latest_network_fee(cfg),
        }
    return {"ok": False, "message": f"不支持的 gas provider: {provider}"}


async def _fetch_pools_async() -> list[dict[str, Any]]:
    load_dotenv()
    sdk_module = import_cantex_sdk()
    CantexSDK = getattr(sdk_module, "CantexSDK")
    OperatorKeySigner = getattr(sdk_module, "OperatorKeySigner")
    IntentTradingKeySigner = getattr(sdk_module, "IntentTradingKeySigner")

    operator_hex = os.getenv("CANTEX_OPERATOR_KEY") or os.getenv("OPERATOR_PRIVATE_KEY_HEX")
    intent_hex = os.getenv("CANTEX_TRADING_KEY") or os.getenv("INTENT_TRADING_PRIVATE_KEY_HEX")
    base_url = os.getenv("CANTEX_BASE_URL") or os.getenv("CANTEX_API_BASE_URL") or "https://api.cantex.io"
    if not operator_hex or not intent_hex:
        raise RuntimeError(".env 缺少 Cantex 密钥。")

    operator = OperatorKeySigner.from_hex(operator_hex)
    intent = IntentTradingKeySigner.from_hex(intent_hex)
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    api_key_path = str((SECRETS_DIR / "ui_pools_api_key.txt").resolve())
    async with CantexSDK(operator, intent, base_url=base_url, api_key_path=api_key_path) as sdk:
        await sdk.authenticate()
        pools_info = await sdk.get_pool_info()
        pools: list[dict[str, Any]] = []
        for p in pools_info.pools:
            pools.append(
                {
                    "contract_id": p.contract_id,
                    "token_a": {"id": p.token_a.id, "admin": p.token_a.admin},
                    "token_b": {"id": p.token_b.id, "admin": p.token_b.admin},
                }
            )
        return pools


def fetch_pools() -> list[dict[str, Any]]:
    return asyncio.run(_fetch_pools_async())


async def _fetch_wallet_snapshot_async(
    operator_hex: str,
    trading_hex: str,
    wallet_id: str,
) -> dict[str, Any]:
    load_dotenv()
    sdk_module = import_cantex_sdk()
    CantexSDK = getattr(sdk_module, "CantexSDK")
    OperatorKeySigner = getattr(sdk_module, "OperatorKeySigner")
    IntentTradingKeySigner = getattr(sdk_module, "IntentTradingKeySigner")
    base_url = os.getenv("CANTEX_BASE_URL") or os.getenv("CANTEX_API_BASE_URL") or "https://api.cantex.io"

    operator = OperatorKeySigner.from_hex(operator_hex)
    intent = IntentTradingKeySigner.from_hex(trading_hex)

    balances = {"CC": "0", "USDC": "0", "CBTC": "0"}
    address = ""
    # For wallet snapshots, always re-authenticate and avoid local API-key cache
    # to prevent credential crossover between wallets.
    async with CantexSDK(operator, intent, base_url=base_url, api_key_path=None) as sdk:
        await sdk.authenticate()
        admin = await sdk.get_account_admin()
        info = await sdk.get_account_info()
        address = str(getattr(admin, "address", "") or getattr(info, "address", ""))
        for token in getattr(info, "tokens", []):
            sym = str(getattr(token, "instrument_symbol", "")).upper()
            amt = str(getattr(token, "unlocked_amount", "0"))
            if sym in {"CC", "AMULET"}:
                balances["CC"] = amt
            elif sym in {"USDC", "USDCX"}:
                balances["USDC"] = amt
            elif sym == "CBTC":
                balances["CBTC"] = amt
    return {"address": address, "balances": balances}


def refresh_wallet_snapshots(wallets: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    updated: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for w in wallets:
        nw = dict(w)
        op = str(nw.get("operator_key", "")).strip()
        tr = str(nw.get("trading_key", "")).strip()
        wid = str(nw.get("id", "")) or f"w{wallet_seq(nw, 0)}"
        seq = wallet_seq(nw, 0)
        if op and tr:
            try:
                snap = asyncio.run(_fetch_wallet_snapshot_async(op, tr, wid))
                nw["address"] = str(snap.get("address", ""))
                nw["balances"] = dict(snap.get("balances", {}))
                nw["refresh_error"] = ""
                results.append({"id": wid, "seq": seq, "ok": True, "error": ""})
            except Exception as exc:
                nw.setdefault("address", "")
                nw.setdefault("balances", {"CC": "-", "USDC": "-", "CBTC": "-"})
                nw["refresh_error"] = str(exc)
                results.append({"id": wid, "seq": seq, "ok": False, "error": str(exc)})
        else:
            nw["refresh_error"] = "缺少私钥"
            results.append({"id": wid, "seq": seq, "ok": False, "error": "缺少私钥"})
        updated.append(nw)
    return updated, results


def short_addr(addr: str) -> str:
    s = (addr or "").strip()
    if not s:
        return "-"
    if len(s) <= 16:
        return s
    return f"{s[:10]}...{s[-8:]}"


def json_response(handler: BaseHTTPRequestHandler, data: dict[str, Any], status: int = 200) -> None:
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
    handler.send_header("Pragma", "no-cache")
    handler.send_header("Expires", "0")
    handler.send_header("X-UI-Version", UI_VERSION)
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Cantex 自动交易面板</title>
<style>
:root { --fg:#e5e7eb; --muted:#94a3b8; --line:#334155; --ok:#16a34a; --warn:#f59e0b; --bad:#dc2626; --pri:#2563eb; }
*{box-sizing:border-box;font-family:"Segoe UI","PingFang SC",sans-serif;} body{margin:0;color:var(--fg);background:radial-gradient(circle at 20% 10%,#1e293b 0,#0b1220 45%,#070b16 100%);} 
.wrap{max-width:1120px;margin:20px auto;padding:0 14px 20px;} .grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:14px;align-items:start;} .col{display:flex;flex-direction:column;gap:14px;min-width:0;}
.card{background:rgba(17,24,39,.92);border:1px solid var(--line);border-radius:14px;padding:12px;width:100%;} h1{margin:0 0 8px;font-size:24px;} h2{margin:0 0 8px;font-size:15px;color:#cbd5e1;} .row{display:grid;grid-template-columns:1fr 1fr;gap:10px;} label{display:block;font-size:12px;color:var(--muted);margin:8px 0 4px;} input,select,textarea{width:100%;background:#020617;color:var(--fg);border:1px solid #334155;border-radius:8px;padding:8px;} textarea{min-height:88px;resize:vertical;} .btns{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap;} button{border:0;padding:8px 12px;border-radius:8px;cursor:pointer;color:#fff;font-weight:600;} .ok{background:var(--ok);} .warn{background:var(--warn);} .bad{background:var(--bad);} .pri{background:var(--pri);} .ghost{background:#1f2937;border:1px solid #475569;color:#e2e8f0;} #status{font-weight:700;} pre{margin:0;background:#020617;border:1px solid #334155;border-radius:8px;padding:10px;height:360px;overflow:auto;white-space:pre-wrap;width:100%;} .wallet-item{display:flex;justify-content:space-between;gap:8px;padding:6px 0;border-bottom:1px dashed #334155;} .tiny{font-size:12px;color:var(--muted);} @media (max-width:900px){.grid{grid-template-columns:1fr;} pre{height:280px;}}
</style></head>
<body><div class="wrap"><h1>Cantex 自动交易 <span class="tiny">(2026-04-09-bulk-v2)</span></h1><div class="grid">
<div class="col">
  <div class="card">
    <h2>运行控制</h2>
    <div>状态：<span id="status">-</span> ｜ 运行实例：<span id="running_count">0</span></div>
    <div><button class="ok" onclick="loadNetworkFee()">获取</button> 当前gas：<span id="network_fee">未获取</span></div>
    <div id="message" style="display:none;"></div>
    <div class="btns"><button class="ok" onclick="startBot()">启动</button><button class="warn" onclick="pauseBot()">暂停</button><button class="bad" onclick="stopBot()">停止</button><button class="ghost" onclick="refreshAll()">刷新状态</button><button class="ghost" onclick="clearLogs()">清空日志</button></div>
  </div>
  <div class="card"><h2>交易结果（按钱包）</h2><pre id="logs"></pre></div>
</div>
<div class="col">
  <div class="card">
    <h2>策略设置</h2>
    <div class="row"><div><label>交易对</label><select id="pair_select"></select></div><div><label>交易方向</label><select id="direction"><option value="A_TO_B">A → B</option><option value="B_TO_A">B → A</option></select></div></div>
    <div class="row"><div><label>交易数量（随机区间）</label><div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;"><input id="sell_amount_min" placeholder="最小值" /><input id="sell_amount_max" placeholder="最大值（可空）" /></div></div><div><label>自动交易次数（max_trades，0=无限）</label><input id="max_trades" /></div></div>
    <div class="row"><div><label>并发钱包数（concurrent_wallets，0=全部）</label><input id="concurrent_wallets" /></div><div><label>往返交易（roundtrip_enabled）</label><select id="roundtrip_enabled"><option value="false">关闭</option><option value="true">开启</option></select></div></div>
    <div class="row"><div><label>全仓模式（use_max_balance）</label><select id="use_max_balance"><option value="false">关闭</option><option value="true">开启（按余额MAX）</option></select></div><div><label>演练模式（dry_run）</label><select id="dry_run"><option value="true">开启（不下真实单）</option><option value="false">关闭（真实交易）</option></select></div></div>
    <div class="row"><div><label>保留数量（reserve_amount）</label><input id="reserve_amount" /></div><div><label>最大网络费（max_network_fee）</label><input id="max_network_fee" /></div></div>
    <div class="row"><div><label>最大滑点（%）</label><input id="max_price_impact_pct" /></div><div><label>轮询间隔秒（interval_seconds）</label><input id="interval_seconds" /></div></div>
    <div class="btns"><button class="pri" onclick="saveConfig()">保存配置</button><span id="save_notice" class="tiny"></span></div>
  </div>
</div>
</div>
<div class="card" style="margin-top:14px;">
  <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;">
    <h2 style="margin:0;">钱包管理（增删钱包/停用）</h2>
    <div class="btns" style="margin-top:0;">
      <button class="ghost" onclick="queryHistoryByAddress()">交易记录查询</button>
      <button class="ghost" onclick="refreshWalletSnapshots()">刷新地址与余额</button>
      <button class="warn" onclick="setAllWalletDisabled(true)">全停用</button>
      <button class="ok" onclick="setAllWalletDisabled(false)">全启用</button>
      <button class="bad" onclick="deleteAllWallets()">全删除</button>
    </div>
  </div>
  <div class="tiny">当前钱包：<span id="wallet_current">无</span></div>
  <div class="tiny">说明：停用后，该钱包不会参与交易。</div>
  <div id="wallet_list"></div>
  <label style="margin-top:12px;">批量添加（每行一个钱包：`操作员私钥 空格/Tab 交易私钥 [空格/Tab 注释]`）</label>
  <textarea id="wallet_batch" placeholder="op_hex_1 tr_hex_1 主钱包&#10;op_hex_2<TAB>tr_hex_2<TAB>测试钱包"></textarea>
  <div class="btns"><button class="pri" onclick="batchAddWallets()">批量添加钱包</button></div>
</div>
</div></div>
<script>
let POOLS=[];
let walletRefreshInFlight=false;
let walletRetryTimer=null;
let serverLogText='';
let uiLocalLogLines=[];
async function jget(url){const r=await fetch(url);return await r.json();}
async function jpost(url,body){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});return await r.json();}
function msg(t){if(t)pushLogLine(t);}
function renderLogs(){const el=document.getElementById('logs');if(!el)return;const parts=[];if(uiLocalLogLines.length){parts.push(uiLocalLogLines.join('\\n'));}if(serverLogText){parts.push(serverLogText);}el.textContent=parts.join(parts.length===2?'\\n':'');}
function pushLogLine(t){if(!t)return;const now=new Date();const hh=String(now.getHours()).padStart(2,'0');const mm=String(now.getMinutes()).padStart(2,'0');const ss=String(now.getSeconds()).padStart(2,'0');const line=`${hh}:${mm}:${ss} | ${t}`;uiLocalLogLines.unshift(line);if(uiLocalLogLines.length>200){uiLocalLogLines=uiLocalLogLines.slice(0,200);}renderLogs();}
function scheduleWalletRetry(){if(walletRetryTimer)return;walletRetryTimer=setTimeout(async()=>{walletRetryTimer=null;await refreshWalletSnapshots(true);},4000);}
function setSaveNotice(t){const el=document.getElementById('save_notice');if(!el)return;el.textContent=t||'';if(t){setTimeout(()=>{if(el.textContent===t)el.textContent='';},2500);}}
function fmtAmt(v){if(v===null||v===undefined)return '-';const s=String(v).trim();if(!s||s==='-')return '-';const n=Number(s);if(!Number.isFinite(n))return s;return n.toFixed(3);}
async function copyText(t,onDone){
if(!t){if(typeof onDone==='function'){onDone(false);}return;}
try{
  if(navigator.clipboard&&typeof navigator.clipboard.writeText==='function'){
    await navigator.clipboard.writeText(t);if(typeof onDone==='function'){onDone(true);}return;
  }
  const ta=document.createElement('textarea');
  ta.value=t;ta.setAttribute('readonly','');
  ta.style.position='fixed';ta.style.opacity='0';ta.style.pointerEvents='none';
  document.body.appendChild(ta);ta.select();
  const ok=document.execCommand('copy');
  document.body.removeChild(ta);
  if(typeof onDone==='function'){onDone(!!ok);}
}catch(e){if(typeof onDone==='function'){onDone(false);}}
}
function setStatus(d){const el=document.getElementById('status');el.textContent=d.running?'运行中':'已停止';el.style.color=d.running?'#16a34a':'#f59e0b';document.getElementById('running_count').textContent=d.running_count||0;}
function tokenLabel(id){return id==='Amulet'?'CC':id;}
function pairLabel(p){return `${tokenLabel(p.token_a.id)} <-> ${tokenLabel(p.token_b.id)}`;}
function instEq(a,b){return a&&b&&a.id===b.id&&a.admin===b.admin;}
function fillPools(pools){POOLS=pools||[];const sel=document.getElementById('pair_select');sel.innerHTML='';for(let i=0;i<POOLS.length;i++){const o=document.createElement('option');o.value=String(i);o.textContent=pairLabel(POOLS[i]);sel.appendChild(o);}}
function syncFromConfig(cfg){
  document.getElementById('dry_run').value=String(!!cfg.trade.dry_run);
  document.getElementById('roundtrip_enabled').value=String(!!cfg.trade.roundtrip_enabled);
  document.getElementById('interval_seconds').value=cfg.loop.interval_seconds??15;
  document.getElementById('max_network_fee').value=cfg.trade.max_network_fee??'1';
  document.getElementById('max_price_impact_pct').value=((Number(cfg.trade.max_price_impact_bps??200))/100).toString();
  const baseAmt=(cfg.trade.quote_params&&cfg.trade.quote_params.sell_amount)?cfg.trade.quote_params.sell_amount:'1';
  document.getElementById('sell_amount_min').value=(cfg.trade.sell_amount_min??baseAmt);
  document.getElementById('sell_amount_max').value=(cfg.trade.sell_amount_max??'');
  document.getElementById('max_trades').value=cfg.trade.max_trades??0;
  document.getElementById('concurrent_wallets').value=cfg.trade.concurrent_wallets??0;
  document.getElementById('use_max_balance').value=String(!!cfg.trade.use_max_balance);
  document.getElementById('reserve_amount').value=cfg.trade.reserve_amount??'0';
  const s=cfg.trade.quote_params.sell_instrument,b=cfg.trade.quote_params.buy_instrument;
  let found=false;
  for(let i=0;i<POOLS.length;i++){
    const p=POOLS[i];
    if(instEq(s,p.token_a)&&instEq(b,p.token_b)){document.getElementById('pair_select').value=String(i);document.getElementById('direction').value='A_TO_B';found=true;break;}
    if(instEq(s,p.token_b)&&instEq(b,p.token_a)){document.getElementById('pair_select').value=String(i);document.getElementById('direction').value='B_TO_A';found=true;break;}
  }
  if(!found&&POOLS.length>0){document.getElementById('pair_select').value='0';}
}
async function loadConfig(){const d=await jget('/api/config');if(!d.ok){msg(d.message);return null;}return d.config;}
async function loadPools(){const d=await jget('/api/pools');if(!d.ok){msg(d.message);return;}fillPools(d.pools);const cfg=await loadConfig();if(cfg)syncFromConfig(cfg);}
async function saveConfig(){
  try{
    const cfgRes=await jget('/api/config');
    if(!cfgRes.ok){msg(cfgRes.message);return;}
    const cfg=cfgRes.config;
    if(!POOLS.length){msg('没有可用交易对，请检查网络后刷新页面。');return;}
    const idx=Number(document.getElementById('pair_select').value||0);
    const dir=document.getElementById('direction').value;
    const pair=POOLS[idx];
    const sell=dir==='A_TO_B'?pair.token_a:pair.token_b;
    const buy=dir==='A_TO_B'?pair.token_b:pair.token_a;
    const amountMin=String(document.getElementById('sell_amount_min').value||'1');
    const amountMaxRaw=String(document.getElementById('sell_amount_max').value||'').trim();
    cfg.trade.dry_run=document.getElementById('dry_run').value==='true';
    cfg.trade.roundtrip_enabled=document.getElementById('roundtrip_enabled').value==='true';
    cfg.trade.max_trades=Number(document.getElementById('max_trades').value||0);
    cfg.trade.concurrent_wallets=Number(document.getElementById('concurrent_wallets').value||0);
    cfg.trade.use_max_balance=document.getElementById('use_max_balance').value==='true';
    cfg.trade.reserve_amount=String(document.getElementById('reserve_amount').value||'0');
    cfg.loop.interval_seconds=Number(document.getElementById('interval_seconds').value||15);
    cfg.trade.max_network_fee=String(document.getElementById('max_network_fee').value||'1');
    cfg.trade.max_price_impact_bps=Math.round((Number(document.getElementById('max_price_impact_pct').value||'2'))*100);
    cfg.trade.sell_amount_min=amountMin;
    cfg.trade.sell_amount_max=amountMaxRaw||null;
    cfg.trade.quote_params.sell_amount=amountMin;
    cfg.trade.swap_params.sell_amount=amountMin;
    cfg.trade.quote_params.sell_instrument=sell;
    cfg.trade.quote_params.buy_instrument=buy;
    cfg.trade.swap_params.sell_instrument=sell;
    cfg.trade.swap_params.buy_instrument=buy;
    const r=await jpost('/api/config',cfg);
    if(r.ok){setSaveNotice('配置已保存。');msg('');}else{msg(r.message||'保存失败');}
  }catch(e){msg('保存失败：'+e);}
}
async function loadStatus(){const d=await jget('/api/status');setStatus(d);} 
async function loadNetworkFee(){const d=await jget('/api/gas');if(!d.ok){document.getElementById('network_fee').textContent='读取失败';msg(d.message||'读取网络费失败');return;}document.getElementById('network_fee').textContent=d.latest_network_fee||'待获取';}
async function loadLogs(){const d=await jget('/api/logs');serverLogText=d.logs||'';renderLogs();}
async function clearLogs(){await jpost('/api/logs/clear',{});uiLocalLogLines=[];await loadLogs();}
async function startBot(){await jpost('/api/logs/clear',{});await jpost('/api/start',{});await refreshAll();}
async function pauseBot(){await jpost('/api/pause',{});await refreshAll();}
async function stopBot(){await jpost('/api/stop',{});await refreshAll();}
async function viewWalletHistory(seq){const d=await jget(`/api/wallets/history?seq=${encodeURIComponent(String(seq))}&limit=20`);const el=document.getElementById('wallet_history');if(!el)return;el.textContent=d&&d.ok?(d.history||`钱包${seq} 暂无交易记录。`):(d.message||'查询失败');}
async function queryHistoryByAddress(){const raw=prompt('请输入钱包地址（完整地址）');if(raw===null)return;const address=String(raw||'').trim();if(!address){msg('请输入有效钱包地址。');return;}const d=await jget(`/api/wallets/history_by_address?address=${encodeURIComponent(address)}&limit=20`);const text=d&&d.ok?(d.history||'暂无交易记录。'):(d.message||'查询失败');alert(text);}
async function loadWallets(){const d=await jget('/api/wallets');const box=document.getElementById('wallet_list');const cur=document.getElementById('wallet_current');if(!d.ok){box.textContent=d.message||'读取钱包失败';cur.textContent='读取失败';return;}if(!d.wallets.length){box.innerHTML='<div class="tiny">当前未配置钱包，将使用 .env 默认密钥。</div>';cur.textContent='无（使用 .env 默认）';return;}box.innerHTML='';const active=d.wallets.filter(w=>!w.disabled).map(w=>w.display_name);const paused=d.wallets.filter(w=>w.disabled).map(w=>w.display_name);cur.textContent=`启用: ${active.length?active.join('、'):'无'}；停用: ${paused.length?paused.join('、'):'无'}`;let hasRefreshError=false;for(const w of d.wallets){const row=document.createElement('div');row.className='wallet-item';const comment=w.comment?` | 备注: ${w.comment}`:'';const err=w.refresh_error?` | 获取失败，自动重试中`:'';
if(w.refresh_error){hasRefreshError=true;}
const addr=w.address_short||'-';const bal=w.balances||{};row.innerHTML=`<div><div>${w.display_name}</div><div class="tiny">地址: ${addr} | 余额 CC:${fmtAmt(bal.CC)} USDC:${fmtAmt(bal.USDC)} CBTC:${fmtAmt(bal.CBTC)}${comment}${err}</div><div class="tiny">operator: ${w.operator_key_mask} | trading: ${w.trading_key_mask}</div></div>`;const tools=document.createElement('div');tools.style.display='flex';tools.style.gap='6px';tools.style.alignItems='center';const cpbtn=document.createElement('button');cpbtn.className='ghost';cpbtn.textContent='复制地址';const cpnote=document.createElement('span');cpnote.className='tiny';cpnote.style.minWidth='42px';cpnote.textContent='';cpbtn.onclick=()=>copyText(w.address||'',(ok)=>{cpnote.textContent=ok?'已复制':'复制失败';setTimeout(()=>{cpnote.textContent='';},1800);});const cbtn=document.createElement('button');cbtn.className=w.disabled?'warn':'ok';cbtn.textContent=w.disabled?'已停用':'已启用';cbtn.onclick=()=>setWalletDisabled(w.id,!w.disabled);const dbtn=document.createElement('button');dbtn.className='bad';dbtn.textContent='删除';dbtn.onclick=()=>deleteWallet(w.id);tools.appendChild(cpnote);tools.appendChild(cpbtn);tools.appendChild(cbtn);tools.appendChild(dbtn);row.appendChild(tools);box.appendChild(row);} if(hasRefreshError){scheduleWalletRetry();}}
async function batchAddWallets(){const text=document.getElementById('wallet_batch').value.trim();if(!text){msg('请先粘贴批量钱包内容。');return;}const r=await jpost('/api/wallets/batch_add',{text});msg(r.message||'');if(r.ok){document.getElementById('wallet_batch').value='';await loadWallets();await refreshWalletSnapshots();}}
async function deleteWallet(id){const r=await jpost('/api/wallets/delete',{id});msg(r.message||'');await loadWallets();}
async function setWalletDisabled(id,toDisabled){const r=await jpost('/api/wallets/comment',{id,disabled:toDisabled});if(r&&r.message)pushLogLine(r.message);await loadWallets();}
async function setAllWalletDisabled(toDisabled){const r=await jpost('/api/wallets/set_all_disabled',{disabled:toDisabled});if(r&&r.message)pushLogLine(r.message);await loadWallets();}
async function deleteAllWallets(){if(!confirm('确认删除全部钱包？此操作不可恢复。'))return;const r=await jpost('/api/wallets/delete_all',{});if(r&&r.message)pushLogLine(r.message);await loadWallets();}
async function refreshWalletSnapshots(silent=false){if(walletRefreshInFlight)return;walletRefreshInFlight=true;try{const r=await jget('/api/wallets/refresh');if(!silent){msg(r.message||'');}await loadWallets();}finally{walletRefreshInFlight=false;}}
async function refreshAll(){await Promise.all([loadStatus(),loadLogs()]);}
loadPools();loadWallets();refreshAll();setTimeout(()=>refreshWalletSnapshots(true),500);setInterval(refreshAll,3000);setInterval(()=>refreshWalletSnapshots(true),30000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        split = urlsplit(self.path)
        path = split.path
        query = parse_qs(split.query or "")

        if path == "/":
            body = HTML.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("X-UI-Version", UI_VERSION)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/version":
            json_response(
                self,
                {
                    "ok": True,
                    "ui_version": UI_VERSION,
                    "pid": os.getpid(),
                    "cwd": str(ROOT),
                },
            )
            return

        if path == "/api/status":
            info = running_info()
            json_response(self, {"ok": True, "running": len(info) > 0, "running_count": len(info), "instances": info})
            return

        if path == "/api/gas":
            try:
                json_response(self, get_live_gas())
            except Exception as exc:
                json_response(self, {"ok": False, "message": str(exc)}, status=500)
            return

        if path == "/api/wallets":
            wallets = sorted(load_wallets_locked(), key=lambda w: wallet_seq(w, 10**9))
            shaped = []
            for idx, w in enumerate(wallets, 1):
                seq = wallet_seq(w, idx)
                address = str(w.get("address", ""))
                balances_raw = w.get("balances", {})
                balances = {"CC": "-", "USDC": "-", "CBTC": "-"}
                if isinstance(balances_raw, dict):
                    balances["CC"] = str(balances_raw.get("CC", "-"))
                    balances["USDC"] = str(balances_raw.get("USDC", "-"))
                    balances["CBTC"] = str(balances_raw.get("CBTC", "-"))
                shaped.append(
                    {
                        "id": w.get("id"),
                        "seq": seq,
                        "display_name": f"钱包{seq}",
                        "comment": str(w.get("comment", "")),
                        "disabled": bool(w.get("disabled", False)),
                        "address": address,
                        "address_short": short_addr(address),
                        "balances": balances,
                        "refresh_error": str(w.get("refresh_error", "")),
                        "operator_key_mask": mask_key(str(w.get("operator_key", ""))),
                        "trading_key_mask": mask_key(str(w.get("trading_key", ""))),
                    }
                )
            json_response(self, {"ok": True, "wallets": shaped})
            return

        if path == "/api/wallets/history":
            try:
                seq_raw = (query.get("seq", ["0"])[0] or "0").strip()
                limit_raw = (query.get("limit", ["20"])[0] or "20").strip()
                seq = int(seq_raw)
                limit = int(limit_raw)
                if limit <= 0:
                    limit = 20
                if limit > 200:
                    limit = 200
                history = summarize_wallet_trade_history(seq, limit)
                json_response(self, {"ok": True, "seq": seq, "limit": limit, "history": history})
            except Exception as exc:
                json_response(self, {"ok": False, "message": str(exc)}, status=400)
            return

        if path == "/api/wallets/history_by_address":
            try:
                addr_raw = (query.get("address", [""])[0] or "").strip()
                limit_raw = (query.get("limit", ["20"])[0] or "20").strip()
                if not addr_raw:
                    raise ValueError("address 不能为空。")
                limit = int(limit_raw)
                if limit <= 0:
                    limit = 20
                if limit > 200:
                    limit = 200
                history = summarize_wallet_trade_history_by_address_from_archives(addr_raw, limit)
                # Backward compatibility: old logs may not contain wallet_address field.
                if history in {"该地址暂无交易记录。", "暂无交易记录。"}:
                    norm_addr = addr_raw.lower()
                    wallets = sorted(load_wallets_locked(), key=lambda w: wallet_seq(w, 10**9))
                    matched_seq = 0
                    for idx, w in enumerate(wallets, 1):
                        seq = wallet_seq(w, idx)
                        w_addr = str(w.get("address", "")).strip()
                        if w_addr and w_addr.lower() == norm_addr:
                            matched_seq = seq
                            break
                    if matched_seq > 0:
                        history = summarize_wallet_trade_history_from_archives(matched_seq, limit)
                json_response(
                    self,
                    {"ok": True, "limit": limit, "address": addr_raw, "history": history},
                )
            except Exception as exc:
                json_response(self, {"ok": False, "message": str(exc)}, status=400)
            return

        if path == "/api/wallets/refresh":
            try:
                base_wallets = sorted(load_wallets_locked(), key=lambda w: wallet_seq(w, 10**9))
                refreshed_wallets, results = refresh_wallet_snapshots(base_wallets)
                refreshed_by_id = {str(w.get("id", "")): w for w in refreshed_wallets}
                with _wallets_lock:
                    latest_wallets = load_wallets()
                    merged: list[dict[str, Any]] = []
                    for w in latest_wallets:
                        wid = str(w.get("id", ""))
                        rw = refreshed_by_id.get(wid)
                        if rw is None:
                            merged.append(w)
                            continue
                        merged.append(
                            {
                                **w,
                                "address": rw.get("address", w.get("address", "")),
                                "balances": rw.get("balances", w.get("balances", {"CC": "-", "USDC": "-", "CBTC": "-"})),
                                "refresh_error": rw.get("refresh_error", ""),
                            }
                        )
                    save_wallets(merged)
                ok_count = sum(1 for r in results if bool(r.get("ok")))
                fail_items = [r for r in results if not bool(r.get("ok"))]
                msg = f"刷新完成：成功 {ok_count}，失败 {len(fail_items)}。"
                if fail_items:
                    snippets = []
                    for item in fail_items[:3]:
                        snippets.append(f"钱包{item.get('seq', '?')}：{item.get('error', '未知错误')}")
                    msg += " " + "；".join(snippets)
                json_response(self, {"ok": True, "message": msg, "results": results})
            except Exception as exc:
                json_response(self, {"ok": False, "message": str(exc)}, status=500)
            return

        if path == "/api/config":
            try:
                json_response(self, {"ok": True, "config": load_config()})
            except Exception as exc:
                json_response(self, {"ok": False, "message": str(exc)}, status=500)
            return

        if path == "/api/pools":
            try:
                pools = fetch_pools()
                json_response(self, {"ok": True, "pools": pools})
            except Exception as exc:
                json_response(self, {"ok": False, "message": str(exc)}, status=500)
            return

        if path == "/api/logs":
            json_response(self, {"ok": True, "logs": summarize_trade_results(300)})
            return

        json_response(self, {"ok": False, "message": "未找到接口。"}, status=404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b"{}"
        payload = json.loads(raw.decode("utf-8") or "{}")
        path = urlsplit(self.path).path

        if path == "/api/start":
            ok, message = start_bot()
            json_response(self, {"ok": ok, "message": message}, status=200 if ok else 400)
            return

        if path == "/api/stop":
            ok, message = stop_bot()
            json_response(self, {"ok": ok, "message": message}, status=200 if ok else 400)
            return

        if path == "/api/pause":
            ok, message = stop_bot()
            json_response(self, {"ok": ok, "message": message}, status=200 if ok else 400)
            return

        if path == "/api/wallets/add":
            try:
                op = str(payload.get("operator_key", "")).strip()
                tr = str(payload.get("trading_key", "")).strip()
                comment = str(payload.get("comment", "")).strip()
                if not op or not tr:
                    raise ValueError("请填写两把私钥。")
                with _wallets_lock:
                    wallets = load_wallets()
                    if keypair_exists(wallets, op, tr):
                        raise ValueError("该钱包已存在（私钥组合重复）。")
                    seq = next_wallet_seq(wallets)
                    wid = f"w{seq}"
                    while any(str(x.get("id")) == wid for x in wallets):
                        wid = f"w{seq}_{os.urandom(2).hex()}"
                    wallets.append(
                        {
                            "id": wid,
                            "seq": seq,
                            "operator_key": op,
                            "trading_key": tr,
                            "comment": comment,
                            "address": "",
                            "balances": {"CC": "-", "USDC": "-", "CBTC": "-"},
                        }
                    )
                    save_wallets(wallets)
                json_response(self, {"ok": True, "message": "钱包已添加。"})
            except Exception as exc:
                json_response(self, {"ok": False, "message": str(exc)}, status=400)
            return

        if path == "/api/wallets/batch_add":
            try:
                text = str(payload.get("text", "")).strip()
                if not text:
                    raise ValueError("批量内容不能为空。")
                added = 0
                skipped = 0
                details: list[str] = []
                with _wallets_lock:
                    wallets = load_wallets()
                    for i, raw_line in enumerate(text.splitlines(), 1):
                        line = raw_line.strip()
                        if not line:
                            continue
                        parts = [x.strip() for x in re.split(r"[ \t]+", line, maxsplit=2)]
                        if len(parts) < 2:
                            skipped += 1
                            details.append(f"第{i}行格式错误")
                            continue
                        op, tr = parts[0], parts[1]
                        comment = parts[2] if len(parts) >= 3 else ""
                        if not op or not tr:
                            skipped += 1
                            details.append(f"第{i}行字段缺失")
                            continue
                        if keypair_exists(wallets, op, tr):
                            skipped += 1
                            details.append(f"第{i}行重复钱包")
                            continue
                        seq = next_wallet_seq(wallets)
                        wid = f"w{seq}"
                        while any(str(x.get("id")) == wid for x in wallets):
                            wid = f"w{seq}_{os.urandom(2).hex()}"
                        wallets.append(
                            {
                                "id": wid,
                                "seq": seq,
                                "operator_key": op,
                                "trading_key": tr,
                                "comment": comment,
                                "address": "",
                                "balances": {"CC": "-", "USDC": "-", "CBTC": "-"},
                            }
                        )
                        added += 1
                    save_wallets(wallets)
                msg = f"批量添加完成：新增 {added} 个，跳过 {skipped} 个。"
                if details:
                    msg += " " + "；".join(details[:6])
                json_response(self, {"ok": True, "message": msg})
            except Exception as exc:
                json_response(self, {"ok": False, "message": str(exc)}, status=400)
            return

        if path == "/api/wallets/comment":
            try:
                wid = str(payload.get("id", "")).strip()
                has_comment = "comment" in payload
                comment = str(payload.get("comment", "")).strip()
                disabled = bool(payload.get("disabled", False))
                with _wallets_lock:
                    wallets = load_wallets()
                    found = False
                    for w in wallets:
                        if str(w.get("id")) == wid:
                            if has_comment:
                                w["comment"] = comment
                            w["disabled"] = disabled
                            found = True
                            break
                    if found:
                        save_wallets(wallets)
                if not found:
                    raise ValueError("未找到该钱包。")
                json_response(self, {"ok": True, "message": "钱包状态已更新。"})
            except Exception as exc:
                json_response(self, {"ok": False, "message": str(exc)}, status=400)
            return

        if path == "/api/wallets/set_all_disabled":
            try:
                disabled = bool(payload.get("disabled", False))
                with _wallets_lock:
                    wallets = load_wallets()
                    for w in wallets:
                        w["disabled"] = disabled
                    save_wallets(wallets)
                json_response(
                    self,
                    {
                        "ok": True,
                        "message": f"已{'停用' if disabled else '启用'}全部钱包（{len(wallets)}个）。",
                    },
                )
            except Exception as exc:
                json_response(self, {"ok": False, "message": str(exc)}, status=400)
            return

        if path == "/api/wallets/delete_all":
            try:
                with _wallets_lock:
                    wallets = load_wallets()
                    n = len(wallets)
                    save_wallets([])
                json_response(self, {"ok": True, "message": f"已删除全部钱包（{n}个）。"})
            except Exception as exc:
                json_response(self, {"ok": False, "message": str(exc)}, status=400)
            return

        if path == "/api/wallets/delete":
            try:
                wid = str(payload.get("id", "")).strip()
                with _wallets_lock:
                    wallets = load_wallets()
                    new_wallets = [w for w in wallets if str(w.get("id")) != wid]
                    if len(new_wallets) == len(wallets):
                        raise ValueError("未找到该钱包。")
                    save_wallets(new_wallets)
                json_response(self, {"ok": True, "message": "钱包已删除。"})
            except Exception as exc:
                json_response(self, {"ok": False, "message": str(exc)}, status=400)
            return

        if path == "/api/config":
            try:
                if not isinstance(payload, dict):
                    raise ValueError("配置必须是 JSON 对象。")
                save_config(payload)
                json_response(self, {"ok": True, "message": "配置已保存。"})
            except Exception as exc:
                json_response(self, {"ok": False, "message": str(exc)}, status=400)
            return

        if path == "/api/logs/clear":
            try:
                message = archive_and_clear_log()
                json_response(self, {"ok": True, "message": message})
            except Exception as exc:
                json_response(self, {"ok": False, "message": str(exc)}, status=500)
            return

        json_response(self, {"ok": False, "message": "未找到接口。"}, status=404)

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> int:
    host = os.getenv("UI_HOST", "0.0.0.0")
    preferred_port = int(os.getenv("UI_PORT", "39087"))
    server = None
    bound_port = None
    for port in (preferred_port, preferred_port + 1, preferred_port + 2, 8787, 18787, 28080):
        try:
            server = ThreadingHTTPServer((host, port), Handler)
            bound_port = port
            break
        except OSError:
            continue
    if server is None or bound_port is None:
        raise RuntimeError("无法绑定 UI 端口。")

    lan_ip = "127.0.0.1"
    try:
        lan_ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        pass

    print(f"UI started: http://127.0.0.1:{bound_port}")
    print(f"LAN URL   : http://{lan_ip}:{bound_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            stop_bot()
        except Exception:
            pass
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
