// Post-build guard: the committed build must carry no absolute local paths
// and no machine identifiers (public repo). Scans every emitted text file.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { hostname, userInfo } from "node:os";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const out = fileURLToPath(new URL("../../pseudolife_memory/web/static/", import.meta.url));
const TEXT = /\.(html|js|css|json|svg|txt|map)$/i;

const patterns = [
  /[A-Za-z]:[\/]{1,2}(Users|home|Documents)/, // Windows absolute paths
  /\/(Users|home)\/[^/\s"']+\//, // POSIX home directories
  /\\Users\\/, // escaped Windows paths inside JS strings
  /node_modules[\/]/, // leaked module paths
  /\.claude[\/]worktrees/,
];
const local = [userInfo().username, hostname()].filter((s) => s && s.length >= 3);

function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const p = join(dir, name);
    return statSync(p).isDirectory() ? walk(p) : [p];
  });
}

let bad = 0;
for (const file of walk(out)) {
  if (!TEXT.test(file)) continue;
  const text = readFileSync(file, "utf8");
  for (const re of patterns) {
    const m = text.match(re);
    if (m) {
      console.error(`check-dist: ${relative(out, file)} matches ${re}: ${m[0]}`);
      bad++;
    }
  }
  for (const id of local) {
    if (text.toLowerCase().includes(id.toLowerCase())) {
      console.error(`check-dist: ${relative(out, file)} contains a local machine identifier`);
      bad++;
    }
  }
}
if (bad) {
  console.error(`check-dist: ${bad} problem(s); the build output must not be committed as is.`);
  process.exit(1);
}
console.log("check-dist: no absolute paths or machine identifiers in the build output.");
