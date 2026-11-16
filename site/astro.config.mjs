import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

// Telemetry: CI sets ASTRO_TELEMETRY_DISABLED=1 (Astro has no config switch for it).
// The deploy workflow supplies SITE_BASE (default '/') and SITE_ORIGIN (optional).
const base = process.env.SITE_BASE || '/';
const origin = process.env.SITE_ORIGIN;

export default defineConfig({
  base,
  ...(origin ? { site: origin } : {}),
  redirects: { '/': '/project-status/' },
  // The docs are shown exactly as written: no typographic rewriting ("smartypants" turns straight quotes into curly ones and
  // "--" into dashes, which alters the repo's text). The drift test (tests/test_site_drift.py) is what caught this.
  markdown: { smartypants: false },
  integrations: [
    starlight({
      title: 'Agent Orchestrator docs',
      // Pagefind search is enabled by default and runs locally.
      credits: false,
      customCss: ['./src/styles/category-banner.css'],
      components: {
        ThemeProvider: './src/components/ThemeProvider.astro',
        PageTitle: './src/components/PageTitle.astro',
        Hero: './src/components/Hero.astro',
        Footer: './src/components/Footer.astro',
      },
      sidebar: [
        { label: 'Project status', slug: 'project-status' },
        { label: 'Audit', slug: 'audit' },
        { label: 'Onboarding', slug: 'onboarding' },
        { label: 'Live run', slug: 'live-run' },
        { label: 'Decisions', slug: 'decisions' },
      ],
    }),
  ],
});
