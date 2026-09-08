#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把站点 HTML 内嵌 `const article = {...}` 里的英文段落批量翻译成中文。

调用本地 OpenAI 兼容接口（默认 http://192.168.10.6:8000/v1/chat/completions）。

相对旧版的主要优化
------------------
1. 磁盘翻译缓存（translation memory）
   - 相同英文只向模型请求一次；跨文件、跨次运行复用，保证术语一致。
   - 中断后重跑可断点续传：已翻译过的直接命中缓存，不再重复调用模型。
2. 线程池并发请求
   - 每个文件内先对相同文本去重，再并发调用模型，避免逐条串行等待。
3. 失败重试 + 指数退避
   - 对网络抖动、429、5xx 自动重试；最终失败保留原文，不影响后续文件。
4. 保留文本前后空白
   - 只翻译去掉首尾空白后的核心文字，节点前后的换行/缩进原样保留，不破坏排版。
5. 精准“原块替换”，不再整文件重写
   - 不再用 BeautifulSoup 重写整份 HTML，只定位并替换 `const article = {...};`
     那一小段，避免全文件格式化噪声、HTML 实体转义与无意义的 diff。
6. 健壮的 JSON 定位
   - 用“花括号深度 + 字符串状态”扫描（替代非贪婪正则），可正确处理字符串内含
     `};`、花括号等内容的段落。
7. 幂等且最小写盘
   - 已含中文的内容自动跳过；只有 article 块确实变化时才写回文件。
8. 命令行参数
   - --root / --only / --workers / --retries / --cache / --check / --quiet 等。

用法示例
--------
python tools/f.py                           # 全站翻译并写回
python tools/f.py --only doc/current/gacui  # 只处理含该子串的 html
python tools/f.py --check                   # 离线统计剩余待翻译段落（不联网、不写文件）
python tools/f.py --workers 2 --retries 2   # 调低并发与重试次数
python tools/f.py --file-workers 4            # 需要提速：多文件并行（各文件完成后仍即时写盘）
python tools/f.py --batch 16                  # 可选：两遍式批量（更快，但写盘延后到末尾）
python tools/f.py --batch 1                   # 默认：关闭批量，逐文件即时翻译、写盘、落缓存
python tools/f.py --save-after 1              # 默认：有新增缓存即写盘（落盘及时）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

# ---------------- 配置 ----------------
DEFAULT_API_URL = "http://192.168.10.6:8000/v1/chat/completions"
DEFAULT_MODEL = "Qwen3-VL-30B-A3B-Instruct-MLX-4bit"
TEMPERATURE = 0.3
REQUEST_TIMEOUT = 120          # 单次请求超时（秒）
MAX_CORE_LEN = 2000            # 超过此长度的核心文本不再翻译（防单请求过长）

ARTICLE_MARKER = "const article = "
SCRIPT_RE = re.compile(r"<script\b[^>]*>(.*?)</script>", re.I | re.S)

# 逐条请求的系统提示词
SYSTEM_PROMPT = (
    "You are a professional translator for C++ GUI framework "
    "documentation (GacUI). Translate English prose to Simplified "
    "Chinese. IMPORTANT: if the text is a code identifier, type "
    "name, class/member/method/event name, enum value, XML tag, "
    "file/resource name, namespace, or technical keyword, return "
    "it EXACTLY unchanged (do NOT translate, do NOT add spaces). "
    "Only translate genuine natural-language sentences. Output only "
    "the result, no extra explanation."
)
# 批量请求的系统提示词（按行首索引逐行翻译、保持行数与顺序）
SYSTEM_PROMPT_BATCH = (
    "You are a professional translator for C++ GUI framework "
    "documentation (GacUI). Translate English prose to Simplified "
    "Chinese. Keep code identifiers, type/member/method/event names, "
    "enum values, XML tags, file/resource names, namespaces and "
    "technical keywords EXACTLY unchanged. The user message contains "
    "several lines, each prefixed with an index like \"[3] text\". "
    "Translate each line's English text into Chinese and reply with "
    "exactly the same number of lines, each starting with the same "
    "index prefix like \"[3] \", in the same order. Output only those "
    "indexed lines, nothing else."
)
_PAD_RE = re.compile(r"^(\s*)(.*?)(\s*)$", re.S)
_HAS_CJK = re.compile(r"[\u4e00-\u9fff]")
# 行分隔（换行+缩进）：逐行翻译，从而保留原文的换行/缩进排版样式
_LINE_RE = re.compile(r"(\s*(?:[\r\n]\s*)+)")
# 去掉中文全角标点前多余的空格（如 "时 ，" -> "时，"）
_FW_PUNCT_SPACE = re.compile(r"[ \t]+([，。；：！？、）】])")
# 孤立英文半角标点 -> 中文全角（仅当整段已含中文时应用）
_PUNCT_MAP = str.maketrans({',': '，', ';': '；', ':': '：'})

