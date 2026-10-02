// Scheduled demo-data cleanup (wrangler.jsonc cron). Telemetry and
// evaluations past TELEMETRY_RETENTION_DAYS are deleted; accounts with no
// session in INACTIVE_ACCOUNT_RETENTION_DAYS are deleted with everything they
// own (foreign keys cascade from users and organizations).

import { type Env, INACTIVE_ACCOUNT_RETENTION_DAYS, TELEMETRY_RETENTION_DAYS } from "./config";

const DAY = 86400e3;

export async function cleanup(env: Env, now = Date.now()): Promise<Record<string, number>> {
  const telemetryCutoff = now - TELEMETRY_RETENTION_DAYS * DAY;
  const accountCutoff = now - INACTIVE_ACCOUNT_RETENTION_DAYS * DAY;
  const statements: [string, D1PreparedStatement][] = [
    ["evaluation_results", env.DB.prepare("DELETE FROM evaluation_results WHERE written_at < ?").bind(telemetryCutoff)],
    ["evaluation_jobs", env.DB.prepare("DELETE FROM evaluation_jobs WHERE created_at < ?").bind(telemetryCutoff)],
    ["spans", env.DB.prepare("DELETE FROM spans WHERE ingested_at < ?").bind(telemetryCutoff)],
    [
      "users",
      env.DB.prepare(
        `DELETE FROM users WHERE created_at < ?1 AND NOT EXISTS (
           SELECT 1 FROM dashboard_sessions s WHERE s.user_id = users.id AND s.created_at >= ?1)`,
      ).bind(accountCutoff),
    ],
    [
      "organizations",
      env.DB.prepare(
        "DELETE FROM organizations WHERE NOT EXISTS (SELECT 1 FROM organization_memberships m WHERE m.organization_id = organizations.id)",
      ),
    ],
    ["dashboard_sessions", env.DB.prepare("DELETE FROM dashboard_sessions WHERE expires_at < ?").bind(now - DAY)],
    ["rate_limits", env.DB.prepare("DELETE FROM rate_limits WHERE window_start < ?").bind(now - DAY)],
  ];
  const results = await env.DB.batch(statements.map(([, s]) => s));
  return Object.fromEntries(statements.map(([table], i) => [table, results[i].meta.changes ?? 0]));
}
