const fs = require("node:fs");
const path = require("node:path");
const { randomUUID } = require("node:crypto");

const heldLocks = new Map();

function isAlive(pid) {
  if (!Number.isSafeInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error.code !== "ESRCH";
  }
}

// Next 15 has no project-wide dev lock. Different ports still write the same
// manifests and chunks, so the CLI parent must own this lock for its lifetime.
function ensureSingleDevServer(frontendDirectory) {
  const directory = fs.realpathSync(frontendDirectory);
  const lockPath = path.join(directory, ".next-dev.lock");
  if (heldLocks.has(lockPath)) return heldLocks.get(lockPath);

  const owner = { pid: process.pid, token: randomUUID() };
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      fs.writeFileSync(lockPath, JSON.stringify(owner), { flag: "wx", mode: 0o600 });
    } catch (error) {
      if (error.code !== "EEXIST") throw error;
      let previous;
      try {
        previous = JSON.parse(fs.readFileSync(lockPath, "utf8"));
      } catch (readError) {
        if (readError.code === "ENOENT") continue;
        // Never delete a lock that another process may still be writing.
        throw new Error("前端启动锁尚未就绪，请稍后重试；不要同时启动多个开发服务。");
      }
      const activePid = [previous.pid, previous.workerPid].find(isAlive);
      if (activePid) {
        throw new Error(`RepoPilot 前端开发服务已在运行（PID ${activePid}）。请使用现有服务，或先停止它再启动；更换端口仍会覆盖同一个 .next-dev，已阻止重复启动。`);
      }
      // A killed CLI cannot run its exit handler. Reclaim only its stale lock.
      try {
        if (fs.readFileSync(lockPath, "utf8") === JSON.stringify(previous)) fs.unlinkSync(lockPath);
      } catch (readError) {
        if (readError.code !== "ENOENT") throw readError;
      }
      continue;
    }

    const release = () => {
      try {
        const current = JSON.parse(fs.readFileSync(lockPath, "utf8"));
        if (current.token === owner.token && !isAlive(current.workerPid)) fs.unlinkSync(lockPath);
      } catch (error) {
        if (error.code !== "ENOENT") console.warn("无法释放前端启动锁：", error.message);
      }
      heldLocks.delete(lockPath);
      process.removeListener("exit", release);
    };
    heldLocks.set(lockPath, release);
    process.once("exit", release);
    try {
      // Start with coherent manifests after an interrupted run. Production
      // artifacts live in .next and are deliberately preserved.
      fs.rmSync(path.join(directory, ".next-dev"), { recursive: true, force: true });
    } catch (error) {
      release();
      throw error;
    }
    return release;
  }
  throw new Error("前端启动锁发生竞争，请等待现有服务退出后再试。");
}

function registerDevWorker(frontendDirectory) {
  const lockPath = path.join(fs.realpathSync(frontendDirectory), ".next-dev.lock");
  const owner = JSON.parse(fs.readFileSync(lockPath, "utf8"));
  if (owner.workerPid === process.pid || owner.workerPid === process.ppid) return;
  if (owner.pid !== process.ppid) throw new Error("前端开发进程不属于当前启动实例，请停止旧服务后重新启动。");

  // Remember the actual server too: a SIGKILL of the CLI must not let a new
  // instance remove chunks still being served by its orphaned child.
  const temporary = `${lockPath}.${process.pid}.tmp`;
  fs.writeFileSync(temporary, JSON.stringify({ ...owner, workerPid: process.pid }), { mode: 0o600 });
  fs.renameSync(temporary, lockPath);
  process.once("exit", () => {
    try {
      const current = JSON.parse(fs.readFileSync(lockPath, "utf8"));
      if (current.token === owner.token && current.workerPid === process.pid && !isAlive(current.pid)) fs.unlinkSync(lockPath);
    } catch (error) {
      if (error.code !== "ENOENT") console.warn("无法释放前端工作进程锁：", error.message);
    }
  });
}

module.exports = { ensureSingleDevServer, registerDevWorker };
