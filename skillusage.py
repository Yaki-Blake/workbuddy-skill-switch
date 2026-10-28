"""从 WorkBuddy 会话记录中提取真实的 token 与积分消耗，并按技能归集。

数据来源（全部只读，不写任何 WorkBuddy 文件）：
    <config>/projects/<项目>/<会话>.jsonl

    function_call 记录里带 providerData：
      rawUsage              prompt_tokens / completion_tokens / prompt_cache_hit_tokens / credit
      conversationRequestId 一次用户请求的标识（同一次请求的多个步骤共享）

    name == "Skill" 的调用记录，arguments 里是 {"skill": "<技能名>"}

归集口径（重要，页面必须如实标注）：
    一次用户请求的全部 token 与积分，记到该请求里调用过的技能名下。
    若同一请求调用了多个技能，成本会在各技能重复计入，另给出 multi 计数。

用法：
    python skillusage.py                 # 表格输出
    python skillusage.py --refresh       # 忽略缓存重扫
    python skillusage.py --json          # 输出 JSON
"""

import argparse
import json
import os
import sys
import time

CACHE_VERSION = 1


def now():
    return time.time()


def _ts(v):
    """把时间戳统一成 float 秒。兼容毫秒与 ISO 字符串。"""
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v) / 1000.0 if v > 1e11 else float(v)
    s = str(v).strip()
    try:
        return float(s)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            return time.mktime(time.strptime(s[:len("2026-01-01T00:00:00.000Z")], fmt))
        except Exception:
            continue
    return 0.0


def scan_file(path):
    """解析单个会话文件，返回按请求聚合的记录。

    返回 [[rid, ts, prompt, completion, cache_hit, credit, steps, [skills]]...]
    """
    reqs = {}
    skill_of = {}
    for line in open(path, "r", encoding="utf-8", errors="ignore"):
        if "rawUsage" not in line and "Skill" not in line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        pd = d.get("providerData") or {}
        rid = pd.get("conversationRequestId")
        if not rid:
            continue
        ru = pd.get("rawUsage")
        if isinstance(ru, dict):
            a = reqs.get(rid)
            if a is None:
                a = reqs[rid] = [0, 0, 0, 0.0, 0, 0.0]
            a[0] += int(ru.get("prompt_tokens") or 0)
            a[1] += int(ru.get("completion_tokens") or 0)
            a[2] += int(ru.get("prompt_cache_hit_tokens") or 0)
            a[3] += float(ru.get("credit") or 0.0)
            a[4] += 1
            a[5] = max(a[5], _ts(d.get("timestamp")))
        if d.get("name") == "Skill":
            try:
                arg = json.loads(d.get("arguments") or "{}")
            except Exception:
                arg = {}
            name = (arg or {}).get("skill")
            if name:
                skill_of.setdefault(rid, []).append(str(name))

    out = []
    for rid, a in reqs.items():
        out.append([rid, a[5], a[0], a[1], a[2], round(a[3], 4), a[4], skill_of.get(rid, [])])
    return out


def load_cache(cache_file):
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            d = json.load(f)
        if d.get("version") == CACHE_VERSION and isinstance(d.get("files"), dict):
            return d
    except Exception:
        pass
    return {"version": CACHE_VERSION, "files": {}}


def save_cache(cache_file, data):
    try:
        os.makedirs(os.path.dirname(cache_file), exist_ok=True)
        tmp = cache_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, cache_file)
    except Exception:
        pass


def collect(projects_dir, cache_file=None, refresh=False):
    files = []
    if os.path.isdir(projects_dir):
        for proj in sorted(os.listdir(projects_dir)):
            pdir = os.path.join(projects_dir, proj)
            if not os.path.isdir(pdir):
                continue
            for fn in os.listdir(pdir):
                if fn.endswith(".jsonl"):
                    files.append((proj, os.path.join(pdir, fn)))

    cache = {"version": CACHE_VERSION, "files": {}} if refresh else load_cache(cache_file)
    cached = cache.get("files", {})
    new_cache = {}
    parsed = 0
    t0 = now()

    for proj, path in files:
        key = os.path.abspath(path)
        try:
            st = os.stat(path)
        except OSError:
            continue
        hit = cached.get(key)
        if hit and hit.get("m") == st.st_mtime and hit.get("s") == st.st_size:
            new_cache[key] = hit
            continue
        try:
            reqs = scan_file(path)
        except Exception:
            continue
        parsed += 1
        new_cache[key] = {
            "m": st.st_mtime,
            "s": st.st_size,
            "p": proj,
            "n": os.path.splitext(os.path.basename(path))[0],
            "r": reqs,
        }

    if cache_file:
        save_cache(cache_file, {"version": CACHE_VERSION, "files": new_cache})

    return _aggregate(new_cache, parsed, now() - t0)


