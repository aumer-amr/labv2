const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.join(__dirname, "../../kubernetes/apps/ai/langfuse/app/redis-socket-timeout.cjs"),
  "utf8",
);

function harness(version = "5.10.1") {
  const timers = new Map();
  const messages = [];
  let loads = 0;
  let closes = 0;
  class Redis {
    constructor() {
      this.options = { socketTimeout: 30000 };
      this.commandQueue = [];
      this.stream = new EventEmitter();
      this.stream.destroy = (error) => {
        this.stream.error = error;
      };
    }
  }
  const handlers = {
    closeHandler: () => () => {
      closes++;
    },
  };
  const unrelated = {};
  const module = {
    _load(id) {
      loads++;
      return id === "ioredis" ? Redis : unrelated;
    },
    createRequire() {
      return (id) => {
        if (id === "ioredis/package.json") return { version };
        assert.equal(id, "ioredis/built/redis/event_handler");
        return handlers;
      };
    },
  };
  vm.runInNewContext(source, {
    require(id) {
      assert.equal(id, "node:module");
      return module;
    },
    setTimeout(fn, delay) {
      const timer = {};
      timers.set(timer, { fn, delay });
      return timer;
    },
    clearTimeout(timer) {
      timers.delete(timer);
    },
    console: { info: (message) => messages.push(message) },
  });
  return {
    Redis,
    handlers,
    timers,
    messages,
    module,
    unrelated,
    load: () => module._load("ioredis", { filename: "/app/worker/dist/index.js" }),
    loads: () => loads,
    closes: () => closes,
    fire(timer) {
      const pending = timers.get(timer);
      timers.delete(timer);
      pending.fn();
    },
  };
}

test("preload stays lazy and patches each client constructor once", () => {
  const h = harness();
  assert.equal(h.loads(), 0);
  assert.equal(h.module._load("unrelated"), h.unrelated);
  h.load();
  const close = h.handlers.closeHandler;
  const timeout = h.Redis.prototype.setSocketTimeout;
  h.load();
  assert.equal(h.handlers.closeHandler, close);
  assert.equal(h.Redis.prototype.setSocketTimeout, timeout);
  assert.equal(h.messages.length, 1);
});

test("unsupported client versions require review", () => {
  assert.throws(() => harness("6.0.0").load(), /Review.*6\.0\.0/);
});

test("close cancels the old timer before a replacement receives data", () => {
  const h = harness();
  h.load();
  const client = new h.Redis();
  client.setSocketTimeout();
  assert.equal(h.timers.size, 1);
  h.handlers.closeHandler(client)();
  assert.equal(h.closes(), 1);
  assert.equal(client.socketTimeoutTimer, undefined);
  assert.equal(h.timers.size, 0);
  client.stream = new h.Redis().stream;
  client.setSocketTimeout();
  client.stream.emit("data");
  assert.equal(h.timers.size, 0);
  assert.equal(client.stream.error, undefined);
});

test("timeout callback belongs to the original stream", () => {
  const h = harness();
  h.load();
  const client = new h.Redis();
  const original = client.stream;
  original.destroy = (error) => {
    original.error = error;
  };
  client.setSocketTimeout();
  const timer = client.socketTimeoutTimer;
  const replacement = new h.Redis().stream;
  client.stream = replacement;
  h.fire(timer);
  assert.match(original.error.message, /didn't receive any in 30000ms/);
  assert.equal(replacement.error, undefined);
});

test("genuine stalled connections still time out after the configured interval", () => {
  const h = harness();
  h.load();
  const client = new h.Redis();
  client.options.socketTimeout = 45000;
  client.setSocketTimeout();
  const timer = client.socketTimeoutTimer;
  assert.equal(h.timers.get(timer).delay, 45000);
  h.fire(timer);
  assert.match(client.stream.error.message, /45000ms/);
  assert.equal(client.socketTimeoutTimer, undefined);
});

test("partial replies rearm protection until the pending queue is empty", () => {
  const h = harness();
  h.load();
  const client = new h.Redis();
  client.commandQueue.push({});
  client.setSocketTimeout();
  const first = client.socketTimeoutTimer;
  client.stream.emit("data");
  assert.equal(h.timers.has(first), false);
  assert.equal(h.timers.size, 1);
  assert.notEqual(client.socketTimeoutTimer, first);
  client.commandQueue.pop();
  client.stream.emit("data");
  assert.equal(h.timers.size, 0);
  assert.equal(client.stream.listenerCount("data"), 0);
});
