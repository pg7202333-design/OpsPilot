# Publish OpsPilot to GitHub (macOS / VS Code)

1. Extract this ZIP into a new folder and open that folder in VS Code.
2. In the integrated terminal, run the three test commands from the README. Keep your local results.
3. Review `git status` and confirm that `.env`, API keys, personal logs, and local result files are not staged.
4. Create a new **public** repository on GitHub named `OpsPilot-AI` (or your preferred name). Do not initialize it with a README, because this folder already contains one.
5. From the project folder, run the following commands, replacing the URL with the HTTPS URL shown by GitHub:

```bash
git init
git add README.md .gitignore docs opspilot shop tests shop_tests
git status
git commit -m "Prepare OpsPilot v6 portfolio release"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/OpsPilot-AI.git
git push -u origin main
```

6. Open the repository page and verify the README renders, the docs are present, and no secrets were uploaded.
7. In GitHub, create a release/tag such as `v6.0.0` only after the clean-checkout checks pass.

If `git remote add origin` says the remote already exists, inspect it with `git remote -v` before changing anything. Never paste a private API key into a README, source file, terminal screenshot, or public issue.
