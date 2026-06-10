create table if not exists scans (
  id          bigint generated always as identity primary key,
  code        text not null,
  device      text,
  status      text default 'received',
  source      text,
  scanned_at  timestamptz,
  created_at  timestamptz default now()
);

create index if not exists idx_scans_created_at on scans (created_at desc);
create index if not exists idx_scans_source on scans (source);
create index if not exists idx_scans_code on scans (code);

create table if not exists app_settings (
  key        text primary key,
  value      jsonb not null default '[]'::jsonb,
  updated_at timestamptz default now()
);
insert into app_settings (key, value) values ('forward_urls', '[]'::jsonb)
on conflict (key) do nothing;
