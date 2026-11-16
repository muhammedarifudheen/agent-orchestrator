import { defineCollection } from 'astro:content';
import { glob } from 'astro/loaders';
import { docsSchema } from '@astrojs/starlight/schema';

// The pages ARE the repo's own Markdown files, read in place (no copies).
const SOURCES: Record<string, { slug: string; title: string }> = {
  'PROJECT_STATUS.md': { slug: 'project-status', title: 'Project status' },
  'AUDIT.md': { slug: 'audit', title: 'Audit' },
  'docs/ONBOARDING.md': { slug: 'onboarding', title: 'Onboarding' },
  'docs/LIVE_RUN.md': { slug: 'live-run', title: 'Live run' },
  'CLAUDE.md': { slug: 'decisions', title: 'Decisions' },
};

function repoLoader() {
  const inner = glob({
    base: '..',
    pattern: Object.keys(SOURCES),
    generateId: ({ entry }) => {
      const hit = SOURCES[entry.split('\\').join('/')];
      if (!hit) throw new Error(`Unexpected source file: ${entry}`);
      return hit.slug;
    },
  });
  return {
    name: 'repo-markdown-loader',
    load: async (context: any) => {
      const parseData = async (props: any) => {
        const rel = String(props.id);
        const meta = Object.values(SOURCES).find((s) => s.slug === rel)!;
        const data = await context.parseData({
          ...props,
          data: { title: meta.title, ...props.data },
        });
        return data;
      };
      await inner.load({ ...context, parseData });
      for (const s of Object.values(SOURCES)) {
        if (!context.store.has(s.slug)) {
          throw new Error(`Required source for /${s.slug}/ is missing or matched nothing`);
        }
      }
    },
  };
}

export const collections = {
  docs: defineCollection({ loader: repoLoader(), schema: docsSchema() }),
};
