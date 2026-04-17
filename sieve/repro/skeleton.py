"""AST-based skeleton extraction: signatures and top-level definitions, no bodies."""
from __future__ import annotations

import ast


def _first_docline(docstring: str | None) -> str:
    if not docstring:
        return ""
    first = docstring.strip().splitlines()[0].strip()
    return first[:120]


def _sig(func: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args = []
    a = func.args
    for arg in a.args:
        args.append(arg.arg)
    if a.vararg:
        args.append("*" + a.vararg.arg)
    for arg in a.kwonlyargs:
        args.append(arg.arg)
    if a.kwarg:
        args.append("**" + a.kwarg.arg)
    prefix = "async def" if isinstance(func, ast.AsyncFunctionDef) else "def"
    return f"{prefix} {func.name}({', '.join(args)})"


def build_file_skeleton(source: str) -> str:
    """Return a skeleton of the file: module docstring + signatures of top-level
    functions, classes (with method signatures), and imports.

    No function or method bodies.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return "(unparsable)"
    lines: list[str] = []

    mod_doc = _first_docline(ast.get_docstring(tree))
    if mod_doc:
        lines.append(f'"""{mod_doc}"""')
        lines.append("")

    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            lines.append(ast.unparse(node))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            d = _first_docline(ast.get_docstring(node))
            lines.append(f"{_sig(node)}:")
            if d:
                lines.append(f'    """{d}"""')
            lines.append("    ...")
            lines.append("")
        elif isinstance(node, ast.ClassDef):
            lines.append(f"class {node.name}:")
            d = _first_docline(ast.get_docstring(node))
            if d:
                lines.append(f'    """{d}"""')
            has_member = False
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    lines.append(f"    {_sig(sub)}: ...")
                    has_member = True
                elif isinstance(sub, ast.Assign):
                    # class-level attribute (first target only, no value)
                    for t in sub.targets:
                        if isinstance(t, ast.Name):
                            lines.append(f"    {t.id} = ...")
                            has_member = True
                            break
            if not has_member:
                lines.append("    ...")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def list_symbols(source: str) -> list[str]:
    """Return a flat list of top-level symbols defined in the file
    (as `name` for functions/classes, `Class.method` for methods)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    out: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(node.name)
        elif isinstance(node, ast.ClassDef):
            out.append(node.name)
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.append(f"{node.name}.{sub.name}")
    return out
