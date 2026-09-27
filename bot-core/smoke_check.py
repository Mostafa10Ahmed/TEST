from __future__ import annotations

import ast
import os
import sys


def collect_slash_commands(root: str) -> list[str]:
    names: list[str] = []
    for dirpath, _, files in os.walk(root):
        for filename in files:
            if not filename.endswith(".py"):
                continue
            path = os.path.join(dirpath, filename)
            with open(path, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=path)
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for dec in node.decorator_list:
                    if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
                        continue
                    if dec.func.attr != "command":
                        continue
                    is_app = isinstance(dec.func.value, ast.Name) and dec.func.value.id == "app_commands"
                    is_tree = isinstance(dec.func.value, ast.Attribute) and dec.func.value.attr == "tree"
                    if not (is_app or is_tree):
                        continue
                    explicit_name = None
                    for kw in dec.keywords:
                        if kw.arg == "name" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                            explicit_name = kw.value.value
                            break
                    names.append(explicit_name or node.name)
    return names


def main() -> int:
    root = os.path.dirname(__file__)
    names = collect_slash_commands(root)
    duplicates = sorted({n for n in names if names.count(n) > 1})
    print(f"slash_total={len(names)}")
    if duplicates:
        print("duplicate_slash_commands:")
        for name in duplicates:
            print(f"- {name}")
        return 1
    print("duplicates=none")
    print("smoke_check=ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
