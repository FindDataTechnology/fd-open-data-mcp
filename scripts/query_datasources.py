#!/usr/bin/env python3
"""Quick query tool for registered data sources in fd-open-data-mcp."""
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path("/Users/chengsishi/finddata/fd-open-data-mcp/fd_open_data_mcp/metadata/daas.db")

def list_sources():
    """List all registered data sources."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT name, label, url, 
               (SELECT COUNT(*) FROM functions WHERE source_id = sources.id) as func_count
        FROM sources 
        ORDER BY func_count DESC, name
    """)
    
    print("="*80)
    print("REGISTERED DATA SOURCES IN FD-OPEN-DATA-MCP")
    print("="*80)
    print(f"\nTotal: {cursor.rowcount} sources\n")
    
    for name, label, url, func_count in cursor.fetchall():
        print(f"📡 {name}")
        print(f"   Label: {label}")
        print(f"   URL: {url}")
        print(f"   Functions: {func_count}")
        print()
    
    conn.close()

def search_sources(keyword):
    """Search data sources by keyword."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT name, label, url
        FROM sources 
        WHERE name LIKE ? OR label LIKE ? OR url LIKE ?
        ORDER BY name
    """, (f'%{keyword}%', f'%{keyword}%', f'%{keyword}%'))
    
    results = cursor.fetchall()
    
    print(f"\n🔍 Search results for '{keyword}': {len(results)} found\n")
    
    for name, label, url in results:
        print(f"• {name}: {label}")
        print(f"  URL: {url}")
        print()
    
    conn.close()

def show_functions(source_name):
    """Show all functions for a specific source."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT f.command, f.description, f.category, f.frequency
        FROM functions f
        JOIN sources s ON f.source_id = s.id
        WHERE s.name = ?
        ORDER BY f.command
    """, (source_name,))
    
    results = cursor.fetchall()
    
    if not results:
        print(f"❌ Source '{source_name}' not found or has no functions")
        conn.close()
        return
    
    print(f"\n📋 Functions for source '{source_name}': {len(results)} found\n")
    
    for command, description, category, frequency in results:
        print(f"• {command}")
        print(f"  Description: {description}")
        print(f"  Category: {category}")
        print(f"  Frequency: {frequency}")
        print()
    
    conn.close()

def show_stats():
    """Show database statistics."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    print("="*80)
    print("DATABASE STATISTICS")
    print("="*80)
    
    # Total sources
    cursor.execute("SELECT COUNT(*) FROM sources")
    total_sources = cursor.fetchone()[0]
    print(f"\n📊 Total Data Sources: {total_sources}")
    
    # Total functions
    cursor.execute("SELECT COUNT(*) FROM functions")
    total_functions = cursor.fetchone()[0]
    print(f"📊 Total Functions: {total_functions}")
    
    # Total columns
    cursor.execute("SELECT COUNT(*) FROM columns")
    total_columns = cursor.fetchone()[0]
    print(f"📊 Total Columns: {total_columns}")
    
    # Top sources by function count
    print(f"\n🏆 Top 10 Sources by Function Count:")
    cursor.execute("""
        SELECT s.name, s.label, COUNT(f.id) as func_count
        FROM sources s
        LEFT JOIN functions f ON s.id = f.source_id
        GROUP BY s.id
        ORDER BY func_count DESC
        LIMIT 10
    """)
    
    for name, label, count in cursor.fetchall():
        print(f"  • {name}: {count} functions")
    
    # Sources by category
    print(f"\n📁 Sources by Category:")
    cursor.execute("""
        SELECT COALESCE(f.category, 'uncategorized') as cat, COUNT(DISTINCT s.id) as source_count
        FROM sources s
        JOIN functions f ON s.id = f.source_id
        GROUP BY cat
        ORDER BY source_count DESC
    """)
    
    for category, count in cursor.fetchall():
        print(f"  • {category}: {count} sources")
    
    conn.close()

def main():
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Usage:")
        print("  query_datasources.py list              - List all sources")
        print("  query_datasources.py search <keyword>  - Search sources")
        print("  query_datasources.py functions <name>  - Show functions for source")
        print("  query_datasources.py stats             - Show database statistics")
        return
    
    command = sys.argv[1]
    
    if command == "list":
        list_sources()
    elif command == "search" and len(sys.argv) >= 3:
        search_sources(sys.argv[2])
    elif command == "functions" and len(sys.argv) >= 3:
        show_functions(sys.argv[2])
    elif command == "stats":
        show_stats()
    else:
        print(f"Unknown command: {command}")

if __name__ == "__main__":
    main()
