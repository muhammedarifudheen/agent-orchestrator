import path from 'node:path';
import { getEntry } from 'astro:content';
import { provenance } from './provenance';

/**
 * The repo-relative path of the Markdown file a page was built from (e.g. "docs/LIVE_RUN.md"), or null for Starlight's own 404 page,
 * which has no source file. Throws for any other page whose source cannot be determined: a page must always know where it came from.
 */
export async function sourcePath(routeEntry: { id: string }): Promise<string | null> {
  if (routeEntry.id === '404') return null;
  const entry: any = (await getEntry('docs', routeEntry.id)) ?? routeEntry;
  if (!entry.filePath) throw new Error(`Cannot determine the repo source path for ${routeEntry.id}`);
  const source = path.relative(provenance.repoRoot, path.resolve(process.cwd(), entry.filePath)).split(path.sep).join('/');
  if (source.startsWith('..')) throw new Error(`Source of ${routeEntry.id} is outside the repo: ${source}`);
  return source;
}
