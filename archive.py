#!/usr/bin/env python3
"""每日热榜精选 → GitHub 归档。

从 cron job 5d587752a3af 的输出目录解析精选条目，归档到
/opt/daily-tech-digest 并推送到 GitHub。

设计要点：
- 幂等：按 URL 去重，已归档的不重复添加；无新增则静默退出（不打扰用户）
- index.json 是权威累积存储 —— 即使 cron 输出被清理，历史仍在
- 全量重写 docs/*.md，保证同日多条时命名规则一致（date.md / date-N.md）
- 推送走 Git Data API：国内网络下 git smart-HTTP 传输实测会挂死超时

退出码：0 = 正常（有新增或无新增），非 0 = 出错（会触发 cron 告警）
"""
import base64
import datetime
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time

REPO = pathlib.Path("/opt/daily-tech-digest")
DOCS = REPO / "docs"
INDEX = DOCS / "index.json"
CRON_OUT = pathlib.Path.home() / ".hermes/cron/output/5d587752a3af"
GH_REPO = "ldyers/daily-tech-digest"
HOT_HUB = "https://github.com/cxyfreedom/website-hot-hub"


def run(cmd, cwd=REPO, check=True, timeout=180):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"命令失败 {' '.join(cmd)}\nstdout: {r.stdout}\nstderr: {r.stderr}")
    return r.stdout.strip()


# 脚本模式（no_agent: true）输出没有"推荐理由/数据来源"文案，
# 用固定的事实性说明代替，不得杜撰推荐语。
SCRIPT_NOTE = (
    "由每日筛选脚本自动选出（非人工编辑）：按当日多平台热榜采集顺序取第 1 条，"
    "本条为 {p} 当日榜单首位。"
)
SCRIPT_SOURCE = (
    "本条由筛选脚本从当日多平台热榜采集数据中自动选出"
    "（8 平台：36Kr / B站 / GitHub / 抖音 / 掘金 / 少数派 / 微信读书 / 快手）。"
)
AGENT_SOURCE = "本条由 Hermes Agent 代理模式从当日多平台热榜采集数据中筛选。"


