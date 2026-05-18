# SR Linux Command Reference Corrections

Entries below were flagged by agents during operation.

## [2026-05-18 15:18:14] config-agent
**Command:** `set / network-instance bridge bridge-table vlan 10`
**Issue:** The command reference does not document VLAN configuration under bridge-table. However, the parser indicates 'vlan' is not a valid token under /network-instance bridge/bridge-table/. For bridge domains in SR Linux mac-vrf instances, VLANs may need to be configured at a different path, or may be implicitly created through subinterface VLAN encapsulation. The documentation should clarify how to create VLAN-specific bridge domains and map untagged access ports to VLANs.
