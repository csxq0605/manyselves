# Electron client

The desktop package contains the same React application as the browser distribution. It never imports Python or starts an Agent Runtime; it connects to a separately deployed FastAPI server.

## Build and test

```powershell
npm.cmd --prefix frontend ci
npm.cmd --prefix desktop ci
npm.cmd --prefix frontend run build
npm.cmd --prefix desktop run verify
```

`desktop/release/` contains the unpacked host-platform package. Production signing and notarization credentials are release-pipeline inputs and must not be committed.

## Security model

- `nodeIntegration=false`, `contextIsolation=true`, `sandbox=true`, `webSecurity=true`.
- Production loads only the packaged React files. A localhost development URL is accepted only through `MANYSELVES_DESKTOP_DEV_URL` during development.
- Unexpected navigation and new windows are denied; explicit HTTPS links open in the operating-system browser.
- The preload exposes only file selection, directory selection, save/open completed downloads, notifications, and encrypted token operations. There is no generic filesystem, shell, process, or IPC method.
- Provider keys remain on the server. The deployment token uses Electron `safeStorage`; when OS encryption is unavailable it remains only in process memory and is never written as plaintext.

On first start, open **设置**, enter the HTTPS server URL and deployment token, then reconnect. The default `http://127.0.0.1:8000` is only a local-development convenience. Native imports upload bytes to the server; server project files remain authoritative and are not synchronized to the client filesystem.

Directory import skips symbolic links and reports skipped entries. Downloads use a partial file and atomic rename, and only a path issued by a completed download in the current profile can be opened. Desktop shortcuts trigger the existing React controls, so they retain API authentication, controller-lease, conflict, and interruption checks.