def parse_run(path):
    """从单份 cron 输出解析精选条目。返回 dict 或 None（格式不符/运行失败）。

    兼容两种输出：
    - agent 模式：中文模板（**标题** / 链接： / 平台： / 推荐理由： / 数据来源：）
    - 脚本模式：LLM 转述脚本 JSON 结果，实测两种形态——
      a) 内嵌 ```json 或单行 {"platform","title","url"} 块（08-24/25/27）
      b) markdown 行：> ### 标题 / 🔗 URL / **来源**：平台（08-29）
      c) 代理模式回退：**[平台] 标题** / 🔗 URL / **推荐理由**：…（08-30）
    """
    txt = path.read_text(encoding="utf-8", errors="replace")
    if "## Response" not in txt:
        return None
    resp = txt.split("## Response", 1)[-1].strip()
    if "[SILENT]" in resp:
        return None

    run_time = re.search(r"\*\*Run Time:\*\*\s*(.+)", txt)
    date_m = re.search(r"每日热榜精选\**\s*\((\d{4}-\d{2}-\d{2})\)", resp) or re.search(
        r"(\d{4}-\d{2}-\d{2})", resp
    )
    base = {
        "run_ts": path.stem,
        "run_time": run_time.group(1).strip() if run_time else None,
        "date": date_m.group(1) if date_m else path.stem[:10],
    }

    # 旧格式优先（信息最全：含推荐理由与候选池明细）
    url = re.search(r"链接：(\S+)", resp)
    title = re.search(r"^\*\*(.+?)\*\*\s*$", resp, re.M)
    if url and title:
        plat = re.search(r"平台：(.+)", resp)
        reason = re.search(r"推荐理由：(.+?)(?:\n\n|\n---|\Z)", resp, re.S)
        source = re.search(r"数据来源：(.+)", resp)
        return {
            **base,
            "title": title.group(1).strip(),
            "platform": plat.group(1).strip() if plat else "未知",
            "url": url.group(1).strip(),
            "reason": reason.group(1).strip() if reason else "",
            "source": source.group(1).strip() if source else "",
        }

    # 脚本模式 a)：JSON 块（fenced 或单行），要求 title 非空且 url 为 http(s)
    def _valid(d):
        return (
            isinstance(d, dict)
            and "error" not in d
            and isinstance(d.get("url"), str)
            and d["url"].strip().startswith("http")
            and isinstance(d.get("title"), str)
            and bool(d["title"].strip())
        )

    cands = []
    for block in re.findall(r"```(?:json)?\s*\n(.*?)```", resp, re.S):
        s = block.strip()
        if s.startswith("{"):
            try:
                cands.append(json.loads(s))
            except json.JSONDecodeError:
                pass
    for line in resp.splitlines():
        s = line.strip()
        if s.startswith("{") and s.endswith("}") and '"url"' in s:
            try:
                cands.append(json.loads(s))
            except json.JSONDecodeError:
                pass
    for d in reversed(cands):  # 取最后一个有效候选：最终结果通常在文末
        if _valid(d):
            plat = (d.get("platform") or "").strip() or "未知"
            plat = re.sub(r"\s*热榜$", "", plat)
            return {
                **base,
                "title": d["title"].strip(),
                "platform": plat,
                "url": d["url"].strip(),
                "reason": SCRIPT_NOTE.format(p=plat),
                "source": SCRIPT_SOURCE,
            }

    # 脚本模式 b)：markdown 行（> ### 标题 / 🔗 URL / **来源**：平台）
    # 代理模式回退变体（08-30 实测）：**[平台] 标题** / 🔗 URL / **推荐理由**：…
    murl = re.search(r"🔗\s*<?(\S+?)>?(?:\s|$)", resp)
    mtitle = re.search(r"^>\s*#{1,6}\s+(.+?)\s*$", resp, re.M)
    atitle = re.search(r"^\*\*\[(.+?)\]\s*(.+?)\*\*\s*$", resp, re.M)
    if murl and (mtitle or atitle):
        u = murl.group(1)
        md = re.match(r"\[.*?\]\((\S+?)\)", u)
        if md:
            u = md.group(1)
        if not u.startswith("http"):
            return None
        plat = "未知"
        pm = re.search(r"\*\*来源\*\*[：:]\s*(.+)", resp)
        if pm:
            plat = re.sub(r"\s*热榜$", "", pm.group(1).strip()) or "未知"
        reason = None
        if atitle:
            plat = re.sub(r"\s*热榜$", "", atitle.group(1).strip()) or plat
            rm = re.search(r"\*\*推荐理由\*\*[：:]?\s*(.+?)(?:\n\n|\n---|\Z)", resp, re.S)
            if rm:
                reason = rm.group(1).strip()
        return {
            **base,
            "title": (atitle.group(2) if atitle else mtitle.group(1)).strip(),
            "platform": plat,
            "url": u,
            "reason": reason or SCRIPT_NOTE.format(p=plat),
            "source": AGENT_SOURCE,
        }

    return None


def write_doc(path, p):
    path.write_text(
        f"""# {p['title']}

> 每日热榜精选 · {p['date']}

| 项目 | 内容 |
|---|---|
| 日期 | {p['date']} |
| 来源平台 | {p['platform']} |
| 原文链接 | [{p['url']}]({p['url']}) |
| 采集时间 | {p['run_time']} |

## 为什么值得看

{p['reason']}

## 数据来源

本条从当日多平台热榜中筛选得出。

- 候选池：{p['source']}
- 筛选逻辑：技术/AI/编程类优先 → 商业科技重大事件次之 → 要求有深度信息量，排除纯娱乐内容

---

<sub>由 [website-hot-hub]({HOT_HUB}) 采集，经 Hermes Agent 自动筛选归档。</sub>
""",
        encoding="utf-8",
    )


