"""CDP 交互：点击/输入后截图（browser-use 不可用时的替代）。

用法：
  python scripts/cdp.py <url> <out.png> [--width 1440] [--height 1000]
      [--click "css selector"] [--click-xy x y] [--wait 4000] [--full]
      [--eval "js"] [--scroll 0] [--times 1]
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import urllib.request

import websockets

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


async def run(a) -> None:
    ws_url = _new_target()
    async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
        c = Client(ws)
        await c.call("Page.enable")
        await c.call("Runtime.enable")
        await c.call("DOM.enable")
        await c.call(
            "Emulation.setDeviceMetricsOverride",
            width=a.width, height=a.height, deviceScaleFactor=1, mobile=False,
        )
        await c.call("Page.navigate", url=a.url)
        await asyncio.sleep(a.wait / 1000)

        if a.click:
            for sel in a.click:
                doc = await c.call("DOM.getDocument")
                node = await c.call("DOM.querySelector", nodeId=doc["root"]["nodeId"], selector=sel)
                if not node.get("nodeId"):
                    print(f"[warn] selector not found: {sel}")
                    continue
                box = await c.call("DOM.getBoxModel", nodeId=node["nodeId"])
                q = box["model"]["content"]
                x = (q[0] + q[2] + q[4] + q[6]) / 4
                y = (q[1] + q[3] + q[5] + q[7]) / 4
                for kind in ("mousePressed", "mouseReleased"):
                    await c.call(
                        "Input.dispatchMouseEvent",
                        type=kind, x=x, y=y, button="left", clickCount=1,
                    )
                await asyncio.sleep(1.2)

        for xy in a.click_xy:
            x, y = xy
            for kind in ("mousePressed", "mouseReleased"):
                await c.call(
                    "Input.dispatchMouseEvent",
                    type=kind, x=x, y=y, button="left", clickCount=1,
                )
            await asyncio.sleep(1.2)

        if a.eval:
            r = await c.call("Runtime.evaluate", expression=a.eval, returnByValue=True)
            print("[eval]", r.get("result", {}).get("value"))

        if a.scroll:
            await c.call("Runtime.evaluate", expression=f"window.scrollTo(0,{a.scroll})")
            await asyncio.sleep(0.6)

        params = {"format": "png"}
        if a.full:
            params["captureBeyondViewport"] = True
        data = await c.call("Page.captureScreenshot", **params)
        with open(a.out, "wb") as f:
            f.write(base64.b64decode(data["data"]))
        h = await c.call("Runtime.evaluate", expression="document.body.scrollHeight", returnByValue=True)
        print(f"saved {a.out}  pageHeight={h.get('result', {}).get('value')}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("out")
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=1000)
    ap.add_argument("--click", action="append", default=[])
    ap.add_argument("--click-xy", action="append", default=[])
    ap.add_argument("--wait", type=int, default=3000)
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--eval", default=None)
    ap.add_argument("--scroll", type=int, default=0)
    a = ap.parse_args()
    a.click_xy = []
    for item in ap.parse_args().click_xy:
        x, y = item.split(",")
        a.click_xy.append((float(x), float(y)))
    asyncio.run(run(a))
    return 0


if __name__ == "__main__":
    sys.exit(main())
