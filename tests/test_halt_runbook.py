"""Offline net for docs/HALT_RUNBOOK.md (Brief halt-runbook, D19). Never the
network, never a key, never a live file: it parses source with ast and reads
one markdown file. Nothing is imported from the engine.

A HALT class is the token after "HALT:" up to the first space, in any string
literal of the engine sources: forgetrader/run.py, forgetrader/submit.py,
forgetrader_state.py, wheel_dryrun.py, forgetrader/cc_scan.py and every repo
module they import (transitively). Docstrings are skipped (they describe,
they do not write runs.outcome). When "HALT:" is followed directly by an
f-string field the field is resolved from the code:
  * a name assigned a string literal in the enclosing function (run.py's
    reason = "clock ..." / f"snapshot {e}" / f"watchlist_structure {e}")
    -> the leading token of each such literal;
  * {stage} -> every value of forgetrader_state.STAGES (read by ast, an
    over-approximation: those six classes already exist as literals);
  * anything else is UNRESOLVED and fails the run.

  C  coverage: every class has exactly one "## HALT:<class>" heading and
     no heading names a class absent from the code; nothing unresolved.
  B  bites, each on a temp copy (mkdtemp) of the scanned sources / doc:
     B1 a planted literal fake class is extracted and reported missing;
     B2 a planted unresolvable f-string field is reported unresolved;
     B3 a heading for a class absent from the code is reported orphan;
     B4 a duplicated heading is reported as a duplicate.
"""
import ast
import os
import re
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC = os.path.join("docs", "HALT_RUNBOOK.md")
ENTRY = ("forgetrader/run.py", "forgetrader/submit.py", "forgetrader_state.py",
         "wheel_dryrun.py", "forgetrader/cc_scan.py")
HEADING_RE = re.compile(r"^## HALT:(.*)$")

failures = []
n = 0
scenarios = 0


def check(cond, msg):
    global n
    n += 1
    if not cond:
        failures.append(msg)


def scenario(fn):
    global scenarios
    scenarios += 1
    try:
        fn()
    except Exception as e:  # a crash is reported, never a silent pass
        check(False, f"{fn.__name__} crashed: {type(e).__name__}: {e}")


# --------------------------------------------------------------------------- #
# Source closure + extraction                                                 #
# --------------------------------------------------------------------------- #

def _module_file(root, dotted):
    """Repo-relative path of a dotted module name, or None (stdlib / SDK)."""
    rel = dotted.replace(".", "/")
    for cand in (rel + ".py", rel + "/__init__.py"):
        if os.path.isfile(os.path.join(root, cand)):
            return cand
    return None


def source_closure(root):
    """ENTRY plus every repo module they import, transitively. Sorted."""
    seen = set()
    todo = list(ENTRY)
    while todo:
        rel = todo.pop()
        if rel in seen:
            continue
        seen.add(rel)
        with open(os.path.join(root, rel)) as f:
            tree = ast.parse(f.read(), rel)
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            for dotted in names:
                hit = _module_file(root, dotted)
                if hit and not hit.endswith("__init__.py"):
                    todo.append(hit)
    return sorted(seen)


def _stages(root):
    """forgetrader_state.STAGES as strings, resolved by ast."""
    with open(os.path.join(root, "forgetrader_state.py")) as f:
        tree = ast.parse(f.read())
    consts, stages = {}, None
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            name, val = node.targets[0].id, node.value
            if isinstance(val, ast.Constant) and isinstance(val.value, str):
                consts[name] = val.value
            if name == "STAGES" and isinstance(val, ast.List):
                stages = val
    return [consts[e.id] for e in stages.elts]


def _lead_token(node):
    """Leading space-delimited token of a str Constant / JoinedStr whose first
    piece is literal; None otherwise."""
    if isinstance(node, ast.JoinedStr) and node.values:
        node = node.values[0]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        tok = node.value.split(" ")[0]
        return tok or None
    return None


def _assignments(func):
    """name -> [value nodes] for single-Name assignments inside func."""
    out = {}
    for node in ast.walk(func):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            out.setdefault(node.targets[0].id, []).append(node.value)
    return out


