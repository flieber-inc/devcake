// Browser-safe base64 for uploaded files (chunked — a spread over a large
// Uint8Array overflows the call stack). Shared by the skills import and the
// settings-bundle import.
export async function fileToB64(file) {
  const buf = new Uint8Array(await file.arrayBuffer());
  let s = "";
  for (let i = 0; i < buf.length; i += 0x8000)
    s += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
  return btoa(s);
}

/** UTF-8 string → base64 (same chunking as fileToB64). */
export function textToB64(text) {
  const buf = new TextEncoder().encode(text ?? "");
  let s = "";
  for (let i = 0; i < buf.length; i += 0x8000)
    s += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
  return btoa(s);
}
