// Backport https://github.com/redis/ioredis/pull/2148 without changing the client version.
// shortcut: remove this preload when the Langfuse worker includes the upstream fix.
const Module = require("node:module");
const load = Module._load;
const patched = new WeakSet();

// Load lazily so OpenTelemetry can still instrument ioredis at application startup.
Module._load = function (id, parent, isMain) {
  const result = load.apply(this, arguments);
  if (id !== "ioredis" || patched.has(result)) return result;

  const localRequire = Module.createRequire(parent.filename);
  const version = localRequire("ioredis/package.json").version;
  if (version !== "5.10.1") {
    throw new Error("Review the Langfuse Redis timeout backport for ioredis " + version);
  }
  const handlers = localRequire("ioredis/built/redis/event_handler");
  const closeHandler = handlers.closeHandler;
  handlers.closeHandler = function (client) {
    const close = closeHandler(client);
    return function () {
      clearTimeout(client.socketTimeoutTimer);
      client.socketTimeoutTimer = undefined;
      return close.apply(this, arguments);
    };
  };

  result.prototype.setSocketTimeout = function () {
    const stream = this.stream;
    this.socketTimeoutTimer = setTimeout(() => {
      stream.destroy(
        new Error(
          "Socket timeout. Expecting data, but didn't receive any in " +
            this.options.socketTimeout +
            "ms.",
        ),
      );
      this.socketTimeoutTimer = undefined;
    }, this.options.socketTimeout);
    // ioredis processes replies before this listener inspects the pending queue.
    stream.once("data", () => {
      clearTimeout(this.socketTimeoutTimer);
      this.socketTimeoutTimer = undefined;
      if (this.commandQueue.length !== 0) this.setSocketTimeout();
    });
  };
  patched.add(result);
  console.info("Langfuse Redis socket timeout backport active (ioredis 5.10.1)");
  return result;
};