# 批量请求：行首索引（如 "[3] text"）用于对齐与校验
_BATCH_IDX = re.compile(r"^\[\s*(\d+)\s*\]\s*(.*)$")
MAX_BATCH_ITEM = 200    # 超过此长度的片段单独请求（防止被模型折行破坏对齐）
MAX_BATCH_CHARS = 1200  # 单批累计字符上限


# ---------------- 判断是否应该翻译 ----------------
_CODE_KEYWORDS = {
    'if', 'else', 'for', 'while', 'return', 'class', 'struct', 'void',
    'int', 'float', 'double', 'char', 'bool', 'template', 'typename',
    'using', 'namespace', 'std', 'cout', 'endl', 'new', 'delete',
    'public', 'private', 'protected', 'virtual', 'override', 'const',
    'static', 'inline', 'extern', 'operator', 'sizeof', 'typedef',
    'enum', 'union', 'true', 'false', 'nullptr', 'auto', 'decltype',
    'noexcept', 'nullptr_t', 'wchar_t', 'short', 'long', 'signed',
    'unsigned', 'friend', 'explicit', 'export', 'register', 'volatile',
}


def should_translate(text: str) -> bool:
    """返回 True 表示这是值得翻译的自然语言英文。"""
    t = text.strip()
    if not t:
        return False
    if len(t) < 3:                     # 太短（标点/缩写）
        return False
    if _HAS_CJK.search(t):             # 已含中文 -> 不重复翻译
        return False
    if len(t) > MAX_CORE_LEN:          # 过长 -> 跳过
        return False

    total = len(t)
    letters = sum(1 for ch in t if ch.isascii() and ch.isalpha())
    symbols = sum(1 for ch in t if not (ch.isascii() and (ch.isalnum())) and not ch.isspace())

    if letters / total < 0.3:          # 字母占比过低 -> 数字/符号/路径
        return False
    if symbols / total > 0.2:          # 符号占比过高 -> 代码特征明显
        return False

    # 代码关键字黑名单
    words = re.findall(r'\b[a-zA-Z]+\b', t)
    if words:
        keyword_count = sum(1 for w in words if w.lower() in _CODE_KEYWORDS)
        if keyword_count == len(words) or keyword_count / len(words) > 0.5:
            return False

    # 路径 / 文件名特征
    if t.count('/') >= 2 or t.count('\\') >= 2:
        return False
    if '.' in t and re.search(r'\.[a-zA-Z0-9]{1,5}', t) and not re.search(r'\s', t):
        return False

    # 判断是否为“正文句子”：含 >=3 个非关键字英文词。
    # 是正文则允许其中出现函数名/->/分号/等号等代码记号
    # （这些记号原样保留，交由模型按提示处理），整体仍作正文翻译；
    # 不足 3 个词的片段才按“纯代码/标识符”过滤。
    lc_words = [w.lower() for w in re.findall(r'[A-Za-z]{3,}', t)]
    is_prose = sum(1 for w in lc_words if w not in _CODE_KEYWORDS) >= 3

    # 等号 / 分号 -> 代码特征（正文句子除外；HTML 实体如 &quot; 内的分号先剔除）
    if not is_prose and (
            '=' in t or ';' in re.sub(r'&[A-Za-z0-9#]+;', '', t)):
        return False
    # 函数调用/声明：字母或数字与 "(" 直接相邻（如 GetX(、Foo::Bar(）
    if not is_prose and ')' in t and re.search(r'[A-Za-z0-9_]\(', t):
        return False

    # 全大写缩写（如 VCZH、GACUI）：仅当整段没有任何小写字母时才视为“纯缩写”；
    # 正文中出现 JSON/XML/HTTP 等术语词（如 "JSON parser"）不拦截，
    # 交由模型的“保留技术术语”提示原样保留。
    if re.search(r'[A-Z]{4,}', t) and not re.search(r'[a-z]', t):
        return False

    # 无空格、且没有小写字母的单 token（缩写/标签/标识符，如 ALT、RGB、3D）
    if not re.search(r'\s', t) and re.fullmatch(r'[A-Za-z0-9_-]+', t) \
            and not re.search(r'[a-z]', t):
        return False

    # 无空格、且含“内部大写”的 token → 驼峰式标识符，不翻译
    # （osSuper、mouseDown、GacUI…；不含仅首字母大写的普通词，如 Windows）
    if not re.search(r'\s', t) and re.search(r'[A-Z]', t[1:]) \
            and re.fullmatch(r'[A-Za-z0-9_]+', t):
        return False

    # 结构上明显是“标识符/字面量”的单 token → 不翻译
    # （snake_case 资源键、XML/模板标签、C++ 作用域、含数字代号、特殊前缀）
    if not re.search(r'\s', t):
        if '_' in t or '<' in t or '>' in t or '::' in t or '->' in t:
            return False
        if re.search(r'\d', t):            # Mouse4、CppXml、Demo2 等代号
            return False
        if re.match(r'^[$#@`\\]', t):
            return False

    return True


