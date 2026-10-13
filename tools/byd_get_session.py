"""从抓包的 curl 或加密 body 中提取比亚迪账号的 session_id。

换账号流程:
  1. 用新微信账号登录 PC 微信, 打开一次比亚迪小程序(触发登录, 产生请求)
  2. 用代理(mitmproxy/Charles)抓一条发往 weixin90.bydauto.com.cn 的请求, 复制其 curl
  3. 本工具解密 body, 输出其中的 session_id

用法:
  # 方式一: 直接粘贴整条 curl(用引号包住)
  python tools/byd_get_session.py --curl "curl -X POST ... -d '\"密文\"'"

  # 方式二: 只给加密后的 body 字符串
  python tools/byd_get_session.py --body "Qp/HkOzD..."
"""
from __future__ import annotations

import argparse
import json
import re
import sys

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from byd_app_sign import aes_decrypt_b64  # noqa: E402


def extract_body_from_curl(curl: str) -> str:
    """从 curl 命令里抠出 -d / --data 后的 body 字符串。"""
    m = re.search(r"(?:-d|--data(?:-raw|--binary)?)\s+('([^']*)'|\"([^\"]*)\")", curl)
    if not m:
        raise SystemExit("curl 里没找到 -d/--data 参数")
    body = m.group(2) if m.group(2) is not None else m.group(3)
    return body


def parse_session(body: str) -> None:
    # body 可能是 '"密文"'(带引号的 JSON 字符串) 或裸密文
    candidate = body.strip()
    if candidate.startswith('"') and candidate.endswith('"'):
        try:
            candidate = json.loads(candidate)
        except Exception:
            candidate = candidate.strip('"')
    plain = aes_decrypt_b64(candidate)
    print("解密后的明文:", plain)
    obj = json.loads(plain)
    sid = obj.get("session_id")
    if not sid:
        raise SystemExit("明文里没有 session_id 字段")
    print("\n>>> session_id =", sid)
    print(">>> 签到命令:")
    print(f"    ./python tools/byd_app_sign.py --session-id {sid}")


def main() -> None:
    ap = argparse.ArgumentParser(description="从抓包提取比亚迪 session_id")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--curl", help="整条抓包 curl 命令")
    g.add_argument("--body", help="加密后的 body 字符串")
    args = ap.parse_args()

    body = extract_body_from_curl(args.curl) if args.curl else args.body
    parse_session(body)


if __name__ == "__main__":
    main()
