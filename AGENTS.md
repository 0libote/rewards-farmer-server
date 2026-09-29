# AGENTS.md

## Runtime and tooling policy

This repository is intentionally Python-first because it wraps and tracks the Python upstream rewards-farmer implementation and its browser automation stack.

- Preserve Python compatibility with upstream unless a change explicitly requires a different architecture.
- Do not port the server, runner, Selenium/Edge integration, configuration, or upstream-facing tests to Bun merely for toolchain consistency with other repositories.
- When adding JavaScript or TypeScript tooling in the future, prefer Bun as the package manager and runtime for that isolated tooling unless the chosen framework requires otherwise.
- Before adding a JavaScript dependency, check whether Bun or a standard Web API already provides the capability.
- Keep cross-runtime boundaries explicit. A future Bun component should communicate with the Python application through a documented interface rather than duplicating upstream behaviour.
- Favour maintainability and upstream parity over maximising Bun usage.
