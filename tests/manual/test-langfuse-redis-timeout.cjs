// node tests/manual/test-langfuse-redis-timeout.cjs /path/to/worker/package.json
const assert = require("node:assert/strict");
const net = require("node:net");
const { createRequire } = require("node:module");
const clientRequire = createRequire(process.argv[2]);
let Redis = clientRequire("ioredis");

async function probe(mode, duplicate = false) {
  let connections = 0;
  let closes = 0;
  const sockets = new Set();
  const errors = [];
  const server = net.createServer((socket) => {
    sockets.add(socket);
    const connection = ++connections;
    socket.on("close", () => sockets.delete(socket));
    socket.on("data", (data) => {
      if (!data.toString().toLowerCase().includes("ping")) return;
      if (mode === "stalled") return;
      if (mode === "reconnect" && connection === 1) {
        setTimeout(() => socket.destroy(), 20);
        return;
      }
      socket.write("+PONG\r\n");
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const base = new Redis({
    host: "127.0.0.1",
    port: server.address().port,
    lazyConnect: true,
    enableReadyCheck: false,
    disableClientInfo: true,
    socketTimeout: 250,
    retryStrategy: mode === "stalled" ? null : () => 10,
  });
  const client = duplicate ? base.duplicate() : base;
  client.on("error", (error) => errors.push(error.message));
  client.on("close", () => closes++);
  const deadline = setTimeout(() => {
    console.error("FAIL: Redis test exceeded five seconds");
    process.exit(1);
  }, 5000);
  try {
    await client.connect();
    if (mode === "stalled") {
      await assert.rejects(client.ping());
    } else {
      assert.equal(await client.ping(), "PONG");
    }
    await new Promise((resolve) => setTimeout(resolve, 400));
    return { mode, duplicate, connections, closes, errors, status: client.status };
  } finally {
    clearTimeout(deadline);
    clearTimeout(client.socketTimeoutTimer);
    client.disconnect();
    base.disconnect();
    for (const socket of sockets) socket.destroy();
    await new Promise((resolve) => server.close(resolve));
  }
}

(async () => {
  const baseline = await probe("reconnect");
  assert.equal(baseline.connections, 3);
  assert(baseline.errors.some((message) => message.startsWith("Socket timeout.")));
  console.log("PASS: unpatched client reproduces stale timeout");

  require("../../kubernetes/apps/ai/langfuse/app/redis-socket-timeout.cjs");
  Redis = clientRequire("ioredis");
  for (let trial = 0; trial < 3; trial++) {
    for (const duplicate of [false, true]) {
      const result = await probe("reconnect", duplicate);
      assert.deepEqual(result.errors, []);
      assert.equal(result.connections, 2);
      assert.equal(result.closes, 1);
      assert.equal(result.status, "ready");
    }
  }
  console.log("PASS: replacement stays healthy in 3 direct and 3 duplicate trials");
  const healthy = await probe("healthy");
  assert.deepEqual(healthy.errors, []);
  assert.equal(healthy.connections, 1);
  const stalled = await probe("stalled");
  assert(stalled.errors.some((message) => message.includes("250ms")));
  assert.equal(stalled.connections, 1);
  console.log("PASS: healthy idle client stays connected; stalled client still times out");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
