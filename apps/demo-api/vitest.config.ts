import { cloudflareTest, readD1Migrations } from "@cloudflare/vitest-pool-workers";
import { defineConfig } from "vitest/config";

// Tests run inside workerd (the Workers runtime) against a real local D1,
// with the production migrations applied before each test file.
export default defineConfig({
  plugins: [
    cloudflareTest(async () => ({
      wrangler: { configPath: "./wrangler.jsonc" },
      miniflare: {
        bindings: {
          TEST_MIGRATIONS: await readD1Migrations("./migrations"),
          DASHBOARD_CLIENT_IP_TOKEN: "test-dashboard-token",
        },
      },
    })),
  ],
  test: {
    setupFiles: ["./test/setup.ts"],
    // Integration tests drive the real runtime + D1 (signups hash passwords);
    // the 5 s default is too tight when files run in parallel.
    testTimeout: 30_000,
  },
});
