"""
Verify the yang_index improvements against the failure cases from trace
019e6322cd40684fb6fbaf6b34e92edd.

Checks:
  1. search() still returns correct paths for the keywords used in the trace
  2. format_results() collapses repeated-container hits (smaller, structured)
  3. list_children() answers the navigation questions that caused failures
  4. MCP-level dedup wrapper returns a short notice on repeats
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yang_index
import mcp_server  # for the dedup wrapper

PASS = "PASS"
FAIL = "FAIL"


def check(label: str, cond: bool, detail: str = "") -> bool:
    tag = PASS if cond else FAIL
    print(f"  [{tag}] {label}" + (f" — {detail}" if detail and not cond else ""))
    return cond


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------------------
# 1. Sanity: search still returns the expected paths
# ---------------------------------------------------------------------------
section("1. search() still returns correct paths")

oper = yang_index.search("oper-state", "interfaces")
check(
    "oper-state/interfaces includes /interface[name=*]/oper-state",
    any(e["path"] == "/interface[name=*]/oper-state" for e in oper),
)

rt = yang_index.search("route-table", "network-instance")
check(
    "route-table/network-instance includes ipv4-unicast routes",
    any("/network-instance/route-table/ipv4-unicast/route[" in e["path"] for e in rt),
)

bridge = yang_index.search("bridge")
check(
    "bridge search surfaces bridge-table under subinterface (not interface root)",
    any("/interface/subinterface/bridge-table/" in e["path"] for e in bridge)
    and not any(p["path"].startswith("/interface[name=*]/bridge-table") for p in bridge),
)

# ---------------------------------------------------------------------------
# 2. format_results collapses by container
# ---------------------------------------------------------------------------
section("2. format_results() container-collapsed output is smaller")

raw_lines = []
for e in rt:
    kind = "config" if e["config"] else "state"
    desc = f"  # {e['description']}" if e["description"] else ""
    raw_lines.append(f"{e['path']}  [{e['type']} | {kind}]{desc}")
old_render = "\n".join(raw_lines)
new_render = yang_index.format_results(rt)

print(f"  route-table search: old={len(old_render)} chars  new={len(new_render)} chars")
check(
    "new render is smaller than old per-leaf render",
    len(new_render) < len(old_render),
    f"new={len(new_render)} not < old={len(old_render)}",
)
check(
    "new render still mentions ipv4-unicast",
    "ipv4-unicast" in new_render,
)
check(
    "new render uses '+N more' summary",
    "more leaves under this container" in new_render,
)

# ---------------------------------------------------------------------------
# 3. list_children answers the navigation questions from the failures
# ---------------------------------------------------------------------------
section("3. list_children() resolves the actual failure cases")

# Failure 1: model invented /network-instance[name=default]/route-table/srl_nokia-ip-route-tables
# Real children per the JSON-RPC error were:
#   [ipv4-unicast, ipv6-unicast, next-hop-group, next-hop, mpls]
rt_children = yang_index.list_children("/network-instance[name=default]/route-table")
rt_names = {c["name"] for c in rt_children}
print(f"  /network-instance[name=*]/route-table children: {sorted(rt_names)}")
check(
    "route-table children include ipv4-unicast and ipv6-unicast",
    {"ipv4-unicast", "ipv6-unicast"}.issubset(rt_names),
)
check(
    "route-table children do NOT include hallucinated 'srl_nokia-ip-route-tables'",
    "srl_nokia-ip-route-tables" not in rt_names,
)

# Failure 4-6: model used /interface[name=ethernet-1/X]/bridge-table directly
# (bridge-table actually lives under subinterface)
intf_children = yang_index.list_children("/interface[name=ethernet-1/1]")
intf_names = {c["name"] for c in intf_children}
print(f"  /interface[name=*] children: {sorted(intf_names)}")
check(
    "interface children include 'subinterface[index=*]' (where bridge-table lives)",
    any("subinterface" in n for n in intf_names),
)
check(
    "interface children do NOT include bare 'bridge-table'",
    "bridge-table" not in intf_names,
)

# Drill into the right place — subinterface should have bridge-table
sub_children = yang_index.list_children("/interface[name=*]/subinterface[index=*]")
sub_names = {c["name"] for c in sub_children}
check(
    "subinterface children include 'bridge-table'",
    "bridge-table" in sub_names,
)

# Confirm formatter handles concrete keys
formatted = yang_index.format_children(
    "/network-instance[name=default]/route-table",
    rt_children,
)
check("format_children annotates child kinds", "(" in formatted and "leaves below" in formatted)
print("\n  --- sample list_yang_children output ---")
print(formatted)
print("  --- end sample ---")

# ---------------------------------------------------------------------------
# 4. MCP-level dedup wrapper returns short notice on repeats
# ---------------------------------------------------------------------------
section("4. MCP dedup short-circuits repeated queries")

# Reset state so we don't pollute by previous runs
mcp_server._RECENT_SEARCH_KEYS.clear()

search_fn = mcp_server._search_yang_paths_impl

first = search_fn("oper-state", "interfaces")
second = search_fn("oper-state", "interfaces")
third = search_fn("OPER-STATE", "Interfaces")  # case-insensitivity

check("first call returns full results", "/interface[name=*]/oper-state" in first)
check("second call returns short dedup notice", "Already searched" in second)
check("third call (different case) also de-duped", "Already searched" in third)
print(f"  first response: {len(first)} chars  |  dedup response: {len(second)} chars")

# Different keyword does NOT trigger dedup
fresh = search_fn("admin-state", "interfaces")
check("different keyword is NOT de-duped", "Already searched" not in fresh)

# ---------------------------------------------------------------------------
# 5. End-to-end size comparison: simulate the trace's 3x route-table calls
# ---------------------------------------------------------------------------
section("5. Replay trace's duplicate route-table calls — expect savings")

mcp_server._RECENT_SEARCH_KEYS.clear()
total_old = 0
total_new = 0
for i in range(3):
    # Old behaviour: every call ran full search + per-leaf formatting (~8.4KB)
    full = yang_index.format_results.__wrapped__(rt) if hasattr(yang_index.format_results, "__wrapped__") else None
    # Use raw per-leaf rendering as the "old" baseline
    raw_text = old_render
    total_old += len(raw_text)
    total_new += len(search_fn("route-table", "network-instance"))
print(f"  3x route-table calls: old~={total_old} chars  new={total_new} chars")
check(
    "duplicate-burst total response shrinks by >50%",
    total_new < total_old * 0.5,
    f"new={total_new} old={total_old}",
)

print("\nAll checks complete.")
