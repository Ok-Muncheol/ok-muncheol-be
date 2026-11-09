-- 테스트 전용: Supabase가 프로젝트에 미리 만들어 두는 것을 흉내 낸다. 실제 Supabase에는 적용하지 않는다.
-- 역할(anon·authenticated·service_role), auth.users·auth.uid(), public 스키마 기본 권한, Realtime publication.
-- 기본 권한까지 흉내 내야 마이그레이션의 명시적 revoke가 실제로 효과가 있는지 검사할 수 있다.

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'anon') then
    create role anon nologin noinherit;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then
    create role authenticated nologin noinherit;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'service_role') then
    create role service_role nologin noinherit bypassrls;
  end if;
end;
$$;

create schema auth;
create table auth.users (id uuid primary key);

-- Supabase의 auth.uid()와 같은 규칙: request.jwt.claim.sub 또는 request.jwt.claims의 sub
create function auth.uid() returns uuid
language sql
stable
as $$
  select coalesce(
    nullif(current_setting('request.jwt.claim.sub', true), ''),
    nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub'
  )::uuid
$$;

grant usage on schema auth to anon, authenticated, service_role;
grant execute on function auth.uid() to anon, authenticated, service_role;

grant usage on schema public to anon, authenticated, service_role;
alter default privileges in schema public grant all on tables to anon, authenticated, service_role;
alter default privileges in schema public grant all on functions to anon, authenticated, service_role;
alter default privileges in schema public grant all on sequences to anon, authenticated, service_role;

create publication supabase_realtime;
