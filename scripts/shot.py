"""通过 Chrome DevTools Protocol 截图（不依赖 playwright / puppeteer）。

用法：
  python scripts/shot.py <url> <out.png> [--width 1440] [--height 1000] [--full] [--wait 2500] [--scroll 0]
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import urllib.request

import websockets  # type: ignore

CDP = "http://127.0.0.1:9222"


def _new_target() -> str:
    req = urllib.request.Request(f"{CDP}/json/new?about:blank", method="PUT")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)["webSocketDebuggerUrl"]


class Client:
    def __init__(self, ws) -> None:
        self.ws = ws
        self._id = 0

    async def call(self, method: str, **params):
        self._id += 1
        mid = self._id
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})


async def shoot(url: str, out: str, width: int, height: int, full: bool, wait: int, scroll: int):
    ws_url = _new_target()
    async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
        c = Client(ws)
        await c.call("Page.enable")
        await c.call("Runtime.enable")
        await c.call(
            "Emulation.setDeviceMetricsOverride",
            width=width,
            height=height,
            deviceScaleFactor=1,
            mobile=False,
        )
        await c.call("Page.navigate", url=url)
        await asyncio.sleep(wait / 1000)
        if scroll:
            await c.call(
                "Runtime.evaluate",
                expression=f"window.scrollTo(0,{scroll})",
                awaitPromise=False,
            )
            await asyncio.sleep(0.6)

        # 收集页面错误，便于验收
        errs = await c.call(
            "Runtime.evaluate",
            expression="JSON.stringify({title:document.title, errs:(window.__errs||[]), h:document.body.scrollHeight})",
            returnByValue=True,
        )
        info = json.loads(errs.get("result", {}).get("value") or "{}")

        params = {"format": "png"}
        if full:
            params["captureBeyondViewport"] = True
        data = await c.call("Page.captureScreenshot", **params)
        with open(out, "wb") as f:
            f.write(base64.b64decode(data["data"]))
        print(f"saved {out}  title={info.get('title')!r}  pageHeight={info.get('h')}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("out")
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=1000)
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--wait", type=int, default=2500)
    ap.add_argument("--scroll", type=int, default=0)
    a = ap.parse_args()
    asyncio.run(shoot(a.url, a.out, a.width, a.height, a.full, a.wait, a.scroll))
    return 0


if __name__ == "__main__":
    sys.exit(main())