def write_readme(items):
    """items 已按日期降序。"""
    rows = "\n".join(
        f"| {p['date']} | [{p['title']}](docs/{p['file']}) | {p['platform']} |" for p in items
    )
    (REPO / "README.md").write_text(
        f"""# 每日技术热点归档 · Daily Tech Digest

每天从 8 个中文平台的热榜中筛选出**最有价值的 1 条**技术/科技资讯，自动归档留存。

热榜每天刷过去就没了，真正有信号量的那几条值得留下来。这个仓库就是干这个的。

## 索引

共 {len(items)} 篇。

| 日期 | 标题 | 平台 |
|---|---|---|
{rows}

全部归档在 [`docs/`](docs/) 目录，另有结构化数据 [`docs/index.json`](docs/index.json) 便于程序读取。

## 数据来源

采集覆盖 8 个平台：

**36Kr** · **B站** · **GitHub Trending** · **抖音** · **掘金** · **少数派** · **微信读书** · **快手**

每日候选池约 300–700 条，从中选出 1 条。

## 筛选标准

按优先级排序：

1. **技术 / AI / 编程**相关热点优先 —— AI 突破、新开源项目、语言与框架动态、开发者工具
2. **商业 / 科技产业**重大事件次之 —— 大厂战略、融资、行业变革
3. 要求**有深度信息量**，排除纯娱乐、八卦、短视频梗
4. 同等价值时优先技术类平台（36Kr / GitHub / 掘金）
5. 仍然并列则取排名靠前者

## 归档格式

每篇文档包含：

- **标题** —— 原始热榜标题
- **元信息表** —— 日期、来源平台、原文链接、采集时间
- **为什么值得看** —— 入选理由，说明这条的实际价值而非泛泛而谈
- **数据来源** —— 当日候选池规模与各平台条数明细

## 自动化

| 环节 | 实现 |
|---|---|
| 热榜采集 | [website-hot-hub]({HOT_HUB}) |
| 每日筛选 | [Hermes Agent](https://hermes-agent.nousresearch.com) 定时任务，13:00 |
| 自动归档 | 定时任务 13:30 解析当日结果，提交并推送本仓库 |

全流程无人工介入。归档脚本见 [`archive.py`](archive.py)。

## 致谢

热榜采集基于 [cxyfreedom/website-hot-hub]({HOT_HUB})，感谢原作者。

## License

MIT —— 归档内容的版权归各原文作者与平台所有，本仓库仅作摘要索引与个人留存之用。
""",
        encoding="utf-8",
    )


def write_docs_readme(items):
    rows = "\n".join(
        f"| [{p['file']}]({p['file']}) | {p['date']} | {p['title']} |" for p in items
    )
    (DOCS / "README.md").write_text(
        f"""# 归档目录

按日期命名，一天一条。同一天有多条时以 `-1` / `-2` 后缀区分。

共 {len(items)} 篇。

| 文件 | 日期 | 标题 |
|---|---|---|
{rows}

## index.json

同目录下的 `index.json` 是结构化版本，字段如下：

| 字段 | 说明 |
|---|---|
| `date` | 归档日期 |
| `title` | 热榜原标题 |
| `platform` | 来源平台 |
| `url` | 原文链接 |
| `reason` | 入选理由 |
| `source` | 当日候选池明细 |
| `run_time` | 采集时间 |
| `run_ts` | 采集任务运行标识 |
""",
        encoding="utf-8",
    )


def gh_api(args, payload=None, timeout=90):
    """调用 gh api。payload 写入临时文件再 --input，避免 shell 引号地狱。"""
    cmd = ["gh", "api"] + args
    tmp = None
    if payload is not None:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(payload, tmp, ensure_ascii=False)
        tmp.close()
        cmd += ["--input", tmp.name]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError(f"gh api 失败 {args}\n{r.stderr}")
        return r.stdout.strip()
    finally:
        if tmp:
            os.unlink(tmp.name)


