#!/usr/bin/env python3
"""
AST Canonicalizer

Parses a Python file, normalizes its AST structure (sorting definitions,
optionally stripping docstrings), and saves the representation to a file.
This allows visual diffing of two canonicalized outputs in tools like VS Code.
"""

import argparse
import ast
import sys
from pathlib import Path
from typing import List, Union


class ASTCanonicalizer(ast.NodeTransformer):
    """Recursively normalizes AST by sorting definitions and optionally stripping docstrings."""

    def __init__(self, ignore_docstrings: bool = False, ignore_order: bool = True):
        super().__init__()
        self.ignore_docstrings = ignore_docstrings
        self.ignore_order = ignore_order

    def _remove_docstring(self, body: List[ast.stmt]) -> List[ast.stmt]:
        """Removes the leading docstring from a body of statements if present."""
        if not body:
            return body

        first = body[0]
        if isinstance(first, ast.Expr):
            val = first.value
            # Modern ast.Constant (Python 3.8+)
            if isinstance(val, ast.Constant) and isinstance(val.value, str):
                return body[1:]
            # Fallback for older python AST specifications (ast.Str)
            elif (
                hasattr(ast, "Str")
                and isinstance(val, ast.Str)
                and isinstance(val.s, str)
            ):  # type: ignore
                return body[1:]

        return body

    def _canonicalize_body(self, body: List[ast.stmt]) -> List[ast.stmt]:
        """Sorts consecutive class and function definitions alphabetically by name."""
        if not self.ignore_order:
            return body

        new_body: List[ast.stmt] = []
        group: List[Union[ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef]] = []

        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                group.append(node)
            else:
                if group:
                    group.sort(key=lambda x: x.name)
                    new_body.extend(group)
                    group = []
                new_body.append(node)
        if group:
            group.sort(key=lambda x: x.name)
            new_body.extend(group)

        return new_body

    def visit_Module(self, node: ast.Module) -> ast.Module:
        node = self.generic_visit(node)
        if self.ignore_docstrings:
            node.body = self._remove_docstring(node.body)
        node.body = self._canonicalize_body(node.body)
        return node

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.ClassDef:
        node = self.generic_visit(node)
        if self.ignore_docstrings:
            node.body = self._remove_docstring(node.body)
        node.body = self._canonicalize_body(node.body)
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
        node = self.generic_visit(node)
        if self.ignore_docstrings:
            node.body = self._remove_docstring(node.body)
        # Execution order inside functions is semantic and is preserved as-is
        return node

    def visit_AsyncFunctionDef(
        self, node: ast.AsyncFunctionDef
    ) -> ast.AsyncFunctionDef:
        node = self.generic_visit(node)
        if self.ignore_docstrings:
            node.body = self._remove_docstring(node.body)
        return node


def canonicalize(
    source_code: str,
    filename: str,
    ignore_docstrings: bool = False,
    ignore_order: bool = True,
    output_format: str = "ast",
) -> str:
    """Parses, normalizes, and renders the AST into the requested string format."""
    tree = ast.parse(source_code, filename=filename)
    transformer = ASTCanonicalizer(
        ignore_docstrings=ignore_docstrings, ignore_order=ignore_order
    )
    canonical_tree = transformer.visit(tree)

    # Re-fix missing locations to ensure validity after transformations
    ast.fix_missing_locations(canonical_tree)

    if output_format == "source":
        if hasattr(ast, "unparse"):
            return ast.unparse(canonical_tree)
        else:
            raise RuntimeError(
                "Source code formatting requires Python 3.9 or newer (ast.unparse)."
            )

    # Default to raw AST dump format
    try:
        return ast.dump(canonical_tree, indent=2, include_attributes=False)
    except TypeError:
        # Fallback for Python versions before 3.9 where 'indent' isn't supported
        return ast.dump(canonical_tree, include_attributes=False)


def color_text(text: str, color_code: str) -> str:
    """Wraps text in ANSI escape codes if outputting to an interactive terminal."""
    if sys.stdout.isatty():
        return f"\033[{color_code}m{text}\033[0m"
    return text


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Canonicalize a Python file's AST and save it for comparison."
    )
    parser.add_argument(
        "input_file",
        type=Path,
        help="Path to the Python file to canonicalize.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Path to the output file. Defaults to <input_file>.out",
    )
    parser.add_argument(
        "--format",
        choices=["ast", "source"],
        default="ast",
        help=(
            "Output format: 'ast' (raw abstract syntax tree representation) "
            "or 'source' (re-generated Python code, requires Python 3.9+)."
        ),
    )
    parser.add_argument(
        "--ignore-docstrings",
        action="store_true",
        help="Strip docstrings during canonicalization.",
    )
    parser.add_argument(
        "--keep-order",
        action="store_false",
        dest="ignore_order",
        help="Do not sort class or function definitions.",
    )

    args = parser.parse_args()

    # Determine default output path if not specified
    output_path = (
        args.output
        if args.output
        else args.input_file.with_suffix(args.input_file.suffix + ".out")
    )

    try:
        source_code = args.input_file.read_text(encoding="utf-8")
    except OSError as e:
        print(
            color_text(f"Error reading file {args.input_file}: {e}", "31"),
            file=sys.stderr,
        )
        sys.exit(1)
    except UnicodeDecodeError as e:
        print(
            color_text(f"Encoding read error in {args.input_file}: {e}", "31"),
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        canonical_output = canonicalize(
            source_code=source_code,
            filename=str(args.input_file),
            ignore_docstrings=args.ignore_docstrings,
            ignore_order=args.ignore_order,
            output_format=args.format,
        )
    except SyntaxError as e:
        print(
            color_text(f"Syntax error in {args.input_file}: {e}", "31"), file=sys.stderr
        )
        sys.exit(1)
    except Exception as e:
        print(
            color_text(f"An error occurred during canonicalization: {e}", "31"),
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        output_path.write_text(canonical_output, encoding="utf-8")
        print(color_text(f"Canonicalized output written to: {output_path}", "32"))
    except OSError as e:
        print(
            color_text(f"Error writing output to {output_path}: {e}", "31"),
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
