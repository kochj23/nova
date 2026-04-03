#!/usr/bin/env python3
"""
nova_web_search.py — Real-time web search with local caching (24h TTL).

Uses DuckDuckGo API (free, no key required). Caches results locally to reduce
redundant queries. Integrates with memory system for enhanced recall.

Usage:
  python3 nova_web_search.py "query text"
  python3 nova_web_search.py "query" --count 5 --region us-en
  python3 nova_web_search.py "query" --json
  python3 nova_web_search.py --cache-stats
  python3 nova_web_search.py "query" --store-memories --topic "ai-news"
"""

import json
import subprocess
import sys
import time
import hashlib
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict
import argparse

# Configuration
CACHE_DIR = Path(os.getenv("WEBSEARCH_CACHE_DIR", "~/.openclaw/workspace/web-search-cache")).expanduser()
CACHE_TTL = int(os.getenv("WEBSEARCH_CACHE_TTL", "86400"))  # 24h
DEFAULT_REGION = os.getenv("WEBSEARCH_REGION", "us-en")
DEFAULT_SAFE_SEARCH = os.getenv("WEBSEARCH_SAFE_SEARCH", "moderate")
DEFAULT_COUNT = int(os.getenv("WEBSEARCH_DEFAULT_COUNT", "5"))
TIMEOUT = int(os.getenv("WEBSEARCH_TIMEOUT", "10"))

# Ensure cache directory exists
CACHE_DIR.mkdir(parents=True, exist_ok=True)


class WebSearchCache:
    """Manage web search result caching."""
    
    INDEX_FILE = CACHE_DIR / "cache-index.json"
    
    @staticmethod
    def _query_hash(query: str) -> str:
        """Generate cache key from query."""
        return hashlib.sha256(query.encode()).hexdigest()[:12]
    
    @staticmethod
    def get(query: str) -> Optional[List[Dict]]:
        """
        Get cached results if fresh.
        
        Returns:
            List of results, or None if not cached or expired
        """
        cache_key = WebSearchCache._query_hash(query)
        cache_file = CACHE_DIR / f"query-{cache_key}.json"
        
        if not cache_file.exists():
            return None
        
        try:
            with open(cache_file) as f:
                data = json.load(f)
            
            timestamp = data.get("timestamp", 0)
            age_seconds = time.time() - timestamp
            
            if age_seconds < CACHE_TTL:
                return data.get("results", [])
        except Exception:
            pass
        
        return None
    
    @staticmethod
    def store(query: str, results: List[Dict]):
        """Store search results."""
        cache_key = WebSearchCache._query_hash(query)
        cache_file = CACHE_DIR / f"query-{cache_key}.json"
        
        data = {
            "query": query,
            "timestamp": time.time(),
            "results": results,
        }
        
        try:
            with open(cache_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"Warning: Could not write cache: {e}", file=sys.stderr)
    
    @staticmethod
    def clear_all():
        """Clear all cached queries."""
        try:
            for f in CACHE_DIR.glob("query-*.json"):
                f.unlink()
            if WebSearchCache.INDEX_FILE.exists():
                WebSearchCache.INDEX_FILE.unlink()
            print("Cache cleared.")
        except Exception as e:
            print(f"Error clearing cache: {e}", file=sys.stderr)
    
    @staticmethod
    def clear_entry(query: str):
        """Clear cache entry for specific query."""
        cache_key = WebSearchCache._query_hash(query)
        cache_file = CACHE_DIR / f"query-{cache_key}.json"
        try:
            if cache_file.exists():
                cache_file.unlink()
                print(f"Cache entry cleared for: {query}")
        except Exception as e:
            print(f"Error clearing cache entry: {e}", file=sys.stderr)
    
    @staticmethod
    def stats() -> Dict:
        """Get cache statistics."""
        cache_files = list(CACHE_DIR.glob("query-*.json"))
        
        if not cache_files:
            return {
                "total_queries": 0,
                "total_size_mb": 0,
                "oldest_entry": None,
                "newest_entry": None,
            }
        
        total_size = 0
        oldest_time = time.time()
        newest_time = 0
        
        for f in cache_files:
            total_size += f.stat().st_size
            
            try:
                with open(f) as fp:
                    data = json.load(fp)
                    ts = data.get("timestamp", 0)
                    oldest_time = min(oldest_time, ts)
                    newest_time = max(newest_time, ts)
            except Exception:
                pass
        
        return {
            "total_queries": len(cache_files),
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "oldest_entry": datetime.fromtimestamp(oldest_time).isoformat() if oldest_time < time.time() else None,
            "newest_entry": datetime.fromtimestamp(newest_time).isoformat() if newest_time > 0 else None,
            "cache_ttl_hours": CACHE_TTL / 3600,
        }


