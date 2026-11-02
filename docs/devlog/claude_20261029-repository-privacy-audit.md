# Repository privacy audit

The audit reads the two employer identities from private instructions, never from a tracked allowlist. It checks case-insensitive full names, organization handles, domains, short names, and separators between name characters.

The current-content scan covers `git ls-files --cached --others --exclude-standard`. It found no prohibited identifiers in tracked files or nonignored pending additions. No identifying examples needed replacement.

The history scan reads every blob reachable from local refs using `git rev-list --objects --all` and `git cat-file --batch`. It found no matches in 2,385 blobs. Current content and history were checked separately. The private machine-readable scan output stays outside the repository.

This result covers the refs present locally on October 29. It does not prove that deleted remote refs, hosting caches, unreachable objects, ignored runtime files, or another checkout are clean. No history was rewritten and nothing was pushed.
