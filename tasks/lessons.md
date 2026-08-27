# Lessons

- A repository-level adapter is not a completed delivery when the user asks to
  experience the harness locally. Verify actual installation and discovery in
  every named client, and repair the implementation if the documented install
  path is not sufficient.
- Discovery support must be tested with the real client, not inferred from a
  readable file. Codex accepted linked skill directories, while one installed
  Codex build failed to apply a linked user-agent profile. Share only assets the
  client demonstrably resolves and keep platform profile files regular.
- Cross-platform installer tests must exercise Windows copy semantics, not only
  platform-specific executable names. A POSIX symlink can remain valid when its
  payload changes, while a Windows copy becomes a drifted regular file and must
  be matched and diagnosed through the copy-specific path.
