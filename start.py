# 本地模拟 GitHub Pages 子路径部署：/vczh-libraries-doc-zh/* -> 仓库根 *
import http.server, socketserver, os, sys, urllib.parse

# __file__ 就是 start.py，网站根目录就是 start.py 所在文件夹！不要双重 dirname
ROOT = os.path.dirname(os.path.abspath(__file__))
SITE = '/vczh-libraries-doc-zh'
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8357


class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def translate_path(self, path):
        path = urllib.parse.urlparse(path).path
        # 把子路径前缀剥离
        if path.startswith(SITE):
            path = path[len(SITE):]
        if path == "" or path == "/":
            path = "/index.html"
        return super().translate_path(path)

    def log_message(self, *a):
        # 全部转为字符串，兼容 Python3.14 HTTPStatus 枚举对象
        parts = [str(x) for x in a]
        sys.stderr.write(" ".join(parts) + "\n")


if __name__ == "__main__":
    with socketserver.ThreadingTCPServer(("127.0.0.1", PORT), H) as httpd:
        print(f"serving http://127.0.0.1:{PORT}{SITE}/", flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nserver exit")
