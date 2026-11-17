-- YSL Photobooth session metadata. Run in Supabase SQL Editor.
create table if not exists public.photobooth_sessions (
    id text primary key,
    access_token_hash text not null,
    created_at timestamptz not null default now(),
    expires_at timestamptz,
    photo_slots jsonb not null default '[]'::jsonb,
    strip_path text,
    gif_path text,
    metadata jsonb not null default '{}'::jsonb
);

alter table public.photobooth_sessions enable row level security;
create index if not exists photobooth_sessions_access_token_hash_idx
    on public.photobooth_sessions (access_token_hash);
-- No anon/authenticated policies are created. Only the server-side Supabase secret key
-- used by the Render backend can access this table. Never expose that key in browser code.
