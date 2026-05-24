"""
YANG path index for SR Linux 24.10.1.

Parses .yang files and builds a searchable flat list of paths with their
type, config/state classification, and description. Results are cached at
first access so the MCP server only pays the parse cost once.
"""

import re
from functools import lru_cache
from pathlib import Path

YANG_ROOT = Path("/Users/dominikkrebs/IP6/YANG_MODELS/srlinux-yang-models/all/v24.10.1/srl_nokia/models")

_BODY_KEYWORDS = {
    "container", "list", "leaf", "leaf-list", "grouping", "augment",
    "choice", "case", "input", "output", "rpc", "notification", "action",
    "anydata", "anyxml", "uses", "typedef", "identity", "extension",
    "feature", "deviation", "module", "submodule",
}


# ---------------------------------------------------------------------------
# Tokeniser
# ---------------------------------------------------------------------------

def _strip_comments(text: str) -> str:
    """Strip // and /* */ comments, skipping content inside quoted strings."""
    result: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    while i < n:
        if not in_string:
            if text[i : i + 2] == "/*":
                end = text.find("*/", i + 2)
                i = (end + 2) if end != -1 else n
                result.append(" ")
                continue
            if text[i : i + 2] == "//":
                end = text.find("\n", i + 2)
                i = end if end != -1 else n
                continue
            if text[i] == '"':
                in_string = True
        else:
            if text[i] == "\\" and i + 1 < n:
                result.append(text[i])
                i += 1
            elif text[i] == '"':
                in_string = False
        result.append(text[i])
        i += 1
    return "".join(result)


def _tokenise(text: str) -> list[str]:
    text = _strip_comments(text)
    return re.findall(r'"(?:[^"\\]|\\.)*"|\{|\}|;|[^\s{};]+', text)


# ---------------------------------------------------------------------------
# Tree
# ---------------------------------------------------------------------------

class _Node:
    __slots__ = ("keyword", "name", "children")

    def __init__(self, keyword: str, name: str | None = None):
        self.keyword = keyword
        self.name = name
        self.children: list["_Node"] = []


def _build_tree(tokens: list[str]) -> _Node:
    root = _Node("__root__")
    stack: list[_Node] = [root]
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok == "}":
            if len(stack) > 1:
                stack.pop()
            i += 1
            continue
        if tok in ("{", ";"):
            i += 1
            continue

        keyword = tok
        i += 1
        name: str | None = None
        if i < n and tokens[i] not in ("{", "}", ";"):
            name = tokens[i].strip('"')
            i += 1

        node = _Node(keyword, name)
        stack[-1].children.append(node)

        if i < n and tokens[i] == "{":
            stack.append(node)
            i += 1  # consume {
        elif i < n and tokens[i] == ";":
            i += 1  # consume ;

    return root


# ---------------------------------------------------------------------------
# Path extraction
# ---------------------------------------------------------------------------

def _strip_prefix(s: str) -> str:
    return s.split(":")[-1]


def _get_child_value(node: _Node, keyword: str) -> str | None:
    for c in node.children:
        if c.keyword == keyword:
            return c.name
    return None


def _config_flag(node: _Node, inherited: bool) -> bool:
    val = _get_child_value(node, "config")
    if val == "false":
        return False
    if val == "true":
        return True
    return inherited


def _extract(node: _Node, path: list[str], config: bool, groupings: dict, results: list) -> None:
    for child in node.children:
        kw = child.keyword
        name = child.name

        # Recurse into module/submodule body without adding to path
        if kw in ("module", "submodule"):
            _extract(child, path, config, groupings, results)
            continue

        if kw == "grouping":
            if name:
                groupings[name] = child
            continue

        if kw == "augment":
            if name:
                aug_segs = [_strip_prefix(s) for s in name.strip("/").split("/") if s]
                _extract(child, aug_segs, config, groupings, results)
            continue

        if kw == "uses":
            if name and name in groupings:
                _extract(groupings[name], path, config, groupings, results)
            continue

        if kw in ("container", "choice", "case", "input", "output", "rpc", "notification", "action", "anydata", "anyxml"):
            new_path = path + [name] if name else path
            new_config = _config_flag(child, config)
            _extract(child, new_path, new_config, groupings, results)
            continue

        if kw == "list":
            if name:
                key_raw = _get_child_value(child, "key") or ""
                key_leaf = key_raw.split()[0] if key_raw else "*"
                seg = f"{name}[{key_leaf}=*]"
                new_path = path + [seg]
            else:
                new_path = path
            new_config = _config_flag(child, config)
            _extract(child, new_path, new_config, groupings, results)
            continue

        if kw in ("leaf", "leaf-list"):
            if name:
                leaf_config = _config_flag(child, config)
                type_node = next((c for c in child.children if c.keyword == "type"), None)
                type_str = type_node.name if type_node else "unknown"
                desc_node = next((c for c in child.children if c.keyword == "description"), None)
                desc = (desc_node.name or "")[:120] if desc_node else ""
                results.append({
                    "path": "/" + "/".join(path + [name]),
                    "type": type_str,
                    "config": leaf_config,
                    "description": desc,
                })
            continue


# ---------------------------------------------------------------------------
# Index builder
# ---------------------------------------------------------------------------

def _parse_file(yang_file: Path) -> list[dict]:
    try:
        text = yang_file.read_text(errors="replace")
    except OSError:
        return []
    tokens = _tokenise(text)
    tree = _build_tree(tokens)
    groupings: dict = {}
    results: list = []
    _extract(tree, [], True, groupings, results)
    domain = yang_file.parent.name
    for r in results:
        r["file"] = yang_file.name
        r["domain"] = domain
    return results


@lru_cache(maxsize=1)
def get_index() -> list[dict]:
    """Build and return the full path index (cached after first call)."""
    index: list[dict] = []
    for yang_file in sorted(YANG_ROOT.rglob("*.yang")):
        index.extend(_parse_file(yang_file))
    return index


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def search(keyword: str, domain: str | None = None, max_results: int = 40) -> list[dict]:
    """
    Search YANG paths by keyword (case-insensitive substring match on path +
    description). Optionally filter by domain folder name.
    """
    kw = keyword.lower()
    results = []
    for entry in get_index():
        if domain and domain.lower() not in entry["domain"].lower():
            continue
        if kw in entry["path"].lower() or kw in entry["description"].lower():
            results.append(entry)
        if len(results) >= max_results:
            break
    return results


def format_results(entries: list[dict]) -> str:
    if not entries:
        return "No YANG paths found for that keyword."
    lines = []
    for e in entries:
        kind = "config" if e["config"] else "state"
        desc = f"  # {e['description']}" if e['description'] else ""
        lines.append(f"{e['path']}  [{e['type']} | {kind}]{desc}")
    return "\n".join(lines)
