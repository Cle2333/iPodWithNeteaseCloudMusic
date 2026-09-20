# Third-Party Notices

## iOpenPod (vendored subset)

The `src/iopenpod/` directory in this repository is a **trimmed subset** of
[iOpenPod](https://github.com/TheRealSavi/iOpenPod) by John Gibbons, used under
the MIT License.

Files under `src/iopenpod/` are copied **unmodified** from upstream except for
`src/iopenpod/device/__init__.py`, which was rewritten to expose only the
handful of entry points this project needs instead of importing the whole
device layer.

Vendored scope:
- `itunesdb_shared/`, `itunesdb_parser/`, `itunesdb_writer/` — iTunesDB
  binary format read/write and HASH58/HASH72/HASHAB signing
- `artworkdb_shared/`, `artworkdb_parser/`, `artworkdb_writer/` — ArtworkDB
  and `.ithmb` cover-art encoding
- `device/` — subset: device model tables, capabilities, SysInfo parsing,
  HASH58 checksum wiring, and the FAT32-safe write path
  (`durability.py`, `write_guard.py`, `filesystem_profile.py`, `path_safety.py`)

Upstream commit this was trimmed from:
`a202424` (iOpenPod v1.68.1)

iOpenPod is licensed under the MIT License:

```
MIT License

Copyright (c) John Gibbons

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## calcHashAB.wasm

`src/iopenpod/itunesdb_writer/wasm/calcHashAB.wasm` comes from
[dstaley/hashab](https://github.com/dstaley/hashab) (clean-room HASHAB
implementation). It is unused by this project's Classic-only code path but is
retained alongside the vendored writer for completeness.

## Node.js runtime (bundled in Windows releases)

Windows release archives ship `node_api/node.exe` — an **unmodified** official
Node.js build — so that the bundled NetEase Cloud Music API can run without
requiring the user to install Node. The version is pinned in
`tools/bundle_node_api.py`; the licence text that ships inside the official
archive is redistributed alongside it as `node_api/NODE-LICENSE`.

Node.js is licensed under the MIT License:

```
Copyright Node.js contributors. All rights reserved.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to
deal in the Software without restriction, including without limitation the
rights to use, copy, modify, merge, publish, distribute, sublicense, and/or
sell copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
IN THE SOFTWARE.
```

Node's own licence file also carries the licences of the third-party libraries
compiled into the binary — see `node_api/NODE-LICENSE`.

## NetEase Cloud Music API (api-enhanced) — bundled in Windows releases

Windows release archives ship `node_api/api/` — an unmodified copy of
[`@neteasecloudmusicapienhanced/api`](https://www.npmjs.com/package/@neteasecloudmusicapienhanced/api)
(a fork of Binaryify/NeteaseCloudMusicApi), including its `node_modules`, so the
"download from NetEase Cloud Music" feature works out of the box. The upstream
`LICENSE` is redistributed alongside it as `node_api/api/LICENSE`.

This project does **not** claim any affiliation with NetEase. The bundled API is
an unofficial, community-maintained client; users are responsible for complying
with NetEase's terms of service.

api-enhanced is licensed under the MIT License:

```
The MIT License (MIT)

Copyright (c) 2013-2022 Binaryify

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## CPython runtime (bundled in Windows releases)

Windows release archives embed a CPython runtime via
[serious-python](https://github.com/flet-dev/serious-python) (Apache-2.0), which
in turn bundles a `python-build-standalone` CPython build (PSF licence). The
licence files for both are included in the bundle's `Lib/` and alongside
`serious_python_windows_plugin.dll`.
