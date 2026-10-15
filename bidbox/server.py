"""MCP server. Run: python -m bidbox.server  (stdio)"""
from mcp.server.fastmcp import FastMCP

from . import tools

mcp = FastMCP("bidbox", log_level="WARNING")
mcp.tool()(tools.get_tender_notice)
mcp.tool()(tools.buyer_award_history)
mcp.tool()(tools.price_benchmark)
mcp.tool()(tools.peer_baseline)
mcp.tool()(tools.window_baseline)
mcp.tool()(tools.file_brief)  # queues for human approval; there is deliberately no approve/award tool

if __name__ == "__main__":
    mcp.run()
