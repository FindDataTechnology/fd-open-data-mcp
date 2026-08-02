"""Playwright browser management and scraping utilities."""
from __future__ import annotations

import logging
import os
from typing import Optional, Any
from contextlib import contextmanager

logger = logging.getLogger(__name__)


def _get_config() -> dict:
    """Get Playwright configuration from environment."""
    return {
        "browser_type": os.environ.get("PLAYWRIGHT_BROWSER", "chromium"),
        "headless": os.environ.get("PLAYWRIGHT_HEADLESS", "true").lower() == "true",
        "timeout": int(os.environ.get("PLAYWRIGHT_TIMEOUT", "30000")),
        "slow_mo": int(os.environ.get("PLAYWRIGHT_SLOW_MO", "0")),
        "user_agent": os.environ.get("PLAYWRIGHT_USER_AGENT"),
        "viewport_width": int(os.environ.get("PLAYWRIGHT_VIEWPORT_WIDTH", "1920")),
        "viewport_height": int(os.environ.get("PLAYWRIGHT_VIEWPORT_HEIGHT", "1080")),
        "javascript_enabled": os.environ.get("PLAYWRIGHT_JAVASCRIPT_ENABLED", "true").lower() == "true",
        "ignore_https_errors": os.environ.get("PLAYWRIGHT_IGNORE_HTTPS_ERRORS", "false").lower() == "true",
        "proxy_server": os.environ.get("PLAYWRIGHT_PROXY_SERVER"),
        "proxy_username": os.environ.get("PLAYWRIGHT_PROXY_USERNAME"),
        "proxy_password": os.environ.get("PLAYWRIGHT_PROXY_PASSWORD"),
    }


@contextmanager
def get_browser():
    """Context manager for Playwright browser instance.
    
    Usage:
        with get_browser() as browser:
            page = browser.new_page()
            page.goto("https://example.com")
            content = page.content()
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ImportError(
            "Playwright not installed. Install with: pip install playwright\n"
            "Then install browsers: playwright install chromium"
        )
    
    config = _get_config()
    logger.info(f"Starting Playwright browser: {config['browser_type']}")
    
    with sync_playwright() as p:
        # Select browser type
        browser_type = getattr(p, config["browser_type"])
        
        # Build launch options
        launch_opts = {
            "headless": config["headless"],
            "slow_mo": config["slow_mo"],
        }
        
        # Add proxy if configured
        if config["proxy_server"]:
            proxy_opts = {"server": config["proxy_server"]}
            if config["proxy_username"]:
                proxy_opts["username"] = config["proxy_username"]
                proxy_opts["password"] = config["proxy_password"]
            launch_opts["proxy"] = proxy_opts
        
        # Launch browser
        browser = browser_type.launch(**launch_opts)
        
        try:
            yield browser
        finally:
            browser.close()
            logger.info("Browser closed")


def scrape_page(
    url: str,
    wait_for: Optional[str] = None,
    timeout: Optional[int] = None,
    javascript: Optional[bool] = None,
) -> str:
    """Scrape a web page and return HTML content.
    
    Args:
        url: URL to scrape
        wait_for: CSS selector to wait for (e.g., "div#content")
        timeout: Timeout in milliseconds (overrides config)
        javascript: Enable/disable JavaScript (overrides config)
        
    Returns:
        HTML content of the page
        
    Example:
        html = scrape_page("https://example.com", wait_for="table.data")
    """
    config = _get_config()
    
    if timeout is None:
        timeout = config["timeout"]
    if javascript is None:
        javascript = config["javascript_enabled"]
    
    with get_browser() as browser:
        # Create new page with viewport
        context = browser.new_context(
            viewport={"width": config["viewport_width"], "height": config["viewport_height"]},
            user_agent=config["user_agent"],
            java_script_enabled=javascript,
            ignore_https_errors=config["ignore_https_errors"],
        )
        
        page = context.new_page()
        page.set_default_timeout(timeout)
        
        logger.info(f"Navigating to {url}")
        page.goto(url, wait_until="networkidle")
        
        # Wait for specific element if requested
        if wait_for:
            logger.info(f"Waiting for selector: {wait_for}")
            page.wait_for_selector(wait_for, timeout=timeout)
        
        # Get page content
        content = page.content()
        logger.info(f"Scraped {len(content)} bytes from {url}")
        
        context.close()
        return content


def scrape_with_selector(
    url: str,
    selector: str,
    attribute: Optional[str] = None,
    wait_for: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Scrape elements matching a CSS selector.
    
    Args:
        url: URL to scrape
        selector: CSS selector for elements to extract
        attribute: Optional attribute to extract (e.g., "href", "src")
        wait_for: CSS selector to wait for before extraction
        
    Returns:
        List of dicts with 'text' and optionally 'attribute' keys
        
    Example:
        links = scrape_with_selector(
            "https://example.com",
            "a.article-link",
            attribute="href"
        )
    """
    config = _get_config()
    
    with get_browser() as browser:
        context = browser.new_context(
            viewport={"width": config["viewport_width"], "height": config["viewport_height"]},
            user_agent=config["user_agent"],
        )
        
        page = context.new_page()
        page.set_default_timeout(config["timeout"])
        
        page.goto(url, wait_until="networkidle")
        
        if wait_for:
            page.wait_for_selector(wait_for, timeout=config["timeout"])
        
        # Find all matching elements
        elements = page.query_selector_all(selector)
        
        results = []
        for elem in elements:
            item = {"text": elem.inner_text()}
            if attribute:
                item["attribute"] = elem.get_attribute(attribute)
            results.append(item)
        
        logger.info(f"Found {len(results)} elements matching '{selector}'")
        
        context.close()
        return results