def split_padding(text: str):
    """把文本拆成 (前导空白, 核心文字, 尾随空白)。"""
    m = _PAD_RE.match(text or '')
    if not m:
        return '', text or '', ''
    return m.group(1), m.group(2), m.group(3)


# 作为“行内强调/字面量”的父节点类型：其中的单个单词通常是不需要翻译的标识符
LITERAL_PARENTS = {"Strong"}


def is_translatable(core: str, parent_kind=None) -> bool:
    """核心文字是否值得翻译。

    - 加粗（Strong）里的单个单词视为字面量，不翻译（如 osSuper、Windows、Left）；
    - 其余交给 should_translate 判定。
    """
    if not core:
        return False
    if parent_kind in LITERAL_PARENTS and not re.search(r"\s", core):
        return False
    return should_translate(core)


def iter_text_nodes(obj, out, parent=None):
    """递归收集需要翻译的字段：Text 节点的 "text"，以及含 "title" 的标题（如 Topic）。

    out 元素为 (节点dict, 字段名, 父节点 kind)；父节点用于识别“加粗字面量”等。
    """
    if isinstance(obj, dict):
        kind = obj.get("kind")
        if kind == "Text" and isinstance(obj.get("text"), str):
            out.append((obj, "text", parent))
        title = obj.get("title")
        if isinstance(title, str) and title.strip():
            out.append((obj, "title", parent))
        for value in obj.values():
            iter_text_nodes(value, out, parent=kind)
    elif isinstance(obj, list):
        for item in obj:
            iter_text_nodes(item, out, parent=parent)


# ---------------- 定位 article 块 ----------------
def extract_balanced_object(s: str, start: int) -> str:
    """从 s[start]=='{' 开始，按括号深度 + 字符串状态扫描，返回配对的 {...}。"""
    depth = 0
    in_str = False
    esc = False
    i = start
    n = len(s)
    while i < n:
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == '\\':
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    return s[start:i + 1]
        i += 1
    raise ValueError("JSON 块未闭合")


def find_article_span(html: str):
    """返回首个含 article 的块在 html 中的 (绝对起始下标, 块文本) 或 None。"""
    for m in SCRIPT_RE.finditer(html):
        body = m.group(1)
        body_start = m.start(1)
        pos = body.find(ARTICLE_MARKER)
        if pos < 0:
            pos = body.find("const article=")
        if pos < 0:
            continue
        brace = body.find('{', pos)
        if brace < 0:
            continue
        try:
            block = extract_balanced_object(body, brace)
        except ValueError:
            continue
        return body_start + brace, block
    return None


def load_article_data(path: Path):
    """读取并解析一个 html 的 article JSON；失败返回 None（供批量两遍式使用）。"""
    try:
        html = path.read_text(encoding="utf-8")
    except Exception:
        return None
    span = find_article_span(html)
    if span is None:
        return None
    try:
        return json.loads(span[1])
    except json.JSONDecodeError:
        return None


