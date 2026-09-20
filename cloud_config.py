"""
cloud_config.py — Supabase project settings for optional match-data sync.

Fill in SUPABASE_URL and SUPABASE_ANON_KEY after creating the free Supabase
project and running supabase_schema.sql (Project → SQL Editor). Both values
come from Project Settings → API — use the "anon" / "public" key here, never
the "service_role" key: the anon key is safe to ship inside the app because
supabase_schema.sql's Row-Level Security policies only let it INSERT, never
read other players' data back. The service_role key belongs only in
devview_secrets.json, on the developer's own machine.

Leaving these blank just disables sync entirely (cloudsync.sync_now()
no-ops) — the app runs exactly as before.
"""

SUPABASE_URL = "https://iczycmxiqjciecxipekq.supabase.co"
SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImljenljbXhpcWpjaWVjeGlwZWtxIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODk4NjE1ODUsImV4cCI6MjEwNTQzNzU4NX0.Kx9Ee7-LjRg_7RZ-sFHkJo5mxuoTj4hVwmb8Vdtv-RA"
