# Cursor bridge reuse prototype

## Existing component checked

The MIT-licensed [`pi-cursor-provider`](https://github.com/ndraiman/pi-cursor-provider) package, version 0.1.11, contains a native MCP bridge. Its exported `startProxy` function can run outside Pi with a small wrapper and `tsx`, but upstream documents it as a Pi extension rather than a standalone proxy.

The package was installed unmodified in an isolated test directory with lifecycle scripts disabled and an exact version pin. Its bridge and token-redaction code were inspected before execution. The existing Cursor CLI access token was read on the inference host. It was not copied to another host, logged, or refreshed.

## Live result

The package started its loopback HTTP listener and opened the Cursor transport. It registered client tool definitions and answered blob/context requests. It did not complete a tool round trip.

A request using the same bare Opus model id as the existing proxy failed with a Connect `not_found` error. What resource the server could not find is unresolved. A control using a model id from the package's own catalog progressed to an exec request, then stalled.

The control's exec message contained unknown fields 36 and 55. The shipped schema did not recognize the tool-call variant. Its stderr reported `UNHANDLED exec case: "undefined". Bridge may stall.` No tool calls reached the HTTP client before its timeout. Marker-dependent continuation, error-result continuation, and a real Claude client through this package therefore did not pass. The absence of an unadvertised call in an otherwise failed exchange is not a meaningful safety pass.

This is evidence against immediately replacing the current Rust provider with this package. It does not establish the meaning of either unknown field. The existing Rust schema is also incomplete, but identifying a shared gap is not a repaired implementation.

## Research gate and cleanup

The user requested `/deep-research` on existing Cursor bridge and proxy implementations before repair. The prototype was paused, and the research workflow was launched. No Cursor provider source was replaced and no production service was restarted. All owned test children were stopped by captured PIDs, with the test-root process scan checked afterward. Cursor remains excluded from both live fallback chains.

Private raw protocol captures, test wrappers, and the dependency install remain in session-specific diagnostic storage. They are not copied into this repository. The next implementation choice depends on the research findings and a passing result-dependent tool test, not a package README claim.
