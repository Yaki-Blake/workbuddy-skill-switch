#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
skillset —— WorkBuddy 技能名单管理与一键开关

核心概念
    名单(list)  一组技能的命名集合，如「编程」「设计」「办公」
    状态        每个技能一条：默认 / 休眠(user-invocable-only) / 关闭(off)
    裁决        同一技能出现在多份名单时，最后一次执行的操作生效

数据文件
    lists.json    名单定义，本工具自建，不污染 WorkBuddy 配置
    ledger.json   每个技能最后被谁改的、什么时候改的
    settings.json WorkBuddy 真正读取的配置，只写其中的 skillOverrides

常用命令
    python skillset.py new 编程 code-reviewer tdd-guide    新建名单
    python skillset.py add 编程 ponytail                   追加成员
    python skillset.py rm  编程 ponytail                   移除成员
    python skillset.py ls                                  列出全部名单
    python skillset.py show 编程                           查看名单内容与成员当前状态
    python skillset.py off 编程                            一键关闭（休眠，仍可 /技能名 调用）
    python skillset.py off 编程 --hard                     一键彻底关闭
    python skillset.py on  编程                            一键开启（恢复默认）
    python skillset.py status                              查看所有被改过状态的技能及来源
    python skillset.py del 编程                            删除名单（不影响已写入的状态）

通用开关
    --dry-run        只显示会改什么，不写文件
    --config-dir X   指定 WorkBuddy 配置目录（默认 ~/.workbuddy，主要用于测试）
    --force          应用运行中仍强制写入 / 允许操作内置技能

安全约束
    改 settings.json 前必须先完全退出 WorkBuddy，否则运行中的应用会把内存旧值写回。
    写入前自动备份，写入用临时文件替换，写后校验除 skillOverrides 外的配置逐字节未变。
"""
import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile

# ---------------------------------------------------------------------------
# token 估算
#   系统提示里每个技能只占一行「name: description」，SKILL.md 正文只在真正
#   调用时才加载。所以这里的估算基准是 frontmatter 的名称与描述，不是全文。
#   系数取自常见分词器的经验值：CJK 约 1.5 token/字，其余约 3.6 字符/token。
#   结果用于横向比较与量级判断，不是精确计数。
# ---------------------------------------------------------------------------

def is_cjk(ch):
    return ("\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f"
            or "\uff00" <= ch <= "\uffef" or "\u3400" <= ch <= "\u4dbf")


def est_tokens(text):
    """估算一段文本的 token 数。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if is_cjk(ch))
    other = len(text) - cjk
    return cjk * 1.5 + other / 3.6


def skill_cost(name, desc):
    """单个技能在系统提示里的大致开销（一行 name + 描述）。"""
    return est_tokens("%s: %s" % (name or "", desc or ""))


def fmt_tok(n):
    """把 token 数格式化成易读形式：1234 -> 1.2k"""
    n = float(n or 0)
    if n >= 1000:
        return "%.1fk" % (n / 1000.0)
    return "%d" % round(n)

DEFAULT_CONFIG = os.path.join(os.path.expanduser("~"), ".workbuddy")
STATE_DIR_NAME = "skill-switch"
KEY = "skillOverrides"
DORMANT = "user-invocable-only"
HARD_OFF = "off"
OK = "默认"
PROCESS_NAMES = ("WorkBuddy.exe", "WorkBuddyAI.exe")
PROCESS_BY_CONFIG = {
    ".workbuddy": "WorkBuddy.exe",
    ".workbuddy-ai": "WorkBuddyAI.exe",
}
MAX_BACKUPS = 30
MAX_HISTORY = 200


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Ctx:
    def __init__(self, config_dir):
        self.config = config_dir
        self.settings = os.path.join(config_dir, "settings.json")
        self.cache = os.path.join(config_dir, ".skill-list-cache.json")
        self.state_dir = os.path.join(config_dir, STATE_DIR_NAME)
        self.lists_path = os.path.join(self.state_dir, "lists.json")
        self.ledger_path = os.path.join(self.state_dir, "ledger.json")
        self.backup_dir = os.path.join(self.state_dir, "backup")


def die(msg):
    print("[中止] " + msg)
    sys.exit(1)


