"""Build the site pages from web/ sources: python3 scripts/build_pages.py"""
import os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
W = lambda f: open(os.path.join(ROOT, "web", f)).read()
css, eng = W("style.css"), W("engine.js")
def out(path, s):
    p = os.path.join(ROOT, "docs", path); os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "w").write(s); print("wrote", path, len(s))
dyn = W("dynasty.html").replace("/*BASECSS*/", css).replace("/*ENGINE*/", eng)
out("index.html", dyn)
out("ring/index.html", dyn)
out("redraft/index.html", W("redraft.html").replace("/*BASECSS*/", css))
out("hub/index.html", W("hub.html").replace("/*BASECSS*/", css))