# ---------------- 翻译器（带缓存 + 并发 + 重试） ----------------
class Translator:
    def __init__(self, api_url, model, cache_path, workers=4,
                 retries=3, temperature=TEMPERATURE, quiet=False):
        self.api_url = api_url
        self.model = model
        self.cache_path = Path(cache_path)
        self.workers = max(1, workers)
        self.retries = max(0, retries)
        self.temperature = temperature
        self.quiet = quiet

        self.cache = self._load_cache()
        self._memo = {}          # 本次运行内 原文->译文 记忆（含失败 None）
        self._promises = {}      # 全局去重：原文 -> Future（跨文件复用同一请求）
        self._lock = threading.Lock()
        self._session = requests.Session()
        # 共享请求线程池：文件级并行时仍把模型并发限制在 workers 内
        self._executor = ThreadPoolExecutor(max_workers=self.workers)
        self._dirty = 0          # 自上次写盘以来新增的缓存条目数

        self.stats = {"requested": 0, "cached": 0, "skipped": 0, "failed": 0}

    # ---------- 缓存 ----------
    def _load_cache(self):
        if not self.cache_path.exists():
            return {}
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception as e:
            print(f"[警告] 缓存读取失败，将新建缓存: {e}")
            return {}

    def save_cache(self):
        """原子写盘（锁内快照，避免并发改动损坏），成功后重置脏计数。"""
        try:
            with self._lock:
                snapshot = dict(self.cache)
            tmp = self.cache_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=1),
                encoding="utf-8")
            tmp.replace(self.cache_path)
            self._dirty = 0
        except Exception as e:
            print(f"[警告] 缓存写入失败: {e}")

    def dirty(self) -> int:
        """自上次写盘以来新增的缓存条目数（用于周期性落盘）。"""
        with self._lock:
            return self._dirty

    # ---------- 通用请求（带重试），返回文本或 None ----------
    def _request_text(self, payload):
        for attempt in range(self.retries + 1):
            try:
                resp = self._session.post(self.api_url, json=payload,
                                          timeout=REQUEST_TIMEOUT)
                if resp.status_code in (408, 429) or resp.status_code >= 500:
                    raise requests.exceptions.HTTPError(
                        f"HTTP {resp.status_code}", response=resp)
                resp.raise_for_status()
                out = (resp.json()["choices"][0]["message"]["content"] or "").strip()
                if not out:
                    raise ValueError("模型返回空内容")
                return out
            except Exception:
                if attempt < self.retries:
                    time.sleep(min(2 ** attempt, 8))   # 指数退避
        return None

    # ---------- 单条请求 ----------
    def _call_one(self, core: str):
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": core},
            ],
            "temperature": self.temperature,
        }
        out = self._request_text(payload)
        if out is None:
            with self._lock:
                self.stats["failed"] += 1
            if not self.quiet:
                print(f"  [失败] {core[:40]!r}")
            return None
        with self._lock:
            self.stats["requested"] += 1
        if not self.quiet:
            print(f"  [请求] {core[:40]!r} -> {out[:40]!r}")
        return out

    # ---------- 批量请求（多条短片段合并成一次请求，显著减少请求数） ----------
    def _call_batch(self, parts):
        lines = [f"[{i}] {p}" for i, p in enumerate(parts)]
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT_BATCH},
                {"role": "user", "content": "\n".join(lines)},
            ],
            "temperature": self.temperature,
        }
        out = self._request_text(payload)
        if out is None:
            return None
        result = {}
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            m = _BATCH_IDX.match(line)
            if not m:
                return None                    # 格式不符 -> 整批逐条兜底
            result[int(m.group(1))] = m.group(2).strip()
        if len(result) != len(parts) or any(i not in result for i in range(len(parts))):
            return None                        # 行数/索引缺失 -> 整批逐条兜底
        with self._lock:
            self.stats["requested"] += len(parts)
        if not self.quiet:
            for i, p in enumerate(parts):
                print(f"  [批量] {p[:40]!r} -> {result[i][:40]!r}")
        return {parts[i]: result[i] for i in range(len(parts))}

    def _batch_worker(self, batch):
        try:
            res = self._call_batch(batch)
        except Exception:
            res = None
        if res is None:
            res = {}
            for p in batch:
                r = self._call_one(p)          # 批量失败则逐条兜底
                if r is not None:
                    res[p] = r
        return res

    # ---------- 批量两遍式：第 1 遍收集待译片段 ----------
    def collect_file_parts(self, data, out: set):
        """收集一个文件里所有需要联网翻译的片段（与 translate_nodes 判定一致）。"""
        nodes = []
        iter_text_nodes(data, nodes)
        for nd, field, parent in nodes:
            _, core, _ = split_padding(nd.get(field) or "")
            if not core or not is_translatable(core, parent):
                continue
            parts = _LINE_RE.split(core)
            for i in range(0, len(parts), 2):
                part = parts[i]
                if part and is_translatable(part, parent) \
                        and part not in self.cache and part not in self._memo:
                    out.add(part)

    def translate_batches(self, parts, batch_size):
        """把一批待译片段按 batch_size 分组并发请求，结果写回缓存。"""
        batch_size = max(1, batch_size)
        small, big = [], []
        for p in parts:
            (small if len(p) <= MAX_BATCH_ITEM else big).append(p)
        jobs = [[p] for p in big]              # 长片段单独请求
        cur, curlen = [], 0
        for p in sorted(small):
            if len(cur) >= batch_size or (cur and curlen + len(p) > MAX_BATCH_CHARS):
                jobs.append(cur)
                cur, curlen = [], 0
            cur.append(p)
            curlen += len(p)
        if cur:
            jobs.append(cur)
        futs = []
        with self._lock:
            for job in jobs:
                futs.append(self._executor.submit(self._batch_worker, job))
        for f in futs:
            try:
                res = f.result()
            except Exception:
                res = None
            if res:
                with self._lock:
                    for p, r in res.items():
                        self.cache[p] = r
                        self._memo[p] = r
                        self._dirty += 1
            if self._dirty >= 200:
                self.save_cache()

    # ---------- 并发请求一批（共享线程池 + 全局去重） ----------
    def _resolve(self, cores):
        """把一批原文注册进共享线程池并等待结果。

        - 相同原文若已被其它线程（或文件）提交过，则复用同一 Future，避免重复请求；
        - 成功结果写入 cache/_memo，并累加脏计数供周期性写盘。
        """
        futs = {}
        with self._lock:
            for c in cores:
                if c in self._promises:
                    futs[c] = self._promises[c]
                else:
                    f = self._executor.submit(self._call_one, c)
                    self._promises[c] = f
                    futs[c] = f
        added = 0
        for c, f in futs.items():
            try:
                r = f.result()
            except Exception:
                r = None
            if r is not None:
                with self._lock:
                    self.cache[c] = r
                    self._memo[c] = r
                    added += 1
                    self._dirty += 1
        return added

    # ---------- 翻译一批 Text 节点 ----------
    def translate_nodes(self, nodes) -> int:
        """就地翻译节点列表，返回实际发生改变的节点数。

        按“换行+缩进”把核心文字切成若干行块分别翻译，再原样拼回分隔符，
        从而保留原文的排版样式（例如 `",\\n                returning"`
        会译成 `"，\\n                返回"`，而不是被压成一行）。
        """
        entries = []
        need_net = set()
        for nd, field, parent in nodes:
            orig = nd.get(field) or ""
            pre, core, post = split_padding(orig)
            if not core or not is_translatable(core, parent):
                with self._lock:
                    self.stats["skipped"] += 1
                continue
            parts = _LINE_RE.split(core)      # 偶数位=待译文字段，奇数位=换行分隔符
            for i in range(0, len(parts), 2):
                part = parts[i]
                if not part or not is_translatable(part, parent):
                    continue
                if part in self.cache:
                    with self._lock:
                        self.stats["cached"] += 1
                elif part not in self._memo:
                    need_net.add(part)
            entries.append((nd, field, orig, pre, parts, post))

        if need_net:
            self._resolve(list(need_net))

        changed = 0
        for nd, field, orig, pre, parts, post in entries:
            # 第一遍：逐行翻译，分隔符原样保留
            translated = []
            for i, part in enumerate(parts):
                if i % 2 == 1 or not part:
                    translated.append(part)
                    continue
                res = self._memo.get(part)
                if res is None:
                    res = self.cache.get(part)   # 缓存命中但本次未发请求
                translated.append(res if res is not None else part)
            new_core = "".join(translated)

            # 第二遍：整段已含中文时，把孤立的英文半角标点(, ; :)转成中文全角
            if _HAS_CJK.search(new_core):
                parts2 = []
                for i, part in enumerate(parts):
                    if i % 2 == 1 or not part:
                        parts2.append(part)
                        continue
                    res = self._memo.get(part)
                    if res is None:
                        res = self.cache.get(part)
                    if res is not None and _HAS_CJK.search(res):
                        parts2.append(res)
                    elif re.fullmatch(r"[ \t]*[,;:][ \t]*", part):
                        parts2.append(part.translate(_PUNCT_MAP))
                    else:
                        parts2.append(part)
                new_core = "".join(parts2)
            new_core = _FW_PUNCT_SPACE.sub(r"\1", new_core)  # 中文标点前不留空格

            new_text = pre + new_core + post
            if new_text != orig:
                nd[field] = new_text
                changed += 1
        return changed


