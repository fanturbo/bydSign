"""比亚迪小程序 · 多账号批量签到。

账号配置: data/byd_accounts.json（运行时可直接编辑，增删账号即时生效）
  [
    {"name": "账号A", "session_id": "xxxx", "note": "备注(可选)"},
    {"name": "账号B", "session_id": "yyyy"}
  ]

签到状态: data/byd_state.json —— 管理页 tools/byd_admin.py 与它共用，
批量签到跑完（无论手动还是 cron）页面刷新即可看到最新状态。

用法:
  python tools/byd_sign_all.py                # 按顺序签所有账号
  python tools/byd_sign_all.py --name 账号A   # 只重签指定账号
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from byd_app_sign import call, check_session  # noqa: E402

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACCOUNTS_PATH = os.path.join(BASE_DIR, "data", "byd_accounts.json")
STATE_PATH = os.path.join(BASE_DIR, "data", "byd_state.json")


# ---------- 配置与状态读写 ----------

def load_accounts() -> List[Dict[str, Any]]:
    """读账号配置。文件不存在/损坏返回 []，由调用方提示。"""
    try:
        with open(ACCOUNTS_PATH, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError) as exc:
        print(f"[警告] 账号配置读不到: {ACCOUNTS_PATH} ({exc})", file=sys.stderr)
        return []
    accounts = []
    for i, item in enumerate(raw or [], 1):
        if not isinstance(item, dict):
            continue
        sid = str(item.get("session_id") or "").strip()
        if not sid:
            print(f"[警告] 第 {i} 条账号缺 session_id，跳过", file=sys.stderr)
            continue
        accounts.append({
            "name": str(item.get("name") or f"账号{i}").strip(),
            "session_id": sid,
            "note": str(item.get("note") or "").strip(),
        })
    return accounts


def load_accounts_raw() -> List[Dict[str, Any]]:
    """读原始配置列表（不做归一化），供管理页增删改用。"""
    try:
        with open(ACCOUNTS_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        return raw if isinstance(raw, list) else []
    except (OSError, ValueError):
        return []


def save_accounts(accounts: List[Dict[str, Any]]) -> None:
    """把账号配置原子写回磁盘。"""
    os.makedirs(os.path.dirname(ACCOUNTS_PATH), exist_ok=True)
    tmp = ACCOUNTS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(accounts, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ACCOUNTS_PATH)


def load_state() -> Dict[str, Any]:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"updated_at": None, "accounts": {}}


def save_state(state: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


# ---------- 签到 ----------

def sign_one(acc: Dict[str, Any]) -> Dict[str, Any]:
    """签到一个账号，返回写入状态文件的结果条目。

    status 取值:
      ok        签到成功
      duplicate 今日已签过
      failed    接口返回非 200 —— 大概率 session_id 过期，页面上标红
      error     网络异常/响应解不开 —— 不一定是过期，但也标出来
    """
    res: Dict[str, Any] = {
        "date": time.strftime("%Y-%m-%d"),
        "last_run": time.strftime("%Y-%m-%d %H:%M:%S"),
        "duration_days": None,
        "integral": None,
    }
    payload = {
        "session_id": acc["session_id"],
        "app_version": "240",
        "app_client": "mina",
        "date": "",
        "isCard": "0",
    }
    try:
        r = call("activity/sign/signIn", payload)
    except Exception as exc:  # requests 网络异常
        res.update(status="error", ret=None, expired=False,
                   msg=f"网络异常: {exc}")
        return res

    dec = r.get("decrypted")
    if not dec:
        res.update(status="error", ret=None, expired=False,
                   msg=f"响应解不开(HTTP {r.get('status')}): {str(r.get('raw'))[:100]}")
        return res
    try:
        obj = json.loads(dec)
    except ValueError:
        res.update(status="error", ret=None, expired=False, msg=f"响应不是 JSON: {dec[:100]}")
        return res

    ret = obj.get("ret")
    if str(ret) == "200":
        data = obj.get("data") or {}
        dup = bool(data.get("duplicate"))
        res.update(
            status="duplicate" if dup else "ok",
            ret=ret, expired=False,
            duration_days=data.get("durationDays"),
            integral=data.get("integral"),
            msg=obj.get("msg") or "",
        )
    else:
        # 已知过期症状: ret="401" msg="网络繁忙，请稍后重试"。
        # 用小程序启动时的 checkSession 接口复核，把「确认过期」和「其他错误」分开：
        #   10011 = "3rd服务器session过期" → 确认过期
        #   200   = session 还活着，签到失败另有原因（不标红误导换 session）
        msg = obj.get("msg") or dec[:100]
        expired = True
        try:
            ck = json.loads(check_session(acc["session_id"]).get("decrypted") or "{}")
            ck_ret = str(ck.get("ret"))
            if ck_ret == "10011":
                msg = f"session 已过期（checkSession 确认），请重新抓包"
            elif ck_ret == "200":
                expired = False
                msg = f"签到失败但 session 有效（{msg}），稍后重试或反馈"
        except Exception:
            pass  # 复核失败就维持原判：宁可误报也不漏报
        res.update(status="failed", ret=ret, expired=expired, msg=msg)
    return res


def run_all(only_name: Optional[str] = None) -> Dict[str, Any]:
    """批量签到并把结果合并进状态文件。返回最新状态。"""
    accounts = load_accounts()
    state = load_state()
    entries = state.setdefault("accounts", {})

    targets = accounts
    if only_name:
        targets = [a for a in accounts if a["name"] == only_name]
        if not targets:
            raise SystemExit(f"配置里没有名为「{only_name}」的账号")

    for acc in targets:
        print(f"[{acc['name']}] 签到中 ...", flush=True)
        res = sign_one(acc)
        entries[acc["name"]] = res
        print(f"[{acc['name']}] {res['status']} | {res.get('msg') or ''}")
        time.sleep(1)  # 账号之间隔一秒，别打太密

    state["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_state(state)
    return state


def main() -> None:
    ap = argparse.ArgumentParser(description="比亚迪小程序多账号批量签到")
    ap.add_argument("--name", help="只签指定名字的账号（默认全部）")
    args = ap.parse_args()

    if not load_accounts():
        raise SystemExit(f"没有可签到的账号，请先编辑 {ACCOUNTS_PATH}")
    run_all(args.name)


if __name__ == "__main__":
    main()