def expected_process(config_dir):
    """配置目录对应的应用进程。只认自己那一个，避免另一版本开着就误拦。"""
    if not config_dir:
        return None
    return PROCESS_BY_CONFIG.get(os.path.basename(os.path.normpath(config_dir)).lower())


def app_running(config_dir=None):
    exp = expected_process(config_dir)
    targets = (exp,) if exp else PROCESS_NAMES
    for name in targets:
        try:
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq " + name, "/NH"],
                capture_output=True, text=True, encoding="gbk", errors="ignore",
            ).stdout
            if name.lower() in out.lower():
                return name
        except Exception:
            pass
    return None


def read_json(path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return fallback
    except json.JSONDecodeError as e:
        die("%s 不是合法 JSON：%s" % (path, e))


def write_json_atomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    os.close(fd)
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    with open(tmp, "r", encoding="utf-8") as f:
        json.load(f)
    os.replace(tmp, path)


def registry(ctx):
    """返回 {技能名: 来源}，来源为 userSettings / builtin / plugin。"""
    found = {}
    skills_dir = os.path.join(ctx.config, "skills")
    if os.path.isdir(skills_dir):
        for name in os.listdir(skills_dir):
            if os.path.isfile(os.path.join(skills_dir, name, "SKILL.md")):
                found[name] = "userSettings"
    try:
        with open(ctx.cache, "r", encoding="utf-8") as f:
            for item in json.load(f).get("results", []):
                name = item.get("name")
                if name and name not in found:
                    found[name] = item.get("source") or "unknown"
    except Exception:
        pass
    return found


def load_lists(ctx):
    data = read_json(ctx.lists_path, {})
    if not isinstance(data.get("lists"), dict):
        data = {"version": 1, "lists": {}}
    return data


def save_lists(ctx, data):
    data["version"] = 1
    write_json_atomic(ctx.lists_path, data)


def load_ledger(ctx):
    data = read_json(ctx.ledger_path, {})
    if not isinstance(data.get("skills"), dict):
        data = {"version": 1, "skills": {}, "history": []}
    return data


def save_ledger(ctx, data):
    data["version"] = 1
    data["history"] = data.get("history", [])[-MAX_HISTORY:]
    write_json_atomic(ctx.ledger_path, data)


def load_settings(ctx):
    data = read_json(ctx.settings, None)
    if not isinstance(data, dict):
        die("读不到或解析不了 %s" % ctx.settings)
    return data


def backup_settings(ctx):
    os.makedirs(ctx.backup_dir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    dst = os.path.join(ctx.backup_dir, "settings-%s.json" % stamp)
    n = 1
    while os.path.exists(dst):
        dst = os.path.join(ctx.backup_dir, "settings-%s-%d.json" % (stamp, n))
        n += 1
    shutil.copy2(ctx.settings, dst)
    olds = sorted(
        (os.path.join(ctx.backup_dir, n) for n in os.listdir(ctx.backup_dir)),
        key=os.path.getmtime,
    )
    while len(olds) > MAX_BACKUPS:
        os.remove(olds.pop(0))
    return dst


def write_settings(ctx, data, bak):
    before = {k: v for k, v in load_settings(ctx).items() if k != KEY}
    after = {k: v for k, v in data.items() if k != KEY}
    if json.dumps(before, sort_keys=True, ensure_ascii=False) != json.dumps(after, sort_keys=True, ensure_ascii=False):
        die("自检失败：除 %s 外的配置发生了变化，已放弃写入。备份：%s" % (KEY, bak))
    write_json_atomic(ctx.settings, data)


def _enable_ansi():
    if os.name != "nt":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        return True
    except Exception:
        return False


def _read_key():
    import msvcrt
    ch = msvcrt.getwch()
    if ch in ("\x00", "\xe0"):
        ch2 = msvcrt.getwch()
        return {"H": "up", "P": "down", "K": "left", "M": "right", "I": "pgup", "Q": "pgdn"}.get(ch2, "")
    if ch in ("\r", "\n"):
        return "enter"
    if ch == " ":
        return "space"
    low = ch.lower()
    if low == "q":
        return "quit"
    if low in ("a", "f", "j"):
        return low
    return ""


def _desc_map(ctx, full=False):
    """技能描述映射。full=False 取首行（适合列表显示），True 取完整描述（用于 token 估算）。"""
    out = {}
    try:
        with open(ctx.cache, "r", encoding="utf-8") as f:
            for item in json.load(f).get("results", []):
                raw = (item.get("description") or "").strip()
                if full:
                    out[item.get("name")] = " ".join(raw.split())
                else:
                    d = raw.splitlines()
                    out[item.get("name")] = d[0] if d else ""
    except Exception:
        pass
    return out


def _dmi_map(ctx):
    """技能是否已被排除出模型清单。

    缓存里的 disableModelInvocation 由技能自身的 frontmatter
    （disable-model-invocation: true）或插件声明决定。为 True 的技能不会进入
    系统提示的技能清单，因此基本不占上下文 token —— 实测对照：workbuddy-docx
    插件下 9 个技能，7 个为 True，系统提示里只出现剩下 2 个。
    """
    out = {}
    try:
        with open(ctx.cache, "r", encoding="utf-8") as f:
            for item in json.load(f).get("results", []):
                out[item.get("name")] = bool(item.get("disableModelInvocation"))
    except Exception:
        pass
    return out


def _clip(text, width):
    text = (text or "").replace("\t", " ")
    w = 0
    out = []
    for ch in text:
        w += 2 if ord(ch) > 0x2E80 else 1
        if w > width:
            out.append("…")
            break
        out.append(ch)
    return "".join(out)


def pick_interactive(ctx, list_name, members):
    """方向键勾选。返回 (选中列表, 动作)，动作为 save/quit。"""
    reg = registry(ctx)
    descs = _desc_map(ctx)
    rows = []
    for name in sorted(reg):
        rows.append({
            "name": name,
            "src": reg[name],
            "desc": descs.get(name, ""),
            "checked": name in members,
            "locked": reg[name] != "userSettings",
        })
    if not rows:
        die("没有扫描到任何技能。")

    page_size = 14
    page = 0
    pages = max(1, (len(rows) + page_size - 1) // page_size)
    cursor = 0
    filter_text = ""
    message = ""

    def view_rows():
        if not filter_text:
            return rows
        kw = filter_text.lower()
        return [r for r in rows if kw in r["name"].lower() or kw in r["desc"].lower()]

    while True:
        shown = view_rows()
        page = max(0, min(page, pages - 1))
        start = page * page_size
        chunk = shown[start:start + page_size]
        if chunk:
            cursor = max(0, min(cursor, len(chunk) - 1))
        total_checked = sum(1 for r in rows if r["checked"])

        lines = []
        lines.append("\x1b[1m名单：%s\x1b[0m   已选 %d 个   %s" % (
            list_name, total_checked,
            ("筛选：%s（f 清除）" % filter_text) if filter_text else ""))
        lines.append("↑↓ 移动   空格 勾选/取消   a 全选   j 搜索   f 清除筛选   ←→ 翻页   Enter 保存   q 放弃")
        lines.append("")
        for i, r in enumerate(chunk):
            mark = ">" if i == cursor else " "
            box = "[x]" if r["checked"] else "[ ]"
            tag = "" if r["src"] == "userSettings" else " \x1b[33m[%s 不可调]\x1b[0m" % (
                "内置" if r["src"] == "builtin" else "插件")
            name = _clip(r["name"], 34).ljust(34)
            desc = _clip(r["desc"], 46)
            line = " %s %s %s  \x1b[90m%s\x1b[0m%s" % (mark, box, name, desc, tag)
            if i == cursor:
                line = "\x1b[7m" + line + "\x1b[0m"
            lines.append(line)
        lines.append("")
        lines.append("第 %d/%d 页   共 %d 个技能（已筛选 %d）" % (page + 1, pages, len(rows), len(shown)))
        if message:
            lines.append("\x1b[33m" + message + "\x1b[0m")
            message = ""

        sys.stdout.write("\x1b[H\x1b[2J" + "\n".join(lines))
        sys.stdout.flush()

        key = _read_key()
        if key == "up":
            if cursor > 0:
                cursor -= 1
            elif page > 0:
                page -= 1
                cursor = page_size - 1
        elif key == "down":
            if cursor < len(chunk) - 1:
                cursor += 1
            elif page < pages - 1:
                page += 1
                cursor = 0
        elif key == "left":
            page = max(0, page - 1)
            cursor = 0
        elif key == "right":
            page = min(pages - 1, page + 1)
            cursor = 0
        elif key == "pgup":
            page = max(0, page - 1)
            cursor = 0
        elif key == "pgdn":
            page = min(pages - 1, page + 1)
            cursor = 0
        elif key == "space":
            if chunk:
                r = chunk[cursor]
                if r["locked"]:
                    message = "「%s」是内置/插件技能，关掉会破坏基础能力，已跳过。" % r["name"]
                else:
                    r["checked"] = not r["checked"]
                    if cursor < len(chunk) - 1:
                        cursor += 1
        elif key == "a":
            selectable = [r for r in shown if not r["locked"]]
            all_on = all(r["checked"] for r in selectable) if selectable else False
            for r in selectable:
                r["checked"] = not all_on
        elif key == "j":
            sys.stdout.write("\x1b[H\x1b[2J")
            sys.stdout.flush()
            try:
                kw = input("筛选关键词（留空取消）：").strip()
            except EOFError:
                kw = ""
            filter_text = kw
            page = 0
            cursor = 0
        elif key == "f":
            filter_text = ""
            page = 0
            cursor = 0
        elif key == "enter":
            sys.stdout.write("\x1b[H\x1b[2J")
            sys.stdout.flush()
            return [r["name"] for r in rows if r["checked"]], "save"
        elif key == "quit":
            sys.stdout.write("\x1b[H\x1b[2J")
            sys.stdout.flush()
            return [], "quit"


def cmd_pick(ctx, args):
    data = load_lists(ctx)
    exists = args.name in data["lists"]
    members = set(data["lists"].get(args.name, []) if args.append else [])

    if args.numbers or not sys.stdin.isatty():
        chosen = pick_numbers(ctx, args.name, members)
    else:
        if not _enable_ansi():
            print("当前终端不支持 ANSI 重绘，改用编号模式。")
            chosen = pick_numbers(ctx, args.name, members)
        else:
            chosen, action = pick_interactive(ctx, args.name, members)
            if action == "quit":
                print("已放弃，未保存任何改动。")
                return

    if not chosen:
        print("没有勾选任何技能，未保存。")
        return
    data = load_lists(ctx)
    if exists and not args.append:
        data["lists"][args.name] = list(chosen)
        verb = "已覆盖名单"
    elif exists:
        merged = data["lists"][args.name] + [c for c in chosen if c not in data["lists"][args.name]]
        data["lists"][args.name] = merged
        verb = "已追加到名单"
    else:
        data["lists"][args.name] = list(chosen)
        verb = "已建立名单"
    save_lists(ctx, data)
    final = data["lists"][args.name]
    print("%s「%s」，共 %d 个技能。" % (verb, args.name, len(final)))
    print("下一步：python skillset.py off \"%s\"   或   on \"%s\"" % (args.name, args.name))


def pick_numbers(ctx, list_name, preselected):
    reg = registry(ctx)
    names = sorted(reg)
    if not names:
        die("没有扫描到任何技能。")
    print("可用技能 %d 个（输入编号，支持 1 3 5-8，直接回车取消）：\n" % len(names))
    for i, n in enumerate(names, 1):
        mark = "*" if n in preselected else " "
        tag = "" if reg[n] == "userSettings" else "  [%s]" % ("内置" if reg[n] == "builtin" else "插件")
        print("%3d %s %s%s" % (i, mark, n, tag))
    try:
        raw = input("\n选择（空格或逗号分隔，* 为已在名单内）：").strip()
    except EOFError:
        raw = ""
    if not raw:
        return []
    picked = set()
    for token in raw.replace(",", " ").replace("，", " ").split():
        if "-" in token:
            a, _, b = token.partition("-")
            if a.isdigit() and b.isdigit():
                for k in range(int(a), int(b) + 1):
                    if 1 <= k <= len(names):
                        picked.add(names[k - 1])
        elif token.isdigit() and 1 <= int(token) <= len(names):
            picked.add(names[int(token) - 1])
        else:
            print("[忽略] 无法识别：%s" % token)
    locked = [n for n in picked if reg[n] != "userSettings"]
    if locked:
        print("[拦截] 以下内置/插件技能不允许加入操作范围：%s" % "、".join(sorted(locked)))
        picked = {n for n in picked if reg[n] == "userSettings"}
    if preselected:
        picked |= set(preselected)
    return sorted(picked)


def cmd_ls(ctx, args):
    lists = load_lists(ctx)["lists"]
    if not lists:
        print("还没有任何名单。用 new 建立，例如：python skillset.py new 编程 code-reviewer tdd-guide")
        return
    ov = load_settings(ctx).get(KEY) or {}
    descs = _desc_map(ctx, full=True)
    dmi = _dmi_map(ctx)
    print("共 %d 份名单：\n" % len(lists))
    total_saveable = 0.0
    for name in lists:
        members = lists[name]
        on = sum(1 for m in members if m not in ov)
        dormant = sum(1 for m in members if ov.get(m) == DORMANT)
        off = sum(1 for m in members if ov.get(m) == HARD_OFF)
        saveable = sum(skill_cost(m, descs.get(m, "")) for m in members
                       if m not in ov and not dmi.get(m))
        total_saveable += saveable
        print("  %-12s %3d 个技能   开启 %d / 休眠 %d / 关闭 %d   关闭可省 ~%s tok"
              % (name, len(members), on, dormant, off, fmt_tok(saveable)))
    if total_saveable:
        print("\n  合计关闭后约省 ~%s tok（只计当前真正注入的技能；按名称+描述估算）"
              % fmt_tok(total_saveable))


def cmd_cost(ctx, args):
    """按上下文开销排序，用于决定先关哪些。"""
    reg = registry(ctx)
    descs = _desc_map(ctx, full=True)
    dmi = _dmi_map(ctx)
    ov = load_settings(ctx).get(KEY) or {}

    rows = []
    for name, src in reg.items():
        if src != "userSettings" and not args.all:
            continue
        st = ov.get(name)
        status = "休眠" if st == DORMANT else ("关闭" if st == HARD_OFF else "开启")
        injected = (st is None) and not dmi.get(name)
        rows.append([name, src, status, injected, skill_cost(name, descs.get(name, ""))])

    # 已排除的排在最后：它们本来就不占上下文
    rows.sort(key=lambda r: (-(r[4] if r[3] else -1), r[0]))
    total_inj = sum(r[4] for r in rows if r[3])
    n_inj = sum(1 for r in rows if r[3])

    if args.top and args.top > 0:
        rows = rows[:args.top]

    w = max([len(r[0]) for r in rows] + [10])
    print("%-4s %-*s %-6s %8s %8s" % ("#", w, "技能", "状态", "token", "累计占比"))
    print("-" * (w + 36))
    cum = 0.0
    for i, (name, src, status, inj, cost) in enumerate(rows, 1):
        if inj:
            cum += cost
            print("%-4d %-*s %-6s %8s %8.0f%%" % (i, w, name, status, "~" + fmt_tok(cost),
                                                 cum / total_inj * 100 if total_inj else 0))
        else:
            print("%-4d %-*s %-6s %8s %9s" % (i, w, name, status, "—", "—"))
    print("-" * (w + 36))
    print("当前真正注入：%d 个，合计 ~%s tok" % (n_inj, fmt_tok(total_inj)))
    if args.top and args.top < n_inj:
        print("  上表只列前 %d 个，已覆盖注入总量的 %.0f%%；去掉 --top 看全部。"
              % (args.top, cum / total_inj * 100 if total_inj else 0))
    print("说明：数值为名称+完整描述的估算，SKILL.md 正文不计入；「—」表示该技能已声明")
    print("      disable-model-invocation 或已被你关闭，本来就不占上下文，关它没有收益。")
    if not args.all:
        print("提示：加 --all 可一并列出内置与插件技能（它们不可调整）。")


def cmd_show(ctx, args):
    lists = load_lists(ctx)["lists"]
    if args.name not in lists:
        die("没有名为「%s」的名单。现有：%s" % (args.name, "、".join(lists) or "无"))
    ov = load_settings(ctx).get(KEY) or {}
    ledger = load_ledger(ctx)["skills"]
    descs = _desc_map(ctx, full=True)
    dmi = _dmi_map(ctx)
    print("名单「%s」共 %d 个技能：\n" % (args.name, len(lists[args.name])))
    for m in lists[args.name]:
        st = ov.get(m)
        tag = "休眠" if st == DORMANT else ("关闭" if st == HARD_OFF else "开启")
        extra = ""
        if m in ledger:
            rec = ledger[m]
            extra = "   ← 最后由 %s 于 %s 设置" % (rec.get("source", "?"), rec.get("at", "?"))
        if dmi.get(m) and st is None:
            print("  [已排除] %-36s        —%s" % (m, extra))
        else:
            print("  [%s] %-38s ~%5s tok%s"
                  % (tag, m, fmt_tok(skill_cost(m, descs.get(m, ""))), extra))


def normalize_members(ctx, names, force):
    reg = registry(ctx)
    unknown, protected = [], []
    for n in names:
        if n not in reg:
            unknown.append(n)
        elif reg[n] != "userSettings":
            protected.append("%s(%s)" % (n, reg[n]))
    if unknown:
        print("[警告] 以下名字不在已安装技能中，可能写错：%s" % "、".join(unknown))
        if not force:
            die("已停止。确认无误可加 --force。")
    if protected:
        print("[警告] 以下属于内置或插件技能，关掉会破坏基础能力：%s" % "、".join(protected))
        if not force:
            die("已停止。确需操作可加 --force。")
    return reg


def cmd_new(ctx, args):
    data = load_lists(ctx)
    if args.name in data["lists"]:
        die("名单「%s」已存在。要追加成员请用 add。" % args.name)
    members = list(dict.fromkeys(args.members or []))
    normalize_members(ctx, members, args.force)
    data["lists"][args.name] = members
    save_lists(ctx, data)
    print("已建立名单「%s」，含 %d 个技能。" % (args.name, len(members)))
    if not members:
        print("目前是空名单，可用 add 追加成员。")


def cmd_add(ctx, args):
    data = load_lists(ctx)
    if args.name not in data["lists"]:
        die("没有名为「%s」的名单。" % args.name)
    normalize_members(ctx, args.members, args.force)
    before = len(data["lists"][args.name])
    for m in args.members:
        if m not in data["lists"][args.name]:
            data["lists"][args.name].append(m)
    save_lists(ctx, data)
    print("名单「%s」成员 %d → %d。" % (args.name, before, len(data["lists"][args.name])))


def cmd_rm(ctx, args):
    data = load_lists(ctx)
    if args.name not in data["lists"]:
        die("没有名为「%s」的名单。" % args.name)
    removed = [m for m in args.members if m in data["lists"][args.name]]
    data["lists"][args.name] = [m for m in data["lists"][args.name] if m not in args.members]
    save_lists(ctx, data)
    print("已从「%s」移除 %d 个成员：%s" % (args.name, len(removed), "、".join(removed) or "无匹配"))
    if not removed:
        print("提示：成员名不匹配，未做改动。")


def cmd_del(ctx, args):
    data = load_lists(ctx)
    if args.name not in data["lists"]:
        die("没有名为「%s」的名单。" % args.name)
    n = len(data["lists"].pop(args.name))
    save_lists(ctx, data)
    print("已删除名单「%s」（%d 个成员）。已写入的技能状态保持不变，需要恢复请用 on。" % (args.name, n))


def apply_to_settings(ctx, members, action, hard, dry, force):
    running = app_running(getattr(ctx, "config", None))
    if running and not force and not dry:
        die("检测到 %s 正在运行。运行中的应用会覆盖修改，请先完全退出（含托盘图标）。" % running)

    settings = load_settings(ctx)
    ov = settings.get(KEY) if isinstance(settings.get(KEY), dict) else {}
    ledger = load_ledger(ctx)
    recs = ledger["skills"]

    target_value = None
    if action == "off":
        target_value = HARD_OFF if hard else DORMANT

    changes = []
    for m in members:
        cur = ov.get(m)
        if target_value is None:
            if m in ov:
                changes.append((m, cur, "移除记录（恢复默认）"))
        else:
            if cur != target_value:
                changes.append((m, cur or OK, target_value))
    if not changes:
        print("这些技能已经是目标状态，无需改动。")
        return

    cur_list = getattr(ctx, "_current_list", "?")
    conflicts = []
    for m, _old, _new in changes:
        prev = recs.get(m)
        if prev and prev.get("source") and prev.get("source") != "名单:" + cur_list:
            conflicts.append((m, prev))

    print("将改动 %d 个技能：" % len(changes))
    for m, old, new in changes:
        print("    %-40s %s → %s" % (m, old, new))
    if conflicts:
        print("\n其中 %d 个技能当前状态来自其他名单，本次操作将按「最新指令」覆盖：" % len(conflicts))
        for m, prev in conflicts:
            print("    %-40s 原由 %s 于 %s 设为 %s" % (m, prev.get("source"), prev.get("at"), prev.get("value")))
    if dry:
        print("\n[演练] 未写入任何文件。")
        return

    for m, _old, _new in changes:
        rec = {"value": "default" if target_value is None else target_value,
               "at": now(), "source": "名单:" + cur_list}
        if target_value is None:
            ov.pop(m, None)
        else:
            ov[m] = target_value
        recs[m] = rec
    if ov:
        settings[KEY] = ov
    else:
        settings.pop(KEY, None)

    bak = backup_settings(ctx)
    write_settings(ctx, settings, bak)
    ledger["history"].append({
        "at": now(),
        "action": "on" if target_value is None else ("off-hard" if hard else "off"),
        "list": getattr(ctx, "_current_list", "?"),
        "count": len(changes),
        "skills": [c[0] for c in changes],
    })
    save_ledger(ctx, ledger)
    print("\n写入完成。备份：%s" % bak)
    print("重启 WorkBuddy 后生效。")


def cmd_on(ctx, args):
    ctx._current_list = args.name
    members = resolve_members(ctx, args.name)
    apply_to_settings(ctx, members, "on", False, args.dry_run, args.force)


def cmd_off(ctx, args):
    ctx._current_list = args.name
    members = resolve_members(ctx, args.name)
    apply_to_settings(ctx, members, "off", args.hard, args.dry_run, args.force)


def resolve_members(ctx, name):
    lists = load_lists(ctx)["lists"]
    if name not in lists:
        die("没有名为「%s」的名单。现有：%s" % (name, "、".join(lists) or "无"))
    if not lists[name]:
        die("名单「%s」是空的，先用 add 加成员。" % name)
    return lists[name]


def cmd_status(ctx, args):
    ov = load_settings(ctx).get(KEY) or {}
    ledger = load_ledger(ctx)["skills"]
    if not ov:
        print("当前所有技能都是默认状态（模型可自动调用），没有写入任何记录。")
        return
    dormant = sorted(k for k, v in ov.items() if v == DORMANT)
    off = sorted(k for k, v in ov.items() if v == HARD_OFF)
    print("配置文件：%s" % ctx.settings)
    print("休眠 %d 个（模型不自动调用，仍可 /技能名 手动调用）：" % len(dormant))
    for k in dormant:
        r = ledger.get(k, {})
        print("    %-40s %s" % (k, ("← %s @ %s" % (r.get("source", "?"), r.get("at", "?"))) if r else ""))
    print("关闭 %d 个（完全禁用）：" % len(off))
    for k in off:
        r = ledger.get(k, {})
        print("    %-40s %s" % (k, ("← %s @ %s" % (r.get("source", "?"), r.get("at", "?"))) if r else ""))
    reg = registry(ctx)
    ghost = [k for k in list(ov) if k not in reg]
    if ghost:
        print("\n注意：以下记录在已安装技能中找不到（可能已卸载），可安全清理：%s" % "、".join(ghost))


def cmd_log(ctx, args):
    hist = load_ledger(ctx).get("history", [])
    if not hist:
        print("还没有操作记录。")
        return
    for h in reversed(hist[-20:]):
        print("%s  %-8s 名单「%s」  %d 个技能" % (h.get("at"), h.get("action"), h.get("list"), h.get("count", 0)))


def cmd_restore(ctx, args):
    if not os.path.isdir(ctx.backup_dir):
        die("没有可用备份。")
    files = sorted(
        (os.path.join(ctx.backup_dir, n) for n in os.listdir(ctx.backup_dir)),
        key=os.path.getmtime,
    )
    if not files:
        die("没有可用备份。")
    running = app_running(getattr(ctx, "config", None))
    if running and not args.force:
        die("检测到 %s 正在运行，请先完全退出再恢复。" % running)
    shutil.copy2(files[-1], ctx.settings)
    print("已从备份恢复：%s" % files[-1])
    print("注意：ledger 未回滚，仅用于溯源，不影响功能。")


def add_common_flags(parser, force=True, dry=True):
    """让 --force / --dry-run 既能写在子命令前，也能写在子命令后。
    default=SUPPRESS 保证子命令未显式指定时不会覆盖全局已解析的值。"""
    if force:
        parser.add_argument("--force", action="store_true", default=argparse.SUPPRESS, help="允许应用运行中写入")
    if dry:
        parser.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS, help="只显示会改什么，不写文件")


def main():
    p = argparse.ArgumentParser(
        description="WorkBuddy 技能名单管理与一键开关",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n  python skillset.py new 编程 code-reviewer tdd-guide ponytail\n  python skillset.py off 编程\n  python skillset.py on 编程",
    )
    p.add_argument("--config-dir", default=DEFAULT_CONFIG, help="WorkBuddy 配置目录，默认 ~/.workbuddy")
    p.add_argument("--dry-run", action="store_true", help="只显示会改什么，不写文件")
    p.add_argument("--force", action="store_true", help="允许应用运行中写入 / 允许操作内置技能")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("ls", help="列出全部名单")
    s.set_defaults(func=cmd_ls)

    s = sub.add_parser("cost", help="按上下文开销排序，看先关哪些最划算")
    s.add_argument("--top", type=int, default=0, help="只看前 N 个，默认全部")
    s.add_argument("--all", action="store_true", help="同时列出内置与插件技能")
    s.set_defaults(func=cmd_cost)

    s = sub.add_parser("show", help="查看某名单的成员与状态")
    s.add_argument("name")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("new", help="新建名单")
    s.add_argument("name")
    s.add_argument("members", nargs="*")
    s.set_defaults(func=cmd_new)

    s = sub.add_parser("pick", help="交互式勾选技能组成名单（推荐）")
    s.add_argument("name")
    s.add_argument("--append", action="store_true", help="勾选结果追加到现有名单，而非整体覆盖")
    s.add_argument("--numbers", action="store_true", help="用编号输入代替方向键勾选")
    s.set_defaults(func=cmd_pick)

    s = sub.add_parser("add", help="向名单追加成员")
    s.add_argument("name")
    s.add_argument("members", nargs="+")
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("rm", help="从名单移除成员")
    s.add_argument("name")
    s.add_argument("members", nargs="+")
    s.set_defaults(func=cmd_rm)

    s = sub.add_parser("del", help="删除名单")
    s.add_argument("name")
    s.set_defaults(func=cmd_del)

    s = sub.add_parser("on", help="一键开启名单内技能")
    s.add_argument("name")
    add_common_flags(s)
    s.set_defaults(func=cmd_on)

    s = sub.add_parser("off", help="一键关闭名单内技能")
    s.add_argument("name")
    s.add_argument("--hard", action="store_true", help="彻底关闭，连手动调用也不行")
    add_common_flags(s)
    s.set_defaults(func=cmd_off)

    s = sub.add_parser("status", help="查看所有被改过状态的技能")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("log", help="查看最近的操作记录")
    s.set_defaults(func=cmd_log)

    s = sub.add_parser("restore", help="从最近备份恢复 settings.json")
    add_common_flags(s, dry=False)
    s.set_defaults(func=cmd_restore)

    args = p.parse_args()
    ctx = Ctx(os.path.abspath(os.path.expanduser(args.config_dir)))
    if not os.path.isfile(ctx.settings):
        die("找不到 %s" % ctx.settings)
    args.func(ctx, args)


if __name__ == "__main__":
    main()