def gh_identity(now):
    """提交者身份：优先 GitHub token 用户（本地与远程两侧用同一身份）。"""
    try:
        u = json.loads(gh_api(["user", "--jq", "{login: .login, id: .id}"]))
        name = u.get("login") or "archive-bot"
        email = f"{u.get('id', 0)}+{name}@users.noreply.github.com"
    except Exception:
        name = run(["git", "config", "user.name"], check=False) or "archive-bot"
        email = run(["git", "config", "user.email"], check=False) or "archive-bot@local"
    iso = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"name": name, "email": email, "date": iso}


def push_via_api(files, message, parent, identity=None):
    """通过 GitHub Git Data API 推送。

    message 会规范化为恰好一个尾部换行 —— 与本地 `git commit -m` 的
    规范化一致（实测 GitHub 原样存储传入字节，多一个少一个换行 SHA 都会不同）。
    

    国内网络下 git smart-HTTP 传输经常挂死（实测 push 超时 300s），
    而 gh api 走不同的 HTTPS 路径正常。这里构建 blob→tree→commit→ref。

    关键：不使用 base_tree，而是提交完整文件列表构成的全新 tree。
    base_tree 是增量合并语义，删除/重命名的文件会留在远程（实测踩过：
    docs 文件从 date.md 改名 date-1.md 后旧文件残留）。全量 tree 保证
    远程精确镜像本地文件集。

    files: [(repo内相对路径, bytes)]
    返回新 commit SHA。
    """
    tree = []
    for path, data in files:
        sha = gh_api(
            [f"repos/{GH_REPO}/git/blobs", "--jq", ".sha"],
            {"content": base64.b64encode(data).decode(), "encoding": "base64"},
        )
        tree.append({"path": path, "mode": "100644", "type": "blob", "sha": sha})

    tree_sha = gh_api([f"repos/{GH_REPO}/git/trees", "--jq", ".sha"], {"tree": tree})
    message = message.rstrip("\n") + "\n"
    commit_payload = {"message": message, "tree": tree_sha, "parents": [parent]}
    if identity:
        # 显式传 author/committer（实测 GitHub 原样存储，含 +0000 偏移），
        # 使服务端对象与本地确定性提交逐字节一致 → 同 SHA。
        commit_payload["author"] = identity
        commit_payload["committer"] = identity
    commit_sha = gh_api(
        [f"repos/{GH_REPO}/git/commits", "--jq", ".sha"],
        commit_payload,
    )
    try:
        gh_api(
            ["-X", "PATCH", f"repos/{GH_REPO}/git/refs/heads/main", "--jq", ".object.sha"],
            {"sha": commit_sha},
        )
    except RuntimeError as e:
        # 非快进：仅当远程 tip 与本次提交内容一致（同 tree）时才允许强推——
        # 此时被替换的只是内容重复的历史，不丢任何数据；否则报错终止。
        if "fast forward" not in str(e):
            raise
        tip = json.loads(gh_api([f"repos/{GH_REPO}/git/refs/heads/main"]))
        tip_c = json.loads(gh_api([f"repos/{GH_REPO}/git/commits/{tip['object']['sha']}"]))
        if tip_c["tree"]["sha"] != tree_sha:
            raise RuntimeError(
                f"远程 tip 内容与本次提交不一致，拒绝强推："
                f"tip tree {tip_c['tree']['sha']} != {tree_sha}"
            )
        gh_api(
            ["-X", "PATCH", f"repos/{GH_REPO}/git/refs/heads/main", "--jq", ".object.sha"],
            {"sha": commit_sha, "force": True},
        )
    return commit_sha


