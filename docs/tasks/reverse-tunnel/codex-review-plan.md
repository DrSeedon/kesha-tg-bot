## Summary

The plan is workable for an MVP, but I would not implement it as written. There are two blocking issues: the whitelist can be bypassed into arbitrary shell execution, and the reverse tunnel bind address may not match the `permitlisten` restriction.

## Findings

**blocking** - Whitelist prefix matching does not actually prevent arbitrary command execution.  
At [plan.md:152](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/kesha-p0-fix/docs/tasks/reverse-tunnel/plan.md:152), `cmd.startswith(prefix)` allows payloads like `ls /; rm ...`, `cat ~/.ssh/id_ed25519`, or `w...` because [plan.md:155](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/kesha-p0-fix/docs/tasks/reverse-tunnel/plan.md:155) runs the accepted string through SSH as a remote shell command. `shlex.quote()` protects the local VPS shell, not the laptop shell. Also, whitelist entries like `who` and `w` have no trailing space, so `whoami`, `wget ...`, or any command starting with `w` can pass. Fix by using explicit command templates with parsed argv and rejecting shell metacharacters, or better, use a laptop-side forced command/helper that validates and executes known subcommands.

**blocking** - `permitlisten` may reject the tunnel requested by systemd.  
The key restriction allows `permitlisten="127.0.0.1:2222"` at [plan.md:33](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/kesha-p0-fix/docs/tasks/reverse-tunnel/plan.md:33), but the unit requests `-R 2222:localhost:22` at [plan.md:53](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/kesha-p0-fix/docs/tasks/reverse-tunnel/plan.md:53). OpenSSH permit rules are sensitive to the requested listen host. Make them match explicitly, e.g. `-R 127.0.0.1:2222:localhost:22`, or relax/change `permitlisten` to the exact host form OpenSSH sees.

**suggestion** - Timeout handling should kill the SSH subprocess.  
At [plan.md:160](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/kesha-p0-fix/docs/tasks/reverse-tunnel/plan.md:160), `wait_for(proc.communicate())` timing out does not automatically terminate the SSH process or remote command. On timeout, call `proc.kill()` or `proc.terminate()`, then await `proc.communicate()`.

**suggestion** - Document how `systemctl restart orchestra` gets permission.  
The whitelist includes `systemctl restart orchestra` at [plan.md:95](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/kesha-p0-fix/docs/tasks/reverse-tunnel/plan.md:95), but SSH logs in as `maxim`. If `orchestra` is a system service, this likely needs sudoers or a user service form like `systemctl --user restart orchestra`.

## Verdict

Revise before implementation. The architecture is fine for a small trusted MVP, but the command validation needs to be real, and the tunnel bind restriction should be made exact so the service starts reliably.