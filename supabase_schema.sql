-- Wardogs Profit Tracker — shared match-data schema.
--
-- One-time setup: create a free project at https://supabase.com, then
-- Project -> SQL Editor -> New query -> paste this whole file -> Run.
-- Afterwards, copy the "anon" / "public" key (Project Settings -> API)
-- into cloud_config.py, and the "service_role" key into devview_secrets.json
-- (see devview_secrets.json.example) — never the other way around.
--
-- Security model: the "anon" role is what the distributed app authenticates
-- as. It gets INSERT-only policies below and nothing else — no SELECT,
-- UPDATE, or DELETE — so every player can submit their own match data but
-- none of them, including via a decompiled copy of the app, can read
-- anyone else's. Only the service_role key (kept solely on the developer's
-- machine, used only by devview.py) bypasses Row-Level Security entirely.

create table if not exists matches (
    id uuid primary key default gen_random_uuid(),
    steam_id text not null,
    persona_name text,
    session_file text not null unique,
    map text,
    faction text,
    started_at timestamptz,
    ended_at timestamptz,
    final_profit integer,
    max_life integer,
    app_version text,
    created_at timestamptz not null default now()
);

create table if not exists match_ticks (
    id bigint generated always as identity primary key,
    match_id uuid not null references matches(id) on delete cascade,
    timestamp timestamptz,
    seconds_elapsed numeric,
    cash integer,
    life integer
);

create table if not exists role_xp_events (
    id bigint generated always as identity primary key,
    steam_id text not null,
    persona_name text,
    timestamp timestamptz,
    session_file text,
    role text not null,
    xp_gained integer not null,
    created_at timestamptz not null default now()
);

-- One-way dev news/announcements feed, read by every player. The mirror
-- image of every table above: no INSERT policy for anon at all — only the
-- service_role key (devview.py's "Post News", never the shipped app) can
-- write, so there's no public write surface and nothing to moderate.
-- Players just read.
create table if not exists news_posts (
    id uuid primary key default gen_random_uuid(),
    title text not null check (char_length(title) between 1 and 120),
    content text not null check (char_length(content) between 1 and 2000),
    created_at timestamptz not null default now()
);

create index if not exists match_ticks_match_id_idx on match_ticks (match_id);
create index if not exists matches_steam_id_idx on matches (steam_id);
create index if not exists role_xp_events_steam_id_idx on role_xp_events (steam_id);
create index if not exists news_posts_created_at_idx on news_posts (created_at desc);

alter table matches enable row level security;
alter table match_ticks enable row level security;
alter table role_xp_events enable row level security;
alter table news_posts enable row level security;

create policy "anon can insert matches" on matches
    for insert to anon with check (true);

create policy "anon can insert match_ticks" on match_ticks
    for insert to anon with check (true);

create policy "anon can insert role_xp_events" on role_xp_events
    for insert to anon with check (true);

create policy "anon can read news_posts" on news_posts
    for select to anon using (true);
