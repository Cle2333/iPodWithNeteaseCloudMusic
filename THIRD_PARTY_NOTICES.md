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
