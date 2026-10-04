// Copy resources (system.md) next to dist/*.js.
import { mkdirSync, cpSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const src = join(here, "..", "resources");
const dest = join(here, "..", "dist", "resources");

mkdirSync(dest, { recursive: true });
cpSync(src, dest, { recursive: true });
console.log("copied resources →", dest);
