"""比亚迪主 App 小程序自动签到（协议已完整破解，实测通过）。

破解结论（反编译 wxa28c31d4ff7ae869 的 appservice.app.js）:
  网关:     https://weixin90.bydauto.com.cn?s=ForCommonUcSrv.forward&serviceDir=<dir>
  Appkey:   wcMinaApi
  Checksum: sha256(appSecret + Nonce + Curtime).lower()，appSecret = wAWUW(jC6bZ%)Hhs
  Body:     AES-128-CBC, key=6298134637481073, iv=PacWxpOwHnVgsQrh, base64
  签到:     serviceDir=activity/sign/signIn, payload {date:"", isCard:"0"}
  日历:     serviceDir=activity/sign/getSignCalendar

用法:
  python tools/byd_app_sign.py --session-id <SID>            # 签到
  python tools/byd_app_sign.py --session-id <SID> --calendar # 签到日历
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import time
import uuid
from typing import Any, Dict

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

DOMAIN = "https://weixin90.bydauto.com.cn"
APP_KEY = "wcMinaApi"
APP_SECRET = "wAWUW(jC6bZ%)Hhs"
AES_KEY = b"6298134637481073"
AES_IV = b"PacWxpOwHnVgsQrh"


def aes_encrypt_b64(plain: str) -> str:
    c = AES.new(AES_KEY, AES.MODE_CBC, AES_IV)
    return base64.b64encode(c.encrypt(pad(plain.encode(), 16))).decode()


def aes_decrypt_b64(b64: str) -> str:
    c = AES.new(AES_KEY, AES.MODE_CBC, AES_IV)
    return unpad(c.decrypt(base64.b64decode(b64)), 16).decode()


def make_headers(nonce: str, curtime: str) -> Dict[str, str]:
    checksum = hashlib.sha256((APP_SECRET + nonce + curtime).encode()).hexdigest()
    return {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 "
            "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
            "MiniProgramEnv/Mac MacWechat/WMPF MacWechat/3.8.7(0x13080712) "
            "UnifiedPCMacWechat(0xf2641f16) XWEB/25755"
        ),
        "Content-Type": "application/json",
        "x-clienttraceid": f"mina-{uuid.uuid4()}",
        "Curtime": curtime,
        "xweb_xhr": "1",
        "Algo": "1",
        "Nonce": nonce,
        "Checksum": checksum,
        "Appkey": APP_KEY,
        "Referer": "https://servicewechat.com/wxa28c31d4ff7ae869/690/page-frame.html",
        "Accept": "*/*",
    }


def call(service_dir: str, payload: Dict[str, Any], timeout: int = 30) -> Dict[str, Any]:
    nonce = uuid.uuid4().hex[:16]
    curtime = str(int(time.time()))
    plain = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    body = json.dumps(aes_encrypt_b64(plain), ensure_ascii=False)
    url = f"{DOMAIN}?s=ForCommonUcSrv.forward&serviceDir={service_dir}"
    resp = requests.post(url, headers=make_headers(nonce, curtime),
                         data=body.encode(), timeout=timeout)
    result = {"url": url, "status": resp.status_code, "raw": resp.text}
    try:
        result["decrypted"] = aes_decrypt_b64(resp.text.strip('"'))
    except Exception:
        pass
    return result


def check_session(session_id: str, timeout: int = 15) -> Dict[str, Any]:
    """校验 session_id 是否有效（小程序启动时的 checkSession 接口）。

    抓包实测（2026-10-13）：
      有效 → {"ret":200,"data":null,"msg":"success"}
      过期 → {"ret":10011,"data":[],"msg":"3rd服务器session过期"}
    注意 URL 形态与业务接口不同：?service=mina.checkSession（没有 s= / serviceDir），
    body 只带 session_id（无 app_version / app_client）。
    """
    nonce = uuid.uuid4().hex[:16]
    curtime = str(int(time.time()))
    plain = json.dumps({"session_id": session_id}, separators=(",", ":"))
    body = json.dumps(aes_encrypt_b64(plain), ensure_ascii=False)
    url = f"{DOMAIN}?service=mina.checkSession"
    resp = requests.post(url, headers=make_headers(nonce, curtime),
                         data=body.encode(), timeout=timeout)
    result = {"url": url, "status": resp.status_code, "raw": resp.text}
    try:
        result["decrypted"] = aes_decrypt_b64(resp.text.strip('"'))
    except Exception:
        pass
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="比亚迪小程序自动签到")
    ap.add_argument("--session-id", required=True)
    ap.add_argument("--calendar", action="store_true", help="查询签到日历")
    args = ap.parse_args()

    base = {"session_id": args.session_id, "app_version": "240", "app_client": "mina"}

    if args.calendar:
        r = call("activity/sign/getSignCalendar", base)
    else:
        r = call("activity/sign/signIn", {**base, "date": "", "isCard": "0"})

    dec = r.get("decrypted", r["raw"])
    print(json.dumps(json.loads(dec), ensure_ascii=False, indent=2)
          if dec.startswith("{") else dec)

    if not args.calendar and dec.startswith("{"):
        data = json.loads(dec)
        if data.get("ret") == 200 and data.get("data", {}).get("duplicate"):
            print("\n今日已签到")
        elif data.get("ret") == 200:
            print("\n签到成功")


if __name__ == "__main__":
    main()
