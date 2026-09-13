import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Phase 4B (deployment): a self-contained production server bundle
  // (.next/standalone), required for the lean multi-stage Docker image in
  // apps/dashboard/Dockerfile.
  output: "standalone",
};

export default nextConfig;