def align_local_to_remote(remote_sha):
    """fetch 失败时（本环境 git smart-HTTP 被断连）从 API 重建远程 commit 对象。

    GitHub 存的 commit 对象与本地 git commit 产出的字节不完全一致：
    消息不带尾部换行、时区偏移未知（API 只返回 Z 规范化时刻，实测存的是
    本机时区 +0800）。穷举时区（步长 30 分钟）与消息尾部两种形态，命中即
    写入对象并更新本地引用；全部未命中（如带 gpgsig 等额外头）则放弃对齐，
    保持本地平行历史——不影响后续推送（parent 每次取自远程 ref）。
    """
    try:
        c = json.loads(gh_api([f"repos/{GH_REPO}/git/commits/{remote_sha}"]))
    except Exception:
        return False
    tree = c["tree"]["sha"]
    parents = [p["sha"] for p in c["parents"]]
    a, cm = c["author"], c["committer"]
    ts_a = int(datetime.datetime.fromisoformat(a["date"].replace("Z", "+00:00")).timestamp())
    ts_c = int(datetime.datetime.fromisoformat(cm["date"].replace("Z", "+00:00")).timestamp())

    for off2 in range(-28, 29):  # -14h ~ +14h，步长 30 分钟
        sign = "+" if off2 >= 0 else "-"
        n = abs(off2)
        tz = f"{sign}{n // 2:02d}{(n % 2) * 30:02d}"
        for trail in (False, True):
            lines = [f"tree {tree}"] + [f"parent {x}" for x in parents]
            lines.append(f"author {a['name']} <{a['email']}> {ts_a} {tz}")
            lines.append(f"committer {cm['name']} <{cm['email']}> {ts_c} {tz}")
            raw = ("\n".join(lines) + "\n\n" + c["message"] + ("\n" if trail else "")).encode()
            r = subprocess.run(
                ["git", "hash-object", "-t", "commit", "--stdin"],
                cwd=REPO, input=raw, capture_output=True,
            )
            if r.stdout.decode().strip() == remote_sha:
                subprocess.run(
                    ["git", "hash-object", "-t", "commit", "-w", "--stdin"],
                    cwd=REPO, input=raw, capture_output=True,
                )
                subprocess.run(["git", "update-ref", "refs/heads/main", remote_sha], cwd=REPO, check=True)
                subprocess.run(
                    ["git", "update-ref", "refs/remotes/origin/main", remote_sha],
                    cwd=REPO, check=False,
                )
                return True
    return False


