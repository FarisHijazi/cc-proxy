# Chrome fleet recheck

The aggregate initially returned four connected browsers: two Mac profiles and two Linux profiles. Both configured account lookups were available.

Loaded the missing third Mac profile into the existing Chrome process with `--profile-directory` and `--no-startup-window`. This did not create a window or navigate a tab. The aggregate then returned all three Mac profiles. A read-only `tabs_context_mcp` request reached the restored profile and reported no MCP groups. This establishes extension access, not current authentication to Google or other sites.

One expected Linux workstation already had Chrome running under its existing service. Its Xorg process was in uninterruptible sleep, blocked in `qxl_fence_wait`. A bounded display query timed out. No process was killed, no desktop or service was restarted, and no machine was rebooted. Its browser remains absent from the aggregate.

Two other servers are not expected signed-in desktop browsers. One has the Chrome binary installed but no desktop environment, display server, usable profile, or running browser. The other runs the multi-user target, has an Xvfb display for headless applications, and has no running Chrome or normal desktop session. The private browser map now excludes these from `expected_hosts` and records the diagnostic distinction in `headless_hosts`.

The restored Mac device now has a confirmed host and owning Claude-account alias in the private identity map. Synced that map to the proxy host so native labels can use the existing host-plus-email formatter. No credentials were copied or refreshed during this Chrome check.

The final aggregate returned five browsers: the three Mac profiles plus two Linux VM browsers. Both account lookups were available. The third expected Linux VM browser is still missing because of the stuck display server described above. Recovering it needed a display or VM restart.

With approval, a display-manager restart was attempted and canceled. A guest shutdown then hung on the stuck display process: SSH and the guest agent stopped while the kernel still answered ping. After explicit approval, the VM was force-reset from its hypervisor. Xorg came back in a normal state, the display answered, and the existing Chrome service reconnected. The aggregate then listed six browsers: three Mac profiles and three Linux VM browsers. The new device ID was confirmed in that VM's extension storage.

No commit or push was made.
