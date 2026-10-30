/** @param {string | undefined} value */
export function normalizeTeaBasePath(value) {
  const prefix = (value ?? "").trim().replace(/\/+$/, "");
  if (prefix && !/^(?:\/[A-Za-z0-9_-]+)+$/.test(prefix)) {
    throw new Error("NEXT_PUBLIC_TEA_BASE_PATH must be a path such as /aitea");
  }
  return prefix;
}

/** @param {string} path */
export function teaPath(path) {
  const prefix = normalizeTeaBasePath(process.env.NEXT_PUBLIC_TEA_BASE_PATH);
  if (prefix && (path === prefix || path.startsWith(`${prefix}/`))) return path;
  return `${prefix}${path}`;
}

/** Prefix only authenticated tea resources; external and fragment links stay intact.
 * @param {string} url
 */
export function teaResourceUrl(url) {
  return /^\/api\/tea(?:\/|$)/.test(url) ? teaPath(url) : url;
}
