/** Transport for a rebuilt OpenTTD 15.3 WASM with the Parabox C++ patch. */
const PROTOCOL = "parabox-openttd-v1";

function transportError(message, requestId, outcomeUnknown) {
  const error = new Error(message);
  error.requestId = requestId;
  error.outcomeUnknown = outcomeUnknown;
  return error;
}

function squirrelJSON(value) {
  if (value === null || typeof value === "string" || typeof value === "boolean") return true;
  if (typeof value === "number") return Number.isSafeInteger(value);
  if (Array.isArray(value)) return value.every(squirrelJSON);
  if (typeof value === "object") return Object.values(value).every(squirrelJSON);
  return false;
}

/**
 * Call after the patched Emscripten Module has initialized and a GameScript is
 * running. The caller owns the browser and installs this bridge on its page.
 * Submitted actions are never automatically retried after a timeout.
 */
export function createOpenTTDBridge(wasmModule, { timeoutMs = 10000 } = {}) {
  if (!wasmModule || typeof wasmModule.cwrap !== "function") {
    throw new Error("patched OpenTTD WASM with cwrap is required");
  }
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs <= 0) {
    throw new TypeError("timeoutMs must be a positive integer");
  }
  let revision;
  let submit;
  try {
    revision = wasmModule.cwrap("ParaboxOpenTTDRevision", "string", []);
    submit = wasmModule.cwrap("ParaboxOpenTTDSubmit", "number", ["string"]);
    revision = revision(); // resolves the symbol now, so a stock build fails fast
  } catch (error) {
    throw new Error("patched OpenTTD browser exports are unavailable", { cause: error });
  }
  if (typeof revision !== "string" || !revision) {
    throw new Error("patched OpenTTD browser revision is unavailable");
  }

  const pending = new Map();
  const previousReply = wasmModule.onParaboxOpenTTDReply;
  let closed = false;
  let sequence = 0;

  function onReply(raw) {
    try {
      const reply = JSON.parse(raw);
      if (reply.protocol === PROTOCOL && typeof reply.request_id === "string") {
        const entry = pending.get(reply.request_id);
        if (entry) {
          clearTimeout(entry.timer);
          pending.delete(reply.request_id);
          if (typeof reply.ok !== "boolean") {
            entry.reject(transportError("invalid GameScript outcome; outcome unknown",
                                        reply.request_id, true));
          } else {
            entry.resolve(reply);
          }
        }
      }
    } catch {
      // Other GameScript output may not be a Parabox JSON response.
    } finally {
      if (typeof previousReply === "function") previousReply(raw);
    }
  }
  wasmModule.onParaboxOpenTTDReply = onReply;

  return {
    info() {
      return { protocol: PROTOCOL, version: revision, patched: true, seed: null };
    },
    request(op, args, companyId) {
      if (closed) return Promise.reject(transportError("OpenTTD browser bridge is closed", null, false));
      if (typeof op !== "string" || !op || !args || Array.isArray(args) ||
          typeof args !== "object" || !squirrelJSON(args) ||
          !Number.isSafeInteger(companyId) || companyId < 0) {
        return Promise.reject(transportError("invalid OpenTTD browser request", null, false));
      }
      const randomId = globalThis.crypto?.randomUUID?.().replaceAll("-", "") ??
        `${Date.now().toString(16)}${(++sequence).toString(16)}${Math.random().toString(16).slice(2)}`;
      const requestId = randomId.slice(0, 64);
      const payload = JSON.stringify({ protocol: PROTOCOL, request_id: requestId,
                                       op, args, company_id: companyId });
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          pending.delete(requestId);
          reject(transportError("OpenTTD GameScript reply timed out; outcome unknown",
                                requestId, true));
        }, timeoutMs);
        pending.set(requestId, { resolve, reject, timer });
        try {
          if (submit(payload) !== 1) {
            clearTimeout(timer);
            pending.delete(requestId);
            reject(transportError("patched browser rejected request or has no active GameScript",
                                  requestId, false));
          }
        } catch (error) {
          clearTimeout(timer);
          pending.delete(requestId);
          reject(transportError(`browser submission failed: ${error}`, requestId, true));
        }
      });
    },
    close() {
      if (closed) return;
      closed = true;
      if (wasmModule.onParaboxOpenTTDReply === onReply) {
        wasmModule.onParaboxOpenTTDReply = previousReply;
      }
      for (const [requestId, entry] of pending) {
        clearTimeout(entry.timer);
        entry.reject(transportError("browser bridge closed with an outstanding request",
                                    requestId, true));
      }
      pending.clear();
    },
  };
}