# ---------------- 文件级处理 ----------------
def translate_html_file(path: Path, tr: Translator):
    """翻译单个 html 文件。返回 'changed' / 'same' / 'no-article' / 'error'。"""
    try:
        html = path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"[错误] 读取失败 {path}: {e}")
        return "error"

    span = find_article_span(html)
    if span is None:
        return "no-article"

    hstart, block = span
    try:
        data = json.loads(block)
    except json.JSONDecodeError as e:
        print(f"[警告] JSON 解析失败 {path}: {e}")
        return "error"

    try:
        nodes = []
        iter_text_nodes(data, nodes)
        tr.translate_nodes(nodes)

        new_block = json.dumps(data, ensure_ascii=False, indent=2)
        if new_block == block:               # 没有实质变化 -> 不写盘
            return "same"

        new_html = html[:hstart] + new_block + html[hstart + len(block):]
        path.write_text(new_html, encoding="utf-8")
        return "changed"
    except Exception as e:                   # 序列化/写盘等意外错误不中断整批
        print(f"[错误] 处理失败 {path}: {type(e).__name__}: {e}")
        return "error"


def count_pending_html_file(path: Path) -> int:
    """离线统计一个文件里还剩多少值得翻译的英文段落（不联网、不写盘）。"""
    try:
        html = path.read_text(encoding="utf-8")
    except Exception:
        return -1
    span = find_article_span(html)
    if span is None:
        return -1
    try:
        data = json.loads(span[1])
    except json.JSONDecodeError:
        return -1
    nodes = []
    iter_text_nodes(data, nodes)
    pending = 0
    for nd, field, parent in nodes:
        _, core, _ = split_padding(nd.get(field) or "")
        if is_translatable(core, parent):
            pending += 1
    return pending


