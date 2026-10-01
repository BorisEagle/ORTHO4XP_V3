# Custom Overpass servers

Keep personal server settings in local files at the Ortho4XP application root
(beside `Ortho4XP.py`). They are loaded when the application starts, before the
configuration window builds its server list. Restart after editing these files.

## NextGIS

Save your NextGIS Overpass API key in `overpass_server_api_key.txt`, as a single
line. The file may alternatively contain the complete Overpass interpreter URL.
The loader registers this service as `NG`, using:

```text
https://overpass.nextgis.com/<api-key>/api/interpreter
```

Select `NG` under `overpass_server_choice`. Existing configs with this selection
continue working once the key file is present. Get the key through the Overpass
API card in your NextGIS account's Software and services section, as described
in the [NextGIS documentation](https://docs.nextgis.com/docs_ngcom/source/services.html#overpass).

The key belongs in the local file, not in Python source files or a Git commit.
Query logs identify the server code and OSM query without printing the URL.

## Other custom servers

Create an optional `overpass_servers.json` containing server codes and complete
interpreter URLs. For example:

```json
{
  "LOCAL": "http://localhost:8080/api/interpreter"
}
```

These entries augment the built-in server list. An entry can override an
existing code, including `NG`. The JSON file takes precedence over the NextGIS
key file. Codes must be nonempty strings other than `random`; URLs must use
HTTP or HTTPS and have a hostname.

Both local files are excluded by `.gitignore`. Copy them separately when moving
your installation to another computer. Replacing Python source files does not
remove the files; replacing the entire application folder still requires
preserving your personal configuration files.

## Missing or invalid settings

If a saved server code no longer exists, Ortho4XP logs a warning and selects an
available server, applying the existing cooldown and retry rotation. It does
not crash with a dictionary `KeyError`. An empty server list returns a failed
download without attempting a network request.

Malformed local configuration is reported without printing its contents or
API keys. Existing valid server entries remain available. A valid endpoint
does not guarantee that a server is online or that an API key is accepted.
