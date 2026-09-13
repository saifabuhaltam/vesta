/**
 * Copy this to config.js and fill it in. config.js is gitignored: it is not secret
 * (the anon key is designed to be public) but it is per-deployment, and keeping it
 * out of the repo stops a stale project URL being committed.
 *
 * If config.js is missing, Vesta falls back to the local Flask server, which is how
 * the local app keeps working during the migration.
 */
window.VESTA_CONFIG = {
  // Supabase → Project Settings → API
  supabaseUrl: 'https://YOUR-PROJECT-REF.supabase.co',

  // The anon/public key. Safe to ship in the page: it identifies the project and
  // grants nothing on its own. Row Level Security is what decides access.
  // The service_role key must NEVER appear here.
  supabaseAnonKey: 'YOUR-ANON-KEY',

  // The deployed Worker, e.g. https://vesta-api.YOUR-SUBDOMAIN.workers.dev
  // Used for file uploads, downloads and Headstart.
  workerUrl: 'https://vesta-api.YOUR-SUBDOMAIN.workers.dev',
};