def collect_html_files(root: Path, only: str):
    # 统一用正斜杠比较，保证 Windows 下 --only "doc/current/gacui" 也能命中
    files = [p for p in root.rglob("*.html") if only in p.as_posix()]
    return sorted(files)


def page_url(root: Path, path: Path, url_base: str) -> str:
    """把被修改的 html 映射为可在浏览器打开/预览的站点 URL。"""
    rel = path.relative_to(root).as_posix()   # 相对仓库根，如 doc/current/gacui/events.html
    return url_base.rstrip("/") + "/" + rel


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="HTML article JSON 批量英译中")
    p.add_argument("--root", default=str(Path(__file__).resolve().parent.parent),
                   help="遍历根目录（默认=仓库根目录）")
    p.add_argument("--only", default="", help="只处理路径包含该子串的 html")
    p.add_argument("--workers", type=int, default=4, help="模型请求并发数（默认 4）")
    p.add_argument("--file-workers", type=int, default=1,
                   help="并行处理文件数（默认 1=逐文件串行、即时写盘；提速可调 4-8）")
    p.add_argument("--batch", type=int, default=1,
                   help="每批合并的短片段数（>1 启用两遍式批量以提速，写盘延后到末尾；默认 1=关闭）")
    p.add_argument("--save-after", type=int, default=1,
                   help="新增缓存条目达到该数量即写盘（默认 1=有新增就落盘，落盘及时）")
    p.add_argument("--retries", type=int, default=3, help="失败重试次数（默认 3）")
    p.add_argument("--cache", default=None,
                   help="翻译缓存文件路径（默认 <根目录>/translation_cache.json）")
    p.add_argument("--api", default=DEFAULT_API_URL, help="OpenAI 兼容接口地址")
    p.add_argument("--model", default=DEFAULT_MODEL, help="模型名")
    p.add_argument("--temperature", type=float, default=TEMPERATURE)
    p.add_argument("--url-base",
                   default="http://127.0.0.1:8357/vczh-libraries-doc-zh",
                   help="站点根 URL（用于在浏览器打开修改后的页面）")
    p.add_argument("--open", action="store_true",
                   help="每修改一个文件就在默认浏览器打开其页面 URL")
    p.add_argument("--check", action="store_true",
                   help="离线检查：统计剩余待翻译段落，不联网、不写文件")
    p.add_argument("--quiet", action="store_true", help="关闭逐条翻译日志")
    return p.parse_args(argv)