class DuckDuckGoSearch:
    """Query DuckDuckGo for web search results."""
    
    @staticmethod
    def search(query: str, count: int = DEFAULT_COUNT,
               region: str = DEFAULT_REGION,
               safe_search: str = DEFAULT_SAFE_SEARCH) -> Optional[List[Dict]]:
        """
        Search DuckDuckGo API.
        
        Args:
            query: Search query
            count: Number of results
            region: Region code (e.g., "us-en", "uk-en")
            safe_search: Level (strict, moderate, off)
        
        Returns:
            List of results with title, url, snippet, or None if error
        """
        # Safe search param mapping
        safe_search_map = {"strict": "1", "moderate": "0", "off": "-1"}
        safe_val = safe_search_map.get(safe_search.lower(), "0")
        
        # Build API call
        params = [
            f"q={query}",
            f"format=json",
            f"no_redirect=1",
            f"no_html=1",
            f"t=nova",
            f"kl={region}",
            f"safe={safe_val}",
        ]
        
        url = "https://api.duckduckgo.com/?" + "&".join(params)
        
        try:
            result = subprocess.run(
                ["curl", "-s", "-m", str(TIMEOUT), "-A", "Nova-Local-AI",
                 "--", url],
                capture_output=True,
                text=True,
                timeout=TIMEOUT + 2
            )
            
            if result.returncode == 0 and result.stdout:
                data = json.loads(result.stdout)
                
                results = []
                
                # Parse abstract result
                if data.get("AbstractText"):
                    results.append({
                        "title": data.get("AbstractTitle", "Definition"),
                        "url": data.get("AbstractURL", ""),
                        "snippet": data.get("AbstractText", ""),
                        "source": "duckduckgo",
                    })
                
                # Parse related topics
                for item in data.get("RelatedTopics", [])[:count]:
                    if "Text" in item:
                        results.append({
                            "title": item.get("FirstURL", "").split("/")[-1] or item.get("FirstURL", ""),
                            "url": item.get("FirstURL", ""),
                            "snippet": item.get("Text", ""),
                            "source": "duckduckgo",
                        })
                
                return results[:count] if results else None
        except Exception as e:
            print(f"DuckDuckGo search error: {e}", file=sys.stderr)
        
        return None


def search(query: str, count: int = DEFAULT_COUNT,
           region: str = DEFAULT_REGION,
           safe_search: str = DEFAULT_SAFE_SEARCH,
           force_refresh: bool = False,
           cache_only: bool = False) -> Optional[List[Dict]]:
    """
    Perform web search with caching.
    
    Args:
        query: Search query
        count: Number of results
        region: Region code
        safe_search: Safe search level
        force_refresh: Skip cache and refresh
        cache_only: Return cached results only
    
    Returns:
        List of search results, or None
    """
    # Try cache first (unless force_refresh)
    if not force_refresh:
        cached = WebSearchCache.get(query)
        if cached:
            return cached
    
    # If cache_only, don't search web
    if cache_only:
        return None
    
    # Search DuckDuckGo
    results = DuckDuckGoSearch.search(query, count, region, safe_search)
    
    if results:
        # Store in cache
        WebSearchCache.store(query, results)
    
    return results


