import { execSync } from 'node:child_process';
import path from 'node:path';

// Repo root = parent of site/ (the build runs with cwd = site/). Evaluated ONCE per build (module level).
const repoRoot = path.resolve(process.cwd(), '..');

function git(cmd: string): string {
  return execSync(cmd, { cwd: repoRoot, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
}

const commit = git('git rev-parse HEAD').trim();
if (!/^[0-9a-f]{40}$/.test(commit)) throw new Error(`git rev-parse HEAD returned: ${commit}`);

export const provenance = {
  commit,
  dirty: git('git status --porcelain').trim().length > 0,
  built: new Date().toISOString().replace(/\.\d{3}Z$/, 'Z'),
  repoRoot,
};
