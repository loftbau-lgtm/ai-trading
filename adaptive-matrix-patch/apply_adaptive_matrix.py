"""Apply Adaptive Experiment Matrix to an existing QuantLab AI checkout.

Run from project root:
    python apply_adaptive_matrix.py

Creates backups before modifying files. Does not import or enable adaptive_live.py.
"""
from pathlib import Path
import shutil
import sys

ROOT=Path.cwd()
HERE=Path(__file__).resolve().parent

required=["adaptive.py","adaptive_portfolio.py","adaptive_runner.py","server.py","public/index.html"]
missing=[p for p in required if not (ROOT/p).exists()]
if missing:
    raise SystemExit("Run this script from QuantLab AI project root. Missing: "+", ".join(missing))

def backup(rel):
    p=ROOT/rel
    b=p.with_suffix(p.suffix+".before-matrix.bak")
    if not b.exists():
        shutil.copy2(p,b)

def write(rel, source):
    dst=ROOT/rel
    src=HERE/source
    dst.parent.mkdir(parents=True,exist_ok=True)
    if src.resolve() == dst.resolve():
        return
    shutil.copy2(src,dst)

for rel in ["adaptive_runner.py","server.py","public/index.html"]:
    backup(rel)

write("adaptive_matrix_config.json","adaptive_matrix_config.json")
write("adaptive_matrix.py","adaptive_matrix.py")
write("public/adaptive_matrix.js","public_adaptive_matrix.js")
write("tests/test_adaptive_matrix.py","test_adaptive_matrix.py")

# Patch adaptive_runner.py
p=ROOT/"adaptive_runner.py"
s=p.read_text(encoding="utf-8")
if "from adaptive_matrix import AdaptiveMatrix" not in s:
    s=s.replace("from adaptive_portfolio import PaperPortfolio",
                "from adaptive_portfolio import PaperPortfolio\nfrom adaptive_matrix import AdaptiveMatrix")
if "self.matrix = AdaptiveMatrix" not in s:
    s=s.replace("self.portfolio = PaperPortfolio(self.directory/'adaptive.sqlite3',self.config)",
                "self.portfolio = PaperPortfolio(self.directory/'adaptive.sqlite3',self.config)\n        self.matrix = AdaptiveMatrix(self.directory,self.config)")
needle="""self.portfolio.process(histories,ranking,ended,context_ok=fresh,
                               manual_kill=(self.directory/'adaptive.kill').exists())"""
replacement=needle+"""
        # Matrix reuses the exact same histories/ranking snapshot. No extra Binance requests.
        self.matrix.process(histories,ranking,ended,context_ok=fresh,
                            manual_kill=(self.directory/'adaptive.kill').exists())"""
if "self.matrix.process(histories,ranking" not in s:
    if needle not in s:
        raise SystemExit("adaptive_runner.py anchor not found; restore backup and patch manually.")
    s=s.replace(needle,replacement)
p.write_text(s,encoding="utf-8")

# Patch server.py read-only endpoints and static file allowlist.
p=ROOT/"server.py"
s=p.read_text(encoding="utf-8")
anchor="""if path=='/api/adaptive':
            self.send_json(adaptive.snapshot())"""
repl="""if path=='/api/adaptive':
            self.send_json(adaptive.snapshot())
        elif path=='/api/adaptive-matrix':
            self.send_json(adaptive.matrix.snapshot())
        elif path=='/api/adaptive-matrix/variants':
            self.send_json({'variants': adaptive.matrix.snapshot()['variants']})
        elif path.startswith('/api/adaptive-matrix/variant/'):
            variant_id=path.rsplit('/',1)[-1]
            data=adaptive.matrix.variant_snapshot(variant_id)
            self.send_json(data if data else {'error':'not found'},200 if data else 404)
        elif path=='/api/adaptive-matrix/frontier':
            self.send_json(adaptive.matrix.frontier_snapshot())
        elif path=='/api/adaptive-matrix/stress':
            self.send_json(adaptive.matrix.stress_snapshot())"""
if "/api/adaptive-matrix'" not in s:
    if anchor not in s:
        raise SystemExit("server.py adaptive endpoint anchor not found.")
    s=s.replace(anchor,repl)
if "'/adaptive_matrix.js'" not in s:
    s=s.replace("'/scanner.js','/adaptive.js','/style.css'",
                "'/scanner.js','/adaptive.js','/adaptive_matrix.js','/style.css'")
p.write_text(s,encoding="utf-8")

# Patch dashboard.
p=ROOT/"public/index.html"
s=p.read_text(encoding="utf-8")
if 'id="adaptive-matrix"' not in s:
    marker='<section id="view-paper" class="workspace-view" hidden>'
    if marker not in s:
        raise SystemExit("public/index.html paper view anchor not found.")
    s=s.replace(marker, marker+'<section id="adaptive-matrix" class="panel"><p class="muted">Adaptive Matrix: inicjalizacja…</p></section>')
if '<script src="/adaptive_matrix.js"></script>' not in s:
    s=s.replace('<script src="/adaptive.js"></script>',
                '<script src="/adaptive.js"></script><script src="/adaptive_matrix.js"></script>')
p.write_text(s,encoding="utf-8")

print("Adaptive Experiment Matrix applied.")
print("Backups: *.before-matrix.bak")
print("Run tests: python -m unittest discover -s tests -v")
print("PAPER ONLY. adaptive_live.py was not imported or enabled.")
