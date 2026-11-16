Enable Pages once, by hand: repository Settings → Pages → Source: GitHub Actions.
After that, every push to main runs .github/workflows/docs.yml, which builds the site, runs the drift test and deploys only if it passes.
