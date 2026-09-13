-- Vesta: stop the database exposing its own functions as public endpoints.
--
-- Found by probing the live project with nothing but the public anon key:
--
--   GET /rest/v1/rpc/run_vesta_retention  ->  405, not 404
--
-- A 405 means "wrong method", which means the endpoint exists. PostgREST publishes
-- every function in the `public` schema that the caller may execute, and Postgres
-- grants EXECUTE on new functions to PUBLIC by default. So the retention job was
-- callable by anyone who opened the app, or who read the anon key out of the page,
-- which is exactly what the anon key is designed to allow.
--
-- run_vesta_retention() is SECURITY DEFINER, so it runs with the owner's rights and
-- ignores Row Level Security. Calling it does not read anyone's data, but it does
-- delete: notes and files whose 30 days in the Trash are up, and uploads that never
-- finished. An anonymous caller could therefore cut short the recovery window a
-- user was promised. That is small, but it is real, and it is not something a
-- stranger should be able to trigger.
--
-- Nothing in Vesta calls an RPC from the browser: the app talks to tables, and the
-- Worker uses the service role. So the correct setting is that client roles may
-- execute nothing at all, and any future function is closed unless deliberately
-- opened.

-- Existing functions: close them to both client roles.
revoke execute on all functions in schema public from anon, authenticated;
revoke execute on all functions in schema public from public;

-- Future functions: closed by default, so this cannot silently regress the next
-- time a migration adds one.
--
-- PUBLIC has to be named explicitly. Postgres grants EXECUTE on every new function
-- to PUBLIC, and anon and authenticated inherit through it, so revoking from those
-- two roles alone leaves the function reachable. Caught by the migration 006 test,
-- which found a new trigger function still callable after only the named revokes.
alter default privileges in schema public
  revoke execute on functions from public;
alter default privileges in schema public
  revoke execute on functions from anon, authenticated;

-- The Worker and the scheduler still need to work.
grant execute on all functions in schema public to service_role;

-- pg_cron runs run_vesta_retention() as the job owner, not as a client role, so
-- the nightly cleanup is unaffected by the revoke above.

-- ---------------------------------------------------------------------------
-- Check it. Both queries should return zero rows.
-- ---------------------------------------------------------------------------
-- Functions any client role can still execute:
--
-- select p.proname, r.rolname
-- from pg_proc p
-- join pg_namespace n on n.oid = p.pronamespace
-- cross join (values ('anon'),('authenticated')) as r(rolname)
-- where n.nspname = 'public'
--   and has_function_privilege(r.rolname, p.oid, 'EXECUTE');
--
-- Tables without Row Level Security:
--
-- select c.relname from pg_class c
-- join pg_namespace n on n.oid = c.relnamespace
-- where n.nspname = 'public' and c.relkind = 'r' and not c.relrowsecurity;
