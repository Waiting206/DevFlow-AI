const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawn, spawnSync } = require("node:child_process");
const { once } = require("node:events");
const { test } = require("node:test");
const { ensureSingleDevServer } = require("./dev-server-lock.cjs");

function fixture(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "repopilot-dev-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  return directory;
}

test("fresh CLI clears interrupted dev output, preserving production and source", (t) => {
  const directory = fixture(t);
  for (const name of [".next-dev", ".next", "app"]) {
    fs.mkdirSync(path.join(directory, name));
    fs.writeFileSync(path.join(directory, name, "keep.txt"), "content");
  }
  const release = ensureSingleDevServer(directory);
  t.after(release);
  assert.equal(fs.existsSync(path.join(directory, ".next-dev")), false);
  assert.equal(fs.readFileSync(path.join(directory, ".next/keep.txt"), "utf8"), "content");
  assert.equal(fs.readFileSync(path.join(directory, "app/keep.txt"), "utf8"), "content");
  fs.mkdirSync(path.join(directory, ".next-dev"));
  assert.equal(ensureSingleDevServer(directory), release);
  assert.equal(fs.existsSync(path.join(directory, ".next-dev")), true);
  release();
  assert.equal(fs.existsSync(path.join(directory, ".next-dev.lock")), false);
});

test("another CLI is rejected without removing the running server's chunks", (t) => {
  const directory = fixture(t);
  const release = ensureSingleDevServer(directory);
  t.after(release);
  fs.mkdirSync(path.join(directory, ".next-dev"));
  fs.writeFileSync(path.join(directory, ".next-dev/page.js"), "active chunk");
  const child = spawnSync(process.execPath, ["-e", `require(${JSON.stringify(require.resolve("./dev-server-lock.cjs"))}).ensureSingleDevServer(${JSON.stringify(directory)})`], { encoding: "utf8" });
  assert.notEqual(child.status, 0);
  assert.match(child.stderr, /前端开发服务已在运行/);
  assert.equal(fs.readFileSync(path.join(directory, ".next-dev/page.js"), "utf8"), "active chunk");
  assert.equal(JSON.parse(fs.readFileSync(path.join(directory, ".next-dev.lock"))).pid, process.pid);
});

test("abruptly killed CLI leaves a reclaimable lock", async (t) => {
  const directory = fixture(t);
  const child = spawn(process.execPath, ["-e", `require(${JSON.stringify(require.resolve("./dev-server-lock.cjs"))}).ensureSingleDevServer(${JSON.stringify(directory)}); process.stdout.write('ready'); setInterval(() => {}, 1000)`], { stdio: ["ignore", "pipe", "inherit"] });
  t.after(() => child.kill("SIGKILL"));
  await once(child.stdout, "data");
  const exited = once(child, "exit");
  child.kill("SIGKILL");
  await exited;
  assert.equal(fs.existsSync(path.join(directory, ".next-dev.lock")), true);
  const release = ensureSingleDevServer(directory);
  t.after(release);
  assert.equal(JSON.parse(fs.readFileSync(path.join(directory, ".next-dev.lock"))).pid, process.pid);
});

test("incomplete startup lock is never treated as an abandoned server", (t) => {
  const directory = fixture(t);
  fs.writeFileSync(path.join(directory, ".next-dev.lock"), "");
  fs.mkdirSync(path.join(directory, ".next-dev"));
  assert.throws(() => ensureSingleDevServer(directory), /启动锁尚未就绪/);
  assert.equal(fs.existsSync(path.join(directory, ".next-dev")), true);
});

test("normal CLI exit releases the lock", (t) => {
  const directory = fixture(t);
  const child = spawnSync(process.execPath, ["-e", `require(${JSON.stringify(require.resolve("./dev-server-lock.cjs"))}).ensureSingleDevServer(${JSON.stringify(directory)})`], { encoding: "utf8" });
  assert.equal(child.status, 0, child.stderr);
  assert.equal(fs.existsSync(path.join(directory, ".next-dev.lock")), false);
});

test("orphaned Next worker keeps its chunks protected after the CLI is killed", async (t) => {
  const directory = fixture(t);
  const helper = JSON.stringify(require.resolve("./dev-server-lock.cjs"));
  const workerCode = `require(${helper}).registerDevWorker(${JSON.stringify(directory)}); process.stdout.write('ready'); setInterval(() => {}, 1000)`;
  const parentCode = `require(${helper}).ensureSingleDevServer(${JSON.stringify(directory)}); const worker = require('node:child_process').spawn(process.execPath, ['-e', ${JSON.stringify(workerCode)}], {stdio:['ignore','pipe','inherit']}); worker.stdout.once('data', () => process.stdout.write(String(worker.pid))); setInterval(() => {}, 1000)`;
  const parent = spawn(process.execPath, ["-e", parentCode], { stdio: ["ignore", "pipe", "inherit"] });
  t.after(() => parent.kill("SIGKILL"));
  const [data] = await once(parent.stdout, "data");
  const workerPid = Number(data.toString());
  t.after(() => { try { process.kill(workerPid, "SIGTERM"); } catch {} });
  const exited = once(parent, "exit");
  parent.kill("SIGKILL");
  await exited;
  fs.mkdirSync(path.join(directory, ".next-dev"));
  fs.writeFileSync(path.join(directory, ".next-dev/page.js"), "orphaned worker's chunk");
  assert.throws(() => ensureSingleDevServer(directory), /前端开发服务已在运行/);
  assert.equal(fs.existsSync(path.join(directory, ".next-dev/page.js")), true);
});

test("build and start use production output without acquiring the dev lock", () => {
  const config = require("../next.config.js");
  const { PHASE_PRODUCTION_BUILD, PHASE_PRODUCTION_SERVER } = require("next/constants");
  const before = process.env.NEXT_PRIVATE_WORKER;
  process.env.NEXT_PRIVATE_WORKER = "1";
  try {
    assert.equal(config(PHASE_PRODUCTION_BUILD).distDir, ".next");
    assert.equal(config(PHASE_PRODUCTION_SERVER).distDir, ".next");
  } finally {
    if (before === undefined) delete process.env.NEXT_PRIVATE_WORKER;
    else process.env.NEXT_PRIVATE_WORKER = before;
  }
});
