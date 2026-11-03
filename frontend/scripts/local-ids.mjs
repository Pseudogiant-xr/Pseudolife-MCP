// Which of this machine's names check-dist treats as private identifiers.
// The guard exists for the maintainer's own user and host names. A generic
// account name (a container's "dev", a CI "runner", "root") names nobody and
// occurs in ordinary bundle text ("dev" in every https://svelte.dev/e/... URL),
// so checking it would fail every build on that machine while protecting
// nothing; such names are skipped, and the build says so.
const GENERIC = new Set([
  "admin",
  "administrator",
  "build",
  "builder",
  "dev",
  "developer",
  "docker",
  "localhost",
  "node",
  "root",
  "runner",
  "ubuntu",
  "user",
  "vagrant",
]);

/** `{check, skipped}`: the names to search the build for, and the generic ones left out. */
export function localIdentifiers({ username, host, ci }) {
  if (ci) return { check: [], skipped: [] };
  const names = [username, host].filter((s) => typeof s === "string" && s.length >= 3);
  return {
    check: names.filter((s) => !GENERIC.has(s.toLowerCase())),
    skipped: names.filter((s) => GENERIC.has(s.toLowerCase())),
  };
}