def store_as_memories(results: List[Dict], topic: str = "web-search"):
    """
    Store search results as vector memories.
    
    Requires nova_remember.sh to be available.
    """
    recall_script = Path("~/.openclaw/scripts/nova_remember.sh").expanduser()
    
    if not recall_script.exists():
        print(f"Warning: Memory script not found at {recall_script}", file=sys.stderr)
        return
    
    for result in results:
        text = f"{result['title']}: {result['snippet']}"
        metadata = json.dumps({"topic": topic, "source": "web-search"})
        
        try:
            subprocess.run(
                [str(recall_script), text, "web-search", metadata],
                capture_output=True,
                timeout=5
            )
        except Exception as e:
            print(f"Warning: Could not store memory: {e}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Web search with local caching")
    parser.add_argument("query", nargs="?", help="Search query")
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT, help="Number of results")
    parser.add_argument("--region", default=DEFAULT_REGION, help="Region code (us-en, uk-en, de-de, etc.)")
    parser.add_argument("--safe-search", default=DEFAULT_SAFE_SEARCH, help="Level: strict, moderate, off")
    parser.add_argument("--force-refresh", action="store_true", help="Bypass cache")
    parser.add_argument("--cache-only", action="store_true", help="Return cached results only")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument("--cache-stats", action="store_true", help="Show cache statistics")
    parser.add_argument("--clear-cache", action="store_true", help="Clear all cache")
    parser.add_argument("--clear-cache-entry", action="store_true", help="Clear cache for this query")
    parser.add_argument("--store-memories", action="store_true", help="Store results as vector memories")
    parser.add_argument("--memory-topic", default="web-search", help="Topic tag for memories")
    parser.add_argument("--verbose", action="store_true", help="Verbose output")
    
    args = parser.parse_args()
    
    # Handle cache operations
    if args.cache_stats:
        stats = WebSearchCache.stats()
        if args.json:
            print(json.dumps(stats, indent=2))
        else:
            print("Cache statistics:")
            print(f"  Total queries: {stats['total_queries']}")
            print(f"  Total size: {stats['total_size_mb']} MB")
            print(f"  Cache TTL: {stats['cache_ttl_hours']} hours")
            if stats['oldest_entry']:
                print(f"  Oldest: {stats['oldest_entry']}")
            if stats['newest_entry']:
                print(f"  Newest: {stats['newest_entry']}")
        return 0
    
    if args.clear_cache:
        WebSearchCache.clear_all()
        return 0
    
    if not args.query:
        if args.clear_cache_entry:
            print("Error: --clear-cache-entry requires a query", file=sys.stderr)
            return 1
        parser.print_help()
        return 1
    
    if args.clear_cache_entry:
        WebSearchCache.clear_entry(args.query)
        return 0
    
    # Perform search
    results = search(
        args.query,
        count=args.count,
        region=args.region,
        safe_search=args.safe_search,
        force_refresh=args.force_refresh,
        cache_only=args.cache_only
    )
    
    if results:
        if args.store_memories:
            store_as_memories(results, args.memory_topic)
        
        if args.json:
            output = {
                "query": args.query,
                "count": len(results),
                "results": results,
                "cached": not args.force_refresh,
            }
            print(json.dumps(output, indent=2))
        else:
            if args.verbose:
                print(f"Search: {args.query} ({len(results)} results)")
            for i, result in enumerate(results, 1):
                print(f"\n{i}. {result['title']}")
                print(f"   {result['url']}")
                if result.get('snippet'):
                    snippet = result['snippet'][:200]
                    if len(result['snippet']) > 200:
                        snippet += "..."
                    print(f"   {snippet}")
        return 0
    else:
        if args.json:
            print(json.dumps({
                "success": False,
                "error": "No results found or search failed"
            }, indent=2))
        else:
            print(f"No results found for: {args.query}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