def extract(root):
    """(classes {class: [file:line]}, unresolved [file:line text], files)."""
    files = source_closure(root)
    stages = _stages(root)
    classes, unresolved = {}, []

    def add(cls, where):
        classes.setdefault(cls, []).append(where)

    for rel in files:
        with open(os.path.join(root, rel)) as f:
            tree = ast.parse(f.read(), rel)
        skip = set()
        func_of = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
                b = node.body
                if (b and isinstance(b[0], ast.Expr)
                        and isinstance(b[0].value, ast.Constant)
                        and isinstance(b[0].value.value, str)):
                    skip.add(id(b[0].value))
            if isinstance(node, ast.JoinedStr):
                for v in node.values:
                    skip.add(id(v))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for sub in ast.walk(node):
                    func_of.setdefault(id(sub), node)  # outermost def wins
        for node in ast.walk(tree):
            if id(node) in skip:
                continue
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                pieces = [node]
            elif isinstance(node, ast.JoinedStr):
                pieces = node.values
            else:
                continue
            where = f"{rel}:{node.lineno}"
            for i, piece in enumerate(pieces):
                if not (isinstance(piece, ast.Constant)
                        and isinstance(piece.value, str)):
                    continue
                for m in re.finditer(r"HALT:", piece.value):
                    rest = piece.value[m.end():]
                    tok = rest.split(" ")[0] if rest else ""
                    nxt = pieces[i + 1] if i + 1 < len(pieces) else None
                    tail_dynamic = (" " not in rest
                                    and isinstance(nxt, ast.FormattedValue))
                    if tok and not tail_dynamic:
                        add(tok, where)
                    elif not tok and not tail_dynamic:
                        continue  # bare "HALT:" (startswith / len), no class
                    elif tok:
                        unresolved.append(f"{where} HALT:{tok}{{...}}")
                    else:
                        val = nxt.value
                        name = val.id if isinstance(val, ast.Name) else None
                        func = func_of.get(id(node))
                        lits = []
                        if name and func is not None:
                            lits = [_lead_token(v) for v in
                                    _assignments(func).get(name, [])]
                        if lits and all(lits):
                            for t in lits:
                                add(t, where)
                        elif name == "stage":
                            for t in stages:
                                add(t, where)
                        else:
                            unresolved.append(
                                f"{where} HALT:{{{ast.unparse(val)}}}")
    return classes, unresolved, files


def problems(classes, unresolved, doc_text):
    """Every way the doc and the code disagree, as strings."""
    out = [f"unresolved {u}" for u in unresolved]
    heads = [m.group(1) for line in doc_text.splitlines()
             for m in [HEADING_RE.match(line)] if m]
    for cls in sorted(classes):
        k = heads.count(cls)
        if k == 0:
            out.append(f"missing section ## HALT:{cls}")
        elif k > 1:
            out.append(f"duplicate section ## HALT:{cls} x{k}")
    for h in sorted(set(heads)):
        if h not in classes:
            out.append(f"orphan section ## HALT:{h}")
    return out


def _read(path):
    with open(path) as f:
        return f.read()


def _temp_copy(files):
    tmp = tempfile.mkdtemp(prefix="halt_runbook_")
    for rel in files + [DOC]:
        dst = os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(os.path.join(ROOT, rel), dst)
    return tmp


# --------------------------------------------------------------------------- #
# Scenarios                                                                   #
# --------------------------------------------------------------------------- #

REAL = {}


def c_coverage():
    classes, unresolved, files = extract(ROOT)
    REAL.update(classes=classes, files=files)
    print(f"scanned {len(files)} files: {' '.join(files)}")
    for cls in sorted(classes):
        print(f"  HALT:{cls}  {' '.join(sorted(set(classes[cls])))}")
    for e in ENTRY:
        check(e in files, f"C entry file not scanned: {e}")
    check(len(classes) > 0, "C no HALT classes extracted")
    probs = problems(classes, unresolved, _read(os.path.join(ROOT, DOC)))
    for p in probs:
        print(f"  PROBLEM {p}")
    check(probs == [], f"C doc vs code: {probs}")


def _bite(name, mutate_src, mutate_doc, expect):
    files = REAL["files"]
    tmp = _temp_copy(files)
    try:
        target = os.path.join(tmp, "forgetrader_state.py")
        if mutate_src:
            with open(target, "a") as f:
                f.write(mutate_src)
        doc = os.path.join(tmp, DOC)
        if mutate_doc:
            with open(doc, "a") as f:
                f.write(mutate_doc)
        classes, unresolved, _ = extract(tmp)
        probs = problems(classes, unresolved, _read(doc))
        print(f"  {name} problems {probs}")
        check(expect in probs, f"{name} expected {expect!r}, got {probs}")
        check(len(probs) == 1, f"{name} expected exactly one problem, got {probs}")
    finally:
        shutil.rmtree(tmp)


def b1_planted_class():
    _bite("B1", '\n_PLANTED = "HALT:planted_fake_class detail"\n', None,
          "missing section ## HALT:planted_fake_class")


def b2_unresolved_field():
    _bite("B2", '\ndef _planted(mystery):\n    return f"HALT:{mystery} x"\n',
          None, "unresolved forgetrader_state.py:"
          f"{len(_read(os.path.join(ROOT, 'forgetrader_state.py')).splitlines()) + 3}"
          " HALT:{mystery}")


def b3_orphan_section():
    _bite("B3", None, "\n## HALT:ghost_class\n\nnot in the code\n",
          "orphan section ## HALT:ghost_class")


def b4_duplicate_section():
    cls = sorted(REAL["classes"])[0]
    _bite("B4", None, f"\n## HALT:{cls}\n\nsecond copy\n",
          f"duplicate section ## HALT:{cls} x2")


def main():
    scenario(c_coverage)
    if REAL:
        for fn in (b1_planted_class, b2_unresolved_field, b3_orphan_section,
                   b4_duplicate_section):
            scenario(fn)
    for f in failures:
        print(f"  FAILED {f}")
    if failures:
        print(f"FAIL: {len(failures)} failed of {n} assertions")
        return 1
    print(f"PASS: test_halt_runbook {scenarios} scenarios, {n} assertions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
