# Native OpenTTD bridge

This adapter uses the **native OpenTTD 15.3 dedicated server**. The unmodified published `swords02/openttd-online` browser build exports no gameplay command API and cannot be controlled by this adapter. The source for the native API and wire protocol was checked against [OpenTTD 15.3](https://github.com/OpenTTD/OpenTTD/tree/14ec60f248547d4d062a1160f0fc26d742319888).

## Start an isolated server

Use a directory outside Git, for example `/tmp/parabox-openttd`. Put an OpenGFX base graphics set, `no_sound.obs`, `no_music.obm`, and `english.lng` from the OpenTTD distribution into its `baseset/` and `lang/` directories. Copy the two files in [`game/`](game/) into `game/ParaboxBridge/`. Select the script and set a local AdminPort in `openttd.cfg`:

```ini
[network]
server_admin_port = 39897
admin_password = CHANGE_ME
allow_insecure_admin_login = true
server_game_type = local

[game_scripts]
ParaboxBridge =
```

Run from that directory; the `-D` port is for game clients, while `server_admin_port` is the bridge's port:

```sh
openttd -D 127.0.0.1:39899 -c "$PWD/openttd.cfg" -X -x \
  -I OpenGFX -s null -m null -G 73 -Q
```

The server needs an **existing company**. Load a save with one, join through a client to create one, or use the dedicated console's `start_ai` command to create an idle company for a local smoke test. The smoke test used company ID `0` after `start_ai`. The bridge refuses mutating commands without a valid company. Keep insecure password authentication restricted to loopback.

## Record one action

From the Parabox repository root, with `OPENTTD_ADMIN_PASSWORD` set in your shell environment:

```sh
python -m examples.openttd_game --port 39897 --company-id 0 \
  --objective "Operate a profitable bus route" \
  --save-dir /tmp/parabox-openttd/save \
  --output runs/game-interface-openttd \
  --action '{"name":"inspect_town","arguments":{"town_id":0}}'
```

The example creates a fresh `GameSession`, writes a unique JSON trace, and copies verified native `.sav` checkpoints when `--save-dir` points to the server's local save directory. Each trace records command request/reply, native start/end ticks, the next live observation, and checkpoint hashes. OpenTTD's clock keeps running between observations. If a command reply is lost, the outcome is marked unknown; do not retry a mutating command blindly.

The script returns bounded pages for towns, engines, vehicles, and stations. Native town actions include `bribe`; a detected bribe may still return `native_return=true`, so the response records detailed town rating and action availability before and after instead of asserting a favorable outcome.
