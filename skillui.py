#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 技能名单 —— 本地图形界面。

只监听 127.0.0.1，写盘逻辑完全复用 skillset.py（备份、原子写、自检、进程保护）。
启动：python skillui.py
"""

import argparse
import contextlib
import io
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import webbrowser
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import skillset as S  # noqa: E402

UI_FILE = os.path.join(HERE, "ui.html")
DEFAULT_PORT = 8899
MAX_DETAIL_BYTES = 262144      # 单个正文最多读 256KB，防异常大文件拖慢响应
PREVIEW_CHARS = 1800           # 悬浮预览最多返回的字符数
FM_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)


def capture(fn, *args, **kwargs):
    """执行会 print / sys.exit 的老函数，返回 (成功, 输出文本)。"""
    buf = io.StringIO()
    ok = True
    with contextlib.redirect_stdout(buf):
        try:
            fn(*args, **kwargs)
        except SystemExit:
            ok = False
    return ok, buf.getvalue().strip()


VER_RE = re.compile(r"^\d+(?:\.\d+)*(?:[-.][0-9A-Za-z]+)*$")


def _plugin_label(skills_dir):
    """从 .../<插件>/<版本>/skills 或 .../<插件>/skills 推出插件名。"""
    parent = os.path.dirname(skills_dir)
    base = os.path.basename(parent)
    if VER_RE.match(base):
        return os.path.basename(os.path.dirname(parent))
    return base


def _tokens(text):
    return {t for t in re.split(r"[^0-9a-z\u4e00-\u9fff]+", (text or "").lower()) if len(t) >= 3}


def _sim(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def build_groups(ctx, selectable):
    """推荐勾选分组：优先按真实目录（同一插件包），其次按名称前缀。

    目录分组的依据来自缓存里的 filePath，比名字可靠：同一个插件包内的技能
    往往名字各异但功能成套（例如 library-office-edit / tencent-docs / tencent-saas-docs）。
    默认的 ~/.workbuddy/skills 不参与目录分组，因为那是一技能一目录，没有信息量。
    """
    paths, descs = {}, {}
    try:
        with open(ctx.cache, "r", encoding="utf-8") as f:
            for it in json.load(f).get("results", []):
                n = it.get("name")
                if not n:
                    continue
                paths[n] = it.get("filePath") or ""
                descs[n] = it.get("description") or ""
    except Exception:
        pass

    default_dir = os.path.normcase(os.path.join(ctx.config, "skills"))
    by_folder = defaultdict(list)
    for name in selectable:
        fp = paths.get(name)
        if not fp:
            continue
        parent = os.path.dirname(os.path.dirname(fp))
        if not parent or os.path.normcase(parent) == default_dir:
            continue
        by_folder[parent].append(name)

    groups, covered = [], set()
    for parent, members in by_folder.items():
        if len(members) < 2:
            continue
        members = sorted(members)
        groups.append({
            "id": "dir:" + parent,
            "label": _plugin_label(parent),
            "kind": "folder",
            "members": members,
            "note": "同一插件目录",
        })
        covered.update(members)

    by_prefix = defaultdict(list)
    for name in selectable:
        if name in covered:
            continue
        by_prefix[name.split("-")[0]].append(name)

    for prefix, members in by_prefix.items():
        if not prefix or len(prefix) < 3:
            continue
        members = sorted(members)
        n = len(members)
        if n >= 3:
            ok = True
        elif n == 2:
            # 只有两个时名字证据弱，要求描述确有交集，避免把无关技能凑成一组
            a, b = members
            ok = _sim(_tokens(descs.get(a, "")), _tokens(descs.get(b, ""))) >= 0.3
        else:
            ok = False
        if ok:
            groups.append({
                "id": "prefix:" + prefix,
                "label": prefix,
                "kind": "prefix",
                "members": members,
                "note": "同名前缀",
            })

    groups.sort(key=lambda g: (g["kind"] != "folder", -len(g["members"]), g["label"].lower()))
    return groups


def build_state(config_dir):
    ctx = S.Ctx(config_dir)
    reg = S.registry(ctx)
    descs = S._desc_map(ctx)
    full_descs = S._desc_map(ctx, full=True)
    dmi = S._dmi_map(ctx)
    settings = S.read_json(ctx.settings, {})
    ov = settings.get(S.KEY) if isinstance(settings.get(S.KEY), dict) else {}
    ledger = S.load_ledger(ctx)["skills"]

    skills = []
    for name, src in reg.items():
        raw = ov.get(name)
        if raw is None:
            status = "default"
        elif raw == S.DORMANT:
            status = "dormant"
        else:
            status = "off"
        rec = ledger.get(name) or {}
        skills.append({
            "name": name,
            "source": src,
            "desc": descs.get(name, ""),
            "status": status,
            "tokens": round(S.skill_cost(name, full_descs.get(name, "")), 1),
            "injected": not dmi.get(name, False),
            "at": rec.get("at", "") if status != "default" else "",
            "by": rec.get("source", "") if status != "default" else "",
        })
    skills.sort(key=lambda s: (s["source"] != "userSettings", s["name"].lower()))

    raw_lists = S.load_lists(ctx)["lists"]
    cost_by_name = {s["name"]: s["tokens"] for s in skills}
    s_inject = {s["name"]: s["injected"] for s in skills}
    lists = []
    for name, members in raw_lists.items():
        stat = {"default": 0, "dormant": 0, "off": 0}
        missing = []
        saveable = 0.0
        for m in members:
            if m not in reg:
                missing.append(m)
                continue
            v = ov.get(m)
            st = "default" if v is None else ("dormant" if v == S.DORMANT else "off")
            stat[st] += 1
            if st == "default" and s_inject.get(m, True):
                saveable += cost_by_name.get(m, 0.0)
        lists.append({
            "name": name,
            "count": len(members),
            "on": stat["default"],
            "dormant": stat["dormant"],
            "off": stat["off"],
            "cost": round(saveable, 1),
            "costAll": round(sum(cost_by_name.get(m, 0.0) for m in members), 1),
            "missing": missing,
            "members": list(members),
        })
    lists.sort(key=lambda x: x["name"].lower())

    running = S.app_running(config_dir)
    bak = []
    if os.path.isdir(ctx.backup_dir):
        for n in sorted(os.listdir(ctx.backup_dir), key=lambda n: os.path.getmtime(
                os.path.join(ctx.backup_dir, n)), reverse=True)[:10]:
            p = os.path.join(ctx.backup_dir, n)
            bak.append({"name": n, "at": time.strftime("%m-%d %H:%M:%S", time.localtime(os.path.getmtime(p)))})

    return {
        "ok": True,
        "config": config_dir,
        "settings": ctx.settings,
        "running": running,
        "skills": skills,
        "costTotal": round(sum(s["tokens"] for s in skills), 1),
        "costActive": round(sum(s["tokens"] for s in skills
                                if s["status"] == "default" and s["injected"]), 1),
        "costUserActive": round(sum(s["tokens"] for s in skills
                                    if s["status"] == "default" and s["injected"]
                                    and s["source"] == "userSettings"), 1),
        "costExcluded": round(sum(s["tokens"] for s in skills if not s["injected"]), 1),
        "groups": build_groups(ctx, {s["name"] for s in skills if s["source"] == "userSettings"}),
        "lists": lists,
        "history": S.load_ledger(ctx)["history"][-30:][::-1],
        "backups": bak,
        "dormantValue": S.DORMANT,
        "hardValue": S.HARD_OFF,
    }


def split_frontmatter(text):
    """拆出 YAML frontmatter，返回 (frontmatter, 正文)。没有则 frontmatter 为空串。"""
    m = FM_RE.match(text or "")
    if not m:
        return "", text or ""
    return m.group(1), text[m.end():]


def skill_detail(config_dir, name):
    """读取某个技能的正文，供界面悬浮预览。只读。

    路径只能来自技能缓存里登记的 filePath，不接受外部传入路径，
    因此不存在路径穿越问题。SKILL.md 缺失时回退同目录的 README.md。
    """
    name = name or ""
    if not name:
        return {"ok": False, "log": "没有指定技能名。"}

    ctx = S.Ctx(config_dir)
    fp = ""
    try:
        with open(ctx.cache, "r", encoding="utf-8") as f:
            for it in json.load(f).get("results", []):
                if it.get("name") == name:
                    fp = it.get("filePath") or ""
                    break
    except Exception:
        pass
    if not fp:
        return {"ok": False, "log": "缓存里没有登记「%s」的正文路径，可能已被卸载。" % name}

    if not os.path.isfile(fp):
        alt = os.path.join(os.path.dirname(fp), "README.md")
        if os.path.isfile(alt):
            fp = alt
        else:
            return {"ok": False, "log": "找不到正文文件：%s" % fp}

    try:
        with open(fp, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read(MAX_DETAIL_BYTES)
    except Exception as e:
        return {"ok": False, "log": "读取失败：%s" % e}

    fm, body = split_frontmatter(raw)
    body = body.strip("\r\n")
    truncated = len(body) > PREVIEW_CHARS
    return {
        "ok": True,
        "name": name,
        "file": fp,
        "fileName": os.path.basename(fp),
        "hasFrontmatter": bool(fm.strip()),
        "lines": body.count("\n") + 1 if body else 0,
        "chars": len(body),
        "tokens": round(S.est_tokens(body), 1),
        "body": body[:PREVIEW_CHARS],
        "truncated": truncated,
        "cut": os.path.getsize(fp) > MAX_DETAIL_BYTES,
    }


def save_list(config_dir, name, members, old_name=None):
    name = (name or "").strip()
    if not name:
        return {"ok": False, "log": "名单名不能为空。"}
    if len(name) > 40:
        return {"ok": False, "log": "名单名太长（上限 40 字）。"}
    ctx = S.Ctx(config_dir)
    data = S.load_lists(ctx)
    reg = S.registry(ctx)

    kept, rejected = [], []
    for m in members or []:
        if reg.get(m) == "userSettings":
            if m not in kept:
                kept.append(m)
        else:
            rejected.append(m)
    if rejected:
        return {"ok": False, "log": "以下不属于可调整的用户级技能，已拒绝：%s" % "、".join(rejected)}

    old = old_name or name
    existed = old in data["lists"]
    if old != name:
        if name in data["lists"]:
            return {"ok": False, "log": "名单「%s」已存在，请换个名字。" % name}
        data["lists"].pop(old, None)
    data["lists"][name] = kept
    S.save_lists(ctx, data)
    return {
        "ok": True,
        "log": "%s名单「%s」，共 %d 个技能。%s" % (
            "已更新" if existed else "已新建", name, len(kept),
            "" if kept else "（空名单，稍后可继续勾选添加）"),
    }


def delete_list(config_dir, name):
    ctx = S.Ctx(config_dir)
    data = S.load_lists(ctx)
    if name not in data["lists"]:
        return {"ok": False, "log": "没有名为「%s」的名单。" % name}
    n = len(data["lists"].pop(name))
    S.save_lists(ctx, data)
    return {"ok": True, "log": "已删除名单「%s」（%d 个成员）。技能状态保持不变。" % (name, n)}


def apply_list(config_dir, name, action, hard=False, force=False):
    ctx = S.Ctx(config_dir)
    data = S.load_lists(ctx)
    members = data["lists"].get(name)
    if members is None:
        return {"ok": False, "log": "没有名为「%s」的名单。" % name}
    if not members:
        return {"ok": False, "log": "名单「%s」是空的，没有可操作的技能。" % name}
    ctx._current_list = name
    ok, out = capture(S.apply_to_settings, ctx, members,
                      "on" if action == "on" else "off", hard, False, force)
    return {"ok": ok, "log": out or "（无输出）"}


def restore(config_dir, force=False):
    ctx = S.Ctx(config_dir)

    class A:
        pass

    a = A()
    a.force = force
    ok, out = capture(S.cmd_restore, ctx, a)
    return {"ok": ok, "log": out}


class Handler(BaseHTTPRequestHandler):
    config_dir = S.DEFAULT_CONFIG
    server_version = "SkillUI/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        raw = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            if not os.path.isfile(UI_FILE):
                return self._json({"ok": False, "log": "缺少 ui.html"}, 500)
            with open(UI_FILE, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if path == "/api/state":
            try:
                return self._json(build_state(self.config_dir))
            except Exception as e:
                return self._json({"ok": False, "log": "读取状态失败：%s" % e}, 500)
        if path == "/api/skill/detail":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._json(skill_detail(self.config_dir, (q.get("name") or [""])[0]))
        if path == "/api/usage":
            try:
                import skillusage
                refresh = "refresh=1" in (self.path.split("?", 1) + [""])[1]
                proj = os.path.join(self.config_dir, "projects")
                cache = os.path.join(self.config_dir, "skill-switch", "usage-cache.json")
                return self._json(skillusage.collect(proj, cache, refresh))
            except Exception as e:
                return self._json({"ok": False, "log": "读取用量失败：%s" % e}, 500)
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        self._json({"ok": False, "log": "未知路径"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._json({"ok": False, "log": "请求体不是合法 JSON"}, 400)

        try:
            if path == "/api/list/save":
                res = save_list(self.config_dir, payload.get("name"),
                                payload.get("members"), payload.get("oldName"))
            elif path == "/api/list/delete":
                res = delete_list(self.config_dir, payload.get("name", ""))
            elif path == "/api/apply":
                res = apply_list(self.config_dir, payload.get("name", ""),
                                 payload.get("action", "off"),
                                 bool(payload.get("hard")), bool(payload.get("force")))
            elif path == "/api/restore":
                res = restore(self.config_dir, bool(payload.get("force")))
            else:
                res = {"ok": False, "log": "未知接口"}
        except Exception as e:
            res = {"ok": False, "log": "执行出错：%s" % e}
        self._json(res)


def main():
    p = argparse.ArgumentParser(description="WorkBuddy 技能名单图形界面")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--config-dir", default=S.DEFAULT_CONFIG, help="默认 ~/.workbuddy")
    p.add_argument("--no-browser", action="store_true")
    a = p.parse_args()

    Handler.config_dir = a.config_dir
    httpd = None
    port = a.port
    for _ in range(12):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            port += 1
    if httpd is None:
        print("端口被占用，无法启动。")
        sys.exit(1)

    url = "http://127.0.0.1:%d/" % port
    print("=" * 52)
    print(" 技能名单界面已启动")
    print(" 地址：%s" % url)
    print(" 配置：%s" % a.config_dir)
    print(" 关闭窗口或按 Ctrl+C 停止")
    print("=" * 52)
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        httpd.shutdown()


if __name__ == "__main__":
    main()