def _aggregate(files, parsed, elapsed):
    totals = {"prompt": 0, "completion": 0, "cache_hit": 0, "credit": 0.0,
              "steps": 0, "requests": 0, "skill_rounds": 0, "multi": 0,
              "skill_credit": 0.0}
    skills = {}
    recent = []
    sessions = {}

    for info in files.values():
        proj, sess = info.get("p", ""), info.get("n", "")
        s_tot = sessions.setdefault(sess, {"session": sess, "project": proj,
                                           "credit": 0.0, "tokens": 0, "requests": 0})
        for rid, ts, p, c, ch, cr, steps, names in info.get("r", []):
            totals["prompt"] += p
            totals["completion"] += c
            totals["cache_hit"] += ch
            totals["credit"] += cr
            totals["steps"] += steps
            totals["requests"] += 1
            s_tot["credit"] += cr
            s_tot["tokens"] += p + c
            s_tot["requests"] += 1

            if not names:
                continue
            totals["skill_rounds"] += 1
            totals["skill_credit"] += cr
            if len(names) > 1:
                totals["multi"] += 1
            for n in names:
                a = skills.setdefault(n, {"name": n, "calls": 0, "rounds": 0, "prompt": 0,
                                          "completion": 0, "cache_hit": 0, "credit": 0.0,
                                          "steps": 0, "sessions": set(), "last_at": 0.0})
                a["calls"] += 1
                a["rounds"] += 1
                a["prompt"] += p
                a["completion"] += c
                a["cache_hit"] += ch
                a["credit"] += cr
                a["steps"] += steps
                a["sessions"].add(sess)
                a["last_at"] = max(a["last_at"], ts)
                recent.append({"skill": n, "session": sess, "project": proj,
                               "credit": round(cr, 4), "tokens": p + c,
                               "steps": steps, "at": ts})

    out_skills = []
    for a in skills.values():
        out_skills.append({
            "name": a["name"], "calls": a["calls"],
            "prompt": a["prompt"], "completion": a["completion"],
            "cache_hit": a["cache_hit"], "credit": round(a["credit"], 4),
            "steps": a["steps"], "sessions": len(a["sessions"]),
            "avg_credit": round(a["credit"] / a["calls"], 4) if a["calls"] else 0,
            "last_at": a["last_at"],
        })
    out_skills.sort(key=lambda x: (-x["credit"], -x["calls"], x["name"]))
    recent.sort(key=lambda x: -x["at"])

    avg_step_prompt = totals["prompt"] / totals["steps"] if totals["steps"] else 0
    totals["avg_step_prompt"] = round(avg_step_prompt, 1)
    totals["credit"] = round(totals["credit"], 4)
    totals["skill_credit"] = round(totals["skill_credit"], 4)

    sess_list = sorted(sessions.values(), key=lambda x: -x["credit"])[:20]
    for s in sess_list:
        s["credit"] = round(s["credit"], 4)

    return {
        "ok": True,
        "scanned": {"files": len(files), "parsed": parsed,
                    "elapsed": round(elapsed, 2)},
        "totals": totals,
        "skills": out_skills,
        "recent": recent[:40],
        "sessions": sess_list,
    }


def fmt(n):
    n = float(n or 0)
    if n >= 1e6:
        return "%.2fM" % (n / 1e6)
    if n >= 1e3:
        return "%.1fk" % (n / 1e3)
    return "%.0f" % n


def print_table(data):
    t = data["totals"]
    sc = data["scanned"]
    print("扫描 %d 个会话文件（重解析 %d 个，耗时 %.2fs）" % (sc["files"], sc["parsed"], sc["elapsed"]))
    print()
    print("总请求 %d 次，计费步骤 %d 步" % (t["requests"], t["steps"]))
    print("输入 token %s，其中缓存命中 %s（命中率 %.1f%%）"
          % (fmt(t["prompt"]), fmt(t["cache_hit"]),
             (t["cache_hit"] / t["prompt"] * 100) if t["prompt"] else 0))
    print("输出 token %s" % fmt(t["completion"]))
    print("积分合计 %.2f" % t["credit"])
    print("平均每次请求输入 %s token" % fmt(t["avg_step_prompt"] * 1))
    print()
    if not data["skills"]:
        print("没有找到任何技能调用记录。")
        return
    print("按技能归集（成本记到调用该技能的那一轮）：")
    print("%-34s %6s %10s %8s %10s" % ("技能", "调用", "输入token", "积分", "平均积分"))
    print("-" * 72)
    for s in data["skills"]:
        print("%-34s %6d %10s %8.2f %10.2f"
              % (s["name"], s["calls"], fmt(s["prompt"]), s["credit"], s["avg_credit"]))
    print("-" * 72)
    n_skill = sum(s["credit"] for s in data["skills"])
    if t["credit"]:
        print("技能相关轮次占总积分 %.1f%%（其余为未调用技能时的开销）"
              % (n_skill / t["credit"] * 100))
    if t["multi"]:
        print("注意：有 %d 轮同时调用了多个技能，成本在各技能重复计入。" % t["multi"])


def main():
    ap = argparse.ArgumentParser(description="WorkBuddy 技能的真实 token 与积分消耗")
    ap.add_argument("--config-dir", default=os.path.expanduser("~/.workbuddy"))
    ap.add_argument("--refresh", action="store_true", help="忽略缓存，全部重扫")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    a = ap.parse_args()

    projects = os.path.join(a.config_dir, "projects")
    cache = os.path.join(a.config_dir, "skill-switch", "usage-cache.json")
    data = collect(projects, cache, a.refresh)

    if a.json:
        json.dump(data, sys.stdout, ensure_ascii=False, indent=1)
        print()
    else:
        print_table(data)


if __name__ == "__main__":
    main()
