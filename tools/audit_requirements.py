"""
Audit of requirements.txt against what the code really imports.

Run it INSIDE the project's virtual environment (on the Raspberry Pi for the real picture: the import -> package
mapping comes from the packages installed there):

    python tools/audit_requirements.py [project_root]

It prints (1) the third-party modules imported by the app / the tests / ai/, (2) which requirements.txt entries are
imported directly, which are only dependencies of something that is imported (transitive: pip installs them by
itself), which are not needed by anything, and (3) modules imported but missing from requirements.txt.
Modules that are not installed in the current environment are reported as "NOT INSTALLED" (install them first, or
the mapping to a package name is unknown). System packages installed with apt (libcamera, picamera2, python3-prctl)
are not pip packages.
"""
import ast
import re
import sys
from collections import defaultdict
from importlib import metadata
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".venv", "__pycache__", ".claude", "node_modules", "input_images"}

def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()

# --- 1. imports used by the project, per area
areas = defaultdict(lambda: defaultdict(set))   # area -> top-level module -> files
std = set(sys.stdlib_module_names)
local = {p.stem for p in ROOT.glob("*.py")} | {p.name for p in ROOT.iterdir() if p.is_dir()}
for path in ROOT.rglob("*.py"):
    parts = path.relative_to(ROOT).parts
    if any(part in SKIP_DIRS for part in parts):
        continue
    area = "tests" if "tests" in parts else ("ai" if parts[0] == "ai" else "app")
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        continue
    for node in ast.walk(tree):
        mods = []
        if isinstance(node, ast.Import):
            mods = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods = [node.module.split(".")[0]]
        for m in mods:
            if m not in std and m not in local:
                areas[area][m].add(str(Path(*parts)))

pkg_dists = metadata.packages_distributions()
def dists_for(module):
    return sorted({norm(d) for d in pkg_dists.get(module, [])})

print("=== third-party modules imported, by area ===")
for area in ("app", "tests", "ai"):
    print(f"\n[{area}]")
    for mod in sorted(areas[area]):
        d = dists_for(mod)
        print(f"  {mod:24s} -> {', '.join(d) if d else 'NOT INSTALLED in this venv'}   ({len(areas[area][mod])} file(s))")

# --- 2. requirements.txt
reqs = []
for line in (ROOT / "requirements.txt").read_text().splitlines():
    line = line.split("#")[0].strip()
    if line:
        reqs.append(norm(re.split(r"[<>=!~\[; ]", line)[0]))
req_set = set(reqs)
print(f"\n=== requirements.txt: {len(reqs)} entries ===")

used = {d for area in ("app",) for m in areas[area] for d in dists_for(m)}
used_any = {d for area in areas for m in areas[area] for d in dists_for(m)}

# --- 3. which requirement is a dependency of another one (transitive / redundant)
required_by = defaultdict(set)
for d in req_set:
    try:
        for r in metadata.requires(d) or []:
            if "extra ==" in r:
                continue
            n = norm(re.split(r"[<>=!~\[; ]", r)[0])
            required_by[n].add(d)
    except metadata.PackageNotFoundError:
        pass

def closure(roots):
    seen, stack = set(), list(roots)
    while stack:
        d = stack.pop()
        if d in seen:
            continue
        seen.add(d)
        try:
            for r in metadata.requires(d) or []:
                if "extra ==" in r:
                    continue
                stack.append(norm(re.split(r"[<>=!~\[; ]", r)[0]))
        except metadata.PackageNotFoundError:
            pass
    return seen

direct_app = used & req_set
need_for_app = closure(direct_app)
print("\n=== requirements entries by role ===")
print("imported by the app (direct):", sorted(direct_app))
print("imported only by tests:", sorted((used_any - used) & req_set - {d for d in used}))
ai_only = {d for m in areas["ai"] for d in dists_for(m)} - used
print("imported only by ai/:", sorted(ai_only & req_set))
pulled = sorted(d for d in req_set if d not in used_any and d in need_for_app)
print("not imported, but a dependency of something the app imports (transitive):", pulled)
unneeded = sorted(d for d in req_set if d not in used_any and d not in need_for_app)
print("not imported and not needed by anything the app imports:", unneeded)

print("\n=== imported by the app but NOT in requirements.txt ===")
for m in sorted(areas["app"]):
    ds = dists_for(m)
    if not ds or not (set(ds) & req_set):
        print(f"  {m} -> {ds or 'not installed in this venv'}")