def main():
    if not REPO.is_dir():
        sys.exit(f"仓库目录不存在: {REPO}")

    # 已归档条目（权威累积存储）
    archived = json.loads(INDEX.read_text(encoding="utf-8")) if INDEX.exists() else []
    known = {p["url"] for p in archived}

    if not CRON_OUT.is_dir():
        return  # 输出目录还不存在，静默

    found = []
    for f in sorted(CRON_OUT.glob("*.md")):
        p = parse_run(f)
        if p:
            found.append(p)

    new = []
    seen = set(known)
    for p in found:
        if p["url"] not in seen:
            new.append(p)
            seen.add(p["url"])

    if not new:
        return  # 无新增 —— 静默退出，不打扰用户

    # 合并 + 全量重写
    allitems = archived + new
    allitems.sort(key=lambda x: (x["date"], x["run_ts"]))

    bydate = {}
    for p in allitems:
        bydate.setdefault(p["date"], []).append(p)

    for old in DOCS.glob("*.md"):
        if old.name != "README.md":
            old.unlink()

    for date, items in bydate.items():
        for i, p in enumerate(items, 1):
            p["file"] = f"{date}.md" if len(items) == 1 else f"{date}-{i}.md"
            write_doc(DOCS / p["file"], p)

    desc = sorted(allitems, key=lambda x: (x["date"], x.get("file", "")), reverse=True)
    write_readme(desc)
    write_docs_readme(desc)
    INDEX.write_text(json.dumps(allitems, ensure_ascii=False, indent=2), encoding="utf-8")

    # 本地提交（保留可读历史）
    run(["git", "add", "-A"])
    if not run(["git", "status", "--porcelain"]):
        return  # 内容无实质变化

    titles = "\n".join(f"- {p['date']} {p['title']}" for p in new)
    msg = f"docs: 归档 {len(new)} 条热榜精选\n\n{titles}"

    # 推送：以远程当前 main 为 parent（避免本地领先时构错父提交）
    remote_before = gh_api([f"repos/{GH_REPO}/git/refs/heads/main", "--jq", ".object.sha"])

    # 确定性提交要求本地 HEAD 与 remote_before 同父链，否则 SHA 必然不等。
    # 本地领先/分叉时：先补齐远程对象（缺则经 API 重建），再软回退——
    # 工作区/暂存区内容原样保留，未推送的本地提交折叠进本次提交。
    if run(["git", "rev-parse", "HEAD"], check=False) != remote_before:
        _e = subprocess.run(["git", "cat-file", "-e", remote_before],
                            cwd=REPO, capture_output=True)
        if _e.returncode != 0:
            align_local_to_remote(remote_before)
        run(["git", "reset", "-q", "--soft", remote_before], check=False)

    # 确定性提交：本地与 API 两侧用同一身份/时间/时区（+0000）构造同一 commit 对象，
    # 推送返回的 SHA 即本地 HEAD —— 对象本地天然已有，update-ref 即可对齐。
    now = int(time.time())
    ident = gh_identity(now)
    env = {**os.environ,
           "GIT_AUTHOR_NAME": ident["name"], "GIT_AUTHOR_EMAIL": ident["email"],
           "GIT_AUTHOR_DATE": f"{now} +0000",
           "GIT_COMMITTER_NAME": ident["name"], "GIT_COMMITTER_EMAIL": ident["email"],
           "GIT_COMMITTER_DATE": f"{now} +0000"}
    c = subprocess.run(["git", "commit", "-q", "-m", msg], cwd=REPO, env=env,
                       capture_output=True, text=True)
    if c.returncode != 0:
        sys.exit(f"git commit 失败: {c.stderr}")

    payload = []
    for rel in run(["git", "ls-files"]).splitlines():
        fp = REPO / rel
        if fp.is_file():
            payload.append((rel, fp.read_bytes()))

    try:
        new_sha = push_via_api(payload, msg, remote_before, identity=ident)
    except Exception as e:
        sys.exit(f"归档已本地提交但推送失败: {e}\n手动处理: cd {REPO} && git push origin main")

    # 独立验证：回读远程 ref，确认真的落地
    remote_after = gh_api([f"repos/{GH_REPO}/git/refs/heads/main", "--jq", ".object.sha"])
    if remote_after != new_sha:
        sys.exit(f"推送未生效！期望 {new_sha[:8]}，远程为 {remote_after[:8]}")

    # 本地对齐远程 commit，保证下次运行的 parent 正确。
    # 首选：确定性提交命中（本地 HEAD 即 new_sha），直接更新引用，无需网络。
    # 回退：fetch（本环境实测必失败）→ API 重建对象（align_local_to_remote）。
    local_now = run(["git", "rev-parse", "HEAD"], check=False)
    if local_now == new_sha:
        subprocess.run(["git", "update-ref", "refs/remotes/origin/main", new_sha],
                       cwd=REPO, check=False)
    else:
        fetched = subprocess.run(
            ["git", "fetch", "origin", "main"], cwd=REPO, capture_output=True, text=True, timeout=120
        )
        if fetched.returncode == 0:
            run(["git", "reset", "-q", "--hard", "FETCH_HEAD"], check=False)
        else:
            align_local_to_remote(new_sha)
    local_now = run(["git", "rev-parse", "HEAD"], check=False)
    aligned = local_now == new_sha

    lines = "\n".join(f"· {p['date']} 〔{p['platform']}〕{p['title']}" for p in new)
    warn = "" if aligned else f"\n\n⚠️ 本地 ref 未对齐远程（本地 {local_now[:8]} / 远程 {new_sha[:8]}），推送本身已成功。"
    print(
        f"📚 已归档 {len(new)} 条到 daily-tech-digest（共 {len(allitems)} 篇）\n\n"
        f"{lines}\n\nhttps://github.com/{GH_REPO}{warn}"
    )


if __name__ == "__main__":
    main()
