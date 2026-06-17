"""Bilibili comment API client with WBI signing."""

from __future__ import annotations

import hashlib
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from functools import reduce
from typing import Callable, Iterator

import requests

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}


@dataclass
class CommentRecord:
    comment_type: str
    username: str
    uid: int
    content: str
    likes: int
    publish_time: str
    rpid: int
    root_rpid: int | None
    reply_to: str


class BilibiliClient:
    def __init__(self, request_delay: float = 0.6, cookie: str = ""):
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        if cookie.strip():
            self.session.headers["Cookie"] = cookie.strip()
        self.request_delay = request_delay
        self._img_key: str | None = None
        self._sub_key: str | None = None

    def set_cookie(self, cookie: str) -> None:
        cookie = cookie.strip()
        if cookie:
            self.session.headers["Cookie"] = cookie
        else:
            self.session.headers.pop("Cookie", None)

    @staticmethod
    def normalize_bvid(bvid: str) -> str:
        bvid = bvid.strip()
        if len(bvid) >= 2 and bvid[:2].upper() == "BV":
            return "BV" + bvid[2:]
        return f"BV{bvid}"

    @staticmethod
    def _get_mixin_key(raw_key: str) -> str:
        return reduce(lambda s, i: s + raw_key[i], MIXIN_KEY_ENC_TAB, "")[:32]

    def _refresh_wbi_keys(self) -> None:
        resp = self.session.get("https://api.bilibili.com/x/web-interface/nav", timeout=15)
        resp.raise_for_status()
        data = resp.json()
        wbi_img = data["data"]["wbi_img"]
        self._img_key = wbi_img["img_url"].rsplit("/", 1)[1].split(".")[0]
        self._sub_key = wbi_img["sub_url"].rsplit("/", 1)[1].split(".")[0]

    def _sign_params(self, params: dict) -> dict:
        if not self._img_key or not self._sub_key:
            self._refresh_wbi_keys()

        signed = dict(params)
        signed["wts"] = round(time.time())
        signed = dict(sorted(signed.items()))
        signed = {
            key: "".join(ch for ch in str(value) if ch not in "!'()*")
            for key, value in signed.items()
        }
        mixin_key = self._get_mixin_key(self._img_key + self._sub_key)
        query = urllib.parse.urlencode(signed)
        signed["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
        return signed

    def _get_json(
        self,
        url: str,
        params: dict | None = None,
        signed: bool = False,
        max_retries: int = 8,
    ) -> dict:
        last_error: Exception | None = None
        for attempt in range(max_retries):
            time.sleep(self.request_delay)
            try:
                request_params = self._sign_params(params or {}) if signed else (params or {})
                resp = self.session.get(url, params=request_params, timeout=20)

                if resp.status_code in (412, 429):
                    self._refresh_wbi_keys()
                    wait = min(2 ** attempt, 30)
                    last_error = requests.HTTPError(
                        f"{resp.status_code} Client Error",
                        response=resp,
                    )
                    time.sleep(wait)
                    continue

                resp.raise_for_status()
                payload = resp.json()

                if payload.get("code") in (-412, -509):
                    self._refresh_wbi_keys()
                    wait = min(2 ** attempt, 30)
                    last_error = RuntimeError(payload.get("message", "请求被限制"))
                    time.sleep(wait)
                    continue

                if payload.get("code") != 0:
                    message = payload.get("message", "未知错误")
                    raise RuntimeError(
                        f"API 请求失败: {message} (code={payload.get('code')})"
                    )
                return payload
            except requests.RequestException as exc:
                last_error = exc
                time.sleep(min(2 ** attempt, 12))

        detail = f": {last_error}" if last_error else ""
        raise RuntimeError(
            f"请求失败，已重试 {max_retries} 次{detail}。"
            "请稍后重试，或在界面中填写 B 站 Cookie 以提高成功率。"
        )

    def get_video_info(self, bvid: str) -> dict:
        bvid = self.normalize_bvid(bvid)
        payload = self._get_json(
            "https://api.bilibili.com/x/web-interface/view",
            {"bvid": bvid},
        )
        return payload["data"]

    @staticmethod
    def _format_time(timestamp: int) -> str:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _parse_comment(
        item: dict,
        comment_type: str,
        root_rpid: int | None = None,
        reply_to: str = "",
    ) -> CommentRecord:
        member = item.get("member") or {}
        content = item.get("content") or {}
        return CommentRecord(
            comment_type=comment_type,
            username=member.get("uname", ""),
            uid=member.get("mid", 0),
            content=content.get("message", ""),
            likes=item.get("like", 0),
            publish_time=BilibiliClient._format_time(item.get("ctime", 0)),
            rpid=item.get("rpid", 0),
            root_rpid=root_rpid,
            reply_to=reply_to,
        )

    def iter_main_comments(self, aid: int) -> Iterator[dict]:
        next_page = 0
        while True:
            payload = self._get_json(
                "https://api.bilibili.com/x/v2/reply/main",
                {
                    "oid": aid,
                    "type": 1,
                    "mode": 3,
                    "next": next_page,
                    "plat": 1,
                },
                signed=True,
            )
            data = payload["data"]
            replies = data.get("replies") or []
            for reply in replies:
                yield reply

            cursor = data.get("cursor") or {}
            if cursor.get("is_end"):
                break
            next_page = cursor.get("next", 0)

    def iter_sub_replies(self, aid: int, root_rpid: int) -> Iterator[dict]:
        page = 1
        while True:
            payload = self._get_json(
                "https://api.bilibili.com/x/v2/reply/reply",
                {
                    "type": 1,
                    "oid": aid,
                    "root": root_rpid,
                    "ps": 20,
                    "pn": page,
                    "plat": 1,
                },
                signed=True,
            )
            data = payload["data"]
            replies = data.get("replies") or []
            for reply in replies:
                yield reply

            page_info = data.get("page") or {}
            total = page_info.get("count", 0)
            if page * page_info.get("size", 20) >= total or not replies:
                break
            page += 1

    def collect_user_comments(
        self,
        bvid: str,
        username: str,
        *,
        exact_match: bool = True,
        progress_callback: Callable[[str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> tuple[list[CommentRecord], dict]:
        def log(message: str) -> None:
            if progress_callback:
                progress_callback(message)

        def stopped() -> bool:
            return bool(should_stop and should_stop())

        username = username.strip()
        if not username:
            raise ValueError("用户名不能为空")

        video = self.get_video_info(bvid)
        aid = video["aid"]
        title = video.get("title", "")
        bvid = self.normalize_bvid(bvid)

        def matches(name: str) -> bool:
            if exact_match:
                return name == username
            return username.lower() in name.lower()

        matched: list[CommentRecord] = []
        main_total = 0
        sub_total = 0

        log(f"视频: {title}")
        log(f"BV号: {bvid} | AID: {aid}")
        log(f"正在扫描主评论，匹配用户: {username}")

        roots_with_replies: list[tuple[int, str]] = []

        for item in self.iter_main_comments(aid):
            if stopped():
                raise InterruptedError("用户已取消任务")

            main_total += 1
            uname = (item.get("member") or {}).get("uname", "")
            if matches(uname):
                matched.append(self._parse_comment(item, "主评论"))

            rcount = item.get("rcount", 0)
            if rcount:
                roots_with_replies.append((item["rpid"], uname))

            if main_total % 50 == 0:
                log(f"已扫描主评论 {main_total} 条，当前匹配 {len(matched)} 条")

        log(f"主评论扫描完成，共 {main_total} 条，其中 {len(roots_with_replies)} 条有回复")

        for index, (root_rpid, root_user) in enumerate(roots_with_replies, start=1):
            if stopped():
                raise InterruptedError("用户已取消任务")

            for reply in self.iter_sub_replies(aid, root_rpid):
                sub_total += 1
                uname = (reply.get("member") or {}).get("uname", "")
                if matches(uname):
                    parent = (reply.get("content") or {}).get("reply_control", {})
                    reply_to = root_user
                    if reply.get("parent") and reply["parent"] != root_rpid:
                        reply_to = "楼中楼回复"
                    matched.append(
                        self._parse_comment(
                            reply,
                            "回复评论",
                            root_rpid=root_rpid,
                            reply_to=reply_to,
                        )
                    )

            if index % 20 == 0 or index == len(roots_with_replies):
                log(
                    f"子评论进度 {index}/{len(roots_with_replies)}，"
                    f"已扫描子评论 {sub_total} 条，当前匹配 {len(matched)} 条"
                )

        matched.sort(key=lambda item: item.publish_time)
        summary = {
            "bvid": bvid,
            "aid": aid,
            "title": title,
            "username": username,
            "main_total": main_total,
            "sub_total": sub_total,
            "matched_total": len(matched),
        }
        log(f"完成！共找到该用户评论 {len(matched)} 条")
        return matched, summary
