# -*- coding: utf-8 -*-
"""把站点根路径从域名根迁移到子路径根 SITE_BASE。
用法: python _t/fix.py --html|--js [--write]
"""
import re, os, sys

SITE = "/vczh-libraries-doc-zh"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WRITE = "--write" in sys.argv

PREFIX = SITE + "/"

# ============================ HTML ============================
TAG_RE = re.compile(r"<[^<>]+>")
# 标签内属性: attr=/x  (注意不匹配 "/" 后紧跟 "/" 的协议相对,也不匹配 "/" 后紧跟引号的空值)
ATTR_IN = re.compile(r'(\b[\w-]+=)(["\'])/(?!/)')
# 页面级 const hrefPrefix = "/doc/current"
CONST_RE = re.compile(r'(hrefPrefix\s*=\s*)(["\'])/(?!/)')

def fix_html(text):
    # 1) 只处理 <head> 内静态资源属性（避免误改正文代码样例）
    def tagfix(seg):
        return ATTR_IN.sub(lambda m: m.group(1) + m.group(2) + PREFIX, seg)
    headm = re.search(r'(<head[^>]*>)(.*?)(</head>)', text, re.S | re.I)
    if headm:
        head_in = tagfix(headm.group(2))
        text = text[:headm.start(2)] + head_in + text[headm.end(2):]
    # 2) hrefPrefix 常量值烘焙 SITE_BASE（其值本身是版本根，如 /doc/current）
    text = CONST_RE.sub(lambda m: m.group(1) + m.group(2) + PREFIX, text)
    return text

# ============================ JS ============================
def js_attr(txt):
    return ATTR_IN.sub(lambda m: m.group(1) + m.group(2) + PREFIX, txt)

def js_body(txt):
    # 新式 R(): s,t / n,t
    txt = txt.replace('s.startsWith("//")?s.substr(1):',
                      's.startsWith("//")?"' + SITE + '"+s.substr(1):')
    txt = txt.replace('n.startsWith("//")?n.substr(1):',
                      'n.startsWith("//")?"' + SITE + '"+n.substr(1):')
    txt = txt.replace('t.startsWith("//")?t.substr(1):',
                      't.startsWith("//")?"' + SITE + '"+t.substr(1):')
    txt = txt.replace('(t.hrefPrefix===void 0?"":t.hrefPrefix)+s:s',
                      '(t.hrefPrefix===void 0?"' + SITE + '":t.hrefPrefix)+s:s')
    txt = txt.replace('(t.hrefPrefix===void 0?"":t.hrefPrefix)+n:n',
                      '(t.hrefPrefix===void 0?"' + SITE + '":t.hrefPrefix)+n:n')
    # 旧式 B(): t,e —— 前缀恒由页面传入(已含 SITE),仅 "//" 转义需要 SITE
    txt = txt.replace('function B(t,e){return t.startsWith("//")?t.substr(1):t.startsWith("/")?(void 0===e.hrefPrefix?"":e.hrefPrefix)+t:t}',
                      'function B(t,e){return t.startsWith("//")?"' + SITE + '"+t.substr(1):t.startsWith("/")?(void 0===e.hrefPrefix?"":e.hrefPrefix)+t:t}')
    # 视图模板中写死的根字面量
    txt = txt.replace('href="/${o}.html"', 'href="' + PREFIX + '${o}.html"')
    txt = txt.replace('href="/${h}.html"', 'href="' + PREFIX + '${h}.html"')
    txt = txt.replace('href="/home/${i.href}"', 'href="' + PREFIX + 'home/${i.href}"')
    txt = txt.replace('<a href="/">', '<a href="' + PREFIX + '">')
    txt = txt.replace('src="${i.image}"/>', 'src="' + SITE + '${i.image}"/>')
    return txt

JS_ATTR = [
    r"scripts\rootView.js",
    r"scripts\homeView.js",
    r"scripts\homeCategoryFeatureView.js",
    r"doc\current\scripts\rootView.js",
    r"doc\ver1\scripts\rootView.js",
]
JS_BODY = [
    r"scripts\articleView.js",
    r"scripts\homeCategoryArticleView.js",
    r"scripts\homeCategoryFeatureView.js",
    r"scripts\homeView.js",
    r"doc\current\scripts\articleView.js",
    r"doc\ver1\scripts\articleView.js",
    r"doc\current\scripts\documentView.js",
    r"doc\ver1\scripts\documentView.js",
]

BAD = {".git", ".venv", "_t", "_translate"}

def run_html():
    n = 0
    for dp, dn, fns in os.walk(ROOT):
        if BAD & set(dp.split(os.sep)):
            continue
        for fn in fns:
            if not fn.lower().endswith(".html"):
                continue
            p = os.path.join(dp, fn)
            txt = open(p, encoding="utf-8").read()
            if SITE in txt:
                continue
            new = fix_html(txt)
            if new != txt:
                n += 1
                if WRITE:
                    open(p, "w", encoding="utf-8", newline="\r\n").write(new)
    print("HTML changed:", n)

def run_js():
    for rel in JS_ATTR:
        p = os.path.join(ROOT, rel)
        txt = open(p, encoding="utf-8").read()
        new = js_attr(txt)
        new = js_body(new)
        if WRITE:
            open(p, "w", encoding="utf-8", newline="\r\n").write(new)
            print("JS written:", rel)
    for rel in JS_BODY:
        p = os.path.join(ROOT, rel)
        txt = open(p, encoding="utf-8").read()
        new = js_body(txt)
        if WRITE:
            open(p, "w", encoding="utf-8", newline="\r\n").write(new)
            print("JS written:", rel)

if __name__ == "__main__":
    if "--html" in sys.argv:
        run_html()
    if "--js" in sys.argv:
        run_js()
