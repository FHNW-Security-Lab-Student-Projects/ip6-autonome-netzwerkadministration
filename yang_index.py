"""
YANG path index for SR Linux 24.10.1.

Parses .yang files and builds a searchable flat list of paths with their
type, config/state classification, and description. Results are cached at
first access so the MCP server only pays the parse cost once.
"""

import re
from pathlib import Path

YANG_ROOT = Path(__file__).resolve().parent / "YANG_MODELS/srlinux-yang-models/all/v24.10.1/srl_nokia/models"

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


_index_cache: list[dict] | None = None


def get_index() -> list[dict]:
    """Build and return the full path index (cached after first successful build)."""
    global _index_cache
    if _index_cache is not None:
        return _index_cache
    if not YANG_ROOT.exists():
        raise FileNotFoundError(
            f"YANG model directory not found: {YANG_ROOT}\n"
            "Check that the srlinux-yang-models repo is cloned at the expected path."
        )
    index: list[dict] = []
    for yang_file in sorted(YANG_ROOT.rglob("*.yang")):
        index.extend(_parse_file(yang_file))
    if not index:
        raise RuntimeError(f"YANG index built empty — no .yang files parsed from {YANG_ROOT}")
    _index_cache = index
    return _index_cache


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


def _container_of(leaf_path: str) -> str:
    """Return the parent container path for a leaf path."""
    return leaf_path.rsplit("/", 1)[0] or "/"


def format_results(
    entries: list[dict],
    max_leaves_per_container: int = 4,
) -> str:
    """
    Format search results grouped by parent container.

    Each container is printed once with its leaves indented underneath; if a
    container has more than `max_leaves_per_container` matching leaves, the
    extras are summarised as `... +N more`. This keeps navigational signal
    while trimming bulk for searches that match many sibling leaves under the
    same container (e.g. /network-instance/route-table/ipv4-unicast/route[*]).
    """
    if not entries:
        return "No YANG paths found for that keyword."

    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for e in entries:
        container = _container_of(e["path"])
        if container not in groups:
            groups[container] = []
            order.append(container)
        groups[container].append(e)

    lines: list[str] = []
    for container in order:
        leaves = groups[container]
        if len(leaves) == 1:
            e = leaves[0]
            kind = "config" if e["config"] else "state"
            desc = f"  # {e['description']}" if e["description"] else ""
            lines.append(f"{e['path']}  [{e['type']} | {kind}]{desc}")
            continue

        lines.append(f"{container}/")
        for e in leaves[:max_leaves_per_container]:
            leaf_name = e["path"].rsplit("/", 1)[-1]
            kind = "config" if e["config"] else "state"
            desc = f"  # {e['description']}" if e["description"] else ""
            lines.append(f"  {leaf_name}  [{e['type']} | {kind}]{desc}")
        extra = len(leaves) - max_leaves_per_container
        if extra > 0:
            lines.append(f"  ... +{extra} more leaves under this container")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Navigation: list immediate children of a YANG path
# ---------------------------------------------------------------------------

_KEY_BRACKET_RE = re.compile(r"\[[^\]]*\]")


def _strip_keys(path: str) -> str:
    """Drop every `[...]` key segment entirely.

    The YANG parser is inconsistent: paths reached through `augment` statements
    carry no `[key=*]` brackets, while paths reached directly do. Comparing
    bracket-stripped forms makes matching robust to that inconsistency.
    """
    return _KEY_BRACKET_RE.sub("", path)


def _normalise_input_path(path: str) -> str:
    """Ensure leading slash, drop trailing slash; keys are tolerated either way."""
    if not path:
        return ""
    p = path.strip()
    if not p.startswith("/"):
        p = "/" + p
    return p.rstrip("/")


def list_children(path: str) -> list[dict]:
    """
    Return the immediate children of a YANG container/list path.

    Derived from the leaf index: any leaf with `<path>/<segment>/...` contributes
    `<segment>` as a child. Containers without leaves underneath will not appear
    (rare in real SR Linux schemas).

    Each child dict has: name, kind ("list" if it carries a `[...]` key segment,
    "container" or "leaf" otherwise), leaf_count, sample_description.

    Matching is bracket-insensitive: a query for
    `/network-instance[name=default]/route-table` matches index entries stored
    as either `/network-instance/route-table/...` (augment-derived, no keys)
    or `/network-instance[name=*]/route-table/...`.
    """
    raw_prefix = _normalise_input_path(path)
    if not raw_prefix:
        return []
    prefix_stripped = _strip_keys(raw_prefix)
    prefix_slash = prefix_stripped + "/"

    children: dict[str, dict] = {}
    for entry in get_index():
        ipath_full = entry["path"]
        ipath_stripped = _strip_keys(ipath_full)
        if not ipath_stripped.startswith(prefix_slash):
            continue

        # Walk the original (bracketed) path segment-by-segment, skipping
        # exactly as many segments as the (stripped) prefix consumed. This
        # preserves the `[key=*]` annotation on the returned child name when
        # the index has it.
        prefix_seg_count = prefix_stripped.count("/")  # leading "/" counts as 1
        full_segments = ipath_full.lstrip("/").split("/")
        if len(full_segments) <= prefix_seg_count:
            continue
        first = full_segments[prefix_seg_count]
        deeper = len(full_segments) > prefix_seg_count + 1
        kind = "list" if "[" in first else ("container" if deeper else "leaf")

        child = children.setdefault(
            first,
            {"name": first, "kind": kind, "leaf_count": 0, "sample_description": ""},
        )
        # Prefer "list"/"container" over "leaf" if we see deeper paths later
        if child["kind"] == "leaf" and kind != "leaf":
            child["kind"] = kind
        child["leaf_count"] += 1
        if not child["sample_description"] and entry.get("description"):
            child["sample_description"] = entry["description"]
    return sorted(children.values(), key=lambda c: c["name"])


def format_children(path: str, children: list[dict]) -> str:
    if not children:
        return (
            f"No children found under {path!r}. "
            "Check the path spelling, or call search_yang_paths with a keyword."
        )
    lines = [f"Children of {path}:"]
    for c in children:
        marker = {"list": "[]", "container": "/ ", "leaf": "  "}.get(c["kind"], "  ")
        desc = f"  # {c['sample_description']}" if c["sample_description"] else ""
        lines.append(f"  {marker} {c['name']}  ({c['leaf_count']} leaves below){desc}")
    return "\n".join(lines)