def main(argv=None):
    # 控制台编码（如 GBK）打不出个别字符（emoji 等）会导致 print 抛
    # UnicodeEncodeError 而整批退出(exit 1)；这里改为“替换”，保证永不因打印崩溃。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(errors="replace")
        except Exception:
            pass
    args = parse_args(argv)
    root = Path(args.root)
    if not root.is_dir():
        print(f"[错误] 目录不存在: {root}")
        return 2

    cache_path = Path(args.cache) if args.cache else root / "translation_cache.json"
    files = collect_html_files(root, args.only)
    if not files:
        print(f"[提示] {root} 下没有匹配 --only {args.only!r} 的 html 文件")
        return 0

    # ---------- 离线检查模式 ----------
    if args.check:
        total = 0
        dirty = 0
        for path in files:
            n = count_pending_html_file(path)
            if n > 0:
                total += n
                dirty += 1
                print(f"  [待译 {n:>3}] {path}")
        print(f"\n检查完成：共 {len(files)} 个 html，"
              f"其中 {dirty} 个仍需处理，剩余待翻译段落 {total} 处。")
        return 0

    # ---------- 正常翻译模式 ----------
    tr = Translator(args.api, args.model, cache_path,
                    workers=args.workers, retries=args.retries,
                    temperature=args.temperature, quiet=args.quiet)
    print(f"缓存: {cache_path}")
    print(f"共 {len(files)} 个 html，开始翻译……")

    # ---------- 批量两遍式：第 1 遍全站收集去重并分批请求 ----------
    if args.batch > 1:
        # 可选的两遍式批量：仅当用户显式 --batch >1 时启用
        print("批量模式：第 1 遍收集待译原文……")
        need = set()
        for path in files:
            data = load_article_data(path)
            if data is not None:
                tr.collect_file_parts(data, need)
        if need:
            print(f"待译唯一片段 {len(need)} 条，分批请求中（每批约 {args.batch} 条）……")
            try:
                tr.translate_batches(need, args.batch)
            except KeyboardInterrupt:
                tr.save_cache()
                print("\n[中断] 已保存批量进度缓存，可重跑续传（文件未写盘）。")
                return 1
        else:
            print("无需联网翻译（全部命中缓存）。")
        tr.save_cache()
        print("第 2 遍：应用译文并写盘……")

    counts = {"changed": 0, "same": 0, "no-article": 0, "error": 0}
    counts_lock = threading.Lock()
    file_workers = max(1, args.file_workers)

    def handle(path):
        """处理单个文件（在文件级工作线程中执行）。"""
        try:
            state = translate_html_file(path, tr)
        except Exception as e:           # 兜底：单文件异常不中断整批
            print(f"[错误] 意外异常 {path}: {type(e).__name__}: {e}")
            state = "error"
        with counts_lock:
            counts[state] += 1
        if state == "changed":
            url = page_url(root, path, args.url_base)
            if not args.quiet:
                print(f"  [已更新] {path}\n            {url}")
            if args.open:
                webbrowser.open(url)

    def flush_if_needed():
        # 缓存攒批落盘（仅主线程调用，避免并发写文件）
        if tr.dirty() >= args.save_after:
            tr.save_cache()

    try:
        if file_workers <= 1 or len(files) <= 1:
            for path in files:
                handle(path)
                flush_if_needed()
        else:
            with ThreadPoolExecutor(max_workers=file_workers) as ex:
                futures = [ex.submit(handle, p) for p in files]
                try:
                    for fut in as_completed(futures):
                        fut.result()
                        flush_if_needed()
                except KeyboardInterrupt:
                    for fut in futures:      # 停止排队中的任务
                        fut.cancel()
                    raise
    except KeyboardInterrupt:
        print("\n[中断] 已保存缓存，可重新运行以断点续传。")
    finally:
        tr.save_cache()
        tr._executor.shutdown(wait=False, cancel_futures=True)

    s = tr.stats
    print("\n完成汇总：")
    print(f"  文件: 修改 {counts['changed']} / 无变化 {counts['same']} / "
          f"无article {counts['no-article']} / 出错 {counts['error']}")
    print(f"  文本: 请求模型 {s['requested']} / 命中缓存 {s['cached']} / "
          f"跳过 {s['skipped']} / 失败 {s['failed']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
