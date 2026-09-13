-- Two throwaway accounts so the two-user isolation check can run against the
-- live project. Gmail plus-addresses land in your normal inbox.
-- Delete them afterwards from Supabase -> Authentication -> Users.
insert into allowed_emails (email, note) values
  ('saifabuhaltam+vtest1@gmail.com', 'test A, safe to delete'),
  ('saifabuhaltam+vtest2@gmail.com', 'test B, safe to delete')
on conflict (email) do nothing;
