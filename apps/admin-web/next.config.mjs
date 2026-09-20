import path from "node:path";
import { fileURLToPath } from "node:url";

const workspaceRoot = path.join(
  path.dirname(fileURLToPath(import.meta.url)),
  "../.."
);

/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  outputFileTracingRoot: workspaceRoot,
  transpilePackages: ["@wiki-agent/web-ui"]
};

export default nextConfig;
