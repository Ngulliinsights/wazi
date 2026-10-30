"""MCP server. Run: python -m wazi.server  (stdio)"""
from mcp.server.fastmcp import FastMCP

from . import tools

mcp = FastMCP("wazi", log_level="WARNING")
mcp.tool()(tools.get_tender_notice)
mcp.tool()(tools.buyer_award_history)
mcp.tool()(tools.price_benchmark)
mcp.tool()(tools.peer_baseline)
mcp.tool()(tools.window_baseline)
mcp.tool()(tools.counterparty_quality)  # pre-trade style counterparty data quality check
mcp.tool()(tools.amendment_check)  # detect proper amendment handling vs overwrites
mcp.tool()(tools.aged_items_check)  # detect unusually long open tenders
mcp.tool()(tools.budget_exposure)  # estimate budget exposure based on awards
mcp.tool()(tools.ppada_date_order_check)  # PPADA Act date order validation
mcp.tool()(tools.single_bidder_check)  # single-bidder detection
mcp.tool()(tools.termination_check)  # termination reason/date check
mcp.tool()(tools.publication_quality_check)  # missing fields per stage
mcp.tool()(tools.process_timeline_check)  # process timeline anomalies (decision window)
mcp.tool()(tools.file_brief)  # queues for human approval; there is deliberately no approve/award tool

if __name__ == "__main__":
    mcp.run()
