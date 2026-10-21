const { PHASE_DEVELOPMENT_SERVER } = require("next/constants");
const { ensureSingleDevServer, registerDevWorker } = require("./scripts/dev-server-lock.cjs");

/** @type {import('next').NextConfig} */
module.exports = (phase) => {
  const development = phase === PHASE_DEVELOPMENT_SERVER;
  // The CLI parent owns the lock; Next's child workers and hot restarts belong
  // to that same instance and must not clean its output directory again.
  if (development) {
    if (process.env.NEXT_PRIVATE_WORKER) registerDevWorker(__dirname);
    else ensureSingleDevServer(__dirname);
  }
  return {
    distDir: development ? ".next-dev" : ".next",
    env: {
      NEXT_PUBLIC_API_BASE_URL: process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000"
    }
  };
};
