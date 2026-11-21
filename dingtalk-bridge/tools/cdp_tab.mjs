#!/usr/bin/env node
/**
 * 常驻 tab 版 CDP 小工具（fygo-cdp 的 cdp.mjs 每次都会关 tab，扫码登录不行）。
 *
 *   node cdp_tab.mjs open --url <url> --out <png> [--wait 6000]
 *   node cdp_tab.mjs shot --out <png>
 *   node cdp_tab.mjs eval --expr '<js>'
 *   node cdp_tab.mjs click --expr '<js selector 表达式>'   // 等价 eval 但顺便返回结果
 *   node cdp_tab.mjs type  --expr '<js>'                    // 同上，语义区分
 *   node cdp_tab.mjs close
 */
import WebSocket from "/vol1/@appdata/deepseek.harness/dsh-runtime/node_modules/ws/wrapper.mjs";
import fs from "node:fs";
import path from "node:path";

const CDP_HTTP = process.env.CDP_HTTP || "http://127.0.0.1:16002";
const STATE = path.resolve(process.env.CDP_STATE || "./.cdp_tab");

function parseArgs(argv) {
  const out = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith("--")) out[a.slice(2)] = argv[++i];
    else out._.push(a);
  }
  return out;
}

async function connect() {
  const v = await (await fetch(`${CDP_HTTP}/json/version`)).json();
  const ws = new WebSocket(v.webSocketDebuggerUrl);
  await new Promise((r, j) => { ws.once("open", r); ws.once("error", j); });
  let id = 0;
  const rpc = (method, params = {}, sessionId) =>
    new Promise((resolve, reject) => {
      const myId = ++id;
      const handler = (data) => {
        const m = JSON.parse(data.toString());
        if (m.id === myId) {
          ws.off("message", handler);
          if (m.error) reject(new Error(`${method}: ${m.error.message}`));
          else resolve(m.result);
        }
      };
      ws.on("message", handler);
      ws.send(JSON.stringify(sessionId ? { sessionId, id: myId, method, params } : { id: myId, method, params }));
    });
  return { ws, rpc };
}


async function discoverTargets(ws, rpc, ms = 2500) {
  const found = new Map();
  const onMsg = (data) => {
    const m = JSON.parse(data.toString());
    if (m.method === "Target.targetCreated" || m.method === "Target.attachedToTarget") {
      const info = m.params?.targetInfo || m.params?.targetInfo;
      if (info) found.set(info.targetId, info);
    }
  };
  ws.on("message", onMsg);
  try {
    await rpc("Target.setDiscoverTargets", { discover: true });
    await rpc("Target.setAutoAttach", { autoAttach: true, waitForDebuggerOnStart: false, flatten: true });
    await new Promise((r) => setTimeout(r, ms));
  } catch (e) { /* ignore */ }
  ws.off("message", onMsg);
  return [...found.values()];
}

async function attach(rpc, targetId) {
  const s = await rpc("Target.attachToTarget", { targetId, flatten: true });
  await rpc("Page.enable", {}, s.sessionId);
  await rpc("Runtime.enable", {}, s.sessionId);
  return s.sessionId;
}

const cmd = process.argv[2];
const args = parseArgs(process.argv.slice(3));
const { ws, rpc } = await connect();

