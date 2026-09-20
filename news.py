"""
news.py — Read-only dev news/announcements feed, shown to every player.

The mirror image of cloudsync.py: this only ever reads, via the shipped
anon key (which has a SELECT policy on news_posts and nothing else
readable — see supabase_schema.sql), never writes. Posting only happens
from devview.py using the service_role key, kept solely on the
developer's machine — there is no path from the shipped app to writing a
news post.
"""

import requests

import cloud_config

REQUEST_TIMEOUT = 8


def fetch_news(limit=20):
    """Most recent posts first, or [] on any failure (no internet, not
    configured yet, etc.) — a missing news feed is never an error worth
    surfacing to a player, just nothing to show."""
    if not cloud_config.SUPABASE_URL or not cloud_config.SUPABASE_ANON_KEY:
        return []
    try:
        resp = requests.get(
            f"{cloud_config.SUPABASE_URL}/rest/v1/news_posts",
            headers={
                "apikey": cloud_config.SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {cloud_config.SUPABASE_ANON_KEY}",
            },
            params={"select": "*", "order": "created_at.desc", "limit": limit},
            timeout=REQUEST_TIMEOUT,
        )
        if not resp.ok:
            return []
        return resp.json()
    except requests.RequestException:
        return []
