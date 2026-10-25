"""Remote MCP server exposing the Business Manager role to AI tools.

Every tool in `crm.mcp.tools` calls the same service-layer functions and
permission checks (`crm.core.permissions`) that the web views use - nothing
here bypasses the CRM's own guardrails. See docs/crm/user-manuals/11_business_manager_ai.txt
for what the connected AI can and cannot do.
"""