try {
  if (cmd === "open") {
    const t = await rpc("Target.createTarget", { url: args.url || "about:blank" });
    fs.writeFileSync(STATE, t.targetId);
    const sid = await attach(rpc, t.targetId);
    await new Promise((r) => setTimeout(r, Number(args.wait || 6000)));
    if (args.out) {
      const shot = await rpc("Page.captureScreenshot", { format: "png" }, sid);
      fs.writeFileSync(args.out, Buffer.from(shot.data, "base64"));
      console.log(`opened ${t.targetId} -> ${args.out}`);
    } else {
      console.log(`opened ${t.targetId}`);
    }
  } else if (cmd === "shot") {
    const targetId = fs.readFileSync(STATE, "utf8").trim();
    const sid = await attach(rpc, targetId);
    const shot = await rpc("Page.captureScreenshot", { format: "png", captureBeyondViewport: false }, sid);
    fs.writeFileSync(args.out, Buffer.from(shot.data, "base64"));
    console.log(`wrote ${args.out}`);
  } else if (cmd === "eval" || cmd === "click" || cmd === "type") {
    const targetId = fs.readFileSync(STATE, "utf8").trim();
    const sid = await attach(rpc, targetId);
    const r = await rpc("Runtime.evaluate", {
      expression: args.expr,
      awaitPromise: true,
      returnByValue: true,
      userGesture: true,
    }, sid);
    if (r.exceptionDetails) {
      console.error("JS 异常:", r.exceptionDetails.exception?.description || JSON.stringify(r.exceptionDetails));
      process.exitCode = 1;
    } else {
      console.log(JSON.stringify(r.result?.value ?? r.result?.description ?? null));
    }
  } else if (cmd === "goto") {
    const targetId = fs.readFileSync(STATE, "utf8").trim();
    const sid = await attach(rpc, targetId);
    await rpc("Page.navigate", { url: args.url }, sid);
    await new Promise((r) => setTimeout(r, Number(args.wait || 5000)));
    if (args.out) {
      const shot = await rpc("Page.captureScreenshot", { format: "png" }, sid);
      fs.writeFileSync(args.out, Buffer.from(shot.data, "base64"));
    }
    const r = await rpc("Runtime.evaluate", { expression: "location.href", returnByValue: true }, sid);
    console.log("now at " + r.result.value);
  } else if (cmd === "hover") {
    const targetId = fs.readFileSync(STATE, "utf8").trim();
    const sid = await attach(rpc, targetId);
    const pt = await rpc("Runtime.evaluate", {
      expression: `(() => { const cands=[...document.querySelectorAll('*')].filter(e=>e.textContent.trim()===${JSON.stringify(args.text || "")}).map(e=>({e,r:e.getBoundingClientRect()})).filter(o=>o.r.width>1&&o.r.height>1); if(!cands.length) return null; const {r}=cands[cands.length-1]; return {x:r.x+r.width/2,y:r.y+r.height/2}; })()`,
      returnByValue: true,
    }, sid);
    const p = pt.result.value;
    if (!p) { console.error("element not found"); process.exitCode = 1; }
    else {
      await rpc("Input.dispatchMouseEvent", { type: "mouseMoved", x: p.x, y: p.y }, sid);
      await new Promise((r) => setTimeout(r, Number(args.wait || 1500)));
      console.log("hovered", JSON.stringify(p));
    }
  } else if (cmd === "clickat") {
    const targetId = fs.readFileSync(STATE, "utf8").trim();
    const sid = await attach(rpc, targetId);
    const pt = await rpc("Runtime.evaluate", {
      expression: `(() => { const cands=[...document.querySelectorAll('*')].filter(e=>e.textContent.trim()===${JSON.stringify(args.text || "")}).map(e=>({e,r:e.getBoundingClientRect()})).filter(o=>o.r.width>1&&o.r.height>1); if(!cands.length) return null; const {r}=cands[cands.length-1]; return {x:r.x+r.width/2,y:r.y+r.height/2}; })()`,
      returnByValue: true,
    }, sid);
    const p = pt.result.value;
    if (!p) { console.error("element not found"); process.exitCode = 1; }
    else {
      await rpc("Input.dispatchMouseEvent", { type: "mouseMoved", x: p.x, y: p.y }, sid);
      await rpc("Input.dispatchMouseEvent", { type: "mousePressed", x: p.x, y: p.y, button: "left", clickCount: 1 }, sid);
      await rpc("Input.dispatchMouseEvent", { type: "mouseReleased", x: p.x, y: p.y, button: "left", clickCount: 1 }, sid);
      await new Promise((r) => setTimeout(r, Number(args.wait || 2500)));
      console.log("clicked", JSON.stringify(p));
    }
  } else if (cmd === "targets2") {
    const list = await discoverTargets(ws, rpc);
    for (const t of list) console.log(`${t.type}\t${t.targetId}\t${(t.url||"").slice(0,110)}`);
  } else if (cmd === "targets") {
    const { targetInfos } = await rpc("Target.getTargets");
    for (const t of targetInfos) {
      if (t.type === "iframe" || t.type === "page") console.log(`${t.type}\t${t.targetId}\t${t.url.slice(0,110)}`);
    }
  } else if (cmd === "f_eval" || cmd === "f_call") {
    const targetId = fs.readFileSync(STATE, "utf8").trim();
    const sid = await attach(rpc, targetId);
    const want = args.url || "card.dingtalk.com";
    const tree = await rpc("Page.getFrameTree", {}, sid);
    const flat = [];
    (function walk(n) {
      flat.push(n.frame);
      (n.childFrames || []).forEach(walk);
    })(tree.frameTree);
    const frame = flat.find((f) => (f.url || "").includes(want));
    if (!frame) { console.error("frame not found; have: " + flat.map((f) => f.url.slice(0, 60)).join(" | ")); process.exitCode = 1; }
    else {
      const world = await rpc("Page.createIsolatedWorld", { frameId: frame.id, worldName: "probe", grantUniveralAccess: true }, sid);
      const r = await rpc("Runtime.evaluate", {
        expression: args.expr, contextId: world.executionContextId,
        awaitPromise: true, returnByValue: true, userGesture: true,
      }, sid);
      if (r.exceptionDetails) {
        console.error("JS 异常:", r.exceptionDetails.exception?.description || JSON.stringify(r.exceptionDetails));
        process.exitCode = 1;
      } else {
        const v = r.result?.value ?? r.result?.description ?? null;
        console.log(typeof v === "string" ? v : JSON.stringify(v));
      }
    }
  } else if (cmd === "ktype") {
    const targetId = fs.readFileSync(STATE, "utf8").trim();
    const sid = await attach(rpc, targetId);
    const [x, y] = (args.at || "").split(",").map(Number);
    if (x !== undefined && !Number.isNaN(x)) {
      await rpc("Input.dispatchMouseEvent", { type: "mouseMoved", x, y }, sid);
      await rpc("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 }, sid);
      await rpc("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 }, sid);
      await new Promise((r) => setTimeout(r, 300));
    }
    if (args.clear !== "no") {
      await rpc("Input.dispatchKeyEvent", { type: "keyDown", key: "a", code: "KeyA", windowsVirtualKeyCode: 65, modifiers: 2 }, sid);
      await rpc("Input.dispatchKeyEvent", { type: "keyUp", key: "a", code: "KeyA", windowsVirtualKeyCode: 65, modifiers: 2 }, sid);
      await new Promise((r) => setTimeout(r, 150));
    }
    await rpc("Input.insertText", { text: args.text || "" }, sid);
    await new Promise((r) => setTimeout(r, Number(args.wait || 800)));
    if (args.blur !== "no") {
      await rpc("Input.dispatchMouseEvent", { type: "mousePressed", x: 900, y: 600, button: "left", clickCount: 1 }, sid);
      await rpc("Input.dispatchMouseEvent", { type: "mouseReleased", x: 900, y: 600, button: "left", clickCount: 1 }, sid);
      await new Promise((r) => setTimeout(r, 500));
    }
    console.log("typed");
  } else if (cmd === "mousedrag") {
    const targetId = fs.readFileSync(STATE, "utf8").trim();
    const sid = await attach(rpc, targetId);
    const [sx, sy] = (args.from || "").split(",").map(Number);
    const [tx, ty] = (args.to || "").split(",").map(Number);
    await rpc("Input.dispatchMouseEvent", { type: "mouseMoved", x: sx, y: sy }, sid);
    await rpc("Input.dispatchMouseEvent", { type: "mousePressed", x: sx, y: sy, button: "left", clickCount: 1 }, sid);
    await new Promise((r) => setTimeout(r, 120));
    for (let i = 1; i <= 20; i++) {
      const x = sx + ((tx - sx) * i) / 20, y = sy + ((ty - sy) * i) / 20;
      await rpc("Input.dispatchMouseEvent", { type: "mouseMoved", x, y, button: "left", buttons: 1 }, sid);
      await new Promise((r) => setTimeout(r, 40));
    }
    await new Promise((r) => setTimeout(r, 250));
    await rpc("Input.dispatchMouseEvent", { type: "mouseReleased", x: tx, y: ty, button: "left", clickCount: 1 }, sid);
    await new Promise((r) => setTimeout(r, 400));
    console.log("mousedrag done");
  } else if (cmd === "drag") {
    const targetId = fs.readFileSync(STATE, "utf8").trim();
    const sid = await attach(rpc, targetId);
    const [sx, sy] = (args.from || "").split(",").map(Number);
    const [tx, ty] = (args.to || "").split(",").map(Number);
    let dragData = null;
    const onMsg = (data) => {
      const m = JSON.parse(data.toString());
      if (m.method === "Input.dragIntercepted") dragData = m.params.data;
    };
    ws.on("message", onMsg);
    await rpc("Input.setInterceptDrags", { enabled: true }, sid);
    await rpc("Input.dispatchMouseEvent", { type: "mouseMoved", x: sx, y: sy }, sid);
    await rpc("Input.dispatchMouseEvent", { type: "mousePressed", x: sx, y: sy, button: "left", clickCount: 1 }, sid);
    for (let i = 1; i <= 6; i++) {
      await rpc("Input.dispatchMouseEvent", { type: "mouseMoved", x: sx + i * 4, y: sy + i * 3, button: "left", buttons: 1 }, sid);
      await new Promise((r) => setTimeout(r, 60));
    }
    await new Promise((r) => setTimeout(r, 400));
    if (!dragData) {
      console.error("no drag intercepted (目标可能不是 HTML5 DnD)");
    } else {
      for (let i = 1; i <= 6; i++) {
        const x = sx + ((tx - sx) * i) / 6, y = sy + ((ty - sy) * i) / 6;
        await rpc("Input.dispatchMouseEvent", { type: "mouseMoved", x, y, button: "left", buttons: 1 }, sid);
        await new Promise((r) => setTimeout(r, 60));
      }
      await rpc("Input.dispatchDragEvent", { type: "dragEnter", x: tx, y: ty, data: dragData }, sid);
      await new Promise((r) => setTimeout(r, 150));
      await rpc("Input.dispatchDragEvent", { type: "dragOver", x: tx, y: ty, data: dragData }, sid);
      await new Promise((r) => setTimeout(r, 250));
      await rpc("Input.dispatchDragEvent", { type: "drop", x: tx, y: ty, data: dragData }, sid);
      await rpc("Input.dispatchMouseEvent", { type: "mouseReleased", x: tx, y: ty, button: "left", clickCount: 1 }, sid);
      console.log("dropped " + JSON.stringify(dragData).slice(0, 160));
    }
    ws.off("message", onMsg);
    await rpc("Input.setInterceptDrags", { enabled: false }, sid);
  } else if (cmd === "net") {
    const targetId = fs.readFileSync(STATE, "utf8").trim();
    const sid = await attach(rpc, targetId);
    await rpc("Network.enable", {}, sid);
    const reqs = new Map();
    const out = [];
    const onMsg = (data) => {
      const m = JSON.parse(data.toString());
      if (m.sessionId !== sid) return;
      if (m.method === "Network.requestWillBeSent") {
        const r = m.params.request;
        reqs.set(m.params.requestId, { requestId: m.params.requestId, url: r.url, method: r.method, postData: r.postData, headers: r.headers });
      } else if (m.method === "Network.responseReceived") {
        const info = reqs.get(m.params.requestId);
        if (info) {
          info.status = m.params.response.status;
          info.mime = m.params.response.mimeType;
          info.type = m.params.type;
          out.push(info);
        }
      }
    };
    ws.on("message", onMsg);
    if (args.expr) {
      const w = await rpc("Page.createIsolatedWorld", { frameId: (await rpc("Page.getFrameTree", {}, sid)).frameTree.frame.id, worldName: "netw" }, sid);
      const rr = await rpc("Runtime.evaluate", { expression: args.expr, contextId: w.executionContextId, userGesture: true, returnByValue: true }, sid);
      console.error("expr -> " + JSON.stringify(rr.result?.value ?? rr.exceptionDetails?.text ?? null));
    }
    if (args.x) {
      const x = Number(args.x), y = Number(args.y);
      await new Promise((r) => setTimeout(r, 500));
      await rpc("Input.dispatchMouseEvent", { type: "mouseMoved", x, y }, sid);
      await rpc("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 }, sid);
      await rpc("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 }, sid);
    }
    await new Promise((r) => setTimeout(r, Number(args.seconds || 8) * 1000));
    ws.off("message", onMsg);
    const filt = args.filter || "";
    for (const o of out) {
      if (filt && !o.url.includes(filt)) continue;
      if (o.type !== "XHR" && o.type !== "Fetch") continue;
      console.log(`\n[${o.method} ${o.status}] ${o.url.slice(0, 160)}`);
      if (o.postData) console.log("  BODY: " + o.postData.slice(0, Number(args.max || 1500)));
      if (args.hdrs) console.log("  HDR: " + JSON.stringify(o.headers).slice(0, Number(args.max || 1500)));
      if (args.resp && o.mime && o.mime.includes("json")) {
        try {
          const b = await rpc("Network.getResponseBody", { requestId: o.requestId }, sid);
          const body = b.base64Encoded ? Buffer.from(b.body, "base64").toString() : b.body;
          console.log("  RESP: " + body.slice(0, Number(args.max || 1500)));
        } catch (e) { console.log("  RESP: <unavailable " + e.message + ">"); }
      }
    }
  } else if (cmd === "setViewport") {
    const targetId = fs.readFileSync(STATE, "utf8").trim();
    const sid = await attach(rpc, targetId);
    await rpc("Emulation.setDeviceMetricsOverride", { width: Number(args.w||1440), height: Number(args.h||900), deviceScaleFactor: 1, mobile: false }, sid);
    console.log("viewport " + (args.w||1440) + "x" + (args.h||900));
  } else if (cmd === "frames") {
    const targetId = fs.readFileSync(STATE, "utf8").trim();
    const sid = await attach(rpc, targetId);
    const tree = await rpc("Page.getFrameTree", {}, sid);
    (function walk(n, d) { console.log("  ".repeat(d) + n.frame.url); (n.childFrames || []).forEach((c) => walk(c, d + 1)); })(tree.frameTree, 0);
  } else if (cmd === "f_upload") {
    const targetId = fs.readFileSync(STATE, "utf8").trim();
    const sid = await attach(rpc, targetId);
    const tree = await rpc("Page.getFrameTree", {}, sid);
    const flat = [];
    (function walk(n) { flat.push(n.frame); (n.childFrames || []).forEach(walk); })(tree.frameTree);
    const want = args.url || "editor.dingtalk.com";
    const frame = flat.find((f) => (f.url || "").includes(want));
    if (!frame) { console.error("frame not found"); process.exitCode = 1; }
    else {
      const world = await rpc("Page.createIsolatedWorld", { frameId: frame.id, worldName: "up", grantUniveralAccess: true }, sid);
      const r = await rpc("Runtime.evaluate", {
        expression: args.expr, contextId: world.executionContextId, returnByValue: false, awaitPromise: true,
      }, sid);
      const objId = r.result && r.result.objectId;
      if (!objId) { console.error("element not found: " + JSON.stringify(r.result)); process.exitCode = 1; }
      else {
        await rpc("DOM.enable", {}, sid);
        await rpc("DOM.setFileInputFiles", { files: [args.path], objectId: objId }, sid);
        console.log("uploaded " + args.path);
      }
    }
  } else if (cmd === "clickxy") {
    const targetId = fs.readFileSync(STATE, "utf8").trim();
    const sid = await attach(rpc, targetId);
    const x = Number(args.x), y = Number(args.y);
    await rpc("Input.dispatchMouseEvent", { type: "mouseMoved", x, y }, sid);
    await rpc("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 }, sid);
    await rpc("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 }, sid);
    await new Promise((r) => setTimeout(r, Number(args.wait || 1500)));
    console.log(`clicked ${x},${y}`);
  } else if (cmd === "activate") {
    const targetId = fs.readFileSync(STATE, "utf8").trim();
    await rpc("Target.activateTarget", { targetId });
    console.log("activated " + targetId);
  } else if (cmd === "url") {
    const targetId = fs.readFileSync(STATE, "utf8").trim();
    const sid = await attach(rpc, targetId);
    const r = await rpc("Runtime.evaluate", { expression: "location.href", returnByValue: true }, sid);
    console.log(r.result.value);
  } else if (cmd === "close") {
    const targetId = fs.readFileSync(STATE, "utf8").trim();
    await rpc("Target.closeTarget", { targetId });
    fs.unlinkSync(STATE);
    console.log("closed");
  } else {
    console.error("usage: open|shot|eval|url|close");
    process.exitCode = 2;
  }
} finally {
  ws.close();
}
