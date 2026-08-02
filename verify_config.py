#!/usr/bin/env python3
"""Verify all configuration is properly loaded."""
import os
import sys
from pathlib import Path

# Load .env files
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=".env.local", override=True)
    load_dotenv(dotenv_path=".env", override=False)
    dotenv_loaded = True
except ImportError:
    dotenv_loaded = False
    print("⚠️  python-dotenv not installed. Install with: pip install python-dotenv")

def check_env_var(name: str, required: bool = False, mask: bool = False) -> tuple[bool, str]:
    """Check if an environment variable is set."""
    value = os.environ.get(name)
    if value is None or value == "":
        status = "❌ NOT SET" if required else "⚠️  Optional"
        return False, status
    
    if mask and len(value) > 8:
        display = "***" + value[-4:]
    else:
        display = value[:50] + "..." if len(value) > 50 else value
    
    return True, f"✅ {display}"

def check_playwright_installed() -> tuple[bool, str]:
    """Check if Playwright is installed."""
    try:
        import playwright
        return True, "✅ Installed"
    except ImportError:
        return False, "❌ Not installed (pip install playwright)"

def check_playwright_browsers() -> tuple[bool, str]:
    """Check if Playwright browsers are installed."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            # Try to launch chromium
            try:
                browser = p.chromium.launch(headless=True)
                browser.close()
                return True, "✅ Browsers installed"
            except Exception:
                return False, "❌ Browsers not installed (playwright install chromium)"
    except ImportError:
        return False, "⚠️  Playwright not installed"

def main():
    print("\n" + "=" * 70)
    print("fd-open-data-mcp Configuration Verification")
    print("=" * 70 + "\n")
    
    if not dotenv_loaded:
        print("⚠️  Warning: python-dotenv not installed")
        print("   Install with: pip install python-dotenv\n")
    
    checks = [
        ("Database", [
            ("FD_OPEN_DATA_MCP_DATABASE_URL", False, False),
        ]),
        ("Workspace", [
            ("FINDDATA_ROOT", False, False),
        ]),
        ("SEC EDGAR", [
            ("EDGAR_IDENTITY", True, False),
        ]),
        ("LLM (PDF Extraction)", [
            ("LLM_BASE_URL", False, False),
            ("LLM_API_KEY", True, True),  # required for PDF extraction
            ("LLM_MODEL", False, False),
        ]),
        ("Financial Platforms", [
            ("WIND_API_KEY", False, True),
            ("IFIND_API_KEY", False, True),
            ("TONGDAOXIN_API_KEY", False, True),
        ]),
        ("Playwright (Web Scraping)", [
            ("PLAYWRIGHT_BROWSER", False, False),
            ("PLAYWRIGHT_HEADLESS", False, False),
            ("PLAYWRIGHT_TIMEOUT", False, False),
        ]),
    ]
    
    all_passed = True
    required_failed = 0
    
    for category, vars in checks:
        print(f"\n{category}:")
        print("-" * 70)
        for var_name, required, mask in vars:
            success, status = check_env_var(var_name, required, mask)
            print(f"  {var_name:<35} {status}")
            if not success and required:
                all_passed = False
                required_failed += 1
    
    # Check Playwright installation
    print(f"\nPlaywright Installation:")
    print("-" * 70)
    pw_installed, pw_status = check_playwright_installed()
    print(f"  {'Playwright Package':<35} {pw_status}")
    
    if pw_installed:
        browsers_installed, browser_status = check_playwright_browsers()
        print(f"  {'Playwright Browsers':<35} {browser_status}")
        if not browsers_installed:
            print("\n  Install browsers with: playwright install chromium")
    
    print("\n" + "=" * 70)
    if all_passed:
        print("✅ All required configuration is set!")
        print("\nYou can now run:")
        print("  fd-open-data-mcp migrate")
        print("  fd-open-data-mcp list-sources")
        print("  fd-open-data-mcp serve")
    else:
        print(f"⚠️  {required_failed} required configuration(s) missing")
        print("\nSet missing values in .env.local:")
        print("  nano .env.local")
        print("\nOr see CONFIG.md for detailed configuration guide")
    print("=" * 70 + "\n")
    
    return 0 if all_passed else 1

if __name__ == "__main__":
    sys.exit(main())
