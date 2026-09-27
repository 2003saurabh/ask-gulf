/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Emit a self-contained server bundle (server.js + minimal node_modules) so
  // the Docker runtime stage stays small. See the multi-stage Dockerfile.
  output: "standalone",
};

module.exports = nextConfig;
