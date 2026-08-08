"""Crawl control-center panel (add-fund-crawl-control-center, design D7).

FastAPI + Jinja2, server-rendered, no build step. Two frontends on the same
tables: the MCP policy tools (policy_tools.py) and this panel share the same
session factory. ``PANEL_TOKEN`` env gates access (header / ?token= / cookie).
"""
from fd_open_data_mcp.panel.app import create_app

__all__ = ["create_app"]
