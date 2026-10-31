# Optional OpenTTD 15.3 browser bridge

The published browser WASM has no Parabox control exports. This directory adds **new** exports to a rebuilt OpenTTD 15.3 browser binary and connects them to the same GameScript in `integrations/openttd/game/`. It does not modify game rules or execute Squirrel source supplied by the caller.

## Build and install

1. Check out official OpenTTD tag `15.3` (commit `14ec60f248547d4d062a1160f0fc26d742319888`). Apply `openttd-15.3-browser-bridge.patch` with `git apply --check` then `git apply`. The patch changes only `src/network/network_admin.cpp` under `#ifdef __EMSCRIPTEN__`.

2. Rebuild that source with Emscripten using the browser project's normal build recipe. Add `-sEXPORTED_RUNTIME_METHODS='["cwrap","UTF8ToString"]'` to the final Emscripten link flags. `EMSCRIPTEN_KEEPALIVE` retains the two new C exports: `ParaboxOpenTTDSubmit` and `ParaboxOpenTTDRevision`.

3. Install `integrations/openttd/game/` as a GameScript named `ParaboxBridge` in the browser build's OpenTTD data directory and select it for the game. Start a local game and create a company before sending actions. A network client is rejected by the C export because OpenTTD ignores GameScript events on clients.

4. After the patched Emscripten Module has initialized, install the JS transport on that page:

   ```js
   import { createOpenTTDBridge } from "./bridge.mjs";
   globalThis.paraboxOpenTTDBridge = createOpenTTDBridge(Module);
   ```

   Here `Module` is the **initialized rebuilt** OpenTTD module owned by the browser app. `createOpenTTDBridge` fails fast if its new exports are absent.

An existing browser page evaluator that awaits Promise results can then be bound to the Parabox game contract:

```python
from parabox.games.openttd_browser import OpenTTDBrowserGame

game = OpenTTDBrowserGame(page.evaluate, company_id=0,
                          objectives=["operate a road route"], horizon_ticks=10000)
description = game.describe()
observation = game.observe()
```

The browser remains caller-owned. Each session should bind its own page/game instance and company. `checkpoint()` returns `None`; this bridge does not claim a browser save API. The version comes from the rebuilt binary's `_openttd_revision`; the seed is unknown unless supplied by another verified source. A request timeout has an unknown outcome and is never retried automatically.

## Verification boundary

`git apply --check` passes on the official 15.3 source. Node tests use a fake FFI to check request/reply correlation and timeout behavior. Python tests use a fake page evaluator to check the adapter. A rebuilt WASM binary and live browser/GameScript run have **not** been verified by these checks.

The C++ patch is a derivative patch to GPL-2.0-only OpenTTD source and is distributed under GPL-2.0-only ([license copy](LICENSE-GPL-2.0.txt)). It is available from the repository only; Parabox packages do not include it. See the repository's third-party notices.
